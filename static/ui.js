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

      // Avatar — comportamiento original + fallback iniciales solo si no hay URL
      const photoUrl = u.avatar_url || u.foto_url || '';
      ['user-avatar', 'dd-avatar'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;

        const _showInitials = () => {
          const initials = (u.nombre || u.email || '?')
            .split(' ').map(w => w[0]).slice(0, 2).join('').toUpperCase();
          const isDd = id === 'dd-avatar';
          const size = isDd ? 40 : 22;
          const fs   = isDd ? 15 : 10;
          const span = document.createElement('span');
          span.textContent = initials;
          span.style.cssText = `display:inline-flex;align-items:center;justify-content:center;
            width:${size}px;height:${size}px;border-radius:${isDd ? '50%' : '6px'};
            background:var(--blue,#4A75A8);color:#fff;font-size:${fs}px;font-weight:700;
            flex-shrink:0;letter-spacing:0.5px;`;
          el.style.display = 'none';
          if (!el.parentNode.querySelector('span[data-initials]')) {
            span.setAttribute('data-initials', '1');
            el.parentNode.insertBefore(span, el);
          }
        };

        if (photoUrl) {
          el.onerror = _showInitials;
          el.onload  = () => { el.onerror = null; };
          el.removeAttribute('src');   // limpia estado de error previo del src=""
          el.src = photoUrl;
        } else {
          _showInitials();
        }
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

  // Liquidity panel — se inicia en cualquier página que tenga #liq-content
  if (document.getElementById('liq-content')) {
    cargarLiquidity();
    setInterval(cargarLiquidity, 60000);
  }
});

// ══════════════════════════════════════════════════════════════
// LIQUIDITY — L2 Order Book + Zonas de liquidación
// Disponible en journal.html, edge.html y cualquier página con #liq-content
// ══════════════════════════════════════════════════════════════

async function cargarLiquidity() {
  const el  = document.getElementById('liq-content');
  const btn = document.getElementById('liq-refresh');
  if (!el) return;
  if (btn) btn.style.opacity = '0.4';

  try {
    const res = await fetch('/api/liquidity');
    const d   = await res.json();
    if (!d.ok) throw new Error(d.error || 'Error');

    const precio = d.precio_ref || 0;
    const imb    = d.imbalance_pct || 50;
    const bias   = d.imbalance_bias || 'neutral';

    const imbColor = bias === 'bid' ? 'var(--green)' : bias === 'ask' ? 'var(--red)' : '#f59e0b';

    const imbHtml = `
      <div class="liq-imbalance-wrap">
        <div class="liq-imbalance-label">
          <span style="color:var(--green)">BID ${imb.toFixed(1)}%</span>
          <span style="color:var(--text3)">Imbalance</span>
          <span style="color:var(--red)">ASK ${(100-imb).toFixed(1)}%</span>
        </div>
        <div class="liq-imbalance-track">
          <div class="liq-imbalance-fill" style="width:${imb}%;background:${imbColor}"></div>
        </div>
      </div>`;

    const allWalls = [...(d.top_asks || []), ...(d.top_bids || [])];
    const maxUsd   = allWalls.reduce((m, w) => Math.max(m, w.usd || 0), 1);

    const asks = (d.top_asks || []).slice(0, 4).sort((a, b) => b.price - a.price);
    const bids = (d.top_bids || []).slice(0, 4).sort((a, b) => b.price - a.price);

    const wallRow = (w, type) => {
      const pct = Math.round((w.usd / maxUsd) * 100);
      const m   = w.usd >= 1e6 ? `$${(w.usd/1e6).toFixed(2)}M` : `$${(w.usd/1e3).toFixed(0)}K`;
      return `
        <div class="liq-wall-row">
          <div class="liq-wall-price">$${w.price.toLocaleString()}</div>
          <div class="liq-wall-bar-track">
            <div class="liq-wall-bar-fill ${type}" style="width:${pct}%"></div>
          </div>
          <div class="liq-wall-usd">${m}</div>
        </div>`;
    };

    const wallsHtml = `
      <div class="liq-walls-title">Order Book Walls</div>
      ${asks.map(w => wallRow(w, 'ask')).join('')}
      <div class="liq-price-line">
        <div class="liq-price-line-label">$${precio.toLocaleString()}</div>
        <hr class="liq-price-line-hr">
      </div>
      ${bids.map(w => wallRow(w, 'bid')).join('')}`;

    const bd1 = d.bid_depth_1pct || 0;
    const ad1 = d.ask_depth_1pct || 0;
    const depthHtml = `
      <div style="display:flex;gap:6px;margin-top:8px">
        <div style="flex:1;background:var(--bg3);border-radius:7px;padding:6px 8px">
          <div style="font-size:8px;color:var(--text3);margin-bottom:2px">BID ±1%</div>
          <div style="font-size:11px;font-weight:700;color:var(--green);font-family:var(--mono)">$${(bd1/1e6).toFixed(1)}M</div>
        </div>
        <div style="flex:1;background:var(--bg3);border-radius:7px;padding:6px 8px">
          <div style="font-size:8px;color:var(--text3);margin-bottom:2px">ASK ±1%</div>
          <div style="font-size:11px;font-weight:700;color:var(--red);font-family:var(--mono)">$${(ad1/1e6).toFixed(1)}M</div>
        </div>
      </div>`;

    const ll = d.liq_longs  || {};
    const ls = d.liq_shorts || {};
    const zoneRow = (label, price, type) => `
      <div class="liq-zone-row">
        <div class="liq-zone-label">${label}</div>
        <div class="liq-zone-price ${type}">$${Number(price).toLocaleString()}</div>
      </div>`;

    const zonesHtml = `
      <div class="liq-zones-wrap">
        <div class="liq-zones-title">Zonas Liquidación Est.</div>
        ${ls['10x']  ? zoneRow('Short 10x', ls['10x'],  'short') : ''}
        ${ls['25x']  ? zoneRow('Short 25x', ls['25x'],  'short') : ''}
        ${ls['50x']  ? zoneRow('Short 50x', ls['50x'],  'short') : ''}
        <div style="height:4px"></div>
        ${ll['50x']  ? zoneRow('Long  50x', ll['50x'],  'long') : ''}
        ${ll['25x']  ? zoneRow('Long  25x', ll['25x'],  'long') : ''}
        ${ll['10x']  ? zoneRow('Long  10x', ll['10x'],  'long') : ''}
        <div class="liq-nota">Estimación matemática desde precio actual — no posiciones reales</div>
      </div>`;

    const now = new Date().toLocaleTimeString('es', {hour:'2-digit', minute:'2-digit'});
    el.innerHTML = imbHtml + wallsHtml + depthHtml + zonesHtml +
      `<div class="liq-ts">L2 snapshot · ${now}</div>`;

    // Cargar heatmap de liquidaciones (request separado, datos propios)
    _cargarHeatmap();

  } catch(err) {
    if (el) el.innerHTML = `<div style="font-size:11px;color:var(--text3)">Error cargando liquidez</div>`;
  } finally {
    if (btn) btn.style.opacity = '1';
  }
}

async function _cargarHeatmap() {
  const box = document.getElementById('liq-heatmap-box');
  if (!box) return;
  box.innerHTML = '<div style="font-size:10px;color:var(--text3);padding:6px 0;text-align:center">Calculando heatmap...</div>';
  try {
    const r = await fetch('/api/liquidity/heatmap');
    const d = await r.json();
    if (!d.ok || !d.bins || d.bins.length === 0) {
      box.innerHTML = '<div style="font-size:10px;color:var(--text3)">Heatmap no disponible</div>';
      return;
    }
    _renderHeatmap(box, d);
  } catch(e) {
    box.innerHTML = '<div style="font-size:10px;color:var(--text3)">Error heatmap</div>';
  }
}

function _renderHeatmap(box, d) {
  const bins      = d.bins;
  const precio    = d.precio_ref;
  const poc       = d.vp_poc;
  const vah       = d.vp_vah;
  const val       = d.vp_val;
  const liqLevels = d.liq_levels || [];

  // Mostrar solo el rango ±12% para no saturar la UI (bins más relevantes)
  const visible = bins.filter(b => Math.abs(b.pct) <= 12);

  // Mapa precio → nivel de leverage para overlay
  const levMap = {};
  liqLevels.forEach(l => { levMap[l.price] = l; });

  // Máximos para normalización visual
  const maxVol    = Math.max(...visible.map(b => b.vol), 0.01);
  const maxDLong  = Math.max(...visible.map(b => b.d_long), 0.01);
  const maxDShort = Math.max(...visible.map(b => b.d_short), 0.01);

  let rows = '';
  // Invertir para mostrar precios altos arriba
  [...visible].reverse().forEach(b => {
    const isPrice    = Math.abs(b.price - precio) / precio < 0.003;
    const isPoc      = Math.abs(b.price - poc) / poc < 0.003;
    const isVah      = Math.abs(b.price - vah) / vah < 0.003;
    const isVal      = Math.abs(b.price - val) / val < 0.003;
    const liqInfo    = liqLevels.find(l => Math.abs(l.price - b.price) / b.price < 0.003);

    // Densidad como opacidad de color
    const longDens  = b.d_long  / maxDLong;
    const shortDens = b.d_short / maxDShort;
    const volW      = Math.round((b.vol / maxVol) * 52);   // px para barra de volumen
    const wickDots  = b.wicks > 0 ? '·'.repeat(Math.min(b.wicks, 5)) : '';

    // Color de fondo: rojo para short density, verde para long density
    let bgStyle = '';
    if (shortDens > 0.08)
      bgStyle = `background:rgba(239,68,68,${(shortDens * 0.35).toFixed(2)})`;
    else if (longDens > 0.08)
      bgStyle = `background:rgba(34,197,94,${(longDens * 0.35).toFixed(2)})`;

    const priceColor = b.pct > 0 ? 'var(--red)' : b.pct < 0 ? 'var(--green)' : 'var(--accent)';
    const priceFmt   = `$${b.price.toLocaleString()}`;
    const pctFmt     = `${b.pct > 0 ? '+' : ''}${b.pct.toFixed(1)}%`;

    // Badges: POC, VAH, VAL, precio actual, leverage levels
    let badges = '';
    if (isPrice)     badges += `<span style="font-size:8px;background:var(--accent);color:#000;padding:0 3px;border-radius:2px;font-weight:700">NOW</span> `;
    if (isPoc)       badges += `<span style="font-size:8px;background:#8b5cf6;color:#fff;padding:0 3px;border-radius:2px">POC</span> `;
    if (isVah)       badges += `<span style="font-size:8px;background:rgba(109,148,197,.3);color:var(--accent);padding:0 3px;border-radius:2px">VAH</span> `;
    if (isVal)       badges += `<span style="font-size:8px;background:rgba(109,148,197,.3);color:var(--accent);padding:0 3px;border-radius:2px">VAL</span> `;
    if (liqInfo)     badges += `<span style="font-size:8px;background:${liqInfo.side==='long'?'rgba(34,197,94,.2)':'rgba(239,68,68,.2)'};color:${liqInfo.side==='long'?'var(--green)':'var(--red)'};padding:0 3px;border-radius:2px">${liqInfo.lev}</span> `;
    if (b.wicks > 0) badges += `<span style="font-size:8px;color:#f59e0b" title="${b.wicks} wicks históricos">${wickDots}</span>`;

    rows += `
      <div style="display:flex;align-items:center;gap:3px;height:16px;padding:0 4px;${isPrice?'border-top:1px solid var(--accent);border-bottom:1px solid var(--accent);':''}${bgStyle}">
        <span style="width:56px;font-size:9px;font-family:var(--mono);color:${isPrice?'var(--accent)':priceColor};flex-shrink:0">${priceFmt}</span>
        <span style="width:30px;font-size:8px;color:var(--text3);flex-shrink:0">${pctFmt}</span>
        <div style="width:${volW}px;height:5px;background:var(--text3);opacity:.35;border-radius:2px;flex-shrink:0"></div>
        <div style="flex:1;font-size:8px;white-space:nowrap;overflow:hidden">${badges}</div>
      </div>`;
  });

  const pocPct  = poc  ? `$${poc.toLocaleString()}`  : '—';
  const vahPct  = vah  ? `$${vah.toLocaleString()}`  : '—';
  const valPct  = val  ? `$${val.toLocaleString()}`  : '—';

  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span style="font-size:10px;font-weight:700;color:var(--text2)">Liquidation Heatmap</span>
      <div style="display:flex;gap:8px;font-size:8.5px;color:var(--text3)">
        <span><span style="color:#8b5cf6">■</span> POC ${pocPct}</span>
        <span><span style="color:var(--accent)">■</span> VA ${valPct}–${vahPct}</span>
      </div>
    </div>
    <div style="display:flex;gap:6px;margin-bottom:5px;font-size:8.5px;color:var(--text3)">
      <span style="display:flex;align-items:center;gap:3px"><span style="width:10px;height:10px;background:rgba(34,197,94,.3);border-radius:2px;display:inline-block"></span>Long liq</span>
      <span style="display:flex;align-items:center;gap:3px"><span style="width:10px;height:10px;background:rgba(239,68,68,.3);border-radius:2px;display:inline-block"></span>Short liq</span>
      <span style="display:flex;align-items:center;gap:3px"><span style="width:18px;height:4px;background:var(--text3);opacity:.4;border-radius:2px;display:inline-block"></span>Vol</span>
      <span><span style="color:#f59e0b">·</span> Wicks hist.</span>
    </div>
    <div style="overflow-y:auto;max-height:380px;border:1px solid var(--border);border-radius:6px">${rows}</div>
    <div style="font-size:8.5px;color:var(--text3);margin-top:5px">Leverage math + vol profile 500H + wicks · no posiciones reales</div>`;
}
