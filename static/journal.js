/**
 * journal.js — TradeBot AI Journal de Trading
 * Este archivo es la versión standalone del JS del journal.html
 * Solo necesitas este archivo si prefieres separar el JS del HTML.
 * El journal.html ya contiene todo el JS inline y funciona sin este archivo.
 *
 * Para usarlo: en journal.html reemplaza el bloque <script>...</script>
 * por: <script src="/static/journal.js"></script>
 */

// ═══════════════════════════════════════════════════
// ESTADO LOCAL
// ═══════════════════════════════════════════════════
let direccion = "";
let resultado = "";
let trades    = [];
let stats     = {};

// ═══════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════
document.addEventListener("DOMContentLoaded", () => {
  cargarUsuario();
  cargarTrades();

  // Sidebar toggle
  document.getElementById("sidebar-toggle")?.addEventListener("click", () => {
    document.getElementById("sidebar")?.classList.toggle("collapsed");
  });
});

// ═══════════════════════════════════════════════════
// USUARIO — cargar nombre, avatar y plan
// ═══════════════════════════════════════════════════
function cargarUsuario() {
  fetch("/me")
    .then(r => r.json())
    .then(data => {
      if (!data.logueado || !data.usuario) return;

      const pill   = document.getElementById("user-pill");
      const avatar = document.getElementById("user-avatar");
      const name   = document.getElementById("user-name");
      const plan   = document.getElementById("plan-label");

      if (pill)   pill.style.display = "flex";
      if (name)   name.textContent = data.usuario.nombre?.split(" ")[0] || data.usuario.email || "";
      if (avatar && data.usuario.avatar_url) {
        avatar.src = data.usuario.avatar_url;
        avatar.onerror = () => { avatar.src = ""; };
      }
      if (plan) {
        const esPro = data.usuario.plan === "pro";
        plan.textContent = esPro ? "Pro" : "Free";
        document.getElementById("status-pill")?.classList.toggle("thinking", esPro);
      }

      // Deshabilitar análisis profundo si no es Pro
      if (data.usuario.plan !== "pro") {
        const btn = document.getElementById("btn-analizar");
        if (btn) {
          btn.disabled = true;
          btn.innerHTML = `<span>🔒</span> Solo plan Pro`;
        }
      }
    })
    .catch(() => {});
}

// ═══════════════════════════════════════════════════
// SETTERS DE BOTONES (dirección y resultado)
// ═══════════════════════════════════════════════════
function setDir(dir) {
  direccion = dir;
  document.getElementById("btn-long").className  = "dir-btn" + (dir === "LONG" ? " active-long" : "");
  document.getElementById("btn-short").className = "dir-btn" + (dir === "SHORT" ? " active-short" : "");
  calcRR();
}

function setResult(res) {
  resultado = res;
  document.getElementById("btn-win").className  = "result-btn" + (res === "WIN"  ? " active-win"  : "");
  document.getElementById("btn-loss").className = "result-btn" + (res === "LOSS" ? " active-loss" : "");
  document.getElementById("btn-be").className   = "result-btn" + (res === "BE"   ? " active-be"   : "");
}

function setTab(tab) {
  document.getElementById("tab-historial").className = "j-tab" + (tab === "historial" ? " active" : "");
  document.getElementById("tab-ia").className        = "j-tab" + (tab === "ia"        ? " active" : "");
  document.getElementById("tc-historial").className  = "tab-content" + (tab === "historial" ? " active" : "");
  document.getElementById("tc-ia").className         = "tab-content" + (tab === "ia"        ? " active" : "");
}

// ═══════════════════════════════════════════════════
// CÁLCULO R:R automático
// ═══════════════════════════════════════════════════
function calcRR() {
  const entrada = parseFloat(document.getElementById("f-entrada").value);
  const sl      = parseFloat(document.getElementById("f-sl").value);
  const tp      = parseFloat(document.getElementById("f-tp").value);
  const el      = document.getElementById("rr-display");
  const vl      = document.getElementById("rr-val");

  if (!entrada || !sl || !tp || !direccion) {
    el.className   = "rr-display";
    vl.textContent = "Ingresa entrada, SL y TP para calcular";
    return;
  }

  let riesgo, recompensa;
  if (direccion === "LONG") {
    riesgo     = entrada - sl;
    recompensa = tp - entrada;
  } else {
    riesgo     = sl - entrada;
    recompensa = entrada - tp;
  }

  if (riesgo <= 0 || recompensa <= 0) {
    el.className   = "rr-display rr-bad";
    vl.textContent = "⚠️ Precios inválidos para " + direccion;
    return;
  }

  const rr      = (recompensa / riesgo).toFixed(2);
  const riskPct = (Math.abs(entrada - sl) / entrada * 100).toFixed(2);
  const ok      = parseFloat(rr) >= 1;

  el.className   = "rr-display " + (ok ? "rr-ok" : "rr-bad");
  vl.textContent = `${rr}:1 R:R — Riesgo: ${riskPct}%`;
}

// ═══════════════════════════════════════════════════
// REGISTRAR TRADE → POST /journal/trade
// ═══════════════════════════════════════════════════
async function registrarTrade() {
  const activo  = document.getElementById("f-activo").value.trim();
  const entrada = document.getElementById("f-entrada").value;
  const sl      = document.getElementById("f-sl").value;
  const tp      = document.getElementById("f-tp").value;
  const pnl     = document.getElementById("f-pnl").value;
  const notas   = document.getElementById("f-notas").value.trim();
  const tf      = document.getElementById("f-tf").value;

  if (!activo || !entrada || !direccion) {
    showToast("Activo, entrada y dirección son obligatorios", "error");
    return;
  }

  const btn = document.getElementById("btn-submit");
  btn.disabled = true;
  btn.innerHTML = `<div class="loading-dots"><span></span><span></span><span></span></div> Analizando...`;

  // Ocultar feedback anterior
  document.getElementById("ia-feedback-box").classList.remove("visible");

  try {
    const res = await fetch("/journal/trade", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        activo,
        direccion,
        entrada:   parseFloat(entrada),
        sl:        sl   ? parseFloat(sl)   : null,
        tp:        tp   ? parseFloat(tp)   : null,
        resultado: resultado || null,
        pnl:       pnl  ? parseFloat(pnl)  : null,
        timeframe: tf,
        notas,
      })
    });

    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "Error desconocido");

    // Mostrar feedback IA si viene
    if (data.ia_feedback) {
      document.getElementById("ia-feedback-text").textContent = data.ia_feedback;
      document.getElementById("ia-feedback-box").classList.add("visible");
    }

    showToast("✅ Trade registrado", "success");
    limpiarFormulario();
    cargarTrades();

  } catch (err) {
    showToast("Error: " + err.message, "error");
  } finally {
    btn.disabled = false;
    btn.innerHTML = `
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
        <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
      </svg>
      Registrar trade`;
  }
}

// ═══════════════════════════════════════════════════
// CARGAR TRADES → GET /journal/trades
// ═══════════════════════════════════════════════════
async function cargarTrades() {
  try {
    const res  = await fetch("/journal/trades");
    const data = await res.json();
    if (!data.ok) return;

    trades = data.trades || [];
    stats  = data.stats  || {};

    actualizarStats();
    renderTrades();
  } catch {}
}

// ═══════════════════════════════════════════════════
// ACTUALIZAR STATS en el DOM
// ═══════════════════════════════════════════════════
function actualizarStats() {
  const total = stats.total || 0;
  const wr    = total > 0 ? stats.win_rate + "%" : "—";
  const rr    = stats.rr_promedio != null ? stats.rr_promedio + ":1" : "—";

  document.getElementById("st-total").textContent = total || "—";
  document.getElementById("st-wr").textContent    = wr;
  document.getElementById("st-rr").textContent    = rr;

  // Sidebar también
  const sbTotal = document.querySelector(".sb-stat-total");
  const sbWr    = document.querySelector(".sb-stat-wr");
  const sbRr    = document.querySelector(".sb-stat-rr");
  if (sbTotal) sbTotal.textContent = total || "—";
  if (sbWr)    sbWr.textContent    = wr;
  if (sbRr)    sbRr.textContent    = rr;
}

// ═══════════════════════════════════════════════════
// RENDER LISTA DE TRADES
// ═══════════════════════════════════════════════════
function renderTrades() {
  const list = document.getElementById("trades-list");
  if (!list) return;

  if (!trades.length) {
    list.innerHTML = `
      <div class="empty-state">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
          <line x1="16" y1="13" x2="8" y2="13"/>
          <line x1="16" y1="17" x2="8" y2="17"/>
        </svg>
        <p>Aún no hay trades registrados.<br>Registra tu primer trade para empezar.</p>
      </div>`;
    return;
  }

  list.innerHTML = trades.map(t => {
    const cardClass = t.resultado === "WIN" ? "win-card"
                    : t.resultado === "LOSS" ? "loss-card"
                    : t.resultado === "BE"   ? "be-card" : "";
    const resClass  = (t.resultado || "pending").toLowerCase();
    const resLabel  = t.resultado || "Pendiente";
    const dirClass  = t.direccion === "LONG" ? "long" : "short";
    const pnlClass  = t.pnl > 0 ? "pos" : t.pnl < 0 ? "neg" : "";
    const pnlStr    = t.pnl != null
                    ? (t.pnl > 0 ? "+" : "") + t.pnl.toFixed(2) + "%"
                    : "—";

    return `
    <div class="trade-card ${cardClass}" id="tc-${t.id}">
      <button class="trade-delete-btn" onclick="borrarTrade(${t.id})" title="Eliminar trade">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
          <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
      <div class="trade-card-header">
        <div class="trade-card-asset">
          ${t.activo}
          <span class="trade-dir-badge ${dirClass}">${t.direccion}</span>
        </div>
        <span class="trade-result-badge ${resClass}">${resLabel}</span>
      </div>
      <div class="trade-card-stats">
        <div class="trade-stat-item">
          <span class="trade-stat-label">Entrada</span>
          <span class="trade-stat-val">$${Number(t.entrada).toLocaleString()}</span>
        </div>
        ${t.sl ? `
        <div class="trade-stat-item">
          <span class="trade-stat-label">SL</span>
          <span class="trade-stat-val">$${Number(t.sl).toLocaleString()}</span>
        </div>` : ""}
        ${t.tp ? `
        <div class="trade-stat-item">
          <span class="trade-stat-label">TP</span>
          <span class="trade-stat-val">$${Number(t.tp).toLocaleString()}</span>
        </div>` : ""}
        ${t.rr_planeado != null ? `
        <div class="trade-stat-item">
          <span class="trade-stat-label">R:R Plan</span>
          <span class="trade-stat-val">${t.rr_planeado}:1</span>
        </div>` : ""}
        ${t.pnl != null ? `
        <div class="trade-stat-item">
          <span class="trade-stat-label">PnL</span>
          <span class="trade-stat-val ${pnlClass}">${pnlStr}</span>
        </div>` : ""}
        ${t.timeframe ? `
        <div class="trade-stat-item">
          <span class="trade-stat-label">TF</span>
          <span class="trade-stat-val">${t.timeframe}</span>
        </div>` : ""}
      </div>
      <div class="trade-card-footer">
        <span class="trade-card-notes">${t.notas || ""}</span>
        <span class="trade-card-date">${t.fecha_trade}</span>
      </div>
    </div>`;
  }).join("");
}

// ═══════════════════════════════════════════════════
// BORRAR TRADE → DELETE /journal/trade/<id>
// ═══════════════════════════════════════════════════
async function borrarTrade(id) {
  if (!confirm("¿Eliminar este trade?")) return;
  try {
    const res  = await fetch(`/journal/trade/${id}`, { method: "DELETE" });
    const data = await res.json();
    if (data.ok) {
      document.getElementById(`tc-${id}`)?.remove();
      showToast("Trade eliminado", "success");
      cargarTrades();
    }
  } catch {
    showToast("Error al eliminar", "error");
  }
}

// ═══════════════════════════════════════════════════
// ANÁLISIS PROFUNDO → POST /journal/analisis-profundo
// ═══════════════════════════════════════════════════
async function analisisProfundo() {
  const btn    = document.getElementById("btn-analizar");
  const result = document.getElementById("ia-result");
  const cta    = document.getElementById("ia-cta");

  btn.disabled = true;
  btn.innerHTML = `<div class="loading-dots"><span></span><span></span><span></span></div> Analizando...`;

  try {
    const res  = await fetch("/journal/analisis-profundo", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({})
    });
    const data = await res.json();

    if (!data.ok) {
      if (data.upgrade) {
        showToast("Función exclusiva del plan Pro 🔒", "error");
      } else {
        showToast(data.error || "Error en el análisis", "error");
      }
      return;
    }

    result.textContent = data.analisis;
    result.classList.add("visible");
    if (cta) cta.style.display = "none";

  } catch {
    showToast("Error de conexión", "error");
  } finally {
    btn.disabled = false;
    btn.innerHTML = `
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
        <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
      </svg>
      Analizar de nuevo`;
  }
}

// ═══════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════
function limpiarFormulario() {
  ["f-activo","f-entrada","f-sl","f-tp","f-pnl","f-notas"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });
  const tf = document.getElementById("f-tf");
  if (tf) tf.value = "4H";

  direccion = "";
  resultado = "";

  document.querySelectorAll(".dir-btn").forEach(b    => b.className = "dir-btn");
  document.querySelectorAll(".result-btn").forEach(b => b.className = "result-btn");

  const rrEl = document.getElementById("rr-display");
  const rrVl = document.getElementById("rr-val");
  if (rrEl) rrEl.className   = "rr-display";
  if (rrVl) rrVl.textContent = "Ingresa entrada, SL y TP para calcular";
}

let toastTimeout;
function showToast(msg, type = "success") {
  const t = document.getElementById("j-toast");
  if (!t) return;
  t.textContent = msg;
  t.className   = `j-toast ${type} show`;
  clearTimeout(toastTimeout);
  toastTimeout  = setTimeout(() => t.classList.remove("show"), 3000);
}
