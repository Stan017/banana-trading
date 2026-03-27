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
from datetime import datetime
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
No eres asesor financiero — análisis educativo."""

SYSTEM_JOURNAL_PROFUNDO = """Eres TradeBot, el mentor institucional de trading más exigente.
Tienes acceso al historial completo de trades del usuario y a metodología institucional.
Tu trabajo: encontrar los patrones de error que el trader no puede ver por sí mismo.
Sé brutalmente honesto — la comodidad no mejora a los traders.
El objetivo es que el usuario salga de este análisis sabiendo exactamente qué está haciendo mal y cómo corregirlo.
Máximo 400 palabras. Usa secciones claras. No eres asesor financiero — análisis educativo."""

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


def formatear_trade_para_ia(trade_data: dict) -> str:
    """Formatea los datos del trade para mandárselos a Claude"""
    return f"""
TRADE REGISTRADO:
Activo:     {trade_data.get('activo', 'N/A')}
Dirección:  {trade_data.get('direccion', 'N/A')}
Entrada:    ${trade_data.get('entrada', 0):,.2f}
Stop Loss:  ${trade_data.get('sl', 0):,.2f} {f"({round(abs(trade_data.get('sl',0) - trade_data.get('entrada',0)) / trade_data.get('entrada',1) * 100, 2)}% de riesgo)" if trade_data.get('sl') else ''}
Take Profit:${trade_data.get('tp', 0):,.2f} {f"({round(abs(trade_data.get('tp',0) - trade_data.get('entrada',0)) / trade_data.get('entrada',1) * 100, 2)}% objetivo)" if trade_data.get('tp') else ''}
Resultado:  {trade_data.get('resultado', 'Pendiente')}
PnL:        {f"{trade_data.get('pnl', 0):+.2f}%" if trade_data.get('pnl') is not None else 'No registrado'}
R:R Plan:   {trade_data.get('rr_planeado', 'N/A')}
R:R Real:   {trade_data.get('rr_real', 'N/A')}
Timeframe:  {trade_data.get('timeframe', 'No especificado')}
Notas:      {trade_data.get('notas', 'Sin notas')}
Fecha:      {trade_data.get('fecha_trade', 'Hoy')}
""".strip()


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
    sl        = float(data["sl"])  if data.get("sl")  else None
    tp        = float(data["tp"])  if data.get("tp")  else None
    pnl       = float(data["pnl"]) if data.get("pnl") is not None else None
    resultado = data.get("resultado", "").upper() or None
    timeframe = data.get("timeframe", "")
    notas     = data.get("notas", "")[:500]  # max 500 chars

    if resultado and resultado not in ("WIN", "LOSS", "BE"):
        return jsonify({"ok": False, "error": "Resultado debe ser WIN, LOSS o BE"}), 400

    # ── Calcular R:R automáticamente ────────────────────────
    rr_planeado = calcular_rr(entrada, sl, tp, direccion) if sl and tp else None

    # R:R real si hay resultado y PnL
    rr_real = None
    if sl and pnl is not None:
        salida_estimada = entrada * (1 + pnl/100) if direccion == "LONG" else entrada * (1 - pnl/100)
        rr_real = calcular_rr_real(entrada, salida_estimada, sl, direccion)

    # ── Crear registro ───────────────────────────────────────
    trade = Journal(
        usuario_id  = current_user.id,
        activo      = activo,
        direccion   = direccion,
        entrada     = entrada,
        sl          = sl,
        tp          = tp,
        resultado   = resultado,
        pnl         = pnl,
        rr_planeado = rr_planeado,
        rr_real     = rr_real,
        timeframe   = timeframe,
        notas       = notas,
    )
    db.session.add(trade)
    db.session.flush()  # obtener ID sin commit

    # ── Análisis rápido de IA ────────────────────────────────
    ia_feedback = None
    try:
        trade_texto = formatear_trade_para_ia({
            "activo":       activo,
            "direccion":    direccion,
            "entrada":      entrada,
            "sl":           sl,
            "tp":           tp,
            "resultado":    resultado,
            "pnl":          pnl,
            "rr_planeado":  rr_planeado,
            "rr_real":      rr_real,
            "timeframe":    timeframe,
            "notas":        notas,
            "fecha_trade":  str(data.get("fecha_trade", "Hoy")),
        })

        resp = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            system=SYSTEM_JOURNAL_RAPIDO,
            messages=[{
                "role": "user",
                "content": f"{trade_texto}\n\nDame feedback directo sobre este trade."
            }]
        )
        ia_feedback = resp.content[0].text
        trade.ia_feedback = ia_feedback

    except Exception as e:
        ia_feedback = None  # no bloquear el guardado si falla la IA

    db.session.commit()

    return jsonify({
        "ok":          True,
        "trade":       trade.to_dict(),
        "ia_feedback": ia_feedback,
        "rr_planeado": rr_planeado,
        "rr_real":     rr_real,
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

        # ── Pregunta del usuario (opcional) ─────────────────
        data      = request.json or {}
        pregunta  = data.get("pregunta", "").strip()
        if not pregunta:
            pregunta = "Analiza mis trades y dime exactamente qué estoy haciendo mal y cómo mejorar."

        mensaje = f"""Conocimiento institucional:
{contexto_kb}

{historial_texto}

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
