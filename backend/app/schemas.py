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
    excluded_unit_ids: list[str] = Field(default_factory=list)   # e.g. hidden worksheets
    title: str
    observations: list[str] = Field(default_factory=list)


class Submission(BaseModel):
    submission_id: str
    received_name: str
    files: list[SourceFile] = Field(default_factory=list)
    units: list[ContentUnit] = Field(default_factory=list)
    classified: list[ClassifiedUnit] = Field(default_factory=list)
    documents: list[LogicalDocument] = Field(default_factory=list)
    extractions: list["Extraction"] = Field(default_factory=list)
    reconciliation: "Reconciliation | None" = None
    decision: "Decision | None" = None
    events: list[dict] = Field(default_factory=list)   # audit trail of pipeline steps


# ---------------------------------------------------------------- stage 2: extraction

class FactStatus(str, Enum):
    FOUND = "found"          # value read and grounded in the source
    MISSING = "missing"      # the extractor looked and the document does not state it
    INVALID = "invalid"      # value read, but fails a format rule (e.g. 16-character VIN)
    UNVERIFIED = "unverified"  # value returned but its quote was not found on the page


class Fact(BaseModel):
    """One extracted value with its proof."""
    name: str
    kind: str                          # text | date | vin | int | money | bool | percent
    raw: str = ""                      # value exactly as the model read it
    value: str | int | float | bool | None = None   # normalized (ISO date, upper-case VIN...)
    quote: str = ""                    # verbatim text from the page that shows the value
    unit_id: str | None = None         # which page/sheet/photo the quote is on
    model_confidence: float = 0.0
    grounding: float = 0.0             # 1.0 quote found verbatim ... 0.4 not found
    confidence: float = 0.0            # combined reading confidence
    status: FactStatus = FactStatus.MISSING
    checks: list[str] = Field(default_factory=list)   # format/validation findings


class Extraction(BaseModel):
    """Everything extracted from one logical document."""
    doc_id: str
    doc_type: DocType
    status: str = "ok"                 # ok | not_run (no model available) | skipped | error
    model: str = ""
    facts: dict[str, Fact] = Field(default_factory=dict)
    # Repeating rows (drivers, vehicles, claims...): list name -> rows -> field -> Fact
    records: dict[str, list[dict[str, Fact]]] = Field(default_factory=dict)
    error: str = ""



# ---------------------------------------------------------------- stage 3: reconciliation

class Observation(BaseModel):
    """One value for one field of an entity, and exactly where it came from."""
    value: str | int | float | bool | None
    doc_id: str | None = None          # None for derived values (counts, VIN decode)
    source: str                        # human label: "Driver Schedule row 4", "VIN decode"
    unit_id: str | None = None
    quote: str = ""
    confidence: float = 0.0


class Entity(BaseModel):
    entity_id: str                     # "driver:M5520814", "vehicle:4V4NC9EH9LN248615"
    kind: str                          # applicant | driver | vehicle | policy_term | ifta_quarter
    label: str
    fields: dict[str, list[Observation]] = Field(default_factory=dict)
    doc_ids: list[str] = Field(default_factory=list)

    def add(self, name: str, obs: "Observation | None") -> None:
        if obs is None or obs.value in (None, ""):
            return
        self.fields.setdefault(name, []).append(obs)
        if obs.doc_id and obs.doc_id not in self.doc_ids:
            self.doc_ids.append(obs.doc_id)


class FindingKind(str, Enum):
    CONFLICT = "conflict"                  # sources disagree on a value
    POSSIBLE_MISREAD = "possible_misread"  # differ only by characters OCR confuses
    OUTDATED_DOCUMENT = "outdated_document"  # an older document disagrees with a newer one
    SUPERSEDED = "superseded"              # a newer copy of the same record exists
    GAP = "gap"                            # one document mentions what another should cover


class AgentStep(BaseModel):
    tool: str
    input: dict = Field(default_factory=dict)
    result: str = ""                       # short summary of what the tool returned
    thought: str = ""                      # the model's stated reason for the call, if any


class AgentReview(BaseModel):
    verdict: str                           # confirmed | resolved | corrected | needs_human | not_run
    explanation: str = ""
    corrected_value: str = ""              # for 'corrected': the value the evidence supports
    corrected_source: str = ""             # which source was misread
    confidence: float = 0.0
    steps: list[AgentStep] = Field(default_factory=list)
    model: str = ""


class Finding(BaseModel):
    finding_id: str
    kind: FindingKind
    severity: str                          # high | medium | low
    entity_id: str
    field: str = ""
    title: str
    detail: str = ""
    observations: list[Observation] = Field(default_factory=list)
    needs_recheck: bool = False            # stage 4's agent should re-read the sources
    status: str = "open"                   # open | confirmed | resolved | needs_human
    review: AgentReview | None = None


class Reconciliation(BaseModel):
    entities: list[Entity] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


# ---------------------------------------------------------------- stage 5: rules

class RuleResult(BaseModel):
    rule_id: str
    section: str
    title: str
    on_fail: str                           # decline | refer | info
    status: str                            # pass | fail | unknown | info
    effect: str = "none"                   # what this result does to the decision
    message: str = ""
    evidence: list[Observation] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    outcome: str                           # decline | refer | proceed
    route: str                             # small_fleet | fleet
    route_reason: str = ""
    reasons: list[str] = Field(default_factory=list)   # rule ids that drove the outcome
    results: list[RuleResult] = Field(default_factory=list)
    assumptions: dict = Field(default_factory=dict)
    rules_version: str = ""                # hash of rules.yaml, for audit


Entity.model_rebuild()
Submission.model_rebuild()
