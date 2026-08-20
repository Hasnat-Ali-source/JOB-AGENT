/**
 * Thin client over the Job Agent API.
 *
 * Every call goes to the local Python process on the same origin. Errors carry
 * the server's own `detail` string, because the API's refusals are written for
 * a person to read ("indeed still looks blocked (captcha still showing)") and
 * replacing them with "Request failed" would throw away the product.
 */

const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function request(path, { method = "GET", body, form, query } = {}) {
  const url = new URL(BASE + path, window.location.origin);

  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === "") continue;
      url.searchParams.set(key, String(value));
    }
  }

  const init = { method, headers: {} };

  if (form) {
    init.body = form;
  } else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new ApiError(
      "Can't reach the agent. Is `python -m job_agent dashboard` still running?",
      0,
      null,
    );
  }

  const isJson = (response.headers.get("content-type") || "").includes("json");
  const payload = isJson ? await response.json().catch(() => null) : await response.text();

  if (!response.ok) {
    const detail =
      (payload && typeof payload === "object" && payload.detail) ||
      (typeof payload === "string" && payload.slice(0, 300)) ||
      `${response.status} ${response.statusText}`;
    throw new ApiError(
      typeof detail === "string" ? detail : JSON.stringify(detail),
      response.status,
      payload,
    );
  }

  return payload;
}

export const api = {
  // Overview
  status: () => request("/status"),
  health: () => request("/health"),
  checkHealth: (platform) => request("/health/check", { method: "POST", query: { platform } }),

  // Platforms and connectors
  connectors: () => request("/connectors"),
  accounts: () => request("/accounts"),
  connect: (platform) => request("/accounts/connect", { method: "POST", query: { platform } }),
  checkStatus: (platform) => request(`/accounts/${platform}/check-status`, { method: "POST" }),
  addStation: (body) => request("/accounts/custom", { method: "POST", body }),
  disconnect: (platform) => request(`/accounts/${platform}/disconnect`, { method: "POST" }),
  reconnect: (platform) => request(`/platforms/${platform}/reconnect`, { method: "POST" }),
  resume: (platform) => request(`/platforms/${platform}/resume`, { method: "POST" }),

  // Interruptions
  interruptions: (includeResolved = false) =>
    request("/interruptions", { query: { include_resolved: includeResolved } }),
  resolveInterruption: (id, verify = true) =>
    request(`/interruptions/${id}/resolve`, { method: "POST", query: { verify } }),

  // Candidate profile
  profile: () => request("/review/profile"),
  saveProfile: (data) => request("/review/profile", { method: "POST", body: data }),

  // Documents
  masters: () => request("/documents/masters"),
  master: (id) => request(`/documents/masters/${id}`),
  uploadMaster: (file, docType, name) => {
    const form = new FormData();
    form.append("file", file);
    form.append("doc_type", docType);
    if (name) form.append("name", name);
    return request("/documents/masters", { method: "POST", form });
  },
  activateMaster: (id) => request(`/documents/masters/${id}/activate`, { method: "POST" }),
  deleteMaster: (id) => request(`/documents/masters/${id}`, { method: "DELETE" }),
  atsScore: (id) => request(`/documents/masters/${id}/ats`),
  atsImprove: (id, apply = false) =>
    request(`/documents/masters/${id}/ats/improve`, { method: "POST", query: { apply } }),
  fitReport: (jobId) => request(`/documents/fit/${jobId}`),
  resyncPipeline: () =>
    request("/documents/masters/resync", { method: "POST" }),
  prepareApplication: (jobId) =>
    request(`/jobs/${jobId}/prepare`, { method: "POST" }),
  resumeMatch: (profileId) =>
    request("/documents/match", { query: { profile_id: profileId } }),
  versions: (query) => request("/documents/versions", { query }),
  generateDocuments: (jobId, docType = "package") =>
    request("/documents/generate", { method: "POST", query: { job_id: jobId, doc_type: docType } }),

  // Search profiles and jobs
  searchProfiles: () => request("/search/profiles"),
  deleteSearchProfile: (id) =>
    request(`/search/profiles/${id}`, { method: "DELETE" }),
  createSearchProfile: (data) => request("/search/profiles", { method: "POST", body: data }),
  searchStats: () => request("/search/stats"),
  jobs: (query) => request("/jobs", { query }),
  job: (id) => request(`/jobs/${id}`),

  // Runs
  runs: (limit = 20) => request("/runs", { query: { limit } }),
  startRun: ({ profileId, generateDocuments, queueApplications }) =>
    request("/runs", {
      method: "POST",
      query: {
        profile_id: profileId,
        generate_documents: generateDocuments,
        queue_applications: queueApplications,
      },
    }),

  // Review queue — the product's centre of gravity
  queue: (status = "queued_for_review") => request("/review", { query: { status } }),
  review: (id) => request(`/review/${id}`),
  readMyAnswers: (id, rememberSensitive = false) =>
    request(`/review/${id}/read-my-answers`, {
      method: "POST",
      query: { remember_sensitive: rememberSensitive },
    }),
  answer: (id, answers, remember = true, rememberSensitive = false) =>
    request(`/review/${id}/answers`, {
      method: "POST",
      body: { answers, remember, remember_sensitive: rememberSensitive },
    }),
  applySavedAnswers: () =>
    request("/review/apply-saved-answers", { method: "POST" }),
  approve: (id, notes) => request(`/review/${id}/approve`, { method: "POST", body: { notes } }),
  discard: (id, reason) => request(`/review/${id}/discard`, { method: "POST", body: { reason } }),
  eligibility: (id) => request(`/review/${id}/eligibility`),
  analysis: (id) => request(`/review/${id}/analysis`),
  // Rebuilds the tailored documents and attaches the new ones. Clears the
  // approval: what was approved is not what would now be sent.
  fitDocumentsToJob: (id) =>
    request(`/review/${id}/regenerate-documents`, {
      method: "POST",
      query: { fit_to_posting: true },
    }),
  regenerateDocuments: (id) =>
    request(`/review/${id}/regenerate-documents`, { method: "POST" }),
  // acceptWeakFit waives the one finding that is a judgement rather than a
  // defect: that the resume evidences too little of what the posting asks for.
  submit: (id, acceptWeakFit = false) =>
    request(`/review/${id}/submit`, {
      method: "POST",
      query: { accept_weak_fit: acceptWeakFit },
    }),

  // Email applications
  detectRecipient: (jobId) => request("/email/detect", { query: { job_id: jobId } }),
  drafts: (status) => request("/email/drafts", { query: { status } }),
  draft: (id) => request(`/email/drafts/${id}`),
  createDraft: (jobId, toEmail) =>
    request("/email/drafts", { method: "POST", query: { job_id: jobId, to_email: toEmail } }),
  editDraft: (id, data) => request(`/email/drafts/${id}`, { method: "PATCH", body: data }),
  approveDraft: (id, notes) =>
    request(`/email/drafts/${id}/approve`, { method: "POST", body: { notes } }),
  sendDraft: (id) => request(`/email/drafts/${id}/send`, { method: "POST" }),
  discardDraft: (id, reason) =>
    request(`/email/drafts/${id}/discard`, { method: "POST", body: { reason } }),
  threads: () => request("/email/threads"),

  // Register
  audit: (query) => request("/audit", { query }),
  auditActions: () => request("/audit/actions"),
  auditSummary: (days = 7) => request("/audit/summary", { query: { days } }),
  exports: () => request("/exports"),

  // Settings
  settings: () => request("/settings"),
  updatePlatform: (platform, data) =>
    request(`/settings/platforms/${platform}`, { method: "PATCH", body: data }),
  schedule: () => request("/schedule"),
};

/** URL for a rendered PDF, used directly in links. */
export const pdfUrl = (versionId) => `${BASE}/documents/versions/${versionId}/pdf`;

/** URL for the screenshot of a filled form. */
export const screenshotUrl = (applicationId) => `${BASE}/review/${applicationId}/screenshot`;

/** URL for a CSV export. */
export const exportUrl = (name) => `${BASE}/exports/${name}`;
