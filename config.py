"""
config.py — Configuración centralizada de TradeBot AI
══════════════════════════════════════════════════════
Fuente única de verdad para variables de entorno y constantes de configuración.
Cualquier módulo que necesite una variable de config importa desde aquí.
Llama load_dotenv() una sola vez — no importa en qué orden se importen los módulos.

Variables REQUERIDAS en .env:
  ADMIN_TOKEN        — token para endpoints de administración
  ANTHROPIC_API_KEY  — API key de Anthropic (Claude)

Variables REQUERIDAS solo en producción:
  FLASK_SECRET_KEY   — clave secreta de Flask (en dev se usa un fallback)
  QDRANT_URL         — URL del cluster Qdrant Cloud
  QDRANT_API_KEY     — API key de Qdrant Cloud

Variables OPCIONALES (tienen defaults funcionales):
  FLASK_ENV          — "production" o "development" (default: development)
  DATABASE_URL       — SQLAlchemy URI (default: sqlite:///tradebot.db)
  CLAUDE_MODEL       — modelo Anthropic (default: claude-haiku-4-5-20251001)
  ALLOWED_ORIGIN     — CORS origin (default: "" → cualquiera en dev)
  KB_PATH            — ruta ChromaDB local (default: ./base_conocimiento)
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET  — OAuth Google
  RESEND_API_KEY / BANANA_* — email transaccional
  TELEGRAM_TOKEN / TELEGRAM_CHAT_ID        — alertas Telegram
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Entorno ───────────────────────────────────────────────────────────────────

FLASK_ENV = os.getenv("FLASK_ENV", "development")
IS_PROD   = FLASK_ENV == "production"

# ── Seguridad Flask ───────────────────────────────────────────────────────────

FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
if not FLASK_SECRET_KEY:
    if IS_PROD:
        raise RuntimeError("FLASK_SECRET_KEY no está definida — el servidor no puede arrancar en producción sin ella.")
    FLASK_SECRET_KEY = "tradebot-dev-only-not-for-production"

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
if not ADMIN_TOKEN:
    raise RuntimeError("❌ ADMIN_TOKEN no está definido en el .env")

# ── Base de datos ─────────────────────────────────────────────────────────────

# Railway entrega "postgres://" pero SQLAlchemy 2.x requiere "postgresql://"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///tradebot.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ── CORS ──────────────────────────────────────────────────────────────────────

ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "")

# ── Google OAuth ──────────────────────────────────────────────────────────────

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# ── Anthropic / Claude ────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# ── Qdrant Cloud ──────────────────────────────────────────────────────────────

QDRANT_URL        = os.getenv("QDRANT_URL", "")
QDRANT_API_KEY    = os.getenv("QDRANT_API_KEY", "")
QDRANT_COLLECTION = "killaxbt"

# ── Knowledge Base local (ChromaDB fallback) ──────────────────────────────────

KB_PATH = os.getenv(
    "KB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "base_conocimiento"),
)

# ── Email transaccional (Resend) ──────────────────────────────────────────────

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = os.getenv("BANANA_FROM_EMAIL", "Banana <onboarding@resend.dev>")
LOGO_URL       = os.getenv("BANANA_LOGO_URL", "")
APP_URL        = os.getenv("BANANA_APP_URL", "https://banana.ai")

# ── Telegram ──────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8412560173")

# ── On-Chain (Glassnode) — opcional ───────────────────────────────────────────
# Free tier disponible en https://studio.glassnode.com
# Sin esta key el sistema funciona normalmente, sin datos on-chain.
GLASSNODE_API_KEY = os.getenv("GLASSNODE_API_KEY", "")

# ── BYOK — cifrado Fernet para API keys de usuarios ──────────────────────────
# Generar con: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# NUNCA cambiar en producción (invalida todas las BYOK keys guardadas)
FERNET_KEY = os.getenv("FERNET_KEY", "")
if not FERNET_KEY:
    if IS_PROD:
        raise RuntimeError("FERNET_KEY no está definida — requerida para BYOK en producción.")
    # Dev: clave temporal (se regenera en cada reinicio — BYOK no persiste entre reinicios, OK en dev)
    from cryptography.fernet import Fernet as _Fernet
    FERNET_KEY = _Fernet.generate_key().decode()

# ── Rate limiting ─────────────────────────────────────────────────────────────

RATE_LIMIT         = 10 if IS_PROD else 100   # req/min por usuario/IP
RATE_WINDOW        = 60                        # ventana en segundos
LOGIN_MAX_ATTEMPTS = 5                         # intentos antes de bloqueo
LOGIN_BLOCK_WINDOW = 300                       # segundos de bloqueo
MAX_INPUT_LEN      = 4000                      # máx caracteres de entrada (anti-ReDoS)

# ── Planes ────────────────────────────────────────────────────────────────────

PLAN_LIMITS: dict[str, int] = {
    "free": 10,    # consultas/día
    "pro" : 999,   # ilimitado
}

# ── Modelos disponibles por clave corta ───────────────────────────────────────
# Cambiar aquí si Anthropic lanza nuevas versiones — sin tocar chat_routes.py
MODEL_MAP: dict[str, str] = {
    "haiku" : "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-5-20251001",
    "opus"  : "claude-opus-4-5",
}
