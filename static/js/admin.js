/**
 * CLAT Vision Admin Panel — JavaScript
 * Handles navigation, API calls, and UI interactions.
 */

// ── State ──────────────────────────────────────────────────────────────────
let allQuestions = [];
let allUsers     = [];
let qPage        = 1;
const Q_PER_PAGE = 15;
let editingId    = null;

// ── Navigation ─────────────────────────────────────────────────────────────
function nav(section) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById(`sec-${section}`).classList.add('active');
  document.querySelector(`[data-section="${section}"]`).classList.add('active');

  if (section === 'dashboard') loadDashboard();
  if (section === 'questions') loadQuestions();
  if (section === 'users')     loadUsers();
}

document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => nav(item.dataset.section));
});

// ── Toast ──────────────────────────────────────────────────────────────────
function toast(msg, type = 'success') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

// ── API helpers ────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  try {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    return res.json();
  } catch (e) {
    console.error(e);
    return { error: e.message };
  }
}

// ── Dashboard ──────────────────────────────────────────────────────────────
async function loadDashboard() {
  const [metrics, qData] = await Promise.all([
    api('/api/metrics'),
    api('/api/questions'),
  ]);

  document.getElementById('stat-questions').textContent = qData.total ?? '—';
  document.getElementById('stat-users').textContent     = metrics.total_users ?? '—';

  const qs_d = metrics.qs_d || {};
  const att  = qs_d.attempts ?? 0;
  const cor  = qs_d.correct  ?? 0;
  document.getElementById('stat-attempts').textContent  = att;
  document.getElementById('stat-acc').textContent       =
    att > 0 ? Math.round(cor / att * 100) + '%' : '—';
}

// ── Questions ──────────────────────────────────────────────────────────────
async function loadQuestions() {
  const data   = await api('/api/questions');
  allQuestions = data.questions || [];
  document.getElementById('q-count-badge').textContent = `(${allQuestions.length})`;
  qPage = 1;
  renderQuestions();
}

function filterQuestions() {
  qPage = 1;
  renderQuestions();
}

function renderQuestions() {
  const q    = (document.getElementById('q-search').value || '').toLowerCase();
  const list = q
    ? allQuestions.filter(q_ =>
        (q_.question || '').toLowerCase().includes(q) ||
        (q_.category || '').toLowerCase().includes(q))
    : allQuestions;

  const total = list.length;
  const pages = Math.max(1, Math.ceil(total / Q_PER_PAGE));
  qPage = Math.min(qPage, pages);

  const slice = list.slice((qPage - 1) * Q_PER_PAGE, qPage * Q_PER_PAGE);
  const tbody = document.getElementById('questions-tbody');

  if (!slice.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="loading">No questions found</td></tr>`;
    document.getElementById('q-pagination').innerHTML = '';
    return;
  }

  tbody.innerHTML = slice.map(q_ => {
    const opts    = q_.options || [];
    const correct = opts[q_.correct_answer] || '—';
    const cat     = q_.category || 'General';
    const preview = (q_.question || '').substring(0, 60) +
                    (q_.question?.length > 60 ? '…' : '');
    return `
      <tr>
        <td><code style="color:var(--muted)">#${q_.id}</code></td>
        <td title="${escHtml(q_.question || '')}">${escHtml(preview)}</td>
        <td><span class="badge badge-purple">${escHtml(cat)}</span></td>
        <td style="color:var(--success);font-size:12px">${escHtml(correct)}</td>
        <td style="text-align:right">
          <button class="btn btn-ghost btn-sm" onclick="editQuestion(${q_.id})">✏️</button>
          <button class="btn btn-danger btn-sm" onclick="deleteQuestion(${q_.id})">🗑</button>
        </td>
      </tr>`;
  }).join('');

  const pag = document.getElementById('q-pagination');
  pag.innerHTML = `
    <button class="page-btn" onclick="qPage--;renderQuestions()" ${qPage<=1?'disabled':''}>‹</button>
    <span class="page-btn current">${qPage} / ${pages}</span>
    <button class="page-btn" onclick="qPage++;renderQuestions()" ${qPage>=pages?'disabled':''}>›</button>`;
}

// ── Add / Edit form ────────────────────────────────────────────────────────
function showAddForm() {
  editingId = null;
  document.getElementById('form-title').textContent = 'Add Question';
  document.getElementById('q-text').value           = '';
  document.getElementById('q-correct').value        = '0';
  document.getElementById('q-category').value       = 'General';
  ['opt-0','opt-1','opt-2','opt-3'].forEach(id =>
    (document.getElementById(id).value = ''));
  document.getElementById('add-form').style.display = 'block';
  document.getElementById('q-text').focus();
}

function hideAddForm() {
  document.getElementById('add-form').style.display = 'none';
  editingId = null;
}

function editQuestion(id) {
  const q = allQuestions.find(q_ => q_.id === id);
  if (!q) return;
  editingId = id;
  document.getElementById('form-title').textContent = 'Edit Question';
  document.getElementById('q-text').value           = q.question || '';
  document.getElementById('q-correct').value        = String(q.correct_answer || 0);
  document.getElementById('q-category').value       = q.category || 'General';
  (q.options || []).forEach((opt, i) => {
    const el = document.getElementById(`opt-${i}`);
    if (el) el.value = opt;
  });
  document.getElementById('add-form').style.display = 'block';
  document.getElementById('q-text').focus();
}

async function submitQuestion() {
  const question = document.getElementById('q-text').value.trim();
  const options  = ['opt-0','opt-1','opt-2','opt-3'].map(id =>
    document.getElementById(id).value.trim());
  const correct  = parseInt(document.getElementById('q-correct').value);
  const category = document.getElementById('q-category').value;

  if (!question)               return toast('Question text is required', 'error');
  if (options.some(o => !o))   return toast('All 4 options are required', 'error');

  const body   = { question, options, correct_answer: correct, category };
  const result = editingId
    ? await api(`/api/questions/${editingId}`, { method: 'PUT', body: JSON.stringify(body) })
    : await api('/api/questions', { method: 'POST', body: JSON.stringify(body) });

  if (result.success || result.id) {
    toast(editingId ? 'Question updated ✓' : 'Question added ✓');
    hideAddForm();
    loadQuestions();
  } else {
    toast(result.error || 'Failed to save question', 'error');
  }
}

async function deleteQuestion(id) {
  if (!confirm(`Delete question #${id}? This cannot be undone.`)) return;
  const result = await api(`/api/questions/${id}`, { method: 'DELETE' });
  if (result.success) {
    toast('Question deleted');
    loadQuestions();
  } else {
    toast(result.error || 'Delete failed', 'error');
  }
}

// ── Users ──────────────────────────────────────────────────────────────────
async function loadUsers() {
  const data = await api('/api/users');
  allUsers   = data.users || [];
  renderUsers();
}

function filterUsers() {
  renderUsers();
}

function renderUsers() {
  const q    = (document.getElementById('u-search').value || '').toLowerCase();
  const list = q
    ? allUsers.filter(u => (u.name || u.username || '').toLowerCase().includes(q))
    : allUsers;

  const tbody = document.getElementById('users-tbody');
  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="loading">No users found</td></tr>`;
    return;
  }

  tbody.innerHTML = list.slice(0, 100).map((u, i) => {
    const name  = escHtml(u.name || u.username || `User ${u.user_id}`);
    const score = u.total_marks ?? u.correct_answers ?? 0;
    const att   = u.total_answers ?? u.quizzes_attempted ?? 0;
    const acc   = att > 0 ? Math.round(score / att * 100) + '%' : '—';
    const medal = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : `${i+1}.`;
    return `
      <tr>
        <td>${medal}</td>
        <td>${name} <span style="color:var(--muted);font-size:11px">#${u.user_id}</span></td>
        <td><b>${score}</b></td>
        <td>${att}</td>
        <td><span class="badge badge-green">${acc}</span></td>
      </tr>`;
  }).join('');
}

// ── Broadcast ──────────────────────────────────────────────────────────────
function previewBroadcast() {
  const text = document.getElementById('bc-text').value;
  const prev = document.getElementById('bc-preview');
  prev.innerHTML = text;
  prev.style.display = text ? 'block' : 'none';
}

async function sendBroadcast() {
  const message = document.getElementById('bc-text').value.trim();
  if (!message) return toast('Message cannot be empty', 'error');
  if (!confirm('Send this broadcast to all users and groups?')) return;

  const result = await api('/api/broadcast', {
    method: 'POST',
    body: JSON.stringify({ message }),
  });

  const out = document.getElementById('bc-result');
  if (result.queued) {
    out.innerHTML = `
      <div style="background:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.3);
                  border-radius:8px;padding:14px;font-size:13px">
        ✅ <b>Queued for broadcast</b><br>
        👥 Users: <b>${result.users}</b> · 💬 Groups: <b>${result.groups}</b><br>
        <span style="color:var(--muted);font-size:12px">${result.note || ''}</span>
      </div>`;
    toast('Broadcast queued');
  } else {
    out.innerHTML = `<div style="color:var(--danger)">❌ ${result.error || 'Failed'}</div>`;
    toast(result.error || 'Broadcast failed', 'error');
  }
}

// ── Reload DB cache ────────────────────────────────────────────────────────
async function reloadQuestions() {
  const result = await api('/api/reload', { method: 'POST' });
  if (result.success) {
    toast(`Cache reloaded — ${result.total} questions`);
    loadDashboard();
  } else {
    toast(result.error || 'Reload failed', 'error');
  }
}

// ── Utils ──────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Init ───────────────────────────────────────────────────────────────────
loadDashboard();
