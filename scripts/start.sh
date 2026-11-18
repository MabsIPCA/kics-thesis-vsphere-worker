#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export KICS_VM_WORK_ROOT="${KICS_VM_WORK_ROOT:-$HOME/kics-vsphere-work-batch2}"
python3 "$repo/scripts/bootstrap_from_release.py"
bash "$repo/scripts/check_vm.sh"
bash "$repo/scripts/run_side.sh" pilot
bash "$repo/scripts/run_side.sh" assigned
python3 "$repo/scripts/export_results.py"
echo "vSphere assignment completed in $KICS_VM_WORK_ROOT/results/vsphere-run"
