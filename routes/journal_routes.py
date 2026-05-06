"""
journal_routes.py — Blueprint /journal
═══════════════════════════════════════
Endpoints:
    POST /journal/trade              → registrar trade + análisis rápido IA
    GET  /journal/trades             → listar trades del usuario
    GET  /journal/stats              → estadísticas agregadas
    POST /journal/analisis-profundo  → análisis de patrones con RAG (solo Pro)
    DELETE /journal/trade/<id>       → borrar un trade
    GET  /journal                    → sirve journal.html
"""

import re
import csv
import io
from datetime import datetime, date
from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user

from models import db, Journal
from resources import claude, buscar_contexto, build_system_prompt, CLAUDE_MODEL

journal_bp = Blueprint("journal", __name__)

# ============================================================
# DECORADOR — solo usuarios Pro
# ============================================================

from functools import wraps
from flask import jsonify

def pro_required(f):
    """Bloquea el endpoint si el usuario no es Pro"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.es_pro():
            return jsonify({
                "ok": False,
                "error": "Esta función es exclusiva del plan Pro. Upgrade para acceder al análisis profundo de tus trades.",
                "upgrade": True
            }), 403
        return f(*args, **kwargs)
    return decorated

# ============================================================
# SYSTEM PROMPTS DEL JOURNAL
# ============================================================

SYSTEM_JOURNAL_RAPIDO = """Eres TradeBot, analizando el trade de un trader.
Sé directo y brutal — sin filtros pero constructivo.
Máximo 150 palabras. Sin markdown complejo.
TERMINOLOGÍA OBLIGATORIA: usa "PnL no realizado" o "ganancia/pérdida no realizada". NUNCA "en papel".
FUTUROS PERPETUOS: Nocional = Margen × Leverage | Unidades = Nocional / Precio entrada | PnL = Unidades × diferencia de precio. NUNCA multipliques diferencia × leverage directamente.
No eres asesor financiero — análisis educativo."""

SYSTEM_JOURNAL_APERTURA = """Eres TradeBot, mentor institucional de trading.
El trader está CONSIDERANDO entrar a un trade AHORA. Tu trabajo:
1. Evalúa si las condiciones técnicas actuales apoyan o contradicen la entrada
2. Señala el mayor riesgo de gestión (leverage, margen, SL vs liquidación)
3. Si hay problemas, ofrece una alternativa concreta (precio mejor, TF diferente, tamaño reducido)
Máximo 120 palabras. Sé conciso y accionable. Termina siempre en una sola línea de veredicto.
NUNCA uses "en papel" — usa "PnL no realizado".
No eres asesor financiero — análisis educativo."""

SYSTEM_JOURNAL_PROFUNDO = """Eres TradeBot, el mentor institucional de trading más exigente.
Tienes acceso al historial completo de trades del usuario y a metodología institucional.
Tu trabajo: encontrar los patrones de error que el trader no puede ver por sí mismo.
Sé brutalmente honesto — la comodidad no mejora a los traders.

ESTRUCTURA OBLIGATORIA (en este orden exacto, sin repetir ideas entre secciones):
1. PROBLEMA PRINCIPAL — 1 línea
2. ESTADO ACTUAL — PnL con fórmula correcta de perpetuos
3. PROBABILIDADES — máx 3 escenarios con %
4. MATEMÁTICA — R:R + P_min
5. ACCIÓN INMEDIATA — máx 3 bullets

REGLAS DE CÁLCULO — FUTUROS PERPETUOS:
  Nocional = Margen × Leverage
  Unidades = Nocional / Precio entrada
  PnL no realizado = Unidades × (diferencia de precio)
  NUNCA multipliques diferencia × leverage directamente.
  NUNCA uses "ganancia en papel" — usa "PnL no realizado" o "ganancia no realizada".

REGLAS DE FORMATO:
  Máximo 400 palabras en total.
  Una idea = una sección (no repetir en múltiples bloques).
  PROHIBIDO ABSOLUTO: cualquier frase con fecha de análisis pasado ("el análisis del 2026-03-XX", "según el RAG", "análisis histórico predijo", "setup del YYYY-MM-DD", "el documento de marzo"). El conocimiento base es tuyo internamente — NUNCA lo atribuyas ni lo cites. Sección 5 = SOLO números estadísticos puros: % retorno por día, % fill rate CME, días hasta FOMC, fase halving. Sin narrativa de "análisis previos".
  No eres asesor financiero — análisis educativo."""

# ============================================================
# HELPERS
# ============================================================

def calcular_rr(entrada: float, sl: float, tp: float, direccion: str) -> float | None:
    """Calcula el R:R planeado dado entrada, SL y TP"""
    try:
        if direccion == "LONG":
            riesgo    = entrada - sl
            recompensa = tp - entrada
        else:  # SHORT
            riesgo    = sl - entrada
            recompensa = entrada - tp

        if riesgo <= 0:
            return None
        return round(recompensa / riesgo, 2)
    except Exception:
        return None


def calcular_rr_real(entrada: float, salida: float, sl: float, direccion: str) -> float | None:
    """Calcula el R:R real obtenido dado el precio de salida"""
    try:
        if direccion == "LONG":
            riesgo  = entrada - sl
            obtenido = salida - entrada
        else:  # SHORT
            riesgo  = sl - entrada
            obtenido = entrada - salida

        if riesgo <= 0:
            return None
        return round(obtenido / riesgo, 2)
    except Exception:
        return None


def _tipo_trade_ctx(tipo: str, timeframe: str) -> str:
    """
    Devuelve descripción del tipo de trade + flag de coherencia con el timeframe.
    La IA usa esto para detectar contradicciones intención vs ejecución.
    """
    tf = timeframe.upper() if timeframe else ""

    # Coherencia esperada
    scalp_tfs    = {"1M", "3M", "5M", "15M", "30M"}
    swing_tfs    = {"1H", "2H", "4H", "6H", "8H", "12H"}
    position_tfs = {"1D", "3D", "1W", "1M"}

    coherencia = ""
    if tipo == "SCALP":
        if tf in swing_tfs or tf in position_tfs:
            coherencia = f"⚠️ INCOHERENCIA: scalp declarado pero TF {tf} es de swing/position — ¿cerraste por emoción o era la intención real?"
        else:
            coherencia = "✅ TF coherente con scalp"
        rr_minimo = "R:R mínimo aceptable para scalp: 0.7:1 si win rate > 65%"
        duracion_esperada = "Duración esperada: minutos a pocas horas"
    elif tipo == "SWING":
        if tf in scalp_tfs:
            coherencia = f"⚠️ INCOHERENCIA: swing declarado pero TF {tf} es de scalp — señal de entrada en TF muy bajo para hold días"
        elif tf in position_tfs:
            coherencia = f"⚠️ INCOHERENCIA: swing en TF {tf} es más bien position trading"
        else:
            coherencia = "✅ TF coherente con swing"
        rr_minimo = "R:R mínimo aceptable para swing: 1.5:1"
        duracion_esperada = "Duración esperada: horas a pocos días"
    elif tipo == "POSITION":
        if tf in scalp_tfs or tf in swing_tfs:
            coherencia = f"⚠️ INCOHERENCIA: position trading en TF {tf} — señal de entrada demasiado corta para hold semanas"
        else:
            coherencia = "✅ TF coherente con position"
        rr_minimo = "R:R mínimo aceptable para position: 2:1 o más"
        duracion_esperada = "Duración esperada: días a semanas"
    else:
        return tipo

    return f"{tipo} | {coherencia} | {rr_minimo} | {duracion_esperada}"


def formatear_trade_para_ia(trade_data: dict, precio_actual: float = None) -> str:
    """Formatea los datos del trade para mandárselos a Claude.
    precio_actual: precio de mercado ahora mismo (para trades ABIERTOS).
    """
    pal      = trade_data.get('apalancamiento', 1.0) or 1.0
    palstr   = f"{int(pal)}x futures" if pal > 1 else "spot (sin apalancamiento)"
    sl       = trade_data.get('sl')
    tp       = trade_data.get('tp')
    entr     = trade_data.get('entrada', 0)
    cierre   = trade_data.get('precio_cierre')
    capital  = trade_data.get('capital_cuenta')
    margen   = trade_data.get('margen_usado')
    tipo_m   = trade_data.get('tipo_margen', 'AISLADO')
    dir_     = trade_data.get('direccion', 'LONG')

    riesgo_precio_pct = round(abs(sl - entr) / entr * 100, 2) if sl and entr else None
    obj_pct           = round(abs(tp - entr) / entr * 100, 2) if tp and entr else None
    mov_precio        = round(abs(cierre - entr) / entr * 100, 2) if cierre and entr else None

    # Calcular métricas avanzadas
    metricas = calcular_metricas_riesgo(entr, sl, dir_, pal, margen, capital, tipo_m)

    # Sección de gestión de riesgo
    riesgo_lines = []
    if capital:
        riesgo_lines.append(f"Capital cuenta:   ${capital:,.2f} USDT")
    if margen:
        riesgo_lines.append(f"Margen usado:     ${margen:,.2f} USDT ({tipo_m})")
    if metricas.get('posicion_usdt'):
        riesgo_lines.append(f"Tamaño posición (margen × leverage): ${metricas['posicion_usdt']:,.2f} USDT")
    if metricas.get('precio_liquidacion'):
        sl_ok = metricas.get('sl_antes_de_liq')
        if sl_ok is True:
            liq_warning = "✅ SL se ejecuta antes de liquidación"
        elif sl_ok is False:
            liq_warning = "⛔ SL MÁS LEJOS QUE LIQUIDACIÓN — el exchange te liquida antes de tocar el SL"
        else:
            liq_warning = "⛔ SIN SL — esta es tu única salida automática. Mencionarlo UNA SOLA VEZ."
        riesgo_lines.append(f"Precio liquidación: ${metricas['precio_liquidacion']:,.2f}  {liq_warning}")
    if margen and capital and tipo_m == "AISLADO":
        perdida_max_pct = round(margen / capital * 100, 1)
        riesgo_lines.append(f"Pérdida máxima posible (AISLADO): ${margen:,.2f} = {perdida_max_pct}% cuenta — SOLO el margen comprometido, no la posición total")
    elif tipo_m == "CRUZADO":
        riesgo_lines.append(
            f"⛔ Margen CRUZADO: liquidación depende del balance total de la cuenta — "
            f"NO mencionar precio de liquidación. "
            + (f"Capital declarado: ${capital:,.2f} USDT." if capital else "Capital no proporcionado.")
        )
    if metricas.get('riesgo_usdt'):
        riesgo_lines.append(f"Riesgo real hasta SL: ${metricas['riesgo_usdt']:,.2f} USDT (distancia precio × margen × leverage)")
    if metricas.get('riesgo_pct_cuenta'):
        nivel = "✅ profesional (≤1%)" if metricas['riesgo_pct_cuenta'] <= 1 else ("⚠️ agresivo (1-2%)" if metricas['riesgo_pct_cuenta'] <= 2 else "🚨 SOBREEXPOSICIÓN (>2%)")
        riesgo_lines.append(f"Riesgo hasta SL como % cuenta: {metricas['riesgo_pct_cuenta']}%  {nivel}")
    if metricas.get('exposicion_pct'):
        riesgo_lines.append(f"Margen comprometido: {metricas['exposicion_pct']}% del capital total")

    # PnL pre-computado usando calcular_pnl_perpetuo() — fórmula correcta perpetuos
    pnl_lines = []
    precio_ref = cierre or precio_actual   # cierre real o precio de mercado actual
    if precio_ref and entr and margen:
        p = calcular_pnl_perpetuo(entr, precio_ref, margen, pal, dir_)
        signo = "+" if p["pnl_usd"] >= 0 else ""
        label = "PnL realizado" if cierre else f"PnL no realizado (precio actual ${precio_ref:,.2f})"
        pnl_lines.append(f"Nocional (margen × leverage): ${p['nocional']:,.2f} USDT | Unidades: {p['unidades_btc']} BTC")
        pnl_lines.append(f"{label}: {signo}${p['pnl_usd']:,.2f} USDT ({signo}{p['pnl_pct']:.2f}% sobre margen)")
        pnl_lines.append(f"USAR ESTOS NÚMEROS — NO RECALCULAR")
    elif precio_ref and entr and not margen:
        # Sin margen: calcular solo % de movimiento × leverage, NUNCA inventar monto USD
        mov_pct = abs(precio_ref - entr) / entr * 100
        pnl_pct_estimado = mov_pct * pal
        favor = (dir_ == "SHORT" and precio_ref < entr) or (dir_ == "LONG" and precio_ref > entr)
        signo = "+" if favor else "-"
        pnl_lines.append(f"Movimiento de precio: {mov_pct:.2f}% → PnL estimado: {signo}{pnl_pct_estimado:.2f}% sobre margen")
        pnl_lines.append(f"MONTO USD: NO CALCULABLE — margen no proporcionado. PROHIBIDO inventar cifras en USD.")
    elif margen and metricas.get("posicion_usdt"):
        # Trade abierto sin precio actual: solo nocional
        posicion_usdt = metricas["posicion_usdt"]
        pnl_lines.append(f"Nocional (margen × leverage): ${posicion_usdt:,.2f} USDT")
        if trade_data.get("pnl") is not None:
            pnl_cuenta = trade_data["pnl"]
            pnl_usdt_est = abs(pnl_cuenta) / 100 * margen
            signo = "+" if pnl_cuenta >= 0 else ""
            pnl_lines.append(f"PnL registrado: {signo}${pnl_usdt_est:,.2f} USDT ({signo}{pnl_cuenta:.2f}% sobre margen de ${margen:,.2f})")
    pnl_block = "\n".join(pnl_lines) if pnl_lines else ""

    riesgo_block = "\n".join(riesgo_lines) if riesgo_lines else "⚠️ Sin datos de gestión de riesgo — análisis de posicionamiento no disponible"

    # Warning datos faltantes para el prompt
    faltantes = check_completeness({**trade_data, "estado": "CERRADO" if cierre else "ABIERTO"})
    faltantes_block = ""
    if faltantes:
        faltantes_block = f"\n[DATOS FALTANTES: {', '.join(faltantes)} — el análisis de gestión de riesgo es parcial]"

    return f"""
TRADE:
Activo:         {trade_data.get('activo', 'N/A')} — {dir_}
Entrada:        ${entr:,.2f}
Stop Loss:      {'$'+f"{sl:,.2f}" + f' ({riesgo_precio_pct}% movimiento en precio)' if sl else '⚠️ Sin SL'}
Take Profit:    {'$'+f"{tp:,.2f}" + f' ({obj_pct}% objetivo en precio)' if tp else 'Sin TP'}
Precio cierre:  {'$'+f"{cierre:,.2f}" + f' ({mov_precio}% movimiento real)' if cierre else 'N/A'}
Resultado:      {trade_data.get('resultado', 'Pendiente')}
PnL en cuenta:  {f"{trade_data.get('pnl', 0):+.2f}%" if trade_data.get('pnl') is not None else 'No registrado'}
Apalancamiento: {palstr}
R:R Planeado:   {trade_data.get('rr_planeado', 'N/A')}
R:R Real:       {trade_data.get('rr_real', 'N/A')}
Timeframe:      {trade_data.get('timeframe', 'No especificado')}
Tipo de trade:  {_tipo_trade_ctx(trade_data.get('tipo_trade','SWING'), trade_data.get('timeframe',''))}
Notas:          {trade_data.get('notas', 'Sin notas')}
Fecha:          {trade_data.get('fecha_trade', 'Hoy')}

GESTIÓN DE RIESGO:
{riesgo_block}
{f"PNL EN USDT (PRE-CALCULADO — USAR ESTOS NÚMEROS, NO RECALCULAR):{chr(10)}{pnl_block}" if pnl_block else ""}
{faltantes_block}
""".strip()


def calcular_metricas_riesgo(entrada: float, sl: float | None, direccion: str,
                              leverage: float, margen_usado: float | None,
                              capital_cuenta: float | None, tipo_margen: str) -> dict:
    """
    Calcula métricas de riesgo completas para el análisis IA.
    Todas son derivadas — no se guardan en DB, se pasan al modelo.
    """
    m = {}
    lev = max(leverage or 1.0, 1.0)

    # ── Tamaño de posición ───────────────────────────────────
    if margen_usado:
        m["posicion_usdt"] = round(margen_usado * lev, 2)

    # ── Precio de liquidación (solo AISLADO — CRUZADO depende del balance total) ──
    TASA_MANT = 0.005
    if lev > 1 and tipo_margen != "CRUZADO":
        if direccion == "LONG":
            m["precio_liquidacion"] = round(entrada * (1 - 1 / lev + TASA_MANT), 2)
        else:
            m["precio_liquidacion"] = round(entrada * (1 + 1 / lev - TASA_MANT), 2)

        # ¿El SL protege antes de ser liquidado?
        if sl and "precio_liquidacion" in m:
            liq = m["precio_liquidacion"]
            if direccion == "LONG":
                m["sl_antes_de_liq"] = sl > liq
            else:
                m["sl_antes_de_liq"] = sl < liq

    # ── Riesgo en USDT ───────────────────────────────────────
    if sl and margen_usado:
        dist_pct = abs(entrada - sl) / entrada
        riesgo_bruto = margen_usado * dist_pct * lev
        if tipo_margen == "AISLADO":
            m["riesgo_usdt"] = round(min(riesgo_bruto, margen_usado), 2)
        else:  # CRUZADO — puede perder más que el margen
            m["riesgo_usdt"] = round(riesgo_bruto, 2)

    # ── Riesgo % de cuenta ───────────────────────────────────
    if capital_cuenta and "riesgo_usdt" in m:
        m["riesgo_pct_cuenta"] = round(m["riesgo_usdt"] / capital_cuenta * 100, 2)

    # ── % de cuenta como margen (exposición) ─────────────────
    if capital_cuenta and margen_usado:
        m["exposicion_pct"] = round(margen_usado / capital_cuenta * 100, 1)

    return m


def check_completeness(trade_data: dict) -> list:
    """
    Devuelve lista de datos críticos que faltan.
    Se usa para el warning en el feedback de IA.
    """
    faltantes = []
    if not trade_data.get("capital_cuenta"):
        faltantes.append("capital total de cuenta")
    if not trade_data.get("margen_usado"):
        faltantes.append("margen usado (USDT)")
    if not trade_data.get("sl"):
        faltantes.append("stop loss")
    if trade_data.get("estado") == "CERRADO" and not trade_data.get("precio_cierre"):
        faltantes.append("precio de cierre exacto")
    return faltantes


def calcular_confianza_bot(activo: str, direccion: str) -> int:
    """
    Score 0-100 de confluencias técnicas al momento de entrada.
    Pesos: Scanner 40% | Régimen mercado 35% | Kill zone 10% | Base 15%
    Nunca retorna 0 ni 100 — siempre hay incertidumbre.
    """
    puntos = 15  # base neutral

    try:
        # ── Scanner confluencias (0-40 pts) ──────────────────
        from scanner import evaluar_confluencias
        scanner = evaluar_confluencias(activo)
        sc   = scanner.get("score", 0)   # 0-4
        bias = scanner.get("bias") or ""

        if bias == direccion:
            puntos += int(sc / 4 * 40)
        elif bias and bias != direccion:
            puntos += max(0, int((4 - sc) / 4 * 8))  # penalizar ir contra scanner
        else:
            puntos += int(sc / 4 * 20)  # sin bias claro

        # ── Régimen de mercado (0-35 pts) ────────────────────
        from binance_data import get_regimen_mercado
        reg = get_regimen_mercado(activo).get("regimen", "INDEFINIDO")
        alcistas = {"TENDENCIA_ALCISTA", "MOMENTUM_ALCISTA"}
        bajistas = {"TENDENCIA_BAJISTA", "MOMENTUM_BAJISTA"}
        if (direccion == "LONG"  and reg in alcistas) or \
           (direccion == "SHORT" and reg in bajistas):
            puntos += 35
        elif reg == "RANGO_NEUTRO":
            puntos += 17
        # else: régimen opuesto → 0 pts extra

        # ── Kill zone activa (0-10 pts) ───────────────────────
        hora = datetime.utcnow().hour
        if hora in {7, 8, 9, 10, 12, 13, 14, 15, 16, 17}:
            puntos += 10
        else:
            puntos += 4

    except Exception:
        return 50  # neutral si falla cualquier cálculo

    return min(97, max(3, puntos))


def calcular_pnl_perpetuo(entrada: float, precio_actual: float,
                          margen: float, apalancamiento: float,
                          direccion: str) -> dict:
    """
    Calcula PnL real para futuros perpetuos BTC/USDT (Binance).
    Usa la fórmula correcta: nocional → unidades → PnL, no diferencia × leverage.
    """
    TASA_MANT = 0.005
    lev = max(apalancamiento or 1.0, 1.0)
    nocional = margen * lev
    unidades = nocional / entrada

    if direccion == "SHORT":
        pnl_usd = unidades * (entrada - precio_actual)
        liq = entrada * (1 + 1 / lev - TASA_MANT)
    else:  # LONG
        pnl_usd = unidades * (precio_actual - entrada)
        liq = entrada * (1 - 1 / lev + TASA_MANT)

    pnl_pct = (pnl_usd / margen) * 100
    dist_liq = abs((liq - precio_actual) / precio_actual) * 100

    return {
        "nocional":           round(nocional, 2),
        "unidades_btc":       round(unidades, 6),
        "pnl_usd":            round(pnl_usd, 2),
        "pnl_pct":            round(pnl_pct, 2),
        "precio_liquidacion": round(liq, 2),
        "distancia_liq_pct":  round(dist_liq, 2),
    }


def calcular_pnl_real(entrada: float, cierre: float,
                      direccion: str, apalancamiento: float = 1.0) -> float:
    """
    PnL en % sobre el capital (con apalancamiento).
    Sin apalancamiento (1x) = movimiento real del precio.
    Con 10x: un 1% de precio = 10% de ganancia.
    """
    if direccion == "LONG":
        pnl_precio = (cierre - entrada) / entrada * 100
    else:
        pnl_precio = (entrada - cierre) / entrada * 100
    return round(pnl_precio * apalancamiento, 3)


def precio_salida_desde_pnl(entrada: float, pnl_cuenta_pct: float,
                             direccion: str, apalancamiento: float) -> float:
    """
    Calcula el precio de salida real a partir de PnL% en cuenta y apalancamiento.
    pnl_cuenta_pct: lo que ganó/perdió sobre el capital (ej: +38%)
    apalancamiento: leverage usado (ej: 10)
    """
    pnl_precio_pct = pnl_cuenta_pct / apalancamiento   # movimiento real del precio
    if direccion == "LONG":
        return round(entrada * (1 + pnl_precio_pct / 100), 4)
    else:
        return round(entrada * (1 - pnl_precio_pct / 100), 4)


def formatear_historial_para_ia(trades: list, stats: dict) -> str:
    """Formatea historial completo y stats para análisis profundo"""
    lines = [
        f"HISTORIAL DE TRADING — {stats.get('total', 0)} trades totales",
        f"Win Rate: {stats.get('win_rate', 0)}%",
        f"R:R Promedio: {stats.get('rr_promedio', 'N/A')}",
        f"PnL Total: {('+' if stats.get('pnl_total', 0) >= 0 else '') + str(round(stats.get('pnl_total', 0), 2)) + '%' if stats.get('pnl_total') is not None else 'N/A'}",
        f"Wins: {stats.get('wins', 0)} | Losses: {stats.get('losses', 0)} | BE: {stats.get('be', 0)}",
        "",
        "RENDIMIENTO POR ACTIVO:",
    ]

    for activo, data in stats.get("por_activo", {}).items():
        lines.append(f"  {activo}: {data['win_rate']}% win rate ({data['wins']}/{data['total']})")

    racha = stats.get("racha_actual", {})
    if racha.get("resultado"):
        lines.append(f"\nRacha actual: {racha['count']} {racha['resultado']} consecutivos")

    lines.append("\nÚLTIMOS 20 TRADES:")
    for i, t in enumerate(trades[:20], 1):
        resultado_emoji = "✅" if t["resultado"] == "WIN" else "❌" if t["resultado"] == "LOSS" else "➖"
        lines.append(
            f"{i:2}. {resultado_emoji} {t['activo']} {t['direccion']} "
            f"${t['entrada']:,.0f} "
            f"RR:{t.get('rr_real', '?')} "
            f"PnL:{('+' if t['pnl'] >= 0 else '') + str(round(t['pnl'], 1)) + '%' if t.get('pnl') is not None else '?'} "
            f"| {t.get('notas', '')[:50]}"
        )

    return "\n".join(lines)

# ============================================================
# RUTAS
# ============================================================

@journal_bp.route("/journal")
@login_required
def journal_page():
    """Sirve la página del journal"""
    return render_template("journal.html")


@journal_bp.route("/journal/trade", methods=["POST"])
@login_required
def registrar_trade():
    """
    Registra un nuevo trade y genera análisis rápido de IA.
    Free y Pro pueden registrar — el análisis IA es para ambos (rápido).
    """
    data = request.json or {}

    # ── Validaciones básicas ─────────────────────────────────
    activo    = data.get("activo", "").strip().upper()
    direccion = data.get("direccion", "").strip().upper()

    if not activo or not direccion:
        return jsonify({"ok": False, "error": "Activo y dirección son requeridos"}), 400

    if direccion not in ("LONG", "SHORT"):
        return jsonify({"ok": False, "error": "Dirección debe ser LONG o SHORT"}), 400

    try:
        entrada = float(data.get("entrada", 0))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Precio de entrada inválido"}), 400

    if entrada <= 0:
        return jsonify({"ok": False, "error": "El precio de entrada debe ser mayor a 0"}), 400

    # ── Parsear campos opcionales ────────────────────────────
    sl             = float(data["sl"])  if data.get("sl")  else None
    tp             = float(data["tp"])  if data.get("tp")  else None
    pnl            = float(data["pnl"]) if data.get("pnl") is not None else None
    precio_cierre  = float(data["precio_cierre"]) if data.get("precio_cierre") else None
    apalancamiento = max(1.0, float(data.get("apalancamiento") or 1.0))
    capital_cuenta = float(data["capital_cuenta"]) if data.get("capital_cuenta") else None
    margen_usado   = float(data["margen_usado"])   if data.get("margen_usado")   else None
    tipo_margen    = (data.get("tipo_margen") or "AISLADO").upper()
    if tipo_margen not in ("AISLADO", "CRUZADO"):
        tipo_margen = "AISLADO"
    tipo_trade     = (data.get("tipo_trade") or "SWING").upper()
    if tipo_trade not in ("SCALP", "SWING", "POSITION"):
        tipo_trade = "SWING"
    resultado      = (data.get("resultado") or "").upper() or None
    timeframe      = data.get("timeframe", "")
    notas          = data.get("notas", "")[:500]

    if resultado and resultado not in ("WIN", "LOSS", "BE"):
        return jsonify({"ok": False, "error": "Resultado debe ser WIN, LOSS o BE"}), 400

    # ── Estado ───────────────────────────────────────────────
    estado = "CERRADO" if resultado else "ABIERTO"

    # ── R:R planeado ─────────────────────────────────────────
    rr_planeado = calcular_rr(entrada, sl, tp, direccion) if sl and tp else None

    # ── R:R real y PnL — con apalancamiento correcto ─────────
    rr_real   = None
    pnl_final = pnl  # guardamos el pnl% que ingresó el usuario

    if estado == "CERRADO":
        # Prioridad: precio_cierre exacto > inferir desde pnl + leverage
        if precio_cierre:
            salida = precio_cierre
            pnl_final = calcular_pnl_real(entrada, salida, direccion, apalancamiento)
        elif pnl is not None:
            salida = precio_salida_desde_pnl(entrada, pnl, direccion, apalancamiento)
            precio_cierre = salida
        else:
            salida = None

        if salida and sl:
            rr_real = calcular_rr_real(entrada, salida, sl, direccion)

    # ── Crear registro ───────────────────────────────────────
    trade = Journal(
        usuario_id     = current_user.id,
        activo         = activo,
        direccion      = direccion,
        entrada        = entrada,
        sl             = sl,
        tp             = tp,
        resultado      = resultado,
        pnl            = pnl_final,
        precio_cierre  = precio_cierre,
        rr_planeado    = rr_planeado,
        rr_real        = rr_real,
        timeframe      = timeframe,
        notas          = notas,
        estado         = estado,
        fuente         = "MANUAL",
        apalancamiento = apalancamiento,
        capital_cuenta = capital_cuenta,
        margen_usado   = margen_usado,
        tipo_margen    = tipo_margen,
        tipo_trade     = tipo_trade,
    )
    db.session.add(trade)
    db.session.flush()

    # ── Confianza bot + análisis apertura — SOLO trades ABIERTOS ──────────
    ia_apertura = None
    if estado == "ABIERTO":
        try:
            trade.confianza_bot = calcular_confianza_bot(activo, direccion)
        except Exception:
            pass

        pass  # análisis vía ChatBot — botón directo en el journal

    # ── Trades CERRADOS: sin análisis rápido — usar ChatBot para análisis profundo ──
    ia_feedback = None

    db.session.commit()

    faltantes = check_completeness({
        "capital_cuenta": capital_cuenta, "margen_usado": margen_usado,
        "sl": sl, "precio_cierre": precio_cierre, "estado": estado,
    })

    return jsonify({
        "ok":            True,
        "trade":         trade.to_dict(),
        "trade_id":      trade.id,
        "estado":        estado,
        "confianza_bot": trade.confianza_bot,
        "ia_feedback":   ia_feedback,
        "ia_apertura":   ia_apertura,
        "datos_faltantes": faltantes,
        "rr_planeado":   rr_planeado,
        "rr_real":       rr_real,
    })


@journal_bp.route("/journal/trades", methods=["GET"])
@login_required
def listar_trades():
    """Retorna los últimos trades del usuario"""
    limite = min(int(request.args.get("limite", 50)), 200)
    trades = Journal.listar(current_user.id, limite=limite)
    stats  = Journal.stats(current_user.id)
    return jsonify({
        "ok":     True,
        "trades": trades,
        "stats":  stats,
    })


@journal_bp.route("/journal/stats", methods=["GET"])
@login_required
def get_stats():
    """Retorna solo las estadísticas — para el dashboard"""
    stats = Journal.stats(current_user.id)
    return jsonify({"ok": True, "stats": stats})


@journal_bp.route("/journal/analisis-profundo", methods=["POST"])
@login_required
@pro_required
def analisis_profundo():
    """
    Análisis de patrones con RAG — solo Pro.
    Claude analiza el historial completo + metodología de la KB.
    """
    trades = Journal.listar(current_user.id, limite=50)
    stats  = Journal.stats(current_user.id)

    if stats.get("total", 0) < 3:
        return jsonify({
            "ok":    False,
            "error": "Necesitas al menos 3 trades registrados para el análisis profundo."
        }), 400

    try:
        # ── Contexto de la KB — metodología institucional ────
        pregunta_kb = "patrones de error trading, gestión de riesgo, psicología del trader, win rate, R:R"
        contexto_kb = buscar_contexto(pregunta_kb, n=3)

        # ── Historial formateado ─────────────────────────────
        historial_texto = formatear_historial_para_ia(trades, stats)

        # ── Precio actual para trades ABIERTOS — PnL pre-calculado ──
        data      = request.json or {}
        pregunta  = data.get("pregunta", "").strip()
        if not pregunta:
            pregunta = "Analiza mis trades y dime exactamente qué estoy haciendo mal y cómo mejorar."

        # Inyectar PnL pre-calculado de trades abiertos para que Claude NO recalcule
        trades_obj = Journal.query.filter_by(
            usuario_id=current_user.id, estado="ABIERTO"
        ).all()
        pnl_abiertos_block = ""
        if trades_obj:
            try:
                from binance_data import get_precio_actual
                lineas_pnl = ["PNL ACTUAL TRADES ABIERTOS (PRE-CALCULADO — USAR ESTOS, NO RECALCULAR):"]
                for t in trades_obj:
                    if t.margen_usado and t.apalancamiento and t.entrada:
                        activo_sym = t.activo.replace("/", "") + "T" if "/" not in t.activo else t.activo
                        if not activo_sym.endswith("USDT"):
                            activo_sym = t.activo.split("/")[0] + "/USDT"
                        precio_data = get_precio_actual(activo_sym)
                        if precio_data and precio_data.get("precio"):
                            precio_now = precio_data["precio"]
                            p = calcular_pnl_perpetuo(
                                t.entrada, precio_now,
                                t.margen_usado, t.apalancamiento,
                                t.direccion
                            )
                            signo = "+" if p["pnl_usd"] >= 0 else ""
                            lineas_pnl.append(
                                f"  Trade #{t.id} {t.activo} {t.direccion} entrada ${t.entrada:,.2f}: "
                                f"precio ahora ${precio_now:,.2f} | "
                                f"PnL no realizado: {signo}${p['pnl_usd']:,.2f} ({signo}{p['pnl_pct']:.2f}% sobre margen) | "
                                f"Liquidación en: ${p['precio_liquidacion']:,.2f} ({p['distancia_liq_pct']:.1f}% de distancia)"
                            )
                if len(lineas_pnl) > 1:
                    pnl_abiertos_block = "\n".join(lineas_pnl)
            except Exception:
                pass

        mensaje = f"""Conocimiento institucional:
{contexto_kb}

{historial_texto}
{chr(10) + pnl_abiertos_block if pnl_abiertos_block else ""}

Pregunta del trader: {pregunta}"""

        resp = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=800,
            system=SYSTEM_JOURNAL_PROFUNDO,
            messages=[{"role": "user", "content": mensaje}]
        )

        analisis = resp.content[0].text

        return jsonify({
            "ok":      True,
            "analisis": analisis,
            "trades_analizados": stats.get("total", 0),
        })

    except Exception as e:
        return jsonify({"ok": False, "error": "Error generando análisis. Intenta de nuevo."}), 500


@journal_bp.route("/journal/trade/<int:trade_id>/cerrar", methods=["PATCH"])
@login_required
def cerrar_trade(trade_id: int):
    """
    Cierra un trade ABIERTO: recibe precio_cierre + resultado,
    calcula PnL real + duración, genera ia_feedback.
    """
    trade = db.session.get(Journal, trade_id)
    if not trade:
        return jsonify({"ok": False, "error": "Trade no encontrado"}), 404
    if trade.usuario_id != current_user.id:
        return jsonify({"ok": False, "error": "No autorizado"}), 403
    if trade.estado == "CERRADO":
        return jsonify({"ok": False, "error": "Este trade ya está cerrado"}), 400

    data = request.json or {}
    try:
        precio_cierre = float(data["precio_cierre"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "precio_cierre requerido"}), 400

    resultado = (data.get("resultado") or "").upper() or None
    if resultado and resultado not in ("WIN", "LOSS", "BE"):
        return jsonify({"ok": False, "error": "Resultado debe ser WIN, LOSS o BE"}), 400

    # Auto-detectar resultado si no viene
    if not resultado:
        if trade.direccion == "LONG":
            resultado = "WIN" if precio_cierre > trade.entrada else "LOSS"
        else:
            resultado = "WIN" if precio_cierre < trade.entrada else "LOSS"

    # Calcular métricas de cierre — con apalancamiento real del trade
    apal     = trade.apalancamiento or 1.0
    pnl_pct  = calcular_pnl_real(trade.entrada, precio_cierre, trade.direccion, apal)
    rr_real  = calcular_rr_real(trade.entrada, precio_cierre, trade.sl, trade.direccion) if trade.sl else None
    ahora    = datetime.utcnow()
    duracion = int((ahora - trade.creado_en).total_seconds() / 60) if trade.creado_en else None

    # Actualizar registro
    trade.precio_cierre    = precio_cierre
    trade.resultado        = resultado
    trade.pnl              = pnl_pct
    trade.rr_real          = rr_real
    trade.duracion_minutos = duracion
    trade.fecha_cierre     = ahora
    trade.estado           = "CERRADO"
    if data.get("notas"):
        trade.notas = (trade.notas or "") + f"\nCierre: {data['notas']}"

    db.session.flush()

    # ── Sin análisis rápido al cerrar — usar ChatBot para análisis profundo ──
    ia_feedback = None

    db.session.commit()

    faltantes = check_completeness({
        "capital_cuenta": trade.capital_cuenta, "margen_usado": trade.margen_usado,
        "sl": trade.sl, "precio_cierre": precio_cierre, "estado": "CERRADO",
    })

    return jsonify({
        "ok":              True,
        "trade":           trade.to_dict(),
        "ia_feedback":     ia_feedback,
        "datos_faltantes": faltantes,
        "pnl_pct":         pnl_pct,
        "rr_real":         rr_real,
    })


@journal_bp.route("/journal/importar-csv", methods=["POST"])
@login_required
def importar_csv():
    """
    Importa trades desde CSV de Bitunix, Binance o Bybit.
    Detecta el formato por los nombres de columna.
    Retorna cuántos trades se importaron y cuántos se saltaron (duplicados).
    """
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No se adjuntó archivo"}), 400

    f       = request.files["file"]
    content = f.read().decode("utf-8-sig")  # utf-8-sig maneja BOM de Excel
    reader  = csv.DictReader(io.StringIO(content))
    cols    = [c.lower().strip() for c in (reader.fieldnames or [])]

    # ── Detectar exchange por columnas ──────────────────────
    if any("open price" in c or "open_price" in c for c in cols):
        fmt = "bitunix"
    elif "realizedprofit" in "".join(cols) or "realized profit" in " ".join(cols):
        fmt = "binance"
    elif "closedpnl" in "".join(cols) or "closed pnl" in " ".join(cols):
        fmt = "bybit"
    else:
        fmt = "generico"

    def _col(row, *keys):
        """Busca la primera key que exista en el row (case-insensitive)."""
        for k in keys:
            for rk in row:
                if rk.lower().strip() == k.lower():
                    v = row[rk]
                    return v.strip() if isinstance(v, str) else v
        return None

    importados = 0
    saltados   = 0
    errores    = []

    for i, row in enumerate(reader):
        try:
            if fmt == "bitunix":
                symbol    = _col(row, "symbol", "contract") or ""
                side      = (_col(row, "direction", "side", "type") or "").upper()
                entrada   = float(_col(row, "open price", "avg open price") or 0)
                cierre    = float(_col(row, "close price", "avg close price") or 0)
                pnl_val   = float(_col(row, "realized pnl", "profit") or 0)
                open_time = _col(row, "open time", "created time", "time")
                ext_id    = _col(row, "order id", "trade id", "id") or f"bitunix_{i}"

            elif fmt == "binance":
                symbol    = _col(row, "symbol") or ""
                side      = (_col(row, "side") or "").upper()
                entrada   = float(_col(row, "price", "avg price") or 0)
                cierre    = float(_col(row, "realized profit") or entrada)
                pnl_val   = float(_col(row, "realizedprofit", "realized profit") or 0)
                open_time = _col(row, "time", "date")
                ext_id    = _col(row, "order id", "tradeid") or f"binance_{i}"

            elif fmt == "bybit":
                symbol    = _col(row, "symbol", "contract") or ""
                side      = (_col(row, "side", "direction") or "").upper()
                entrada   = float(_col(row, "entry price", "avg entry price") or 0)
                cierre    = float(_col(row, "exit price", "avg exit price") or entrada)
                pnl_val   = float(_col(row, "closed pnl", "closedpnl") or 0)
                open_time = _col(row, "open time", "created time")
                ext_id    = _col(row, "order id", "id") or f"bybit_{i}"

            else:  # generico
                symbol    = _col(row, "symbol", "activo", "pair") or ""
                side      = (_col(row, "side", "direction", "direccion") or "").upper()
                entrada   = float(_col(row, "entry", "entrada", "open price", "price") or 0)
                cierre    = float(_col(row, "exit", "cierre", "close price") or 0)
                pnl_val   = float(_col(row, "pnl", "profit", "resultado_pnl") or 0)
                open_time = _col(row, "date", "time", "fecha")
                ext_id    = _col(row, "id", "order_id", "trade_id") or f"csv_{i}"

            if not symbol or entrada <= 0:
                saltados += 1
                continue

            # Normalizar símbolo y dirección
            symbol = symbol.replace("/", "").upper()
            if not symbol.endswith("USDT"):
                symbol += "USDT"

            if "BUY" in side or "LONG" in side:
                direccion = "LONG"
            elif "SELL" in side or "SHORT" in side:
                direccion = "SHORT"
            else:
                saltados += 1
                continue

            # Evitar duplicados por exchange_trade_id
            dup = Journal.query.filter_by(
                usuario_id=current_user.id,
                exchange_trade_id=str(ext_id)
            ).first()
            if dup:
                saltados += 1
                continue

            # Resultado basado en PnL
            if pnl_val > 0:
                resultado = "WIN"
            elif pnl_val < 0:
                resultado = "LOSS"
            else:
                resultado = "BE"

            # Fecha del trade
            fecha = date.today()
            if open_time:
                for fmt_str in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
                                "%m/%d/%Y %H:%M:%S", "%d/%m/%Y"):
                    try:
                        fecha = datetime.strptime(str(open_time)[:19], fmt_str).date()
                        break
                    except ValueError:
                        continue

            pnl_pct = calcular_pnl_real(entrada, cierre, direccion) if cierre > 0 else None
            rr_real = None  # no tenemos SL en el CSV

            t = Journal(
                usuario_id        = current_user.id,
                activo            = symbol,
                direccion         = direccion,
                entrada           = entrada,
                precio_cierre     = cierre if cierre > 0 else None,
                resultado         = resultado,
                pnl               = pnl_pct,
                pnl_real          = pnl_val,
                estado            = "CERRADO",
                fuente            = fmt.upper(),
                exchange_trade_id = str(ext_id),
                fecha_trade       = fecha,
            )
            db.session.add(t)
            importados += 1

        except Exception as ex:
            errores.append(f"Fila {i+1}: {str(ex)}")
            saltados += 1
            continue

    db.session.commit()

    return jsonify({
        "ok":        True,
        "importados": importados,
        "saltados":   saltados,
        "errores":    errores[:10],  # máximo 10 errores en respuesta
        "mensaje":   f"{importados} trades importados, {saltados} saltados (duplicados o inválidos)."
    })


@journal_bp.route("/journal/trade/<int:trade_id>", methods=["DELETE"])
@login_required
def borrar_trade(trade_id: int):
    """Borra un trade — solo el dueño puede borrarlo"""
    trade = db.session.get(Journal, trade_id)

    if not trade:
        return jsonify({"ok": False, "error": "Trade no encontrado"}), 404

    if trade.usuario_id != current_user.id:
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    db.session.delete(trade)
    db.session.commit()

    return jsonify({"ok": True})