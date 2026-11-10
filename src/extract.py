"""
Extraction without Azure Document Intelligence. For each file we hand screening:
  - markdown Claude reads
  - per-page {width_pt, height_pt, rendered image, word bboxes} so screening can check for hidden text

PDF: pdfplumber reads the text layer and tables when there is one; pages with no text layer fall
back to Tesseract OCR on the rendered page. Office files are printed to PDF with LibreOffice first
(same approach as finley-poc/excel_print.py); images go straight to Tesseract.

The rendered page image is kept because that is what the low-contrast hidden-text check samples --
Document Intelligence's styleFont colours are not available here.
"""

import datetime
import html as html_mod
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import openpyxl
import pdfplumber
import pymupdf
import pytesseract
from openpyxl.worksheet.properties import PageSetupProperties
from PIL import Image

RENDER_DPI = 150
PT_PER_INCH = 72

IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
OFFICE_TYPES = {".xlsx", ".xlsm", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".odt", ".ods"}
TEXT_TYPES = {".txt", ".csv", ".json", ".md", ".html", ".htm"}

# Workbook print prep: ported from finley-poc/src/excel_print.py so numbers don't print as '###'
# when the source column is narrower than the value.
PORTRAIT_MAX_WIDTH = 110
PAGE_HEIGHT_PT = {"portrait": 684, "landscape": 504}
MIN_SCALE = 0.7


def _scale() -> float:
    return RENDER_DPI / PT_PER_INCH


def _render(page: pymupdf.Page) -> Image.Image:
    pix = page.get_pixmap(dpi=RENDER_DPI, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _tesseract_words(image: Image.Image) -> list[dict]:
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    scale, words = _scale(), []
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        if not text or int(data["conf"][i]) < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append({
            "text": text,
            "bbox_px": (x, y, x + w, y + h),
            "bbox_pt": (x / scale, y / scale, (x + w) / scale, (y + h) / scale),
        })
    return words


def _plumber_words(page) -> list[dict]:
    scale, words = _scale(), []
    for w in page.extract_words() or []:
        x0, y0, x1, y1 = float(w["x0"]), float(w["top"]), float(w["x1"]), float(w["bottom"])
        words.append({
            "text": w["text"],
            "bbox_px": (x0 * scale, y0 * scale, x1 * scale, y1 * scale),
            "bbox_pt": (x0, y0, x1, y1),
        })
    return words


def _table_markdown(rows: list[list]) -> str:
    """Rows -> HTML <table> block. Matches Azure Document Intelligence's markdown output."""
    rows = [[(c or "").replace("\n", " ").strip() for c in r] for r in rows if r]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header, *body = rows
    lines = ["<table>", "<tr>"]
    lines += [f"<th>{html_mod.escape(c)}</th>" for c in header]
    lines.append("</tr>")
    for row in body:
        lines.append("<tr>")
        lines += [f"<td>{html_mod.escape(c)}</td>" for c in row]
        lines.append("</tr>")
    lines.append("</table>")
    return "\n".join(lines)


def read_pdf(data: bytes) -> dict:
    """PDF -> {markdown, pages:[{width_pt, height_pt, image, words}]}."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        with pdfplumber.open(io.BytesIO(data)) as plumber:
            md_parts, pages = [], []
            for i, (p_fitz, p_plumb) in enumerate(zip(doc, plumber.pages)):
                image = _render(p_fitz)
                text = (p_plumb.extract_text() or "").strip()
                if text:
                    words = _plumber_words(p_plumb)
                    found_tables = p_plumb.find_tables() or []
                    tables_md = [t for t in (_table_markdown(ft.extract()) for ft in found_tables) if t]
                    if tables_md:
                        # On a table page, extract_text() mashes cells together with spaces --
                        # use the structured table output, plus any text outside the table regions.
                        page_ref = p_plumb
                        for ft in found_tables:
                            page_ref = page_ref.outside_bbox(ft.bbox, strict=False)
                        outside = (page_ref.extract_text() or "").strip()
                        md = (outside + "\n\n" if outside else "") + "\n\n".join(tables_md)
                    else:
                        md = text
                else:
                    words = _tesseract_words(image)
                    md = " ".join(w["text"] for w in words)
                md_parts.append(f"<!-- page {i + 1} -->\n{md}")
                pages.append({
                    "width_pt": float(p_plumb.width), "height_pt": float(p_plumb.height),
                    "image": image, "words": words,
                })
    finally:
        doc.close()
    return {"markdown": "\n\n".join(md_parts), "pages": pages}


def _cell_str(cell) -> str:
    """Cell value as a display string. Respects number_format for currency / percent / dates."""
    v = cell.value
    if v is None:
        return ""
    if isinstance(v, datetime.datetime):
        return v.strftime("%m/%d/%Y %H:%M") if v.hour or v.minute else v.strftime("%m/%d/%Y")
    if isinstance(v, datetime.date):
        return v.strftime("%m/%d/%Y")
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        fmt = (cell.number_format or "").lower()
        if "$" in fmt or "accounting" in fmt:
            return f"${v:,.2f}"
        if "%" in fmt:
            return f"{v * 100:,.1f}%"
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return f"{v:,.2f}" if isinstance(v, float) else f"{v:,}"
    return str(v)


def _sheet_rows(ws) -> list[list[str]]:
    """Every row as a list of cell strings; trailing empty rows and columns trimmed."""
    rows = [[_cell_str(c) for c in row] for row in ws.iter_rows()]
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    if not rows:
        return []
    width = max(len(r) for r in rows)
    while width > 0 and all(width > len(r) or not r[width - 1].strip() for r in rows):
        width -= 1
    return [r[:width] for r in rows]


def _split_sections(rows: list[list[str]]) -> list[list[list[str]]]:
    """Split on fully-empty rows so each self-contained table in a sheet is its own <table>."""
    sections, current = [], []
    for row in rows:
        if any(c.strip() for c in row):
            current.append(row)
        elif current:
            sections.append(current)
            current = []
    if current:
        sections.append(current)
    return sections


def _trim_section(section: list[list[str]]) -> list[list[str]]:
    """Drop trailing empty columns from one section so a 1-col title doesn't inherit sheet width."""
    width = max(len(r) for r in section) if section else 0
    while width > 0 and all(width > len(r) or not r[width - 1].strip() for r in section):
        width -= 1
    return [r[:width] for r in section]


def _render_section(section: list[list[str]]) -> str:
    """One section: plain text if it's a single column (title, narrative), <table> otherwise."""
    trimmed = _trim_section(section)
    if not trimmed:
        return ""
    if len(trimmed[0]) == 1:
        return "\n".join(html_mod.escape(row[0]) for row in trimmed if row and row[0].strip())
    return _table_markdown(trimmed)


def read_workbook(data: bytes) -> dict:
    """
    Read an .xlsx / .xlsm with openpyxl -- one HTML <table> per section per sheet. data_only=True
    gets the formula's cached result; number_format gives '$X,XXX.XX' back to currency cells.
    Pages are returned empty: the hidden-text check still runs on the LibreOffice-rendered PDF.
    """
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    parts = []
    for ws in wb.worksheets:
        rendered = [r for r in (_render_section(s) for s in _split_sections(_sheet_rows(ws))) if r]
        if not rendered:
            continue
        parts.append(f"<!-- sheet: {html_mod.escape(ws.title)} -->\n" + "\n\n".join(rendered))
    return {"markdown": "\n\n".join(parts), "pages": []}


def read_image(data: bytes) -> dict:
    """Image file -> OCR'd text + one page with word bboxes."""
    image = Image.open(io.BytesIO(data)).convert("RGB")
    words = _tesseract_words(image)
    scale = _scale()
    return {
        "markdown": " ".join(w["text"] for w in words),
        "pages": [{
            "width_pt": image.width / scale, "height_pt": image.height / scale,
            "image": image, "words": words,
        }],
    }


def fit_for_print(ws, one_page: bool = False):
    """
    Widen every column so amounts don't print as '###', and set orientation so the sheet fits one
    page wide. Ported from finley-poc/src/excel_print.py. Widths only grow; nothing is hidden or
    shown, and fonts and colours are untouched.
    """
    ws.print_area = None
    cells = [c for row in ws.iter_rows() for c in row if c.value not in (None, "")]
    filled = {(c.row, c.column) for c in cells}
    widest = {}
    for cell in cells:
        value = cell.value
        if not isinstance(value, str):
            need = len(f"{value:,.2f}" if isinstance(value, (int, float)) else str(value)) + 3
        elif value.startswith("="):
            need = 14
        elif (cell.row, cell.column + 1) in filled:
            need = min(len(value), 60) + 1
        else:
            continue
        widest[cell.column_letter] = max(widest.get(cell.column_letter, 0), need)
    for letter, width in widest.items():
        dim = ws.column_dimensions[letter]
        dim.width = max(dim.width or 8.43, width)
    used = {c.column_letter for c in cells}
    total = sum(ws.column_dimensions[letter].width or 8.43 for letter in used)
    orientation = "portrait" if total <= PORTRAIT_MAX_WIDTH else "landscape"
    rows = [ws.row_dimensions.get(r) for r in range(1, ws.max_row + 1)]
    height = sum((d.height if d and d.height else 15) for d in rows if not (d and d.hidden))
    readable = height * MIN_SCALE <= PAGE_HEIGHT_PT[orientation]
    ws.page_setup.orientation = orientation
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, (1 if one_page and readable else 0)
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)


def printable_workbook(data: bytes, keep_vba: bool = False) -> bytes:
    """A copy of the workbook with every sheet set up to print whole."""
    wb = openpyxl.load_workbook(io.BytesIO(data), keep_vba=keep_vba)
    for ws in wb.worksheets:
        fit_for_print(ws)
    wb.calculation.fullCalcOnLoad = True  # formulas, not Excel's cached results
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def libreoffice_pdf(name: str, data: bytes) -> bytes:
    """Convert a Word / Excel / PowerPoint file to PDF via headless LibreOffice. Returns PDF bytes."""
    binary = shutil.which("libreoffice") or shutil.which("soffice")
    if not binary:
        raise RuntimeError("LibreOffice is required for Office files (brew install --cask libreoffice).")
    if name.lower().endswith((".xlsx", ".xlsm")):
        data = printable_workbook(data, keep_vba=name.lower().endswith(".xlsm"))
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, os.path.basename(name))
        with open(src, "wb") as f:
            f.write(data)
        result = subprocess.run(
            [binary, f"-env:UserInstallation=file://{tmp}",
             "--headless", "--convert-to", "pdf", "--outdir", tmp, src],
            capture_output=True, timeout=180,
            env={**os.environ, "HOME": tmp},
        )
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice conversion of {name!r} failed: {result.stderr.decode()[:400]}")
        out = Path(tmp) / (Path(name).stem + ".pdf")
        if not out.exists():
            raise RuntimeError(f"LibreOffice produced no PDF for {name!r}")
        return out.read_bytes()
