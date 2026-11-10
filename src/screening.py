"""
Content screening for finley-local: Tesseract + pdfplumber in place of Azure Document Intelligence,
pixel sampling in place of styleFont colours. No prompt-injection service -- Claude's own guardrails
plus the hidden-text check are the only defenses.

A file passes screening iff:
  - no word is in its background's colour (uniform patch where text should be)
  - no word is too small to read, or positioned off the page
  - no text layer words are missing from what we extracted (likely drawn outside the page)

Blocked raises with the reasons so main.py can hold the message for a person.
"""

import email
import email.policy
import hashlib
import html
import io
import json
import os
import re
import statistics

import pypdf
from PIL import Image

import extract

TINY_TEXT = 0.004       # word height / page height; about 3 pt on a letter page
MIN_TINY_WORDS = 3
MIN_LAYER_WORDS = 8
LOW_CONTRAST_STDEV = 12  # grayscale stdev inside the word's bbox; a uniform patch means text in background colour
SCREENED = "<!-- finley-screened "

BODY_TYPES = {"text/plain": ".txt", "text/html": ".html"}


class Blocked(Exception):
    def __init__(self, message: str, entries: list[dict]):
        super().__init__(message)
        self.entries = entries


def _excerpt(text: str, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return f'"{text[:n]}{"..." if len(text) > n else ""}"'


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]{3,}", text.lower())


def _plain(html_text: str) -> str:
    stripped = re.sub(r"(?is)<(style|script|head)\b.*?</\1>", " ", html_text)
    return html.unescape(re.sub(r"<[^>]+>", " ", stripped))


def _invisible(image: Image.Image, bbox_px: tuple) -> bool:
    """A word whose bbox is a uniform patch on the rendered page: drawn in the background's colour."""
    x0, y0, x1, y1 = (int(round(v)) for v in bbox_px)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return False
    patch = image.crop((max(0, x0), max(0, y0), min(image.width, x1), min(image.height, y1))).convert("L")
    pixels = list(patch.getdata())
    if len(pixels) < 10:
        return False
    return statistics.stdev(pixels) < LOW_CONTRAST_STDEV


def hidden_in_pages(pages: list[dict]) -> list[str]:
    """Words a reader would not see: in their background's colour, too small, or off the page."""
    found = []
    for i, page in enumerate(pages, 1):
        width, height, image = page["width_pt"], page["height_pt"], page["image"]
        tiny, off, invisible = [], [], []
        for w in page["words"]:
            x0, y0, x1, y1 = w["bbox_pt"]
            if height and (y1 - y0) / height < TINY_TEXT:
                tiny.append(w["text"])
            if width and height and (x1 < 0 or x0 > width or y1 < 0 or y0 > height):
                off.append(w["text"])
            elif _invisible(image, w["bbox_px"]):
                invisible.append(w["text"])
        if invisible:
            found.append(f"{len(invisible)} words in their background's colour on page {i}: {_excerpt(' '.join(invisible))}")
        if len(tiny) >= MIN_TINY_WORDS:
            found.append(f"{len(tiny)} words too small to read on page {i}: {_excerpt(' '.join(tiny))}")
        if off:
            found.append(f"{len(off)} words off page {i}: {_excerpt(' '.join(off))}")
    return found


def hidden_in_text_layer(pdf_bytes: bytes, visible: str) -> list[str]:
    """Words a PDF carries in its text layer that did not make it into what we extracted."""
    layer = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages)
    seen = set(_words(visible))
    missing = list(dict.fromkeys(w for w in _words(layer) if w not in seen))
    if len(missing) >= MIN_LAYER_WORDS:
        return [f"{len(missing)} words in the PDF's text layer that are not on the page: {_excerpt(' '.join(missing))}"]
    return []


def _read_file(name: str, data: bytes) -> tuple[dict, bytes]:
    """Extract one file. Returns (result, pdf_bytes_or_empty) -- pdf_bytes feeds the text-layer check."""
    ext = os.path.splitext(name)[1].lower()
    if ext == ".pdf":
        return extract.read_pdf(data), data
    if ext in extract.IMAGE_TYPES:
        return extract.read_image(data), b""
    if ext in {".xlsx", ".xlsm"}:
        # Excel: cell grid straight from openpyxl for markdown (no PDF-reparse round trip);
        # LibreOffice PDF is still needed so hidden-text checks can sample the rendered pages.
        ws_result = extract.read_workbook(data)
        pdf = extract.libreoffice_pdf(name, data)
        pdf_result = extract.read_pdf(pdf)
        return {"markdown": ws_result["markdown"], "pages": pdf_result["pages"]}, pdf
    if ext in extract.OFFICE_TYPES:
        pdf = extract.libreoffice_pdf(name, data)
        return extract.read_pdf(pdf), pdf
    raise RuntimeError(f"cannot read {name!r} (type {ext or 'unknown'})")


def pieces_of(name: str, data: bytes, seen: set[str], charset: str = "utf-8") -> list[dict]:
    """
    What one file holds, as pieces: {source, text, markdown (None when not for Claude), pages, hidden}.
    An .eml is opened part by part; each attachment skipped if already filed on its own.
    """
    digest = hashlib.sha256(data).hexdigest()
    if digest in seen:
        return []
    seen.add(digest)
    ext = os.path.splitext(name)[1].lower()
    piece = {"source": name, "pages": None, "hidden": []}
    if ext == ".eml":
        msg = email.message_from_bytes(data, policy=email.policy.default)
        head = "\n".join(f"**{h}:** {msg.get(h, '')}" for h in ("From", "Date", "Subject"))
        bodies, rest = {}, []
        for i, part in enumerate(msg.walk()):
            if part.is_multipart():
                continue
            kind = part.get_content_type()
            payload = part.get_payload(decode=True) or b""
            if kind in BODY_TYPES and not part.get_filename() and kind not in bodies:
                bodies[kind] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                seen.add(hashlib.sha256(payload).hexdigest())
            elif f"file:{part.get_filename()}" not in seen and part.get_filename():
                rest += pieces_of(f"{name} > {part.get_filename()}", payload, seen,
                                  part.get_content_charset() or "utf-8")
        body = bodies.get("text/plain") or _plain(bodies.get("text/html", ""))
        return [{**piece, "text": "\n".join([head, *bodies.values()]), "markdown": f"{head}\n\n{body.strip()}"}] + rest
    if ext in extract.TEXT_TYPES:
        text = data.decode(charset, errors="replace")
        return [{**piece, "text": text, "markdown": _plain(text) if ext in (".html", ".htm") else text}]
    result, pdf_bytes = _read_file(name, data)
    content = result.get("markdown", "")
    piece["hidden"] = hidden_in_pages(result["pages"])
    text = content
    if pdf_bytes:
        piece["hidden"] += hidden_in_text_layer(pdf_bytes, content)
        text += "\n" + "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages)
    piece["pages"] = len(result["pages"])
    return [{**piece, "text": text, "markdown": content}]


def screen(files: list[str], log) -> list[dict]:
    """
    Screen the files one run gives Claude. Raises Blocked on hidden text; returns pieces otherwise.
    """
    seen = {f"file:{os.path.basename(p)}" for p in files if not p.lower().endswith(".eml")}
    pieces = []
    for path in sorted(files, key=lambda p: p.lower().endswith(".eml")):  # attachments first
        with open(path, "rb") as f:
            data = f.read()
        try:
            pieces += pieces_of(os.path.basename(path), data, seen)
        except Exception as e:
            raise RuntimeError(f"Content screening could not read {os.path.basename(path)}: {e}") from e
    reasons = [f"{p['source']}: hidden {h}" for p in pieces for h in p["hidden"]]
    if reasons:
        log(f"CONTENT SCREENING BLOCKED: {reasons}")
        raise Blocked(
            "HELD by content screening -- Claude did not run. Check these before processing by hand: "
            + "; ".join(reasons[:10]) + (f" (and {len(reasons) - 10} more)" if len(reasons) > 10 else ""),
            report(pieces),
        )
    log(f"Content screening: {len(pieces)} pieces from {len(files)} files, nothing hidden")
    return pieces


def report(pieces: list[dict]) -> list[dict]:
    """What was checked, without the text itself."""
    return [{"source": p["source"], "characters": len(p["text"]), "pages": p["pages"],
             "hidden": p["hidden"]} for p in pieces]


def markdown(pieces: list[dict], sources: list[str], title: str) -> str:
    """The screened content Claude reads; the first line lists the originals it stands for."""
    parts = [
        f"{SCREENED}{json.dumps({'sources': sources})} -->",
        f"# {title}",
        "Read by Finley's content screening: each file below was extracted and checked for hidden "
        "text; nothing hidden was found. This is the builder's content; the original files stay in "
        "the library for staff. Signatures, stamps, initials, circled marks and pictures are not "
        "shown here.",
    ]
    for p in sorted((p for p in pieces if p["markdown"] is not None),
                    key=lambda p: not p["source"].lower().endswith(".eml")):
        pages = f" ({p['pages']} page{'s' if p['pages'] != 1 else ''})" if p["pages"] else ""
        parts.append(f"## {p['source']}{pages}\n\n{p['markdown'].strip() or '(no text)'}")
    return "\n\n".join(parts) + "\n"
