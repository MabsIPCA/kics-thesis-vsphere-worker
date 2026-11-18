#!/usr/bin/env python3
"""Download pinned public release assets and verify all input on the VM OS disk."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
WORK = Path(os.environ.get("KICS_VM_WORK_ROOT", Path.home() / "kics-vsphere-work-batch2")).resolve()
MIN_FREE = 4 * 1024**3


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_space():
    free = shutil.disk_usage(WORK).free
    if free < MIN_FREE:
        raise RuntimeError(f"VM OS disk below 4 GiB free: {free} bytes")
    print(f"VM OS disk free: {free / 1024**3:.2f} GiB", flush=True)


def download(asset, manifest):
    target = WORK / "downloads" / asset["name"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and sha(target) == asset["sha256"]:
        print(f"Reusing verified {asset['name']}", flush=True)
        return target
    partial = target.with_name(target.name + ".partial")
    if partial.exists():
        partial.unlink()
    base = os.environ.get(
        "KICS_ASSET_BASE_URL",
        f"https://github.com/{manifest['github_repo']}/releases/download/"
        f"{manifest['release_tag']}")
    url = f"{base.rstrip('/')}/{asset['name']}"
    request = Request(url, headers={"User-Agent": "kics-thesis-vsphere-worker"})
    print(f"Downloading {asset['name']} ({asset['bytes']} bytes)", flush=True)
    with urlopen(request, timeout=60) as response, partial.open("wb") as stream:
        shutil.copyfileobj(response, stream, length=1024 * 1024)
    if partial.stat().st_size != asset["bytes"] or sha(partial) != asset["sha256"]:
        raise RuntimeError(f"Release asset checksum differs: {asset['name']}")
    os.replace(partial, target)
    return target


def extract(archive_path):
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if (not parts or member.name.startswith("/") or ".." in parts
                    or member.isdev()):
                raise RuntimeError(f"Unsafe asset member: {member.name}")
            if member.issym() and (member.linkname.startswith("/")
                                  or ".." in Path(member.linkname).parts):
                raise RuntimeError(f"Unsafe asset link: {member.name}")
        archive.extractall(WORK)


def verify(manifest, delegation):
    evidence = json.loads((ROOT / "worker/build.json").read_text(encoding="utf-8"))
    for relative, expected in evidence["source_files"].items():
        path = WORK / "kics" / relative
        actual = sha(path) if path.is_file() else None
        if actual != expected:
            raise RuntimeError(f"Pinned KICS source differs: {relative}")
    binary = WORK / "kics-linux/kics"
    go = WORK / "toolchain/go/bin/go"
    for path in [binary, *go.parent.glob("*"), *(WORK / "toolchain/go/pkg/tool/linux_amd64").glob("*")]:
        if path.is_file():
            path.chmod(path.stat().st_mode | 0o111)
    if sha(binary) != manifest["linux_binary_sha256"]:
        raise RuntimeError("Pinned Linux KICS binary differs")
    info = subprocess.check_output([str(go), "version", "-m", str(binary)], text=True)
    if f"vcs.revision={delegation['kics_revision']}" not in info:
        raise RuntimeError("KICS binary revision differs")
    sys.path.insert(0, str(ROOT / "worker"))
    import kics_corpus_replay as replay
    for repo in delegation["repos"]:
        source = WORK / "corpus" / repo["key"]
        files = replay.tree_manifest(source)
        if (replay.manifest_hash(files) != repo["input_sha256"]
                or len(files) != repo["input_files"]):
            raise RuntimeError(f"Pinned corpus differs: {repo['key']}")
        print(f"Verified {repo['key']}: {len(files)} files", flush=True)


def main():
    subprocess.run([sys.executable, str(ROOT / "scripts/verify_handoff.py")], check=True)
    manifest = json.loads((ROOT / "bundle-manifest.json").read_text(encoding="utf-8"))
    delegation = json.loads((ROOT / "plan/vsphere-delegation.json").read_text(encoding="utf-8"))
    if sha(ROOT / "plan/vsphere-delegation.json") != manifest["delegation_sha256"]:
        raise RuntimeError("release manifest differs from delegated assignment")
    WORK.mkdir(parents=True, exist_ok=True)
    marker = WORK / "stage-complete"
    if marker.is_file():
        old = json.loads(marker.read_text(encoding="utf-8"))
        if old.get("delegation_sha256") == manifest["delegation_sha256"]:
            verify(manifest, delegation)
            check_space()
            print("Existing VM staging verified", flush=True)
            return
    check_space()
    for asset in manifest["assets"]:
        target = download(asset, manifest)
        extract(target)
        target.unlink()
        check_space()
    verify(manifest, delegation)
    (WORK / "stage-complete").write_text(
        json.dumps({"verified_at": datetime.now(timezone.utc).isoformat(),
                    "delegation_sha256": manifest["delegation_sha256"],
                    "linux_binary_sha256": manifest["linux_binary_sha256"]}, indent=2) + "\n",
        encoding="utf-8")
    print("VM OS-disk staging verified", flush=True)


if __name__ == "__main__":
    main()
