"""The only things the recheck agent can do. Plain code; each returns content for the model
and a one-line summary for the audit trail.

  read_source_text(unit_id)   extracted text of a page / sheet / photo
  view_source_image(unit_id)  the page image or photo itself (rendered from the PDF if needed)
  decode_vin(vin)             manufacturer, model year and check-digit result
  record_verdict(...)         ends the review (handled by the loop, not here)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..pipeline.intake import stored_path
from ..pipeline.verify import normalize_vin, vin_checks, vin_decode
from ..schemas import Submission, UnitKind

VERDICTS = ["confirmed", "resolved", "corrected", "needs_human"]

TOOLS = [
    {"name": "read_source_text",
     "description": "Return the extracted text of one source unit (a PDF page, worksheet or photo). "
                    "Text from scans and photos came from OCR and may contain reading errors.",
     "input_schema": {"type": "object", "properties": {"unit_id": {"type": "string"}},
                      "required": ["unit_id"]}},
    {"name": "view_source_image",
     "description": "Show the original image of one source unit: the scanned or rendered page, or "
                    "the photo. Use it to check characters OCR may have misread. Not available "
                    "for spreadsheets.",
     "input_schema": {"type": "object", "properties": {"unit_id": {"type": "string"}},
                      "required": ["unit_id"]}},
    {"name": "decode_vin",
     "description": "Decode a VIN: manufacturer (from the first three characters), model year "
                    "(10th character) and whether the check digit (9th character) is valid.",
     "input_schema": {"type": "object", "properties": {"vin": {"type": "string"}},
                      "required": ["vin"]}},
    {"name": "record_verdict",
     "description": "Finish the review. confirmed = the difference is really printed in the "
                    "documents; resolved = the values actually agree once read correctly (a reading "
                    "or formatting difference only); corrected = one source was misread, give the "
                    "correct value; needs_human = the evidence is not enough to decide.",
     "input_schema": {"type": "object", "properties": {
         "verdict": {"type": "string", "enum": VERDICTS},
         "explanation": {"type": "string", "description": "Two or three sentences citing what you saw."},
         "corrected_value": {"type": "string", "description": "Only for 'corrected'."},
         "corrected_source": {"type": "string", "description": "Only for 'corrected': which source was misread."},
         "confidence": {"type": "number"}},
         "required": ["verdict", "explanation", "confidence"]}},
]


@dataclass
class ToolResult:
    content: list[dict]          # blocks for the model: text and/or image
    summary: str                 # one line for the audit trail
    is_error: bool = False


class AgentTools:
    def __init__(self, sub: Submission, work_dir: Path, render_dpi: int = 150):
        self.sub = sub
        self.work = work_dir
        self.dpi = render_dpi
        self.units = {u.unit_id: u for u in sub.units}
        self.files = {f.file_id: f for f in sub.files}

    def run(self, name: str, args: dict) -> ToolResult:
        fn = {"read_source_text": self.read_source_text, "view_source_image": self.view_source_image,
              "decode_vin": self.decode_vin}.get(name)
        if fn is None:
            return ToolResult([{"type": "text", "text": f"Unknown tool {name}."}], f"unknown tool {name}", True)
        try:
            return fn(**args)
        except Exception as e:                    # a tool failure is reported, never fatal
            return ToolResult([{"type": "text", "text": f"Tool error: {e}"}], f"error: {e}", True)

    def _unit(self, unit_id: str):
        u = self.units.get(unit_id)
        if u is None:
            raise ValueError(f"no source unit '{unit_id}'")
        return u

    def read_source_text(self, unit_id: str) -> ToolResult:
        u = self._unit(unit_id)
        text = re.sub(r" {3,}", "  ", u.text)[:8000]
        return ToolResult([{"type": "text", "text": f"[{u.label} | text from {u.text_source}]\n{text}"}],
                          f"read {len(text)} characters of {u.label} ({u.text_source})")

    def view_source_image(self, unit_id: str) -> ToolResult:
        u = self._unit(unit_id)
        if u.kind == UnitKind.SHEET:
            return ToolResult([{"type": "text", "text": "This source is a spreadsheet; it has no image. "
                                                         "Use read_source_text."}],
                              f"{u.label} is a spreadsheet (no image)", True)
        path = u.image_path
        if not path:                              # a text PDF page: render it on demand
            import pypdfium2 as pdfium
            f = self.files[u.file_id]
            out = self.work / "units" / f"{u.file_id}_p{u.index}_view.png"
            if not out.exists():
                out.parent.mkdir(parents=True, exist_ok=True)
                pdf = pdfium.PdfDocument(str(stored_path(self.work, f)))
                pdf[u.index - 1].render(scale=self.dpi / 72).to_pil().save(out)
                pdf.close()
            path = str(out)
        return ToolResult([{"type": "text", "text": f"[image of {u.label}]"},
                           {"type": "image", "path": path, "ref": unit_id}],
                          f"viewed image of {u.label}")

    def decode_vin(self, vin: str) -> ToolResult:
        v = normalize_vin(vin)
        d = vin_decode(v)
        problems = vin_checks(v)
        text = (f"VIN {v}: manufacturer {d['make'] or 'not in lookup table'}, "
                f"model year {d['model_year'] or 'unknown'}; "
                + ("structure and check digit valid." if not problems else "; ".join(problems)))
        return ToolResult([{"type": "text", "text": text}], text)
