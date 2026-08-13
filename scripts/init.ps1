# Yuxi Initialization Script for PowerShell
# This script helps set up the environment for the Yuxi project
# Note: API keys will be visible during input - use with care

function New-RandomHex($ByteCount) {
    $bytes = [byte[]]::new($ByteCount)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
        return -join ($bytes | ForEach-Object { $_.ToString("x2") })
    } finally {
        $rng.Dispose()
    }
}

function Test-EnvValue($Name) {
    return [bool](Select-String -Path ".env" -Pattern "^$Name=.+" -Quiet)
}

function Set-EnvValue($Name, $Value) {
    $escapedName = [regex]::Escape($Name)
    if (Select-String -Path ".env" -Pattern "^$escapedName=" -Quiet) {
        $written = $false
        $envContent = Get-Content -Path ".env" | ForEach-Object {
            if ($_ -match "^$escapedName=") {
                if (-not $written) {
                    "$Name=$Value"
                    $written = $true
                }
            } else {
                $_
            }
        }
        $envContent | Set-Content -Path ".env" -Encoding UTF8
    } else {
        "`n$Name=$Value" | Add-Content -Path ".env" -Encoding UTF8
    }
}

function Ensure-RequiredApiEnv {
    if (Test-EnvValue "SILICONFLOW_API_KEY") {
        return
    }

    Write-Host "SILICONFLOW_API_KEY is missing in .env." -ForegroundColor Yellow
    do {
        $SILICONFLOW_API_KEY = Read-Host "Please enter your SILICONFLOW_API_KEY"
        if ([string]::IsNullOrEmpty($SILICONFLOW_API_KEY)) {
            Write-Host "❌ API Key cannot be empty. Please try again." -ForegroundColor Red
        }
    } while ([string]::IsNullOrEmpty($SILICONFLOW_API_KEY))
    Set-EnvValue "SILICONFLOW_API_KEY" $SILICONFLOW_API_KEY
}

function Ensure-JwtEnv {
    if (-not (Test-EnvValue "JWT_SECRET_KEY")) {
        Write-Host "JWT_SECRET_KEY is missing in .env." -ForegroundColor Yellow
        $JWT_SECRET_KEY = Read-Host "Please enter your JWT_SECRET_KEY (press Enter to auto-generate)"
        if ([string]::IsNullOrEmpty($JWT_SECRET_KEY)) {
            $JWT_SECRET_KEY = New-RandomHex 32
            Write-Host "Generated JWT_SECRET_KEY and saved it to .env." -ForegroundColor Green
        }

        Set-EnvValue "JWT_SECRET_KEY" $JWT_SECRET_KEY
    }

    if (-not (Test-EnvValue "YUXI_INSTANCE_ID")) {
        Write-Host "YUXI_INSTANCE_ID is missing in .env." -ForegroundColor Yellow
        $YUXI_INSTANCE_ID = Read-Host "Please enter your YUXI_INSTANCE_ID (press Enter to auto-generate)"
        if ([string]::IsNullOrEmpty($YUXI_INSTANCE_ID)) {
            $YUXI_INSTANCE_ID = "instance-$(New-RandomHex 8)"
            Write-Host "Generated YUXI_INSTANCE_ID and saved it to .env." -ForegroundColor Green
        }

        Set-EnvValue "YUXI_INSTANCE_ID" $YUXI_INSTANCE_ID
    }
}

function Ensure-SandboxEnv {
    if (Test-EnvValue "SANDBOX_PROVISIONER_TOKEN") {
        return
    }

    Write-Host "SANDBOX_PROVISIONER_TOKEN is missing in .env." -ForegroundColor Yellow
    $SANDBOX_PROVISIONER_TOKEN = Read-Host "Please enter your SANDBOX_PROVISIONER_TOKEN (press Enter to auto-generate)"
    if ([string]::IsNullOrEmpty($SANDBOX_PROVISIONER_TOKEN)) {
        $SANDBOX_PROVISIONER_TOKEN = New-RandomHex 32
        Write-Host "Generated SANDBOX_PROVISIONER_TOKEN and saved it to .env." -ForegroundColor Green
    }

    Set-EnvValue "SANDBOX_PROVISIONER_TOKEN" $SANDBOX_PROVISIONER_TOKEN
}

function Get-EnvValue($Name) {
    $escapedName = [regex]::Escape($Name)
    $line = Get-Content -Path ".env" | Where-Object { $_ -match "^$escapedName=" } | Select-Object -Last 1
    if ($null -eq $line) {
        return ""
    }
    $value = $line.Substring($line.IndexOf("=") + 1).Trim()
    if ($value.StartsWith('"') -or $value.StartsWith("'")) {
        $quote = $value.Substring(0, 1)
        $closing = $value.IndexOf($quote, 1)
        if ($closing -gt 0 -and $value.Substring($closing + 1) -match '^\s*(#.*)?$') {
            return $value.Substring(1, $closing - 1)
        }
    }
    return ($value -replace '\s+#.*$', '').TrimEnd()
}

function Test-DirectoryHasData($Path) {
    if (-not (Test-Path $Path)) {
        return $false
    }
    try {
        return $null -ne (Get-ChildItem -Path $Path -Force -ErrorAction Stop | Select-Object -First 1)
    } catch {
        Write-Host "❌ Cannot safely inspect persisted data path: $Path." -ForegroundColor Red
        exit 1
    }
}

function Ensure-ServiceCredential($Name, $PublicDefault, $ByteCount, $DataPath) {
    $currentValue = Get-EnvValue $Name
    if (-not [string]::IsNullOrEmpty($currentValue) -and $currentValue -ne $PublicDefault -and -not $currentValue.Contains('$')) {
        return
    }

    if (Test-DirectoryHasData $DataPath) {
        Write-Host "❌ $Name is missing or insecure while $DataPath contains persisted data." -ForegroundColor Red
        Write-Host "Rotate the service credential first, then update .env. See docs/advanced/deployment.md." -ForegroundColor Red
        exit 1
    }

    Set-EnvValue $Name (New-RandomHex $ByteCount)
    Write-Host "Generated secure $Name and saved it to .env." -ForegroundColor Green
}

function Ensure-ServiceCredentials {
    Ensure-ServiceCredential "POSTGRES_PASSWORD" "postgres" 32 "docker/volumes/postgresql"
    Ensure-ServiceCredential "NEO4J_PASSWORD" "0123456789" 32 "docker/volumes/neo4j/data"
    Ensure-ServiceCredential "MINIO_ACCESS_KEY" "minioadmin" 10 "docker/volumes/milvus/minio"
    Ensure-ServiceCredential "MINIO_SECRET_KEY" "minioadmin" 32 "docker/volumes/milvus/minio"
}

function Confirm-NewInstallHasNoServiceData {
    foreach ($dataPath in @("docker/volumes/postgresql", "docker/volumes/neo4j/data", "docker/volumes/milvus/minio")) {
        if (Test-DirectoryHasData $dataPath) {
            Write-Host "❌ .env is missing while $dataPath contains persisted data." -ForegroundColor Red
            Write-Host "Restore the matching credentials before initialization. See docs/advanced/deployment.md." -ForegroundColor Red
            exit 1
        }
    }
}

function Test-SkipExistingImage($ImageTag) {
    & docker image inspect $ImageTag *> $null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }

    Write-Host "⏭️  $ImageTag already exists. Skipping pull." -ForegroundColor Green
    return $true
}

Write-Host "🚀 Initializing Yuxi project..." -ForegroundColor Cyan
Write-Host "==================================" -ForegroundColor Cyan

# Check if .env file exists
if (Test-Path ".env") {
    Write-Host "✅ .env file already exists. Checking required settings." -ForegroundColor Green
    Ensure-RequiredApiEnv
    Ensure-JwtEnv
    Ensure-SandboxEnv
    Ensure-ServiceCredentials
} else {
    Confirm-NewInstallHasNoServiceData
    Write-Host "📝 .env file not found. Let's set up your environment variables." -ForegroundColor Yellow
    Write-Host ""

    # Get SILICONFLOW_API_KEY
    Write-Host "🔑 SiliconFlow API Key required" -ForegroundColor Yellow
    Write-Host "Get your API key from: https://cloud.siliconflow.cn/i/Eo5yTHGJ" -ForegroundColor Blue
    Write-Host "Note: Press Ctrl+C at any time to cancel" -ForegroundColor Gray
    Write-Host ""

    do {
        $apiKey = Read-Host "Please enter your SILICONFLOW_API_KEY"
        if ([string]::IsNullOrEmpty($apiKey)) {
            Write-Host "❌ API Key cannot be empty. Please try again." -ForegroundColor Red
        }
    } while ([string]::IsNullOrEmpty($apiKey))

    # Get Web Search Provider and API Key (optional)
    Write-Host ""
    Write-Host "🔍 Web Search Provider (optional)" -ForegroundColor Yellow
    Write-Host "1) doubao (Doubao Custom Search)" -ForegroundColor Blue
    Write-Host "2) tavily (Tavily Search)" -ForegroundColor Blue

    $SEARCH_CHOICE = Read-Host "Please select web search provider (1 for doubao, 2 for tavily, press Enter to skip)"

    $WEB_SEARCH_PROVIDER = ""
    $DOUBAO_SEARCH_API_KEY = ""
    $TAVILY_API_KEY = ""

    if ($SEARCH_CHOICE -eq "1" -or $SEARCH_CHOICE -eq "doubao") {
        $WEB_SEARCH_PROVIDER = "doubao"
        Write-Host "Get your Doubao API Key from Volcengine Console" -ForegroundColor Blue
        $DOUBAO_SEARCH_API_KEY = Read-Host "Please enter your DOUBAO_SEARCH_API_KEY"
    } elseif ($SEARCH_CHOICE -eq "2" -or $SEARCH_CHOICE -eq "tavily") {
        $WEB_SEARCH_PROVIDER = "tavily"
        Write-Host "Get your Tavily API key from: https://app.tavily.com/" -ForegroundColor Blue
        $TAVILY_API_KEY = Read-Host "Please enter your TAVILY_API_KEY"
    }

    Write-Host ""
    Write-Host "JWT security settings" -ForegroundColor Yellow
    $JWT_SECRET_KEY = Read-Host "Please enter your JWT_SECRET_KEY (press Enter to auto-generate)"
    if ([string]::IsNullOrEmpty($JWT_SECRET_KEY)) {
        $JWT_SECRET_KEY = New-RandomHex 32
        Write-Host "Generated JWT_SECRET_KEY and saved it to .env." -ForegroundColor Green
    }

    $YUXI_INSTANCE_ID = Read-Host "Please enter your YUXI_INSTANCE_ID (press Enter to auto-generate)"
    if ([string]::IsNullOrEmpty($YUXI_INSTANCE_ID)) {
        $YUXI_INSTANCE_ID = "instance-$(New-RandomHex 8)"
        Write-Host "Generated YUXI_INSTANCE_ID and saved it to .env." -ForegroundColor Green
    }

    $SANDBOX_PROVISIONER_TOKEN = Read-Host "Please enter your SANDBOX_PROVISIONER_TOKEN (press Enter to auto-generate)"
    if ([string]::IsNullOrEmpty($SANDBOX_PROVISIONER_TOKEN)) {
        $SANDBOX_PROVISIONER_TOKEN = New-RandomHex 32
        Write-Host "Generated SANDBOX_PROVISIONER_TOKEN and saved it to .env." -ForegroundColor Green
    }

    $POSTGRES_PASSWORD = New-RandomHex 32
    $NEO4J_PASSWORD = New-RandomHex 32
    $MINIO_ACCESS_KEY = New-RandomHex 10
    $MINIO_SECRET_KEY = New-RandomHex 32

    # Create .env file
    $envContent = @"
# SiliconFlow API Key (required)
SILICONFLOW_API_KEY=$apiKey

# Web Search Provider settings
"@

    if (-not [string]::IsNullOrEmpty($WEB_SEARCH_PROVIDER)) {
        $envContent += "`nWEB_SEARCH_PROVIDER=$WEB_SEARCH_PROVIDER"
    }
    if (-not [string]::IsNullOrEmpty($DOUBAO_SEARCH_API_KEY)) {
        $envContent += "`nDOUBAO_SEARCH_API_KEY=$DOUBAO_SEARCH_API_KEY"
    }
    if (-not [string]::IsNullOrEmpty($TAVILY_API_KEY)) {
        $envContent += "`nTAVILY_API_KEY=$TAVILY_API_KEY"
    }

    $envContent += @"

# JWT security settings
JWT_SECRET_KEY=$JWT_SECRET_KEY
YUXI_INSTANCE_ID=$YUXI_INSTANCE_ID
SANDBOX_PROVISIONER_TOKEN=$SANDBOX_PROVISIONER_TOKEN

# Service credentials generated for this installation
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
NEO4J_PASSWORD=$NEO4J_PASSWORD
MINIO_ACCESS_KEY=$MINIO_ACCESS_KEY
MINIO_SECRET_KEY=$MINIO_SECRET_KEY
"@

    $envContent | Out-File -FilePath ".env" -Encoding UTF8
    Write-Host "✅ .env file created successfully!" -ForegroundColor Green

    # Clear the variables from memory
    Remove-Variable -Name "apiKey" -ErrorAction SilentlyContinue
    Remove-Variable -Name "WEB_SEARCH_PROVIDER" -ErrorAction SilentlyContinue
    Remove-Variable -Name "DOUBAO_SEARCH_API_KEY" -ErrorAction SilentlyContinue
    Remove-Variable -Name "TAVILY_API_KEY" -ErrorAction SilentlyContinue
    Remove-Variable -Name "JWT_SECRET_KEY" -ErrorAction SilentlyContinue
    Remove-Variable -Name "YUXI_INSTANCE_ID" -ErrorAction SilentlyContinue
    Remove-Variable -Name "SANDBOX_PROVISIONER_TOKEN" -ErrorAction SilentlyContinue
    Remove-Variable -Name "POSTGRES_PASSWORD" -ErrorAction SilentlyContinue
    Remove-Variable -Name "NEO4J_PASSWORD" -ErrorAction SilentlyContinue
    Remove-Variable -Name "MINIO_ACCESS_KEY" -ErrorAction SilentlyContinue
    Remove-Variable -Name "MINIO_SECRET_KEY" -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "📦 Pulling Docker images..." -ForegroundColor Cyan
Write-Host "=========================" -ForegroundColor Cyan

# List of Docker images to pull
$images = @(
    "python:3.13-slim",
    "node:24-slim",
    "node:24-alpine",
    "milvusdb/milvus:v2.5.6",
    "neo4j:5.26",
    "minio/minio:RELEASE.2023-03-20T20-16-18Z",
    "ghcr.io/astral-sh/uv:0.11.26",
    "nginx:alpine",
    "quay.io/coreos/etcd:v3.5.5",
    "postgres:16",
    "redis:7-alpine"
)

# Pull each image
foreach ($image in $images) {
    if (Test-SkipExistingImage $image) {
        continue
    }

    Write-Host "🔄 Pulling ${image}..." -ForegroundColor Yellow
    try {
        & scripts/pull_image.ps1 $image
        if ($LASTEXITCODE -eq 0) {
            Write-Host "✅ Successfully pulled ${image}" -ForegroundColor Green
        } else {
            Write-Host "❌ Failed to pull ${image}" -ForegroundColor Red
            exit 1
        }
    } catch {
        Write-Host "❌ Error pulling ${image}: $_" -ForegroundColor Red
        exit 1
    }
}

$sandboxImage = "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest"
if (-not (Test-SkipExistingImage $sandboxImage)) {
    Write-Host "🔄 Pulling ${sandboxImage}..." -ForegroundColor Yellow
    docker pull $sandboxImage
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✅ Successfully pulled ${sandboxImage}" -ForegroundColor Green
    } else {
        Write-Host "❌ Failed to pull ${sandboxImage}" -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
Write-Host "🎉 Initialization complete!" -ForegroundColor Green
Write-Host "==========================" -ForegroundColor Green
Write-Host "You can now run: docker compose up -d --build" -ForegroundColor Cyan
Write-Host "This will start all services in development mode with hot-reload enabled." -ForegroundColor Cyan
