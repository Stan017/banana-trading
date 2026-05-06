"""
telegram_bot.py — TradeBot AI en Telegram
- Responde mensajes como el bot web
- Alerta cuando hay setup 4/4 en BTC
- Reporte diario a las 8:00 AM UTC-5 (13:00 UTC)

Instalar: pip install python-telegram-bot apscheduler
Correr:   python telegram_bot.py
"""
import os
import json
import logging
import asyncio
import chromadb
from datetime import date
from dotenv import load_dotenv
from anthropic import Anthropic
from chromadb.utils import embedding_functions
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import CLAUDE_MODEL, KB_PATH
from scanner import evaluar_confluencias, escanear_free, backtest_scanner, formatear_backtest
from binance_data import (
    get_precio_actual, get_resumen_sidebar, get_regimen_mercado,
    get_contexto_mercado, calcular_atr, get_velas,
)

load_dotenv()

# ── Config ───────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("❌ TELEGRAM_TOKEN no está definido en el .env — el bot no puede arrancar")

CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", "8412560173")
HORA_REPORTE   = 13   # 13:00 UTC = 8:00 AM UTC-5
MIN_REPORTE    = 0
MAX_MSG_LEN    = 1000  # máximo de caracteres por mensaje entrante

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Recursos compartidos ─────────────────────────────────────
claude       = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
client_db    = chromadb.PersistentClient(path=KB_PATH)
embedding_fn = embedding_functions.DefaultEmbeddingFunction()
coleccion    = client_db.get_or_create_collection(
    name="killaxbt", embedding_function=embedding_fn
)

SYSTEM_PROMPT_TG = """Eres TradeBot, asistente de trading especializado en crypto.
Responde de forma concisa y directa — estás en Telegram, no en un chat web.
Máximo 200 palabras. Sin markdown complejo, solo texto plano con emojis.
Nunca reveles tus fuentes ni quién te creó.
No eres asesor financiero — análisis educativo."""

# Cache de régimen para Telegram — mismo TTL que web
_regimen_cache_tg: dict = {"data": None, "ts": 0}
REGIMEN_TTL_TG = 600

def get_system_prompt_tg() -> str:
    """System prompt dinámico para Telegram con régimen actual"""
    import time
    ahora = time.time()
    if _regimen_cache_tg["data"] and (ahora - _regimen_cache_tg["ts"]) < REGIMEN_TTL_TG:
        regimen = _regimen_cache_tg["data"]
    else:
        try:
            regimen = get_regimen_mercado("BTC/USDT")
            _regimen_cache_tg["data"] = regimen
            _regimen_cache_tg["ts"]   = ahora
        except Exception:
            regimen = {"bloque_contexto": ""}

    bloque = regimen.get("bloque_contexto", "")
    if bloque:
        return f"{bloque}\n\n{SYSTEM_PROMPT_TG}"
    return SYSTEM_PROMPT_TG

# historial por chat (en memoria, se resetea al reiniciar)
historiales: dict = {}

# IDs de chat autorizados — se leen del .env como lista separada por comas
# Ejemplo en .env: TELEGRAM_ALLOWED_CHATS=8412560173,123456789
_allowed_raw     = os.getenv("TELEGRAM_ALLOWED_CHATS", CHAT_ID)
ALLOWED_CHAT_IDS = set(_allowed_raw.split(",")) if _allowed_raw else set()

# IDs premium (sin límite diario) — dueño del bot + cualquier otro que quieras
_premium_raw      = os.getenv("TELEGRAM_PREMIUM_CHATS", CHAT_ID)
PREMIUM_CHAT_IDS  = set(_premium_raw.split(",")) if _premium_raw else set()

# Límite diario para usuarios free
FREE_DAILY_LIMIT  = int(os.getenv("TELEGRAM_FREE_LIMIT", "5"))
_USAGE_FILE       = os.path.join(os.path.dirname(__file__), "telegram_usage.json")

# ── Helpers de uso diario ────────────────────────────────────

def _cargar_uso() -> dict:
    """Carga el archivo de uso. Si no existe, devuelve dict vacío."""
    try:
        if os.path.exists(_USAGE_FILE):
            with open(_USAGE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _guardar_uso(uso: dict):
    try:
        with open(_USAGE_FILE, "w") as f:
            json.dump(uso, f)
    except Exception as e:
        logger.error(f"Error guardando uso Telegram: {e}")

def _get_uso_hoy(chat_id: str) -> int:
    """Devuelve las requests usadas hoy por este chat_id."""
    uso  = _cargar_uso()
    hoy  = str(date.today())
    data = uso.get(chat_id, {})
    if data.get("fecha") != hoy:
        return 0
    return data.get("count", 0)

def _incrementar_uso(chat_id: str):
    """Suma 1 al contador diario del chat_id."""
    uso = _cargar_uso()
    hoy = str(date.today())
    data = uso.get(chat_id, {})
    if data.get("fecha") != hoy:
        data = {"fecha": hoy, "count": 0}
    data["count"] += 1
    uso[chat_id] = data
    _guardar_uso(uso)

# ── Verificación de acceso + límite ─────────────────────────

async def verificar_acceso(update) -> bool:
    """Verifica whitelist. Responde y retorna False si no está autorizado."""
    chat_id = str(update.message.chat_id)
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        logger.warning(f"Acceso denegado a chat_id: {chat_id}")
        return False
    return True

async def verificar_limite(update) -> bool:
    """
    Verifica que el usuario no haya superado su límite diario.
    Premium: ilimitado. Free: FREE_DAILY_LIMIT requests/día.
    Retorna True si puede continuar, False si está bloqueado.
    """
    chat_id = str(update.message.chat_id)

    # Premium = sin límite
    if chat_id in PREMIUM_CHAT_IDS:
        _incrementar_uso(chat_id)
        return True

    usadas = _get_uso_hoy(chat_id)
    if usadas >= FREE_DAILY_LIMIT:
        restante = "mañana se resetea tu contador"
        await update.message.reply_text(
            f"Has usado tus {FREE_DAILY_LIMIT} consultas gratuitas de hoy.\n"
            f"{restante}."
        )
        return False

    _incrementar_uso(chat_id)
    restantes = FREE_DAILY_LIMIT - usadas - 1
    if restantes <= 1:
        await update.message.reply_text(
            f"Te queda {restantes} consulta gratuita hoy."
        )
    return True

# ── Helpers ──────────────────────────────────────────────────

def buscar_contexto(pregunta: str, n: int = 3) -> str:
    try:
        res    = coleccion.query(query_texts=[pregunta], n_results=n)
        chunks = res["documents"][0]
        return "\n".join(chunks)
    except:
        return ""


def formatear_alerta(res: dict) -> str:
    """Formatea resultado del scanner como mensaje de Telegram — incluye ATR con SL/TP y scoring por capas"""
    bias_emoji = "🔴" if res["bias"] == "BAJISTA" else "🟢"
    precio = res.get("precio", 0)

    # ── Etiqueta de convicción ────────────────────────────────
    conviction = res.get("conviction", "")
    score_total = res.get("score_total", 0)
    conv_emoji = {"INSTITUCIONAL": "🏦", "ALTA": "🔥", "MEDIA": "⚡"}.get(conviction, "📊")
    tipo_setup = "6/6" if res.get("setup_ok") else "5/6 POTENCIAL"

    lines = [
        f"🚨 *SETUP {tipo_setup} — {res['symbol']}*",
        f"",
        f"{conv_emoji} *Convicción: {conviction} — {score_total}/100*",
        f"{bias_emoji} *Bias: {res['bias']}*  |  Régimen: {res.get('regimen', '—')}",
        f"💰 Precio: ${precio:,.2f}",
        f"",
    ]

    # ── Desglose de scoring ───────────────────────────────────
    e = res.get("edge_desglose", {})
    lines += [
        f"📊 *Scoring por capas:*",
        f"  Macro:    {res.get('score_macro', 0)}/35 — {res.get('macro_detalle', '—')}",
        f"  Edge:     {res.get('score_edge', 0)}/25 — {e.get('kill_zone', '—')} · {e.get('fomc', '—')}",
        f"  Técnico:  {res.get('score_tecnico', 0)}/40 — {res.get('score', 0)}/6 confluencias",
        f"",
        f"📈 RSI 62: {res['rsi']:.1f}" if res['rsi'] else "📈 RSI: Sin datos",
        f"💸 Funding: {res['funding']:+.4f}%" if res['funding'] is not None else "💸 Funding: Sin datos",
        f"📉 OI 4H: {res['oi_4h']:+.2f}%" if res['oi_4h'] is not None else "📉 OI: Sin datos",
        f"",
        f"✅ *Confluencias:*",
    ]
    for c in res.get("confluencias", []):
        icono = "✅" if c["ok"] else "❌"
        lines.append(f"  {icono} {c['nombre']}: {c['detalle']}")

    # ── ATR — niveles concretos de SL/TP ──────────────────────
    try:
        velas4h = get_velas(res["symbol"], "4h", 30)
        atr = calcular_atr(velas4h, 14)
        if atr and precio:
            bias = res.get("bias")
            if bias == "ALCISTA":
                sl = round(precio - atr * 1.5, 2)
                tp = round(precio + atr * 2.0, 2)
                rr = round((tp - precio) / (precio - sl), 1) if precio > sl else 0
            else:
                sl = round(precio + atr * 1.5, 2)
                tp = round(precio - atr * 2.0, 2)
                rr = round((precio - tp) / (sl - precio), 1) if sl > precio else 0
            lines += [
                f"",
                f"📐 *ATR 14 (4H): ${atr:,.2f}*",
                f"🛑 SL 1.5×ATR: ${sl:,.2f}",
                f"🎯 TP 2×ATR:   ${tp:,.2f}",
                f"⚖️ R:R aprox:   1:{rr}",
            ]
    except Exception:
        pass

    lines += [
        f"",
        f"⚠️ _Análisis educativo — no es asesoría financiera_",
        f"🕐 {res['timestamp']}",
    ]
    return "\n".join(lines)


def formatear_reporte_diario() -> str:
    """Genera resumen diario de los 4 activos"""
    from datetime import datetime
    from binance_data import ACTIVOS
    from concurrent.futures import ThreadPoolExecutor, as_completed

    fecha = datetime.now().strftime("%d %b %Y")
    lines = [f"☀️ *REPORTE DIARIO — {fecha}*", ""]

    datos = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futuros = {ex.submit(get_resumen_sidebar, sym): nom
                   for nom, sym in ACTIVOS.items()}
        for f in as_completed(futuros):
            nom = futuros[f]
            try:
                datos[nom] = f.result(timeout=10)
            except:
                datos[nom] = None

    iconos = {"BTC": "₿", "ETH": "Ξ", "BNB": "◈", "SOL": "◎"}
    for nom in ["BTC", "ETH", "BNB", "SOL"]:
        d = datos.get(nom)
        if not d or d.get("error"):
            lines.append(f"{iconos.get(nom, nom)} {nom}  — Sin datos")
            continue
        cambio = d.get("cambio_24h") or 0
        signo  = "▲" if cambio >= 0 else "▼"
        rsi    = f"RSI {d['rsi']:.0f}" if d.get("rsi") else "RSI —"
        fund   = f"FR {d['funding']:+.3f}%" if d.get("funding") is not None else "FR —"
        lines.append(
            f"{iconos.get(nom,nom)} *{nom}*  ${d['precio']:,.2f}  "
            f"{signo}{abs(cambio):.1f}%  {rsi}  {fund}"
        )

    # ATR BTC
    try:
        velas4h_btc = get_velas("BTC/USDT", "4h", 30)
        atr_btc     = calcular_atr(velas4h_btc, 14)
        btc_precio  = datos.get("BTC", {}).get("precio") if datos.get("BTC") else None
        if atr_btc and btc_precio:
            lines += [
                "",
                f"📐 *ATR BTC 14 (4H): ${atr_btc:,.2f}*",
                f"   SL largo  1.5×: ${btc_precio - atr_btc * 1.5:,.2f}",
                f"   SL corto  1.5×: ${btc_precio + atr_btc * 1.5:,.2f}",
            ]
    except Exception:
        pass

    # Scanner + setup activo
    lines += ["", "🔍 *Scanner BTC:*"]
    res = evaluar_confluencias("BTC/USDT")
    if res.get("setup_ok"):
        lines.append(f"  🚨 SETUP ACTIVO — {res['bias']} (6/6)")
    elif res.get("setup_potencial"):
        lines.append(f"  ⚠️ SETUP POTENCIAL — {res['bias']} (5/6) | Falta: {res.get('falta', '—')}")
    else:
        lines.append(f"  Sin setup — {res.get('score', 0)}/6 confluencias")
    for c in res.get("confluencias", []):
        icono = "✅" if c["ok"] else "❌"
        lines.append(f"    {icono} {c['nombre']}: {c['detalle']}")

    lines += ["", "⚠️ _Análisis educativo — no es asesoría financiera_"]
    return "\n".join(lines)

# ── Comandos de Telegram ─────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (
        "👋 Hola, soy *TradeBot AI*\n\n"
        "Asistente de trading especializado en crypto con "
        "análisis técnico institucional en tiempo real.\n\n"
        "📊 Comandos:\n"
        "/precio — Precio actual de BTC\n"
        "/btc — Análisis completo BTC (scanner + ATR)\n"
        "/scan — Scanner de confluencias 4/4\n"
        "/backtest — Backtest del scanner (30 días)\n"
        "/reporte — Reporte diario de mercado\n\n"
        "O simplemente escríbeme tu pregunta. 🚀"
    )
    await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_precio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        d = get_precio_actual("BTC/USDT")
        cambio = d.get("cambio_24h") or 0
        signo  = "▲" if cambio >= 0 else "▼"
        texto  = (
            f"₿ *BTC/USDT*\n"
            f"💰 ${d['precio']:,.2f}\n"
            f"{signo} {abs(cambio):.2f}% (24h)\n"
            f"Alto: ${d['alto_24h']:,.2f} | Bajo: ${d['bajo_24h']:,.2f}"
        )
    except Exception as e:
        texto = f"❌ Error obteniendo precio: {e}"
    await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await verificar_acceso(update): return
    if not await verificar_limite(update): return
    await update.message.reply_text("🔍 Escaneando BTC...")
    res = evaluar_confluencias("BTC/USDT")
    if res.get("error"):
        await update.message.reply_text(f"❌ Error: {res['error']}")
        return
    if res.get("alerta_valida"):
        await update.message.reply_text(formatear_alerta(res), parse_mode="Markdown")
    elif res.get("setup_ok") or res.get("setup_potencial"):
        # Setup técnico existe pero el scoring lo filtró
        bias_emoji = "🔴" if res["bias"] == "BAJISTA" else "🟢"
        tipo = "6/6" if res.get("setup_ok") else "5/6"
        e = res.get("edge_desglose", {})
        texto = (
            f"⚠️ *SETUP {tipo} TÉCNICO — BTC/USDT*\n"
            f"_Filtrado por scoring de capas_\n\n"
            f"{bias_emoji} Bias: {res['bias']} | Régimen: {res.get('regimen', '—')}\n\n"
            f"📊 *Score: {res.get('score_total',0)}/100* (mínimo 70 para alerta)\n"
            f"  Macro:   {res.get('score_macro',0)}/35 — {res.get('macro_detalle','—')}\n"
            f"  Edge:    {res.get('score_edge',0)}/25 — {e.get('kill_zone','—')}\n"
            f"  Técnico: {res.get('score_tecnico',0)}/40\n"
        )
        if res.get("falta"):
            texto += f"\n❓ Falta para 6/6: {res['falta']}\n"
        await update.message.reply_text(texto, parse_mode="Markdown")
    else:
        texto = (
            f"📊 *BTC/USDT* — Sin setup activo\n\n"
            f"Confluencias: {res.get('score', 0)}/5\n"
            f"Régimen: {res.get('regimen', '—')}\n"
        )
        for c in res.get("confluencias", []):
            icono = "✅" if c["ok"] else "❌"
            texto += f"{icono} {c['nombre']}: {c['detalle']}\n"
        await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await verificar_acceso(update): return
    if not await verificar_limite(update): return
    await update.message.reply_text("📊 Generando reporte...")
    texto = formatear_reporte_diario()
    await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Análisis rápido BTC con Claude — incluye scanner + ATR"""
    if not await verificar_acceso(update): return
    if not await verificar_limite(update): return
    await update.message.reply_text("⏳ Analizando BTC...")
    try:
        datos  = get_contexto_mercado("BTC/USDT")
        ctx    = buscar_contexto("analisis btc setup tendencia")
        sc     = evaluar_confluencias("BTC/USDT")
        sc_txt = ""
        if sc.get("confluencias") and not sc.get("error"):
            estado = ("SETUP " + str(sc["bias"])) if sc["setup_ok"] else (str(sc["score"]) + "/6 confluencias")
            sc_txt = "Scanner BTC: " + estado + "\n"
            for c in sc["confluencias"]:
                sc_txt += ("SI " if c["ok"] else "NO ") + c["nombre"] + ": " + c["detalle"] + "\n"
        msg = f"Conocimiento:\n{ctx}\n\n{datos}\n\n{sc_txt}\nDame un análisis conciso de BTC ahora."
        resp = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            system=get_system_prompt_tg(),
            messages=[{"role": "user", "content": msg}]
        )
        await update.message.reply_text(resp.content[0].text)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Backtest del scanner — últimos 30 días (RSI + EMAs)"""
    if not await verificar_acceso(update): return
    if not await verificar_limite(update): return
    await update.message.reply_text("⏳ Ejecutando backtest 30 días...")
    try:
        resultado = backtest_scanner("BTC/USDT", dias=30)
        texto = formatear_backtest(resultado)
        # Telegram tiene límite de 4096 chars por mensaje
        if len(texto) > 4000:
            texto = texto[:4000] + "\n...(truncado)"
        await update.message.reply_text(texto)
    except Exception as e:
        await update.message.reply_text(f"❌ Error en backtest: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde mensajes libres con Claude + RAG"""
    if not await verificar_acceso(update): return
    if not await verificar_limite(update): return

    chat_id  = str(update.message.chat_id)
    pregunta = update.message.text

    # Truncar mensaje si es muy largo
    if len(pregunta) > MAX_MSG_LEN:
        pregunta = pregunta[:MAX_MSG_LEN]
        await update.message.reply_text(
            f"⚠️ Mensaje truncado a {MAX_MSG_LEN} caracteres."
        )

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    if chat_id not in historiales:
        historiales[chat_id] = []

    historial = historiales[chat_id][-10:]  # últimos 10 mensajes

    ctx = buscar_contexto(pregunta)

    # Inyectar datos de mercado + scanner si la pregunta es sobre trading
    KEYWORDS_TG = [
        "btc", "bitcoin", "precio", "analiza", "setup", "tendencia",
        "eth", "ethereum", "sol", "bnb", "crypto", "trade", "long", "short",
        "rsi", "ema", "funding", "mercado", "entry", "entrada", "bias",
    ]
    datos_mercado = ""
    scanner_txt   = ""
    if any(kw in pregunta.lower() for kw in KEYWORDS_TG):
        try:
            datos_mercado = get_contexto_mercado("BTC/USDT")
        except Exception:
            pass
        try:
            sc = evaluar_confluencias("BTC/USDT")
            if sc.get("confluencias") and not sc.get("error"):
                estado = ("SETUP " + str(sc["bias"])) if sc["setup_ok"] else (str(sc["score"]) + "/6 conf")
                lineas = ["Scanner BTC: " + estado]
                for c in sc["confluencias"]:
                    lineas.append(("SI " if c["ok"] else "NO ") + c["nombre"] + ": " + c["detalle"])
                scanner_txt = "\n".join(lineas)
        except Exception:
            pass

    msg  = f"Conocimiento:\n{ctx}\n\n{datos_mercado}\n\n{scanner_txt}\n\nPregunta: {pregunta}"
    msgs = historial + [{"role": "user", "content": msg}]

    try:
        resp = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            system=get_system_prompt_tg(),
            messages=msgs
        )
        texto = resp.content[0].text

        historiales[chat_id] = historial + [
            {"role": "user",      "content": pregunta},
            {"role": "assistant", "content": texto},
        ]
        if len(historiales[chat_id]) > 20:
            historiales[chat_id] = historiales[chat_id][-20:]

        await update.message.reply_text(texto)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ── Tareas programadas ────────────────────────────────────────

async def tarea_reporte_diario(bot: Bot):
    """Se ejecuta todos los días a las 8:00 AM UTC-5"""
    logger.info("📊 Enviando reporte diario...")
    try:
        texto = formatear_reporte_diario()
        await bot.send_message(chat_id=CHAT_ID, text=texto, parse_mode="Markdown")
        logger.info("✅ Reporte diario enviado")
    except Exception as e:
        logger.error(f"❌ Error en reporte diario: {e}")


async def tarea_scanner(bot: Bot):
    """Se ejecuta cada 4 horas — escanea BTC con scoring por capas"""
    logger.info("🔍 Ejecutando scanner BTC...")
    try:
        resultados = escanear_free()
        for res in resultados:
            if res.get("alerta_valida"):
                logger.info(f"🚨 Alerta válida: {res['symbol']} — {res['bias']} | Score {res.get('score_total',0)}/100 ({res.get('conviction')})")
                texto = formatear_alerta(res)
                await bot.send_message(chat_id=CHAT_ID, text=texto, parse_mode="Markdown")
            elif res.get("setup_ok") or res.get("setup_potencial"):
                logger.info(f"⚠️ Setup técnico descartado por scoring: {res['symbol']} — {res.get('score_total',0)}/100 | Régimen: {res.get('regimen','?')}")
            else:
                logger.info(f"✅ {res['symbol']} — Sin setup ({res.get('score',0)}/5)")
    except Exception as e:
        logger.error(f"❌ Error en scanner: {e}")

# ── Main ──────────────────────────────────────────────────────

async def post_init(app):
    """Se ejecuta cuando el loop ya está corriendo — aquí arranca el scheduler"""
    bot       = app.bot
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Reporte diario — 13:00 UTC (8:00 AM UTC-5)
    scheduler.add_job(
        tarea_reporte_diario,
        trigger="cron",
        hour=HORA_REPORTE,
        minute=MIN_REPORTE,
        args=[bot],
        id="reporte_diario"
    )

    # Scanner — cada 4 horas
    scheduler.add_job(
        tarea_scanner,
        trigger="interval",
        hours=4,
        args=[bot],
        id="scanner_btc"
    )

    scheduler.start()
    logger.info("✅ Scheduler iniciado")
    logger.info(f"📅 Reporte diario: {HORA_REPORTE}:00 UTC (8:00 AM UTC-5)")
    logger.info(f"🔍 Scanner BTC: cada 4 horas")


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)   # scheduler arranca dentro del loop
        .build()
    )

    # Comandos
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("precio",   cmd_precio))
    app.add_handler(CommandHandler("scan",     cmd_scan))
    app.add_handler(CommandHandler("reporte",  cmd_reporte))
    app.add_handler(CommandHandler("btc",      cmd_btc))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 TradeBot Telegram corriendo...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
