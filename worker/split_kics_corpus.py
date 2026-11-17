#!/usr/bin/env python3
"""Freeze, divide, run and merge the pinned KICS corpus across Windows and Linux.

This script is deliberately separate from the live corpus runner. A split plan
can only be made from a stopped, immutable run directory.
"""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile


MODULES = ("kics_corpus_replay.py", "corpus_selection.py", "recovery_results.py", "kics_build.py")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def runner(worker_dir):
    worker_dir = Path(worker_dir).resolve()
    if not all((worker_dir / name).is_file() for name in MODULES):
        raise ValueError(f"runner modules missing from {worker_dir}")
    sys.path.insert(0, str(worker_dir))
    import kics_corpus_replay
    return kics_corpus_replay


def source_hash(evidence):
    return hashlib.sha256(json.dumps(evidence["source_files"], sort_keys=True).encode()).hexdigest()


def verify_source(repo, evidence):
    repo = Path(repo).resolve()
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if head != evidence["head"]:
        raise ValueError("KICS source revision differs from the pinned build")
    for relative, expected in evidence["source_files"].items():
        path = repo / relative
        actual = sha(path) if path.is_file() else None
        if actual != expected:
            raise ValueError(f"KICS source differs: {relative}")
    listed = subprocess.check_output(
        ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    ).decode("utf-8").split("\0")
    if set(filter(None, listed)) != set(evidence["source_files"]):
        raise ValueError("KICS source file set differs from the pinned build")


def verify_source_command(args):
    verify_source(args.kics_repo, read_json(args.build_evidence))
    print("KICS source matches pinned build evidence")


def load_plan(path):
    path = Path(path).resolve()
    plan = read_json(path)
    if plan.get("schema") != 1:
        raise ValueError("unsupported split plan schema")
    selected = path.with_name("selected-charts.json")
    if sha(selected) != plan["selected_charts_sha256"]:
        raise ValueError("selected chart list differs from the split plan")
    charts = read_json(selected)
    if len(charts) != plan["scheduled_charts"]:
        raise ValueError("split plan chart count differs from selection")
    vm = set(plan["vm_case_ids"])
    windows = set(plan["windows_case_ids"])
    pilot = set(plan["pilot_case_ids"])
    if vm & windows or len(vm) != len(plan["vm_case_ids"]) or len(windows) != len(plan["windows_case_ids"]):
        raise ValueError("split plan assignments overlap or contain duplicates")
    if pilot & (vm | windows) or len(pilot) != len(plan["pilot_case_ids"]):
        raise ValueError("VM pilot cases overlap assigned work or contain duplicates")
    for case_id in vm | windows | pilot:
        index = int(case_id)
        if case_id != f"{index:06d}" or index < 0 or index >= len(charts):
            raise ValueError(f"invalid assigned case ID: {case_id}")
    return path, plan, charts


def freeze_run(args):
    if os.name != "nt":
        raise ValueError("freeze is intended for the Windows runner")
    if args.pid < 1:
        raise ValueError("provide the stopped runner PID")
    check = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"if (Get-Process -Id {args.pid} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}"],
        capture_output=True)
    if check.returncode == 0:
        raise ValueError("runner is still active; stop it before freezing")
    children = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "if (Get-Process -Name kics -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"],
        capture_output=True)
    if children.returncode == 0:
        raise ValueError("a KICS scan process is still active; wait for it to exit")
    source = Path(args.source_run).resolve()
    manifest_path = source / "manifest.json"
    manifest = read_json(manifest_path)
    if manifest["status"] == "completed":
        raise ValueError("run already completed")
    execution_path = source / "executions.jsonl"
    with execution_path.open("rb") as stream:
        stream.seek(-1, os.SEEK_END)
        if stream.read(1) != b"\n":
            raise ValueError("execution log ends with an incomplete line")
    with execution_path.open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("execution log has duplicate case IDs")
    manifest.update(status="frozen_for_split", frozen_at=stamp(),
                    terminal_records=manifest.get("prior_retained_records", 0) + len(rows),
                    new_records=len(rows), freeze_reason=args.reason)
    write_json(manifest_path, manifest)
    print(f"Frozen {len(rows)} new records in {source}")


def plan_split(args):
    replay = runner(args.worker_dir)
    source = Path(args.source_run).resolve()
    manifest_path = source / "manifest.json"
    manifest = read_json(manifest_path)
    if manifest["status"] == "running":
        raise ValueError("stop and freeze the current runner before assigning cases")
    selected_path = source / "selected-charts.json"
    charts = read_json(selected_path)
    binary = Path(manifest["kics_binary"])
    catalog = Path(manifest["catalog"])
    records, _ = replay.load_prior_run(source, charts, binary, catalog)
    latest = {r["case_id"]: r for r in records}
    accepted = set(args.accept_incomplete)
    if not accepted <= latest.keys():
        raise ValueError("accepted incomplete case has no retained record")
    pending = [i for i in range(len(charts)) if f"{i:06d}" not in accepted
               and not replay.complete_pair(latest.get(f"{i:06d}", {}))]
    repo = Path(args.vm_repo).resolve()
    target = [i for i in pending if Path(charts[i]["repo_root"]).resolve() == repo]
    if not target:
        raise ValueError("VM repository has no pending charts")
    if not 0 < args.vm_share < 1:
        raise ValueError("VM share must be between zero and one")
    vm_indices = {index for position, index in enumerate(target)
                  if int((position + 1) * args.vm_share) > int(position * args.vm_share)}
    if not vm_indices or len(vm_indices) == len(pending):
        raise ValueError("split left one machine without work")
    vm_key = getattr(args, "vm_repo_key", None)
    if vm_key is None:
        vm_key = hashlib.sha256(str(repo).encode()).hexdigest()[:12] + "-" + repo.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", vm_key) or vm_key in (".", ".."):
        raise ValueError("VM repository key must be a simple directory name")
    vm_hashes = replay.tree_manifest(repo)
    vm_hash = replay.manifest_hash(vm_hashes)
    vm_bytes = sum((repo / relative).stat().st_size for relative in vm_hashes)
    pilot = [case_id for case_id, record in sorted(latest.items())
             if replay.complete_pair(record)
             and Path(charts[int(case_id)]["repo_root"]).resolve() == repo
             and record.get("input_sha256") == vm_hash][:args.pilot_count]
    if len(pilot) < args.pilot_count:
        raise ValueError("not enough completed same-input charts for the VM pilot")
    evidence = read_json(args.build_evidence)
    if evidence["head"] != manifest["kics_revision"]:
        raise ValueError("KICS source evidence differs from the run")
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(selected_path, output / "selected-charts.json")
    plan = {
        "schema": 1, "created_at": stamp(), "source_run": str(source),
        "source_manifest_sha256": sha(manifest_path),
        "source_executions_sha256": sha(source / "executions.jsonl"),
        "selected_charts_sha256": sha(output / "selected-charts.json"),
        "scheduled_charts": len(charts), "retained_case_ids": sorted(latest),
        "accepted_incomplete_ids": sorted(accepted),
        "vm_case_ids": [f"{i:06d}" for i in pending if i in vm_indices],
        "windows_case_ids": [f"{i:06d}" for i in pending if i not in vm_indices],
        "pilot_case_ids": pilot,
        "vm_repo_source": str(repo), "vm_repo_key": vm_key,
        "vm_repo_input_sha256": vm_hash,
        "vm_repo_files": len(vm_hashes),
        "vm_repo_bytes": vm_bytes,
        "kics_revision": evidence["head"],
        "kics_source_sha256": source_hash(evidence),
        "kics_build_evidence_sha256": sha(args.build_evidence),
        "assignment_method": "interleaved fraction of pending charts in one repository",
        "vm_share": args.vm_share,
    }
    write_json(output / "plan.json", plan)
    print(json.dumps({"plan": str(output / "plan.json"), "retained": len(latest),
                      "vm": len(plan["vm_case_ids"]), "windows": len(plan["windows_case_ids"]),
                      "vm_repo_files": len(vm_hashes), "vm_repo_bytes": vm_bytes,
                      "pilot": pilot}, indent=2))


def package_vm(args):
    plan_path, plan, _ = load_plan(args.plan)
    evidence = read_json(args.build_evidence)
    if sha(args.build_evidence) != plan["kics_build_evidence_sha256"]:
        raise ValueError("KICS build evidence differs from the split plan")
    if source_hash(evidence) != plan["kics_source_sha256"]:
        raise ValueError("KICS source hash differs from the split plan")
    verify_source(args.kics_repo, evidence)
    replay = runner(args.worker_dir)
    repo = Path(plan["vm_repo_source"])
    if replay.manifest_hash(replay.tree_manifest(repo)) != plan["vm_repo_input_sha256"]:
        raise ValueError("VM corpus repository changed since split planning")
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(plan_path, output / "plan.json")
    shutil.copy2(plan_path.with_name("selected-charts.json"), output / "selected-charts.json")
    shutil.copy2(args.build_evidence, output / "build.json")
    shutil.copy2(Path(__file__), output / "split_kics_corpus.py")
    shutil.copy2(Path(__file__).with_name("bootstrap_kics_vm.sh"), output / "bootstrap_kics_vm.sh")
    for name in MODULES:
        shutil.copy2(Path(args.worker_dir) / name, output / name)
    staged = getattr(args, "source_tar", None)
    if staged:
        staged = Path(staged).resolve()
        provenance = read_json(staged.with_name("source-stage.json"))
        if (provenance["build_evidence_sha256"] != sha(args.build_evidence)
                or provenance["source_sha256"] != source_hash(evidence)
                or provenance["tar_sha256"] != sha(staged)):
            raise ValueError("staged KICS source differs from pinned build evidence")
        shutil.copy2(staged, output / "kics-source.tar")
    else:
        make_source_tar(args.kics_repo, evidence, output / "kics-source.tar")
    package = {
        "plan_sha256": sha(output / "plan.json"),
        "kics_source_tar_sha256": sha(output / "kics-source.tar"),
        "created_at": stamp(),
    }
    if getattr(args, "existing_corpus", False):
        package["corpus_mode"] = "existing; verify against split plan before scanning"
    else:
        with tarfile.open(output / "vm-corpus.tar", "w") as archive:
            archive.add(repo, arcname=f"repos/{plan['vm_repo_key']}")
        package["vm_corpus_tar_sha256"] = sha(output / "vm-corpus.tar")
    write_json(output / "package.json", package)
    print(output)


def make_source_tar(repo, evidence, destination):
    repo = Path(repo).resolve()
    with tarfile.open(destination, "w") as archive:
        archive.add(repo / ".git", arcname="kics/.git")
        for relative, expected in evidence["source_files"].items():
            if expected is not None:
                archive.add(repo / relative, arcname=f"kics/{relative}", recursive=False)


def stage_source(args):
    evidence = read_json(args.build_evidence)
    verify_source(args.kics_repo, evidence)
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=False)
    archive = output / "kics-source.tar"
    make_source_tar(args.kics_repo, evidence, archive)
    write_json(output / "source-stage.json", {
        "created_at": stamp(), "head": evidence["head"],
        "build_evidence_sha256": sha(args.build_evidence),
        "source_sha256": source_hash(evidence),
        "tar_sha256": sha(archive),
    })
    print(output / "source-stage.json")


def assigned_chart(index, chart, side, plan, corpus_root):
    chart = dict(chart)
    if side in ("vm", "pilot"):
        source = Path(chart["repo_root"])
        if source != Path(plan["vm_repo_source"]):
            raise ValueError(f"VM case {index} is outside its assigned repository")
        root = Path(corpus_root).resolve() / plan["vm_repo_key"]
        chart["repo_root"] = str(root)
        chart["chart"] = str(root / chart["chart_relative"])
    return chart


def overlay_changes(upper, baseline_hashes, case_dir, mode):
    """Capture overlay copy-ups and whiteouts without rereading the lower tree."""
    changed, removed = {}, set()
    for base, dirs, files in os.walk(upper, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            if path.is_symlink():
                raise ValueError(f"linked scan output: {path}")
        for name in files:
            path = Path(base) / name
            relative = path.relative_to(upper).as_posix()
            info = path.lstat()
            if stat.S_ISCHR(info.st_mode) and info.st_rdev == 0:
                removed.add(relative)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"unexpected overlay output: {path}")
            try:
                whiteout = os.getxattr(path, "user.overlay.whiteout") == b"y"
            except (OSError, AttributeError):
                whiteout = False
            if whiteout:
                removed.add(relative)
                continue
            digest = sha(path)
            if baseline_hashes.get(relative) != digest:
                changed[relative] = digest
                target = case_dir / mode / "changed-files" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    return changed, sorted(removed)


def overlay_run_chart(index, chart, output, binary, assets, timeout, work_dir,
                      kics_parallel, input_hashes, input_sha256, replay,
                      repo_revision, repo_status):
    """Use independent writable overlays on the same verified, immutable input."""
    record = dict(chart, case_id=f"{index:06d}", scans={},
                  input_sha256=input_sha256, work_strategy="overlayfs")
    case_dir = output / "raw" / record["case_id"]
    case_dir.mkdir()
    try:
        root = Path(chart["repo_root"])
        if not (Path(chart["chart"]) / "Chart.yaml").is_file():
            raise ValueError("catalog chart is missing Chart.yaml")
        record["repo_revision"] = repo_revision
        record["repo_status"] = repo_status
        shared_inputs = output / "input-files.json"
        try:
            os.link(shared_inputs, case_dir / "input-files.json")
        except OSError:
            replay.write_json(case_dir / "input-files.json", input_hashes)
        with tempfile.TemporaryDirectory(prefix="kics-overlay-", dir=work_dir) as folder:
            temp = Path(folder)
            for mode in ("baseline", "enhanced"):
                upper, overlay_work, merged = (temp / mode / sub
                                               for sub in ("upper", "work", "merged"))
                for directory in (upper, overlay_work, merged):
                    directory.mkdir(parents=True)
                options = f"lowerdir={root},upperdir={upper},workdir={overlay_work}"
                subprocess.run(["mount", "-t", "overlay", "overlay", "-o", options,
                                str(merged)], check=True, capture_output=True)
                try:
                    env = os.environ.copy()
                    env["DISABLE_CRASH_REPORT"] = "1"
                    cache = temp / (mode + "-helm")
                    for key, sub in (("HELM_CACHE_HOME", "cache"),
                                     ("HELM_CONFIG_HOME", "config"),
                                     ("HELM_DATA_HOME", "data")):
                        env[key] = str(cache / sub)
                    record["scans"][mode] = replay.scan_chart(
                        binary, assets, merged / chart["chart_relative"],
                        case_dir / mode, mode == "enhanced", timeout, env, kics_parallel)
                    changed, removed = overlay_changes(upper, input_hashes, case_dir, mode)
                    replay.write_json(case_dir / mode / "filesystem-changes.json",
                                      {"changed": changed, "removed": removed})
                finally:
                    subprocess.run(["umount", str(merged)], check=True, capture_output=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        record["error"] = str(exc)
    return record


def run_worker(args):
    plan_path, plan, charts = load_plan(args.plan)
    replay = runner(args.worker_dir)
    evidence = read_json(args.build_evidence)
    if source_hash(evidence) != plan["kics_source_sha256"]:
        raise ValueError("KICS source differs from the split plan")
    verify_source(args.kics_repo, evidence)
    binary = Path(args.kics_bin).resolve()
    build_info = subprocess.check_output(["go", "version", "-m", str(binary)], text=True)
    if f"vcs.revision={plan['kics_revision']}" not in build_info:
        raise ValueError("KICS binary revision differs from the split plan")
    use_overlay = getattr(args, "overlay", False)
    if use_overlay and args.side == "windows":
        raise ValueError("overlay mode is for Linux workers only")
    ids = plan[f"{args.side}_case_ids"]
    delegated_ids = set()
    delegate_sha = None
    if args.delegate_plan:
        if args.side != "windows":
            raise ValueError("--delegate-plan applies only to the Windows side")
        delegation = read_json(args.delegate_plan)
        delegate_sha = sha(args.delegate_plan)
        if (delegation.get("status") != "active"
                or delegation.get("parent_plan_sha256") != sha(plan_path)
                or not delegation.get("windows_cutover")):
            raise ValueError("vSphere delegation is not active or differs from the frozen plan")
        delegated_ids = set(delegation["assigned_case_ids"])
        if (len(delegated_ids) != len(delegation["assigned_case_ids"])
                or not delegated_ids <= set(ids)):
            raise ValueError("delegated case IDs are invalid")
        ids = [case_id for case_id in ids if case_id not in delegated_ids]
    input_hashes = None
    repo_revision = None
    repo_status = "not_collected_overlayfs; complete source manifest verified"
    if args.side in ("vm", "pilot"):
        if not args.corpus_root:
            raise ValueError("VM worker needs --corpus-root")
        root = Path(args.corpus_root).resolve() / plan["vm_repo_key"]
        input_hashes = replay.tree_manifest(root)
        if replay.manifest_hash(input_hashes) != plan["vm_repo_input_sha256"]:
            raise ValueError("transferred corpus differs from the Windows source")
        if use_overlay:
            repo_revision = replay.git(root, "rev-parse", "HEAD")
    output = Path(args.out).resolve()
    manifest_path = output / "manifest.json"
    if output.exists():
        if not args.resume:
            raise ValueError("output exists; use --resume only for the same worker run")
        manifest = read_json(manifest_path)
        if (manifest["plan_sha256"] != sha(plan_path) or manifest["side"] != args.side
                or manifest["binary_sha256"] != sha(binary)
                or manifest.get("work_strategy", "copytree")
                != ("overlayfs" if use_overlay else "copytree")):
            raise ValueError("worker output provenance differs from this run")
        if args.side == "windows" and args.delegate_plan:
            previous = manifest.get("delegate_plan_sha256")
            if previous and previous != delegate_sha:
                if delegation.get("supersedes_delegation_sha256") != previous:
                    raise ValueError("Windows delegation changed without a recorded predecessor")
                manifest.setdefault("delegation_history", []).append({
                    "plan_sha256": previous,
                    "delegated_case_ids": manifest.get("delegated_case_ids", []),
                    "superseded_at": stamp(),
                    "superseded_by": delegate_sha,
                })
            manifest["assigned_case_ids"] = ids
            manifest["delegated_case_ids"] = sorted(delegated_ids)
            manifest["delegate_plan_sha256"] = delegate_sha
        if use_overlay:
            if manifest.get("repo_revision", repo_revision) != repo_revision:
                raise ValueError("overlay repository revision changed")
            repo_status = manifest.get("repo_status", repo_status)
            manifest["repo_revision"] = repo_revision
            manifest["repo_status"] = repo_status
            shared_inputs = output / "input-files.json"
            if shared_inputs.exists():
                if read_json(shared_inputs) != input_hashes:
                    raise ValueError("overlay input manifest changed")
            else:
                replay.write_json(shared_inputs, input_hashes)
        with (output / "executions.jsonl").open(encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream]
        recorded = {r["case_id"] for r in records}
        if len(recorded) != len(records) or not recorded <= set(ids):
            raise ValueError("worker execution log has duplicate or unassigned cases")
        if args.side == "windows" and args.delegate_plan:
            write_json(manifest_path, manifest)
        aborted = output / "aborted"
        for case_id in ids:
            partial = output / "raw" / case_id
            if case_id not in recorded and partial.exists():
                aborted.mkdir(exist_ok=True)
                partial.rename(aborted / f"{case_id}-{len(list(aborted.glob(case_id + '-*'))):03d}")
    else:
        output.mkdir(parents=True)
        (output / "raw").mkdir()
        (output / "final").mkdir()
        if use_overlay:
            replay.write_json(output / "input-files.json", input_hashes)
        records = []
        manifest = {
            "schema": 1, "status": "running", "started_at": stamp(),
            "side": args.side, "plan_sha256": sha(plan_path),
            "selected_charts_sha256": plan["selected_charts_sha256"],
            "assigned_case_ids": ids, "binary_sha256": sha(binary),
            "kics_revision": plan["kics_revision"],
            "kics_source_sha256": plan["kics_source_sha256"],
            "build_info": build_info, "platform": platform.platform(),
            "python_version": platform.python_version(),
            "workers": args.workers, "kics_parallel": args.kics_parallel,
            "work_strategy": "overlayfs" if use_overlay else "copytree",
        }
        if args.side == "windows" and args.delegate_plan:
            manifest["delegated_case_ids"] = sorted(delegated_ids)
            manifest["delegate_plan_sha256"] = delegate_sha
        if use_overlay:
            manifest["repo_revision"] = repo_revision
            manifest["repo_status"] = repo_status
        write_json(manifest_path, manifest)
        (output / "executions.jsonl").touch()
    recorded = {r["case_id"] for r in records}
    pending_ids = [case_id for case_id in ids if case_id not in recorded]
    assets = Path(args.kics_repo).resolve() / "assets"
    if not (assets / "queries").is_dir() or not (assets / "libraries").is_dir():
        raise ValueError("KICS query assets are missing")
    def task(case_id):
        index = int(case_id)
        original = charts[index]
        mapped = assigned_chart(index, original, args.side, plan, args.corpus_root)
        if use_overlay:
            record = overlay_run_chart(index, mapped, output, binary, assets,
                                       args.timeout, args.work_dir, args.kics_parallel,
                                       input_hashes, plan["vm_repo_input_sha256"], replay,
                                       repo_revision, repo_status)
        else:
            record = replay.run_chart(index, mapped, output, binary, assets,
                                      args.timeout, args.work_dir, args.kics_parallel)
        record["source_repo_root"] = original["repo_root"]
        record["source_chart_relative"] = original["chart_relative"]
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
                        record = future.result()
                        if (args.side in ("vm", "pilot") and record.get("input_sha256")
                                and record["input_sha256"] != plan["vm_repo_input_sha256"]):
                            raise ValueError(f"transferred input changed during case {case_id}")
                        records.append(record)
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                        print(f"{len(records)}/{len(ids)} case {case_id}", flush=True)
                        submit()
        if use_overlay and replay.manifest_hash(replay.tree_manifest(root)) != plan["vm_repo_input_sha256"]:
            raise ValueError("overlay lower input changed during the worker run")
        if use_overlay and replay.git(root, "rev-parse", "HEAD") != repo_revision:
            raise ValueError("overlay repository revision changed during the worker run")
        replay.write_json(output / "final" / "summary.json", replay.summarize(records))
        manifest["status"] = "completed"
    except BaseException:
        manifest["status"] = "interrupted"
        raise
    finally:
        manifest["last_updated_at"] = stamp()
        manifest["terminal_records"] = len(records)
        write_json(manifest_path, manifest)


def merge(args):
    plan_path, plan, charts = load_plan(args.plan)
    replay = runner(args.worker_dir)
    source = Path(plan["source_run"])
    if (sha(source / "manifest.json") != plan["source_manifest_sha256"]
            or sha(source / "executions.jsonl") != plan["source_executions_sha256"]):
        raise ValueError("frozen source run changed after split planning")
    source_manifest = read_json(source / "manifest.json")
    prior, _ = replay.load_prior_run(source, charts, source_manifest["kics_binary"],
                                     source_manifest["catalog"])
    by_id = {r["case_id"]: r for r in prior}
    sources = {case_id: "prior" for case_id in by_id}
    worker_evidence = {}
    if plan["pilot_case_ids"]:
        if not args.pilot_output:
            raise ValueError("VM pilot output is required before merging platforms")
        pilot_dir = Path(args.pilot_output).resolve()
        pilot_manifest = read_json(pilot_dir / "manifest.json")
        if (pilot_manifest["status"] != "completed" or pilot_manifest["side"] != "pilot"
                or pilot_manifest["plan_sha256"] != sha(plan_path)
                or pilot_manifest["kics_source_sha256"] != plan["kics_source_sha256"]):
            raise ValueError("VM pilot evidence is incomplete or mismatched")
        with (pilot_dir / "executions.jsonl").open(encoding="utf-8") as stream:
            pilot_rows = [json.loads(line) for line in stream]
        if {r["case_id"] for r in pilot_rows} != set(plan["pilot_case_ids"]):
            raise ValueError("VM pilot case IDs differ from the split plan")
        for record in pilot_rows:
            case_id = record["case_id"]
            original = by_id[case_id]
            if record.get("input_sha256") != original.get("input_sha256"):
                raise ValueError(f"VM pilot input differs: {case_id}")
            for mode in ("baseline", "enhanced"):
                current_scan = record.get("scans", {}).get(mode, {})
                old_scan = original.get("scans", {}).get(mode, {})
                fields = ("status", "total", "severity", "query_ids")
                if any(current_scan.get(field) != old_scan.get(field) for field in fields):
                    raise ValueError(f"VM pilot finding difference: {case_id} {mode}")
                recovery_fields = ("status", "started", "completed", "incomplete")
                if any(current_scan.get("recovery", {}).get(field)
                       != old_scan.get("recovery", {}).get(field)
                       for field in recovery_fields):
                    raise ValueError(f"VM pilot recovery difference: {case_id} {mode}")
        worker_evidence["pilot"] = {
            "path": str(pilot_dir),
            "manifest_sha256": sha(pilot_dir / "manifest.json"),
            "executions_sha256": sha(pilot_dir / "executions.jsonl"),
            "case_ids": plan["pilot_case_ids"],
            "work_strategy": pilot_manifest.get("work_strategy", "copytree"),
        }
    for side, directory in (("vm", args.vm_output), ("windows", args.windows_output)):
        directory = Path(directory).resolve()
        manifest = read_json(directory / "manifest.json")
        if (manifest["status"] != "completed" or manifest["side"] != side
                or manifest["plan_sha256"] != sha(plan_path)
                or manifest["kics_source_sha256"] != plan["kics_source_sha256"]):
            raise ValueError(f"{side} worker has incomplete or mismatched evidence")
        with (directory / "executions.jsonl").open(encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream]
        assigned = set(plan[f"{side}_case_ids"])
        seen = set()
        for record in rows:
            case_id = record["case_id"]
            index = int(case_id)
            if (case_id not in assigned or case_id in seen
                    or record["source_chart_relative"] != charts[index]["chart_relative"]
                    or record["source_repo_root"] != charts[index]["repo_root"]):
                raise ValueError(f"{side} has an invalid case record: {case_id}")
            if (side == "vm" and record.get("input_sha256")
                    and record["input_sha256"] != plan["vm_repo_input_sha256"]):
                raise ValueError(f"VM case input differs from source: {case_id}")
            seen.add(case_id)
            by_id[case_id] = record
            sources[case_id] = side
        if seen != assigned:
            raise ValueError(f"{side} output is missing {len(assigned - seen)} assigned cases")
        worker_evidence[side] = {"path": str(directory),
                                 "manifest_sha256": sha(directory / "manifest.json"),
                                 "executions_sha256": sha(directory / "executions.jsonl"),
                                 "records": len(rows), "platform": manifest["platform"],
                                 "binary_sha256": manifest["binary_sha256"],
                                 "work_strategy": manifest.get("work_strategy", "copytree")}
    if len(by_id) != len(charts):
        raise ValueError(f"merged evidence has {len(by_id)} of {len(charts)} cases")
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=False)
    records = [by_id[f"{i:06d}"] for i in range(len(charts))]
    summary = replay.summarize(records)
    platform_summary = {
        "windows": replay.summarize([r for r in records if sources[r["case_id"]] != "vm"]),
        "vm": replay.summarize([r for r in records if sources[r["case_id"]] == "vm"]),
    }
    write_json(output / "summary.json", summary)
    write_json(output / "platform-summary.json", platform_summary)
    write_json(output / "recovery.json", summary["recovery"])
    write_json(output / "case-sources.json", sources)
    write_json(output / "manifest.json", {
        "created_at": stamp(), "plan_sha256": sha(plan_path),
        "source_manifest_sha256": plan["source_manifest_sha256"],
        "source_executions_sha256": plan["source_executions_sha256"],
        "workers": worker_evidence, "scheduled_charts": len(charts),
        "paired_successful_scans": summary["paired_successful_scans"],
        "unit": "chart-directory scans; findings are KICS result occurrences",
        "platform_note": "Windows and Linux binaries use the same pinned source; OS effects remain possible",
    })
    (output / "report.md").write_text(
        "# Split KICS corpus replay\n\n"
        f"Paired successful scans: {summary['paired_successful_scans']} / {len(charts)}.\n\n"
        f"Baseline findings: {summary['paired_findings']['baseline']}. "
        f"Enhanced findings: {summary['paired_findings']['enhanced']}. "
        f"Gain: {summary['paired_finding_gain']}.\n\n"
        f"Windows paired scans: {platform_summary['windows']['paired_successful_scans']}. "
        f"VM paired scans: {platform_summary['vm']['paired_successful_scans']}.\n\n"
        "Windows and Linux used separate binaries built from the same pinned source. "
        "Inspect per-platform records for OS effects before reporting a combined total. "
        "The Linux worker used overlayfs over a run-level verified corpus; "
        "Windows used per-chart copytree snapshots.\n\n"
        + replay.recovery_markdown(summary["recovery"]["enhanced"]), encoding="utf-8")
    print(output / "report.md")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("freeze", help="mark a stopped Windows run immutable for split planning")
    p.add_argument("--source-run", required=True)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--reason", default="Windows/Ubuntu workload handoff")
    p.set_defaults(func=freeze_run)
    p = sub.add_parser("plan", help="assign cases after stopping the current runner")
    p.add_argument("--source-run", required=True)
    p.add_argument("--vm-repo", required=True)
    p.add_argument("--vm-repo-key", help="existing Linux corpus directory name")
    p.add_argument("--vm-share", type=float, default=0.6)
    p.add_argument("--accept-incomplete", action="append", default=[])
    p.add_argument("--pilot-count", type=int, default=2)
    p.add_argument("--build-evidence", required=True)
    p.add_argument("--worker-dir", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=plan_split)
    p = sub.add_parser("package", help="make a self-contained VM transfer bundle")
    p.add_argument("--plan", required=True)
    p.add_argument("--kics-repo", required=True)
    p.add_argument("--build-evidence", required=True)
    p.add_argument("--worker-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--existing-corpus", action="store_true",
                   help="package KICS without copying the repository tar")
    p.add_argument("--source-tar", help="preverified KICS source archive from stage-source")
    p.set_defaults(func=package_vm)
    p = sub.add_parser("stage-source", help="prepare exact KICS source before run handoff")
    p.add_argument("--kics-repo", required=True)
    p.add_argument("--build-evidence", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=stage_source)
    p = sub.add_parser("verify-source", help="verify the transferred KICS working tree")
    p.add_argument("--kics-repo", required=True)
    p.add_argument("--build-evidence", required=True)
    p.set_defaults(func=verify_source_command)
    p = sub.add_parser("run", help="run one side of the frozen assignment")
    p.add_argument("--plan", required=True)
    p.add_argument("--side", choices=("vm", "windows", "pilot"), required=True)
    p.add_argument("--worker-dir", default=str(Path(__file__).resolve().parent))
    p.add_argument("--kics-repo", required=True)
    p.add_argument("--kics-bin", required=True)
    p.add_argument("--build-evidence", required=True)
    p.add_argument("--corpus-root")
    p.add_argument("--out", required=True)
    p.add_argument("--work-dir", required=True)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--kics-parallel", type=int, default=1)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--overlay", action="store_true",
                   help="use writable Linux overlays over the verified NAS corpus")
    p.add_argument("--delegate-plan",
                   help="active disjoint vSphere delegation to exclude from Windows")
    p.set_defaults(func=run_worker)
    p = sub.add_parser("merge", help="validate and aggregate both finished sides")
    p.add_argument("--plan", required=True)
    p.add_argument("--vm-output", required=True)
    p.add_argument("--windows-output", required=True)
    p.add_argument("--pilot-output")
    p.add_argument("--worker-dir", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=merge)
    args = parser.parse_args()
    if args.command == "run" and (args.workers < 1 or args.timeout < 1 or args.kics_parallel < 0):
        parser.error("workers and timeout must be positive; kics-parallel must be nonnegative")
    if args.command == "plan" and args.pilot_count < 0:
        parser.error("pilot-count must be nonnegative")
    args.func(args)


if __name__ == "__main__":
    main()
