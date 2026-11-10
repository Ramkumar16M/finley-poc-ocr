"""
Local intake: one Gmail message -> one folder under FINLEY_LIBRARY, with the raw .eml and each
attachment saved by its original filename. Returns the folder and the list of files the screening
pass should read.

No business logic (loan matching, draw numbering, etc.) -- this spike is only about getting the
builder's content to disk and into screening. finley-poc/src/intake.py is the full version.
"""

import email
import email.policy
import os
import re
from datetime import datetime
from pathlib import Path


def _safe(name: str) -> str:
    """Filename from untrusted input: no path separators, no leading dots, bounded length."""
    name = re.sub(r"[\\/\x00-\x1f]", "_", name).strip().lstrip(".")
    return (name or "unnamed")[:160]


def save_message(library: Path, message_id: str, raw: bytes) -> dict:
    """
    Write the .eml and its attachments into one folder. Returns {folder, files, subject, body, from}.
    folder = library/<yyyy-mm-dd>-<message-id>/; files = list of local paths intake dropped there.
    """
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    received = (msg["Date"] and email.utils.parsedate_to_datetime(msg["Date"])) or datetime.now()
    folder = library / f"{received:%Y-%m-%d}-{message_id}"
    folder.mkdir(parents=True, exist_ok=True)

    (folder / "message.eml").write_bytes(raw)
    files = [str(folder / "message.eml")]

    body_text, body_html = "", ""
    for part in msg.walk():
        if part.is_multipart():
            continue
        kind = part.get_content_type()
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            path = folder / _safe(filename)
            # Collision: two attachments with the same name within one message. Keep both.
            n = 1
            while path.exists():
                stem, dot, ext = path.name.partition(".")
                path = folder / f"{stem} ({n}){dot}{ext}"
                n += 1
            path.write_bytes(payload)
            files.append(str(path))
        elif kind == "text/plain" and not body_text:
            body_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        elif kind == "text/html" and not body_html:
            body_html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")

    return {
        "folder": str(folder),
        "files": files,
        "subject": msg["Subject"] or "",
        "from": msg["From"] or "",
        "body_text": body_text or body_html,
    }
