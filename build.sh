#!/usr/bin/env bash
# Assisted by Claude Opus
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./build.sh [-c clusters/<cluster>.yml]

Prepares the harness to run:
  1. installs the Ansible collections in requirements.yml into ./collections
  2. checks that Ansible's Python can import the kubernetes library
  3. syntax-checks every playbook
  4. with -c, compiles the Go tests onto that cluster's PVCs; binaries already
     built from the same sources are kept, so this is quick after the first time

Options:
  -c FILE   cluster config to build on (an inventory under clusters/)
  -h        show this help
EOF
}

cluster=""
while getopts "c:h" opt; do
  case "$opt" in
    c) cluster="$OPTARG" ;;
    h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")"

command -v ansible-playbook >/dev/null || { echo "ansible-playbook not found; install ansible-core 2.16 or newer" >&2; exit 1; }

echo "== collections"
ansible-galaxy collection install -r requirements.yml -p collections

echo "== python"
python="$(ansible --version | sed -n 's/^  python version = .*(\(.*\))$/\1/p')"
"${python:-python3}" -c 'import kubernetes; print("kubernetes", kubernetes.__version__, "for", __import__("sys").executable)' || {
  echo "Ansible's Python cannot import kubernetes; install it (Fedora/RHEL: python3-kubernetes, pip: kubernetes)" >&2
  exit 1
}

echo "== syntax"
check_inventory="${cluster:-$(ls clusters/*.yml | head -1)}"
for playbook in plugins/playbooks/*.yml tests/*.yml tests/batches/*.yml setup/*.yml; do
  [ -f "$playbook" ] || continue
  [ "$playbook" = "plugins/playbooks/sequences.yml" ] && continue
  ansible-playbook -i "$check_inventory" --syntax-check "$playbook" >/dev/null
  echo "  ok  $playbook"
done

if [ -n "$cluster" ]; then
  echo "== build on $(basename "$cluster" .yml)"
  run_id="build_$(date -u +%Y%m%d_%H%M%S)"
  mkdir -p "runs/$run_id"
  ANSIBLE_LOG_PATH="$PWD/runs/$run_id/ansible.log" \
    ansible-playbook -i "$cluster" -e "uat_run_id=$run_id" -e "uat_runs_dir=$PWD/runs" plugins/playbooks/build.yml
  echo "Build log: runs/$run_id/logs/"
fi
