#!/usr/bin/env python3
"""Create a SHA-256-verified result archive for return from the VM."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tarfile


WORK = Path(os.environ.get("KICS_VM_WORK_ROOT", Path.home() / "kics-vsphere-work")).resolve()


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    results = WORK / "results"
    assigned = results / "vsphere-run"
    pilot = results / "vsphere-pilot"
    if not assigned.is_dir() or not pilot.is_dir():
        raise SystemExit("both pilot and assigned results are required")
    manifest = json.loads((assigned / "manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] != "completed" or manifest["terminal_records"] != 810:
        raise SystemExit("vSphere assignment has not completed all 810 cases")
    export = WORK / "exports"
    export.mkdir(exist_ok=True)
    archive_path = export / "vsphere-results-20261117.tar.gz"
    partial = archive_path.with_name(archive_path.name + ".partial")
    if partial.exists():
        partial.unlink()
    with tarfile.open(partial, "w:gz", compresslevel=1) as archive:
        archive.add(pilot, arcname="results/vsphere-pilot")
        archive.add(assigned, arcname="results/vsphere-run")
    os.replace(partial, archive_path)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file": archive_path.name,
        "bytes": archive_path.stat().st_size,
        "sha256": sha(archive_path),
        "delegation_sha256": manifest["delegation_sha256"],
        "records": manifest["terminal_records"],
    }
    (export / "vsphere-results-20261117.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
