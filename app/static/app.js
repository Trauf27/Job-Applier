/* Job Applier — single-page UI. No build step, no dependencies. */

const state = {
  status: null,
  jobs: [],
  applications: [],
  companies: [],
  profile: null,
  openJobId: null,
};

/* ------------------------------------------------------------------ utils */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = text; }
  }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : `Request failed (${response.status})`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

let toastTimer;
function toast(message, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("is-error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 7000 : 3500);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/** Deliberately tiny Markdown renderer: headings, bullets, bold, italics, code. */
function renderMarkdown(source) {
  const lines = escapeHtml(source).split("\n");
  const out = [];
  let inList = false;
  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (heading) {
      closeList();
      const level = Math.min(heading[1].length + 1, 4);
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
    } else if (bullet) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inline(bullet[1])}</li>`);
    } else if (/^\s*---+\s*$/.test(line)) {
      closeList();
      out.push("<hr />");
    } else if (!line.trim()) {
      closeList();
    } else {
      closeList();
      out.push(`<p>${inline(line)}</p>`);
    }
  }
  closeList();
  return out.join("\n");

  function inline(text) {
    return text
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|\W)\*([^*]+)\*/g, "$1<em>$2</em>");
  }
}

const linesToList = (value) => (value || "").split("\n").map((s) => s.trim()).filter(Boolean);
const listToLines = (value) => (Array.isArray(value) ? value.join("\n") : value || "");

function scoreClass(score) {
  if (score >= 70) return "good";
  if (score >= 45) return "mid";
  return "low";
}

function relativeDate(iso) {
  if (!iso) return "";
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  if (Number.isNaN(days)) return "";
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  return `${Math.floor(days / 30)}mo ago`;
}

async function withBusy(button, fn) {
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `<span class="spinner"></span>${original}`;
  try {
    return await fn();
  } finally {
    button.disabled = false;
    button.innerHTML = original;
  }
}

/* ------------------------------------------------------------------ status */

async function loadStatus() {
  state.status = await api("/api/status");
  const pill = document.getElementById("llm-status");
  const { available, model } = state.status.llm;
  pill.textContent = available ? `Claude ready · ${model}` : "No API key — generation off";
  pill.className = `pill ${available ? "pill-good" : "pill-muted"}`;
  pill.title = available
    ? "Resume tailoring, fit review, and interview prep are enabled."
    : "Set ANTHROPIC_API_KEY in your .env file to enable resume tailoring and interview prep.";

  const counts = state.status.counts || {};
  document.getElementById("count-jobs").textContent = counts.jobs ?? "";
  document.getElementById("count-apps").textContent = counts.applications ?? "";
  document.getElementById("count-companies").textContent = counts.companies ?? "";
}

/* -------------------------------------------------------------------- jobs */

async function loadJobs() {
  const params = new URLSearchParams({
    min_score: document.getElementById("min-score").value,
    q: document.getElementById("job-search").value,
    only_untracked: document.getElementById("only-untracked").checked,
    limit: "300",
  });
  state.jobs = await api(`/api/jobs?${params}`);
  renderJobs();
}

function renderJobs() {
  const container = document.getElementById("jobs-list");
  if (!state.jobs.length) {
    container.innerHTML = `<div class="empty">
      <p>No jobs match.</p>
      <p class="hint">Add company boards on the Companies tab, then press <strong>Sync boards</strong>.
      If you just changed your profile, press Re-score or lower the minimum score.</p>
    </div>`;
    return;
  }

  container.innerHTML = state.jobs.map((job) => `
    <article class="card" data-job="${job.id}">
      <div class="score ${scoreClass(job.score || 0)}">${Math.round(job.score || 0)}</div>
      <div class="card-main">
        <div class="card-title">${escapeHtml(job.title)}</div>
        <div class="card-meta">
          <span>${escapeHtml(job.company)}</span>
          ${job.location ? `<span>· ${escapeHtml(job.location)}</span>` : ""}
          ${job.remote ? `<span class="pill">remote</span>` : ""}
          ${job.posted_at ? `<span>· ${relativeDate(job.posted_at)}</span>` : ""}
          ${job.application_status
            ? `<span class="pill pill-good">${escapeHtml(job.application_status)}</span>` : ""}
        </div>
      </div>
      <div class="card-actions">
        ${job.application_id ? "" :
          `<button class="btn btn-sm" data-track="${job.id}">Track</button>`}
        <button class="btn btn-sm btn-ghost" data-hide="${job.id}" title="Hide this job">✕</button>
      </div>
    </article>`).join("");
}

document.getElementById("jobs-list").addEventListener("click", async (event) => {
  const hideBtn = event.target.closest("[data-hide]");
  if (hideBtn) {
    event.stopPropagation();
    await api(`/api/jobs/${hideBtn.dataset.hide}/hide`, { method: "POST", body: { hidden: true } });
    await Promise.all([loadJobs(), loadStatus()]);
    return;
  }
  const trackBtn = event.target.closest("[data-track]");
  if (trackBtn) {
    event.stopPropagation();
    await api(`/api/jobs/${trackBtn.dataset.track}/track`, { method: "POST", body: { status: "saved" } });
    toast("Tracked — see the Pipeline tab");
    await Promise.all([loadJobs(), loadApplications(), loadStatus()]);
    return;
  }
  const card = event.target.closest("[data-job]");
  if (card) openDrawer(Number(card.dataset.job));
});

/* ------------------------------------------------------------------ drawer */

async function openDrawer(jobId) {
  state.openJobId = jobId;
  document.getElementById("drawer").hidden = false;
  document.getElementById("scrim").hidden = false;
  document.getElementById("drawer-body").innerHTML = `<p class="hint"><span class="spinner"></span>Loading…</p>`;
  await refreshDrawer();
}

function closeDrawer() {
  state.openJobId = null;
  document.getElementById("drawer").hidden = true;
  document.getElementById("scrim").hidden = true;
}

document.getElementById("drawer-close").addEventListener("click", closeDrawer);
document.getElementById("scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.openJobId) closeDrawer();
});

async function refreshDrawer() {
  const job = await api(`/api/jobs/${state.openJobId}`);
  const detail = job.score_detail || {};
  const llmReady = state.status?.llm?.available;

  document.getElementById("drawer-title").innerHTML = `
    <div class="card-title">${escapeHtml(job.title)}</div>
    <div class="card-meta">
      <span>${escapeHtml(job.company)}</span>
      ${job.location ? `<span>· ${escapeHtml(job.location)}</span>` : ""}
      ${job.remote ? `<span class="pill">remote</span>` : ""}
      <span class="score ${scoreClass(job.score || 0)}" style="width:auto;height:auto;padding:.05rem .5rem;border-radius:999px;font-size:.75rem">
        ${Math.round(job.score || 0)}/100</span>
    </div>`;

  const hasResume = job.documents.some((doc) => doc.kind === "resume");

  const generators = [
    ["review", "Fit review"],
    ["resume", "Tailor resume"],
    ["cover_letter", "Cover letter"],
    ["interview_prep", "Interview prep"],
    ["onboarding", "First 90 days"],
  ];

  document.getElementById("drawer-body").innerHTML = `
    <div class="row">
      <a class="btn btn-primary" href="${escapeHtml(job.url)}" target="_blank" rel="noopener">Open posting ↗</a>
      ${job.application
        ? `<span class="pill pill-good">tracked · ${escapeHtml(job.application.status)}</span>`
        : `<button class="btn" id="drawer-track">Track this job</button>`}
    </div>

    <h4>Why this score</h4>
    <ul class="reasons">${(detail.reasons || []).map((r) => `<li>${escapeHtml(r)}</li>`).join("") || "<li>Not scored yet</li>"}</ul>
    ${detail.missing_skills?.length
      ? `<p class="hint"><strong>Not evidenced in the posting:</strong> ${escapeHtml(detail.missing_skills.join(", "))}</p>` : ""}

    <h4>Generate ${llmReady ? "" : "(needs an API key)"}</h4>
    <div class="actions-grid">
      ${generators.map(([kind, label]) =>
        `<button class="btn" data-generate="${kind}" ${llmReady ? "" : "disabled"}>${label}</button>`).join("")}
    </div>
    <p class="hint" style="margin-top:.5rem">Generation takes 30-90 seconds and uses your profile as the
      only source of facts. Review before you send anything.</p>

    <h4>Documents</h4>
    <div id="drawer-docs">${job.documents.length ? "" : `<p class="hint">Nothing generated yet.</p>`}</div>

    <h4>Apply</h4>
    ${hasResume
      ? `<div class="row">
           <button class="btn" id="drawer-fill">Fill the form for me</button>
           <a class="btn btn-ghost" href="${escapeHtml(job.apply_url || job.url)}"
              target="_blank" rel="noopener">Open the form ↗</a>
         </div>
         <p class="hint">Opens the application in a browser, fills what your profile answers,
           uploads the tailored resume as a PDF, and stops at the submit button.
           ${state.status?.autofill_available ? "" :
             "Needs Playwright: <code>pip install playwright &amp;&amp; playwright install chromium</code>."}</p>`
      : `<p class="hint">Generate a tailored resume first — that's what gets uploaded.</p>`}
    <div id="drawer-attempts"></div>

    <h4>Job description</h4>
    <div class="jd">${escapeHtml(job.description || "(no description)")}</div>`;

  renderAttempts(job.id);

  const docsContainer = document.getElementById("drawer-docs");
  for (const doc of job.documents) {
    const element = document.createElement("div");
    element.className = "doc";
    element.innerHTML = `
      <div class="doc-head">
        <strong>${escapeHtml(doc.kind.replace(/_/g, " "))}</strong>
        <span>
          <span class="hint">${relativeDate(doc.created_at)}</span>
          <a class="btn btn-sm" href="/api/documents/${doc.id}/download">Download</a>
          <button class="btn btn-sm btn-ghost" data-doc-toggle="${doc.id}">View</button>
        </span>
      </div>
      <div class="md" id="doc-body-${doc.id}" hidden></div>`;
    docsContainer.appendChild(element);
  }

  document.getElementById("drawer-track")?.addEventListener("click", async (event) => {
    await withBusy(event.currentTarget, async () => {
      await api(`/api/jobs/${job.id}/track`, { method: "POST", body: { status: "saved" } });
    });
    await Promise.all([refreshDrawer(), loadJobs(), loadApplications(), loadStatus()]);
  });

  document.querySelectorAll("[data-generate]").forEach((button) => {
    button.addEventListener("click", async () => {
      const kind = button.dataset.generate;
      try {
        await withBusy(button, () => api(`/api/jobs/${job.id}/generate/${kind}`, { method: "POST" }));
        toast(`${kind.replace(/_/g, " ")} ready`);
        await Promise.all([refreshDrawer(), loadJobs(), loadApplications(), loadStatus()]);
      } catch (error) {
        toast(error.message, true);
      }
    });
  });

  document.getElementById("drawer-fill")?.addEventListener("click", async (event) => {
    try {
      const attempt = await withBusy(event.currentTarget, () =>
        api(`/api/jobs/${job.id}/apply`, { method: "POST", body: {} }));
      toast(attempt.detail, attempt.status === "failed");
      await Promise.all([refreshDrawer(), loadApplications(), loadStatus()]);
    } catch (error) {
      toast(error.message, true);
    }
  });

  document.querySelectorAll("[data-doc-toggle]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.dataset.docToggle;
      const body = document.getElementById(`doc-body-${id}`);
      if (!body.hidden) { body.hidden = true; button.textContent = "View"; return; }
      if (!body.dataset.loaded) {
        const doc = await api(`/api/documents/${id}`);
        body.innerHTML = renderMarkdown(doc.content);
        body.dataset.loaded = "1";
      }
      body.hidden = false;
      button.textContent = "Hide";
    });
  });
}

/** Past attempts at this job's form. The unfilled list is the point: it's the
 *  checklist of what still needs a human before the form can be sent. */
async function renderAttempts(jobId) {
  const container = document.getElementById("drawer-attempts");
  if (!container) return;
  const attempts = await api(`/api/jobs/${jobId}/attempts`);
  if (!attempts.length) { container.innerHTML = ""; return; }

  container.innerHTML = attempts.map((attempt) => `
    <div class="attempt">
      <div class="doc-head">
        <strong>${escapeHtml(attempt.status.replace(/_/g, " "))}</strong>
        <span class="hint">${escapeHtml(attempt.method)} · ${relativeDate(attempt.created_at)}</span>
      </div>
      <div class="hint">${escapeHtml(attempt.detail)}</div>
      ${attempt.filled.length ? `<details><summary>Filled ${attempt.filled.length}</summary>
        <ul class="reasons">${attempt.filled.map((f) =>
          `<li>${escapeHtml(f.label)} → ${escapeHtml(f.value)}</li>`).join("")}</ul></details>` : ""}
      ${attempt.unfilled.length ? `<details open><summary>Left for you ${attempt.unfilled.length}</summary>
        <ul class="reasons">${attempt.unfilled.map((f) =>
          `<li>${f.required ? "<strong>required</strong> · " : ""}${escapeHtml(f.label)} —
            ${escapeHtml(f.reason)}</li>`).join("")}</ul></details>` : ""}
      ${attempt.screenshot ? `<a class="btn btn-sm" href="/api/attempts/${attempt.id}/screenshot"
        target="_blank" rel="noopener">View the form ↗</a>` : ""}
    </div>`).join("");
}

/* ---------------------------------------------------------------- pipeline */

async function loadApplications() {
  state.applications = await api("/api/applications");
  renderPipeline();
}

function renderPipeline() {
  const board = document.getElementById("pipeline-board");
  if (!state.applications.length) {
    board.innerHTML = `<div class="empty"><p>Nothing tracked yet.</p>
      <p class="hint">Press <strong>Track</strong> on any job to start following it here.</p></div>`;
    return;
  }

  const statuses = state.status?.statuses || [];
  board.innerHTML = statuses.map((status) => {
    const rows = state.applications.filter((a) => a.status === status);
    if (!rows.length) return "";
    return `
      <section>
        <div class="stage-head">${escapeHtml(status)} <span class="count">${rows.length}</span></div>
        <div class="list">
          ${rows.map((app) => `
            <div class="app-row">
              <div class="card-main" data-job="${app.job_id}">
                <div class="card-title">${escapeHtml(app.title)}</div>
                <div class="card-meta">
                  <span>${escapeHtml(app.company)}</span>
                  ${app.location ? `<span>· ${escapeHtml(app.location)}</span>` : ""}
                  ${app.applied_at ? `<span>· applied ${relativeDate(app.applied_at)}</span>` : ""}
                </div>
              </div>
              <select data-status-for="${app.id}">
                ${statuses.map((s) =>
                  `<option ${s === app.status ? "selected" : ""}>${s}</option>`).join("")}
              </select>
              <a class="btn btn-sm" href="${escapeHtml(app.url)}" target="_blank" rel="noopener">Open ↗</a>
              <button class="btn btn-sm btn-ghost" data-untrack="${app.id}" title="Stop tracking">✕</button>
            </div>`).join("")}
        </div>
      </section>`;
  }).join("");
}

document.getElementById("pipeline-board").addEventListener("change", async (event) => {
  const select = event.target.closest("[data-status-for]");
  if (!select) return;
  await api(`/api/applications/${select.dataset.statusFor}`, {
    method: "PATCH",
    body: { status: select.value },
  });
  toast(`Moved to ${select.value}`);
  await Promise.all([loadApplications(), loadJobs()]);
});

document.getElementById("pipeline-board").addEventListener("click", async (event) => {
  const untrack = event.target.closest("[data-untrack]");
  if (untrack) {
    await api(`/api/applications/${untrack.dataset.untrack}`, { method: "DELETE" });
    await Promise.all([loadApplications(), loadJobs(), loadStatus()]);
    return;
  }
  const main = event.target.closest("[data-job]");
  if (main) openDrawer(Number(main.dataset.job));
});

/* ----------------------------------------------------------------- profile */

function experienceRow(entry = {}) {
  const wrapper = document.createElement("div");
  wrapper.className = "exp-row";
  wrapper.innerHTML = `
    <div class="exp-head">
      <strong class="hint">Role</strong>
      <button type="button" class="btn btn-sm btn-ghost" data-remove-exp>Remove</button>
    </div>
    <div class="grid-2">
      <label>Company<input data-f="company" value="${escapeHtml(entry.company || "")}" /></label>
      <label>Title<input data-f="title" value="${escapeHtml(entry.title || "")}" /></label>
      <label>Start<input data-f="start" placeholder="2021-03" value="${escapeHtml(entry.start || "")}" /></label>
      <label>End<input data-f="end" placeholder="Present" value="${escapeHtml(entry.end || "")}" /></label>
    </div>
    <label>Achievements <span class="hint-inline">one per line — the raw material for tailoring</span>
      <textarea data-f="bullets" rows="4">${escapeHtml(listToLines(entry.bullets))}</textarea>
    </label>`;
  wrapper.querySelector("[data-remove-exp]").addEventListener("click", () => wrapper.remove());
  return wrapper;
}

function fillProfileForm(profile) {
  const form = document.getElementById("profile-form");
  const listFields = ["links", "skills", "target_titles", "target_locations", "exclude_keywords",
    "certifications"];
  for (const field of ["name", "email", "phone", "location", "headline", "summary", "seniority"]) {
    if (form.elements[field]) form.elements[field].value = profile[field] || "";
  }
  for (const field of listFields) {
    if (form.elements[field]) form.elements[field].value = listToLines(profile[field]);
  }
  form.elements.min_salary.value = profile.min_salary ?? "";
  form.elements.remote_ok.checked = profile.remote_ok !== false;
  form.elements.education.value = (profile.education || [])
    .map((e) => [e.school, e.degree, e.year].filter(Boolean).join(" — "))
    .join("\n");

  const rows = document.getElementById("experience-rows");
  rows.innerHTML = "";
  (profile.experience || []).forEach((entry) => rows.appendChild(experienceRow(entry)));
  if (!rows.children.length) rows.appendChild(experienceRow());
}

function readProfileForm() {
  const form = document.getElementById("profile-form");
  const value = (name) => form.elements[name]?.value.trim() || "";

  const experience = [...document.querySelectorAll(".exp-row")].map((row) => {
    const field = (name) => row.querySelector(`[data-f="${name}"]`).value.trim();
    return {
      company: field("company"),
      title: field("title"),
      start: field("start"),
      end: field("end"),
      bullets: linesToList(field("bullets")),
    };
  }).filter((entry) => entry.company || entry.title);

  const education = linesToList(value("education")).map((line) => {
    const [school = "", degree = "", year = ""] = line.split("—").map((s) => s.trim());
    return { school, degree, year };
  });

  return {
    name: value("name"),
    email: value("email"),
    phone: value("phone"),
    location: value("location"),
    headline: value("headline"),
    summary: value("summary"),
    seniority: value("seniority"),
    links: linesToList(value("links")),
    skills: linesToList(value("skills")),
    target_titles: linesToList(value("target_titles")),
    target_locations: linesToList(value("target_locations")),
    exclude_keywords: linesToList(value("exclude_keywords")),
    certifications: linesToList(value("certifications")),
    min_salary: value("min_salary") ? Number(value("min_salary")) : null,
    remote_ok: form.elements.remote_ok.checked,
    experience,
    education,
  };
}

async function loadProfile() {
  state.profile = await api("/api/profile");
  fillProfileForm(state.profile);
}

document.getElementById("add-experience").addEventListener("click", () => {
  document.getElementById("experience-rows").appendChild(experienceRow());
});

document.getElementById("profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter || event.target.querySelector("[type=submit]");
  try {
    await withBusy(button, async () => {
      state.profile = await api("/api/profile", { method: "PUT", body: readProfileForm() });
      await api("/api/rescore", { method: "POST" });
    });
    document.getElementById("profile-saved").textContent = "Saved · jobs re-scored";
    setTimeout(() => { document.getElementById("profile-saved").textContent = ""; }, 4000);
    await Promise.all([loadJobs(), loadStatus()]);
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("parse-resume-btn").addEventListener("click", async (event) => {
  const text = document.getElementById("resume-paste").value;
  if (text.trim().length < 50) return toast("Paste your resume text first", true);
  try {
    const parsed = await withBusy(event.currentTarget, () =>
      api("/api/profile/parse-resume", { method: "POST", body: { text } }));
    // Keep the search preferences already on screen; only overwrite the history.
    const current = readProfileForm();
    fillProfileForm({
      ...parsed,
      target_titles: current.target_titles.length ? current.target_titles : [parsed.headline].filter(Boolean),
      target_locations: current.target_locations,
      exclude_keywords: current.exclude_keywords,
      remote_ok: current.remote_ok,
      min_salary: current.min_salary,
    });
    toast("Extracted — review the fields, then Save profile");
  } catch (error) {
    toast(error.message, true);
  }
});

/* --------------------------------------------------------------- companies */

async function loadCompanies() {
  state.companies = await api("/api/companies");
  const list = document.getElementById("companies-list");
  if (!state.companies.length) {
    list.innerHTML = `<div class="empty"><p>No boards tracked yet.</p>
      <p class="hint">Paste a job URL above, or pick from the suggestions.</p></div>`;
    return;
  }
  list.innerHTML = state.companies.map((company) => `
    <article class="card" style="cursor:default">
      <div class="card-main">
        <div class="card-title">${escapeHtml(company.name)}</div>
        <div class="card-meta">
          <span class="pill pill-muted">${escapeHtml(company.ats)}</span>
          <span>${escapeHtml(company.slug)}</span>
          ${company.last_synced ? `<span>· synced ${relativeDate(company.last_synced)}</span>` : ""}
          ${company.last_error ? `<span class="pill pill-bad" title="${escapeHtml(company.last_error)}">error</span>` : ""}
        </div>
      </div>
      <div class="card-actions">
        <label class="checkbox"><input type="checkbox" data-toggle="${company.id}"
          ${company.enabled ? "checked" : ""} /> enabled</label>
        <button class="btn btn-sm btn-ghost" data-del-company="${company.id}">✕</button>
      </div>
    </article>`).join("");
}

document.getElementById("companies-list").addEventListener("click", async (event) => {
  const del = event.target.closest("[data-del-company]");
  if (del) {
    await api(`/api/companies/${del.dataset.delCompany}`, { method: "DELETE" });
    await Promise.all([loadCompanies(), loadStatus()]);
  }
});

document.getElementById("companies-list").addEventListener("change", async (event) => {
  const toggle = event.target.closest("[data-toggle]");
  if (toggle) {
    await api(`/api/companies/${toggle.dataset.toggle}`, {
      method: "PATCH",
      body: { enabled: toggle.checked },
    });
    await Promise.all([loadCompanies(), loadStatus()]);
  }
});

document.getElementById("detect-btn").addEventListener("click", async (event) => {
  const url = document.getElementById("detect-url").value;
  try {
    const detected = await withBusy(event.currentTarget, () =>
      api("/api/companies/detect", { method: "POST", body: { url } }));
    document.getElementById("company-name").value = detected.name;
    document.getElementById("company-ats").value = detected.ats;
    document.getElementById("company-slug").value = detected.slug;
    toast(`Found ${detected.ats} board "${detected.slug}"`);
  } catch (error) {
    toast(error.message, true);
  }
});

async function addCompany(payload, button) {
  try {
    if (button) await withBusy(button, () => api("/api/companies", { method: "POST", body: payload }));
    else await api("/api/companies", { method: "POST", body: payload });
    toast(`Added ${payload.name}`);
    ["company-name", "company-slug", "detect-url"].forEach((id) => {
      document.getElementById(id).value = "";
    });
    await Promise.all([loadCompanies(), loadStatus()]);
  } catch (error) {
    toast(error.message, true);
  }
}

document.getElementById("add-company-btn").addEventListener("click", (event) => {
  addCompany({
    name: document.getElementById("company-name").value,
    ats: document.getElementById("company-ats").value,
    slug: document.getElementById("company-slug").value,
  }, event.currentTarget);
});

async function loadSuggestions() {
  const suggestions = await api("/api/companies/suggestions");
  const container = document.getElementById("suggestions");
  container.innerHTML = suggestions.map((s, index) =>
    `<button class="btn btn-sm" data-suggestion="${index}">${escapeHtml(s.name)}
      <span class="hint-inline">${escapeHtml(s.ats)}</span></button>`).join("");
  container.addEventListener("click", (event) => {
    const button = event.target.closest("[data-suggestion]");
    if (button) addCompany(suggestions[Number(button.dataset.suggestion)], button);
  });
}

/* ------------------------------------------------------------------- sync */

document.getElementById("sync-btn").addEventListener("click", async (event) => {
  try {
    const result = await withBusy(event.currentTarget, () => api("/api/sync", { method: "POST" }));
    if (result.message) {
      toast(result.message, true);
    } else {
      const failed = (result.results || []).filter((r) => !r.ok);
      toast(`${result.added} new, ${result.updated} updated` +
        (failed.length ? ` · ${failed.length} board(s) failed: ${failed.map((f) => f.company).join(", ")}` : ""),
        failed.length > 0);
    }
    await Promise.all([loadJobs(), loadCompanies(), loadStatus()]);
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("rescore-btn").addEventListener("click", async (event) => {
  await withBusy(event.currentTarget, () => api("/api/rescore", { method: "POST" }));
  toast("Re-scored against your current profile");
  await loadJobs();
});

/* --------------------------------------------------------------- linkedin */

document.getElementById("linkedin-import-btn").addEventListener("click", async (event) => {
  const text = document.getElementById("linkedin-paste").value;
  try {
    const result = await withBusy(event.currentTarget, () =>
      api("/api/linkedin/import", { method: "POST", body: { text } }));
    const parsed = result.parsed || {};
    const gaps = [];
    if (parsed.missing_company) gaps.push(`${parsed.missing_company} without a company`);
    if (parsed.missing_title) gaps.push(`${parsed.missing_title} without a title`);
    toast(result.message + (gaps.length ? ` · ${gaps.join(", ")} — edit or hide those.` : ""));
    document.getElementById("linkedin-paste").value = "";
    await Promise.all([loadJobs(), loadStatus()]);
  } catch (error) {
    toast(error.message, true);
  }
});

/* ------------------------------------------------------------------ agent */

const AGENT_NUMBERS = ["interval_minutes", "min_score", "review_min_score",
  "max_reviews_per_run", "max_documents_per_run", "max_applications_per_run",
  "max_applications_per_day", "followup_days"];
const AGENT_FLAGS = ["enabled", "sync_boards", "dry_run", "auto_apply", "auto_submit", "headless"];

let agentPollTimer = null;

async function loadAgent() {
  const payload = await api("/api/agent/settings");
  state.agent = payload;
  const form = document.getElementById("agent-form");

  AGENT_FLAGS.forEach((key) => { form.elements[key].checked = !!payload.settings[key]; });
  AGENT_NUMBERS.forEach((key) => { form.elements[key].value = payload.settings[key]; });
  form.elements.company_blocklist.value = listToLines(payload.settings.company_blocklist);

  document.getElementById("agent-feeds").innerHTML = (payload.feed_choices || []).map((feed) => `
    <label class="chip-check"><input type="checkbox" data-feed="${escapeHtml(feed)}"
      ${payload.settings.feeds.includes(feed) ? "checked" : ""} /> ${escapeHtml(feed)}</label>`).join("");

  document.getElementById("agent-summary").textContent = payload.summary;
  document.getElementById("autofill-note").textContent = payload.autofill_available
    ? ""
    : "Needs Playwright: pip install playwright && playwright install chromium";
  form.elements.auto_apply.disabled = !payload.autofill_available;

  applyAgentFormRules();
  renderAgentScheduler(payload.scheduler);
  await loadAgentRuns();
}

/** The three levels of autonomy nest, so the UI enforces the nesting visibly. */
function applyAgentFormRules() {
  const form = document.getElementById("agent-form");
  const dry = form.elements.dry_run.checked;
  const autoApply = form.elements.auto_apply.checked;
  form.elements.auto_apply.disabled = dry || !state.agent?.autofill_available;
  form.elements.auto_submit.disabled = dry || !autoApply;
  form.elements.headless.disabled = dry || !autoApply;
  if (form.elements.auto_apply.disabled) form.elements.auto_apply.checked = false;
  if (form.elements.auto_submit.disabled) form.elements.auto_submit.checked = false;
}

function renderAgentScheduler(scheduler) {
  const running = scheduler.running;
  document.getElementById("agent-run-btn").hidden = running;
  document.getElementById("agent-stop-btn").hidden = !running;
  document.getElementById("count-agent").textContent = running ? "•" : "";

  const parts = [];
  if (running) parts.push("A run is in progress.");
  if (scheduler.enabled && scheduler.next_run_at) {
    const next = new Date(scheduler.next_run_at);
    parts.push(next <= new Date()
      ? "Scheduled: due now."
      : `Scheduled: next run ${next.toLocaleString()}.`);
  } else if (!scheduler.enabled) {
    parts.push("Scheduling is off — use Run now, or `python -m app agent` from cron.");
  }
  if (scheduler.last_run_at) parts.push(`Last run ${relativeDate(scheduler.last_run_at)}.`);
  document.getElementById("agent-schedule").textContent = parts.join(" ");

  // Poll only while something is actually happening.
  clearInterval(agentPollTimer);
  if (running) agentPollTimer = setInterval(refreshAgentProgress, 3000);
}

async function refreshAgentProgress() {
  const payload = await api("/api/agent/runs?limit=10");
  renderAgentRuns(payload.runs);
  renderAgentScheduler(payload.scheduler);
  if (!payload.scheduler.running) {
    await Promise.all([loadJobs(), loadApplications(), loadStatus()]);
  }
}

async function loadAgentRuns() {
  const payload = await api("/api/agent/runs?limit=10");
  renderAgentRuns(payload.runs);
}

const STAGE_TONE = { ok: "good", planned: "mid", needs_review: "mid", skip: "", error: "low",
  failed: "low" };

function renderAgentRuns(runs) {
  const container = document.getElementById("agent-runs");
  if (!runs.length) {
    container.innerHTML = `<div class="empty"><p>No runs yet.</p>
      <p class="hint">Press <strong>Run now</strong>. In dry-run mode it costs nothing and shows
      you exactly which jobs it would go after.</p></div>`;
    return;
  }

  container.innerHTML = runs.map((run) => {
    const stats = run.stats || {};
    const summary = ["scanned", "shortlisted", "reviewed", "tailored", "applied", "submitted",
      "followups", "errors"]
      .filter((key) => stats[key]).map((key) => `${stats[key]} ${key}`).join(" · ");
    return `
      <article class="run" data-run="${run.id}">
        <div class="run-head">
          <div>
            <strong>Run ${run.id}</strong>
            <span class="pill ${run.status === "error" ? "pill-bad" : "pill-muted"}">${escapeHtml(run.status)}</span>
            ${run.dry_run ? `<span class="pill">dry run</span>` : ""}
            <span class="hint">${escapeHtml(run.trigger)} · ${relativeDate(run.started_at)}</span>
          </div>
          <button class="btn btn-sm btn-ghost" data-run-toggle="${run.id}">Details</button>
        </div>
        <div class="hint">${escapeHtml(summary || "nothing to do")}</div>
        ${(stats.notes || []).map((note) => `<div class="hint">↳ ${escapeHtml(note)}</div>`).join("")}
        ${run.error ? `<div class="hint error">${escapeHtml(run.error)}</div>` : ""}
        <div class="run-actions" id="run-actions-${run.id}" hidden></div>
      </article>`;
  }).join("");
}

document.getElementById("agent-runs").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-run-toggle]");
  if (!button) return;
  const id = button.dataset.runToggle;
  const body = document.getElementById(`run-actions-${id}`);
  if (!body.hidden) { body.hidden = true; button.textContent = "Details"; return; }

  const run = await api(`/api/agent/runs/${id}`);
  body.innerHTML = run.actions.length
    ? run.actions.map((action) => `
        <div class="run-action">
          <span class="pill pill-${STAGE_TONE[action.decision] || "muted"}">${escapeHtml(action.stage)}</span>
          <div>
            ${action.title
              ? `<a href="${escapeHtml(action.url || "#")}" target="_blank" rel="noopener">
                   ${escapeHtml(action.title)}</a>
                 <span class="hint">${escapeHtml(action.company || "")}</span>` : ""}
            <div class="hint">${escapeHtml(action.detail)}</div>
          </div>
        </div>`).join("")
    : `<p class="hint">Nothing logged for this run.</p>`;
  body.hidden = false;
  button.textContent = "Hide";
});

document.getElementById("agent-form").addEventListener("change", applyAgentFormRules);

document.getElementById("agent-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const body = { company_blocklist: linesToList(form.elements.company_blocklist.value) };
  AGENT_FLAGS.forEach((key) => { body[key] = form.elements[key].checked; });
  AGENT_NUMBERS.forEach((key) => { body[key] = Number(form.elements[key].value); });
  body.feeds = [...document.querySelectorAll("#agent-feeds [data-feed]")]
    .filter((input) => input.checked).map((input) => input.dataset.feed);

  if (body.auto_submit) {
    const ok = confirm(
      "Auto-submit sends applications under your name without you seeing them first.\n\n" +
      `Up to ${body.max_applications_per_day} per day. Forms with unanswered required fields ` +
      "are always held back.\n\nTurn it on?");
    if (!ok) { form.elements.auto_submit.checked = false; return; }
    body.confirm_auto_submit = true;
  }

  try {
    await api("/api/agent/settings", { method: "PUT", body });
    document.getElementById("agent-saved").textContent = "Saved";
    setTimeout(() => { document.getElementById("agent-saved").textContent = ""; }, 2500);
    await loadAgent();
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("agent-run-btn").addEventListener("click", async (event) => {
  try {
    const result = await withBusy(event.currentTarget, () =>
      api("/api/agent/run", { method: "POST", body: {} }));
    toast(result.dry_run ? "Dry run started — nothing will be sent" : "Agent run started");
    await refreshAgentProgress();
  } catch (error) {
    toast(error.message, true);
  }
});

document.getElementById("agent-stop-btn").addEventListener("click", async (event) => {
  await withBusy(event.currentTarget, () => api("/api/agent/stop", { method: "POST" }));
  toast("Stopping after the current job");
});

/* ------------------------------------------------------------------- tabs */

document.getElementById("tabs").addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (!tab) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-active", t === tab));
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("is-active", view.id === `view-${tab.dataset.view}`);
  });
});

let searchTimer;
document.getElementById("job-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadJobs, 250);
});
document.getElementById("min-score").addEventListener("input", (event) => {
  document.getElementById("min-score-out").textContent = event.target.value;
});
document.getElementById("min-score").addEventListener("change", loadJobs);
document.getElementById("only-untracked").addEventListener("change", loadJobs);

/* ------------------------------------------------------------------- boot */

(async function init() {
  try {
    await loadStatus();
    document.getElementById("company-ats").innerHTML =
      (state.status.ats_choices || []).map((ats) => `<option>${ats}</option>`).join("");
    await Promise.all([loadProfile(), loadJobs(), loadApplications(), loadCompanies(),
      loadSuggestions(), loadAgent()]);
    if (!state.status.profile_ready) {
      document.querySelector('.tab[data-view="profile"]').click();
      toast("Start by filling in your profile — it drives matching and every document.");
    }
  } catch (error) {
    toast(error.message, true);
  }
})();
