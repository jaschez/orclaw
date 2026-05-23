// orclaw dashboard — vanilla JS, no framework.
// Each section fetches its own data on its own cadence.

const REFRESH = {
  status: 5_000,
  decision: 5_000,
  plan: 5_000,
  batches: 15_000,
  runs: 10_000,
  events: 10_000,
  config: 60_000,
  tuning: 10_000,
  quota: 30_000,
};

// --- helpers -----------------------------------------------------------------

async function jget(path) {
  const r = await fetch(path, { credentials: 'include' });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}
async function jpost(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : null,
  });
  if (!r.ok) {
    let detail = await r.text().catch(() => '');
    throw new Error(`${path} → ${r.status} ${detail.slice(0, 200)}`);
  }
  return r.json();
}
async function jput(path, body) {
  const r = await fetch(path, {
    method: 'PUT',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = await r.text().catch(() => '');
    throw new Error(`${path} → ${r.status} ${detail.slice(0, 200)}`);
  }
  return r.json();
}
function $(sel) { return document.querySelector(sel); }
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) {
    if (k == null) continue;
    e.appendChild(typeof k === 'string' ? document.createTextNode(k) : k);
  }
  return e;
}
function pill(value) {
  if (!value) return '—';
  return el('span', { class: `pill pill-${value}` }, value);
}
function fmtTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  return d.toISOString().slice(0, 19).replace('T', ' ');
}
function lastRefreshNote() {
  $('#last-refresh').textContent = `refreshed ${new Date().toLocaleTimeString()}`;
}

// --- STATUS ------------------------------------------------------------------

async function loadStatus() {
  try {
    const s = await jget('/api/status');
    $('#status-paused').textContent = s.paused ? 'PAUSED' : 'ACTIVE';
    $('#status-paused').style.color = s.paused ? 'var(--warn)' : 'var(--accent)';
    $('#status-repo').textContent = s.repo;
    $('#status-inflight').firstChild.textContent = s.in_flight;
    $('#status-cap').textContent = s.max_in_flight;

    const batchList = $('#status-batches');
    batchList.innerHTML = '';
    const statuses = ['pending', 'in_progress', 'merged', 'failed', 'skipped'];
    for (const st of statuses) {
      const n = s.batches[st] || 0;
      batchList.appendChild(
        el('div', { class: 'kv' },
          el('span', { class: 'kv-key' }, st),
          el('span', {}, String(n)),
        )
      );
    }
    lastRefreshNote();
  } catch (e) {
    console.error('status', e);
  }
}

// --- DECISION PREVIEW --------------------------------------------------------

async function loadDecision() {
  try {
    const d = await jget('/api/decision_preview');
    $('#decision-verdict').textContent = d.is_idle ? 'IDLE' : 'DISPATCH';
    $('#decision-verdict').style.color = d.is_idle ? 'var(--fg-muted)' : 'var(--accent)';
    $('#decision-issues').textContent = (d.issues_to_dispatch || []).length
      ? d.issues_to_dispatch.map(n => `#${n}`).join(' ')
      : '—';
    $('#decision-prs').textContent = (d.prs_to_review || []).length
      ? d.prs_to_review.map(n => `#${n}`).join(' ')
      : '—';
    $('#decision-reason').textContent = d.reason;
  } catch (e) {
    console.error('decision', e);
  }
}

// --- ORCHESTRATION PLAN (layered visualization) ------------------------------

async function loadPlan() {
  try {
    const p = await jget('/api/plan');

    // Concurrency bar + stats
    $('#conc-inflight').textContent = p.in_flight;
    $('#conc-cap').textContent = p.concurrency_cap;
    $('#conc-remaining').textContent = p.remaining_slots;
    $('#conc-active-layer').textContent = p.active_layer_index < 0 ? '—' : `L${p.active_layer_index}`;
    $('#conc-paused').textContent = p.paused ? 'yes ⏸' : 'no ▶';
    $('#conc-paused').style.color = p.paused ? 'var(--warn)' : 'var(--accent)';
    const pct = p.concurrency_cap > 0 ? Math.min(100, (p.in_flight / p.concurrency_cap) * 100) : 0;
    $('#conc-bar').style.width = pct + '%';
    $('#conc-bar').style.background = pct >= 100 ? 'var(--warn)' : 'var(--accent)';

    // Layered plan
    const container = $('#plan-layers');
    container.innerHTML = '';
    if (!p.layers.length) {
      container.appendChild(el('div', { class: 'plan-empty' }, '(no batches — run the planner)'));
      return;
    }

    const nextSet = new Set(p.next_dispatchable_issues || []);
    const repoUrlBase = `https://github.com/${p.repo}`;

    for (const layer of p.layers) {
      const layerEl = el('div', {
        class: 'plan-layer' +
          (layer.is_active_layer ? ' active' : '') +
          (layer.complete ? ' complete' : ''),
      });

      // Layer header — index + counts
      const countParts = [];
      for (const [key, n] of Object.entries(layer.counts)) {
        if (n > 0) countParts.push(`${key} ${n}`);
      }
      const head = el('div', { class: 'plan-layer-head' },
        el('span', { class: 'plan-layer-index' },
          `Layer ${layer.index}` + (layer.is_active_layer ? ' · active' : layer.complete ? ' · complete' : ' · waiting')
        ),
        el('span', { class: 'plan-layer-stats' },
          `${layer.live_issues}/${layer.total_issues} live`,
          countParts.length ? ' · ' : '',
          ...countParts.flatMap((p, i) => i === 0 ? [p] : [el('span', { class: 'sep' }, '·'), p]),
        ),
      );
      layerEl.appendChild(head);

      // Issue chips
      const issuesEl = el('div', { class: 'plan-issues' });
      for (const iss of layer.issues) {
        const isNext = nextSet.has(iss.number);
        const chip = el('a', {
          class: 'plan-issue' + (isNext ? ' next' : ''),
          'data-status': iss.status,
          href: `${repoUrlBase}/issues/${iss.number}`,
          target: '_blank',
          rel: 'noopener',
          title: `#${iss.number} · ${iss.status} · updated ${iss.updated_at}` +
            (iss.implementer_run_id ? ` · run ${iss.implementer_run_id.slice(0, 12)}` : '') +
            (iss.pr_number ? ` · PR #${iss.pr_number}` : ''),
        },
          el('span', { class: 'status-dot' }),
          `#${iss.number}`,
          iss.pr_number ? el('span', { class: 'pr-badge' }, `PR ${iss.pr_number}`) : null,
        );
        issuesEl.appendChild(chip);
      }
      layerEl.appendChild(issuesEl);
      container.appendChild(layerEl);
    }
  } catch (e) {
    console.error('plan', e);
  }
}

// --- BATCHES -----------------------------------------------------------------

async function loadBatches() {
  try {
    const filter = $('#batches-filter-status').value;
    const rows = await jget('/api/batches' + (filter ? `?status=${filter}` : ''));
    const tbody = $('#batches-table tbody');
    tbody.innerHTML = '';
    if (!rows.length) {
      tbody.appendChild(el('tr', {}, el('td', { colspan: 7, class: 'empty' }, '(none)')));
      return;
    }
    for (const r of rows) {
      tbody.appendChild(el('tr', {},
        el('td', {}, `L${r.layer}`),
        el('td', {}, `#${r.issue_number}`),
        el('td', {}, pill(r.status)),
        el('td', {}, r.implementer_run_id ? r.implementer_run_id.slice(0, 12) : '—'),
        el('td', {}, r.pr_number ? `#${r.pr_number}` : '—'),
        el('td', {}, fmtTs(r.updated_at)),
        el('td', {},
          el('a', { href: `https://github.com/${TARGET_REPO}/issues/${r.issue_number}`, target: '_blank', class: 'btn', style: 'font-size:10px;padding:2px 6px' }, '↗')
        ),
      ));
    }
  } catch (e) {
    console.error('batches', e);
  }
}

// --- RUNS --------------------------------------------------------------------

async function loadRuns() {
  try {
    const a = $('#runs-filter-agent').value;
    const s = $('#runs-filter-status').value;
    const qs = new URLSearchParams();
    if (a) qs.set('agent', a);
    if (s) qs.set('status', s);
    qs.set('limit', '100');
    const rows = await jget('/api/runs?' + qs.toString());
    const tbody = $('#runs-table tbody');
    tbody.innerHTML = '';
    if (!rows.length) {
      tbody.appendChild(el('tr', {}, el('td', { colspan: 7, class: 'empty' }, '(none)')));
      return;
    }
    for (const r of rows) {
      tbody.appendChild(el('tr', {},
        el('td', {}, r.id.slice(0, 12)),
        el('td', {}, r.agent),
        el('td', {}, pill(r.status)),
        el('td', {}, r.issue_number ? `#${r.issue_number}` : '—'),
        el('td', {}, r.pr_number ? `#${r.pr_number}` : '—'),
        el('td', {}, fmtTs(r.started_at)),
        el('td', {}, fmtTs(r.finished_at)),
      ));
    }
  } catch (e) {
    console.error('runs', e);
  }
}

// --- EVENTS ------------------------------------------------------------------

async function loadEvents() {
  try {
    const minutes = parseInt($('#events-filter-minutes').value || '60', 10);
    const level = $('#events-filter-level').value;
    const pattern = $('#events-filter-pattern').value;
    const limit = Math.max(1, Math.min(500, parseInt($('#events-filter-limit').value || '10', 10)));
    const qs = new URLSearchParams({ minutes: String(minutes), limit: String(limit) });
    if (level) qs.set('level', level);
    if (pattern) qs.set('pattern', pattern);
    const rows = await jget('/api/events?' + qs.toString());
    const tbody = $('#events-table tbody');
    tbody.innerHTML = '';
    if (!rows.length) {
      tbody.appendChild(el('tr', {}, el('td', { colspan: 4, class: 'empty' }, '(no events match)')));
      return;
    }
    for (const r of rows) {
      const attrsStr = Object.entries(r.attrs || {})
        .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
        .join(' ');
      tbody.appendChild(el('tr', {},
        el('td', {}, r.ts),
        el('td', {}, pill(r.level)),
        el('td', {}, r.event),
        el('td', {}, attrsStr),
      ));
    }
  } catch (e) {
    console.error('events', e);
  }
}

// --- CONTROLS ----------------------------------------------------------------

function setControlResult(msg, ok = true) {
  const e = $('#control-result');
  e.textContent = msg;
  e.style.color = ok ? 'var(--accent)' : 'var(--danger)';
  setTimeout(() => { e.textContent = ''; }, 8000);
}

async function controlAction(action) {
  try {
    let result;
    if (action === 'skip_issue') {
      const n = parseInt($('#skip-issue-num').value, 10);
      if (!n) return setControlResult('issue number required', false);
      result = await jpost(`/api/skip_issue/${n}`);
    } else if (action === 'force_review') {
      const n = parseInt($('#force-review-num').value, 10);
      if (!n) return setControlResult('PR number required', false);
      result = await jpost(`/api/force_review/${n}`);
    } else if (action === 'require_human_review') {
      const n = parseInt($('#human-review-num').value, 10);
      if (!n) return setControlResult('PR number required', false);
      result = await jpost(`/api/require_human_review/${n}`);
    } else if (action === 'force_tick') {
      result = await jpost('/api/force_tick');
    } else if (action === 'pause') {
      result = await jpost('/api/pause');
    } else if (action === 'resume') {
      result = await jpost('/api/resume');
    } else if (action === 'run_planner') {
      result = await jpost('/api/run_planner');
    }
    setControlResult(`${action}: ${JSON.stringify(result)}`);
    // refresh affected sections
    loadStatus(); loadDecision(); loadBatches(); loadRuns(); loadEvents();
  } catch (e) {
    setControlResult(String(e), false);
  }
}

document.querySelectorAll('[data-action]').forEach(btn => {
  btn.addEventListener('click', () => controlAction(btn.dataset.action));
});

// --- PROMPTS (overlay-backed) -----------------------------------------------

let currentPrompt = null;
let currentPromptIsOverridden = false;

async function jdelete(path) {
  const r = await fetch(path, { method: 'DELETE', credentials: 'include' });
  if (!r.ok) {
    let detail = await r.text().catch(() => '');
    throw new Error(`${path} → ${r.status} ${detail.slice(0, 200)}`);
  }
  return r.json();
}

async function loadPromptList() {
  try {
    const prompts = await jget('/api/prompts');
    const list = $('#prompts-list');
    list.innerHTML = '';
    for (const p of prompts) {
      const btn = el('button', { onclick: () => loadPrompt(p.name) });
      btn.dataset.name = p.name;
      btn.appendChild(document.createTextNode(p.name));
      if (p.is_overridden) {
        btn.appendChild(el('span', { class: 'overlay-badge' }, 'edited'));
      }
      list.appendChild(btn);
    }
  } catch (e) { console.error('prompts list', e); }
}

async function loadPrompt(name) {
  try {
    document.querySelectorAll('#prompts-list button').forEach(b => b.classList.toggle('active', b.dataset.name === name));
    const data = await jget(`/api/prompts/${encodeURIComponent(name)}`);
    currentPrompt = name;
    currentPromptIsOverridden = !!data.is_overridden;
    $('#prompt-current').innerHTML = name + (data.is_overridden
      ? ' <span class="overlay-badge inline">edited</span>'
      : ' <span class="muted small">(default)</span>');
    $('#prompt-content').value = data.content;
    $('#prompt-save').disabled = false;
    $('#prompt-revert').disabled = !data.is_overridden;
    $('#prompt-result').textContent = '';
  } catch (e) {
    $('#prompt-result').textContent = String(e);
    $('#prompt-result').style.color = 'var(--danger)';
  }
}

$('#prompt-save').addEventListener('click', async () => {
  if (!currentPrompt) return;
  const content = $('#prompt-content').value;
  $('#prompt-save').disabled = true;
  $('#prompt-result').textContent = 'saving…';
  $('#prompt-result').style.color = 'var(--fg-muted)';
  try {
    await jput(`/api/prompts/${encodeURIComponent(currentPrompt)}`, { content });
    $('#prompt-result').textContent = '✓ saved — takes effect on next orchestrator tick';
    $('#prompt-result').style.color = 'var(--accent)';
    await loadPromptList();
    await loadPrompt(currentPrompt);
  } catch (e) {
    $('#prompt-result').textContent = `❌ ${e}`;
    $('#prompt-result').style.color = 'var(--danger)';
  } finally {
    $('#prompt-save').disabled = false;
  }
});

$('#prompt-revert').addEventListener('click', async () => {
  if (!currentPrompt || !currentPromptIsOverridden) return;
  if (!confirm(`Revert ${currentPrompt} to the shipped default?`)) return;
  $('#prompt-revert').disabled = true;
  try {
    await jdelete(`/api/prompts/${encodeURIComponent(currentPrompt)}`);
    $('#prompt-result').textContent = '↩ reverted to default';
    $('#prompt-result').style.color = 'var(--accent)';
    await loadPromptList();
    await loadPrompt(currentPrompt);
  } catch (e) {
    $('#prompt-result').textContent = `❌ ${e}`;
    $('#prompt-result').style.color = 'var(--danger)';
    $('#prompt-revert').disabled = false;
  }
});

// --- TUNING: concurrency cap + quota gauge ----------------------------------

async function loadTuning() {
  try {
    const s = await jget('/api/status');
    $('#cap-effective').textContent = s.max_in_flight;
    $('#cap-default').textContent = s.max_in_flight_default;
    $('#cap-override').textContent = s.max_in_flight_override === null ? '— (none)' : s.max_in_flight_override;
    if (!document.activeElement || document.activeElement.id !== 'cap-input') {
      $('#cap-input').value = s.max_in_flight_override !== null ? s.max_in_flight_override : s.max_in_flight_default;
    }
  } catch (e) { console.error('tuning', e); }
}

async function loadQuota() {
  try {
    const q = await jget('/api/quota_estimate');
    $('#quota-used').textContent = q.dispatches_counted;
    $('#quota-budget').textContent = q.budget_per_window;
    $('#quota-pct').textContent = q.pct_used + '%';
    const bar = $('#quota-bar');
    const clamped = Math.min(100, q.pct_used);
    bar.style.width = clamped + '%';
    bar.classList.toggle('warn', q.pct_used >= 70 && q.pct_used < 90);
    bar.classList.toggle('danger', q.pct_used >= 90);
  } catch (e) { console.error('quota', e); }
}

function setCapResult(msg, ok = true) {
  const e = $('#cap-result');
  e.textContent = msg;
  e.style.color = ok ? 'var(--accent)' : 'var(--danger)';
  setTimeout(() => { e.textContent = ''; }, 8000);
}

$('#cap-save').addEventListener('click', async () => {
  const raw = $('#cap-input').value.trim();
  if (raw === '') return setCapResult('value required (0–50)', false);
  const v = parseInt(raw, 10);
  if (isNaN(v) || v < 0 || v > 50) return setCapResult('value must be 0–50', false);
  try {
    const r = await jpost('/api/concurrency_cap', { value: v });
    setCapResult(`override = ${r.override}, effective = ${r.effective}`);
    loadTuning(); loadStatus(); loadPlan();
  } catch (e) { setCapResult(String(e), false); }
});

$('#cap-clear').addEventListener('click', async () => {
  try {
    const r = await jpost('/api/concurrency_cap', { value: null });
    setCapResult(`override cleared (using config = ${r.effective})`);
    loadTuning(); loadStatus(); loadPlan();
  } catch (e) { setCapResult(String(e), false); }
});

// --- TIMERS (overlay-backed, direct systemd apply) ---------------------------

let currentTimer = null;
let currentTimerInfo = null;  // { is_overridden, is_user_created, kind }

async function loadTimersList() {
  try {
    const timers = await jget('/api/timers');
    const list = $('#timers-list');
    list.innerHTML = '';
    for (const t of timers) {
      const btn = el('button', { onclick: () => loadTimer(t.name) });
      btn.dataset.name = t.name;
      btn.appendChild(document.createTextNode(`${t.name} · ${t.kind}`));
      if (t.is_user_created) {
        btn.appendChild(el('span', { class: 'overlay-badge user' }, 'custom'));
      } else if (t.is_overridden) {
        btn.appendChild(el('span', { class: 'overlay-badge' }, 'edited'));
      }
      list.appendChild(btn);
    }
  } catch (e) { console.error('timers list', e); }
}

async function loadTimer(name) {
  try {
    document.querySelectorAll('#timers-list button').forEach(b => b.classList.toggle('active', b.dataset.name === name));
    const data = await jget(`/api/timers/${encodeURIComponent(name)}`);
    currentTimer = name;
    currentTimerInfo = data;
    let badge = '';
    if (data.is_user_created) badge = ' <span class="overlay-badge user inline">custom</span>';
    else if (data.is_overridden) badge = ' <span class="overlay-badge inline">edited</span>';
    else badge = ' <span class="muted small">(default)</span>';
    $('#timer-current').innerHTML = `${name} · ${data.kind}` + badge;
    $('#timer-content').value = data.content;
    $('#timer-save').disabled = false;
    // Revert/delete is available whenever there's something to remove.
    $('#timer-revert').disabled = !(data.is_overridden || data.is_user_created);
    $('#timer-revert').textContent = data.is_user_created ? 'delete unit' : 'revert to default';
    $('#timer-result').textContent = '';
  } catch (e) {
    $('#timer-result').textContent = String(e);
    $('#timer-result').style.color = 'var(--danger)';
  }
}

$('#timer-save').addEventListener('click', async () => {
  if (!currentTimer) return;
  const content = $('#timer-content').value;
  $('#timer-save').disabled = true;
  $('#timer-result').textContent = 'saving + applying to systemd…';
  $('#timer-result').style.color = 'var(--fg-muted)';
  try {
    await jput(`/api/timers/${encodeURIComponent(currentTimer)}`, { content });
    $('#timer-result').textContent = '✓ saved — systemd reloaded, unit restarted';
    $('#timer-result').style.color = 'var(--accent)';
    await loadTimersList();
    await loadTimer(currentTimer);
  } catch (e) {
    $('#timer-result').textContent = `❌ ${e}`;
    $('#timer-result').style.color = 'var(--danger)';
  } finally {
    $('#timer-save').disabled = false;
  }
});

$('#timer-revert').addEventListener('click', async () => {
  if (!currentTimer || !currentTimerInfo) return;
  const isCustom = currentTimerInfo.is_user_created;
  const prompt = isCustom
    ? `Delete ${currentTimer}? This will stop + disable + remove the unit.`
    : `Revert ${currentTimer} to the shipped default? Your changes will be lost.`;
  if (!confirm(prompt)) return;
  $('#timer-revert').disabled = true;
  try {
    const r = await jdelete(`/api/timers/${encodeURIComponent(currentTimer)}`);
    $('#timer-result').textContent = r.action === 'removed' ? '🗑 unit removed' : '↩ reverted to default';
    $('#timer-result').style.color = 'var(--accent)';
    await loadTimersList();
    if (r.action === 'removed') {
      currentTimer = null; currentTimerInfo = null;
      $('#timer-current').textContent = '(none selected)';
      $('#timer-content').value = '';
      $('#timer-save').disabled = true;
    } else {
      await loadTimer(currentTimer);
    }
  } catch (e) {
    $('#timer-result').textContent = `❌ ${e}`;
    $('#timer-result').style.color = 'var(--danger)';
    $('#timer-revert').disabled = false;
  }
});

$('#timer-new').addEventListener('click', async () => {
  const name = prompt('New unit filename (must end in .timer or .service):', 'my-task.timer');
  if (!name) return;
  if (!name.endsWith('.timer') && !name.endsWith('.service')) {
    return setControlResult('name must end in .timer or .service', false);
  }
  const tmpl = name.endsWith('.timer')
    ? `[Unit]\nDescription=My custom timer\n\n[Timer]\nOnCalendar=*-*-* 02:00:00 Europe/Madrid\nPersistent=true\nUnit=${name.replace('.timer', '.service')}\n\n[Install]\nWantedBy=timers.target\n`
    : `[Unit]\nDescription=My custom oneshot\n\n[Service]\nType=oneshot\nUser=engine\nExecStart=/bin/true\n`;
  try {
    await jpost('/api/timers', { name, content: tmpl });
    await loadTimersList();
    await loadTimer(name);
    $('#timer-result').textContent = `✓ ${name} created${name.endsWith('.timer') ? ' + enabled + started' : ''}. Edit and save.`;
    $('#timer-result').style.color = 'var(--accent)';
  } catch (e) {
    $('#timer-result').textContent = `❌ ${e}`;
    $('#timer-result').style.color = 'var(--danger)';
  }
});

// --- CONFIG ------------------------------------------------------------------

async function loadConfig() {
  try {
    const c = await jget('/api/config');
    $('#config-dump').textContent = JSON.stringify(c, null, 2);
  } catch (e) { console.error('config', e); }
}

// --- FILTER WIRING -----------------------------------------------------------

$('#batches-filter-status').addEventListener('change', loadBatches);
$('#runs-filter-agent').addEventListener('change', loadRuns);
$('#runs-filter-status').addEventListener('change', loadRuns);
['#events-filter-minutes', '#events-filter-level', '#events-filter-pattern', '#events-filter-limit'].forEach(s =>
  $(s).addEventListener('change', loadEvents)
);

// --- MOBILE NAV TOGGLE -------------------------------------------------------
(function setupNavToggle() {
  const toggle = $('#nav-toggle');
  const nav = $('#nav');
  if (!toggle || !nav) return;
  toggle.addEventListener('click', (ev) => {
    ev.stopPropagation();
    const open = nav.classList.toggle('open');
    toggle.classList.toggle('open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  // Close after picking a destination on mobile.
  nav.addEventListener('click', (ev) => {
    if (ev.target.tagName === 'A' && nav.classList.contains('open')) {
      nav.classList.remove('open');
      toggle.classList.remove('open');
      toggle.setAttribute('aria-expanded', 'false');
    }
  });
  // Click-outside to close.
  document.addEventListener('click', (ev) => {
    if (!nav.classList.contains('open')) return;
    if (nav.contains(ev.target) || toggle.contains(ev.target)) return;
    nav.classList.remove('open');
    toggle.classList.remove('open');
    toggle.setAttribute('aria-expanded', 'false');
  });
})();

// --- NAV SCROLL HIGHLIGHT ----------------------------------------------------

const sections = ['status', 'orchestration', 'tuning', 'batches', 'runs', 'events', 'controls', 'prompts', 'timers', 'config'];
const navLinks = Array.from(document.querySelectorAll('.nav a'));
function syncNav() {
  let active = sections[0];
  for (const id of sections) {
    const e = document.getElementById(id);
    if (e && e.getBoundingClientRect().top < 100) active = id;
  }
  navLinks.forEach(a => a.classList.toggle('active', a.getAttribute('href') === `#${active}`));
}
window.addEventListener('scroll', syncNav);

// --- BOOT --------------------------------------------------------------------

loadStatus(); setInterval(loadStatus, REFRESH.status);
loadDecision(); setInterval(loadDecision, REFRESH.decision);
loadPlan(); setInterval(loadPlan, REFRESH.plan);
loadBatches(); setInterval(loadBatches, REFRESH.batches);
loadRuns(); setInterval(loadRuns, REFRESH.runs);
loadEvents(); setInterval(loadEvents, REFRESH.events);
loadPromptList();
loadTimersList();
loadTuning(); setInterval(loadTuning, REFRESH.tuning);
loadQuota(); setInterval(loadQuota, REFRESH.quota);
loadConfig(); setInterval(loadConfig, REFRESH.config);
