"""Stage 2 -- extraction. The checks are plain code, so they are tested directly; the
extraction flow is tested with a fake Claude that returns a fixed answer for the real
vehicle schedule text, so the test is free and gives the same result every run."""
from pathlib import Path

from app.pipeline.extract import extract_document, fact_counts, tool_schema
from app.pipeline.verify import grounding_score, parse_date, vin_checks, vin_decode
from app.extraction.specs import SPECS
from app.providers.llm import LLMResult
from app.schemas import ContentUnit, DocType, FactStatus, LogicalDocument, UnitKind

VEHICLE_SHEET = ("Vehicle #\tYear\tMake\tModel\tVIN\tCost New\n"
                 "1\t2022\tVOLVO\tTRACTOR\t4V4NC9EH4NN301742\t82000\n"
                 "2\t2020\tVOLVO\tTRACTOR\t4V4NC9EH9LN248615\t48000\n"
                 "3\t2019\tINTERNATIONAL\tTRACTOR\t3HSDZAPR0KN617093\t41000\n"
                 "4\t2017\tFREIGHTLINER\tTRACTOR\t3AKJGLD59HSKR736\t22000\n"
                 "TOTAL TIV\nTRAILER INTERCHANGE\t50000")


# ---------------------------------------------------------------- checks (no AI)

def test_dates_in_every_format_found_in_the_package():
    assert parse_date("09/14/79").isoformat() == "1979-09-14"        # quote form, 2-digit year
    assert parse_date("06-02-1983").isoformat() == "1983-06-02"      # NJ license card
    assert parse_date("March 31, 2026").isoformat() == "2026-03-31"  # IFTA period end
    assert parse_date("2026-10-22 00:00:00").isoformat() == "2026-10-22"  # Excel cell
    assert parse_date("12/15/2026.").isoformat() == "2026-12-15"     # trailing period on the app
    assert parse_date("OWNER") is None


def test_vin_rules():
    assert vin_checks("4V4NC9EH9LN248615") == []
    assert vin_checks("3AKJGLD59HSKR736") == ["VIN must be 17 characters (has 16)"]
    assert "check digit" in vin_checks("4V4NC9EH8LN248615")[0]       # one character misread
    assert vin_decode("4V4NC9EH9LN248615") == {"make": "VOLVO", "model_year": 2020}


def test_grounding_catches_invented_quotes():
    assert grounding_score("4V4NC9EH9LN248615", VEHICLE_SHEET) == 1.0
    assert grounding_score("2018 KENWORTH T680 1XKYD49X0JJ123456", VEHICLE_SHEET) == 0.4


def test_every_spec_builds_a_tool_schema():
    for spec in SPECS.values():
        schema = tool_schema(spec)
        assert set(schema["required"]) == {f.name for f in spec.fields} | {l.name for l in spec.lists}


# ---------------------------------------------------------------- extraction flow

class FakeLLM:
    name = "fake"

    def complete_json(self, role, system, blocks, schema_name, schema, max_tokens=2000):
        assert role == "extract" and schema_name == "extract_vehicle_schedule"
        assert "[[unit f014:s1]]" in blocks[0].text
        row = lambda n, y, mk, vin, val, q: {"year": y, "make": mk, "model": "TRACTOR", "vin": vin,
                                             "value": val, "quote": q, "unit": "f014:s1",
                                             "confidence": 0.97}
        return LLMResult(model="fake-sonnet", data={
            "trailer_interchange_limit": {"value": "50000", "quote": "TRAILER INTERCHANGE\t50000",
                                          "unit": "f014:s1", "confidence": 0.95},
            "vehicles": [
                row(1, "2022", "VOLVO", "4V4NC9EH4NN301742", "82000",
                    "1\t2022\tVOLVO\tTRACTOR\t4V4NC9EH4NN301742\t82000"),
                row(4, "2017", "FREIGHTLINER", "3AKJGLD59HSKR736", "22000",
                    "4\t2017\tFREIGHTLINER\tTRACTOR\t3AKJGLD59HSKR736\t22000"),
                # A row that is not in the document: the model invented it.
                row(5, "2018", "KENWORTH", "1XKYD49X0JJ123456", "60000",
                    "5\t2018\tKENWORTH\tT680\t1XKYD49X0JJ123456\t60000"),
            ]})


def test_extraction_checks_every_value():
    unit = ContentUnit(unit_id="f014:s1", file_id="f014", kind=UnitKind.SHEET, index=1,
                       label="VEHICLE LIST", text=VEHICLE_SHEET, text_source="sheet")
    doc = LogicalDocument(doc_id="d22", doc_type=DocType.VEHICLE_SCHEDULE, confidence=0.99,
                          unit_ids=["f014:s1"], file_ids=["f014"], title="Vehicle Schedule")
    ex = extract_document(doc, [unit], FakeLLM())

    assert ex.status == "ok"
    assert ex.facts["trailer_interchange_limit"].value == 50000.0
    good, short_vin, invented = ex.records["vehicles"]
    assert good["vin"].status == FactStatus.FOUND and good["year"].value == 2022
    # Read correctly (high confidence) but the document itself is wrong (invalid).
    assert short_vin["vin"].status == FactStatus.INVALID
    assert short_vin["vin"].confidence > 0.9
    assert short_vin["vin"].checks == ["VIN must be 17 characters (has 16)"]
    # Not on the page: flagged and heavily discounted.
    assert invented["vin"].status == FactStatus.UNVERIFIED
    assert invented["vin"].confidence < 0.5
    assert fact_counts(ex)["unverified"] == 5
