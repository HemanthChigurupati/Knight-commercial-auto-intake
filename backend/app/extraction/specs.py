"""What to extract from each document type -- the rule -> fact map, written as code.

Every field names the guideline rule or underwriting question that needs it (`why`).
If no rule or reviewer question needs a value, it is not extracted: shorter prompts,
lower cost, and nothing for the model to get wrong that nobody would use.

Field kinds drive normalization and checks in verify.py:
  text, date, vin, int, money, bool, percent
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas import DocType


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str
    description: str
    why: str = ""


@dataclass(frozen=True)
class ListSpec:
    """A repeating row (a driver, a vehicle, a claim). Each row is quoted once."""
    name: str
    description: str
    fields: tuple[FieldSpec, ...]
    why: str = ""


@dataclass(frozen=True)
class DocSpec:
    doc_type: DocType
    purpose: str
    fields: tuple[FieldSpec, ...] = ()
    lists: tuple[ListSpec, ...] = field(default_factory=tuple)


F = FieldSpec

# Reused row definitions --------------------------------------------------------------

DRIVER_ROW = (
    F("first_name", "text", "Driver first name (and middle initial if shown)"),
    F("last_name", "text", "Driver last name"),
    F("dob", "date", "Date of birth", "age 23+, 65+ medical; match driver across documents"),
    F("license_number", "text", "Driver license number", "key to match driver to MVR and license"),
    F("license_state", "text", "State that issued the license"),
    F("experience", "text", "Years of experience or license class as written (e.g. '4+', 'CLASS C')",
      "2 years CDL experience"),
    F("hire_date", "date", "Date of hire, or the word written instead (e.g. OWNER)"),
)

VEHICLE_ROW = (
    F("year", "int", "Model year"),
    F("make", "text", "Manufacturer"),
    F("model", "text", "Model or body type as written (e.g. TRACTOR)", "eligible vehicle types"),
    F("vin", "vin", "Vehicle identification number exactly as printed", "17-character VIN rule; vehicle key"),
    F("value", "money", "Stated value or cost new", "stated value required"),
)

COMMODITY_ROW = (
    F("commodity", "text", "Commodity hauled", "prohibited cargo (hazmat, lithium, waste, intermodal)"),
    F("percent", "percent", "Share of loads"),
)

# Specs ---------------------------------------------------------------------------------

SPECS: dict[DocType, DocSpec] = {
    DocType.APPLICATION: DocSpec(
        DocType.APPLICATION,
        "Carrier's commercial auto insurance application.",
        fields=(
            F("applicant_name", "text", "Legal business name of the applicant", "match applicant across documents"),
            F("entity_type", "text", "Corporation, LLC, sole proprietorship or partnership (whichever is marked)",
              "new-venture rules differ by entity"),
            F("fein", "text", "Federal employer ID / tax ID number", "submission requirement: FEIN/SSN"),
            F("dot_number", "text", "USDOT number", "SAFER check"),
            F("mc_number", "text", "MC / docket number", "submission requirement: MC/Docket"),
            F("application_date", "date", "Date of application", "60-day loss-run and 90-day MVR windows"),
            F("effective_date", "date", "Requested effective date"),
            F("mailing_address", "text", "Mailing address incl. city, state, zip"),
            F("garaging_address", "text", "Physical garaging address", "available states / border rule"),
            F("years_in_business", "int", "Years of experience operating a transportation company", "new venture"),
            F("years_prior_insurance", "text", "Years of prior insurance as written", "continuous coverage"),
            F("liability_limit", "money", "Auto liability limit requested"),
            F("liability_deductible", "text", "Auto liability deductible as written", "no AL deductibles allowed"),
            F("physical_damage_requested", "bool", "Whether physical damage values/deductible are requested",
              "physical damage not available"),
            F("new_venture", "bool", "Answer to 'New Venture?'"),
            F("owner_is_driver", "bool", "Answer to 'Is Owner a driver?'", "owner must meet driver criteria if driving"),
            F("intermodal", "bool", "Hauls intermodal containers", "prohibited exposure"),
            F("cross_border", "bool", "Operates cross-border services", "50 miles of Mexico border"),
            F("farthest_territory", "text", "Farthest territory traveled"),
            F("radius_breakdown", "text", "Operating radius bands and percentages as written"),
            F("signed", "bool", "Whether the signature/date block is filled in (false if blank)"),
        ),
        lists=(
            ListSpec("drivers", "Drivers listed on the application itself (not an attached list)",
                     DRIVER_ROW),
            ListSpec("vehicles", "Vehicles listed on the application itself (not an attached list)",
                     VEHICLE_ROW),
            ListSpec("commodities", "Commodities hauled with share of loads", COMMODITY_ROW),
        ),
    ),
    DocType.QUOTE_FORM: DocSpec(
        DocType.QUOTE_FORM,
        "Agency quick-quote form summarizing the risk.",
        fields=(
            F("agency_name", "text", "Agency name"),
            F("applicant_name", "text", "Applicant business name", "match applicant across documents"),
            F("mailing_address", "text", "Applicant mailing address incl. city, state, zip"),
            F("years_in_business", "int", "Years in business"),
            F("years_prior_insurance", "text", "Years of prior insurance as written", "continuous coverage"),
            F("dot_number", "text", "USDOT number"),
            F("mc_number", "text", "MC number"),
            F("operation_type", "text", "Operation type marked (For Hire, Private, ...)"),
            F("radius_breakdown", "text", "Radius bands and percentages as written"),
            F("liability_limit", "money", "Auto liability limit"),
            F("physical_damage_requested", "bool", "Physical damage quote requested?", "physical damage not available"),
            F("cargo_requested", "bool", "Cargo quote requested?"),
            F("hauls_hazmat", "bool", "Answer to 'haul any hazardous materials'", "prohibited exposure"),
            F("vehicles_not_listed", "bool", "Answer to 'vehicles ... NOT listed on the application'",
              "unreported units"),
            F("team_driving", "bool", "Answer to 'utilize team driving'"),
            F("ownership_changes", "bool", "Changes in ownership/management/name in past 5 years"),
        ),
        lists=(
            ListSpec("prior_policies", "Prior policy history rows", (
                F("start_date", "date", "Policy start date"),
                F("end_date", "date", "Policy end date"),
                F("carrier", "text", "Carrier name"),
                F("coverage", "text", "Coverage types as written"),
                F("units_insured", "int", "Number of units insured"),
            ), "3 years continuous coverage; lapses over 30 days; loss runs for every term"),
            ListSpec("drivers", "Drivers written on the form itself", DRIVER_ROW),
            ListSpec("commodities", "Commodities with share of loads", COMMODITY_ROW),
        ),
    ),
    DocType.DRIVER_SCHEDULE: DocSpec(
        DocType.DRIVER_SCHEDULE, "List of drivers.",
        lists=(ListSpec("drivers", "Every driver row", DRIVER_ROW, "driver criteria; MVR coverage"),),
    ),
    DocType.VEHICLE_SCHEDULE: DocSpec(
        DocType.VEHICLE_SCHEDULE, "List of vehicles / power units.",
        fields=(F("trailer_interchange_limit", "money", "Trailer interchange amount if listed"),),
        lists=(ListSpec("vehicles", "Every vehicle row (skip total/summary rows)", VEHICLE_ROW,
                        "power-unit count for routing; VIN rule; eligible types"),),
    ),
    DocType.MVR: DocSpec(
        DocType.MVR, "Motor vehicle record for one driver.",
        fields=(
            F("driver_name", "text", "Driver name as printed", "match to driver schedule"),
            F("license_number", "text", "License number", "driver key"),
            F("license_state", "text", "State of the record"),
            F("dob", "date", "Date of birth if shown", "age; DOB conflicts"),
            F("license_class", "text", "License class of the current license (A, B, C...)", "CDL required"),
            F("license_type", "text", "Commercial or personal/non-commercial", "CDL required"),
            F("license_status", "text", "License status (VALID, SUSPENDED...)"),
            F("license_issue_date", "date", "Issue date of the current license", "2 years CDL experience"),
            F("license_expiry_date", "date", "Expiration date"),
            F("first_licensed_year", "int", "Approximate year license first issued"),
            F("cdl_first_issued", "date", "Date the CDL was first issued, if a history line states it",
              "2 years CDL experience"),
            F("order_date", "date", "Date the MVR was ordered/run", "MVR dated within 90 days"),
            F("stale_marker", "text", "Any stamp marking the report as old/outdated (e.g. 'OLD MVR'), else empty",
              "MVR within 90 days"),
        ),
        lists=(
            ListSpec("violations", "Each violation or conviction", (
                F("date", "date", "Violation date"),
                F("description", "text", "Description as written",
                  "unacceptable history: DUI, reckless, 30+ over, etc."),
                F("points", "int", "Points assessed, if shown", "6 pts / 3 yrs, 4 pts / 12 mo"),
            )),
            ListSpec("accidents", "Each accident", (
                F("date", "date", "Accident date"),
                F("location", "text", "Location"),
                F("at_fault", "text", "At-fault indicator as written, empty if not stated"),
            )),
        ),
    ),
    DocType.DRIVER_LICENSE: DocSpec(
        DocType.DRIVER_LICENSE, "Photo or scan of a driver's license card.",
        fields=(
            F("holder_name", "text", "Full name on the card", "match to MVR"),
            F("license_number", "text", "License number", "driver key"),
            F("license_state", "text", "Issuing state"),
            F("dob", "date", "Date of birth", "DOB conflicts"),
            F("license_class", "text", "Class"),
            F("commercial", "bool", "Whether the card says commercial driver license", "CDL required"),
            F("issue_date", "date", "Issue date"),
            F("expiry_date", "date", "Expiration date", "valid license"),
            F("address", "text", "Address on the card"),
        ),
    ),
    DocType.LOSS_RUN: DocSpec(
        DocType.LOSS_RUN, "Carrier claims report for one policy term.",
        fields=(
            F("carrier", "text", "Insurance company"),
            F("insured_name", "text", "Name of insured", "match applicant"),
            F("policy_number", "text", "Policy number", "term key"),
            F("coverage", "text", "Coverage this report covers (auto liability, cargo, physical damage)"),
            F("period_start", "date", "Policy period start", "3 prior years; lapses"),
            F("period_end", "date", "Policy period end"),
            F("run_date", "date", "Run / valuation date", "valued within 60 days"),
            F("total_claims", "int", "Total claims count (0 if it says no claims)"),
            F("open_claims", "int", "Open claims count"),
            F("total_paid", "money", "Total losses paid"),
            F("total_reserved", "money", "Total reserved"),
            F("total_incurred", "money", "Total incurred"),
        ),
        lists=(ListSpec("claims", "Each claim", (
            F("claim_number", "text", "Claim number"),
            F("date_of_loss", "date", "Date of loss"),
            F("date_reported", "date", "Date reported"),
            F("driver_name", "text", "Driver involved", "unreported drivers"),
            F("vehicle_vin", "vin", "Vehicle VIN involved", "unreported units"),
            F("status", "text", "Open or closed"),
            F("incurred", "money", "Claim total incurred"),
            F("late_reported", "bool", "Late reported claim flag"),
            F("unreported_driver_or_unit", "text", "Any unreported driver/unit flags as written"),
        ), "loss history concerns"),),
    ),
    DocType.NO_LOSS_LETTER: DocSpec(
        DocType.NO_LOSS_LETTER, "Letter asserting no losses.",
        fields=(
            F("issuer", "text", "Organization that wrote the letter"),
            F("insured_name", "text", "Insured named"),
            F("policy_number", "text", "Policy number", "term key"),
            F("period_start", "date", "Period start"),
            F("period_end", "date", "Period end"),
            F("letter_date", "date", "Date of the letter", "valued within 60 days"),
            F("statement", "text", "The no-loss statement, verbatim",
              "a letter is weaker evidence than a loss run (e.g. 'no paid losses' says nothing of reserves)"),
        ),
    ),
    DocType.IFTA_RETURN: DocSpec(
        DocType.IFTA_RETURN, "Quarterly IFTA fuel-tax return.",
        fields=(
            F("taxpayer_name", "text", "Taxpayer name", "match applicant (entity name conflicts)"),
            F("account_number", "text", "Account number"),
            F("period_start", "date", "Period begin", "most recent 4 quarters"),
            F("period_end", "date", "Period end"),
            F("total_miles", "int", "Total miles, all jurisdictions"),
            F("penalty", "money", "Penalty amount", "late filing"),
            F("interest", "money", "Interest amount"),
            F("total_due", "money", "Total balance due"),
            F("jurisdictions", "text", "Comma-separated list of every jurisdiction (state) with miles",
              "operations in non-listed states; Mexico border states"),
        ),
    ),
}


def spec_for(doc_type: DocType) -> DocSpec | None:
    return SPECS.get(doc_type)
