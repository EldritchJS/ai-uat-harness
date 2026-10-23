set -euo pipefail
# Assisted by Claude Opus
if [ -n "${REPO_URL:-}" ]; then
  echo "=== cloning ${REPO_URL} ${REPO_REF:-} ==="
  git clone --depth 1 ${REPO_REF:+--branch "$REPO_REF"} "$REPO_URL" /src/repo
else
  echo "=== waiting for the local files ==="
  for _ in $(seq 300); do [ -f /src/.ready ] && break; sleep 2; done
  [ -f /src/.ready ] || { echo "no local files after 10 minutes" >&2; exit 1; }
fi
cd /src/repo
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
echo "=== installing ansible-core and the kubernetes library ==="
python3 -m pip install --quiet "ansible-core~=2.18.0" "kubernetes~=34.1"
ansible-galaxy collection install -r requirements.yml -p collections
echo "=== deploy.sh ${DEPLOY_ARGS} ==="
eval "exec ./deploy.sh ${DEPLOY_ARGS}"
