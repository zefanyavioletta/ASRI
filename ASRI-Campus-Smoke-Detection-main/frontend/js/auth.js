/* ASRI — login, logout, peran (admin/operator) dan manajemen akun.
   Dimuat SETELAH app.js (memakai $, api, render, renderers, titles, heading, esc, icon, formatDateTime). */
let currentUser = null;
const isAdmin = () => currentUser && currentUser.role === 'admin';

Object.assign(iconPaths, {
  userplus: '<circle cx="9" cy="8" r="4"/><path d="M2 21c0-4 3-6 7-6s7 2 7 6M19 8v6M16 11h6"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-5.5 6.5-5.5s6.5 1.9 6.5 5.5"/><circle cx="17.5" cy="9" r="2.5"/><path d="M17 14.5c2.7.2 4.5 1.8 4.5 4.5"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>',
  eyeoff: '<path d="M3 3l18 18"/><path d="M10.6 5.1A10.7 10.7 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4M6.5 6.6C3.7 8.4 2 12 2 12s3.6 7 10 7c1.6 0 3-.4 4.3-1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>'
});
Object.assign(titles, { accountsAdd: 'Tambah Akun', accountsManage: 'Kelola Akun' });

/* ---------- util ---------- */
function initials(u) {
  const name = String((u && u.username) || '?');
  const parts = name.split(/[._-]+/).filter(Boolean);
  return ((parts.length > 1 ? parts[0][0] + parts[1][0] : name.slice(0, 2)) || '?').toUpperCase();
}
function toast(msg, bad) {
  const t = document.createElement('div');
  t.className = 'toast' + (bad ? ' bad' : '');
  t.setAttribute('role', 'status');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.classList.add('out'), 3200);
  setTimeout(() => t.remove(), 3700);
}
const jsonOpts = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const denied = () => heading('Akses ditolak', 'Halaman ini hanya dapat dibuka oleh administrator.');

/* ---------- tampilan login / aplikasi ---------- */
function showView(name) {
  $('#loginView').hidden = name !== 'login';
  $('#appShell').hidden = name !== 'app';
}
function showLoginAlert(msg) {
  const el = $('#loginAlert');
  el.textContent = msg || '';
  el.hidden = !msg;
}
function showLogin(msg) {
  try { stopCamera(); } catch (e) { /* kamera belum aktif */ }
  currentUser = null;
  page.innerHTML = '';
  document.querySelectorAll('.modal-backdrop').forEach(m => m.remove());
  $('#sidebar').classList.remove('open');
  showView('login');
  showLoginAlert(msg || '');
  $('#loginPassword').value = '';
  setTimeout(() => $('#loginUsername').focus(), 30);
}
function applyUser(u) {
  currentUser = u;
  document.querySelectorAll('[data-admin-only]').forEach(el => { el.hidden = !isAdmin(); });
  document.querySelectorAll('.avatar').forEach(a => { a.textContent = initials(u); });
  const nm = document.querySelector('.user-copy b'), st = document.querySelector('.user-copy small');
  if (nm) nm.textContent = u.username;
  if (st) st.innerHTML = '<i class="online-dot"></i> Online · ' + (isAdmin() ? 'Admin' : 'Operator');
}
function enterApp(u) {
  applyUser(u);
  showView('app');
  render('dashboard');
}
async function logout() {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { /* tetap keluar di sisi browser */ }
  $('#loginForm').reset();
  showLogin();
}
window.onSessionExpired = () => { if (currentUser) showLogin('Sesi Anda telah berakhir. Silakan masuk kembali.'); };

/* ---------- ikon pada menu baru ---------- */
(function decorateNav() {
  const map = { accountsAdd: 'userplus', accountsManage: 'users' };
  Object.keys(map).forEach(k => {
    const s = document.querySelector(`.nav-link[data-page="${k}"] span`);
    if (s) s.innerHTML = icon(map[k]);
  });
  const lo = document.querySelector('[data-logout] span');
  if (lo) lo.innerHTML = icon('logout');
})();

/* ---------- form login ---------- */
(function bindLogin() {
  const form = $('#loginForm'), pw = $('#loginPassword'), btn = $('#loginBtn'), label = $('#loginLabel');
  const setLoading = on => {
    btn.disabled = on;
    label.innerHTML = on ? '<span class="spinner"></span> Memproses...' : 'Masuk';
  };
  form.addEventListener('submit', async e => {
    e.preventDefault();
    showLoginAlert('');
    const username = $('#loginUsername').value.trim(), password = pw.value;
    if (!username || !password) {
      showLoginAlert('Username dan password wajib diisi.');
      (username ? pw : $('#loginUsername')).focus();
      return;
    }
    setLoading(true);
    try {
      const d = await api('/api/auth/login', jsonOpts('POST', { username, password, remember: $('#loginRemember').checked }));
      pw.value = '';
      enterApp(d.user);
    } catch (err) {
      showLoginAlert(err instanceof TypeError ? 'Tidak dapat terhubung ke server. Periksa koneksi lalu coba lagi.' : err.message);
      pw.select();
    } finally {
      setLoading(false);
    }
  });
  // tampil/sembunyikan password
  $('#togglePw').addEventListener('click', e => togglePassword(pw, e.currentTarget));
  // peringatan Caps Lock
  const caps = e => { $('#capsWarn').hidden = !(e.getModifierState && e.getModifierState('CapsLock')); };
  pw.addEventListener('keydown', caps);
  pw.addEventListener('keyup', caps);
  pw.addEventListener('blur', () => { $('#capsWarn').hidden = true; });
})();

function togglePassword(input, button) {
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  button.setAttribute('aria-pressed', String(show));
  button.setAttribute('aria-label', show ? 'Sembunyikan password' : 'Tampilkan password');
  button.querySelector('svg').innerHTML = show ? iconPaths.eyeoff : '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>';
}

/* ---------- klik global: keluar & toggle password ---------- */
document.addEventListener('click', e => {
  if (e.target.closest('[data-logout]')) { logout(); return; }
  const t = e.target.closest('[data-toggle-pw]');
  if (t) togglePassword(document.getElementById(t.dataset.togglePw), t);
});

/* ---------- pembatasan peran pada halaman yang sudah ada ---------- */
const baseIncidents = renderers.incidents;
renderers.incidents = () => {
  let html = baseIncidents();
  if (!isAdmin()) {
    // Operator tidak boleh menghapus laporan insiden (server juga menolak dengan 403).
    html = html.replace('id="deleteIncident"', 'id="deleteIncident" hidden')
      .replace('<p id="incidentStatusMsg"', '<p class="note">Penghapusan laporan insiden hanya dapat dilakukan oleh administrator.</p><p id="incidentStatusMsg"');
  }
  return html;
};

/* ---------- Tambah Akun (admin) ---------- */
const pwField = (id, label, ph) => `<div class="field"><label for="${id}">${label}</label><div class="pw-wrap"><input id="${id}" type="password" maxlength="128" autocomplete="new-password" placeholder="${ph}"><button type="button" class="eye" data-toggle-pw="${id}" aria-label="Tampilkan password" aria-pressed="false"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg></button></div></div>`;

renderers.accountsAdd = () => {
  if (!isAdmin()) return denied();
  return heading('Tambah Akun', 'Buat akun operator baru agar dapat masuk ke dashboard ASRI.') +
    `<div class="grid dash-grid"><section class="card panel"><h3>Data Akun Baru</h3>
    <form id="addAccountForm" novalidate autocomplete="off">
      <div class="field"><label for="accUsername">Username</label><input id="accUsername" maxlength="32" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="mis. operator.satpam"><small class="hint">3-32 karakter: huruf, angka, titik, garis bawah, atau strip.</small></div>
      <div class="field"><label for="accEmail">Email</label><input id="accEmail" type="email" maxlength="254" autocomplete="off" spellcheck="false" placeholder="nama@kampus.ac.id"></div>
      ${pwField('accPassword', 'Password', 'Minimal 8 karakter')}
      ${pwField('accPassword2', 'Konfirmasi Password', 'Ulangi password')}
      <div class="field"><label>Peran</label><div class="role-fixed"><span class="role-pill operator">Operator</span><small>Akun baru selalu dibuat sebagai operator.</small></div></div>
      <div class="center-actions"><button class="btn primary" id="saveAccount" type="submit">Buat Akun</button></div>
    </form>
    <p id="addAccountMsg" class="note" role="status" hidden></p></section>
    <section class="card panel"><h3>Hak Akses Peran</h3>
      <div class="setting-line"><span><span class="role-pill admin">Admin</span></span><b>Semua fitur + kelola akun</b></div>
      <div class="setting-line"><span><span class="role-pill operator">Operator</span></span><b>Pemantauan &amp; tindak lanjut</b></div>
      <div class="setting-line"><span>Buat / kelola akun</span><b>Admin saja</b></div>
      <div class="setting-line"><span>Hapus laporan insiden</span><b>Admin saja</b></div>
      <div class="setting-line"><span>Deteksi, riwayat, statistik, tindak lanjut insiden</span><b>Admin &amp; Operator</b></div>
      <button class="btn soft" type="button" data-go="accountsManage" style="margin-top:16px">Lihat Kelola Akun →</button>
    </section></div>`;
};

function bindAddAccount() {
  const form = $('#addAccountForm'), msg = $('#addAccountMsg'), btn = $('#saveAccount');
  const say = (t, bad) => { msg.hidden = !t; msg.textContent = t || ''; msg.classList.toggle('error', !!bad); };
  form.addEventListener('submit', async e => {
    e.preventDefault();
    say('');
    const username = $('#accUsername').value.trim(), email = $('#accEmail').value.trim();
    const password = $('#accPassword').value, again = $('#accPassword2').value;
    if (!username || !email || !password) return say('Username, email, dan password wajib diisi.', true);
    if (password.length < 8) return say('Password minimal 8 karakter.', true);
    if (password !== again) return say('Konfirmasi password tidak sama.', true);
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Menyimpan...';
    try {
      const d = await api('/api/users', jsonOpts('POST', { username, email, password }));
      form.reset();
      say(d.message);
      toast(d.message);
    } catch (err) {
      say(err.message, true);
    } finally {
      btn.disabled = false; btn.textContent = 'Buat Akun';
    }
  });
}

/* ---------- Kelola Akun (admin) ---------- */
renderers.accountsManage = () => {
  if (!isAdmin()) return denied();
  return heading('Kelola Akun', 'Ganti password, ubah email, atau hapus akun operator.') +
    `<section class="card"><div class="card-head"><div><h3>Daftar Akun Operator</h3><p id="accountsCount">Memuat akun...</p></div><button class="btn primary" type="button" data-go="accountsAdd">+ Tambah Akun</button></div><div id="accountsTable">Memuat akun...</div></section>`;
};

function openModal({ title, text, body, confirm, danger, onSubmit }) {
  const wrap = document.createElement('div');
  wrap.className = 'modal-backdrop';
  wrap.innerHTML = `<form class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}" novalidate autocomplete="off"><h3>${esc(title)}</h3>${text ? `<p class="modal-text">${text}</p>` : ''}${body || ''}<p class="modal-msg" role="alert" hidden></p><div class="modal-actions"><button type="button" class="btn white" data-close>Batal</button><button type="submit" class="btn ${danger ? 'danger-solid' : 'primary'}">${esc(confirm)}</button></div></form>`;
  const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = e => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  wrap.addEventListener('mousedown', e => { if (e.target === wrap) close(); });
  wrap.querySelector('[data-close]').onclick = close;
  const form = wrap.firstElementChild, err = wrap.querySelector('.modal-msg'), submit = form.querySelector('[type=submit]');
  form.addEventListener('submit', async e => {
    e.preventDefault();
    err.hidden = true;
    submit.disabled = true;
    try { await onSubmit(form, close); }
    catch (ex) { err.textContent = ex.message; err.hidden = false; }
    finally { submit.disabled = false; }
  });
  document.body.appendChild(wrap);
  const first = form.querySelector('input');
  (first || submit).focus();
}

async function loadAccounts() {
  const box = $('#accountsTable'), count = $('#accountsCount');
  if (!box) return;
  try {
    const d = await api('/api/users');
    count.textContent = d.users.length ? `${d.users.length} akun operator terdaftar` : 'Belum ada akun operator';
    box.innerHTML = `<div class="table-wrap"><table><thead><tr><th>EMAIL</th><th>USERNAME</th><th>DIBUAT</th><th>LOGIN TERAKHIR</th><th>AKSI</th></tr></thead><tbody>${
      d.users.length ? d.users.map(u => `<tr><td><b>${esc(u.email)}</b></td><td>${esc(u.username)}</td><td>${formatDateTime(u.created_at)}</td><td>${u.last_login ? formatDateTime(u.last_login) : 'Belum pernah'}</td><td><div class="row-actions"><button class="btn soft sm" type="button" data-acc="password" data-id="${u.id}">Ganti Password</button><button class="btn white sm" type="button" data-acc="email" data-id="${u.id}">Ubah Email</button><button class="btn danger sm" type="button" data-acc="delete" data-id="${u.id}">Hapus</button></div></td></tr>`).join('')
        : '<tr><td colspan="5">Belum ada akun operator. Klik <b>+ Tambah Akun</b> untuk membuat akun baru.</td></tr>'}</tbody></table></div>`;
    box.querySelectorAll('[data-acc]').forEach(b => b.onclick = () => accountAction(b.dataset.acc, d.users.find(x => x.id == b.dataset.id)));
  } catch (e) {
    count.textContent = '';
    box.innerHTML = `<div class="pending-summary">${esc(e.message)}</div>`;
  }
}

function accountAction(kind, u) {
  if (!u) return;
  const base = `/api/users/${u.id}`;
  if (kind === 'password') {
    openModal({
      title: 'Ganti Password', confirm: 'Simpan Password',
      text: `Password baru untuk <b>${esc(u.email)}</b>. Sesi login akun ini akan diakhiri.`,
      body: pwField('newPw', 'Password baru', 'Minimal 8 karakter') + pwField('newPw2', 'Konfirmasi password baru', 'Ulangi password baru'),
      onSubmit: async (f, close) => {
        const p = f.querySelector('#newPw').value, p2 = f.querySelector('#newPw2').value;
        if (p.length < 8) throw new Error('Password minimal 8 karakter.');
        if (p !== p2) throw new Error('Konfirmasi password tidak sama.');
        const d = await api(base + '/password', jsonOpts('PATCH', { password: p }));
        close(); toast(d.message); loadAccounts();
      }
    });
  } else if (kind === 'email') {
    openModal({
      title: 'Ubah Email', confirm: 'Simpan Email',
      text: `Email baru untuk akun <b>${esc(u.username)}</b>.`,
      body: `<div class="field"><label for="newEmail">Email baru</label><input id="newEmail" type="email" maxlength="254" value="${esc(u.email)}" spellcheck="false"></div>`,
      onSubmit: async (f, close) => {
        const em = f.querySelector('#newEmail').value.trim();
        if (!em) throw new Error('Email wajib diisi.');
        const d = await api(base + '/email', jsonOpts('PATCH', { email: em }));
        close(); toast(d.message); loadAccounts();
      }
    });
  } else if (kind === 'delete') {
    openModal({
      title: 'Hapus Akun', confirm: 'Ya, Hapus Akun', danger: true,
      text: `Hapus akun <b>${esc(u.email)}</b> (${esc(u.username)})? Akun tidak dapat login lagi dan tindakan ini tidak dapat dibatalkan.`,
      onSubmit: async (f, close) => {
        const d = await api(base, { method: 'DELETE' });
        close(); toast(d.message); loadAccounts();
      }
    });
  }
}

/* ---------- Ganti Password sendiri (di Profil & Pengaturan) ---------- */
const baseSettings = renderers.settings;
renderers.settings = () => {
  const html = baseSettings();
  const card = `<section class="card panel"><h3>Ganti Password</h3>
    <form id="ownPasswordForm" novalidate autocomplete="off">
      ${pwField('curPw', 'Password lama', 'Masukkan password lama')}
      ${pwField('ownNewPw', 'Password baru', 'Minimal 8 karakter')}
      ${pwField('ownNewPw2', 'Konfirmasi password baru', 'Ulangi password baru')}
      <div class="center-actions"><button class="btn primary" id="saveOwnPw" type="submit">Simpan Password</button></div>
    </form>
    <p id="ownPwMsg" class="note" role="status" hidden></p></section>`;
  const marker = '<section class="card panel"><h3>Informasi Model';
  const i = html.indexOf(marker);
  if (i < 0) return html;
  // kartu model + kartu password ditumpuk di kolom kanan
  return html.slice(0, i) + '<div class="stack">' + html.slice(i, -'</div>'.length) + card + '</div></div>';
};

function bindOwnPassword() {
  const form = $('#ownPasswordForm'), msg = $('#ownPwMsg'), btn = $('#saveOwnPw');
  if (!form) return;
  const say = (t, bad) => { msg.hidden = !t; msg.textContent = t || ''; msg.classList.toggle('error', !!bad); };
  form.addEventListener('submit', async e => {
    e.preventDefault();
    say('');
    const cur = $('#curPw').value, nw = $('#ownNewPw').value, again = $('#ownNewPw2').value;
    if (!cur || !nw) return say('Password lama dan password baru wajib diisi.', true);
    if (nw.length < 8) return say('Password baru minimal 8 karakter.', true);
    if (nw !== again) return say('Konfirmasi password baru tidak sama.', true);
    if (nw === cur) return say('Password baru harus berbeda dari password lama.', true);
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Menyimpan...';
    try {
      const d = await api('/api/auth/password', jsonOpts('PATCH', { current_password: cur, new_password: nw }));
      form.reset();
      say(d.message);
      toast(d.message);
    } catch (err) {
      say(err.message, true);
    } finally {
      btn.disabled = false; btn.textContent = 'Simpan Password';
    }
  });
}

/* ---------- sambungkan ke bindPage milik app.js ---------- */
const baseBindPage = bindPage;
bindPage = function (key) {
  baseBindPage(key);
  if (key === 'accountsAdd' && isAdmin()) bindAddAccount();
  if (key === 'accountsManage' && isAdmin()) loadAccounts();
  if (key === 'settings') bindOwnPassword();
};

/* ---------- mulai: cek sesi ---------- */
(async function boot() {
  try {
    const d = await api('/api/auth/me');
    enterApp(d.user);
  } catch (e) {
    showLogin(e instanceof TypeError ? 'Tidak dapat terhubung ke server.' : '');
  }
})();
