import './styles.css';
import { isDesktop, pickDirectory } from './platform.js';

/* ------------------------------------------------------------------ *
 * Small helpers
 * ------------------------------------------------------------------ */

const $ = (id) => document.getElementById(id);
const icon = (name, cls = '') => `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
const pad2 = (n) => String(n).padStart(2, '0');
const toLocalISO = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

const state = {
  authenticated: false,
  displayName: null,
  activities: [],
  distanceUnit: 'mi',
  hasKey: false,
  remoteMode: 'auto',
  importPrivate: false,
};

/* ------------------------------------------------------------------ *
 * Toasts replace native browser dialogs: a desktop app should never
 * open a stray popup, and inline errors survive the Electron conversion.
 * ------------------------------------------------------------------ */

const toastHost = $('toasts');
const TOAST_TONES = {
  error: { border: 'border-rose-500/25', dot: 'bg-rose-400', text: 'text-rose-100' },
  success: { border: 'border-emerald-500/25', dot: 'bg-emerald-400', text: 'text-emerald-100' },
  info: { border: 'border-white/10', dot: 'bg-sky-400', text: 'text-slate-200' },
};

function notify(message, kind = 'info') {
  const tone = TOAST_TONES[kind] || TOAST_TONES.info;
  const el = document.createElement('div');
  el.className = `toast pointer-events-auto flex w-full max-w-sm cursor-pointer items-start gap-2.5 rounded-xl border ${tone.border} bg-[#111827] px-3.5 py-3 text-[12px] shadow-[0_8px_24px_-8px_rgba(0,0,0,0.8)]`;
  el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  el.innerHTML = `<span class="mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full ${tone.dot}"></span><span class="leading-relaxed ${tone.text}">${escapeHtml(message)}</span>`;
  el.addEventListener('click', () => el.remove());
  toastHost.appendChild(el);
  setTimeout(() => el.remove(), kind === 'error' ? 8000 : 5000);
}

/** Swap a button into a spinner state and back, without losing its markup. */
function setBusy(button, busy, label = 'Working') {
  if (!button) return;
  if (!button.dataset.idleHtml) button.dataset.idleHtml = button.innerHTML;
  button.disabled = busy;
  button.innerHTML = busy
    ? `${icon('loader', 'animate-spin')}<span>${escapeHtml(label)}</span>`
    : button.dataset.idleHtml;
}

/* ------------------------------------------------------------------ *
 * Connection state in the top bar
 * ------------------------------------------------------------------ */

const CONNECTION_TONES = {
  checking: { dot: 'bg-slate-500', wrap: 'bg-white/[0.04]', text: 'text-slate-300' },
  connected: { dot: 'bg-emerald-400', wrap: 'bg-emerald-500/10', text: 'text-emerald-300' },
  signedout: { dot: 'bg-amber-400', wrap: 'bg-amber-500/10', text: 'text-amber-300' },
  error: { dot: 'bg-rose-400', wrap: 'bg-rose-500/10', text: 'text-rose-300' },
};

function setConnection(kind, label) {
  const tone = CONNECTION_TONES[kind] || CONNECTION_TONES.checking;
  const badge = $('user-badge');
  badge.className = `inline-flex shrink-0 items-center gap-2 rounded-full px-3 py-1.5 text-[11px] font-semibold ${tone.wrap} ${tone.text}`;
  badge.innerHTML = `<span class="h-1.5 w-1.5 rounded-full ${tone.dot}"></span><span class="max-w-[140px] truncate sm:max-w-[220px]">${escapeHtml(label)}</span>`;
}

/* ------------------------------------------------------------------ *
 * Live export progress over WebSocket
 * ------------------------------------------------------------------ */

let socket = null;
let reconnectTimer = null;
let unloading = false;

function connectSocket() {
  clearTimeout(reconnectTimer);
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(`${proto}//${location.host}/ws`);

  socket.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (err) {
      console.error('Unreadable progress message', err);
      return;
    }

    if (message.event === 'export_started') {
      showProgress(true);
      setProgressSpinner('loading');
      $('progress-status-title').textContent = 'Exporting activities…';
      updateProgress(0, message.data.total, 'Started download…');
    } else if (message.event === 'export_progress') {
      const { current, total, filename, success, error } = message.data;
      updateProgress(current, total, success ? `Saved ${filename}` : `Failed: ${error || 'unknown error'}`);
      markActivityStatus(message.data);
    } else if (message.event === 'export_completed') {
      const { total, succeeded, failed, blockDays, runs, duplicates, deleted } = message.data;
      setBusy($('btn-start-export'), false);
      $('progress-status-title').textContent = failed ? 'Export finished with errors' : 'Export complete';

      const parts = [];
      if (blockDays) parts.push(`${blockDays} block ${blockDays === 1 ? 'day' : 'days'}`);
      if (runs) parts.push(`${runs} ${runs === 1 ? 'run' : 'runs'} posted`);
      if (duplicates) parts.push(`${duplicates} duplicate${duplicates === 1 ? '' : 's'}`);
      if (deleted) parts.push(`${deleted} deleted on site`);
      const detail = parts.length ? ` · ${parts.join(' · ')}` : '';
      updateProgress(total, total, `${succeeded} of ${total} activities exported${detail}`);
      setProgressSpinner('complete');
      notify(
        failed
          ? `${failed} of ${total} activities failed. ${succeeded} exported${detail}.`
          : `${total} ${total === 1 ? 'activity' : 'activities'} exported${detail}.`,
        failed ? 'error' : 'success'
      );
    }
  };

  socket.onclose = () => {
    if (unloading) return;
    reconnectTimer = setTimeout(connectSocket, 2500);
  };
}

function updateProgress(current, total, detail) {
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  $('progress-bar-fill').style.width = `${pct}%`;
  $('progress-count').textContent = `${current} / ${total}`;
  $('progress-track').setAttribute('aria-valuenow', String(pct));
  if (detail) $('progress-current-item').textContent = detail;
}

function setProgressSpinner(mode) {
  const el = $('progress-spinner');
  if (mode === 'complete') {
    el.className = 'grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-emerald-500/10 text-emerald-400';
    el.innerHTML = icon('check', 'text-[15px]');
  } else {
    el.className = 'grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-sky-500/10 text-sky-400';
    el.innerHTML = icon('loader', 'text-[15px] animate-spin');
  }
}

function showProgress(show) {
  $('progress-card').hidden = !show;
}

/* ------------------------------------------------------------------ *
 * Date range
 * ------------------------------------------------------------------ */

function setDateRange(daysBack) {
  const end = new Date();
  const start = new Date();
  // Inclusive range: "30d" means 30 calendar days, including today.
  start.setDate(end.getDate() - (daysBack - 1));
  $('end-date').value = toLocalISO(end);
  $('start-date').value = toLocalISO(start);
  updateRangeMeta();
}

function setCurrentWeek() {
  const now = new Date();
  const monday = new Date(now);
  monday.setDate(now.getDate() - ((now.getDay() + 6) % 7));
  $('start-date').value = toLocalISO(monday);
  $('end-date').value = toLocalISO(now);
  updateRangeMeta();
}

function updateRangeMeta() {
  const start = $('start-date').value;
  const end = $('end-date').value;
  if (!start || !end) {
    $('range-days').textContent = '—';
    return;
  }
  if (start > end) {
    $('range-days').textContent = 'Invalid range';
    return;
  }
  const days = Math.round((new Date(`${end}T00:00:00`) - new Date(`${start}T00:00:00`)) / 86400000) + 1;
  $('range-days').textContent = `${days} ${days === 1 ? 'day' : 'days'}`;
}

function markPreset(name) {
  document.querySelectorAll('.preset-btn').forEach((btn) => {
    btn.setAttribute('aria-pressed', String(btn.dataset.preset === name));
  });
}

document.querySelectorAll('.preset-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    const preset = btn.dataset.preset;
    if (preset === 'week') {
      setCurrentWeek();
    } else if (preset === '30d') {
      setDateRange(30);
    } else if (preset === '90d') {
      setDateRange(90);
    } else if (preset === 'ytd') {
      const now = new Date();
      $('end-date').value = toLocalISO(now);
      $('start-date').value = toLocalISO(new Date(now.getFullYear(), 0, 1));
      updateRangeMeta();
    }
    markPreset(preset);
  });
});

['start-date', 'end-date'].forEach((id) => {
  $(id).addEventListener('input', () => {
    markPreset(null);
    updateRangeMeta();
  });
});

/* ------------------------------------------------------------------ *
 * Distance unit
 * ------------------------------------------------------------------ */

document.querySelectorAll('.unit-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    state.distanceUnit = btn.dataset.unit;
    document.querySelectorAll('.unit-btn').forEach((other) => {
      other.setAttribute('aria-pressed', String(other === btn));
    });
    $('dist-col-label').textContent = `Distance (${state.distanceUnit})`;
    if (state.activities.length) renderActivities(state.activities);
  });
});

/* ------------------------------------------------------------------ *
 * Push target (saved server-side, shared with the CLI and RPC sidecar)
 * ------------------------------------------------------------------ */

const remoteKeyEl = $('remote-key');
const pushEnabledEl = $('push-enabled');
const privateImportEl = $('private-import');

const MODE_COPY = {
  block: 'Only block days are imported. A run with no block for its date is skipped.',
  auto: 'A block day when the date matches; otherwise a standalone run on your feed.',
  activity: 'Always a standalone run. Existing block days are left alone.',
};

/** Paint the three-way mode control and its one-line explanation. */
function setRemoteModeUI(mode, { legacyDefault = false } = {}) {
  state.remoteMode = MODE_COPY[mode] ? mode : 'auto';
  document.querySelectorAll('.mode-btn').forEach((btn) => {
    const active = btn.dataset.mode === state.remoteMode;
    btn.setAttribute('aria-pressed', String(active));
    btn.className = `mode-btn rounded-lg px-1.5 py-1.5 text-[10px] font-bold uppercase tracking-[0.04em] transition ${
      active ? 'bg-orange-500/15 text-orange-300' : 'text-slate-400 hover:text-slate-200'
    }`;
  });
  let copy = MODE_COPY[state.remoteMode];
  if (legacyDefault) {
    copy += ' Existing setup kept on Block only — choose Auto or Always run to post runs outside blocks.';
  }
  $('remote-mode-copy').textContent = copy;
}

document.querySelectorAll('.mode-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    const mode = btn.dataset.mode;
    if (!MODE_COPY[mode] || mode === state.remoteMode) return;
    setRemoteModeUI(mode);
    savePreference({ remote_mode: mode }, 'Could not save the import mode.');
  });
});

privateImportEl.addEventListener('change', () => {
  state.importPrivate = privateImportEl.checked;
  savePreference(
    { import_private: privateImportEl.checked },
    'Could not save the private-import setting.',
  );
});

function setSaveStatus(message, ok) {
  const el = $('remote-save-status');
  el.textContent = message;
  el.className = 'text-[11px] ' + (ok === true
    ? 'font-medium text-emerald-400'
    : ok === false
      ? 'font-medium text-rose-400'
      : 'text-slate-400');
}

function refreshPushChip() {
  const chip = $('push-state-chip');
  if (!state.hasKey) {
    chip.textContent = 'No key';
    chip.className = 'shrink-0 rounded-full bg-amber-500/10 px-2 py-0.5 text-[9px] font-bold uppercase tracking-[0.1em] text-amber-400';
    return;
  }
  const on = pushEnabledEl.checked;
  chip.textContent = on ? 'On' : 'Off';
  chip.className = 'shrink-0 rounded-full px-2 py-0.5 text-[9px] font-bold uppercase tracking-[0.1em] '
    + (on ? 'bg-orange-500/10 text-orange-400' : 'bg-white/[0.04] text-slate-400');
}

$('toggle-remote').addEventListener('click', () => {
  const fields = $('remote-fields');
  fields.hidden = !fields.hidden;
  $('remote-chevron').classList.toggle('rotate-180', !fields.hidden);
});

$('btn-toggle-key').addEventListener('click', () => {
  const reveal = remoteKeyEl.type === 'password';
  remoteKeyEl.type = reveal ? 'text' : 'password';
  $('btn-toggle-key').innerHTML = icon(reveal ? 'eye-off' : 'eye', 'text-[13px]');
});

// Preferences are stored by the engine, not localStorage: the desktop shell
// binds a fresh port on every launch, and localStorage is origin-scoped, so a
// saved toggle would vanish on the next start.
async function savePreference(payload, failureMessage) {
  try {
    const res = await fetch('/api/settings/remote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || failureMessage);
    }
    return true;
  } catch (err) {
    notify(err.message || failureMessage, 'error');
    return false;
  }
}

pushEnabledEl.addEventListener('change', () => {
  refreshPushChip();
  savePreference(
    { push_enabled: pushEnabledEl.checked },
    'Could not save the push setting.',
  );
});

remoteKeyEl.addEventListener('input', () => {
  if (remoteKeyEl.value) setSaveStatus('Unsaved key', null);
});

async function loadRemoteSettings() {
  let server = null;
  try {
    const res = await fetch('/api/settings/remote');
    if (res.ok) server = await res.json();
  } catch (err) { /* engine defaults apply */ }

  if (server && server.has_key) {
    setSaveStatus(`Key saved on this computer (${server.key_prefix}…)`, true);
    remoteKeyEl.placeholder = `Saved (${server.key_prefix}…)`;
    $('remote-fields').hidden = false;
    $('remote-chevron').classList.add('rotate-180');
  }
  state.hasKey = Boolean(server && server.has_key);
  if (server) {
    pushEnabledEl.checked = Boolean(server.push_enabled);
    const mode = server.remote_mode || 'auto';
    setRemoteModeUI(mode, {
      legacyDefault: mode === 'block' && !server.remote_mode_explicit,
    });
    state.importPrivate = Boolean(server.import_private);
    privateImportEl.checked = state.importPrivate;
    if (server.export_dir) {
      outputDirEl.value = server.export_dir;
      savedExportDir = server.export_dir;
    }
  }

  refreshPushChip();
}

$('btn-save-remote').addEventListener('click', async () => {
  const button = $('btn-save-remote');
  setBusy(button, true, 'Saving');
  setSaveStatus('Saving…', null);
  try {
    const res = await fetch('/api/settings/remote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ remote_api_key: remoteKeyEl.value || undefined }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not save the key');
    remoteKeyEl.value = '';
    remoteKeyEl.placeholder = data.has_key ? `Saved (${data.key_prefix}…)` : 'lrf_...';
    state.hasKey = Boolean(data.has_key);
    refreshPushChip();
    setSaveStatus(data.has_key ? `Saved (${data.key_prefix}…)` : 'API key cleared', true);
  } catch (err) {
    setSaveStatus(err.message, false);
  } finally {
    setBusy(button, false);
  }
});

function setSyncStatus(message, kind) {
  const el = $('sync-new-status');
  el.textContent = message;
  el.className = 'text-[11px] ' + (kind === 'success'
    ? 'font-medium text-emerald-400'
    : kind === 'error'
      ? 'font-medium text-rose-400'
      : 'text-slate-400');
}

$('btn-sync-new').addEventListener('click', async () => {
  if (!state.authenticated) return notify('Connect your Garmin account first.', 'error');
  const button = $('btn-sync-new');
  setBusy(button, true, 'Looking up');
  setSyncStatus('Asking LetsRunFaster for your latest imported day…', null);
  try {
    const res = await fetch('/api/sync/latest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ remote_url: null, remote_api_key: null }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Lookup failed');
    if (!data.latest_date && !data.suggested_start) throw new Error('No training block found on the site yet.');

    // Local calendar date: toISOString() is UTC and reads as tomorrow
    // after 7pm in US Central. Clamp the start so the range never inverts.
    const today = toLocalISO(new Date());
    const rawStart = data.latest_date || data.suggested_start;
    const start = rawStart && rawStart <= today ? rawStart : today;
    $('start-date').value = start;
    $('end-date').value = today;
    markPreset(null);
    updateRangeMeta();

    setSyncStatus(data.latest_date
      ? `Latest block day ${data.latest_date}. Re-scanning ${start} → ${today}; press Export now to import.`
      : `No days logged yet. Showing activities from block start ${start}; press Export now to import.`, 'success');
    loadActivities();
  } catch (err) {
    setSyncStatus(err.message, 'error');
  } finally {
    setBusy(button, false);
  }
});

/* ------------------------------------------------------------------ *
 * Garmin session
 * ------------------------------------------------------------------ */

async function checkAuth() {
  setConnection('checking', 'Checking Garmin…');
  try {
    const res = await fetch('/api/auth/status');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    updateAuthUI(Boolean(data.authenticated), data.displayName);
  } catch (err) {
    setConnection('error', 'Engine unreachable');
    notify('Could not reach the local engine. Restart the app if this continues.', 'error');
  }
}

function initials(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return 'LR';
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}

function updateAuthUI(authenticated, displayName) {
  state.authenticated = authenticated;
  state.displayName = displayName || null;

  $('auth-form-container').hidden = authenticated;
  $('mfa-container').hidden = true;
  $('auth-success-container').hidden = !authenticated;

  if (authenticated) {
    setConnection('connected', displayName || 'Garmin connected');
    $('auth-display-name').textContent = displayName || 'Garmin user';
    $('auth-avatar').textContent = initials(displayName);
    $('auth-badge-status').textContent = 'Connected';
    $('auth-badge-status').className = 'shrink-0 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[9px] font-bold uppercase tracking-[0.12em] text-emerald-400';
  } else {
    setConnection('signedout', 'Not connected');
    $('auth-badge-status').textContent = 'Sign in';
    $('auth-badge-status').className = 'shrink-0 rounded-full bg-white/[0.04] px-2 py-0.5 text-[9px] font-bold uppercase tracking-[0.12em] text-slate-400';
  }
}

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('btn-login');
  setBusy(button, true, 'Connecting');
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email: $('login-email').value,
        password: $('login-password').value,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.message || 'Sign-in failed');

    if (data.mfaRequired) {
      $('auth-form-container').hidden = true;
      $('mfa-container').hidden = false;
      $('mfa-code').focus();
    } else if (data.status === 'AUTHENTICATED') {
      updateAuthUI(true, data.displayName);
      notify('Garmin account connected.', 'success');
    } else {
      throw new Error(data.message || 'Sign-in failed');
    }
  } catch (err) {
    notify(err.message, 'error');
  } finally {
    setBusy(button, false);
  }
});

$('btn-submit-mfa').addEventListener('click', async () => {
  const code = $('mfa-code').value.trim();
  if (!code) return notify('Enter the verification code Garmin sent you.', 'error');

  const button = $('btn-submit-mfa');
  setBusy(button, true, 'Verifying');
  try {
    const res = await fetch('/api/auth/mfa', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.message || 'Verification failed');
    if (!data.authenticated) throw new Error(data.message || 'Verification failed');
    updateAuthUI(true, data.displayName);
    notify('Garmin account connected.', 'success');
  } catch (err) {
    notify(err.message, 'error');
  } finally {
    setBusy(button, false);
  }
});

$('btn-logout').addEventListener('click', async () => {
  try {
    await fetch('/api/auth/logout', { method: 'POST' });
  } catch (err) { /* token cleanup is best-effort */ }
  updateAuthUI(false, null);
  notify('Disconnected. Cached tokens were cleared.', 'info');
});

/* ------------------------------------------------------------------ *
 * Activities preview
 * ------------------------------------------------------------------ */

function distanceIn(activity, unit) {
  let miles = activity.distanceMiles;
  if (miles === null || miles === undefined) {
    if (activity.distanceKm === null || activity.distanceKm === undefined) return null;
    miles = activity.distanceKm * 0.621371;
  }
  return unit === 'mi' ? miles : miles / 0.621371;
}

function formatDistance(value, unit, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return `${Number(value).toFixed(digits)} ${unit}`;
}

function formatDuration(minutes) {
  if (minutes === null || minutes === undefined || Number.isNaN(Number(minutes))) return '—';
  const total = Math.round(Number(minutes));
  const hours = Math.floor(total / 60);
  return hours ? `${hours}h ${pad2(total % 60)}m` : `${total}m`;
}

function formatDateTime(raw) {
  if (!raw) return '—';
  const text = String(raw).replace('T', ' ');
  const match = text.match(/^(\d{4}-\d{2}-\d{2})[ ](\d{2}:\d{2})/);
  return match ? `${match[1]} · ${match[2]}` : text;
}

function renderSummary(activities) {
  const unit = state.distanceUnit;
  if (!activities.length) {
    ['stat-activities', 'stat-distance', 'stat-time', 'stat-longest'].forEach((id) => {
      $(id).textContent = '—';
    });
    return;
  }

  const distances = activities
    .map((activity) => distanceIn(activity, unit))
    .filter((value) => value !== null && value !== undefined);
  const totalDistance = distances.reduce((sum, value) => sum + value, 0);
  const totalMinutes = activities.reduce((sum, activity) => sum + (Number(activity.durationMin) || 0), 0);
  const longest = distances.length ? Math.max(...distances) : null;

  $('stat-activities').textContent = String(activities.length);
  $('stat-distance').textContent = `${totalDistance.toFixed(1)} ${unit}`;
  $('stat-time').textContent = formatDuration(totalMinutes);
  $('stat-longest').textContent = formatDistance(longest, unit, 1);
}

function renderActivities(activities) {
  const unit = state.distanceUnit;
  const tbody = $('activities-tbody');

  $('dist-col-label').textContent = `Distance (${unit})`;
  $('activity-count-badge').textContent = String(activities.length);
  $('activities-subtitle').textContent = activities.length
    ? `${$('start-date').value} → ${$('end-date').value}`
    : 'No activities in this range.';

  renderSummary(activities);

  if (!activities.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="px-5 py-16 text-center">
      <span class="mx-auto grid h-10 w-10 place-items-center rounded-xl bg-white/[0.03] text-slate-400">${icon('folder', 'text-[18px]')}</span>
      <p class="mt-3 text-[13px] font-medium text-slate-300">No activities in this range</p>
      <p class="mt-1 text-[11px] text-slate-400">Try a wider date range, then preview again.</p>
    </td></tr>`;
    return;
  }

  tbody.innerHTML = activities.map((activity) => {
    const id = escapeHtml(activity.id);
    const distance = distanceIn(activity, unit);

    return `<tr class="border-b border-white/[0.04] transition last:border-0 hover:bg-white/[0.03]">
      <td class="whitespace-nowrap px-5 py-3 font-mono text-[11px] tabular-nums text-slate-400">${escapeHtml(formatDateTime(activity.startTime))}</td>
      <td class="px-3 py-3"><span class="inline-flex items-center rounded-md bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-[0.04em] text-sky-400">${escapeHtml(activity.type || 'other')}</span></td>
      <td class="max-w-[260px] truncate px-3 py-3 text-[12px] font-medium text-slate-100" title="${escapeHtml(activity.name)}">${escapeHtml(activity.name)}</td>
      <td class="whitespace-nowrap px-3 py-3 text-right font-mono text-[12px] font-semibold tabular-nums text-white">${formatDistance(distance, unit)}</td>
      <td class="whitespace-nowrap px-3 py-3 text-right font-mono text-[12px] tabular-nums text-emerald-400">${formatDuration(activity.durationMin)}</td>
      <td class="px-5 py-3 text-center" id="status-${id}"><span class="text-[10px] font-bold uppercase tracking-[0.08em] text-slate-400">Ready</span></td>
    </tr>`;
  }).join('');
}

async function loadActivities() {
  if (!state.authenticated) return notify('Connect your Garmin account first.', 'error');

  const start = $('start-date').value;
  const end = $('end-date').value;
  if (!start || !end) return notify('Choose a start and end date.', 'error');
  if (start > end) return notify('The start date is after the end date.', 'error');

  const button = $('btn-fetch-activities');
  setBusy(button, true, 'Loading');
  try {
    const res = await fetch('/api/activities/list', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start_date: start, end_date: end }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not load activities');
    state.activities = Array.isArray(data.activities) ? data.activities : [];
    renderActivities(state.activities);
    if (!state.activities.length) notify('No Garmin activities in that range.', 'info');
  } catch (err) {
    notify(err.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

$('btn-fetch-activities').addEventListener('click', loadActivities);

function markActivityStatus(data) {
  const cell = document.getElementById(`status-${data.activityId}`);
  if (!cell) return;

  if (!data.success) {
    cell.innerHTML = `<span class="inline-flex items-center gap-1 rounded-md bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-[0.06em] text-rose-400">${icon('x', 'text-[10px]')}Failed</span>`;
    return;
  }

  // `linkage` is absent on older servers; the engine maps those to "block",
  // so the existing branches below still apply. The non-block branch must run
  // before `remoteMatched === false`, because a stored run reports matched:false.
  let note = '';

  if (data.remoteDeleted) {
    note = `<div class="mt-1 text-[10px] font-medium text-slate-400">Deleted on the site — not re-imported</div>`;
  } else if (data.remoteDetail === 'duplicate') {
    note = `<div class="mt-1 text-[10px] font-medium text-slate-400">Duplicate skipped</div>`;
  } else if (data.remoteLinkage === 'nonblock' && data.remoteCreated) {
    const label = data.importPrivate ? 'Run logged (private)' : 'Run logged';
    const link = data.remoteActivityUrl
      ? ` <a href="${escapeHtml(data.remoteActivityUrl)}" target="_blank" rel="noreferrer" class="underline decoration-emerald-400/40 underline-offset-2 hover:text-emerald-300">View ↗</a>`
      : '';
    note = `<div class="mt-1 text-[10px] font-medium text-emerald-400">${label}${link}</div>`;
  } else if (data.remoteConvertedPlan) {
    note = `<div class="mt-1 text-[10px] font-medium text-emerald-400">Plan logged (${escapeHtml(data.remoteDetail || 'primary')})</div>`;
  } else if (data.remoteDetail && String(data.remoteDetail).startsWith('updated')) {
    note = `<div class="mt-1 text-[10px] font-medium text-sky-400">Already logged, refreshed</div>`;
  } else if (data.remoteMatched === true) {
    note = `<div class="mt-1 text-[10px] font-medium text-emerald-400">Block day (${escapeHtml(data.remoteDetail || 'added')})</div>`;
  } else if (data.remoteMatched === false && data.remoteLinkage !== 'nonblock') {
    note = `<div class="mt-1 text-[10px] font-medium text-amber-400">No block for date</div>`;
  }

  cell.innerHTML = `<span class="inline-flex items-center gap-1 rounded-md bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-[0.06em] text-emerald-400">${icon('check', 'text-[10px]')}Exported</span>${note}`;
}

/* ------------------------------------------------------------------ *
 * Export
 * ------------------------------------------------------------------ */

const BULK_WARNING_DAYS = 31;
let bulkArmed = false;
let bulkArmedKey = null;
let bulkTimer = null;

function rangeDays(start, end) {
  return Math.round((new Date(`${end}T00:00:00`) - new Date(`${start}T00:00:00`)) / 86400000) + 1;
}

/**
 * Auto/always-run modes can turn a historical backfill into a wall of feed
 * posts. Ask once (second click) on ranges over a month, and point at the
 * private toggle. Block-only mode never needs the warning.
 */
function confirmBulkPosting(start, end) {
  if (!pushEnabledEl.checked || state.remoteMode === 'block') return true;
  const days = rangeDays(start, end);
  if (days <= BULK_WARNING_DAYS) return true;
  // Confirming is tied to the exact range/mode/visibility that was warned
  // about: changing any of them before the second click warns again.
  const key = `${start}|${end}|${state.remoteMode}|${state.importPrivate}`;
  if (bulkArmed && bulkArmedKey === key) {
    bulkArmed = false;
    bulkArmedKey = null;
    clearTimeout(bulkTimer);
    return true;
  }
  bulkArmed = true;
  bulkArmedKey = key;
  const consequence = state.remoteMode === 'activity'
    ? 'every activity becomes a standalone run'
    : 'every date without a block becomes a run';
  const visibility = state.importPrivate ? 'private' : 'public';
  notify(
    `Bulk import (${days} days) in ${state.remoteMode} mode: ${consequence} (${visibility}). `
      + 'Click Export again to confirm, or turn on "Import runs as private" first.',
    'info',
  );
  bulkTimer = setTimeout(() => { bulkArmed = false; bulkArmedKey = null; }, 8000);
  return false;
}

$('btn-start-export').addEventListener('click', async () => {
  if (!state.authenticated) return notify('Connect your Garmin account first.', 'error');

  const start = $('start-date').value;
  const end = $('end-date').value;
  if (!start || !end) return notify('Choose a start and end date.', 'error');
  if (start > end) return notify('The start date is after the end date.', 'error');
  if (!confirmBulkPosting(start, end)) return;

  const format = document.querySelector('input[name="format"]:checked').value;
  const pushEnabled = $('push-enabled').checked;
  // "" disables pushing; null falls back to the saved server-side config.
  const remoteUrl = pushEnabled ? null : '';
  const remoteApiKey = pushEnabled ? (remoteKeyEl.value || null) : '';

  const button = $('btn-start-export');
  setBusy(button, true, 'Exporting');
  showProgress(true);
  setProgressSpinner('loading');
  $('progress-status-title').textContent = 'Exporting activities…';
  $('progress-current-item').textContent = 'Preparing download…';

  try {
    const res = await fetch('/api/activities/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_date: start,
        end_date: end,
        format,
        output_dir: $('output-dir').value,
        remote_url: remoteUrl,
        remote_api_key: remoteApiKey,
        remote_mode: state.remoteMode,
        import_private: state.importPrivate,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Export failed');
    // The endpoint resolves after the whole loop, so the button can be
    // released here even if the WebSocket completion event is delayed.
    setBusy(button, false);
  } catch (err) {
    setBusy(button, false);
    showProgress(false);
    notify(err.message, 'error');
  }
});

/* ------------------------------------------------------------------ *
 * Clear export folder — two-step, because window.confirm is a browser
 * affordance and this has to feel native inside Electron.
 * ------------------------------------------------------------------ */

const clearButton = $('btn-clear-exports');
const CLEAR_IDLE_CLASS = 'inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-[0.08em] text-slate-400 transition hover:bg-rose-500/10 hover:text-rose-400';
let clearArmed = false;
let clearTimer = null;

function feedback(message, kind) {
  const el = $('clear-folder-feedback');
  el.hidden = false;
  el.textContent = message;
  el.className = 'mt-1.5 text-[11px] ' + (kind === 'success'
    ? 'text-emerald-400'
    : kind === 'error'
      ? 'text-rose-400'
      : 'text-amber-400');
  if (kind !== 'warn') setTimeout(() => { el.hidden = true; }, 7000);
}

function disarmClear() {
  clearArmed = false;
  clearTimeout(clearTimer);
  clearButton.innerHTML = `${icon('trash', 'text-[11px]')}<span>Delete files</span>`;
  clearButton.className = CLEAR_IDLE_CLASS;
  $('clear-folder-feedback').hidden = true;
}

clearButton.addEventListener('click', async () => {
  const directory = $('output-dir').value.trim();
  if (!directory) return notify('Enter the export folder path first.', 'error');

  if (!clearArmed) {
    clearArmed = true;
    clearButton.className = 'inline-flex shrink-0 items-center gap-1 rounded bg-rose-500/15 px-2 py-1 text-[9px] font-bold uppercase tracking-[0.08em] text-rose-300 transition hover:bg-rose-500/25';
    clearButton.innerHTML = `${icon('alert', 'text-[11px]')}<span>Confirm delete</span>`;
    feedback(`Click again to delete files inside ${directory}.`, 'warn');
    clearTimer = setTimeout(disarmClear, 5000);
    return;
  }

  disarmClear();
  setBusy(clearButton, true, 'Deleting');
  try {
    const res = await fetch('/api/exports/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ output_dir: directory }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not delete the files');
    feedback(data.message, 'success');
    notify(data.message, 'success');
  } catch (err) {
    feedback(err.message, 'error');
    notify(err.message, 'error');
  } finally {
    setBusy(clearButton, false);
  }
});

/* ------------------------------------------------------------------ *
 * Runtime defaults from the engine (destination folder, version)
 * ------------------------------------------------------------------ */

const outputDirEl = $('output-dir');
const pickFolderBtn = $('btn-pick-folder');
// Last value written programmatically: editing the field only saves when the
// user actually changed something (blur fires even without a change).
let savedExportDir = null;

// Only the desktop shell can show a real folder picker: a browser will not
// reveal a folder's absolute path. Better to hide the button than to present
// an affordance that cannot work.
pickFolderBtn.hidden = !isDesktop;
$('folder-hint').textContent = isDesktop
  ? ''
  : 'Tip: the desktop app can browse for a folder.';

function persistExportDir() {
  const value = outputDirEl.value.trim();
  if (!value || value === savedExportDir) return;
  savedExportDir = value;
  savePreference({ export_dir: value }, 'Could not save the export folder.');
}

outputDirEl.addEventListener('change', persistExportDir);

pickFolderBtn.addEventListener('click', async () => {
  const picked = await pickDirectory({
    title: 'Choose where exports are saved',
    defaultPath: outputDirEl.value,
  });
  if (picked) {
    outputDirEl.value = picked;
    persistExportDir();
  }
});

async function loadRuntimeConfig() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) return;
    const data = await res.json();
    // Only replace the placeholder when the user has not typed a path.
    if (data.default_output_dir && outputDirEl.value === './garmin_exports') {
      outputDirEl.value = data.default_output_dir;
      savedExportDir = data.default_output_dir;
    }
  } catch (err) { /* engine offline: keep the visible default */ }
}

/* ------------------------------------------------------------------ *
 * Boot
 * ------------------------------------------------------------------ */

$('btn-dismiss-progress').addEventListener('click', () => showProgress(false));

// Keyboard: Ctrl/Cmd+Enter runs the export, Esc backs out of whatever is
// visually in the way (verification prompt, progress card, delete confirm).
document.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    const exportBtn = $('btn-start-export');
    if (!exportBtn.disabled) exportBtn.click();
    return;
  }
  if (event.key !== 'Escape') return;

  let handled = false;
  if (!$('mfa-container').hidden) {
    $('mfa-container').hidden = true;
    $('mfa-code').value = '';
    handled = true;
  }
  if (!$('progress-card').hidden) {
    showProgress(false);
    handled = true;
  }
  if (clearArmed) {
    disarmClear();
    handled = true;
  }
  if (handled) event.preventDefault();
});

setDateRange(30);
markPreset('30d');
setRemoteModeUI('auto');
refreshPushChip();
loadRuntimeConfig();
loadRemoteSettings();
checkAuth();
connectSocket();

window.addEventListener('beforeunload', () => {
  unloading = true;
  if (socket) socket.close();
});

