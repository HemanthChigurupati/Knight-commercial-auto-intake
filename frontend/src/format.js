// Plain-language labels for the values the pipeline produces.

export const DOC_TYPES = {
  application: "Application",
  quote_form: "Quote form",
  driver_schedule: "Driver schedule",
  vehicle_schedule: "Vehicle schedule",
  mvr: "MVR",
  driver_license: "Driver license",
  loss_run: "Loss run",
  no_loss_letter: "No-loss letter",
  ifta_return: "IFTA return",
  underwriting_guideline: "Underwriting guideline",
  other: "Other",
};

export const OUTCOMES = { refer: "Refer", decline: "Decline", proceed: "Proceed" };
export const ROUTES = { small_fleet: "Small Fleet underwriting", fleet: "Fleet underwriting" };

export const KINDS = {
  conflict: "Conflict",
  gap: "Missing information",
  possible_misread: "Possible misread",
  outdated_document: "Outdated document",
  superseded: "Superseded",
};

export const VERDICTS = {
  confirmed: "Agent confirmed",
  resolved: "Agent resolved",
  corrected: "Agent corrected a misread",
  needs_human: "Agent needs a human",
  not_run: "Agent did not run",
};

export const STATUS = {
  open: "Open",
  confirmed: "Confirmed",
  resolved: "Resolved",
  needs_human: "Needs review",
};

export const RULE_STATUS = { pass: "Pass", fail: "Fail", unknown: "Can't verify", info: "Note" };

export const OWNERS = {
  agent: "Ask the agent",
  underwriter: "Underwriter",
  operations: "Operations",
};

export const TOOLS = {
  read_source_text: "Read the text",
  view_source_image: "Looked at the page",
  decode_vin: "Decoded the VIN",
};

export const isRuleId = (id) => /^[A-F]\d$/.test(id);
export const isFindingId = (id) => /^F\d{2}$/.test(id);
export const isDocId = (id) => /^d\d{2}$/.test(id);

export function fmtValue(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (v === true) return "Yes";
  if (v === false) return "No";
  return String(v);
}

export function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function pct(x) {
  return `${Math.round((x || 0) * 100)}%`;
}

// Find a claim by its number across the loss runs.
export function findClaim(sub, number) {
  for (const ex of sub.extractions) {
    for (const c of ex.records?.claims || []) {
      if (String(c.claim_number?.value) === number) return { claim: c, docId: ex.doc_id };
    }
  }
  return null;
}

export function needsAttention(f) {
  return f.status === "open" || f.status === "confirmed" || f.status === "needs_human";
}
