#!/bin/bash

# Yuxi Initialization Script for Bash/Linux/macOS
# This script helps set up the environment for the Yuxi project

set -e

generate_hex() {
    local length="$1"
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$length"
    else
        tr -dc 'a-f0-9' < /dev/urandom | head -c $((length * 2))
    fi
}

set_env_value() {
    local name="$1"
    local value="$2"

    if grep -Eq "^${name}=" .env; then
        ENV_VALUE="$value" awk -v name="$name" '
            $0 ~ "^" name "=" {
                if (!updated) {
                    print name "=" ENVIRON["ENV_VALUE"]
                    updated = 1
                }
                next
            }
            { print }
        ' .env > .env.tmp
        mv .env.tmp .env
    else
        printf '\n%s=%s\n' "$name" "$value" >> .env
    fi
}

ensure_required_api_env() {
    if grep -Eq '^SILICONFLOW_API_KEY=.+' .env; then
        return
    fi

    echo "SILICONFLOW_API_KEY is missing in .env."
    while true; do
        read -s -p "Please enter your SILICONFLOW_API_KEY: " SILICONFLOW_API_KEY
        echo ""
        if [ -n "$SILICONFLOW_API_KEY" ]; then
            break
        fi
        echo "❌ API Key cannot be empty. Please try again."
    done
    set_env_value "SILICONFLOW_API_KEY" "$SILICONFLOW_API_KEY"
}

ensure_jwt_env() {
    if ! grep -Eq '^JWT_SECRET_KEY=.+' .env; then
        echo "JWT_SECRET_KEY is missing in .env."
        read -s -p "Please enter your JWT_SECRET_KEY (press Enter to auto-generate): " JWT_SECRET_KEY
        echo ""
        if [ -z "$JWT_SECRET_KEY" ]; then
            JWT_SECRET_KEY=$(generate_hex 32)
            echo "Generated JWT_SECRET_KEY and saved it to .env."
        fi

        set_env_value "JWT_SECRET_KEY" "$JWT_SECRET_KEY"
    fi

    if ! grep -Eq '^YUXI_INSTANCE_ID=.+' .env; then
        echo "YUXI_INSTANCE_ID is missing in .env."
        read -p "Please enter your YUXI_INSTANCE_ID (press Enter to auto-generate): " YUXI_INSTANCE_ID
        if [ -z "$YUXI_INSTANCE_ID" ]; then
            YUXI_INSTANCE_ID="instance-$(generate_hex 8)"
            echo "Generated YUXI_INSTANCE_ID and saved it to .env."
        fi

        set_env_value "YUXI_INSTANCE_ID" "$YUXI_INSTANCE_ID"
    fi
}

ensure_sandbox_env() {
    if grep -Eq '^SANDBOX_PROVISIONER_TOKEN=.+' .env; then
        return
    fi

    echo "SANDBOX_PROVISIONER_TOKEN is missing in .env."
    read -s -p "Please enter your SANDBOX_PROVISIONER_TOKEN (press Enter to auto-generate): " SANDBOX_PROVISIONER_TOKEN
    echo ""
    if [ -z "$SANDBOX_PROVISIONER_TOKEN" ]; then
        SANDBOX_PROVISIONER_TOKEN=$(generate_hex 32)
        echo "Generated SANDBOX_PROVISIONER_TOKEN and saved it to .env."
    fi

    set_env_value "SANDBOX_PROVISIONER_TOKEN" "$SANDBOX_PROVISIONER_TOKEN"
}

env_value() {
    local name="$1"
    awk -F= -v name="$name" '
        $1 == name {
            sub(/^[^=]*=/, "")
            value = $0
        }
        END {
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            quote = substr(value, 1, 1)
            if ((quote == "\"") || (quote == "\047")) {
                rest = substr(value, 2)
                closing = index(rest, quote)
                suffix = closing ? substr(rest, closing + 1) : ""
                if (closing && suffix ~ /^[[:space:]]*(#.*)?$/) {
                    print substr(rest, 1, closing - 1)
                } else {
                    print value
                }
            } else {
                sub(/[[:space:]]+#.*$/, "", value)
                sub(/[[:space:]]+$/, "", value)
                print value
            }
        }
    ' .env
}

directory_has_data() {
    local path="$1"
    local first_entry
    if [ ! -e "$path" ]; then
        return 1
    fi
    if [ ! -d "$path" ] || [ ! -r "$path" ] || [ ! -x "$path" ]; then
        echo "❌ Cannot safely inspect persisted data path: ${path}." >&2
        exit 1
    fi
    if ! first_entry=$(find "$path" -mindepth 1 -maxdepth 1 -print -quit); then
        echo "❌ Cannot safely inspect persisted data path: ${path}." >&2
        exit 1
    fi
    [ -n "$first_entry" ]
}

ensure_service_credential() {
    local name="$1"
    local public_default="$2"
    local byte_count="$3"
    local data_path="$4"
    local current_value
    current_value=$(env_value "$name")

    if [ -n "$current_value" ] && [ "$current_value" != "$public_default" ] && [[ "$current_value" != *'$'* ]]; then
        return
    fi

    if directory_has_data "$data_path"; then
        echo "❌ ${name} is missing or insecure while ${data_path} contains persisted data."
        echo "Rotate the service credential first, then update .env. See docs/advanced/deployment.md."
        exit 1
    fi

    set_env_value "$name" "$(generate_hex "$byte_count")"
    echo "Generated secure ${name} and saved it to .env."
}

ensure_service_credentials() {
    ensure_service_credential "POSTGRES_PASSWORD" "postgres" 32 "docker/volumes/postgresql"
    ensure_service_credential "NEO4J_PASSWORD" "0123456789" 32 "docker/volumes/neo4j/data"
    ensure_service_credential "MINIO_ACCESS_KEY" "minioadmin" 10 "docker/volumes/milvus/minio"
    ensure_service_credential "MINIO_SECRET_KEY" "minioadmin" 32 "docker/volumes/milvus/minio"
}

ensure_new_install_has_no_service_data() {
    local data_path
    for data_path in docker/volumes/postgresql docker/volumes/neo4j/data docker/volumes/milvus/minio; do
        if directory_has_data "$data_path"; then
            echo "❌ .env is missing while ${data_path} contains persisted data."
            echo "Restore the matching credentials before initialization. See docs/advanced/deployment.md."
            exit 1
        fi
    done
}

skip_existing_image() {
    local image="$1"

    if ! docker image inspect "$image" >/dev/null 2>&1; then
        return 1
    fi

    echo "⏭️  ${image} already exists. Skipping pull."
    return 0
}

echo "🚀 Initializing Yuxi project..."
echo "=================================="

# Check if .env file exists
if [ -f ".env" ]; then
    echo "✅ .env file already exists. Checking required settings."
    ensure_required_api_env
    ensure_jwt_env
    ensure_sandbox_env
    ensure_service_credentials
else
    ensure_new_install_has_no_service_data
    echo "📝 .env file not found. Let's set up your environment variables."
    echo ""

    # Get SILICONFLOW_API_KEY
    echo "🔑 SiliconFlow API Key required"
    echo "Get your API key from: https://cloud.siliconflow.cn/i/Eo5yTHGJ"
    while true; do
        read -s -p "Please enter your SILICONFLOW_API_KEY: " SILICONFLOW_API_KEY
        echo ""
        if [ -z "$SILICONFLOW_API_KEY" ]; then
            echo "❌ API Key cannot be empty. Please try again."
        else
            break
        fi
    done

    # Get Web Search Provider and API Key (optional)
    echo ""
    echo "🔍 Web Search Provider (optional)"
    echo "1) doubao (Doubao Custom Search)"
    echo "2) tavily (Tavily Search)"
    read -p "Please select web search provider (1 for doubao, 2 for tavily, press Enter to skip): " SEARCH_CHOICE

    WEB_SEARCH_PROVIDER=""
    DOUBAO_SEARCH_API_KEY=""
    TAVILY_API_KEY=""

    if [ "$SEARCH_CHOICE" = "1" ] || [ "$SEARCH_CHOICE" = "doubao" ]; then
        WEB_SEARCH_PROVIDER="doubao"
        echo "Get your Doubao API Key from Volcengine Console https://console.volcengine.com/search-infinity/api-key"
        read -s -p "Please enter your DOUBAO_SEARCH_API_KEY: " DOUBAO_SEARCH_API_KEY
        echo ""
    elif [ "$SEARCH_CHOICE" = "2" ] || [ "$SEARCH_CHOICE" = "tavily" ]; then
        WEB_SEARCH_PROVIDER="tavily"
        echo "Get your Tavily API key from: https://app.tavily.com/"
        read -s -p "Please enter your TAVILY_API_KEY: " TAVILY_API_KEY
        echo ""
    fi

    echo ""
    echo "JWT security settings"
    read -s -p "Please enter your JWT_SECRET_KEY (press Enter to auto-generate): " JWT_SECRET_KEY
    echo ""
    if [ -z "$JWT_SECRET_KEY" ]; then
        JWT_SECRET_KEY=$(generate_hex 32)
        echo "Generated JWT_SECRET_KEY and saved it to .env."
    fi

    read -p "Please enter your YUXI_INSTANCE_ID (press Enter to auto-generate): " YUXI_INSTANCE_ID
    if [ -z "$YUXI_INSTANCE_ID" ]; then
        YUXI_INSTANCE_ID="instance-$(generate_hex 8)"
        echo "Generated YUXI_INSTANCE_ID and saved it to .env."
    fi

    read -s -p "Please enter your SANDBOX_PROVISIONER_TOKEN (press Enter to auto-generate): " SANDBOX_PROVISIONER_TOKEN
    echo ""
    if [ -z "$SANDBOX_PROVISIONER_TOKEN" ]; then
        SANDBOX_PROVISIONER_TOKEN=$(generate_hex 32)
        echo "Generated SANDBOX_PROVISIONER_TOKEN and saved it to .env."
    fi

    POSTGRES_PASSWORD=$(generate_hex 32)
    NEO4J_PASSWORD=$(generate_hex 32)
    MINIO_ACCESS_KEY=$(generate_hex 10)
    MINIO_SECRET_KEY=$(generate_hex 32)

    # Create .env file
    cat > .env << EOF
# SiliconFlow API Key (required)
SILICONFLOW_API_KEY=${SILICONFLOW_API_KEY}

# Web Search Provider settings
EOF

    if [ -n "$WEB_SEARCH_PROVIDER" ]; then
        echo "WEB_SEARCH_PROVIDER=${WEB_SEARCH_PROVIDER}" >> .env
    fi
    if [ -n "$DOUBAO_SEARCH_API_KEY" ]; then
        echo "DOUBAO_SEARCH_API_KEY=${DOUBAO_SEARCH_API_KEY}" >> .env
    fi
    if [ -n "$TAVILY_API_KEY" ]; then
        echo "TAVILY_API_KEY=${TAVILY_API_KEY}" >> .env
    fi

    cat >> .env << EOF

# JWT security settings
JWT_SECRET_KEY=${JWT_SECRET_KEY}
YUXI_INSTANCE_ID=${YUXI_INSTANCE_ID}
SANDBOX_PROVISIONER_TOKEN=${SANDBOX_PROVISIONER_TOKEN}

# Service credentials generated for this installation
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
NEO4J_PASSWORD=${NEO4J_PASSWORD}
MINIO_ACCESS_KEY=${MINIO_ACCESS_KEY}
MINIO_SECRET_KEY=${MINIO_SECRET_KEY}
EOF

    echo "✅ .env file created successfully!"
fi

echo ""
echo "📦 Pulling Docker images..."
echo "========================="

# List of Docker images to pull
images=(
    "python:3.13-slim"
    "node:24-slim"
    "node:24-alpine"
    "milvusdb/milvus:v2.5.6"
    "neo4j:5.26"
    "minio/minio:RELEASE.2023-03-20T20-16-18Z"
    "ghcr.io/astral-sh/uv:0.11.26"
    "nginx:alpine"
    "quay.io/coreos/etcd:v3.5.5"
    "postgres:16"
    "redis:7-alpine"
)

# Pull each image
for image in "${images[@]}"; do
    if skip_existing_image "$image"; then
        continue
    fi

    echo "🔄 Pulling ${image}..."
    if bash scripts/pull_image.sh "$image"; then
        echo "✅ Successfully pulled ${image}"
    else
        echo "❌ Failed to pull ${image}"
        exit 1
    fi
done

sandbox_image="enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest"
if ! skip_existing_image "$sandbox_image"; then
    echo "🔄 Pulling ${sandbox_image}..."
    docker pull "$sandbox_image"
    echo "✅ Successfully pulled ${sandbox_image}"
fi

echo ""
echo "🎉 Initialization complete!"
echo "=========================="
echo "You can now run: docker compose up -d --build"
echo "This will start all services in development mode with hot-reload enabled."
