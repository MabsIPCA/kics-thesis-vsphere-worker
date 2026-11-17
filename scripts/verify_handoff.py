#!/usr/bin/env python3
"""Check every small handoff file against the repository manifest."""

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    manifest = json.loads((ROOT / "handoff-manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        path = ROOT / name
        if not path.is_file() or sha(path) != expected:
            raise SystemExit(f"handoff file differs: {name}")
    actual = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(ROOT).parts
        and "__pycache__" not in path.relative_to(ROOT).parts
        and path.suffix != ".pyc" and path.name != "handoff-manifest.json"
    }
    if actual != set(manifest["files"]):
        raise SystemExit("handoff file set differs from manifest")
    plan = json.loads((ROOT / "plan/plan.json").read_text(encoding="utf-8"))
    delegation = json.loads((ROOT / "plan/vsphere-delegation.json").read_text(encoding="utf-8"))
    bundle = json.loads((ROOT / "bundle-manifest.json").read_text(encoding="utf-8"))
    if (plan["vm_repo_input_sha256"] != manifest["corpus_sha256"]
            or plan["kics_revision"] != manifest["kics_revision"]
            or sha(ROOT / "plan/selected-charts.json") != plan["selected_charts_sha256"]
            or sha(ROOT / "worker/build.json") != plan["kics_build_evidence_sha256"]
            or delegation["status"] != "active"
            or delegation["parent_plan_sha256"] != sha(ROOT / "plan/plan.json")
            or bundle["delegation_sha256"] != sha(ROOT / "plan/vsphere-delegation.json")):
        raise SystemExit("pinned plan inputs differ")
    print(f"Verified {len(actual)} handoff files; "
          f"{len(delegation['pilot_case_ids'])} vSphere pilot cases")


if __name__ == "__main__":
    main()
