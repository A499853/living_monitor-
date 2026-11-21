"""Development entry point for the restored LiveWatch project."""
from __future__ import annotations

from pathlib import Path
import runpy
import sys


ROOT = Path(__file__).resolve().parent


def main(argv=None):
    # Keep the release sources unchanged and make both embedded and normal
    # CPython resolve the application package from this project.
    app = ROOT / "app"
    sys.path.insert(0, str(app))
    entry = runpy.run_path(str(app / "run.py"))
    # argparse accepts the last --data-dir, so callers can still isolate data.
    args = ["--data-dir", str(ROOT), *(sys.argv[1:] if argv is None else argv)]
    return entry["main"](args)


if __name__ == "__main__":
    raise SystemExit(main())
