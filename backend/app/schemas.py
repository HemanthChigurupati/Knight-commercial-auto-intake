"""Core data model for a submission as it moves through the pipeline."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class DocType(str, Enum):
    APPLICATION = "application"            # carrier commercial auto application
    QUOTE_FORM = "quote_form"              # agency quick-quote / ACORD-style form
    DRIVER_SCHEDULE = "driver_schedule"
    VEHICLE_SCHEDULE = "vehicle_schedule"
    MVR = "mvr"                            # motor vehicle record for one driver
    DRIVER_LICENSE = "driver_license"      # photo or scan of a license card
    LOSS_RUN = "loss_run"                  # carrier-issued claims history for one policy term
    NO_LOSS_LETTER = "no_loss_letter"      # letter asserting no losses (not a full loss run)
    IFTA_RETURN = "ifta_return"            # quarterly fuel-tax return
    UNDERWRITING_GUIDELINE = "underwriting_guideline"
    OTHER = "other"


class UnitKind(str, Enum):
    PDF_PAGE = "pdf_page"
    EMBEDDED_IMAGE = "embedded_image"     # an image object found inside a PDF page
    IMAGE_FILE = "image_file"
    SHEET = "sheet"                       # one worksheet of a spreadsheet
    TEXT_FILE = "text_file"


class SourceFile(BaseModel):
    file_id: str
    name: str                              # name as received; never trusted for type
    path_in_package: str                   # e.g. "candidate_pack.zip/Sample.zip/AL 23-24.pdf"
    sha256: str
    size: int
    media_type: str
    duplicate_of: str | None = None        # file_id of the first identical file


class ContentUnit(BaseModel):
    """The smallest thing we classify: a page, a sheet, or an image."""
    unit_id: str
    file_id: str
    kind: UnitKind
    index: int                             # page number (1-based) or sheet/image order
    label: str                             # human-readable locator, e.g. "MVR REPORT.pdf p.7 image 1"
    text: str = ""
    text_source: str = "none"              # text_layer | ocr:textract | ocr:tesseract | ocr:vision | sheet
    ocr_confidence: float | None = None
    image_path: str | None = None          # rendered page or extracted image, for OCR/vision
    hidden: bool = False                   # e.g. a hidden worksheet
    meta: dict = Field(default_factory=dict)


class Classification(BaseModel):
    doc_type: DocType
    confidence: float                      # 0..1, combined
    signal_type: DocType | None = None     # keyword/layout signal's pick
    signal_score: float = 0.0
    llm_type: DocType | None = None
    llm_confidence: float | None = None
    reason: str = ""
    needs_review: bool = False


class ClassifiedUnit(BaseModel):
    unit: ContentUnit
    classification: Classification


class LogicalDocument(BaseModel):
    """One real-world document, possibly spanning several units (or part of one file)."""
    doc_id: str
    doc_type: DocType
    confidence: float
    unit_ids: list[str]
    file_ids: list[str]
    title: str
    observations: list[str] = Field(default_factory=list)


class Submission(BaseModel):
    submission_id: str
    received_name: str
    files: list[SourceFile] = Field(default_factory=list)
    units: list[ContentUnit] = Field(default_factory=list)
    classified: list[ClassifiedUnit] = Field(default_factory=list)
    documents: list[LogicalDocument] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)   # audit trail of pipeline steps
