"""
Run intake + screening against a folder of files on disk, with no Gmail involved. For iterating on
the extraction and hidden-text checks before the mailbox is wired in.

    uv run python src/local.py path/to/draw-package/
    uv run python src/local.py path/to/draw-package/ --out data/library/my-run

Outputs screened.md + report.json (or held.json) into a run folder under FINLEY_LIBRARY. Hidden
files, directories, and anything screening cannot read are skipped with a logged note.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import extract
import screening

log = logging.getLogger("finley-local")

SUPPORTED = {".pdf", ".eml"} | extract.IMAGE_TYPES | extract.OFFICE_TYPES | extract.TEXT_TYPES


def _files(root: Path) -> list[str]:
    """Every supported file under root, sorted; hidden files and dotfolders skipped."""
    found = []
    for path in sorted(root.rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.suffix.lower() in SUPPORTED:
            found.append(str(path))
        elif path.is_file():
            log.info("skipping unsupported: %s", path.name)
    return found


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Run finley-local screening against local files.")
    parser.add_argument("input", type=Path, help="Folder containing the draw-package files.")
    parser.add_argument("--out", type=Path, default=None, help="Where to write screened.md and the report.")
    args = parser.parse_args()

    if not args.input.is_dir():
        log.error("not a directory: %s", args.input)
        return 2

    files = _files(args.input)
    if not files:
        log.error("no supported files under %s", args.input)
        return 2
    log.info("screening %d files from %s", len(files), args.input)

    library = Path(os.environ.get("FINLEY_LIBRARY", "data/library"))
    out = args.out or library / f"local-{args.input.name}-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)

    try:
        pieces = screening.screen(files, log.info)
        content = screening.markdown(pieces, [os.path.basename(f) for f in files],
                                     f"Screened content: {args.input.name}")
        (out / "screened.md").write_text(content, encoding="utf-8")
        (out / "report.json").write_text(json.dumps(screening.report(pieces), indent=2))
        log.info("screened -> %s", out / "screened.md")
        return 0
    except screening.Blocked as e:
        (out / "held.json").write_text(json.dumps({"reason": str(e), "report": e.entries}, indent=2))
        log.warning("HELD -> %s", out / "held.json")
        return 1


if __name__ == "__main__":
    sys.exit(main())
