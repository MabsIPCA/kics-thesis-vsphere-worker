"""Validate KICS render JSONL and aggregate only completed rendering evidence."""
import json
from pathlib import Path


def load_recovery(path):
    path = Path(path)
    if not path.exists():
        return {"status": "missing", "started": 0, "completed": 0,
                "incomplete": 0, "records": []}
    started, finished, latest = {}, {}, {}
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            event = json.loads(line)
            trace = event["trace"]
            key = trace["id"]
            if trace["schema_version"] != 1:
                raise ValueError("unsupported render report schema")
            kind = event["event"]
            if kind == "start":
                if key in started:
                    raise ValueError("duplicate render start")
                started[key] = trace
            elif key not in started or key in finished:
                raise ValueError("event outside an active render")
            if kind not in ("start", "attempt", "fix", "finish"):
                raise ValueError("unknown render report event")
            latest[key] = trace
            if kind == "finish":
                initial, final = trace["initial_outcome"], trace["final_outcome"]
                if initial not in ("success", "failure") or final not in ("success", "failure"):
                    raise ValueError("invalid terminal render outcome")
                recovered = initial == "failure" and final == "success"
                if trace["recovered"] != recovered:
                    raise ValueError("recovery flag disagrees with render outcomes")
                if initial == "failure" and not trace.get("initial_kind"):
                    raise ValueError("missing initial failure category")
                configuration_key(trace)
                finished[key] = trace
        incomplete = len(started) - len(finished)
        return {"status": "complete" if started and not incomplete else "incomplete",
                "started": len(started), "completed": len(finished),
                "incomplete": incomplete, "records": list(finished.values()),
                "unfinished": [v for k, v in latest.items() if k not in finished]}
    except (ValueError, KeyError, TypeError) as exc:
        return {"status": "invalid", "error": str(exc), "started": len(started),
                "completed": len(finished), "incomplete": len(started) - len(finished),
                "records": list(finished.values())}


def configuration_key(trace):
    # Deduplicate repeated resolver passes within a scan, preserving first outcome.
    # Options include the ordered values list and explicit overrides, hashed by KICS.
    return json.dumps([trace["chart"], trace["options_sha256"],
                       trace["default_values"].get("sha256"),
                       [(v["path"], v.get("sha256")) for v in trace["values_files"]]],
                      separators=(",", ":"))


def aggregate_recovery(scans):
    result = {
        "reporting_scans": 0, "excluded_scans": 0,
        "completed_renders": 0, "initial_successes": 0, "initial_failures": 0,
        "render_invocations": 0, "repeated_configuration_invocations": 0,
        "repeat_outcome_changes": 0,
        "recovered": 0, "unresolved": 0, "by_initial_kind": {}, "stop_reasons": {},
        "denominator": "initially failed distinct configurations, first observation per scan with complete reporting",
        "incomplete_or_invalid_reports": 0,
    }
    for scan in scans:
        evidence = scan.get("recovery", {})
        if scan.get("status") != "ok" or evidence.get("status") != "complete":
            result["excluded_scans"] += 1
            if evidence.get("status") != "complete":
                result["incomplete_or_invalid_reports"] += 1
            continue
        result["reporting_scans"] += 1
        seen = {}
        for trace in evidence["records"]:
            result["render_invocations"] += 1
            identity = configuration_key(trace)
            outcome = (trace["initial_outcome"], trace["final_outcome"], trace.get("initial_kind"))
            if identity in seen:
                result["repeated_configuration_invocations"] += 1
                if seen[identity] != outcome:
                    result["repeat_outcome_changes"] += 1
                continue
            seen[identity] = outcome
            result["completed_renders"] += 1
            reason = trace["stop_reason"]
            result["stop_reasons"][reason] = result["stop_reasons"].get(reason, 0) + 1
            if trace["initial_outcome"] == "success":
                result["initial_successes"] += 1
                continue
            result["initial_failures"] += 1
            kind = trace["initial_kind"]
            group = result["by_initial_kind"].setdefault(
                kind, {"initial_failures": 0, "recovered": 0, "unresolved": 0})
            group["initial_failures"] += 1
            outcome = "recovered" if trace["recovered"] else "unresolved"
            result[outcome] += 1
            group[outcome] += 1
    for group in [result, *result["by_initial_kind"].values()]:
        n = group["initial_failures"]
        group["recovery_rate_percent"] = 100 * group["recovered"] / n if n else None
    assert result["completed_renders"] == result["initial_successes"] + result["initial_failures"]
    assert result["initial_failures"] == result["recovered"] + result["unresolved"]
    return result


def recovery_markdown(summary):
    lines = [
        "\n## Enhanced rendering recovery\n",
        f"Complete reporting scans: {summary['reporting_scans']}; excluded scans: {summary['excluded_scans']}.\n",
        f"Render invocations: {summary['render_invocations']}; distinct configurations: {summary['completed_renders']}; "
        f"repeated invocations: {summary['repeated_configuration_invocations']}; "
        f"repeats with changed outcomes: {summary['repeat_outcome_changes']}.\n",
        f"Initial successes: {summary['initial_successes']}; initial failures: {summary['initial_failures']}; "
        f"recovered: {summary['recovered']}; unresolved: {summary['unresolved']}.\n",
        "| Initial failure type | Failures | Recovered | Unresolved | Recovery rate |",
        "|---|---:|---:|---:|---:|",
    ]
    for kind, group in sorted(summary["by_initial_kind"].items()):
        rate = group["recovery_rate_percent"]
        rate_text = f"{rate:.1f}%" if rate is not None else "n/a"
        lines.append(f"| {kind} | {group['initial_failures']} | {group['recovered']} | "
                     f"{group['unresolved']} | {rate_text} |")
    lines.extend([
        "",
        "Unit: a distinct chart and ordered values configuration, taking its first observation per scan. "
        "Repeated resolver passes are retained but do not inflate the denominator. "
        "Failures are grouped by their first observed error; later error transitions remain in the raw trace. "
        "This is the integrated KICS recovery rate, not the historical CLI/library agreement experiment.",
        "Baseline renders defaults; enhanced also explores values combinations. "
        "Their render denominators differ and are reported separately.",
        "",
    ])
    return "\n".join(lines)
