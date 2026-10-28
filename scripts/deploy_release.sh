#!/usr/bin/env bash
# Publish one committed snapshot; the service must use current/scripts/serve.sh.
set -euo pipefail
if [[ $# -ne 4 ]]; then
  echo "Usage: $0 REVISION RELEASE_ROOT CONFIG_FILE DATA_DIR" >&2
  exit 2
fi
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
revision="$(git -C "$source_dir" rev-parse --verify "$1^{commit}")"
release_root="$(realpath -m -- "$2")"
config_file="$(realpath -e -- "$3")"
data_dir="$(realpath -e -- "$4")"
[[ -f "$config_file" && -d "$data_dir" ]]
mkdir -p "$release_root/releases"
exec 9>"$release_root/deploy.lock"
flock -n 9 || { echo "Another deployment is running" >&2; exit 1; }
release="$release_root/releases/$revision"
if [[ ! -d "$release" ]]; then
  mkdir "$release"
  git -C "$source_dir" archive "$revision" | tar -xf - -C "$release"
fi
[[ -f "$release/uv.lock" && -f "$release/scripts/serve.sh" ]]
printf '%s\n' "$revision" > "$release/REVISION"
ln -sfn -- "$config_file" "$release/.env"
ln -sfn -- "$data_dir" "$release/data"
uv_args=(sync --locked --no-dev)
if [[ "${DEPLOY_EMBEDDING_TOKENIZER:-0}" == 1 ]]; then
  uv_args+=(--extra embedding-tokenizer)
fi
(cd "$release" && uv "${uv_args[@]}")
(cd "$release" && .venv/bin/python -c 'from agent.main import app; assert app.state.runtime is None')
previous=""
if [[ -L "$release_root/current" ]]; then
  previous="$(readlink -e -- "$release_root/current")"
fi
activate() {
  ln -sfn -- "$1" "$release_root/current.next"
  mv -Tf -- "$release_root/current.next" "$release_root/current"
}
rollback() {
  if [[ -n "$previous" ]]; then
    activate "$previous"
    sudo -n systemctl restart saudade-agent
  fi
  echo "Deployment failed; previous release restored when available" >&2
}
activate "$release"
if ! sudo -n systemctl restart saudade-agent; then
  rollback
  exit 1
fi
port="${AGENT_PORT:-8010}"
for attempt in {1..45}; do
  if curl -fsS "http://127.0.0.1:$port/ready" >/dev/null 2>&1 &&
      curl -fsS "http://127.0.0.1:$port/version" | "$release/.venv/bin/python" -c \
      'import json,sys; assert json.load(sys.stdin)["sha"] == sys.argv[1]' "$revision"; then
    echo "Published $revision"
    exit 0
  fi
  sleep 1
done
rollback
exit 1
