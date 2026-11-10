"""
finley-local entry point: poll Gmail for messages labelled finley-watch, run intake + screening,
write results to disk, move the message to finley-screened.

    uv run src/main.py           # or: python src/main.py
    FINLEY_POLL_ONCE=1 python src/main.py   # one pass and exit (useful from cron)
"""

import json
import logging
import os
import signal
import sys
import time
import traceback
from pathlib import Path

from dotenv import load_dotenv

import gmail
import intake
import screening

log = logging.getLogger("finley-local")


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Set {name} in .env")
    return value


def process(svc, message_id: str, library: Path):
    """One message end-to-end: intake, screen, write screened.md or held.json. Always labels the message."""
    log.info("processing %s", message_id)
    raw = gmail.raw_eml(svc, message_id)
    saved = intake.save_message(library, message_id, raw)
    folder = Path(saved["folder"])
    try:
        pieces = screening.screen(saved["files"], log.info)
        content = screening.markdown(pieces, [os.path.basename(f) for f in saved["files"]],
                                     f"Screened content: {saved['subject'] or message_id}")
        (folder / "screened.md").write_text(content, encoding="utf-8")
        (folder / "report.json").write_text(json.dumps(screening.report(pieces), indent=2))
        log.info("screened %s -> %s", message_id, folder / "screened.md")
    except screening.Blocked as e:
        (folder / "held.json").write_text(json.dumps({"reason": str(e), "report": e.entries}, indent=2))
        log.warning("held %s: %s", message_id, e)


def one_pass(svc, screened_name: str, screened_id: str, library: Path):
    extra = os.environ.get("FINLEY_GMAIL_QUERY", "")
    for message_id in gmail.pending(svc, screened_name, extra):
        try:
            process(svc, message_id, library)
        except Exception:
            log.error("failed on %s:\n%s", message_id, traceback.format_exc())
            continue
        gmail.mark_screened(svc, message_id, screened_id)


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    library = Path(_env("FINLEY_LIBRARY", "data/library"))
    library.mkdir(parents=True, exist_ok=True)

    svc = gmail.service()
    screened_name = _env("FINLEY_GMAIL_SCREENED_LABEL")
    screened_id = gmail.label_id(svc, screened_name)

    if os.environ.get("FINLEY_POLL_ONCE"):
        one_pass(svc, screened_name, screened_id, library)
        return 0

    stop = False

    def shutdown(*_):
        nonlocal stop
        stop = True
        log.info("stopping after this pass")

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    interval = int(_env("FINLEY_POLL_SECONDS", "30"))
    log.info("polling Gmail every %ds (every inbox message, screened=%s)", interval, screened_name)
    while not stop:
        try:
            one_pass(svc, screened_name, screened_id, library)
        except Exception:
            log.error("pass failed:\n%s", traceback.format_exc())
        for _ in range(interval):
            if stop:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
