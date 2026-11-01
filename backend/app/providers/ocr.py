"""OCR providers for image-only pages and photos.

  TextractOCR  -- Amazon Textract; returns per-line confidence           [aws mode]
  VisionOCR    -- Claude vision transcription; best on phone photos       [local / fallback]
  TesseractOCR -- offline, free; used in demo mode and as a last resort
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import Settings
from .llm import Block, LLMProvider, LLMUnavailable


@dataclass
class OCRResult:
    text: str
    confidence: float | None    # 0..1 mean line/word confidence when the engine reports one
    engine: str


class OCRProvider(Protocol):
    name: str

    def read(self, image_path: str, hint: str = "") -> OCRResult: ...


def find_tesseract() -> str | None:
    """The tesseract binary: on PATH, or in the default Windows/macOS install folders."""
    import os
    import shutil

    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                      r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                      os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
                      "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
        if os.path.exists(candidate):
            return candidate
    return None


class TesseractOCR:
    name = "tesseract"

    def read(self, image_path, hint=""):
        import pytesseract
        from PIL import Image

        cmd = find_tesseract()
        if cmd is None:
            raise RuntimeError("Tesseract OCR is not installed (needed for demo-mode OCR of scans "
                               "without recordings). Install it or run in aws mode.")
        pytesseract.pytesseract.tesseract_cmd = cmd

        img = Image.open(image_path)
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        confs = [float(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and float(c) >= 0]
        text = pytesseract.image_to_string(img)
        conf = (sum(confs) / len(confs) / 100) if confs else None
        return OCRResult(text=text, confidence=conf, engine="ocr:tesseract")


class TextractOCR:
    name = "textract"

    def __init__(self, settings: Settings):
        import boto3
        session = boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
        self.client = session.client("textract")

    def read(self, image_path, hint=""):
        data = Path(image_path).read_bytes()
        # Tables matter for IFTA jurisdiction summaries; plain text is cheaper for everything else.
        if hint == "tables":
            resp = self.client.analyze_document(Document={"Bytes": data}, FeatureTypes=["TABLES"])
        else:
            resp = self.client.detect_document_text(Document={"Bytes": data})
        lines = [b for b in resp["Blocks"] if b["BlockType"] == "LINE"]
        text = "\n".join(b["Text"] for b in lines)
        conf = (sum(b["Confidence"] for b in lines) / len(lines) / 100) if lines else None
        return OCRResult(text=text, confidence=conf, engine="ocr:textract")


VISION_SYSTEM = (
    "You transcribe insurance documents and photos of ID cards exactly as printed. "
    "Preserve numbers, dates, names and table rows verbatim. Never guess a character you "
    "cannot read; write [?] instead. Rate how legible the image was.")

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Full transcription, one printed line per line."},
        "legibility": {"type": "number", "description": "0..1 how clearly the image could be read."},
    },
    "required": ["text", "legibility"],
}


class VisionOCR:
    name = "vision"

    def __init__(self, llm: LLMProvider):
        self.llm = llm

    def read(self, image_path, hint=""):
        res = self.llm.complete_json(
            "classify", VISION_SYSTEM,
            [Block(text="Transcribe this image."), Block(image_path=image_path)],
            "transcription", VISION_SCHEMA, max_tokens=4000)
        return OCRResult(text=res.data["text"], confidence=float(res.data["legibility"]),
                         engine="ocr:vision")


class RoutedOCR:
    """Scanned pages -> document OCR engine; photos (embedded images) -> vision model.
    Falls back to Tesseract whenever the preferred engine is unavailable."""

    name = "routed"

    def __init__(self, settings: Settings, llm: LLMProvider):
        self.tesseract = TesseractOCR()
        self.vision = VisionOCR(llm)
        self.document = TextractOCR(settings) if settings.mode == "aws" else None

    def read(self, image_path, hint=""):
        try:
            if hint == "photo":
                return self.vision.read(image_path)
            if self.document is not None:
                return self.document.read(image_path, hint)
        except LLMUnavailable:
            pass
        return self.tesseract.read(image_path)


class CachingOCR:
    """Saves OCR results from live runs and serves them back in demo mode.

    Keyed by the SOURCE file's hash plus the page/unit locator -- not by rendered image
    bytes, which can differ slightly between machines -- so a run recorded on one laptop
    replays identically anywhere.
    """

    def __init__(self, inner: OCRProvider, cache_dir: Path, replay: bool, record: bool):
        self.inner, self.cache_dir, self.replay, self.record = inner, cache_dir, replay, record
        self.name = f"{inner.name}+{'replay' if replay else 'record' if record else 'live'}"

    def read(self, image_path, hint="", cache_key: str | None = None):
        path = self.cache_dir / f"{cache_key}.json" if cache_key else None
        if self.replay and path is not None and path.exists():
            rec = json.loads(path.read_text())
            return OCRResult(text=rec["text"], confidence=rec["confidence"], engine=rec["engine"])
        res = self.inner.read(image_path, hint)
        if self.record and not self.replay and path is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"engine": res.engine, "confidence": res.confidence,
                                        "text": res.text}, indent=2))
        return res


def build_ocr(settings: Settings, llm: LLMProvider) -> OCRProvider:
    return CachingOCR(RoutedOCR(settings, llm), settings.ocr_cache_dir,
                      replay=settings.mode == "demo", record=settings.record_llm)
