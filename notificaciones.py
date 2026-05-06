"""
notificaciones.py — Scheduler de alertas para TradeBot AI
═══════════════════════════════════════════════════════════
Crea notificaciones in-app y envía Telegram cuando ocurren:
  1. Trade < 5% de liquidación   → Telegram + in-app 🔴 (cada 2 min)
  2. Trade < 3% de su SL         → Telegram + in-app 🟡 (cada 2 min)
  3. Scanner 4/4 confluencias    → Telegram + in-app   (cada 5 min)
  4. Scanner 3/4 potencial       → in-app              (cada 5 min)
  5. Cambio régimen macro        → Telegram + in-app   (cada 10 min)
  6. Funding ELEVADO/EXTREMO/CRITICO + OI Spike → in-app + Telegram
  7. Morning briefing 8 AM       → Email + Telegram    (diario)
"""

import time
import logging
import requests
from datetime import datetime, date
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

# ── Cooldowns anti-spam (en memoria, se resetean al reiniciar) ────────────────
# clave: (usuario_id, tipo, ref_id)  →  timestamp último envío
_last_sent: dict = {}

COOLDOWN = {
    "liquidacion":      300,    # 5 min entre misma alerta
    "sl":               300,
    "scanner4":         600,
    "scanner3":         900,
    "regimen":          1800,
    "funding":          1800,   # fallback genérico (no usado por check_funding)
    "funding_elevado":  3600,   # 1h
    "funding_extremo":  1800,   # 30 min
    "funding_critico":  900,    # 15 min
    "oi_spike":         3600,   # 1h
    "briefing":         86400,
}


def _cooldown_ok(key: tuple, tipo: str) -> bool:
    """Retorna True si ya pasó suficiente tiempo desde la última notificación."""
    cd = COOLDOWN.get(tipo, 600)
    last = _last_sent.get(key, 0)
    return (time.time() - last) >= cd


def _mark_sent(key: tuple):
    _last_sent[key] = time.time()


# ── Telegram helper ───────────────────────────────────────────────────────────

def _telegram(msg: str, chat_id: str = None):
    """Envía mensaje Telegram vía HTTP simple (no async bot)."""
    if not TELEGRAM_TOKEN:
        return
    cid = chat_id or TELEGRAM_CHAT_ID
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")


# ── Guardar notificación in-app ───────────────────────────────────────────────

def _guardar_notif(app, usuario_id: int, tipo: str, nivel: str,
                   titulo: str, mensaje: str, trade_id: int = None):
    """Crea una Notificacion en DB dentro del app context dado."""
    with app.app_context():
        from models import db, Notificacion
        from datetime import timedelta
        hace_10 = datetime.utcnow() - timedelta(minutes=10)
        existe = Notificacion.query.filter(
            Notificacion.usuario_id == usuario_id,
            Notificacion.tipo == tipo,
            Notificacion.titulo == titulo,
            Notificacion.creada_en >= hace_10,
        ).first()
        if existe:
            return
        n = Notificacion(
            usuario_id=usuario_id,
            tipo=tipo,
            nivel=nivel,
            titulo=titulo,
            mensaje=mensaje,
            trade_id=trade_id,
        )
        db.session.add(n)
        db.session.commit()


# ── Checkers ──────────────────────────────────────────────────────────────────

def check_liquidacion_sl(app):
    """
    Revisa todos los trades ABIERTOS con apalancamiento.
    Alerta si el precio actual está < 5% de la liquidación o < 3% del SL.
    """
    with app.app_context():
        from models import Journal, Usuario
        from binance_data import get_precio_actual

        trades_abiertos = Journal.query.filter_by(estado="ABIERTO").all()

        for trade in trades_abiertos:
            if not trade.apalancamiento or trade.apalancamiento <= 1:
                continue

            sym = trade.activo or "BTC"
            if "/" not in sym:
                sym = sym + "/USDT"

            precio_data = get_precio_actual(sym)
            if not precio_data:
                continue
            precio = precio_data.get("precio") or precio_data.get("price")
            if not precio:
                continue

            lev = trade.apalancamiento
            TASA_MANT = 0.005

            # ── Alerta de liquidación (solo AISLADO) ──────────────────────
            if trade.tipo_margen != "CRUZADO":
                if trade.direccion == "LONG":
                    liq = trade.entrada * (1 - 1 / lev + TASA_MANT)
                else:
                    liq = trade.entrada * (1 + 1 / lev - TASA_MANT)

                dist_liq_pct = abs((liq - precio) / precio) * 100

                if dist_liq_pct < 5:
                    key = (trade.usuario_id, "liquidacion", trade.id)
                    if _cooldown_ok(key, "liquidacion"):
                        titulo = f"🔴 Cerca de liquidación — {trade.activo}"
                        msg = (
                            f"{trade.direccion} {trade.activo} x{int(lev)}\n"
                            f"Precio actual: ${precio:,.2f}\n"
                            f"Liquidación: ${liq:,.2f} ({dist_liq_pct:.1f}% de distancia)\n"
                            f"⚠️ Riesgo de liquidación inminente"
                        )
                        _guardar_notif(app, trade.usuario_id, "liquidacion",
                                       "ROJO", titulo, msg, trade_id=trade.id)
                        _telegram(
                            f"🔴 *ALERTA LIQUIDACIÓN*\n{trade.direccion} {trade.activo} x{int(lev)}\n"
                            f"Precio: `${precio:,.2f}` | Liq: `${liq:,.2f}` ({dist_liq_pct:.1f}%)"
                        )
                        _mark_sent(key)

            # ── Alerta de SL ───────────────────────────────────────────────
            if trade.sl:
                dist_sl_pct = abs((trade.sl - precio) / precio) * 100

                cerca = False
                if trade.direccion == "LONG" and precio > trade.sl:
                    cerca = dist_sl_pct < 3
                elif trade.direccion == "SHORT" and precio < trade.sl:
                    cerca = dist_sl_pct < 3

                if cerca:
                    key = (trade.usuario_id, "sl", trade.id)
                    if _cooldown_ok(key, "sl"):
                        titulo = f"🟡 SL cercano — {trade.activo}"
                        msg = (
                            f"{trade.direccion} {trade.activo}\n"
                            f"Precio: ${precio:,.2f} | SL: ${trade.sl:,.2f}\n"
                            f"Distancia al SL: {dist_sl_pct:.1f}%"
                        )
                        _guardar_notif(app, trade.usuario_id, "sl",
                                       "AMARILLO", titulo, msg, trade_id=trade.id)
                        _telegram(
                            f"🟡 *SL CERCANO*\n{trade.direccion} {trade.activo}\n"
                            f"Precio: `${precio:,.2f}` | SL: `${trade.sl:,.2f}` ({dist_sl_pct:.1f}%)"
                        )
                        _mark_sent(key)


def check_scanner(app):
    """
    Evalúa confluencias del scanner HTF.
    Alerta si hay 4/4 (Telegram + in-app) o 3/4 (solo in-app).
    """
    with app.app_context():
        try:
            from scanner import evaluar_confluencias
            from models import Usuario
        except ImportError:
            return

        try:
            resultado = evaluar_confluencias("BTC")
            if not resultado:
                return

            confluencias = resultado.get("confluencias_activas", 0)
            direccion = resultado.get("direccion", "")

            if confluencias >= 4:
                key = (0, "scanner4", "BTC")
                if _cooldown_ok(key, "scanner4"):
                    titulo = f"Scanner 4/4 — {direccion} BTC"
                    msg = (
                        f"Confluencias HTF completas: {confluencias}/4\n"
                        f"Dirección: {direccion}\n"
                        f"Condiciones institucionales activas"
                    )
                    # Notificar a todos los usuarios activos
                    usuarios = Usuario.query.filter_by(activo=True).all()
                    for u in usuarios:
                        _guardar_notif(app, u.id, "scanner4", "INFO", titulo, msg)
                    _telegram(
                        f"📊 *SCANNER 4/4 — {direccion}*\n"
                        f"BTC: {confluencias} confluencias HTF activas\n"
                        f"Condiciones institucionales confirmadas"
                    )
                    _mark_sent(key)

            elif confluencias == 3:
                key = (0, "scanner3", "BTC")
                if _cooldown_ok(key, "scanner3"):
                    titulo = f"Scanner 3/4 potencial — {direccion} BTC"
                    msg = (
                        f"3 de 4 confluencias activas — setup en formación\n"
                        f"Dirección: {direccion}"
                    )
                    usuarios = Usuario.query.filter_by(activo=True).all()
                    for u in usuarios:
                        _guardar_notif(app, u.id, "scanner3", "INFO", titulo, msg)
                    _mark_sent(key)

        except Exception as e:
            logger.warning(f"check_scanner error: {e}")


def check_regimen(app):
    """
    Detecta cambios en el régimen macro (bull/bear/rango/crisis).
    Alerta si cambió desde la última verificación.
    """
    with app.app_context():
        try:
            from binance_data import get_regimen_mercado
            from models import Usuario
        except ImportError:
            return

        try:
            regimen = get_regimen_mercado()
            if not regimen:
                return

            # Detectar cambio vs estado anterior (guardado en módulo)
            nuevo = regimen.get("regimen", "")
            anterior = _regimen_anterior.get("regimen", "")

            if nuevo and anterior and nuevo != anterior:
                key = (0, "regimen", nuevo)
                if _cooldown_ok(key, "regimen"):
                    titulo = f"Cambio de régimen: {anterior} → {nuevo}"
                    msg = (
                        f"El régimen macro cambió de *{anterior}* a *{nuevo}*.\n"
                        f"Revisa tus posiciones abiertas y ajusta el sesgo."
                    )
                    usuarios = Usuario.query.filter_by(activo=True).all()
                    for u in usuarios:
                        _guardar_notif(app, u.id, "regimen", "AMARILLO", titulo, msg)
                    _telegram(
                        f"⚡ *CAMBIO RÉGIMEN MACRO*\n{anterior} → *{nuevo}*\n"
                        f"Ajusta sesgo en posiciones abiertas"
                    )
                    _mark_sent(key)

            _regimen_anterior["regimen"] = nuevo

        except Exception as e:
            logger.warning(f"check_regimen error: {e}")


# Estado previo del régimen (singleton en módulo)
_regimen_anterior: dict = {}


def check_funding(app):
    """
    Alerta de funding rate en 3 niveles + spike de OI.

    Niveles de funding (valor absoluto en %):
      ELEVADO  ≥ 0.05%  o  percentil_abs ≥ 75  → AMARILLO  cooldown 3600s
      EXTREMO  ≥ 0.10%  o  percentil_abs ≥ 90  → NARANJA   cooldown 1800s
      CRITICO  ≥ 0.15%  o  percentil_abs ≥ 95  → ROJO      cooldown  900s

    OI Spike:
      cambio_24h ≥ 15%  → alerta independiente AMARILLO    cooldown 3600s
    """
    with app.app_context():
        try:
            from binance_data import get_funding_percentil, get_open_interest
            from models import Usuario
        except ImportError:
            return

        try:
            usuarios = Usuario.query.filter_by(activo=True).all()
            if not usuarios:
                return

            # ── Datos de funding ────────────────────────────────────────────────
            fp = get_funding_percentil("BTC/USDT", n_dias=90)
            funding  = fp.get("current")        # ya en %, ej. 0.0100
            pct_abs  = fp.get("percentil_abs")  # 0–100
            n_hist   = fp.get("n", 0)

            if funding is None:
                return

            abs_funding = abs(funding)
            signo       = "+" if funding > 0 else ""
            direccion   = "longs" if funding > 0 else "shorts"
            contexto_dir = (
                "Longs pagando → presión bajista latente 🔴"
                if funding > 0
                else "Shorts pagando → posible squeeze alcista 🟢"
            )
            pct_str = (
                f" | top {100 - pct_abs:.0f}% históricamente"
                if pct_abs is not None and n_hist >= 20
                else ""
            )

            # Determinar nivel (más alto primero)
            nivel = None
            if abs_funding >= 0.15 or (pct_abs is not None and pct_abs >= 95):
                nivel = "CRITICO"
            elif abs_funding >= 0.10 or (pct_abs is not None and pct_abs >= 90):
                nivel = "EXTREMO"
            elif abs_funding >= 0.05 or (pct_abs is not None and pct_abs >= 75):
                nivel = "ELEVADO"

            if nivel:
                cooldown_nivel = {"ELEVADO": 3600, "EXTREMO": 1800, "CRITICO": 900}
                color_nivel    = {"ELEVADO": "AMARILLO", "EXTREMO": "NARANJA", "CRITICO": "ROJO"}
                emoji_nivel    = {"ELEVADO": "⚠️", "EXTREMO": "🔥", "CRITICO": "🚨"}

                cd   = cooldown_nivel[nivel]
                key  = (0, f"funding_{nivel.lower()}", round(abs_funding * 1000))

                # Cooldown manual porque el nivel varía
                last = _last_sent.get(key, 0)
                if (time.time() - last) >= cd:
                    titulo = (
                        f"{emoji_nivel[nivel]} Funding {nivel} BTC: "
                        f"{signo}{funding:.4f}%{pct_str}"
                    )
                    msg = (
                        f"Funding Rate BTC: {signo}{funding:.4f}%\n"
                        f"{contexto_dir}\n"
                        f"Nivel: {nivel}{pct_str}\n"
                        f"Muestras históricas: {n_hist} registros (90d)"
                    )
                    for u in usuarios:
                        _guardar_notif(
                            app, u.id, "funding",
                            color_nivel[nivel], titulo, msg,
                        )
                    _mark_sent(key)
                    logger.info(f"Funding {nivel}: {signo}{funding:.4f}%{pct_str}")

            # ── OI Spike ────────────────────────────────────────────────────────
            oi = get_open_interest("BTC/USDT")
            if oi and oi.get("cambio_24h") is not None:
                delta_24h = oi["cambio_24h"]
                if abs(delta_24h) >= 15:
                    key_oi = (0, "oi_spike", int(abs(delta_24h)))
                    last_oi = _last_sent.get(key_oi, 0)
                    if (time.time() - last_oi) >= 3600:
                        dir_oi  = "subió" if delta_24h > 0 else "bajó"
                        emoji_oi = "📈" if delta_24h > 0 else "📉"
                        titulo_oi = f"{emoji_oi} OI Spike BTC: {dir_oi} {abs(delta_24h):.1f}% en 24h"
                        msg_oi = (
                            f"Open Interest BTC {dir_oi} {abs(delta_24h):.1f}% en 24h\n"
                            f"OI actual: {oi['valor']:,.0f} contratos\n"
                            f"{'Entrada masiva de capital — apalancamiento creciente ⚠️' if delta_24h > 0 else 'Cierre masivo de posiciones — desapalancamiento 🧯'}"
                        )
                        for u in usuarios:
                            _guardar_notif(
                                app, u.id, "funding",
                                "AMARILLO", titulo_oi, msg_oi,
                            )
                        _mark_sent(key_oi)
                        logger.info(f"OI spike: {delta_24h:+.1f}% 24h")

        except Exception as e:
            logger.warning(f"check_funding error: {e}")


def check_triggers(app):
    """
    Verifica si algún trigger activo fue cruzado por el precio actual.
    Corre cada 2 minutos (mismo ciclo que check_liquidacion_sl).
    """
    with app.app_context():
        try:
            from models import db, ActiveTrigger
            from binance_data import get_precio_actual
            from datetime import datetime as _dt

            triggers = ActiveTrigger.query.filter_by(activo=True, disparado=False).all()
            if not triggers:
                return

            # Agrupar por symbol para minimizar llamadas API
            symbols = list(set(t.symbol or "BTC/USDT" for t in triggers))
            precios = {}
            for sym in symbols:
                p = get_precio_actual(sym)
                if p and p.get("precio"):
                    precios[sym] = float(p["precio"])

            for t in triggers:
                sym    = t.symbol or "BTC/USDT"
                precio = precios.get(sym)
                if precio is None or t.precio_nivel is None:
                    continue

                disparado = False
                if t.direccion == "LONG" and precio >= t.precio_nivel:
                    disparado = True
                elif t.direccion == "SHORT" and precio <= t.precio_nivel:
                    disparado = True
                elif t.direccion is None:
                    # Sin dirección: cruce en cualquier sentido (±0.15%)
                    disparado = abs(precio - t.precio_nivel) / t.precio_nivel * 100 < 0.15

                if disparado:
                    key = (t.usuario_id, "trigger", t.id)
                    if _cooldown_ok(key, "sl"):  # reusar cooldown "sl" (300s)
                        t.disparado    = True
                        t.disparado_en = _dt.utcnow()
                        t.activo       = False
                        db.session.commit()

                        titulo = f"Trigger activado — ${t.precio_nivel:,.0f}"
                        _guardar_notif(app, t.usuario_id, "trigger", "AMARILLO",
                                       titulo, t.condicion_texto)
                        _telegram(
                            f"*TRIGGER ACTIVADO*\n"
                            f"{t.condicion_texto}\n"
                            f"Precio actual: `${precio:,.2f}`"
                        )
                        _mark_sent(key)

        except Exception as e:
            logger.warning(f"check_triggers error: {e}")


def check_briefing_manana(app):
    """
    Envía morning briefing a las 8 AM hora local (una vez por día).
    """
    with app.app_context():
        try:
            from binance_data import get_contexto_mercado, get_regimen_mercado
            from models import Usuario
            from email_service import enviar_bienvenida  # reusar el patrón
        except ImportError:
            return

        hora_actual = datetime.now().hour
        if hora_actual != 8:
            return

        key = (0, "briefing", str(date.today()))
        if not _cooldown_ok(key, "briefing"):
            return

        try:
            ctx = get_contexto_mercado("BTC/USDT")
            regimen = get_regimen_mercado()

            precio = ctx.get("precio", "N/D") if ctx else "N/D"
            funding = ctx.get("funding_rate", 0) if ctx else 0
            reg = regimen.get("regimen", "N/D") if regimen else "N/D"

            msg = (
                f"☀️ *Morning Briefing BTC*\n"
                f"Precio: `${precio:,.2f}`\n"
                f"Régimen: {reg}\n"
                f"Funding: {funding*100:+.3f}%\n"
                f"Revisa tus posiciones abiertas y el scanner."
            )
            _telegram(msg)

            usuarios = Usuario.query.filter_by(activo=True).all()
            for u in usuarios:
                _guardar_notif(app, u.id, "briefing", "INFO",
                               "Morning Briefing", msg.replace("*", "").replace("`", ""))

            _mark_sent(key)

        except Exception as e:
            logger.warning(f"check_briefing error: {e}")


# ── Loop principal del scheduler ──────────────────────────────────────────────

def run_scheduler(app):
    """
    Hilo daemon que corre los checkers en ciclos escalonados.
    Llamar desde app_flask.py como threading.Thread(target=run_scheduler, args=(app,), daemon=True).start()
    """
    logger.info("Scheduler de notificaciones iniciado")
    ciclo = 0

    while True:
        time.sleep(120)  # base: 2 minutos
        ciclo += 1

        # Cada 2 min: liquidación y SL
        try:
            check_liquidacion_sl(app)
        except Exception as e:
            logger.error(f"check_liquidacion_sl: {e}")

        # Cada 5 min: triggers como fallback (WS los dispara en tiempo real)
        if ciclo % 2 == 0:
            try:
                check_triggers(app)
            except Exception as e:
                logger.error(f"check_triggers: {e}")

        # Cada 5 min (ciclos pares): scanner
        if ciclo % 2 == 0:
            try:
                check_scanner(app)
            except Exception as e:
                logger.error(f"check_scanner: {e}")

        # Cada 10 min: régimen y funding
        if ciclo % 5 == 0:
            try:
                check_regimen(app)
                check_funding(app)
            except Exception as e:
                logger.error(f"check_regimen/funding: {e}")

        # Morning briefing: cada ciclo, el chequeo interno filtra por hora
        try:
            check_briefing_manana(app)
        except Exception as e:
            logger.error(f"check_briefing: {e}")
