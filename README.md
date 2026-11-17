# vSphere KICS thesis worker

This public repository runs **810 of the 3,241 cases originally assigned to
Windows (25.0%)**. The four complete saved chart repositories are
`beclab__apps` (412 cases), `kubernetes__charts` (356),
`redhat-community-ai-tools__UnifAI` (21), and `EugenMayer__helm-charts` (21).
Their exact content hashes, source Git
revisions, assigned case IDs, and the Windows cutover journal hash are in
[`plan/vsphere-delegation.json`](plan/vsphere-delegation.json). Windows skips
these IDs. The existing Linux host's 621 cases remain a separate assignment.

The repository is the VM's only starting point. Its pinned KICS source, Linux
binary, Go 1.26.3 toolchain, and exact chart corpus are seven checksum-pinned
assets on the public `corpus-20261117-25pct` GitHub release. The bootstrap downloads
them over HTTPS to the VM **OS disk**, verifies the source and every corpus
file, then starts a four-case cross-platform pilot. Production starts only if
the pilot's findings and render-recovery counts match the Windows controls.
No NAS mount, SSH access to the VM, GitHub token, Docker, or Helm CLI is needed
to start.

## VM prerequisites

- Ubuntu x86-64, Python 3.10+, Git, CA certificates, and `util-linux` with
  `unshare` and `setsid`. Unprivileged user namespaces and overlayfs must work.
- Outbound HTTPS access to GitHub, including release asset downloads.
- At least **12 GiB free** on the OS disk before starting. The worker checks
  space every 30 seconds and stops scans if free space falls below **4 GiB**.
  The exact requirement depends on report sizes; watch the VM's disk.
- A user account that can write to its home directory. A GitHub login is only
  needed later if you choose to upload the result archive to GitHub.

## One-command start

```bash
git clone https://github.com/MabsIPCA/kics-thesis-vsphere-worker.git
cd kics-thesis-vsphere-worker
bash scripts/start.sh
```

The command verifies the repository files, downloads and checks seven release
assets, extracts them under `~/kics-vsphere-work`, verifies KICS and the four
saved corpus trees, checks unprivileged overlayfs, runs the pilot, compares it
with four frozen Windows controls, then runs the 810 assigned cases with
three chart workers. It journals each completed case and resumes safely if
you rerun the same command after an interruption. Set `KICS_VM_WORK_ROOT` to
another **local OS disk** path before starting if needed.

The current source plan covers 9,861 chart directories. The split preserves
their original global case IDs so results can be checked against the frozen
Windows and Linux runs. The VM scans baseline and enhanced modes with the
same pinned KICS source; Linux binary SHA-256 differs from the Windows binary
because it targets Linux. This is recorded in the manifests.

## Results

The full local results are in `~/kics-vsphere-work/results/vsphere-run`.
The startup script also creates a compressed archive and SHA-256 manifest in
`~/kics-vsphere-work/exports`. Return both files through your preferred
vSphere file transfer. If you authenticate `gh` in the VM, you can upload
those two files to a GitHub release instead. The archive includes the full
pilot, per-case raw reports, recovery traces, journal, and summary. Keep its
manifest alongside it when transferring so the thesis merge can verify it.

All input files in this repository and its release are public chart/KICS
source data. Credentials and local Windows paths are not required on the VM;
the Windows paths in the plan are retained only as provenance for the original
saved repositories.
