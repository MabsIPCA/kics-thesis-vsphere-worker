"""Select saved ranked seeds and rediscover charts in retained local clones."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit


def canonical_url(value):
    value = value.strip().rstrip("/")
    if value.startswith("git@"):
        value = "https://" + value[4:].replace(":", "/", 1)
    parts = urlsplit(value)
    if not parts.hostname or not parts.path.strip("/"):
        raise ValueError(f"invalid repository URL in saved seeds: {value!r}")
    path = parts.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return parts.hostname.lower() + path.lower()


def choose_repositories(catalog, github_seeds, artifacthub_seeds, top):
    if top <= 0:
        raise ValueError("top per source must be positive")
    seed_info, wanted = [], {}
    for source, filename in (("github", github_seeds), ("artifacthub", artifacthub_seeds)):
        filename = Path(filename).resolve()
        raw = filename.read_bytes()
        rows = json.loads(raw.decode("utf-8-sig"))["repos"]
        if len(rows) < top:
            raise ValueError(f"{source} seed list has fewer than {top} entries")
        seed_info.append({"source": source, "path": str(filename),
                          "sha256": hashlib.sha256(raw).hexdigest(), "selected_entries": top})
        # Slice before deduplicating. Missing entries are not replaced by lower-ranked seeds.
        for rank, row in enumerate(rows[:top], 1):
            url = row["repo_url"]
            key = canonical_url(url)
            wanted.setdefault(key, {"repo_url": url, "memberships": []})
            wanted[key]["memberships"].append({"source": source, "rank": rank})
    by_url = {}
    for repo in catalog:
        if repo.get("repo_url"):
            by_url.setdefault(canonical_url(repo["repo_url"]), []).append(repo)
    selected, inventory = [], []
    for key, seed in wanted.items():
        candidates = by_url.get(key, [])
        record = dict(seed, canonical_url=key)
        present = [r for r in candidates
                   if r.get("cloned_dir") and Path(r["cloned_dir"]).is_dir()]
        if not candidates:
            record["status"] = "missing_catalog_repository"
        elif not present:
            record["status"] = "missing_local_clone"
            record["catalog_clone_paths"] = [r.get("cloned_dir") for r in candidates]
        else:
            # Prefer the first available catalog clone; never double-count two local
            # clones of one canonical repository. Preserve the choice for audit.
            repo = dict(present[0], charts=[])
            repo["cloned_dir"] = str(Path(repo["cloned_dir"]).resolve())
            repo["seed_memberships"] = seed["memberships"]
            selected.append(repo)
            record.update(status="available", cloned_dir=repo["cloned_dir"],
                          other_available_clones=[r["cloned_dir"] for r in present[1:]])
        inventory.append(record)
    return selected, {
        "top_per_source": top, "seed_files": seed_info,
        "seed_entries": 2 * top, "distinct_seed_repositories": len(wanted),
        "duplicate_seed_entries": 2 * top - len(wanted),
        "available_repositories": len(selected), "repositories": inventory,
    }


def native_discovery_path(value):
    # listcharts emits /mnt/d even when run natively on Windows.
    if os.name == "nt" and value.startswith("/mnt/") and len(value) > 7 and value[6] == "/":
        return Path(value[5].upper() + ":/" + value[7:]).resolve()
    return Path(value).resolve()


def prepare_corpus(catalog, github_seeds, artifacthub_seeds, top, fetcher):
    repos, selection = choose_repositories(catalog, github_seeds, artifacthub_seeds, top)
    selection["discovery"] = "helm_fetcher/cmd/listcharts using helm.FindCharts"
    if repos:
        with tempfile.TemporaryDirectory(prefix="kics-selection-") as folder:
            path = Path(folder) / "selected-repositories.json"
            path.write_text(json.dumps(repos), encoding="utf-8")
            result = subprocess.run(
                ["go", "run", "-mod=readonly", "./cmd/listcharts", "-catalog", str(path)],
                cwd=fetcher, capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=True, timeout=1800)
        selection["discovery_log"] = result.stderr.strip()
        roots = [(Path(r["cloned_dir"]).resolve(), r) for r in repos]
        seen = set()
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            chart = native_discovery_path(line.strip())
            if str(chart) in seen:
                continue
            seen.add(str(chart))
            owners = [(root, r) for root, r in roots if chart.is_relative_to(root)]
            if not owners:
                raise ValueError(f"discovery returned a chart outside selected repositories: {chart}")
            _, owner = max(owners, key=lambda pair: len(pair[0].parts))
            owner["charts"].append({"chart_path": str(chart), "runs": []})
    counts = {r["cloned_dir"]: len(r["charts"]) for r in repos}
    for record in selection["repositories"]:
        if record["status"] == "available":
            record["chart_directories"] = counts[record["cloned_dir"]]
            if not record["chart_directories"]:
                record["status"] = "no_standalone_charts"
    selection["repositories_with_charts"] = sum(bool(r["charts"]) for r in repos)
    selection["chart_directories"] = sum(len(r["charts"]) for r in repos)
    return repos, selection
