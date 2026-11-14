#!/usr/bin/env bash
# Exercise the production publisher on a disposable GitHub-hosted Linux runner.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == true && "${CI:-}" == true && "${RUNNER_ENVIRONMENT:-}" == github-hosted ]]
[[ $# == 2 && ! -e /etc/systemd/system/saudade-agent.service ]]
source_dir="$(git rev-parse --show-toplevel)"
candidate="$(git rev-parse --verify HEAD^{commit})"
previous="$(git rev-parse --verify "$1^{commit}")"
report="$(realpath -m -- "$2")"
stage="$(mktemp -d /tmp/saudade-deploy-rehearsal.XXXXXX)"
chmod 700 "$stage"
mkdir -p "$stage/releases" "$stage/data" "$stage/host" "$stage/traces" "$(dirname "$report")"
cleanup() {
  sudo systemctl stop saudade-agent || true
  sudo rm -f /etc/systemd/system/saudade-agent.service
  sudo systemctl daemon-reload
}
trap cleanup EXIT
cat > "$stage/agent.env" <<EOF
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-ci-fixture-not-a-live-key
OPENAI_BASE_URL=http://127.0.0.1:9/v1
OPENAI_MODEL=ci-fixture
AGENT_ASSERTION_SECRET=ci-assertion-fixture-00000000000000000000000000000000
AGENT_CONFIRM_SECRET=ci-confirm-fixture-0000000000000000000000000000000000
BLOG_API_BASE=https://blog.example.test/api/public
BLOG_ROOT=$stage/host
CHECKPOINT_DSN=postgresql://postgres:rehearsal@127.0.0.1:5432/agent_deployment_ci
CHECKPOINT_FORBID_DB=saudade_blog
DELEGATION_DIR=$stage/data/delegations
TRACE_DIR=$stage/traces
SCHEDULING_ENABLED=false
RAG_HYBRID_ENABLED=false
IOT_ENABLED=false
SAUDADE_IGNORE_ENV_FILE=1
AGENT_PORT=18041
EOF
chmod 600 "$stage/agent.env"
cat > "$stage/saudade-agent.service" <<EOF
[Unit]
Description=Disposable Agent deployment rehearsal
[Service]
User=$(id -un)
WorkingDirectory=$stage/releases/current
EnvironmentFile=$stage/agent.env
ExecStart=$stage/releases/current/scripts/serve.sh
KillMode=control-group
TimeoutStopSec=30
EOF
sudo install -m 644 "$stage/saudade-agent.service" /etc/systemd/system/saudade-agent.service
sudo systemctl daemon-reload
publish() {
  AGENT_PORT="${2:-18041}" bash "$source_dir/scripts/deploy_release.sh" \
    "$1" "$stage/releases" "$stage/agent.env" "$stage/data"
}
probe() {
  python3 - "$1" <<'PY'
import json, sys, time, urllib.error, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(path):
    with opener.open('http://127.0.0.1:18041/' + path, timeout=5) as response:
        return json.load(response)
for attempt in range(60):
    try:
        assert get('version')['sha'] == sys.argv[1]
        assert get('ready')['ready'] is True
        assert get('health')['dials']['checkpoint_backend'] == 'postgres'
        break
    except (urllib.error.URLError, AssertionError):
        if attempt == 59:
            raise
        time.sleep(0.5)
PY
}
publish "$previous"
probe "$previous"
# This marker belongs only to this disposable database and tests restore of data, not just DDL.
export PGPASSWORD=rehearsal
psql -h 127.0.0.1 -U postgres -d agent_deployment_ci -v ON_ERROR_STOP=1 \
  -c "CREATE TABLE deployment_fixture (id integer primary key, value text); INSERT INTO deployment_fixture VALUES (1, 'before-upgrade');"
pg_dump -h 127.0.0.1 -U postgres -d agent_deployment_ci -Fc -f "$stage/checkpoint.dump"
createdb -h 127.0.0.1 -U postgres agent_restore_ci
pg_restore -h 127.0.0.1 -U postgres -d agent_restore_ci --exit-on-error "$stage/checkpoint.dump"
[[ "$(psql -h 127.0.0.1 -U postgres -d agent_restore_ci -Atc 'SELECT value FROM deployment_fixture WHERE id=1')" == before-upgrade ]]
# A closed health-probe port forces the real publisher to restore and restart the previous release.
if publish "$candidate" 1 > "$stage/rejected-publish.log" 2>&1; then
  echo 'Unreachable health probe unexpectedly passed' >&2
  exit 1
fi
[[ "$(readlink -f "$stage/releases/current")" == "$stage/releases/releases/$previous" ]]
probe "$previous"
publish "$candidate"
probe "$candidate"
python3 - "$previous" "$candidate" "$report" <<'PY'
import json, pathlib, sys
pathlib.Path(sys.argv[3]).write_text(json.dumps({
    'baseline_sha': sys.argv[1], 'candidate_sha': sys.argv[2],
    'fresh_install': 'passed', 'health_failure_rollback': 'passed',
    'upgrade': 'passed', 'postgres_backup_restore': 'passed',
    'service': 'real systemd', 'provider_requests': 0,
    'scope': 'Disposable GitHub-hosted runner, production publisher and real PostgreSQL. '
             'No host business API, model response or production data is tested here.'
}, indent=2) + '\n')
PY
