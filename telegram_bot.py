"""
telegram_bot.py — TradeBot AI en Telegram
- Responde mensajes como el bot web
- Alerta cuando hay setup 4/4 en BTC
- Reporte diario a las 8:00 AM UTC-5 (13:00 UTC)

Instalar: pip install python-telegram-bot apscheduler
Correr:   python telegram_bot.py
"""
import os
import logging
import asyncio
import chromadb
from dotenv import load_dotenv
from anthropic import Anthropic
from chromadb.utils import embedding_functions
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from scanner import evaluar_confluencias, escanear_free
from binance_data import get_precio_actual, get_resumen_sidebar, get_regimen_mercado

load_dotenv()

# ── Config ───────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("❌ TELEGRAM_TOKEN no está definido en el .env — el bot no puede arrancar")

CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", "8412560173")
KB_PATH        = os.getenv("KB_PATH", r"C:\Users\stanley\Desktop\copy\base_conocimiento")
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

async def verificar_acceso(update) -> bool:
    """Verifica que el chat_id esté en la whitelist. Responde y retorna False si no."""
    chat_id = str(update.message.chat_id)
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        logger.warning(f"Acceso denegado a chat_id: {chat_id}")
        return False
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
    """Formatea resultado del scanner como mensaje de Telegram"""
    bias_emoji = "🔴" if res["bias"] == "BAJISTA" else "🟢"
    lines = [
        f"🚨 *SETUP DETECTADO — {res['symbol']}*",
        f"",
        f"{bias_emoji} *Bias: {res['bias']}*",
        f"💰 Precio: ${res['precio']:,.2f}",
        f"📊 RSI 62: {res['rsi']:.1f}" if res['rsi'] else "📊 RSI: Sin datos",
        f"💸 Funding: {res['funding']:+.4f}%" if res['funding'] is not None else "💸 Funding: Sin datos",
        f"📈 OI 4H: {res['oi_4h']:+.2f}%" if res['oi_4h'] is not None else "📈 OI: Sin datos",
        f"",
        f"✅ Confluencias: {res['score']}/4",
    ]
    for c in res.get("confluencias", []):
        icono = "✅" if c["ok"] else "❌"
        lines.append(f"  {icono} {c['nombre']}: {c['detalle']}")
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

    # Setups activos
    lines += ["", "🔍 *Setups activos (BTC):*"]
    res = evaluar_confluencias("BTC/USDT")
    if res.get("setup_ok"):
        lines.append(f"  🚨 BTC — {res['bias']} ({res['score']}/4 confluencias)")
    else:
        lines.append(f"  ✅ Sin setup activo — mercado en espera ({res.get('score',0)}/4)")

    lines += ["", "⚠️ _Análisis educativo — no es asesoría financiera_"]
    return "\n".join(lines)

# ── Comandos de Telegram ─────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (
        "👋 Hola, soy *TradeBot AI*\n\n"
        "Soy un asistente de trading especializado en crypto con "
        "análisis técnico institucional en tiempo real.\n\n"
        "📊 Comandos:\n"
        "/precio — Precio actual de BTC\n"
        "/btc — Análisis rápido BTC\n"
        "/reporte — Reporte de mercado\n"
        "/scan — Escanear setup BTC\n\n"
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
    await update.message.reply_text("🔍 Escaneando BTC...")
    res = evaluar_confluencias("BTC/USDT")
    if res.get("error"):
        await update.message.reply_text(f"❌ Error: {res['error']}")
        return
    if res["setup_ok"]:
        await update.message.reply_text(formatear_alerta(res), parse_mode="Markdown")
    else:
        texto = (
            f"📊 *BTC/USDT* — Sin setup activo\n\n"
            f"Score: {res['score']}/4 confluencias\n"
        )
        for c in res.get("confluencias", []):
            icono = "✅" if c["ok"] else "❌"
            texto += f"{icono} {c['nombre']}: {c['detalle']}\n"
        await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await verificar_acceso(update): return
    await update.message.reply_text("📊 Generando reporte...")
    texto = formatear_reporte_diario()
    await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Análisis rápido BTC con Claude"""
    if not await verificar_acceso(update): return
    await update.message.reply_text("⏳ Analizando BTC...")
    from binance_data import get_contexto_mercado
    try:
        datos = get_contexto_mercado("BTC/USDT")
        ctx   = buscar_contexto("analisis btc setup tendencia")
        msg   = f"Conocimiento:\n{ctx}\n\n{datos}\n\nDame un análisis conciso de BTC ahora."
        resp  = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=get_system_prompt_tg(),
            messages=[{"role": "user", "content": msg}]
        )
        await update.message.reply_text(resp.content[0].text)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde mensajes libres con Claude + RAG"""
    if not await verificar_acceso(update): return

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

    ctx  = buscar_contexto(pregunta)
    msg  = f"Conocimiento:\n{ctx}\n\nPregunta: {pregunta}"
    msgs = historial + [{"role": "user", "content": msg}]

    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
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
    """Se ejecuta cada 4 horas — escanea BTC"""
    logger.info("🔍 Ejecutando scanner BTC...")
    try:
        resultados = escanear_free()
        for res in resultados:
            if res.get("setup_ok"):
                logger.info(f"🚨 Setup detectado: {res['symbol']} — {res['bias']}")
                texto = formatear_alerta(res)
                await bot.send_message(chat_id=CHAT_ID, text=texto, parse_mode="Markdown")
            else:
                logger.info(f"✅ {res['symbol']} — Sin setup ({res.get('score',0)}/4)")
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
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("precio",  cmd_precio))
    app.add_handler(CommandHandler("scan",    cmd_scan))
    app.add_handler(CommandHandler("reporte", cmd_reporte))
    app.add_handler(CommandHandler("btc",     cmd_btc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 TradeBot Telegram corriendo...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
