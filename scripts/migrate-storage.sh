#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

compose=(docker compose "$@")
proof_file="$repo_root/docker/volumes/yuxi/.storage-migration-quiesced"
proof_file_created=false
cleanup() {
  if [[ "$proof_file_created" == true ]]; then
    rm -f "$proof_file"
  fi
  "${compose[@]}" stop sandbox-provisioner >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

"${compose[@]}" stop api worker sandbox-provisioner

# Always run the provisioner from the checked-out target version. The old
# provisioner does not own the quiesce protocol yet; --no-deps breaks the
# storage-migrator dependency while this proof is being established.
"${compose[@]}" up -d --no-deps --build --wait sandbox-provisioner

# The provisioner first rejects new creates, then deletes Docker containers or
# Kubernetes Pods and waits for the authoritative backend inventory to reach zero.
"${compose[@]}" exec -T sandbox-provisioner python - <<'PY'
import os
import urllib.request

base = "http://127.0.0.1:8002"
headers = {"Authorization": f"Bearer {os.environ['SANDBOX_PROVISIONER_TOKEN']}"}
request = urllib.request.Request(
    f"{base}/api/sandboxes/quiesce?timeout_seconds=180",
    headers=headers,
    method="POST",
)
with urllib.request.urlopen(request, timeout=240):
    pass
PY

"${compose[@]}" stop sandbox-provisioner

if "${compose[@]}" ps --status running --services | grep -Eq '^(api|worker|sandbox-provisioner)$'; then
  echo "failed to quiesce API, worker, or sandbox-provisioner" >&2
  exit 1
fi

# Docker Desktop 不允许容器 root 修改宿主只读 bind 文件的 mode/owner。
# 停机后先由宿主 owner 补齐迁移所需权限；find 默认不跟随 Workspace symlink。
storage_roots=(
  "$repo_root/docker/volumes/yuxi/threads"
  "$repo_root/docker/volumes/yuxi/skill-sources"
  "$repo_root/docker/volumes/yuxi/skill-projections"
)
for storage_root in "${storage_roots[@]}"; do
  if [[ -d "$storage_root" ]]; then
    find "$storage_root" -type d -exec chmod u+rwx {} +
    find "$storage_root" -type f -exec chmod u+rw {} +
  fi
done

token="$(openssl rand -hex 32)"
if [[ -e "$proof_file" || -L "$proof_file" ]]; then
  echo "quiescence proof already exists: $proof_file" >&2
  exit 1
fi
umask 077
if ! (set -o noclobber; printf '%s\n' "$token" > "$proof_file"); then
  echo "failed to create quiescence proof: $proof_file" >&2
  exit 1
fi
proof_file_created=true

"${compose[@]}" run --rm \
  -e YUXI_STORAGE_MIGRATION_QUIESCENCE_TOKEN="$token" \
  storage-migrator

echo "storage migration completed; restart Yuxi with the same Docker Compose options"
