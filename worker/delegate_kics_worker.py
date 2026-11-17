#!/usr/bin/env python3
"""Run disjoint delegated chart cases over verified local Linux overlays."""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import kics_corpus_replay as replay
import split_kics_corpus as split


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "plan/vsphere-delegation.json"
EXPECTED_PATH = ROOT / "plan/vsphere-pilot-expected.json"
PARENT_PATH = ROOT / "plan/plan.json"
CHARTS_PATH = ROOT / "plan/selected-charts.json"
BUILD_PATH = ROOT / "worker/build.json"
BUNDLE_PATH = ROOT / "bundle-manifest.json"
REPO_STATUS = "not_collected_bundle_overlayfs; complete source manifest verified"
SCAN_FIELDS = ("status", "total", "severity", "query_ids")
RECOVERY_FIELDS = ("status", "started", "completed", "incomplete")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def compact(record):
    scans = {}
    for mode in ("baseline", "enhanced"):
        scan = record.get("scans", {}).get(mode, {})
        scans[mode] = {field: scan.get(field) for field in SCAN_FIELDS}
        scans[mode]["recovery"] = {
            field: scan.get("recovery", {}).get(field) for field in RECOVERY_FIELDS
        }
    return {"case_id": record["case_id"],
            "input_sha256": record.get("input_sha256"), "scans": scans}


def load_inputs(work):
    parent, delegation = read(PARENT_PATH), read(PLAN_PATH)
    if (delegation["parent_plan_sha256"] != split.sha(PARENT_PATH)
            or delegation["selected_charts_sha256"] != split.sha(CHARTS_PATH)
            or delegation["kics_build_evidence_sha256"] != split.sha(BUILD_PATH)):
        raise ValueError("delegation differs from frozen parent inputs")
    charts = read(CHARTS_PATH)
    ids = delegation["assigned_case_ids"]
    if (len(charts) != parent["scheduled_charts"]
            or len(ids) != len(set(ids))
            or not set(ids) <= set(parent["windows_case_ids"])):
        raise ValueError("delegated case IDs are invalid")
    repos = {repo["key"]: repo for repo in delegation["repos"]}
    if len(repos) != len(delegation["repos"]):
        raise ValueError("duplicate repository keys")
    for case_id in ids:
        source = charts[int(case_id)]["repo_root"]
        if source not in {repo["source_root"] for repo in repos.values()}:
            raise ValueError(f"case {case_id} is outside delegated repositories")
    evidence = read(BUILD_PATH)
    if (split.source_hash(evidence) != delegation["kics_source_sha256"]
            or evidence["head"] != delegation["kics_revision"]):
        raise ValueError("KICS source evidence differs")
    kics = work / "kics"
    for relative, expected in evidence["source_files"].items():
        path = kics / relative
        actual = split.sha(path) if path.is_file() else None
        if actual != expected:
            raise ValueError(f"staged KICS source differs: {relative}")
    binary = work / "kics-linux/kics"
    bundle = read(BUNDLE_PATH)
    if split.sha(binary) != bundle["linux_binary_sha256"]:
        raise ValueError("staged Linux binary differs")
    go = work / "toolchain/go/bin/go"
    build_info = subprocess.check_output([str(go), "version", "-m", str(binary)], text=True)
    if f"vcs.revision={delegation['kics_revision']}" not in build_info:
        raise ValueError("Linux binary revision differs")
    manifests = {}
    for key, repo in repos.items():
        source = work / "corpus" / key
        manifest = replay.tree_manifest(source)
        if (replay.manifest_hash(manifest) != repo["input_sha256"]
                or len(manifest) != repo["input_files"]):
            raise ValueError(f"staged corpus differs: {key}")
        manifests[key] = manifest
    return delegation, charts, repos, manifests, binary, build_info


def check_pilot(work, delegation):
    expected = read(EXPECTED_PATH)
    output = work / "results/vsphere-pilot"
    manifest = read(output / "manifest.json")
    if (manifest["status"] != "completed" or manifest["side"] != "pilot"
            or manifest["delegation_sha256"] != split.sha(PLAN_PATH)
            or expected["parent_plan_sha256"] != delegation["parent_plan_sha256"]):
        raise ValueError("vSphere pilot provenance differs")
    rows = [json.loads(line) for line in (output / "executions.jsonl").read_text(
        encoding="utf-8").splitlines()]
    originals = {r["case_id"]: r for r in expected["records"]}
    if (len(rows) != len(originals)
            or {r["case_id"] for r in rows} != set(originals)):
        raise ValueError("vSphere pilot case set differs")
    for row in rows:
        if compact(row) != originals[row["case_id"]]:
            raise ValueError(f"vSphere pilot differs: {row['case_id']}")
    print(f"vSphere pilot matches {len(rows)} frozen Windows cases", flush=True)


def run(args):
    work = Path(args.work_root).resolve()
    if not (work / "stage-complete").is_file():
        raise ValueError("run bootstrap first")
    delegation, charts, repos, manifests, binary, build_info = load_inputs(work)
    if args.side == "assigned":
        if (delegation["status"] != "active" or not delegation["windows_cutover"]):
            raise ValueError("delegation is not active; Windows cutover is required")
        check_pilot(work, delegation)
        ids = delegation["assigned_case_ids"]
    else:
        ids = delegation["pilot_case_ids"]
    if not ids or not set(ids) <= set(delegation["assigned_case_ids"]):
        raise ValueError("pilot or assigned IDs are invalid")
    output = work / "results" / ("vsphere-pilot" if args.side == "pilot" else "vsphere-run")
    plan_sha = split.sha(PLAN_PATH)
    if output.exists():
        if not args.resume:
            raise ValueError("output exists; pass --resume for the same run")
        manifest = read(output / "manifest.json")
        if (manifest["delegation_sha256"] != plan_sha
                or manifest["binary_sha256"] != split.sha(binary)
                or manifest["side"] != args.side):
            raise ValueError("resumed output provenance differs")
        rows = [json.loads(line) for line in (output / "executions.jsonl").read_text(
            encoding="utf-8").splitlines()]
        recorded = {row["case_id"] for row in rows}
        if len(recorded) != len(rows) or not recorded <= set(ids):
            raise ValueError("journal has duplicate or unassigned cases")
        if manifest["status"] == "completed":
            print("This vSphere side is already complete", flush=True)
            return
        for case_id in ids:
            key = Path(charts[int(case_id)]["repo_root"]).name
            partial = output / "repos" / key / "raw" / case_id
            if case_id not in recorded and partial.exists():
                aborted = partial.parent.parent / "aborted"
                aborted.mkdir(exist_ok=True)
                partial.rename(aborted / f"{case_id}-{len(list(aborted.glob(case_id + '-*'))):03d}")
    else:
        output.mkdir(parents=True)
        (output / "final").mkdir()
        for key, hashes in manifests.items():
            folder = output / "repos" / key
            (folder / "raw").mkdir(parents=True)
            write(folder / "input-files.json", hashes)
        (output / "executions.jsonl").touch()
        rows = []
        manifest = {
            "schema": 1, "status": "running", "side": args.side,
            "started_at": stamp(), "delegation_sha256": plan_sha,
            "parent_plan_sha256": delegation["parent_plan_sha256"],
            "assigned_case_ids": ids, "binary_sha256": split.sha(binary),
            "kics_revision": delegation["kics_revision"],
            "kics_source_sha256": delegation["kics_source_sha256"],
            "build_info": build_info, "workers": args.workers,
            "work_strategy": "overlayfs", "repo_status": REPO_STATUS,
            "repo_input_sha256": {key: repo["input_sha256"] for key, repo in repos.items()},
        }
        write(output / "manifest.json", manifest)
    recorded = {row["case_id"] for row in rows}
    pending_ids = [case_id for case_id in ids if case_id not in recorded]
    assets = work / "kics/assets"

    def task(case_id):
        index = int(case_id)
        original = charts[index]
        key = Path(original["repo_root"]).name
        repo = repos[key]
        source = work / "corpus" / key
        mapped = dict(original, repo_root=str(source),
                      chart=str(source / original["chart_relative"]))
        record = split.overlay_run_chart(
            index, mapped, output / "repos" / key, binary, assets,
            args.timeout, work / "work", args.kics_parallel,
            manifests[key], repo["input_sha256"], replay,
            repo["git_revision"], REPO_STATUS)
        record["source_repo_root"] = original["repo_root"]
        record["source_chart_relative"] = original["chart_relative"]
        record["delegation_sha256"] = plan_sha
        return record

    try:
        with (output / "executions.jsonl").open("a", encoding="utf-8") as stream:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                pending = {}
                todo = iter(pending_ids)

                def submit():
                    try:
                        case_id = next(todo)
                    except StopIteration:
                        return False
                    pending[pool.submit(task, case_id)] = case_id
                    return True

                for _ in range(args.workers):
                    if not submit():
                        break
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        case_id = pending.pop(future)
                        row = future.result()
                        if row.get("input_sha256") != repos[
                                Path(charts[int(case_id)]["repo_root"]).name]["input_sha256"]:
                            raise ValueError(f"case input hash differs: {case_id}")
                        rows.append(row)
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                        print(f"{len(rows)}/{len(ids)} case {case_id}", flush=True)
                        submit()
        for key, repo in repos.items():
            if replay.manifest_hash(replay.tree_manifest(work / "corpus" / key)) != repo["input_sha256"]:
                raise ValueError(f"corpus changed during scan: {key}")
        write(output / "final/summary.json", replay.summarize(rows))
        manifest["status"] = "completed"
    except BaseException:
        manifest["status"] = "interrupted"
        raise
    finally:
        manifest["terminal_records"] = len(rows)
        manifest["last_updated_at"] = stamp()
        write(output / "manifest.json", manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--side", choices=("pilot", "assigned"), required=True)
    run_parser.add_argument("--work-root", required=True)
    run_parser.add_argument("--workers", type=int, default=3)
    run_parser.add_argument("--kics-parallel", type=int, default=1)
    run_parser.add_argument("--timeout", type=int, default=600)
    run_parser.add_argument("--resume", action="store_true")
    check_parser = sub.add_parser("check-pilot")
    check_parser.add_argument("--work-root", required=True)
    args = parser.parse_args()
    if args.command == "check-pilot":
        check_pilot(Path(args.work_root).resolve(), read(PLAN_PATH))
    else:
        if args.workers < 1 or args.timeout < 1:
            parser.error("workers and timeout must be positive")
        run(args)


if __name__ == "__main__":
    main()
