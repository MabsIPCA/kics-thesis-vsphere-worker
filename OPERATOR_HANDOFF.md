# vSphere VM operator handoff: KICS batch 1000

This guide is for the person operating the Ubuntu vSphere VM. Run the
`batch-1000` branch of the public [worker repository](https://github.com/MabsIPCA/kics-thesis-vsphere-worker/tree/batch-1000).
It has **1,000 assigned Helm chart cases** in eight saved repositories. The
earlier fork release tagged `vsphere-results-20261118-25pct` used an older
810-case assignment; its archive does not finish this batch. Keep its results
and run this batch in a separate directory.

The worker downloads its pinned KICS executable, source, Go toolchain, and
corpus from the repository's public
[`corpus-20261118-batch1000` release](https://github.com/MabsIPCA/kics-thesis-vsphere-worker/releases/tag/corpus-20261118-batch1000).
It checks their SHA-256 hashes, runs an eight-case control pilot, then scans
the 1,000 assigned cases. Each case runs baseline and enhanced KICS and saves
the raw reports. The final archive and checksum manifest are the deliverables.

## 1. Prepare the VM

Use Ubuntu **x86-64** with Python **3.10 or newer**, Git, CA certificates,
and `util-linux`. The VM needs outbound HTTPS access to GitHub and its release
asset host. The scan uses unprivileged user namespaces and overlayfs. Its
preflight checks those facilities before any case starts.

Install the basic packages if necessary:

```bash
sudo apt-get update
sudo apt-get install -y python3 git ca-certificates util-linux
uname -m
python3 --version
df -h "$HOME"
```

`uname -m` must say `x86_64`. Allow **at least 12 GiB free** on the VM's local
OS disk before starting. The worker stops safely if free space drops below
**4 GiB**; leave enough room for the result archive at the end. A writable
home directory is required. Scanning needs no GitHub login, SSH access, NAS
mount, Docker, or separately installed Helm/KICS executable.

## 2. Clone and check the assignment

Use a new directory, separate from any earlier VM checkout or work root:

```bash
git clone --branch batch-1000 --single-branch \
  https://github.com/MabsIPCA/kics-thesis-vsphere-worker.git \
  "$HOME/kics-thesis-vsphere-batch1000"
cd "$HOME/kics-thesis-vsphere-batch1000"
git branch --show-current
sha256sum plan/vsphere-delegation.json
python3 scripts/verify_handoff.py
python3 -c 'import json; print(len(json.load(open("plan/vsphere-delegation.json"))["assigned_case_ids"]))'
```

Expected branch: `batch-1000`. Expected plan SHA-256:
`f0fb37aa34ec1fe01494774983ab26d93c4c9022b4cddda332cf2cc1d205d57b`.
The last command must print `1000`. If any check differs, stop and report the
output to the thesis owner before scanning.

## 3. Start the run

For an interactive terminal:

```bash
cd "$HOME/kics-thesis-vsphere-batch1000"
bash scripts/start.sh
```

For a session that can be logged out of, use this instead:

```bash
cd "$HOME/kics-thesis-vsphere-batch1000"
nohup bash scripts/start.sh > "$HOME/kics-vsphere-batch1000.log" 2>&1 < /dev/null &
echo $! > "$HOME/kics-vsphere-batch1000.pid"
tail -f "$HOME/kics-vsphere-batch1000.log"
```

The default work root is `~/kics-vsphere-work-batch2` **on the VM OS disk**.
The assigned scan uses three workers. If the VM is short on CPU or memory,
set `KICS_VM_WORKERS=1` or `2` before starting. To use a different local OS
disk path, set `KICS_VM_WORK_ROOT` before starting; use that **same value** on
every resume. Example:

```bash
export KICS_VM_WORKERS=2
export KICS_VM_WORK_ROOT="$HOME/kics-vsphere-work-batch2"
bash scripts/start.sh
```

The pilot must match the saved Windows controls before the main scan begins.
The case journal is flushed to disk after each result. Keep the work root and
checkout until the final archive has been received and verified.

## 4. Watch progress and disk space

In a second terminal:

```bash
tail -f "$HOME/kics-vsphere-batch1000.log"
wc -l "$HOME/kics-vsphere-work-batch2/results/vsphere-run/executions.jsonl"
df -h "$HOME"
```

The `wc` count is completed journal rows out of 1,000; the journal file
appears after the pilot. The log also prints `N/1000 case ID`. If you chose a
different `KICS_VM_WORK_ROOT`, use that path in the progress and result
commands below. A journal row can contain a scan error, so check the final
summary before reporting success.

If the VM restarts, a terminal disconnects, or the disk guard stops the scan,
restore free space as needed and run `bash scripts/start.sh` again from the
**same checkout and work root**. The worker verifies staged inputs and resumes
recorded cases. A VM reboot requires starting the command again manually;
`nohup` only protects against terminal logout.

## 5. Verify the finished run

The final files have these fixed names:

```text
~/kics-vsphere-work-batch2/exports/vsphere-results-batch1000-20261118.tar.gz
~/kics-vsphere-work-batch2/exports/vsphere-results-batch1000-20261118.json
```

Run this check from the checkout. It verifies the exact assigned case set,
the finished pilot and main manifests, both KICS scan statuses for every
case, the export metadata, and the archive SHA-256:

```bash
cd "$HOME/kics-thesis-vsphere-batch1000"
python3 - <<'PY'
import hashlib, json, os
from pathlib import Path

work = Path(os.environ.get('KICS_VM_WORK_ROOT', Path.home() / 'kics-vsphere-work-batch2'))
plan = Path('plan/vsphere-delegation.json')
expected = json.loads(plan.read_text())['assigned_case_ids']
plan_hash = hashlib.sha256(plan.read_bytes()).hexdigest()
pilot = json.loads((work / 'results/vsphere-pilot/manifest.json').read_text())
run = work / 'results/vsphere-run'
manifest = json.loads((run / 'manifest.json').read_text())
rows = [json.loads(line) for line in (run / 'executions.jsonl').read_text().splitlines()]
summary = json.loads((run / 'final/summary.json').read_text())
stem = 'vsphere-results-batch1000-20261118'
archive = work / 'exports' / (stem + '.tar.gz')
receipt = json.loads((work / 'exports' / (stem + '.json')).read_text())
hasher = hashlib.sha256()
with archive.open('rb') as stream:
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        hasher.update(block)
digest = hasher.hexdigest()

assert pilot['status'] == 'completed'
assert manifest['status'] == 'completed' and manifest['delegation_sha256'] == plan_hash
assert manifest['terminal_records'] == len(rows) == len(expected) == 1000
assert len(set(expected)) == 1000 and {row['case_id'] for row in rows} == set(expected)
assert receipt['records'] == 1000 and receipt['delegation_sha256'] == plan_hash
assert receipt['file'] == archive.name and receipt['bytes'] == archive.stat().st_size
assert receipt['sha256'] == digest
failed = [row['case_id'] for row in rows if any(
    row.get('scans', {}).get(mode, {}).get('status') != 'ok'
    for mode in ('baseline', 'enhanced'))]
print('Assigned cases:', len(rows))
print('Paired successful scans:', summary['paired_successful_scans'])
print('Cases with a failed scan:', len(failed), failed[:20])
print('Plan SHA-256:', plan_hash)
print('Archive SHA-256:', digest)
print('Archive:', archive)
assert not failed, 'Report failed case IDs to thesis owner; retain all raw outputs'
PY
```

The printed paired scan count should be `1000`. If the last assertion fails,
keep both export files and send the failed case IDs and log to the thesis
owner. The export still preserves the raw evidence for diagnosis.

## 6. Return the two files

Send the `.tar.gz` archive **and** its matching `.json` receipt to the thesis
owner. A GitHub release on a fork is convenient. Use a new release tag such as
`vsphere-results-batch1000-20261119`; the prior `25pct` tag is a different
result. Upload both files as release assets and send the release URL. The
GitHub website can do this; publishing needs write access to that fork. The
scan itself needs no GitHub credentials.

If you use GitHub CLI and have permission to publish to your fork:

```bash
gh auth login
gh release create vsphere-results-batch1000-20261119 \
  --repo YOUR_GITHUB_USER/kics-thesis-vsphere-worker \
  "$HOME/kics-vsphere-work-batch2/exports/vsphere-results-batch1000-20261118.tar.gz" \
  "$HOME/kics-vsphere-work-batch2/exports/vsphere-results-batch1000-20261118.json" \
  --title 'vSphere KICS batch 1000 results' \
  --notes 'Pinned batch-1000 assignment; 1000 assigned cases; see attached SHA-256 receipt.'
```

Include in your message: the release URL or transfer location, plan SHA-256,
archive SHA-256, `1000` assigned records, paired successful scan count, any
failed case IDs, and any pilot or disk interruption. The thesis owner will
merge these results with the other hosts by case ID.

## If a check fails

- **Download or checksum error:** Confirm HTTPS access to the public asset
  release, then rerun `bash scripts/start.sh`. The bootstrap reuses verified
  downloads and checks all staged files.
- **Namespace or overlayfs preflight error:** The VM must allow unprivileged
  user namespaces and overlayfs mounts inside them. Give the exact error to
  the VM administrator or thesis owner; rerun once the VM setting is fixed.
- **Pilot mismatch:** Stop and send the log. The production scan is gated on
  that control check.
- **Less than 4 GiB free:** Free space on the VM OS disk, then rerun the same
  start command. Do not remove the current work root to recover space.
- **Interrupted scan:** Keep the same checkout and work root; rerun the start
  command to resume journaled cases.
