#!/usr/bin/env python3
"""Export frozen Windows pilot values and gate the Linux full assignment."""

import argparse
import json
from pathlib import Path
import sys

import split_kics_corpus as split


SCAN_FIELDS = ("status", "total", "severity", "query_ids")
RECOVERY_FIELDS = ("status", "started", "completed", "incomplete")


def compact(record):
    scans = {}
    for mode in ("baseline", "enhanced"):
        scan = record.get("scans", {}).get(mode, {})
        scans[mode] = {key: scan.get(key) for key in SCAN_FIELDS}
        scans[mode]["recovery"] = {
            key: scan.get("recovery", {}).get(key) for key in RECOVERY_FIELDS
        }
    return {"case_id": record["case_id"],
            "input_sha256": record.get("input_sha256"), "scans": scans}


def export(args):
    plan_path, plan, charts = split.load_plan(args.plan)
    source = Path(plan["source_run"])
    if (split.sha(source / "manifest.json") != plan["source_manifest_sha256"]
            or split.sha(source / "executions.jsonl") != plan["source_executions_sha256"]):
        raise ValueError("frozen source changed after split planning")
    manifest = split.read_json(source / "manifest.json")
    replay = split.runner(args.worker_dir)
    prior, _ = replay.load_prior_run(source, charts, manifest["kics_binary"],
                                     manifest["catalog"])
    by_id = {record["case_id"]: record for record in prior}
    expected = {
        "plan_sha256": split.sha(plan_path),
        "kics_source_sha256": plan["kics_source_sha256"],
        "records": [compact(by_id[case_id]) for case_id in plan["pilot_case_ids"]],
    }
    split.write_json(args.out, expected)
    print(args.out)


def check(args):
    plan_path, plan, _ = split.load_plan(args.plan)
    expected = split.read_json(args.expected)
    if (expected["plan_sha256"] != split.sha(plan_path)
            or expected["kics_source_sha256"] != plan["kics_source_sha256"]):
        raise ValueError("pilot expectations differ from split plan")
    originals = {r["case_id"]: r for r in expected["records"]}
    if set(originals) != set(plan["pilot_case_ids"]) or len(originals) != len(expected["records"]):
        raise ValueError("pilot expectation case IDs differ from plan")
    output = Path(args.pilot_output)
    manifest = split.read_json(output / "manifest.json")
    if (manifest["status"] != "completed" or manifest["side"] != "pilot"
            or manifest["plan_sha256"] != split.sha(plan_path)
            or manifest["kics_source_sha256"] != plan["kics_source_sha256"]):
        raise ValueError("Linux pilot manifest is incomplete or mismatched")
    with (output / "executions.jsonl").open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    if len(rows) != len(originals) or {r["case_id"] for r in rows} != set(originals):
        raise ValueError("Linux pilot case IDs differ from plan")
    for row in rows:
        case_id = row["case_id"]
        if compact(row) != originals[case_id]:
            raise ValueError(f"Linux pilot findings or recovery differ: {case_id}")
    print(f"Linux pilot matches {len(rows)} frozen Windows cases")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("export")
    p.add_argument("--plan", required=True)
    p.add_argument("--worker-dir", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=export)
    p = sub.add_parser("check")
    p.add_argument("--plan", required=True)
    p.add_argument("--expected", required=True)
    p.add_argument("--pilot-output", required=True)
    p.set_defaults(func=check)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
