#!/usr/bin/env bash
set -euo pipefail

for command in python3 git unshare mount umount setsid; do
    command -v "$command" >/dev/null || { echo "Missing $command" >&2; exit 1; }
done
if [[ "$(uname -m)" != x86_64 ]]; then
    echo "Pinned Linux binary requires x86-64" >&2
    exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10)'

if [[ "${KICS_VM_OVERLAY_CHECK_NS:-}" != yes ]]; then
    export KICS_VM_OVERLAY_CHECK_NS=yes
    exec unshare -Ur --mount bash "$0"
fi

temp="$(mktemp -d)"
trap 'rm -rf -- "$temp"' EXIT
mkdir -p "$temp/lower" "$temp/upper" "$temp/work" "$temp/merged"
printf 'untouched\n' > "$temp/lower/input.txt"
mount -t overlay overlay \
    -o "lowerdir=$temp/lower,upperdir=$temp/upper,workdir=$temp/work" \
    "$temp/merged"
printf 'changed\n' > "$temp/merged/input.txt"
if [[ "$(cat "$temp/lower/input.txt")" != untouched ]]; then
    echo "Overlay changed its lower layer" >&2
    exit 1
fi
umount "$temp/merged"
echo "VM prerequisites and unprivileged overlayfs work"
