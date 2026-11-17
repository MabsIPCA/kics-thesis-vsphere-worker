#!/usr/bin/env python3
"""Replay retained chart directories through the actual pinned KICS binary."""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from corpus_selection import prepare_corpus
from recovery_results import load_recovery, aggregate_recovery, recovery_markdown
from kics_build import verify_build


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], encoding="utf-8", errors="replace",
        stderr=subprocess.PIPE).strip()


def tree_manifest(root):
    """Include untracked inputs; reject links so a scan cannot modify originals."""
    entries = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in dirs + sorted(files):
            path = Path(base) / name
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError(f"linked input requires explicit snapshot handling: {path}")
        for name in sorted(files):
            path = Path(base) / name
            entries[path.relative_to(root).as_posix()] = digest(path)
    return entries


def manifest_hash(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def select_charts(catalog):
    """One scan per repository/chart directory; retain all source memberships."""
    selected = {}
    for repo in catalog:
        root = Path(repo["cloned_dir"]).resolve()
        for chart in repo.get("charts") or []:
            path = Path(chart["chart_path"]).resolve()
            rel = path.relative_to(root)  # reject inputs outside their repo
            key = (str(root), rel.as_posix())
            if key not in selected:
                selected[key] = {
                    "repo_root": str(root), "chart": str(path),
                    "chart_relative": rel.as_posix(), "sources": [],
                    "catalog_runs": chart.get("runs", []),
                }
            selected[key]["sources"].append({
                "repo_url": repo.get("repo_url"), "repo_name": repo.get("repo_name"),
                "seed_memberships": repo.get("seed_memberships", [])
            })
    return list(selected.values())


def scan_chart(binary, assets, chart, output, enhanced, timeout, env, kics_parallel=0):
    output.mkdir(parents=True)
    command = [
        str(binary), "scan", "-p", str(chart),
        "-q", str(assets / "queries"), "-b", str(assets / "libraries"),
        "-o", str(output), "--output-name", "results",
        "--report-formats", "json", "--ignore-on-exit", "results",
        "--image-bom", "--log-level", "DEBUG", "--log-format", "json",
        "--log-path", str(output / "kics.log"),
    ]
    if enhanced:
        command.append("--experimental-helm-scan")
    if kics_parallel:
        command.extend(("--parallel", str(kics_parallel)))
    env = dict(env, KICS_HELM_RENDER_REPORT=str(output / "recovery.jsonl"))
    record = {"started_at": utc(), "command": command, "status": "error"}
    try:
        with (output / "console.log").open("wb") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=timeout, env=env)
        record["exit_code"] = result.returncode
        report_path = output / "results.json"
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8-sig"))
            total = report["total_counter"]
            if not isinstance(total, int) or total < 0:
                raise ValueError("invalid KICS total_counter")
            queries = report.get("queries", [])
            findings = sum(len(q.get("files", [])) for q in queries)
            if findings != total:
                raise ValueError(f"finding rows {findings} != total_counter {total}")
            record.update(
                total=total, severity=report.get("severity_counters", {}),
                query_ids=sorted({q["query_id"] for q in queries}),
                report_sha256=digest(report_path),
                report=str(report_path),
                files_scanned=report.get("files_scanned"),
                files_parsed=report.get("files_parsed"),
            )
            if result.returncode == 0:
                record["status"] = "ok"
        else:
            record["error"] = "KICS did not produce results.json"
    except subprocess.TimeoutExpired:
        record["status"] = "timeout"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        record["error"] = str(exc)
    record["recovery"] = load_recovery(output / "recovery.jsonl")
    record["finished_at"] = utc()
    write_json(output / "execution.json", record)
    return record


def summarize(records):
    summary = {
        "scheduled_chart_directories": len(records),
        "paired_successful_scans": 0, "engines": {},
        "unit": "chart-directory scans; findings are KICS result occurrences",
    }
    for mode in ("baseline", "enhanced"):
        ok = [r["scans"][mode] for r in records
              if r.get("scans", {}).get(mode, {}).get("status") == "ok"]
        summary["engines"][mode] = {
            "successful_scans": len(ok),
            "findings_successful_scans": sum(x["total"] for x in ok),
        }
    pairs = [r for r in records if all(
        r.get("scans", {}).get(m, {}).get("status") == "ok"
        for m in ("baseline", "enhanced"))]
    summary["paired_successful_scans"] = len(pairs)
    summary["paired_findings"] = {
        m: sum(r["scans"][m]["total"] for r in pairs)
        for m in ("baseline", "enhanced")
    }
    summary["paired_finding_gain"] = (
        summary["paired_findings"]["enhanced"] - summary["paired_findings"]["baseline"])
    summary["excluded_from_paired_comparison"] = len(records) - len(pairs)
    summary["recovery"] = {
        mode: aggregate_recovery([r.get("scans", {}).get(mode, {}) for r in records])
        for mode in ("baseline", "enhanced")
    }
    return summary


def complete_pair(record):
    return (not record.get("error") and all(
        record.get("scans", {}).get(mode, {}).get("status") == "ok"
        and record.get("scans", {}).get(mode, {}).get("recovery", {}).get("status") == "complete"
        for mode in ("baseline", "enhanced")))


def load_prior_run(path, charts, binary, catalog_path):
    """Load completed cases across an immutable continuation chain."""
    path = Path(path).resolve()
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    old_charts = json.loads((path / "selected-charts.json").read_text(encoding="utf-8"))
    if old_charts != charts or manifest["scheduled_chart_directories"] != len(charts):
        raise ValueError("prior run selected a different chart corpus")
    if manifest["kics_binary_sha256"] != digest(binary):
        raise ValueError("prior run used a different KICS binary")
    if manifest["catalog_sha256"] != digest(catalog_path):
        raise ValueError("prior run used a different catalog")
    records = []
    if manifest.get("continued_from"):
        ancestor = manifest["continued_from"]
        ancestor_path = Path(ancestor["path"])
        for name, expected in (("manifest.json", ancestor["manifest_sha256"]),
                               ("executions.jsonl", ancestor["executions_sha256"]),
                               ("selected-charts.json", ancestor["selected_charts_sha256"])):
            if digest(ancestor_path / name) != expected:
                raise ValueError(f"prior run ancestry changed: {name}")
        records, _ = load_prior_run(ancestor_path,
                                    charts, binary, catalog_path)
    by_id = {record["case_id"]: record for record in records}
    local_ids = set()
    retried = set(manifest.get("retried_cases") or [])
    with (path / "executions.jsonl").open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            record = json.loads(line)
            case_id = record.get("case_id")
            try:
                index = int(case_id)
            except (ValueError, TypeError):
                raise ValueError(f"invalid case ID at row {line_number}") from None
            if (case_id != f"{index:06d}" or index < 0 or index >= len(charts)
                    or record.get("chart") != charts[index]["chart"]
                    or case_id in local_ids
                    or (case_id in by_id and case_id not in retried)):
                raise ValueError(f"prior run has an invalid or duplicate case at row {line_number}")
            local_ids.add(case_id)
            by_id[case_id] = record
    if len(by_id) > len(charts):
        raise ValueError("prior run has more records than selected charts")
    records = sorted(by_id.values(), key=lambda record: record["case_id"])
    return records, {
        "path": str(path), "records": len(records),
        "manifest_sha256": digest(path / "manifest.json"),
        "executions_sha256": digest(path / "executions.jsonl"),
        "selected_charts_sha256": digest(path / "selected-charts.json"),
    }


def run_chart(index, chart, output, binary, assets, timeout, work_dir, kics_parallel):
    record = dict(chart, case_id=f"{index:06d}", scans={})
    case_dir = output / "raw" / record["case_id"]
    case_dir.mkdir()
    try:
        root = Path(chart["repo_root"])
        if not (Path(chart["chart"]) / "Chart.yaml").is_file():
            raise ValueError("catalog chart is missing Chart.yaml")
        record["repo_revision"] = git(root, "rev-parse", "HEAD")
        record["repo_status"] = git(root, "status", "--porcelain")
        # Each chart owns its snapshot, work copies, Helm caches, and raw output.
        with tempfile.TemporaryDirectory(prefix="kics-replay-", dir=work_dir) as temp:
            temp = Path(temp)
            snapshot = temp / "snapshot"
            original_hashes = tree_manifest(root)
            shutil.copytree(root, snapshot, ignore=shutil.ignore_patterns(".git"))
            hashes = tree_manifest(snapshot)
            if hashes != original_hashes:
                raise ValueError("input changed while creating snapshot")
            write_json(case_dir / "input-files.json", hashes)
            record["input_sha256"] = manifest_hash(hashes)
            for mode in ("baseline", "enhanced"):
                work = temp / mode
                shutil.copytree(snapshot, work)
                before = tree_manifest(work)
                if before != hashes:
                    raise ValueError("paired input hashes differ")
                env = os.environ.copy()
                env["DISABLE_CRASH_REPORT"] = "1"
                cache = temp / (mode + "-helm")
                for key, sub in (
                        ("HELM_CACHE_HOME", "cache"), ("HELM_CONFIG_HOME", "config"),
                        ("HELM_DATA_HOME", "data")):
                    env[key] = str(cache / sub)
                record["scans"][mode] = scan_chart(
                    binary, assets, work / chart["chart_relative"],
                    case_dir / mode, mode == "enhanced", timeout, env, kics_parallel)
                after = tree_manifest(work)
                changed = {p: h for p, h in after.items() if before.get(p) != h}
                removed = sorted(set(before) - set(after))
                write_json(case_dir / mode / "filesystem-changes.json",
                           {"changed": changed, "removed": removed})
                for path in changed:
                    target = case_dir / mode / "changed-files" / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(work / path, target)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        record["error"] = str(exc)
    return record


def execute_remaining(charts, records, new_records, stream, workers, task):
    """Keep only workers charts in flight and journal each completed case."""
    completed = {record["case_id"] for record in records}
    remaining = iter((index, chart) for index, chart in enumerate(charts)
                     if f"{index:06d}" not in completed)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}

        def submit_next():
            try:
                index, chart = next(remaining)
            except StopIteration:
                return False
            pending[pool.submit(task, index, chart)] = (index, chart)
            return True

        for _ in range(workers):
            if not submit_next():
                break
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                index, chart = pending.pop(future)
                record = future.result()
                records.append(record)
                new_records.append(record)
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                print(f"{len(records)}/{len(charts)} case {index + 1} {chart['chart']}",
                      flush=True)
                submit_next()
def run(args):
    catalog_path = Path(args.catalog).resolve()
    if args.resume_from:
        source = Path(args.resume_from).resolve()
        selection = json.loads((source / "selection.json").read_text(encoding="utf-8"))
        charts = json.loads((source / "selected-charts.json").read_text(encoding="utf-8"))
    else:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
        catalog, selection = prepare_corpus(
            catalog, args.github_seeds, args.artifacthub_seeds, args.top,
            Path(__file__).resolve().parents[2] / "helm_fetcher")
        charts = select_charts(catalog)
        if args.limit:
            charts = charts[:args.limit]
    if args.prepare_only:
        output = Path(args.out).resolve()
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / "selection.json", selection)
        write_json(output / "selected-charts.json", charts)
        print(json.dumps({k: v for k, v in selection.items()
                          if k not in ("repositories", "discovery_log")}, indent=2))
        return
    binary, repo = Path(args.kics_bin).resolve(), Path(args.kics_repo).resolve()
    assets, catalog_path = repo / "assets", Path(args.catalog).resolve()
    output = Path(args.out).resolve()
    if output.exists():
        raise ValueError("output directory already exists; choose a new immutable run ID")
    if not binary.is_file() or not (assets / "queries").is_dir():
        raise ValueError("KICS executable and local assets are required")
    prior_records, prior_evidence = (load_prior_run(
        args.resume_from, charts, binary, catalog_path) if args.resume_from else ([], None))
    retry_cases = ([record["case_id"] for record in prior_records
                    if not complete_pair(record)] if args.retry_errors else [])
    if retry_cases:
        retry_set = set(retry_cases)
        prior_records = [record for record in prior_records
                         if record["case_id"] not in retry_set]
    revision = git(repo, "rev-parse", "HEAD")
    dirty = git(repo, "status", "--porcelain", "--untracked-files=no")
    build_evidence = None
    if args.build_evidence:
        build_evidence = verify_build(args.build_evidence, repo, binary)
    elif dirty:
        raise ValueError("instrumented working-tree builds require --build-evidence")
    build_info = subprocess.check_output(["go", "version", "-m", str(binary)],
                                         encoding="utf-8", errors="replace")
    if f"vcs.revision={revision}" not in build_info or ("vcs.modified=true" in build_info and not build_evidence):
        raise ValueError("binary build revision must match clean local KICS HEAD; rebuild with -buildvcs=true")
    for chart in charts:
        if output.is_relative_to(Path(chart["repo_root"])):
            raise ValueError("run output must be outside corpus repositories")
    output.mkdir(parents=True)
    write_json(output / "selection.json", selection)
    write_json(output / "selected-charts.json", charts)
    if build_evidence:
        write_json(output / "build-evidence.json", build_evidence)
    (output / "raw").mkdir()
    (output / "final").mkdir()
    manifest = {
        "started_at": utc(), "status": "running",
        "collection_evidence": args.collection_evidence,
        "catalog": str(catalog_path), "catalog_sha256": digest(catalog_path),
        "kics_revision": revision, "kics_binary": str(binary),
        "kics_binary_sha256": digest(binary), "go_build_info": build_info,
        "runner_sha256": digest(__file__), "timeout_seconds": args.timeout,
        "runner_sources": {name: digest(Path(__file__).with_name(name)) for name in
                          ("kics_corpus_replay.py", "corpus_selection.py",
                           "recovery_results.py", "kics_build.py")},
        "limit": args.limit, "scheduled_chart_directories": len(charts),
        "scope": "standalone charts rediscovered in retained top-ranked seed repositories",
        "seed_selection": selection,
        "eligible_chart_directories_before_limit": selection["chart_directories"],
        "recovery_rate": "completed initial failures recovered, from KICS render-report schema 1",
        "source_build_evidence": str(Path(args.build_evidence).resolve()) if args.build_evidence else None,
        "image_cves": "not scanned; image BoM records image references only",
        "chart_workers": args.workers, "kics_parallel_per_scan": args.kics_parallel,
        "continued_from": prior_evidence,
        "prior_retained_records": len(prior_records),
        "retried_cases": retry_cases,
    }
    write_json(output / "manifest.json", manifest)
    records = list(prior_records)
    new_records = []
    try:
        with (output / "executions.jsonl").open("x", encoding="utf-8") as stream:
            execute_remaining(
                charts, records, new_records, stream, args.workers,
                lambda index, chart: run_chart(index, chart, output, binary, assets,
                                               args.timeout, args.work_dir,
                                               args.kics_parallel))
        summary = summarize(records)
        write_json(output / "final" / "summary.json", summary)
        pairs = summary["paired_successful_scans"]
        text = (
            "# KICS corpus replay\n\n"
            f"Scheduled chart directories: {len(charts)}. Paired successful scans: {pairs}.\n\n"
            "| Metric | Baseline | Enhanced |\n|---|---:|---:|\n"
            f"| Findings on the same {pairs} chart directories | "
            f"{summary['paired_findings']['baseline']} | {summary['paired_findings']['enhanced']} |\n\n"
            f"Paired finding gain: {summary['paired_finding_gain']}. "
            f"Excluded from paired comparison: {summary['excluded_from_paired_comparison']}.\n\n"
            "These are misconfiguration finding occurrences, not unique vulnerabilities, "
            "image CVEs, or render recovery rates. See raw reports and execution logs.\n"
        )
        text += recovery_markdown(summary["recovery"]["enhanced"])
        (output / "final" / "report.md").write_text(text, encoding="utf-8")
        write_json(output / "final" / "recovery.json", summary["recovery"])
        manifest["status"] = "completed"
    except BaseException:
        manifest["status"] = "interrupted"
        raise
    finally:
        manifest["finished_at"] = utc()
        manifest["terminal_records"] = len(records)
        manifest["new_records"] = len(new_records)
        write_json(output / "manifest.json", manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True)
    fetcher = Path(__file__).resolve().parents[2] / "helm_fetcher"
    parser.add_argument("--github-seeds", default=str(
        fetcher / "runs/20260528_215041_github/github_search.json"))
    parser.add_argument("--artifacthub-seeds", default=str(
        fetcher / "runs/20260529_135859_artifacthub/artifacthub_search.json"))
    parser.add_argument("--top", type=int, default=100, help="first N saved entries per source")
    parser.add_argument("--prepare-only", action="store_true",
                        help="write seed inventory and chart list without executing KICS")
    parser.add_argument("--kics-repo")
    parser.add_argument("--kics-bin")
    parser.add_argument("--build-evidence", help="build.json created by kics_build.py for instrumented builds")
    parser.add_argument("--out", required=True)
    parser.add_argument("--collection-evidence", default="unknown; consult original collection logs")
    parser.add_argument("--work-dir", help="existing directory for isolated copies, preferably on D:")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--limit", type=int, default=0, help="pilot chart limit; 0 means all")
    parser.add_argument("--workers", type=int, default=1, help="chart jobs run concurrently")
    parser.add_argument("--kics-parallel", type=int, default=0,
                        help="workers per KICS platform; 0 keeps KICS automatic default")
    parser.add_argument("--resume-from", help="read completed prefix from prior immutable run")
    parser.add_argument("--retry-errors", action="store_true",
                        help="rerun prior cases without two successful scans and complete recovery reports")
    args = parser.parse_args()
    if args.timeout <= 0 or args.limit < 0 or args.top <= 0 or args.workers <= 0 or args.kics_parallel < 0:
        parser.error("timeout, top, and workers must be positive; limit and kics-parallel must be nonnegative")
    if args.prepare_only and args.resume_from:
        parser.error("--prepare-only and --resume-from cannot be combined")
    if args.retry_errors and not args.resume_from:
        parser.error("--retry-errors requires --resume-from")
    if not args.prepare_only and (not args.kics_repo or not args.kics_bin):
        parser.error("--kics-repo and --kics-bin are required for scans")
    run(args)


if __name__ == "__main__":
    main()
