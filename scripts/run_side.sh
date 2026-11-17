#!/usr/bin/env bash
set -euo pipefail

side="${1:?usage: run_side.sh pilot|assigned}"
case "$side" in pilot|assigned) ;; *) echo 'Invalid side' >&2; exit 2 ;; esac
if [[ "${KICS_VM_RUN_NS:-}" != yes ]]; then
    export KICS_VM_RUN_NS=yes
    exec unshare -Ur --mount bash "$0" "$side"
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="${KICS_VM_WORK_ROOT:-$HOME/kics-vsphere-work}"
test -f "$work/stage-complete"
export PATH="$work/toolchain/go/bin:$PATH"
export TMPDIR="$work/tmp"
export PYTHONUNBUFFERED=1
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.filemode
export GIT_CONFIG_VALUE_0=false
mkdir -p "$work/work" "$work/tmp" "$work/results"
if [[ "$side" == pilot ]]; then
    output="$work/results/vsphere-pilot"
    workers=1
else
    output="$work/results/vsphere-run"
    workers="${KICS_VM_WORKERS:-3}"
fi
resume=()
if [[ -f "$output/manifest.json" ]]; then resume=(--resume); fi

setsid python3 "$repo/worker/delegate_kics_worker.py" run \
    --side "$side" --work-root "$work" --workers "$workers" \
    --kics-parallel 1 --timeout 600 "${resume[@]}" &
scan_pid=$!
trap 'kill -TERM -- "-$scan_pid" 2>/dev/null || true' INT TERM
while kill -0 "$scan_pid" 2>/dev/null; do
    free="$(df -PB1 "$work" | awk 'NR == 2 {print $4}')"
    if (( free < 4 * 1024 * 1024 * 1024 )); then
        echo 'VM OS disk below 4 GiB; stopping scan' >&2
        kill -TERM -- "-$scan_pid" 2>/dev/null || true
        wait "$scan_pid" || true
        exit 1
    fi
    sleep 30
done
wait "$scan_pid"
if [[ "$side" == pilot ]]; then
    python3 "$repo/worker/delegate_kics_worker.py" check-pilot --work-root "$work"
fi
