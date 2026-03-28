/* ═══════════════════════════════════════════════════════
   TRADEBOT AI — ui.js
   Header compartido: tema, idioma, dropdown de usuario
   Se incluye en index.html, journal.html, perfil.html
═══════════════════════════════════════════════════════ */

// ── Tema: init inmediato (evita flash) ──
(function () {
  const saved = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
})();

// ── Traducciones ──
const UI_LANG = {
  es: {
    themeLabel:  'Modo claro',
    themeLabelL: 'Modo oscuro',
    langLabel:   'English',
    online:      'En línea',
    thinking:    'Analizando...',
  },
  en: {
    themeLabel:  'Light mode',
    themeLabelL: 'Dark mode',
    langLabel:   'Español',
    online:      'Online',
    thinking:    'Analyzing...',
  }
};

let _currentLang = localStorage.getItem('lang') || 'es';

// ── Toggle de tema ──
function uiToggleTheme() {
  const cur  = document.documentElement.getAttribute('data-theme');
  const next = cur === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  _uiUpdateThemeLabel(next);
}

function _uiUpdateThemeLabel(theme) {
  const label = document.getElementById('dd-theme-label');
  const t = UI_LANG[_currentLang] || UI_LANG.es;
  if (label) label.textContent = theme === 'light' ? t.themeLabelL : t.themeLabel;
}

// ── Toggle de idioma ──
function uiToggleLang() {
  const next = _currentLang === 'es' ? 'en' : 'es';
  _currentLang = next;
  localStorage.setItem('lang', next);
  _uiUpdateLangLabel(next);
  // Disparar evento para que app.js u otras páginas reaccionen
  document.dispatchEvent(new CustomEvent('langchange', { detail: next }));
}

function _uiUpdateLangLabel(lang) {
  const label = document.getElementById('dd-lang-label');
  const t = UI_LANG[lang] || UI_LANG.es;
  if (label) label.textContent = t.langLabel;
}

// ── Rellenar el user chip + dropdown con datos de /me ──
function uiLoadUser() {
  fetch('/me')
    .then(r => r.json())
    .then(data => {
      if (!data.usuario) return;
      const u = data.usuario;

      // Mostrar chip, ocultar status pill
      const chip    = document.getElementById('user-chip');
      const statusP = document.getElementById('status-pill');
      if (chip)    chip.style.display    = 'flex';
      if (statusP) statusP.style.display = 'none';

      // Avatar (usa avatar_url — nombre real en el modelo)
      const photoUrl = u.avatar_url || u.foto_url || '';
      ['user-avatar', 'dd-avatar'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.src = photoUrl;
      });

      // Nombre chip: primer nombre solo
      const firstName = (u.nombre || u.email || '').split(' ')[0];
      const nameEl = document.getElementById('user-name');
      if (nameEl) nameEl.textContent = firstName;

      // Nombre completo en dropdown
      const ddName = document.getElementById('dd-user-name');
      if (ddName) ddName.textContent = u.nombre || u.email || '';

      // Badge plan
      const ddPlan = document.getElementById('dd-plan-badge');
      if (ddPlan) {
        const plan = u.plan || 'free';
        ddPlan.textContent = plan === 'pro' ? 'Pro' : 'Free';
        ddPlan.classList.toggle('pro', plan === 'pro');
      }
    })
    .catch(() => {});
}

// ── Dropdown open / close ──
function uiInitDropdown() {
  const chip     = document.getElementById('user-chip');
  const chipBtn  = document.getElementById('user-chip-btn');
  const themeBtn = document.getElementById('dd-theme-btn');
  const langBtn  = document.getElementById('dd-lang-btn');

  if (chipBtn) {
    chipBtn.addEventListener('click', e => {
      e.stopPropagation();
      chip.classList.toggle('open');
    });
  }

  if (themeBtn) themeBtn.addEventListener('click', uiToggleTheme);
  if (langBtn)  langBtn.addEventListener('click', uiToggleLang);

  // Cerrar al hacer click fuera
  document.addEventListener('click', e => {
    if (chip && !chip.contains(e.target)) chip.classList.remove('open');
  });

  // Cerrar con Escape
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && chip) chip.classList.remove('open');
  });

  // Sidebar toggle (presente en todas las páginas)
  const sidebarToggle = document.getElementById('sidebar-toggle');
  const sidebarEl     = document.getElementById('sidebar');
  if (sidebarToggle && sidebarEl) {
    sidebarToggle.addEventListener('click', () => sidebarEl.classList.toggle('collapsed'));
  }
}

// ── Init al cargar ──
document.addEventListener('DOMContentLoaded', () => {
  const cur = localStorage.getItem('theme') || 'dark';
  _uiUpdateThemeLabel(cur);
  _uiUpdateLangLabel(_currentLang);
  uiInitDropdown();
  uiLoadUser();
});
