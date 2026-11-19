# vSphere KICS thesis worker: second batch

> **Assignment update, 19 November 2026:** The audited VM results already
> cover 42 IDs in this batch. The other 958 IDs have been assigned to a
> dedicated Windows continuation. Do not start this full 1,000-case VM run
> while that Windows continuation is active. Preserve any VM results already
> produced; the final merge will compare and deduplicate by case ID.

**VM operator:** follow the complete [operator handoff](OPERATOR_HANDOFF.md)
for prerequisites, copy-paste startup and resume commands, progress checks,
result verification, and return instructions.

This `batch-1000` branch runs **1,000 new chart cases** on the vSphere VM.
The previous VM [result release](https://github.com/cx-miguel-dasilva/kics-thesis-vsphere-worker/releases/tag/vsphere-results-20261117)
contains 959 completed cases and is preserved separately. The new batch
includes 42 cases missing from the earlier active 810-case plan and 958 more
unrun Windows cases. Its eight complete saved repositories, input hashes,
source revisions, and exact case IDs are in
[`plan/vsphere-delegation.json`](plan/vsphere-delegation.json).

The VM needs only this GitHub branch. Its pinned KICS source, Linux binary,
Go 1.26.3 toolchain, and eight corpus trees are checksum-pinned release assets.
The bootstrap downloads them to the VM **OS disk**, verifies every input,
then runs one Windows-controlled pilot case per repository. The 1,000-case
production run starts only after the pilot passes. Windows excludes these
cases and the previous 959 VM cases at the recorded cutover. The separate
Linux PC completed 621 cases; its host provenance is recorded outside this
repository with the previous VM result archive.

## Prerequisites

- Ubuntu x86-64, Python 3.10+, Git, CA certificates, and `util-linux` with
  `unshare` and `setsid`. Unprivileged user namespaces and overlayfs must work.
- Outbound HTTPS to GitHub, including release asset downloads.
- More than **4 GiB free** on the local OS disk. Bootstrap checks after each
  asset, and the scan checks every 30 seconds. **12 GiB is recommended** for
  an uninterrupted full run.
- A writable home directory. No NAS mount, SSH access to the VM, GitHub token,
  Docker, or Helm CLI is needed to start.

## Start or resume

```bash
git clone --branch batch-1000 https://github.com/MabsIPCA/kics-thesis-vsphere-worker.git kics-thesis-vsphere-batch1000
cd kics-thesis-vsphere-batch1000
bash scripts/start.sh
```

The work root defaults to `~/kics-vsphere-work-batch2`, separate from the
earlier VM run. Rerunning the same command resumes journaled cases. Set
`KICS_VM_WORK_ROOT` to another **local OS disk** path if needed.

## Return the results

The script writes both
`~/kics-vsphere-work-batch2/exports/vsphere-results-batch1000-20261118.tar.gz`
and the matching `.json` checksum manifest. Keep the pair together when
transferring or uploading them. The archive contains the pilot, every raw
baseline and enhanced report, render traces, execution journal, and summary.
The thesis merge will verify this batch alongside the previous 959 VM cases,
the Linux PC's 621 cases, Windows, and the frozen source run.
