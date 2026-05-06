
// ─── Edge Analytics — carga y renderiza los datos ────────────────────────────

const DOW_ORDEN  = ["Lunes","Martes","Miércoles","Jueves","Viernes","Sábado","Domingo"];
const DOW_CORTO  = ["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"];
const MES_ORDEN  = ["Enero","Febrero","Marzo","Abril","Mayo","Junio",
                    "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"];
const MES_CORTO  = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"];

function colorRet(val) {
  if (val > 0) return 'c-up';
  if (val < 0) return 'c-down';
  return 'c-neut';
}
function fmtRet(val) {
  if (val === null || val === undefined) return '—';
  const s = val >= 0 ? '+' : '';
  return s + val.toFixed(2) + '%';
}
function fmtPct(val) {
  if (val === null || val === undefined) return '—';
  return val.toFixed(1) + '%';
}

// ── KILL ZONES ────────────────────────────────────────────────────────────────
let _kzData = null;  // cache para re-render al cambiar timezone

function renderKillZones(kz) {
  _kzData = kz;
  const offset = parseInt(document.getElementById('kz-tz')?.value ?? '0');

  // Subtitle dinámico
  const tzLabel = offset === 0 ? 'UTC+0' : (offset > 0 ? `UTC+${offset}` : `UTC${offset}`);
  const subEl = document.getElementById('kz-sub');
  if (subEl) subEl.textContent = `Movimiento promedio % por hora · ${tzLabel} (histórico 1 año)`;

  // Badge sesión activa (siempre UTC — es la hora real del mercado)
  const badgeEl = document.getElementById('kz-badge');
  const activa  = kz.kill_zone_activa_ahora;
  const esKZ    = kz.es_kill_zone_activa;
  badgeEl.innerHTML = esKZ
    ? `<span class="e-badge active">&#9889; KILL ZONE ACTIVA — ${activa}</span>`
    : `<span class="e-badge neutral">${activa} — Fuera de kill zone principal</span>`;

  // Barras por hora — orden rotado por offset
  const horas  = kz.avg_move_by_hour;
  const top5   = Object.keys(kz.top_5_kill_zones).map(Number);
  const maxVal = Math.max(...Object.values(horas));
  const barsEl = document.getElementById('kz-bars');
  let html = '';
  for (let i = 0; i < 24; i++) {
    // i = display slot (local hour), h = UTC hour source
    const h      = ((i - offset) % 24 + 24) % 24;
    const val    = horas[h] || 0;
    const pct    = maxVal > 0 ? (val / maxVal * 100).toFixed(1) : 0;
    const isTop  = top5.includes(h);
    const label  = String(i).padStart(2,'0') + ':00';
    html += `
      <div class="kz-bar-row">
        <span class="kz-bar-label">${label}</span>
        <div class="kz-bar-track">
          <div class="kz-bar-fill ${isTop ? 'top5' : ''}" style="width:${pct}%"></div>
        </div>
        <span class="kz-bar-val">${fmtPct(val)}</span>
      </div>`;
  }
  barsEl.innerHTML = html;

  // Sesiones
  const sa = kz.sesion_avg;
  document.getElementById('kz-sessions').innerHTML = `
    <div class="kz-session-pill">
      <div class="kz-session-name">Asia</div>
      <div class="kz-session-val">${fmtPct(sa.Asia)}</div>
    </div>
    <div class="kz-session-pill">
      <div class="kz-session-name">London</div>
      <div class="kz-session-val">${fmtPct(sa.London)}</div>
    </div>
    <div class="kz-session-pill">
      <div class="kz-session-name">NY</div>
      <div class="kz-session-val">${fmtPct(sa.NY)}</div>
    </div>`;
}

// ── SWEEP RETURNS ─────────────────────────────────────────────────────────────
function renderSweeps(sw) {
  const rate = sw.global_reversal_rate;
  const rateEl = document.getElementById('sweep-global-rate');
  rateEl.textContent = fmtPct(rate);
  rateEl.className = 'sweep-big-num ' + (rate >= 60 ? 'c-up' : rate >= 50 ? 'c-amber' : 'c-down');

  document.getElementById('sweep-bsl-rate').textContent  = fmtPct(sw.bsl?.reversal_rate);
  document.getElementById('sweep-bsl-ret').textContent   = fmtRet(sw.bsl?.avg_return_4h);
  document.getElementById('sweep-bsl-total').textContent = sw.bsl?.total ?? '—';
  document.getElementById('sweep-ssl-rate').textContent  = fmtPct(sw.ssl?.reversal_rate);
  document.getElementById('sweep-ssl-ret').textContent   = fmtRet(sw.ssl?.avg_return_4h);
  document.getElementById('sweep-ssl-total').textContent = sw.ssl?.total ?? '—';
  document.getElementById('sweep-note').textContent      = `Basado en ${sw.total_sweeps} sweeps detectados en datos históricos`;
}

// ── POST BIG MOVE ─────────────────────────────────────────────────────────────
function renderPostBigMove(pbm) {
  // Banner
  const bannerEl = document.getElementById('pbm-banner');
  if (pbm.big_move_hoy) {
    const cls = pbm.tipo_hoy === 'UP' ? 'up' : 'down';
    const sign = pbm.tipo_hoy === 'UP' ? '+' : '';
    bannerEl.innerHTML = `<div class="pbm-banner ${cls}">&#9889; BIG MOVE HOY: ${sign}${pbm.retorno_hoy_pct}% — Contexto histórico activo</div>`;
  } else {
    bannerEl.innerHTML = `<div class="pbm-banner none">Sin big move hoy (umbral >${pbm.umbral_pct}%)</div>`;
  }

  // Tablas
  const up = pbm.after_big_up || {};
  const dn = pbm.after_big_down || {};
  document.getElementById('pbm-up-1d').innerHTML = `<span class="${colorRet(up['1d_avg'])}">${fmtRet(up['1d_avg'])}</span> <span style="font-size:10px;color:var(--text3)">(${fmtPct(up['1d_positive_rate'])} pos.)</span>`;
  document.getElementById('pbm-up-3d').innerHTML = `<span class="${colorRet(up['3d_avg'])}">${fmtRet(up['3d_avg'])}</span> <span style="font-size:10px;color:var(--text3)">(${fmtPct(up['3d_positive_rate'])} pos.)</span>`;
  document.getElementById('pbm-up-7d').innerHTML = `<span class="${colorRet(up['7d_avg'])}">${fmtRet(up['7d_avg'])}</span> <span style="font-size:10px;color:var(--text3)">(${fmtPct(up['7d_positive_rate'])} pos.)</span>`;
  document.getElementById('pbm-dn-1d').innerHTML = `<span class="${colorRet(dn['1d_avg'])}">${fmtRet(dn['1d_avg'])}</span> <span style="font-size:10px;color:var(--text3)">(${fmtPct(dn['1d_positive_rate'])} pos.)</span>`;
  document.getElementById('pbm-dn-3d').innerHTML = `<span class="${colorRet(dn['3d_avg'])}">${fmtRet(dn['3d_avg'])}</span> <span style="font-size:10px;color:var(--text3)">(${fmtPct(dn['3d_positive_rate'])} pos.)</span>`;
  document.getElementById('pbm-dn-7d').innerHTML = `<span class="${colorRet(dn['7d_avg'])}">${fmtRet(dn['7d_avg'])}</span> <span style="font-size:10px;color:var(--text3)">(${fmtPct(dn['7d_positive_rate'])} pos.)</span>`;

  // Interpretación
  const interp3d = up['3d_avg'];
  document.getElementById('pbm-interp').textContent = interp3d >= 0
    ? 'Históricamente alcista 3D post big up'
    : 'Históricamente bajista 3D post big up (mean reversion)';
}

// ── SESGO TEMPORAL ────────────────────────────────────────────────────────────
function renderSesgoTemporal(dow, monthly) {
  // Día de la semana
  const diaActual = dow.dia_actual;
  let dowHtml = '';
  DOW_ORDEN.forEach((dia, i) => {
    const d = dow[dia] || {};
    const isToday = dia === diaActual;
    const ret = d.avg_return;
    dowHtml += `
      <div class="dow-cell ${isToday ? 'today' : ''}">
        <div class="dow-name">${DOW_CORTO[i]}</div>
        <div class="dow-ret ${colorRet(ret)}">${ret !== undefined ? fmtRet(ret) : '—'}</div>
        <div class="dow-pr">${d.positive_rate !== undefined ? fmtPct(d.positive_rate) : ''}</div>
      </div>`;
  });
  document.getElementById('dow-grid').innerHTML = dowHtml;

  // Mes del año
  const mesActual = monthly.mes_actual;
  let monthHtml = '';
  MES_ORDEN.forEach((mes, i) => {
    const m = monthly[mes] || {};
    const isCurrent = mes === mesActual;
    const ret = m.avg_daily_return;
    monthHtml += `
      <div class="month-cell ${isCurrent ? 'current' : ''}">
        <div class="month-name">${MES_CORTO[i]}</div>
        <div class="month-ret ${colorRet(ret)}">${ret !== undefined ? fmtRet(ret) : '—'}</div>
      </div>`;
  });
  document.getElementById('month-grid').innerHTML = monthHtml;
}

// ── VOLATILIDAD ───────────────────────────────────────────────────────────────
function renderVolatilidad(vol) {
  document.getElementById('vol-7d').textContent   = vol.vol_7d_anualizada  + '%';
  document.getElementById('vol-30d').textContent  = vol.vol_30d_anualizada + '%';
  document.getElementById('vol-hist').textContent = vol.vol_historica_anualizada + '%';

  const pct  = vol.percentil_vol_actual;
  const interp = (vol.interpretacion || '').toUpperCase();
  document.getElementById('vol-percentil-txt').textContent = pct + '% — ' + interp;

  const barEl = document.getElementById('vol-bar');
  barEl.style.width = pct + '%';
  barEl.className = 'vol-bar-fill ' + interp.toLowerCase();

  const implMap = {
    'ALTA':   'En volatilidad ALTA → Kelly fraccional: reducir tamaño de posición',
    'BAJA':   'En volatilidad BAJA → posible compresión antes de breakout',
    'NORMAL': 'Volatilidad en rango normal → sizing estándar'
  };
  document.getElementById('vol-implication').textContent = implMap[interp] || '';
}

// ── CME GAP ───────────────────────────────────────────────────────────────────
function renderCME(cme) {
  const gap = cme.gap_activo;
  let html  = '<div class="bias-section-title">CME Gap</div>';

  if (gap) {
    const cls = 'cme-gap-box active-gap';
    const color = gap.gap_tipo === 'UP' ? 'c-up' : 'c-down';
    html += `
      <div class="${cls}">
        <div class="cme-gap-level ${color}">$${gap.nivel_gap.toLocaleString()}</div>
        <div class="cme-gap-meta">
          Tipo: <strong>${gap.gap_tipo}</strong> &nbsp;·&nbsp;
          Gap: <strong>${gap.gap_pct > 0 ? '+' : ''}${gap.gap_pct}%</strong> &nbsp;·&nbsp;
          Distancia: <strong>${cme.distancia_al_gap_pct}%</strong>
        </div>
        <span class="e-badge warn">GAP ACTIVO — fill rate histórico: ${cme.fill_rate_historico}%</span>
      </div>`;
  } else {
    html += `<div class="cme-gap-box"><span style="color:var(--text3);font-size:12.5px">Sin gap CME activo</span></div>`;
  }

  // Tabla últimos 5 gaps
  if (cme.gaps_recientes && cme.gaps_recientes.length) {
    html += `
      <table class="cme-table">
        <thead><tr>
          <th>Fecha</th><th>Nivel</th><th>Tipo</th><th>Gap%</th><th>Llenado</th>
        </tr></thead><tbody>`;
    cme.gaps_recientes.slice().reverse().forEach(g => {
      html += `<tr>
        <td>${g.fecha}</td>
        <td>$${g.nivel_gap.toLocaleString()}</td>
        <td>${g.gap_tipo}</td>
        <td>${g.gap_pct > 0 ? '+' : ''}${g.gap_pct}%</td>
        <td class="${g.llenado ? 'llenado-si' : 'llenado-no'}">${g.llenado ? 'Sí' : 'No'}</td>
      </tr>`;
    });
    html += '</tbody></table>';
  }

  document.getElementById('cme-section').innerHTML = html;
}

// ── HALVING ───────────────────────────────────────────────────────────────────
function renderHalving(h) {
  document.getElementById('halving-fecha').textContent   = h.ultimo_halving;
  document.getElementById('halving-dias').textContent    = h.dias_desde_halving + ' días';
  document.getElementById('halving-proximo').textContent = h.proximo_halving_estimado + ' (' + h.dias_para_proximo + ' días)';
  document.getElementById('halving-fase').textContent    = h.fase_ciclo;

  // Barra de progreso: 0 días → 0%, ~1400 días (ciclo aprox) → 100%
  const ciclo = 1400;
  const pct   = Math.min((h.dias_desde_halving / ciclo) * 100, 100).toFixed(1);
  document.getElementById('halving-bar').style.width = pct + '%';
}

// ── SESSION DEEP DIVE ─────────────────────────────────────────────────────────
function renderSessionDive(sess) {
  const h   = sess.historico || {};
  const hoy = sess.hoy       || {};

  document.getElementById('sess-dias').textContent = h.dias_analizados || '—';

  // Badge sesión actual
  const badgeEl  = document.getElementById('sess-hoy-badge');
  const sesActual = hoy.sesion_actual || '—';
  const badgeCls  = sesActual === 'NY' ? 'active' : sesActual === 'London' ? 'warn' : sesActual === 'Asia' ? 'neutral' : 'neutral';
  badgeEl.innerHTML = `<span class="e-badge ${badgeCls}">${sesActual} — ${hoy.hora_utc}:00 UTC</span>`;

  // Asia range hoy
  document.getElementById('sess-sesion-actual').textContent = sesActual;
  document.getElementById('sess-asia-high').textContent  = hoy.asia_high  ? '$' + hoy.asia_high.toLocaleString()  : '—';
  document.getElementById('sess-asia-low').textContent   = hoy.asia_low   ? '$' + hoy.asia_low.toLocaleString()   : '—';
  document.getElementById('sess-asia-range').textContent = hoy.asia_range_pct != null ? hoy.asia_range_pct + '%' : '—';

  // Sweeps del día
  const sweepsEl = document.getElementById('sess-sweeps-hoy');
  const sweeps = [];
  if (hoy.london_broke_high) sweeps.push({ txt: 'London barrio Asia High', cls: 'warn' });
  if (hoy.london_broke_low)  sweeps.push({ txt: 'London barrio Asia Low',  cls: 'warn' });
  if (hoy.ny_broke_high)     sweeps.push({ txt: 'NY barrio Asia High',     cls: 'active' });
  if (hoy.ny_broke_low)      sweeps.push({ txt: 'NY barrio Asia Low',      cls: 'active' });
  sweepsEl.innerHTML = sweeps.length
    ? sweeps.map(s => `<span class="e-badge ${s.cls}">${s.txt}</span>`).join('')
    : `<span class="e-badge neutral">Sin sweeps confirmados aun</span>`;

  // London stats
  const lonHighRate = h.london_broke_high_rate;
  const lonHighEl   = document.getElementById('sess-lon-high-rate');
  lonHighEl.textContent = lonHighRate != null ? lonHighRate + '%' : '—';
  lonHighEl.className   = lonHighRate >= 40 ? 'c-up' : 'c-amber';
  lonHighEl.style.cssText = 'font-size:32px;font-family:var(--mono);font-weight:700';

  document.getElementById('sess-lon-low-rate').textContent  = fmtPct(h.london_broke_low_rate);
  document.getElementById('sess-lon-both-rate').textContent = fmtPct(h.london_broke_both_rate);
  document.getElementById('sess-lon-ratio').textContent     = h.avg_london_vs_asia_ratio != null ? h.avg_london_vs_asia_ratio + 'x' : '—';

  const nyPostHighEl = document.getElementById('sess-ny-post-high');
  nyPostHighEl.innerHTML = `<span class="${colorRet(h.ny_ret_after_london_high)}">${fmtRet(h.ny_ret_after_london_high)}</span> <span style="font-size:10px;color:var(--text3)">(${fmtPct(h.ny_positive_after_london_high)} pos.)</span>`;
  const nyPostLowEl  = document.getElementById('sess-ny-post-low');
  nyPostLowEl.innerHTML  = `<span class="${colorRet(h.ny_ret_after_london_low)}">${fmtRet(h.ny_ret_after_london_low)}</span> <span style="font-size:10px;color:var(--text3)">(${fmtPct(h.ny_positive_after_london_low)} pos.)</span>`;

  // NY stats
  const nyHighRate = h.ny_broke_high_rate;
  const nyHighEl   = document.getElementById('sess-ny-high-rate');
  nyHighEl.textContent = nyHighRate != null ? nyHighRate + '%' : '—';
  nyHighEl.className   = nyHighRate >= 50 ? 'c-up' : 'c-amber';
  nyHighEl.style.cssText = 'font-size:32px;font-family:var(--mono);font-weight:700';

  document.getElementById('sess-ny-low-rate').textContent = fmtPct(h.ny_broke_low_rate);

  // Insight automático
  const insight = [];
  if (lonHighRate > 35)
    insight.push('London rompe Asia High el ' + lonHighRate + '% de los días — zona de liquidez frecuente sobre Asia High.');
  if (h.ny_broke_low_rate > 60)
    insight.push('NY tiende a barrer el Low de Asia (' + h.ny_broke_low_rate + '% días) — SSL bajo Asia Low suele ser target.');
  if (h.ny_ret_after_london_high < 0)
    insight.push('Cuando London barre el High, NY cierra bajista ' + (100 - h.ny_positive_after_london_high) + '% de las veces — posible mean reversion.');
  document.getElementById('sess-insight').textContent = insight.length
    ? insight.join(' ')
    : 'Acumulando datos históricos...';
}

// ── FOMC ──────────────────────────────────────────────────────────────────────
function renderFOMC(fomc) {
  const h = fomc.historico || {};
  document.getElementById('fomc-total').textContent = h.total_fomc_analizados || '—';

  // Badge estado
  const badgeEl = document.getElementById('fomc-estado-badge');
  if (fomc.es_dia_fomc) {
    badgeEl.innerHTML = '<span class="e-badge danger">HOY ES DÍA FOMC</span>';
  } else if (fomc.es_semana_fomc) {
    badgeEl.innerHTML = `<span class="e-badge warn">SEMANA FOMC — ${fomc.dias_para_proximo} días</span>`;
  } else {
    badgeEl.innerHTML = `<span class="e-badge neutral">Fuera de semana FOMC</span>`;
  }

  document.getElementById('fomc-ultimo').textContent     = fomc.ultimo_fomc || '—';
  document.getElementById('fomc-dias-desde').textContent = fomc.dias_desde_ultimo != null ? fomc.dias_desde_ultimo + ' días' : '—';
  document.getElementById('fomc-proximo').textContent    = fomc.proximo_fomc || '—';
  document.getElementById('fomc-dias-para').textContent  = fomc.dias_para_proximo != null ? fomc.dias_para_proximo + ' días' : '—';

  // Expansión de rango
  const expEl = document.getElementById('fomc-expansion');
  const exp   = h.range_expansion_ratio;
  expEl.textContent = exp != null ? exp + 'x' : '—';
  expEl.className   = exp >= 1.2 ? 'c-amber' : exp >= 1.0 ? 'c-up' : 'c-neut';
  expEl.style.cssText = 'font-size:36px;font-family:var(--mono);font-weight:700';

  document.getElementById('fomc-rango-fomc').textContent   = h.avg_range_fomc_pct   != null ? h.avg_range_fomc_pct + '%'   : '—';
  document.getElementById('fomc-rango-normal').textContent = h.avg_range_normal_pct != null ? h.avg_range_normal_pct + '%' : '—';

  // Retornos
  const pre2El  = document.getElementById('fomc-pre2');
  const diaEl   = document.getElementById('fomc-dia');
  const post1El = document.getElementById('fomc-post1');
  const post3El = document.getElementById('fomc-post3');

  pre2El.textContent  = fmtRet(h.retorno_pre2_avg);
  pre2El.className    = colorRet(h.retorno_pre2_avg);
  diaEl.textContent   = fmtRet(h.retorno_dia_fomc_avg);
  diaEl.className     = colorRet(h.retorno_dia_fomc_avg);
  post1El.textContent = fmtRet(h.retorno_post1_avg);
  post1El.className   = colorRet(h.retorno_post1_avg);
  post3El.textContent = fmtRet(h.retorno_post3_avg);
  post3El.className   = colorRet(h.retorno_post3_avg);

  document.getElementById('fomc-post1-pr').textContent = h.positive_rate_post1 != null ? ' (' + h.positive_rate_post1 + '% pos.)' : '';
  document.getElementById('fomc-post3-pr').textContent = h.positive_rate_post3 != null ? ' (' + h.positive_rate_post3 + '% pos.)' : '';
}

// ── MAIN: fetch y render ──────────────────────────────────────────────────────
async function cargarEdgeStats() {
  try {
    const res  = await fetch('/api/edge/stats');
    const data = await res.json();

    if (!data.ok) throw new Error(data.error || 'Error desconocido');

    const s = data.stats;

    // Timestamp de cálculo
    if (s.calculado_en) {
      const d = new Date(s.calculado_en + 'Z');
      document.getElementById('edge-calc-time').textContent =
        'Calculado: ' + d.toLocaleDateString('es') + ' ' + d.toLocaleTimeString('es', {hour:'2-digit',minute:'2-digit'});
    }

    renderKillZones(s.kill_zones);
    renderSweeps(s.sweep_returns);
    renderPostBigMove(s.post_big_move);
    renderSesgoTemporal(s.day_of_week, s.monthly_bias);
    renderVolatilidad(s.volatilidad);
    renderCME(s.cme_gap);
    renderHalving(s.halving);
    if (s.session_dive) renderSessionDive(s.session_dive);
    if (s.fomc)         renderFOMC(s.fomc);

    document.getElementById('edge-loading').style.display = 'none';
    document.getElementById('edge-grid').style.display    = 'grid';

  } catch (err) {
    console.error('Edge Analytics error:', err);
    document.getElementById('edge-loading').style.display = 'none';
    document.getElementById('edge-error').style.display   = 'block';
  }
}

// ── FEAR & GREED ──────────────────────────────────────────────────────────────
async function cargarFNG() {
  try {
    const res  = await fetch('/api/fng');
    const json = await res.json();
    if (!json.ok || !json.data || !json.data.valor) return;
    const d = json.data;
    const v = d.valor;

    // Badge
    let badgeClass, badgeLabel;
    if      (v <= 25) { badgeClass = 'danger';  badgeLabel = 'MIEDO EXTREMO'; }
    else if (v <= 45) { badgeClass = 'warn';    badgeLabel = 'MIEDO'; }
    else if (v <= 55) { badgeClass = 'neutral'; badgeLabel = 'NEUTRO'; }
    else if (v <= 75) { badgeClass = 'warn';    badgeLabel = 'CODICIA'; }
    else              { badgeClass = 'danger';  badgeLabel = 'CODICIA EXTREMA'; }
    document.getElementById('fng-badge').innerHTML =
      `<span class="e-badge ${badgeClass}" style="justify-content:center;width:100%;box-sizing:border-box">${badgeLabel}</span>`;

    document.getElementById('fng-valor').textContent   = v;
    document.getElementById('fng-clasif').textContent  = d.clasificacion || '';
    const t = d.tendencia_7d || 0;
    document.getElementById('fng-tendencia').textContent =
      'Tendencia 7D: ' + (t >= 0 ? '+' : '') + t + ' pts';

    // Sparkline
    const hist = (d.hist_30d || d.hist_7d || []).slice().reverse(); // oldest → newest
    if (hist.length >= 2) {
      const W = 300, H = 72, P = 5;
      const n   = hist.length;
      const min = Math.min(...hist);
      const max = Math.max(...hist);
      const rng = max - min || 1;
      const px  = (i) => (P + (i / (n - 1)) * (W - P * 2)).toFixed(1);
      const py  = (v) => (P + (1 - (v - min) / rng) * (H - P * 2)).toFixed(1);

      const pts = hist.map((val, i) => `${px(i)},${py(val)}`).join(' ');

      // Zone backgrounds (clipped to actual data range)
      const zones = [
        [0,  25,  'rgba(239,68,68,.10)'],
        [25, 45,  'rgba(251,191,36,.09)'],
        [45, 55,  'rgba(150,150,150,.06)'],
        [55, 75,  'rgba(251,191,36,.09)'],
        [75, 100, 'rgba(34,197,94,.10)'],
      ];
      let rects = '';
      for (const [lo, hi, fill] of zones) {
        const y1 = Math.max(P, Math.min(H - P, parseFloat(py(Math.min(hi, max)))));
        const y2 = Math.max(P, Math.min(H - P, parseFloat(py(Math.max(lo, min)))));
        if (y2 > y1) rects += `<rect x="${P}" y="${y1.toFixed(1)}" width="${W - P * 2}" height="${(y2 - y1).toFixed(1)}" fill="${fill}"/>`;
      }

      const lastV = hist[hist.length - 1];
      const lc = lastV <= 25 ? 'var(--red)'   : lastV <= 45 ? 'var(--amber)'
               : lastV <= 55 ? 'var(--text3)' : lastV <= 75 ? 'var(--amber)' : 'var(--green)';

      document.getElementById('fng-spark').innerHTML =
        `${rects}<polyline points="${pts}" fill="none" stroke="${lc}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>` +
        `<circle cx="${px(n-1)}" cy="${py(lastV)}" r="3" fill="${lc}"/>`;
    }

    document.getElementById('fng-loading').style.display = 'none';
    document.getElementById('fng-body').style.display    = 'grid';
  } catch (e) {
    document.getElementById('fng-loading').textContent = 'No disponible';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  // Restaurar timezone guardado
  const savedTz = localStorage.getItem('kz_tz');
  const tzSel = document.getElementById('kz-tz');
  if (savedTz && tzSel) tzSel.value = savedTz;

  // Re-render al cambiar timezone
  tzSel?.addEventListener('change', () => {
    localStorage.setItem('kz_tz', tzSel.value);
    if (_kzData) renderKillZones(_kzData);
  });

  cargarEdgeStats();
  cargarFNG();
  cargarMultiTF();
});

// ── Multi-TF Alignment ───────────────────────────────────────────────────────

async function cargarMultiTF() {
  const box = document.getElementById('multitf-content');
  box.innerHTML = '<div style="font-size:11px;color:var(--text3);padding:4px 0">Calculando...</div>';
  try {
    const r = await fetch('/api/scanner/multitf');
    const d = await r.json();
    if (!d.ok) { box.innerHTML = `<div style="color:var(--red);font-size:11px">${d.error||'Error'}</div>`; return; }

    const htf = d.htf || {};
    const ltf = d.ltf;
    const alin = d.alineacion || 'INDEFINIDO';

    const alinColor = {
      'CONFLUENCIA': 'var(--green)',
      'ESPERA':      'var(--amber)',
      'DIVERGENTE':  'var(--red)',
      'INDEFINIDO':  'var(--text3)',
    }[alin] || 'var(--text3)';

    const biasColor = b => b === 'ALCISTA' ? 'var(--green)' : b === 'BAJISTA' ? 'var(--red)' : 'var(--text3)';

    let html = `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
        <div style="background:var(--bg3);border-radius:6px;padding:8px">
          <div style="font-size:9px;color:var(--text3);font-weight:700;letter-spacing:.6px;margin-bottom:4px">HTF 4H</div>
          <div style="font-size:12px;font-weight:700;color:${biasColor(htf.bias)}">${htf.bias||'—'}</div>
          <div style="font-size:10px;font-family:var(--mono);color:var(--text2)">${htf.score||0}/100</div>
          <div style="font-size:9px;color:var(--text3);margin-top:2px">${htf.confluencias_ok||0}/8 conf</div>
        </div>
        <div style="background:var(--bg3);border-radius:6px;padding:8px">
          <div style="font-size:9px;color:var(--text3);font-weight:700;letter-spacing:.6px;margin-bottom:4px">LTF 15M</div>`;

    if (ltf) {
      html += `
          <div style="font-size:12px;font-weight:700;color:${biasColor(ltf.bias)}">${ltf.bias||'—'}</div>
          <div style="font-size:10px;font-family:var(--mono);color:var(--text2)">${ltf.score||0}/100</div>
          <div style="font-size:9px;color:var(--text3);margin-top:2px">${ltf.confluencias_ok||0}/8 conf</div>`;
    } else {
      html += `<div style="font-size:10px;color:var(--text3)">Sin datos</div>`;
    }

    html += `</div></div>
      <div style="background:var(--bg3);border-radius:6px;padding:8px;margin-bottom:6px;text-align:center">
        <div style="font-size:9px;color:var(--text3);margin-bottom:3px">ALINEACIÓN</div>
        <div style="font-size:13px;font-weight:700;color:${alinColor}">${alin}</div>
      </div>
      <div style="font-size:10px;color:var(--text2);line-height:1.4;padding:0 2px">${d.trigger||''}</div>
      <div style="font-size:9px;color:var(--text3);margin-top:6px;text-align:right">${d.timestamp||''}</div>`;

    box.innerHTML = html;
  } catch(e) {
    box.innerHTML = `<div style="color:var(--red);font-size:11px">Error: ${e.message}</div>`;
  }
}

// ── LLM Context Debug ─────────────────────────────────────────────────────────

async function cargarContextDebug() {
  const tf  = document.getElementById('ctx-tf-select')?.value || '4h';
  const box = document.getElementById('ctx-debug-content');
  box.innerHTML = '<div style="font-size:11px;color:var(--text3);padding:8px 0">Cargando...</div>';

  try {
    const r = await fetch(`/api/context-debug?symbol=BTC%2FUSDT&tf=${tf}`);
    const d = await r.json();
    if (!d.ok) { box.innerHTML = `<div style="color:var(--red);font-size:11px">${d.error}</div>`; return; }

    const rows = [];

    // Delta
    const delta = d.delta || {};
    rows.push(_ctxSection(
      'ORDER FLOW',
      delta.error
        ? `<span style="color:var(--red)">${delta.error}</span>`
        : (delta.texto || '<span style="color:var(--text3)">Sin datos</span>'),
      delta.velas
    ));

    // Volume Profile
    const vp = d.volume_profile || {};
    const vpDatos = vp.datos || {};
    let vpExtra = '';
    if (!vp.error && vpDatos.vpoc) {
      vpExtra = `
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-top:6px">
          ${_vpPill('VPOC',  vpDatos.vpoc)}
          ${_vpPill('VAH',   vpDatos.value_area_high)}
          ${_vpPill('VAL',   vpDatos.value_area_low)}
          <div style="background:var(--bg3);border-radius:4px;padding:4px 6px">
            <div style="font-size:9px;color:var(--text3)">Vol</div>
            <div style="font-size:10.5px;font-family:var(--mono);color:var(--text)">${vpDatos.vol_total ? (vpDatos.vol_total/1e3).toFixed(1)+'K' : '—'}</div>
          </div>
        </div>`;
      if (vpDatos.hvn?.length)
        vpExtra += `<div style="font-size:10px;color:var(--text3);margin-top:5px">HVN: ${vpDatos.hvn.map(p=>'$'+p.toLocaleString()).join(' / ')}</div>`;
      if (vpDatos.lvn?.length)
        vpExtra += `<div style="font-size:10px;color:var(--amber);margin-top:2px">LVN: ${vpDatos.lvn.map(p=>'$'+p.toLocaleString()).join(' / ')}</div>`;
    }
    rows.push(_ctxSection(
      'VOLUME PROFILE',
      vp.error
        ? `<span style="color:var(--red)">${vp.error}</span>`
        : (vp.texto || '<span style="color:var(--text3)">Sin datos</span>'),
      null,
      vpExtra
    ));

    // HTF
    const htf = d.htf || {};
    rows.push(_ctxSection(
      'HTF SUPERIOR',
      htf.error
        ? `<span style="color:var(--red)">${htf.error}</span>`
        : (htf.texto || '<span style="color:var(--text3)">Sin TF superior para ' + tf + '</span>')
    ));

    box.innerHTML = rows.join('');
  } catch(e) {
    box.innerHTML = `<div style="color:var(--red);font-size:11px">Error: ${e.message}</div>`;
  }
}

function _ctxSection(label, texto, velas, extra) {
  let velaHtml = '';
  if (velas?.length) {
    velaHtml = '<div style="display:flex;gap:3px;margin-top:5px">';
    for (const v of velas) {
      const color = v.delta > 0 ? 'var(--green)' : v.delta < 0 ? 'var(--red)' : 'var(--text3)';
      const sign  = v.delta >= 0 ? '+' : '';
      velaHtml += `<div style="flex:1;background:var(--bg3);border-radius:4px;padding:4px 3px;text-align:center">
        <div style="font-size:9px;color:var(--text3)">${v.fecha?.slice(-5)}</div>
        <div style="font-size:10px;font-family:var(--mono);color:${color}">${sign}${(v.delta/1e3).toFixed(1)}K</div>
      </div>`;
    }
    velaHtml += '</div>';
  }
  return `<div style="margin-bottom:10px">
    <div style="font-size:9px;font-weight:700;letter-spacing:.8px;color:var(--text3);text-transform:uppercase;margin-bottom:4px">${label}</div>
    <div style="font-size:10.5px;color:var(--text2);line-height:1.5;white-space:pre-line;font-family:var(--mono)">${texto}</div>
    ${velaHtml}${extra||''}
  </div>`;
}

function _vpPill(label, val) {
  return `<div style="background:var(--bg3);border-radius:4px;padding:4px 6px">
    <div style="font-size:9px;color:var(--text3)">${label}</div>
    <div style="font-size:10.5px;font-family:var(--mono);color:var(--text)">${val !== undefined && val !== null ? '$'+Number(val).toLocaleString() : '—'}</div>
  </div>`;
}
