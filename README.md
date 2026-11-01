# Knight Commercial Auto Submission Intake (prototype)

AI-assisted intake and review of emailed Commercial Auto submissions: classify every
document by content, extract and reconcile the data, re-check conflicts with an agent,
apply Knight's underwriting guideline as deterministic rules, and present one consolidated
view to the underwriter.

> Work in progress. Stages 1-3 (intake/classification, extraction, reconciliation) are built and tested.

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
8. Agentic recheck -> 9. Rules -> 10. AI analysis -> 11. Review *(in progress)*

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

## Run stage 1

```bash
cd backend
pip install -r requirements.txt        # plus the tesseract binary for demo mode
cp ../.env.example ../.env
python -m app.pipeline.run "../samples/cobalt-mesa/Sample Submission - Cobalt Mesa Transport.zip"
pytest -q
```

All companies, people and records in `samples/` are fictitious (provided with the case study).
