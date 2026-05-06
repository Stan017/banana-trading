"""
utils/helpers.py — Estado compartido y helpers de seguridad/validación
═══════════════════════════════════════════════════════════════════════
Centraliza:
  - Estado mutable compartido entre blueprints (_rate_data, _login_attempts, _trade_contexts)
  - Helpers de seguridad (rate limit, brute force, prompt injection, admin token)
  - Helpers de negocio (get_journal_stats, check_plan_limit)

Constantes de configuración → config.py
"""

import re
import time
import hmac
import logging
import secrets as _secrets
from collections import defaultdict
from flask import request
from sqlalchemy import func

from config import (
    IS_PROD,
    RATE_LIMIT,
    RATE_WINDOW,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_BLOCK_WINDOW,
    MAX_INPUT_LEN,
    ADMIN_TOKEN,
    PLAN_LIMITS,
)

logger = logging.getLogger(__name__)

INJECTION_PATTERNS = [
    r"ignora\s+(tus\s+)?instrucciones",
    r"ignore\s+(your\s+)?instructions",
    r"olvida\s+(todo|tus)",
    r"forget\s+(everything|your)",
    r"system\s*prompt",
    r"jailbreak",
    r"pretend\s+you",
    r"act\s+as\s+if",
    r"bypass",
    r"override",
    r"<\s*system\s*>",
    r"\[INST\]",
    r"###\s*instruction",
]

# ============================================================
# ESTADO MUTABLE COMPARTIDO
# ============================================================

_rate_data:      defaultdict = defaultdict(list)   # rate limit general
_login_attempts: defaultdict = defaultdict(list)   # protección fuerza bruta
_trade_contexts: dict        = {}                  # contexto de trade por tab token
_TRADE_CTX_TTL               = 300                 # segundos de vida del contexto

# ============================================================
# HELPERS DE IP / RATE LIMIT
# ============================================================

def get_client_ip() -> str:
    """
    Extrae IP real del cliente.
    En producción: usa remote_addr directamente — Railway/Render/Nginx
    ya ponen la IP real aquí. Confiar en X-Forwarded-For permite spoofing.
    En desarrollo: acepta X-Forwarded-For para simular proxies localmente.
    """
    if IS_PROD:
        return request.remote_addr or "unknown"
    for header in ("X-Forwarded-For", "X-Real-IP"):
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    return request.remote_addr or "unknown"


def check_rate_limit(ip: str) -> bool:
    """Rate limit por minuto — retorna False si se excede el límite."""
    now = time.time()
    _rate_data[ip] = [t for t in _rate_data[ip] if now - t < RATE_WINDOW]
    if len(_rate_data[ip]) >= RATE_LIMIT:
        return False
    _rate_data[ip].append(now)
    return True


def check_prompt_injection(texto: str) -> bool:
    """Retorna True si detecta intento de prompt injection o input sospechosamente largo."""
    if len(texto) > MAX_INPUT_LEN:
        return True
    lower = texto.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


# ============================================================
# HELPERS DE AUTENTICACIÓN
# ============================================================

def check_brute_force(ip: str) -> bool:
    """Retorna True si la IP está bloqueada por demasiados intentos fallidos."""
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < LOGIN_BLOCK_WINDOW]
    return len(_login_attempts[ip]) >= LOGIN_MAX_ATTEMPTS


def register_failed_login(ip: str):
    """Registra un intento fallido de login."""
    _login_attempts[ip].append(time.time())


def clear_failed_logins(ip: str):
    """Limpia los intentos fallidos tras login exitoso."""
    _login_attempts[ip] = []


def is_valid_email(email: str) -> bool:
    """Validación básica de formato de email."""
    pattern = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email)) and len(email) <= 254


def check_admin_token() -> bool:
    """Verifica token secreto en header Authorization (comparación segura contra timing attacks)."""
    auth     = request.headers.get("Authorization", "")
    expected = f"Bearer {ADMIN_TOKEN}"
    return hmac.compare_digest(auth, expected)


# ============================================================
# CONTEXTO DE TRADE (tab tokens)
# ============================================================

def _get_trade_context(token: str) -> str | None:
    """Extrae y elimina el contexto de trade para este token. Limpia expirados."""
    now = time.time()
    expired = [k for k, v in _trade_contexts.items() if now - v["ts"] > _TRADE_CTX_TTL]
    for k in expired:
        del _trade_contexts[k]
    entry = _trade_contexts.pop(token, None)
    if entry and (now - entry["ts"]) <= _TRADE_CTX_TTL:
        return entry["context"]
    return None


# ============================================================
# HELPERS DE NEGOCIO
# ============================================================

def get_journal_stats(usuario_id: int) -> str:
    """Devuelve string con stats del journal para inyectar en el contexto del LLM."""
    from models import db, Journal
    try:
        total = Journal.query.filter_by(usuario_id=usuario_id).count()
        if total == 0:
            return ""
        wins   = Journal.query.filter_by(usuario_id=usuario_id, resultado="WIN").count()
        losses = Journal.query.filter_by(usuario_id=usuario_id, resultado="LOSS").count()
        wr     = round((wins / total) * 100)
        rr_avg = db.session.query(func.avg(Journal.rr_real)).filter(
            Journal.usuario_id == usuario_id,
            Journal.rr_real.isnot(None)
        ).scalar()
        rr_str = f"{float(rr_avg):.2f}" if rr_avg else "Sin datos"
        activo_top = db.session.query(
            Journal.activo, func.count(Journal.activo).label("cnt")
        ).filter_by(usuario_id=usuario_id).group_by(Journal.activo)\
         .order_by(func.count(Journal.activo).desc()).first()
        activo_str = activo_top[0] if activo_top else "Sin datos"
        sep = chr(0x2501) * 50
        return (
            f"\n{sep}\n"
            f"PERFIL DEL TRADER (datos reales del usuario)\n"
            f"{sep}\n"
            f"Trades registrados: {total} | Wins: {wins} | Losses: {losses}\n"
            f"Win Rate real:      {wr}%\n"
            f"R:R promedio:       {rr_str}\n"
            f"Activo mas operado: {activo_str}\n"
            f"Nota: Usar estos datos para personalizar el analisis.\n"
            f"{sep}"
        )
    except Exception as e:
        logger.error(f"Error obteniendo journal stats: {e}")
        return ""


def check_plan_limit(usuario) -> tuple[bool, str]:
    """Verifica si el usuario puede hacer más consultas hoy."""
    from models import UsoDiario
    limite = PLAN_LIMITS.get(usuario.plan, 10)
    uso    = UsoDiario.get_o_crear(usuario.id)
    if uso.consultas >= limite:
        if usuario.plan == "free":
            return False, f"Límite diario del plan Free alcanzado ({limite} consultas). Upgrade a Pro para continuar."
        return False, "Límite diario alcanzado."
    uso.incrementar()
    return True, ""
