/* Yogya console.
 *
 * Plain ES modules-free JavaScript: no framework, no bundler, no CDN. The page
 * is one HTML file, one CSS file and this, which is the whole reason it can be
 * deployed on any free tier without a build step — and the reason it still
 * loads on a slow rural connection.
 *
 * The interaction model mirrors the graph exactly. The server tells us which
 * gate the session is waiting on; we render a card for that gate; the human
 * answers; we POST the answer and get the next gate back. There is no
 * client-side state machine to drift out of sync with the server, because the
 * client holds no state beyond the thread id.
 */

const $ = (id) => document.getElementById(id);

const SAMPLE_NOTES = `Sunita Devi, umar 44, Sitapur district, gaon mein rehti hai. Pati ka dehant
2019 mein ho gaya tha. Khet hai thoda sa, lagbhag aadha hectare, usi par kaam
karti hai aur dusron ke khet mein mazdoori bhi karti hai.

Ghar mein: sasur Ram Kumar, umar 68, ek pair se viklang hain (certificate nahi
hai). Beti Anjali 16 saal, class 10 mein padh rahi hai. Beta Rohit 9 saal,
primary school. Choti beti Meena 7 saal.

Antyodaya (peela) ration card hai. Bank account hai SBI mein. Gas connection
nahi hai, lakdi par khana banta hai. Kaccha ghar hai. Saal ka kamai lagbhag
48,000 rupaye. Income tax nahi bharti.

Aadhaar sabka hai, ration card hai, bank passbook hai, foto bhi hai, aur
bachchon ka janm praman patra hai. Income certificate, jaati praman patra,
viklangta praman patra, pati ka mrityu praman patra — inme se koi nahi hai.`;

let threadId = null;
let activeVerdict = 'eligible';
let lastState = null;

/* ── plumbing ─────────────────────────────────────────────── */

async function api(path, options) {
  const response = await fetch(path, {
    headers: { 'content-type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}

const rupees = (n) =>
  n === null || n === undefined ? '—' : '₹' + Number(n).toLocaleString('en-IN');

const titleCase = (s) =>
  String(s || '').replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ── health ───────────────────────────────────────────────── */

async function loadHealth() {
  try {
    const h = await api('/api/health');
    $('health').textContent =
      `${h.schemes} schemes · ${h.corpus_snapshot}` +
      (h.llm_offline ? ' · offline mode' : ` · ${h.llm_model}`);
    $('corpus-note').textContent =
      `Corpus snapshot ${h.corpus_snapshot}. Rules change — decisions record the version used.`;
  } catch (err) {
    $('health').textContent = 'server unreachable';
  }
}

/* ── session ──────────────────────────────────────────────── */

async function startSession() {
  const householdId = $('household-id').value.trim();
  const notes = $('notes').value.trim();
  $('intake-error').hidden = true;

  if (!householdId || !notes) {
    $('intake-error').textContent = 'A household ID and some intake notes are needed.';
    $('intake-error').hidden = false;
    return;
  }

  $('start').disabled = true;
  $('start').innerHTML = '<span class="spinner">Screening…</span>';
  try {
    const state = await api('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({ household_id: householdId, intake_notes: notes }),
    });
    threadId = state.thread_id;
    render(state);
  } catch (err) {
    $('intake-error').textContent = err.message;
    $('intake-error').hidden = false;
  } finally {
    $('start').disabled = false;
    $('start').textContent = 'Screen this family';
  }
}

async function resume(decision) {
  const actions = $('gate-actions');
  actions.querySelectorAll('button').forEach((b) => (b.disabled = true));
  try {
    render(await api(`/api/sessions/${encodeURIComponent(threadId)}/resume`, {
      method: 'POST',
      body: JSON.stringify({ decision }),
    }));
  } catch (err) {
    $('intake-error').textContent = err.message;
    $('intake-error').hidden = false;
    actions.querySelectorAll('button').forEach((b) => (b.disabled = false));
  }
}

/* ── rendering ────────────────────────────────────────────── */

function render(state) {
  lastState = state;
  $('intake-panel').hidden = true;

  if (state.pending_interrupt) {
    renderGate(state.pending_interrupt);
  } else {
    $('gate-panel').hidden = true;
  }

  renderEligibility(state.eligibility || []);
  renderApplications(state.applications || []);
  renderSummary(state.summary);
  renderAudit(state.audit || []);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderGate(request) {
  const panel = $('gate-panel');
  panel.hidden = false;
  $('gate-kind').textContent = titleCase(request.kind);
  $('gate-title').textContent = request.title;
  $('gate-body').textContent = request.body;

  const content = $('gate-content');
  const actions = $('gate-actions');
  content.innerHTML = '';
  actions.innerHTML = '';

  ({
    confirm_profile: gateProfile,
    select_schemes: gateSelect,
    approve_submission: gateSubmission,
    approve_appeal: gateAppeal,
  }[request.kind] || (() => {}))(request, content, actions);
}

/* Gate 1 — confirm the extracted profile. */
function gateProfile(request, content, actions) {
  const household = request.payload.household || {};
  const unresolved = request.payload.unresolved_fields || [];
  const shown = [
    'head_name', 'state', 'district', 'residence', 'annual_income',
    'social_category', 'ration_card', 'land_holding_hectares',
    'owns_pucca_house', 'has_bank_account', 'has_lpg_connection',
  ];

  content.innerHTML = `
    <div class="fields">
      ${shown.map((key) => `
        <label class="field ${unresolved.includes(key) ? 'flagged' : ''}">
          <span>${titleCase(key)}</span>
          <input data-field="${key}" value="${escapeHtml(household[key] ?? '')}">
        </label>`).join('')}
    </div>
    <div class="item" style="margin-top:14px">
      <h3>${(household.members || []).length} members recorded</h3>
      <p>${(household.members || [])
        .map((m) => `${escapeHtml(m.name)}${m.age ? ', ' + m.age : ''}`)
        .join(' · ')}</p>
    </div>`;

  actions.innerHTML = '<button class="primary" id="confirm">Confirm and screen</button>';
  $('confirm').onclick = () => {
    const corrections = {};
    content.querySelectorAll('input[data-field]').forEach((input) => {
      const key = input.dataset.field;
      const before = household[key];
      let value = input.value.trim();
      if (value === String(before ?? '')) return;
      if (value === '') value = null;
      else if (/^-?\d+$/.test(value)) value = parseInt(value, 10);
      else if (/^-?\d*\.\d+$/.test(value)) value = parseFloat(value);
      else if (value === 'true' || value === 'false') value = value === 'true';
      corrections[key] = value;
    });
    resume({
      action: Object.keys(corrections).length ? 'amend' : 'confirm',
      corrections,
      note: '',
    });
  };
}

/* Gate 2 — choose what to file. */
function gateSelect(request, content, actions) {
  const eligible = request.payload.eligible || [];
  const needsInfo = request.payload.needs_info || [];

  content.innerHTML = `
    ${eligible.map((r) => `
      <label class="item pick">
        <input type="checkbox" data-file="${escapeHtml(r.scheme_id)}" checked>
        <div>
          <h3>${escapeHtml(r.scheme_name)}</h3>
          <p>${escapeHtml(r.explanation || '')}</p>
          ${r.estimated_annual_value_inr
            ? `<p class="money">${rupees(r.estimated_annual_value_inr)} per year</p>` : ''}
          <p style="margin-top:6px">
            <label style="display:inline;text-transform:none;letter-spacing:0;font-weight:400;color:var(--ink-soft)">
              <input type="checkbox" data-have="${escapeHtml(r.scheme_id)}"
                     style="width:14px;height:14px;vertical-align:-2px">
              Family already receives this
            </label>
          </p>
        </div>
      </label>`).join('')}
    ${needsInfo.length ? `<h3 style="font-size:13px;margin:18px 0 8px;color:var(--ink-soft)">
        Cannot be decided without more information</h3>` : ''}
    ${needsInfo.map((r) => `
      <div class="item">
        <h3>${escapeHtml(r.scheme_name)}<span class="pill warn">needs info</span></h3>
        <p>${escapeHtml(r.explanation || '')}</p>
      </div>`).join('')}`;

  actions.innerHTML = '<button class="primary" id="file">File selected applications</button>';
  $('file').onclick = () => {
    const already = [...content.querySelectorAll('input[data-have]:checked')]
      .map((i) => i.dataset.have);
    const chosen = [...content.querySelectorAll('input[data-file]:checked')]
      .map((i) => i.dataset.file)
      .filter((id) => !already.includes(id));
    resume({
      file_scheme_ids: chosen,
      already_receiving_scheme_ids: already,
      note: '',
    });
  };
}

/* Gate 3 — approve a submission. The one gate that cannot be turned off. */
function gateSubmission(request, content, actions) {
  const application = request.payload.application || {};
  const form = application.form_data || {};
  const gaps = application.document_gaps || [];

  content.innerHTML = `
    <div class="fields">
      ${Object.entries(form).map(([key, value]) => `
        <label class="field ${value === '<<NEEDS HUMAN>>' ? 'flagged' : ''}">
          <span>${titleCase(key)}</span>
          <input data-form="${escapeHtml(key)}"
                 value="${escapeHtml(value === '<<NEEDS HUMAN>>' ? '' : value)}"
                 placeholder="${value === '<<NEEDS HUMAN>>' ? 'needs you to fill this' : ''}">
        </label>`).join('')}
    </div>
    ${gaps.length ? `
      <h3 style="font-size:13px;margin:16px 0 8px;color:var(--ink-soft)">Missing documents</h3>
      ${gaps.map((g) => `
        <div class="item">
          <h3>${titleCase(g.document)}
            <span class="pill ${g.blocking ? 'bad' : 'warn'}">
              ${g.blocking ? 'blocking' : 'optional'}</span></h3>
          <p>${escapeHtml(g.where_to_get)}</p>
          <p class="money">${g.typical_days ? g.typical_days + ' days' : ''}${
            g.cost_inr ? ' · ' + rupees(g.cost_inr) : ''}</p>
        </div>`).join('')}` : ''}`;

  actions.innerHTML = `
    <button class="primary" id="submit">Submit to portal</button>
    <button id="hold">Hold — come back later</button>
    <button class="danger" id="skip">Skip this scheme</button>`;

  const edits = () => {
    const out = {};
    content.querySelectorAll('input[data-form]').forEach((input) => {
      const key = input.dataset.form;
      if (input.value.trim() && input.value !== form[key]) out[key] = input.value.trim();
    });
    return out;
  };
  $('submit').onclick = () => resume({ action: 'submit', form_edits: edits(), note: '' });
  $('hold').onclick = () => resume({ action: 'hold', form_edits: edits(), note: '' });
  $('skip').onclick = () => resume({ action: 'skip', form_edits: {}, note: '' });
}

/* Gate 4 — approve an appeal against a rejection. */
function gateAppeal(request, content, actions) {
  const application = request.payload.application || {};
  const appeal = request.payload.appeal || {};
  const rejection = application.rejection || {};

  content.innerHTML = `
    <div class="item">
      <h3>The department said
        <span class="pill bad">${escapeHtml(rejection.code || 'rejected')}</span></h3>
      <p>${escapeHtml(rejection.text || '')}</p>
    </div>
    <div class="item">
      <h3>Independent re-check of the rules
        ${rejection.assessed_as_wrongful
          ? '<span class="pill bad">contradicts the rejection</span>'
          : '<span class="pill">consistent</span>'}</h3>
      <p>${escapeHtml(rejection.assessment_note || '')}</p>
      <ul class="criteria">
        ${(appeal.citations || []).map((c) =>
          `<li class="pass">${escapeHtml(c)}</li>`).join('')}
      </ul>
    </div>
    <label for="letter">Draft appeal — edit before filing</label>
    <textarea id="letter" rows="14" class="letter">${escapeHtml(appeal.letter_text || '')}</textarea>`;

  actions.innerHTML = `
    <button class="primary" id="file-appeal">File this appeal</button>
    <button class="danger" id="abandon">Do not appeal</button>`;

  $('file-appeal').onclick = () => resume({
    action: 'edit_and_file',
    letter_text: $('letter').value,
    note: '',
  });
  $('abandon').onclick = () => resume({ action: 'abandon', letter_text: null, note: '' });
}

/* ── result panels ────────────────────────────────────────── */

function renderEligibility(results) {
  if (!results.length) { $('eligibility-panel').hidden = true; return; }
  $('eligibility-panel').hidden = false;

  const counts = { eligible: 0, needs_info: 0, not_eligible: 0 };
  results.forEach((r) => { counts[r.verdict] = (counts[r.verdict] || 0) + 1; });
  document.querySelectorAll('#elig-tabs .tab').forEach((tab) => {
    const verdict = tab.dataset.verdict;
    tab.textContent = `${titleCase(verdict)} (${counts[verdict] || 0})`;
    tab.classList.toggle('active', verdict === activeVerdict);
    tab.onclick = () => { activeVerdict = verdict; renderEligibility(results); };
  });

  const shown = results.filter((r) => r.verdict === activeVerdict);
  $('eligibility').innerHTML = shown.length
    ? shown.map((r) => `
      <div class="item">
        <h3>${escapeHtml(r.scheme_name)}
          <span class="pill">v${escapeHtml(r.rule_version)}</span></h3>
        <p>${escapeHtml(r.explanation || '')}</p>
        ${r.estimated_annual_value_inr
          ? `<p class="money">${rupees(r.estimated_annual_value_inr)} per year</p>` : ''}
        <ul class="criteria">
          ${(r.results || []).map((c) => `
            <li class="${c.unknown ? 'unknown' : c.passed ? 'pass' : 'fail'}">
              ${escapeHtml(c.description)}
              <span class="cite">${escapeHtml(c.citation)}</span>
            </li>`).join('')}
        </ul>
      </div>`).join('')
    : '<p class="hint">Nothing in this category.</p>';
}

function renderApplications(applications) {
  if (!applications.length) { $('applications-panel').hidden = true; return; }
  $('applications-panel').hidden = false;

  const tone = {
    approved: 'good', rejected: 'bad', escalated: 'warn',
    blocked_on_documents: 'warn', appealed: 'warn', abandoned: '',
  };

  $('applications').innerHTML = applications.map((a) => `
    <div class="item">
      <h3>${escapeHtml(a.scheme_name)}
        <span class="pill ${tone[a.status] || ''}">${titleCase(a.status)}</span>
        ${a.appeal_attempts ? `<span class="pill warn">${a.appeal_attempts} appeal(s)</span>` : ''}
      </h3>
      ${a.reference_number ? `<p>Reference ${escapeHtml(a.reference_number)}</p>` : ''}
      ${a.rejection ? `<p>Department said: ${escapeHtml(a.rejection.text)}${
        a.rejection.assessed_as_wrongful
          ? ' <strong>— re-check contradicts this</strong>' : ''}</p>` : ''}
      ${(a.document_gaps || []).filter((g) => g.blocking).length ? `
        <p>Waiting on: ${a.document_gaps.filter((g) => g.blocking)
          .map((g) => titleCase(g.document)).join(', ')}</p>` : ''}
    </div>`).join('');
}

function renderSummary(summary) {
  if (!summary) { $('summary-panel').hidden = true; return; }
  $('summary-panel').hidden = false;

  const cards = [
    ['benefits_discovered', 'Benefits discovered', true],
    ['rupees_recovered_annual', 'Cash per year recovered', true, rupees],
    ['insurance_cover_secured_inr', 'Insurance cover secured', false, rupees],
    ['applications_filed', 'Applications filed'],
    ['approved', 'Approved'],
    ['blocked_on_documents', 'Blocked on documents'],
    ['appeals_filed', 'Appeals filed'],
    ['wrongful_denials_recovered', 'Wrongful denials overturned', true],
    ['escalated', 'Escalated to grievance'],
    ['form_error_rate', 'Form fields needing a human',
      false, (v) => (v * 100).toFixed(0) + '%'],
    ['human_interrupts', 'Decisions you made'],
  ];

  $('metrics').innerHTML = cards.map(([key, label, hero, fmt]) => `
    <div class="metric ${hero ? 'hero' : ''}">
      <div class="v">${fmt ? fmt(summary[key] ?? 0) : (summary[key] ?? 0)}</div>
      <div class="k">${label}</div>
    </div>`).join('');
}

function renderAudit(entries) {
  if (!entries.length) { $('audit-panel').hidden = true; return; }
  $('audit-panel').hidden = false;
  $('audit').innerHTML = entries.map((e) => `
    <div class="line">
      <span class="actor">${escapeHtml(e.actor)}</span> — ${escapeHtml(e.action)}
      ${e.rule_version ? `<span class="pill">v${escapeHtml(e.rule_version)}</span>` : ''}
      ${e.detail ? `<span class="detail">${escapeHtml(e.detail)}</span>` : ''}
      ${(e.citations || []).map((c) =>
        `<span class="citation">├ cited: ${escapeHtml(c)}</span>`).join('')}
    </div>`).join('');
}

/* ── boot ─────────────────────────────────────────────────── */

$('start').onclick = startSession;
$('load-sample').onclick = () => { $('notes').value = SAMPLE_NOTES; };
$('notes').value = SAMPLE_NOTES;
loadHealth();
