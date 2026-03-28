/* ═══════════════════════════════════════════════════════
   TRADEBOT AI — app.js
   Conecta el frontend con Flask backend
═══════════════════════════════════════════════════════ */

// ── Estado global ──
let esperando = false;
const MAX_CHARS = 1000;

// ── Elementos ──
const inputEl     = document.getElementById('input');
const sendBtn     = document.getElementById('send-btn');
const messagesEl  = document.getElementById('messages');
const welcomeEl   = document.getElementById('welcome');
const chatArea    = document.getElementById('chat-area');
const statusPill  = document.getElementById('status-pill');
const btnRefresh  = document.getElementById('btn-refresh');
const btnClear    = document.getElementById('btn-clear');
const sidebarEl   = document.getElementById('sidebar');
const sidebarToggle = document.getElementById('sidebar-toggle');

// ═══════════════════════════════════════
// SIDEBAR TOGGLE
// ═══════════════════════════════════════
sidebarToggle.addEventListener('click', () => {
  sidebarEl.classList.toggle('collapsed');
});

// ═══════════════════════════════════════
// INPUT — auto-resize + enable send btn
// ═══════════════════════════════════════
inputEl.addEventListener('input', () => {
  // Límite de caracteres
  if (inputEl.value.length > MAX_CHARS) {
    inputEl.value = inputEl.value.slice(0, MAX_CHARS);
  }
  // Auto-resize
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + 'px';
  // Habilitar botón solo si hay texto
  sendBtn.disabled = inputEl.value.trim() === '' || esperando;
  // Contador caracteres
  const contador = document.getElementById('char-count');
  if (contador) {
    const restantes = MAX_CHARS - inputEl.value.length;
    contador.textContent = restantes < 200 ? restantes + ' caracteres restantes' : '';
    contador.style.color = restantes < 50 ? 'var(--red)' : 'var(--text3)';
  }
});

// ── Aviso al refrescar solo si hay mensaje en proceso ──
window.addEventListener('beforeunload', (e) => {
  if (esperando) {
    e.preventDefault();
    e.returnValue = '¿Seguro? Hay una consulta en proceso.';
  }
});

inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (!sendBtn.disabled) enviar();
  }
});

sendBtn.addEventListener('click', enviar);

// ═══════════════════════════════════════
// CHIPS DE SUGERENCIA
// ═══════════════════════════════════════
function setInput(texto) {
  inputEl.value = texto;
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + 'px';
  sendBtn.disabled = false;
  inputEl.focus();
  enviar();
}

// ═══════════════════════════════════════
// ENVIAR MENSAJE
// ═══════════════════════════════════════
async function enviar() {
  const texto = inputEl.value.trim();
  if (!texto || esperando) return;

  esperando = true;
  sendBtn.disabled = true;
  // Fix 5: spinner en botón
  sendBtn.innerHTML = `<svg class="spin-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
    <path d="M21 12a9 9 0 11-6.219-8.56"/>
  </svg>`;
  inputEl.value = '';
  inputEl.style.height = 'auto';

  // Ocultar bienvenida
  if (welcomeEl) welcomeEl.style.display = 'none';

  // Mostrar mensaje usuario
  agregarMensaje('user', texto);

  // Status → pensando
  setStatus('thinking');

  // Mostrar typing
  const typingId = mostrarTyping();

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pregunta: texto })
    });

    // Fix 1: quitar typing DESPUÉS de recibir respuesta
    if (!res.ok) {
      quitarTyping(typingId);
      const errores = {
        429: '⏱️ Demasiadas solicitudes. Espera un momento antes de continuar.',
        500: '⚠️ Error interno del servidor. Intenta de nuevo en unos segundos.',
        503: '⚠️ Servicio temporalmente no disponible.',
      };
      const msg = errores[res.status] || `⚠️ Error del servidor (${res.status}). Intenta de nuevo.`;
      agregarMensajeError(msg);
      return;
    }

    const data = await res.json();
    quitarTyping(typingId);

    if (data.error) {
      agregarMensajeError('⚠️ ' + data.error);
    } else {
      agregarMensaje('bot', data.respuesta);
    }

  } catch (err) {
    quitarTyping(typingId);
    if (err instanceof TypeError && err.message.includes('fetch')) {
      agregarMensajeError('📡 Sin conexión con el servidor. Verifica que Flask esté corriendo en el puerto 5000.');
    } else {
      agregarMensajeError('⚠️ Error inesperado: ' + err.message + '. Recarga la página si persiste.');
    }
  } finally {
    // Fix 2: siempre restaurar estado del botón, pase lo que pase
    setStatus('online');
    esperando = false;
    sendBtn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>
    </svg>`;
    sendBtn.disabled = inputEl.value.trim() === '';
    inputEl.focus();
  }
}

// ═══════════════════════════════════════
// SANITIZAR TEXTO PLANO (anti-XSS)
// ═══════════════════════════════════════
function sanitize(str) {
  const el = document.createElement('div');
  el.textContent = str;
  return el.innerHTML;
}

// ═══════════════════════════════════════
// RENDERIZAR MARKDOWN BÁSICO (XSS-safe)
// Flujo: escapar todo → reemplazar tokens markdown
// por tags seguros. NUNCA interpolamos texto del
// usuario sin pasar por sanitize() primero.
// ═══════════════════════════════════════
function renderMarkdown(text) {
  // 1. Extraer bloques de código ANTES de escapar
  //    para preservarlos y escaparlos por separado
  const codeBlocks = [];
  text = text.replace(/```([\s\S]*?)```/g, (_, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push(sanitize(code));
    return `\x00CODE${idx}\x00`;
  });

  // 2. Escapar todo el texto restante
  let html = sanitize(text);

  // 3. Restaurar bloques de código (ya escapados)
  html = html.replace(/\x00CODE(\d+)\x00/g, (_, i) =>
    `<pre><code>${codeBlocks[parseInt(i)]}</code></pre>`
  );

  // 4. Markdown inline — operamos sobre texto ya escapado
  // Negrita
  html = html.replace(/\*\*(.*?)\*\*/g, (_, t) => `<strong>${t}</strong>`);
  // Cursiva
  html = html.replace(/\*(.*?)\*/g, (_, t) => `<em>${t}</em>`);
  // Código inline
  html = html.replace(/`([^`]+)`/g, (_, t) => `<code>${sanitize(t)}</code>`);

  // 5. Headings
  html = html.replace(/^### (.+)$/gm, '<h3 style="font-size:13px;font-weight:600;color:var(--text2);margin:12px 0 4px;letter-spacing:0.3px;text-transform:uppercase;">$1</h3>');
  html = html.replace(/^## (.+)$/gm,  '<h2 style="font-size:15px;font-weight:700;color:var(--text);margin:14px 0 6px;letter-spacing:-0.2px;">$1</h2>');
  html = html.replace(/^# (.+)$/gm,   '<h1 style="font-size:17px;font-weight:700;color:var(--text);margin:14px 0 6px;letter-spacing:-0.3px;">$1</h1>');

  // 6. Listas
  html = html.replace(/^(\d+\.\s.+)$/gm, '<div style="margin:4px 0;padding-left:4px;">$1</div>');
  html = html.replace(/^[-•]\s(.+)$/gm,  '<div style="margin:3px 0;padding-left:12px;color:var(--text2);">· $1</div>');

  // 7. Saltos de línea
  html = html.replace(/\n\n/g, '<br><br>');
  html = html.replace(/\n/g, '<br>');

  return html;
}

// ═══════════════════════════════════════
// MENSAJE DE ERROR (Fix 4)
// ═══════════════════════════════════════
function agregarMensajeError(texto) {
  const div = document.createElement('div');
  div.classList.add('msg', 'msg-bot', 'msg-error');
  div.innerHTML = `
    <div class="msg-avatar msg-avatar-error">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
      </svg>
    </div>
    <div class="msg-bubble msg-bubble-error">${sanitize(texto)}</div>`;
  messagesEl.appendChild(div);
  scrollAbajo();
}


function agregarMensaje(rol, texto) {
  const div = document.createElement('div');
  div.classList.add('msg', rol === 'user' ? 'msg-user' : 'msg-bot');

  if (rol === 'user') {
    div.innerHTML = `
      <div class="msg-bubble">
        ${texto.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
      </div>`;
  } else {
    div.innerHTML = `
      <div class="msg-avatar">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
          <path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z" fill="url(#ga)"/>
          <defs>
            <linearGradient id="ga" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
              <stop stop-color="#2563eb"/>
              <stop offset="1" stop-color="#00d4aa"/>
            </linearGradient>
          </defs>
        </svg>
      </div>
      <div class="msg-bubble">${renderMarkdown(texto)}</div>`;
  }

  messagesEl.appendChild(div);
  scrollAbajo();
}

// ═══════════════════════════════════════
// TYPING INDICATOR
// ═══════════════════════════════════════
function mostrarTyping() {
  const id  = 'typing-' + Date.now();
  const div = document.createElement('div');
  div.classList.add('msg', 'msg-bot');
  div.id = id;
  div.innerHTML = `
    <div class="msg-avatar">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
        <path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z" fill="url(#gt)"/>
        <defs>
          <linearGradient id="gt" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
            <stop stop-color="#2563eb"/>
            <stop offset="1" stop-color="#00d4aa"/>
          </linearGradient>
        </defs>
      </svg>
    </div>
    <div class="msg-bubble thinking-bubble">
      <span></span><span></span><span></span>
    </div>`;
  messagesEl.appendChild(div);
  scrollAbajo();
  return id;
}

function quitarTyping(id) {
  const el = document.getElementById(id);
  if (el) el.remove();
}

// ═══════════════════════════════════════
// STATUS PILL
// ═══════════════════════════════════════
function setStatus(estado) {
  const dot  = statusPill.querySelector('.status-dot');
  const text = statusPill.querySelector('span:last-child');
  if (estado === 'thinking') {
    statusPill.classList.add('thinking');
    text.textContent = 'Analizando...';
  } else {
    statusPill.classList.remove('thinking');
    text.textContent = 'En línea';
  }
}

// ═══════════════════════════════════════
// SCROLL AL FONDO
// ═══════════════════════════════════════
function scrollAbajo() {
  setTimeout(() => {
    chatArea.scrollTop = chatArea.scrollHeight;
  }, 30);
}

// ═══════════════════════════════════════
// LIMPIAR CHAT
// ═══════════════════════════════════════
btnClear.addEventListener('click', async () => {
  messagesEl.innerHTML = '';
  if (welcomeEl) welcomeEl.style.display = 'flex';
  inputEl.focus();
  // Limpiar también el historial en DB
  try {
    await fetch('/chat/limpiar', { method: 'POST' });
  } catch (e) {
    // silencioso — si falla no es crítico para el usuario
  }
});

// ═══════════════════════════════════════
// PRECIO EN VIVO
// ═══════════════════════════════════════
async function cargarPrecio() {
  const btn = document.getElementById('btn-refresh');
  const card = document.getElementById('price-card');
  if (btn) btn.classList.add('spinning');

  try {
    const res  = await fetch('/precio');
    const data = await res.json();

    if (data.error || !data.precio) {
      card.innerHTML = `<div class="price-loading"><div class="pulse-dot"></div><span>Sin datos</span></div>`;
      return;
    }

    const cambio = data.cambio_24h || 0;
    const signo  = cambio >= 0 ? '▲' : '▼';
    const cls    = cambio >= 0 ? 'up' : 'down';
    const fmt    = (n) => '$' + Number(n).toLocaleString('en-US', { maximumFractionDigits: 0 });

    card.innerHTML = `
      <div class="price-pair">
        BTC / USDT · Binance
        <span class="live-badge">LIVE</span>
      </div>
      <div class="price-value">${fmt(data.precio)}</div>
      <div class="price-change ${cls}">${signo} ${Math.abs(cambio).toFixed(2)}% (24h)</div>
      <div class="price-stats">
        <div class="price-stat">
          <div class="price-stat-label">Alto 24h</div>
          <div class="price-stat-val">${fmt(data.alto_24h)}</div>
        </div>
        <div class="price-stat">
          <div class="price-stat-label">Bajo 24h</div>
          <div class="price-stat-val">${fmt(data.bajo_24h)}</div>
        </div>
      </div>`;

  } catch (e) {
    card.innerHTML = `<div class="price-loading"><div class="pulse-dot"></div><span>Sin conexión</span></div>`;
  } finally {
    if (btn) btn.classList.remove('spinning');
  }
}

// ═══════════════════════════════════════
// CHUNKS (knowledge base count)
// ═══════════════════════════════════════
async function cargarChunks() {
  try {
    const res  = await fetch('/info');
    const data = await res.json();
    const el   = document.getElementById('kb-count');
    if (el && data.chunks) {
      el.textContent = Number(data.chunks).toLocaleString('en-US');
    }
  } catch (e) {
    // silencioso
  }
}

// ═══════════════════════════════════════
// BOTÓN REFRESH
// ═══════════════════════════════════════
btnRefresh.addEventListener('click', cargarPrecio);

// ═══════════════════════════════════════
// INIT
// ═══════════════════════════════════════
cargarPrecio();
cargarChunks();
setInterval(cargarPrecio, 60000); // actualiza cada minuto
inputEl.focus();

// ═══════════════════════════════════════
// BOTÓN SCROLL AL FONDO — Fix 8
// ═══════════════════════════════════════
const scrollBtn = document.getElementById('scroll-bottom');

chatArea.addEventListener('scroll', () => {
  const distanciaFondo = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight;
  if (scrollBtn) {
    scrollBtn.style.opacity = distanciaFondo > 200 ? '1' : '0';
    scrollBtn.style.pointerEvents = distanciaFondo > 200 ? 'auto' : 'none';
  }
});

if (scrollBtn) {
  scrollBtn.addEventListener('click', () => {
    chatArea.scrollTo({ top: chatArea.scrollHeight, behavior: 'smooth' });
  });
}

// ═══════════════════════════════════════
// CHROMADB RELOAD — Fix 10
// Refresca el contador de chunks cada 30s
// (si corriste procesar_docs.py, se refleja solo)
// ═══════════════════════════════════════
setInterval(cargarChunks, 30000);

// ═══════════════════════════════════════
// MERCADO EN VIVO — Indicadores sidebar
// ═══════════════════════════════════════
let activoActual = 'BTC';
let datosMercado = {};

async function cargarMercado() {
  try {
    const res  = await fetch('/mercado');
    const data = await res.json();
    datosMercado = data;
    actualizarSidebar(activoActual);
  } catch(e) {
    // silencioso
  }
}

function seleccionarActivo(symbol, btn) {
  activoActual = symbol;
  document.querySelectorAll('.asset-tab').forEach(t => t.classList.remove('active'));
  if (btn) btn.classList.add('active');
  actualizarSidebar(symbol);
}

function actualizarSidebar(symbol) {
  const d = datosMercado[symbol];
  const priceCard = document.getElementById('price-card');
  const indRow    = document.getElementById('indicators-row');

  if (!d || d.error) {
    if (priceCard) priceCard.innerHTML = `<div class="price-loading"><div class="pulse-dot"></div><span>Sin datos</span></div>`;
    return;
  }

  // Precio
  const precio   = d.precio   ? '$' + Number(d.precio).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—';
  const cambio   = d.cambio_24h !== undefined && d.cambio_24h !== null ? d.cambio_24h.toFixed(2) : null;
  const esSubida = cambio !== null && cambio >= 0;
  const cambioHtml = cambio !== null
    ? `<div class="price-change ${esSubida ? 'up' : 'down'}">${esSubida ? '▲' : '▼'} ${Math.abs(cambio)}%</div>`
    : '';

  const fmtP = (n) => n ? '$' + Number(n).toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0}) : '—';
  const alto = d.alto_24h ? fmtP(d.alto_24h) : '—';
  const bajo = d.bajo_24h ? fmtP(d.bajo_24h) : '—';

  if (priceCard) priceCard.innerHTML = `
    <div class="price-top">
      <div class="price-symbol">${symbol}/USDT <span class="live-badge">LIVE</span></div>
      ${cambioHtml}
    </div>
    <div class="price-value">${precio}</div>
    <div class="price-stats">
      <div class="price-stat">
        <div class="price-stat-label">Alto 24h</div>
        <div class="price-stat-val">${alto}</div>
      </div>
      <div class="price-stat">
        <div class="price-stat-label">Bajo 24h</div>
        <div class="price-stat-val">${bajo}</div>
      </div>
    </div>
  `;

  // Indicadores
  if (indRow) indRow.style.display = 'flex';

  // Funding
  const fundingEl = document.getElementById('funding-val');
  if (fundingEl && d.funding !== undefined && d.funding !== null) {
    const f = parseFloat(d.funding);
    const fStr = (f >= 0 ? '+' : '') + f.toFixed(4) + '%';
    // Umbrales en %: neutro ±0.025%, sesgo ±0.05%, extremo ±0.15%
    let cls = 'neutral';
    if (Math.abs(f) >= 0.15)  cls = 'warn';    // crítico ámbar
    else if (f > 0.025)       cls = 'down';    // retail long → trampa bajista → rojo
    else if (f < -0.025)      cls = 'up';      // retail short → trampa alcista → verde
    else                      cls = 'neutral'; // neutro ±0.025%
    fundingEl.textContent = fStr;
    fundingEl.className   = 'ind-val ' + cls;
  }

  // OI 4H
  const oiEl = document.getElementById('oi-val');
  if (oiEl && d.oi_cambio_4h !== undefined && d.oi_cambio_4h !== null) {
    const oi = parseFloat(d.oi_cambio_4h);
    oiEl.textContent = (oi >= 0 ? '▲ +' : '▼ ') + oi.toFixed(2) + '%';
    oiEl.className   = 'ind-val ' + (oi >= 0 ? 'up' : 'down');
  }

  // RSI
  const rsiEl = document.getElementById('rsi-val');
  if (rsiEl && d.rsi !== undefined && d.rsi !== null) {
    const rsi = parseFloat(d.rsi);
    let cls = 'neutral';
    if (rsi >= 80 || rsi <= 20) cls = 'warn';   // extremo — ámbar
    else if (rsi > 60)          cls = 'down';   // zona alta — rojo
    else if (rsi < 40)          cls = 'up';     // zona baja — verde
    else                        cls = 'amber';  // neutro 40-60 — amarillo
    rsiEl.textContent = rsi.toFixed(1);
    rsiEl.className   = 'ind-val ' + cls;
  }
}

// Cargar mercado al inicio y cada 2 minutos
cargarMercado();
setInterval(cargarMercado, 120000);

// Botón refresh también recarga mercado
btnRefresh.addEventListener('click', cargarMercado);

// ═══════════════════════════════════════
// MACRO — DXY + BTC Dominance
// ═══════════════════════════════════════
async function cargarMacro() {
  try {
    const res  = await fetch('/macro');
    const data = await res.json();

    // DXY
    const dxyValEl  = document.getElementById('dxy-val');
    const dxyChgEl  = document.getElementById('dxy-chg');
    if (dxyValEl && data.dxy && data.dxy.valor) {
      dxyValEl.textContent = data.dxy.valor.toFixed(2);
      if (dxyChgEl) {
        const chg = data.dxy.cambio || 0;
        dxyChgEl.textContent = (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%';
        dxyChgEl.className = 'ind-val ' + (chg > 0 ? 'down' : chg < 0 ? 'up' : 'neutral');
      }
    }

    // BTC Dominance
    const btcdValEl = document.getElementById('btcd-val');
    const btcdChgEl = document.getElementById('btcd-chg');
    if (btcdValEl && data.btcd && data.btcd.valor) {
      btcdValEl.textContent = data.btcd.valor.toFixed(2) + '%';
      if (btcdChgEl) {
        const chg = data.btcd.cambio || 0;
        btcdChgEl.textContent = (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%';
        btcdChgEl.className = 'ind-val ' + (chg > 0 ? 'up' : chg < 0 ? 'down' : 'neutral');
      }
    }
  } catch(e) {
    // silencioso
  }
}

cargarMacro();
setInterval(cargarMacro, 300000); // cada 5 min (yfinance es lento)
btnRefresh.addEventListener('click', cargarMacro);

// ═══════════════════════════════════════
// THEME — init desde localStorage
// ═══════════════════════════════════════
(function initTheme() {
  const saved = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
})();

function toggleTheme() {
  const cur  = document.documentElement.getAttribute('data-theme');
  const next = cur === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  // Actualizar label en dropdown
  const label = document.getElementById('dd-theme-label');
  if (label) label.textContent = next === 'light' ? 'Modo oscuro' : 'Modo claro';
}

const ddThemeBtn = document.getElementById('dd-theme-btn');
if (ddThemeBtn) ddThemeBtn.addEventListener('click', toggleTheme);

// Inicializar label del tema
(function() {
  const cur = localStorage.getItem('theme') || 'dark';
  const label = document.getElementById('dd-theme-label');
  if (label) label.textContent = cur === 'light' ? 'Modo oscuro' : 'Modo claro';
})();

// ═══════════════════════════════════════
// USER CHIP DROPDOWN — open / close
// ═══════════════════════════════════════
const userChip    = document.getElementById('user-chip');
const userChipBtn = document.getElementById('user-chip-btn');
const userDropdown = document.getElementById('user-dropdown');

if (userChipBtn) {
  userChipBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    userChip.classList.toggle('open');
  });
}

// Cerrar al hacer click fuera
document.addEventListener('click', (e) => {
  if (userChip && !userChip.contains(e.target)) {
    userChip.classList.remove('open');
  }
});

// Cerrar con Escape
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && userChip) userChip.classList.remove('open');
});

// ═══════════════════════════════════════
// CARGAR USUARIO — rellenar dropdown
// ═══════════════════════════════════════
fetch('/me').then(r => r.json()).then(data => {
  if (!data.usuario) return;
  const u = data.usuario;

  const chip     = document.getElementById('user-chip');
  const statusP  = document.getElementById('status-pill');
  const avatar   = document.getElementById('user-avatar');
  const name     = document.getElementById('user-name');
  const ddAvatar = document.getElementById('dd-avatar');
  const ddName   = document.getElementById('dd-user-name');
  const ddPlan   = document.getElementById('dd-plan-badge');

  if (chip)    chip.style.display  = 'flex';
  if (statusP) statusP.style.display = 'none';  // dot verde ya en el chip
  if (avatar)  avatar.src = u.foto_url || '';
  if (name)    name.textContent = (u.nombre || u.email || '').split(' ')[0];
  if (ddAvatar) ddAvatar.src = u.foto_url || '';
  if (ddName)   ddName.textContent = u.nombre || u.email || '';
  if (ddPlan) {
    const plan = u.plan || 'free';
    ddPlan.textContent = plan === 'pro' ? 'Pro' : 'Free';
    if (plan === 'pro') ddPlan.classList.add('pro');
  }
}).catch(() => {});

// ═══════════════════════════════════════
// LANGUAGE TOGGLE — ES / EN
// ═══════════════════════════════════════
const TRANSLATIONS = {
  es: {
    eyebrow:   'Asistente de Trading',
    online:    'En línea',
    thinking:  'Analizando...',
    inputHint: 'Enter para enviar · Shift+Enter para nueva línea',
    placeholder:'Pregunta sobre BTC, análisis técnico, estrategias...',
    welcome:   '¡Bienvenido a TradeBot AI!',
    welcomeSub:'Tu asistente de trading con inteligencia artificial. Analiza mercados, aprende conceptos y toma decisiones informadas.',
    disclaimer:'⚠️ Solo con fines educativos. No es asesoramiento financiero.',
    clearChat: 'Limpiar conversación',
    tgHint:    'Alertas de setup BTC · Reporte diario',
    market:    'Mercado en vivo',
    kb:        'Base de conocimiento',
    chunks:    'Chunks indexados',
    logoutTitle:'Cerrar sesión',
    charLeft:  ' caracteres restantes',
    errWait:   '⏱️ Demasiadas solicitudes. Espera un momento antes de continuar.',
    errServer: '⚠️ Error interno del servidor. Intenta de nuevo en unos segundos.',
    errUnavail:'⚠️ Servicio temporalmente no disponible.',
    errGeneric:'⚠️ Error del servidor. Intenta de nuevo.',
    errConn:   '📡 Sin conexión con el servidor. Verifica que Flask esté corriendo en el puerto 5000.',
    errUnk:    '⚠️ Error inesperado. Recarga la página si persiste.',
  },
  en: {
    eyebrow:   'Trading Assistant',
    online:    'Online',
    thinking:  'Analyzing...',
    inputHint: 'Enter to send · Shift+Enter for new line',
    placeholder:'Ask about BTC, technical analysis, strategies...',
    welcome:   'Welcome to TradeBot AI!',
    welcomeSub:'Your AI-powered trading assistant. Analyze markets, learn concepts and make informed decisions.',
    disclaimer:'⚠️ For educational purposes only. Not financial advice.',
    clearChat: 'Clear conversation',
    tgHint:    'BTC setup alerts · Daily report',
    market:    'Live market',
    kb:        'Knowledge base',
    chunks:    'Indexed chunks',
    logoutTitle:'Sign out',
    charLeft:  ' characters left',
    errWait:   '⏱️ Too many requests. Please wait a moment before continuing.',
    errServer: '⚠️ Internal server error. Try again in a few seconds.',
    errUnavail:'⚠️ Service temporarily unavailable.',
    errGeneric:'⚠️ Server error. Please try again.',
    errConn:   '📡 No connection to server. Make sure Flask is running on port 5000.',
    errUnk:    '⚠️ Unexpected error. Reload the page if it persists.',
  }
};

let currentLang = localStorage.getItem('lang') || 'es';

function applyLang(lang) {
  const t = TRANSLATIONS[lang];
  if (!t) return;
  currentLang = lang;
  localStorage.setItem('lang', lang);

  // Update html lang attr
  document.documentElement.lang = lang;

  // Update all data-i18n elements
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    if (t[key]) el.textContent = t[key];
  });

  // Textarea placeholder
  if (inputEl) inputEl.placeholder = t.placeholder;

  // Sidebar labels
  const marketLabel = document.querySelector('.sb-block-label');
  if (marketLabel) marketLabel.textContent = t.market;

  // KB label
  const kbLabel = document.querySelector('.kb-label');
  if (kbLabel) kbLabel.textContent = t.chunks;

  // Telegram hint
  const tgHint = document.querySelector('.tg-hint');
  if (tgHint) tgHint.textContent = t.tgHint;

  // Clear button
  const clearSpan = document.querySelector('.clear-btn span');
  if (clearSpan) clearSpan.textContent = t.clearChat;

  // Logout title
  const logoutBtn = document.getElementById('logout-btn');
  if (logoutBtn) logoutBtn.title = t.logoutTitle;

  // Actualizar label del dropdown
  const ddLbl = document.getElementById('dd-lang-label');
  if (ddLbl) ddLbl.textContent = lang === 'es' ? 'English' : 'Español';

  // Status pill — only if currently online (not thinking)
  const statusText = statusPill ? statusPill.querySelector('[data-i18n="online"]') : null;
  if (statusText && !statusPill.classList.contains('thinking')) {
    statusText.textContent = t.online;
  }
}

// Override setStatus to use translations
const _setStatusOrig = setStatus;
function setStatus(estado) {
  const dot  = statusPill.querySelector('.status-dot');
  const text = statusPill.querySelector('[data-i18n="online"]') || statusPill.querySelector('span:last-child');
  const t = TRANSLATIONS[currentLang] || TRANSLATIONS.es;
  if (estado === 'thinking') {
    statusPill.classList.add('thinking');
    text.textContent = t.thinking;
  } else {
    statusPill.classList.remove('thinking');
    text.textContent = t.online;
  }
}

// Lang toggle — ahora en el dropdown
const ddLangBtn = document.getElementById('dd-lang-btn');
if (ddLangBtn) {
  ddLangBtn.addEventListener('click', () => {
    const next = currentLang === 'es' ? 'en' : 'es';
    applyLang(next);
  });
}

// Actualizar label idioma en dropdown
function updateLangLabel(lang) {
  const label = document.getElementById('dd-lang-label');
  if (label) label.textContent = lang === 'es' ? 'English' : 'Español';
}

// Apply saved language on load
applyLang(currentLang);
updateLangLabel(currentLang);