# Knight Commercial Auto Submission Intake (prototype)

AI-assisted intake and review of emailed Commercial Auto submissions: classify every
document by content, extract and reconcile the data, re-check conflicts with an agent,
apply Knight's underwriting guideline as deterministic rules, and present one consolidated
view to the underwriter.

> Pipeline and review web app complete. The hosted demo replays a recorded live Amazon Bedrock run, so it costs nothing to explore.

## Pipeline

1. **Intake**: .eml / .zip / loose files; nested archives expanded; SHA-256 duplicate detection
2. **Preprocess**: split PDFs into pages, detect text layer, extract embedded photos, read worksheets
3. **OCR**: scans -> Textract (aws) or Tesseract (demo); photos -> Claude vision
4. **Classify**: per page, content only; keyword signals + Claude Haiku, combined confidence
5. **Group**: pages regrouped into logical documents (one MVR per driver, one loss run per term)
6. **Extract**: facts per document type from `extraction/specs.py` (the rule -> fact map); each fact
   carries value, verbatim quote, page and confidence, and is checked in code (`pipeline/verify.py`):
   quote grounding, normalization, VIN length/characters/check digit
7. **Reconcile** (`reconcile/`): facts regrouped into entities (applicant, drivers by license
   number, vehicles by VIN, policy terms, IFTA quarters); findings classified as conflict, gap,
   possible misread, outdated document or superseded, with severity and sources. Plain code, no AI.
8. **Agent recheck** (`agent/`): for findings that may be reading mistakes, Claude investigates with
   four read-only tools (read text, view page image, decode VIN, record verdict), capped at 6 turns;
   verdicts confirmed / resolved / corrected / needs_human with the full tool trace kept for audit.
   A plain tool-use loop, no framework.
9. **Rules** (`rules/rules.yaml` + `app/rules/`): Knight's guideline as data -- each rule cites its
   guideline section, a check, an outcome on failure and its thresholds. Open questions from the case
   study are named settings with defaults. Decision: any decline -> DECLINE, else any refer or
   unverifiable -> REFER, else PROCEED; routing by power units. Plain code, no AI.
10. **AI analysis** (`app/analysis/`): one Claude call over an ID-labelled brief of the structured
    results (never the raw PDFs) writes the summary, concerns and next actions. Every item must cite
    rule / finding / document ids; a code check strips unknown ids and drops uncited items. The
    rules-engine decision is fixed and never taken from the model.
11. **Review web app** (`app/api/` + `frontend/`): FastAPI serving a React app. Review queue, upload,
    background processing with step-by-step progress, and one page per submission: the decision,
    the AI analysis with clickable citations, findings with the agent's tool trace, all rules, every
    document with its extracted facts, and the audit trail. Any citation, fact or finding opens the
    source page with the quoted text highlighted. The underwriter can confirm or resolve a finding
    with a note; the rules re-run at once and the analysis is marked as written before the review.

`tests/test_reconcile.py` holds the **expected-findings test**: it replays the recorded Bedrock
run in `fixtures/` and checks that every issue an underwriter would find by hand is reported.

## Modes

| Mode | AI | OCR | Storage | Needs |
|---|---|---|---|---|
| `demo` (default) | Replays recorded responses | Tesseract | Local disk | Nothing |
| `local` | Anthropic API | Tesseract / Claude vision | Local disk | `ANTHROPIC_API_KEY` |
| `aws` | Amazon Bedrock (Claude, US inference profiles) | Textract / Claude vision | S3 (KMS) + SQS | AWS profile |

## Recording a live run for demo mode

Every live (`aws` / `local`) run saves each Claude response to `fixtures/llm_cache/` and each
OCR result to `fixtures/ocr_cache/` (keyed by source-file hash + page). Commit those folders
and `demo` mode replays that run exactly, with no credentials and no cost.

## Run it

```bash
cd backend
pip install -r requirements.txt        # plus the tesseract binary for new uploads in demo mode
uvicorn app.api.main:app --port 8000   # open http://localhost:8000
```

The server processes the Cobalt Mesa sample on first start (about 15 seconds in demo mode).
Pipeline only, from the command line:

```bash
python -m app.pipeline.run "../samples/cobalt-mesa/Sample Submission - Cobalt Mesa Transport.zip"
pytest -q
```

Or as one container: `docker build -t knight-intake . && docker run -p 8000:8000 knight-intake`.

**Live mode on a server.** Set `APP_MODE=aws` and `ACCESS_CODE=<something>`: every upload, rerun and
review then needs the code (sent as the `X-Access-Code` header; the page asks for it). Uploads are
limited to 25 MB and jobs run one at a time.

**Frontend.** The React source is in `frontend/src`; the built bundle in `frontend/dist` is committed
so the server needs only Python. After changing the source: `cd frontend && npm install && npm run build`.

All companies, people and records in `samples/` are fictitious (provided with the case study).
