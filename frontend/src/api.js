// Every call to the backend goes through here.

const CODE_KEY = "knight-access-code";

export function getCode() {
  try { return localStorage.getItem(CODE_KEY) || ""; } catch { return ""; }
}
export function setCode(code) {
  try { localStorage.setItem(CODE_KEY, code); } catch { /* private mode: keep it in memory only */ }
  memoryCode = code;
}
let memoryCode = "";

async function call(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const code = getCode() || memoryCode;
  if (code) headers["X-Access-Code"] = code;
  const res = await fetch(path, { ...options, headers });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return body;
}

export const api = {
  config: () => call("/api/config"),
  list: () => call("/api/submissions"),
  get: (id) => call(`/api/submissions/${id}`),
  status: (id) => call(`/api/submissions/${id}/status`),
  upload: (files) => {
    const form = new FormData();
    for (const f of files) form.append("files", f);
    return call("/api/submissions", { method: "POST", body: form });
  },
  runSample: () => call("/api/samples/run", { method: "POST" }),
  review: (id, findingId, action, note) =>
    call(`/api/submissions/${id}/findings/${findingId}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, note }),
    }),
  imageUrl: (id, unitId) => `/api/submissions/${id}/units/${encodeURIComponent(unitId)}/image`,
  fileUrl: (id, fileId) => `/api/submissions/${id}/files/${fileId}`,
};
