"""Preprocessing: break every unique source file into content units.

  PDF   -> one unit per page (text layer if present, otherwise a rendered image for OCR),
           plus one unit per large embedded image (a photo pasted into a page).
  XLSX  -> one unit per worksheet, rendered as tab-separated text (hidden sheets flagged).
  Image -> one unit.
  Text  -> one unit.

Classification happens per unit, so a single file that mixes document types (e.g. an
"MVR REPORT" PDF that also contains photos of license cards) is split correctly.
"""
from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..schemas import ContentUnit, SourceFile, UnitKind
from .intake import stored_path

LETTER_RATIOS = (11 / 8.5, 8.5 / 11, 14 / 8.5, 8.5 / 14)   # letter + legal, both orientations
MIN_IMAGE_SHARE = 0.15     # embedded images smaller than this share of the page are ignored


def _is_document_shaped(w: float, h: float) -> bool:
    ratio = w / h if h else 0
    return any(abs(ratio - r) < 0.06 for r in LETTER_RATIOS)


def _pdf_units(f: SourceFile, path: Path, out_dir: Path, s: Settings) -> list[ContentUnit]:
    import pdfplumber
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    units: list[ContentUnit] = []
    pdf = pdfium.PdfDocument(str(path))
    with pdfplumber.open(str(path)) as plumber:
        for i, page in enumerate(pdf, start=1):
            text = (plumber.pages[i - 1].extract_text(layout=True) or "").strip()
            pw, ph = page.get_size()
            images = list(page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE]))
            has_text = len(text.replace(" ", "")) >= s.min_text_chars
            unit = ContentUnit(
                unit_id=f"{f.file_id}:p{i}", file_id=f.file_id, kind=UnitKind.PDF_PAGE,
                index=i, label=f"{f.name} p.{i}",
                meta={"page_size_pt": [round(pw), round(ph)], "image_count": len(images)})
            if has_text:
                unit.text, unit.text_source = text, "text_layer"
            else:
                # Image-only page: render it for OCR. Card-shaped pages are treated as photos
                # (vision model); letter-shaped pages as scanned documents (document OCR).
                img_path = out_dir / f"{f.file_id}_p{i}.png"
                page.render(scale=s.render_dpi / 72).to_pil().save(img_path)
                unit.image_path = str(img_path)
                unit.meta["ocr_hint"] = "scan" if _is_document_shaped(pw, ph) else "photo"
            units.append(unit)

            if has_text:
                for k, obj in enumerate(images, start=1):
                    left, bottom, right, top = obj.get_bounds()
                    share = ((right - left) * (top - bottom)) / (pw * ph)
                    if share < MIN_IMAGE_SHARE:
                        continue
                    img_path = out_dir / f"{f.file_id}_p{i}_img{k}.png"
                    obj.get_bitmap(render=False).to_pil().convert("RGB").save(img_path)
                    units.append(ContentUnit(
                        unit_id=f"{f.file_id}:p{i}:img{k}", file_id=f.file_id,
                        kind=UnitKind.EMBEDDED_IMAGE, index=i,
                        label=f"{f.name} p.{i} image {k}", image_path=str(img_path),
                        meta={"ocr_hint": "photo", "page_share": round(share, 2)}))
    pdf.close()
    return units


def _sheet_units(f: SourceFile, path: Path) -> list[ContentUnit]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    units = []
    for n, ws in enumerate(wb.worksheets, start=1):
        rows = []
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v).strip() for v in row]
            if any(cells):
                # Collapse runs of empty cells from merged-cell form layouts.
                compact = [c for c in cells if c]
                rows.append("\t".join(compact))
        units.append(ContentUnit(
            unit_id=f"{f.file_id}:s{n}", file_id=f.file_id, kind=UnitKind.SHEET, index=n,
            label=f"{f.name} [{ws.title}]", text="\n".join(rows), text_source="sheet",
            hidden=ws.sheet_state != "visible",
            meta={"sheet": ws.title, "dimensions": ws.dimensions, "row_count": len(rows)}))
    return units


def build_units(files: list[SourceFile], work_dir: Path, s: Settings) -> list[ContentUnit]:
    out_dir = work_dir / "units"
    out_dir.mkdir(parents=True, exist_ok=True)
    units: list[ContentUnit] = []
    for f in files:
        if f.duplicate_of:
            continue                                    # identical bytes already processed
        path = stored_path(work_dir, f)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            units += _pdf_units(f, path, out_dir, s)
        elif suffix in (".xlsx", ".xlsm"):
            units += _sheet_units(f, path)
        elif suffix in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".heic"):
            units.append(ContentUnit(
                unit_id=f"{f.file_id}:i1", file_id=f.file_id, kind=UnitKind.IMAGE_FILE,
                index=1, label=f.name, image_path=str(path), meta={"ocr_hint": "photo"}))
        elif suffix in (".txt", ".csv", ".eml", ".md"):
            units.append(ContentUnit(
                unit_id=f"{f.file_id}:t1", file_id=f.file_id, kind=UnitKind.TEXT_FILE,
                index=1, label=f.name, text=path.read_text(errors="replace"),
                text_source="text_layer"))
        elif suffix == ".docx":
            from docx import Document
            text = "\n".join(p.text for p in Document(str(path)).paragraphs if p.text.strip())
            for t in Document(str(path)).tables:
                for r in t.rows:
                    text += "\n" + " | ".join(c.text.strip() for c in r.cells)
            units.append(ContentUnit(
                unit_id=f"{f.file_id}:t1", file_id=f.file_id, kind=UnitKind.TEXT_FILE,
                index=1, label=f.name, text=text, text_source="text_layer"))
        else:
            units.append(ContentUnit(
                unit_id=f"{f.file_id}:x1", file_id=f.file_id, kind=UnitKind.TEXT_FILE,
                index=1, label=f.name, meta={"unsupported": suffix}))
    return units


def run_ocr(units: list[ContentUnit], ocr) -> None:
    """Fill text for every unit that has an image but no text layer."""
    for u in units:
        if u.image_path and not u.text:
            res = ocr.read(u.image_path, hint=u.meta.get("ocr_hint", ""))
            u.text, u.text_source, u.ocr_confidence = res.text.strip(), res.engine, res.confidence
