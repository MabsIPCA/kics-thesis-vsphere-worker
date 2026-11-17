"""Build local KICS and retain source/binary evidence, including uncommitted instrumentation."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def source_files(repo):
    paths = set(git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
                .decode("utf-8").split("\0")) - {""}
    return {p: sha(repo / p) if (repo / p).is_file() else None for p in sorted(paths)}


def verify_build(evidence_path, repo, binary):
    evidence_path = Path(evidence_path).resolve()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence["binary_sha256"] != sha(binary):
        raise ValueError("KICS binary differs from its build evidence")
    if evidence["head"] != git(repo, "rev-parse", "HEAD").decode().strip():
        raise ValueError("KICS HEAD differs from its build evidence")
    if evidence["source_files"] != source_files(repo):
        raise ValueError("KICS working tree differs from its build evidence")
    return evidence


def build(repo, out):
    repo, out = Path(repo).resolve(), Path(out).resolve()
    if out.is_relative_to(repo):
        raise ValueError("place build evidence outside the KICS source repository")
    out.mkdir(parents=True, exist_ok=False)
    before = source_files(repo)
    head = git(repo, "rev-parse", "HEAD").decode().strip()
    (out / "source.patch").write_bytes(git(repo, "diff", "--binary", "HEAD"))
    untracked = git(repo, "ls-files", "-z", "--others", "--exclude-standard").decode().split("\0")
    for relative in filter(None, untracked):
        source = repo / relative
        if source.is_file():
            target = out / "untracked" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    binary = out / ("kics.exe" if __import__("os").name == "nt" else "kics")
    command = ["go", "build", "-mod=readonly", "-tags", "dev", "-buildvcs=true",
               "-o", str(binary), "./cmd/console"]
    subprocess.run(command, cwd=repo, check=True)
    if before != source_files(repo) or head != git(repo, "rev-parse", "HEAD").decode().strip():
        raise ValueError("KICS source changed during build")
    evidence = {
        "head": head, "source_files": before, "binary_sha256": sha(binary),
        "command": command, "binary": str(binary),
        "build_info": subprocess.check_output(["go", "version", "-m", str(binary)], text=True),
    }
    (out / "build.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(out / "build.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    build(args.repo, args.out)
