"""Verify every restored release file against its recorded SHA-256 digest."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-release", action="store_true", help="Also verify the untouched release snapshot")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "restoration-manifest.json").read_text(encoding="utf-8"))
    roots = [ROOT]
    if args.check_release:
        roots.append(ROOT / manifest["source"])
    failures = []
    for root in roots:
        for item in manifest["files"]:
            path = root / item["path"]
            if not path.is_file():
                failures.append(f"MISSING: {path}")
            elif path.stat().st_size != item["size"] or sha256(path) != item["sha256"]:
                failures.append(f"CHANGED: {path}")
    for failure in failures:
        print(failure)
    if failures:
        print(f"{len(failures)} file(s) differ; intentional source edits also appear here.")
        return 1
    print(f"PASS: {len(manifest['files'])} files match in each of {len(roots)} location(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
