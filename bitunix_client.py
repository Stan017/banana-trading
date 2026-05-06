"""
bitunix_client.py — Cliente para Bitunix Futures API
═════════════════════════════════════════════════════
Autenticación: Double SHA256 HMAC
    Step 1: SHA256(nonce + timestamp + api_key + queryParams + body)
    Step 2: SHA256(digest + secret_key)

Base URL: https://fapi.bitunix.com
Rate limit: 10 req/s

Endpoints implementados:
    get_account()              → balance USDT
    get_open_positions()       → posiciones abiertas
    get_history_positions()    → historial de trades cerrados
"""

import hashlib
import time
import uuid
import json
import logging

import requests

log = logging.getLogger(__name__)

_BASE_URL = "https://fapi.bitunix.com"
_TIMEOUT  = 10  # segundos


# ────────────────────────────────────────────────────────────
# Firma
# ────────────────────────────────────────────────────────────

def _make_sign(api_key: str, secret_key: str,
               nonce: str, timestamp: str,
               query_params: str, body: str) -> str:
    """Genera la firma doble SHA256 de Bitunix."""
    step1 = hashlib.sha256(
        (nonce + timestamp + api_key + query_params + body).encode()
    ).hexdigest()
    sign = hashlib.sha256(
        (step1 + secret_key).encode()
    ).hexdigest()
    return sign


def _headers(api_key: str, secret_key: str,
             query_params: str = "", body: str = "") -> dict:
    nonce     = uuid.uuid4().hex          # 32 hex chars aleatorios
    timestamp = str(int(time.time() * 1000))  # ms
    sign      = _make_sign(api_key, secret_key, nonce, timestamp, query_params, body)
    return {
        "api-key"     : api_key,
        "nonce"       : nonce,
        "timestamp"   : timestamp,
        "sign"        : sign,
        "Content-Type": "application/json",
    }


# ────────────────────────────────────────────────────────────
# Helpers internos
# ────────────────────────────────────────────────────────────

def _get(api_key: str, secret_key: str, path: str, params: dict = None) -> dict:
    """GET genérico. Devuelve el JSON parseado o lanza excepción."""
    params = params or {}
    # URL query string normal (con = y &) solo para la URL real
    url_qs = "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
    # Bitunix firma con key+value concatenados SIN = ni &, ordenados por key (ASCII asc)
    sign_qs = "".join(f"{k}{v}" for k, v in sorted(params.items())) if params else ""
    url = f"{_BASE_URL}{path}" + (f"?{url_qs}" if url_qs else "")
    hdrs = _headers(api_key, secret_key, query_params=sign_qs)
    resp = requests.get(url, headers=hdrs, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise ValueError(f"Bitunix error {data.get('code')}: {data.get('msg')}")
    return data


def _post(api_key: str, secret_key: str, path: str, payload: dict = None) -> dict:
    """POST genérico."""
    payload = payload or {}
    body = json.dumps(payload, separators=(",", ":"))
    hdrs = _headers(api_key, secret_key, body=body)
    resp = requests.post(f"{_BASE_URL}{path}", headers=hdrs,
                         data=body, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise ValueError(f"Bitunix error {data.get('code')}: {data.get('msg')}")
    return data


# ────────────────────────────────────────────────────────────
# API pública del módulo
# ────────────────────────────────────────────────────────────

def get_account(api_key: str, secret_key: str) -> dict:
    """
    Balance de la cuenta de futuros.
    Retorna: { available, margin_balance, unrealized_pnl, ... }
    """
    data = _get(api_key, secret_key,
                "/api/v1/futures/account", {"marginCoin": "USDT"})
    raw = data.get("data", {})
    return {
        "available"      : float(raw.get("available", 0)),
        "margin_balance" : float(raw.get("marginBalance", 0)),
        "unrealized_pnl" : float(raw.get("unrealizedPNL", 0)),
        "used_margin"    : float(raw.get("used", 0)),
        "margin_coin"    : raw.get("marginCoin", "USDT"),
    }


def get_open_positions(api_key: str, secret_key: str) -> list[dict]:
    """
    Posiciones abiertas actualmente.
    Retorna lista de dicts normalizados para Journal.
    """
    data = _get(api_key, secret_key,
                "/api/v1/futures/position/get_pending_positions")
    positions = data.get("data", {}).get("positionList", [])
    result = []
    for p in positions:
        result.append(_normalize_position(p, estado="ABIERTO"))
    return result


def get_history_positions(api_key: str, secret_key: str,
                          limit: int = 50,
                          symbol: str = None) -> list[dict]:
    """
    Historial de posiciones cerradas.
    `limit` máximo: 100 por request.
    Retorna lista de dicts normalizados para Journal.
    """
    params: dict = {"size": min(limit, 100)}
    if symbol:
        params["symbol"] = symbol

    data = _get(api_key, secret_key,
                "/api/v1/futures/position/get_history_positions", params)
    positions = data.get("data", {}).get("positionList", [])
    result = []
    for p in positions:
        result.append(_normalize_position(p, estado="CERRADO"))
    return result


def validate_keys(api_key: str, secret_key: str) -> bool:
    """
    Intenta llamar a get_account. Si no lanza excepción, las claves son válidas.
    """
    try:
        get_account(api_key, secret_key)
        return True
    except Exception as e:
        log.warning("Bitunix key validation failed: %s", e)
        return False


# ────────────────────────────────────────────────────────────
# Normalización → formato Journal
# ────────────────────────────────────────────────────────────

def _normalize_position(p: dict, estado: str) -> dict:
    """
    Convierte un objeto de posición de Bitunix al formato que espera el Journal.
    Los campos coinciden con las columnas de Journal.to_dict().
    """
    side = p.get("side", "").upper()   # puede ser BUY/SELL o LONG/SHORT
    direccion = "LONG" if side in ("BUY", "LONG") else "SHORT"

    symbol   = p.get("symbol", "")    # ej. BTCUSDT
    entrada  = _f(p.get("openPrice") or p.get("avgOpenPrice"))
    leverage = _f(p.get("leverage", 1))
    margen   = _f(p.get("margin") or p.get("initialMargin"))

    # Cierre (solo posiciones cerradas)
    precio_cierre = _f(p.get("closePrice") or p.get("avgClosePrice"))
    pnl_real      = _f(p.get("realizedPNL") or p.get("pnl"))

    # SL / TP si los expone la API
    sl = _f(p.get("stopLossPrice"))
    tp = _f(p.get("takeProfitPrice"))

    # Fechas
    open_ts  = p.get("openTime") or p.get("createTime")
    close_ts = p.get("closeTime") or p.get("updateTime")
    fecha_trade  = _ts_to_date(open_ts)
    fecha_cierre = _ts_to_datetime(close_ts) if estado == "CERRADO" else None

    # Duración
    duracion = None
    if open_ts and close_ts:
        try:
            duracion = int((int(close_ts) - int(open_ts)) / 60000)  # ms → min
        except Exception:
            pass

    # R:R planeado
    rr_planeado = None
    if entrada and sl and tp and sl != entrada:
        try:
            risk   = abs(entrada - sl)
            reward = abs(tp - entrada)
            rr_planeado = round(reward / risk, 2) if risk > 0 else None
        except Exception:
            pass

    return {
        "activo"           : symbol,
        "direccion"        : direccion,
        "entrada"          : entrada,
        "sl"               : sl,
        "tp"               : tp,
        "precio_cierre"    : precio_cierre,
        "pnl_real"         : pnl_real,
        "estado"           : estado,
        "apalancamiento"   : leverage,
        "margen_usado"     : margen,
        "tipo_margen"      : "CRUZADO" if str(p.get("marginMode","")).upper() == "CROSSED" else "AISLADO",
        "fecha_trade"      : fecha_trade,
        "fecha_cierre"     : fecha_cierre,
        "duracion_minutos" : duracion,
        "rr_planeado"      : rr_planeado,
        "fuente"           : "BITUNIX",
        "exchange_trade_id": str(p.get("positionId") or p.get("orderId") or ""),
        "tipo_trade"       : "SCALP",   # default; el usuario puede editar
    }


# ────────────────────────────────────────────────────────────
# Utilidades
# ────────────────────────────────────────────────────────────

def _f(val) -> float | None:
    """Convierte a float o None si es falsy / 0."""
    if val is None:
        return None
    try:
        v = float(val)
        return v if v != 0 else None
    except (TypeError, ValueError):
        return None


def _ts_to_date(ts):
    """Timestamp ms → date string YYYY-MM-DD."""
    if not ts:
        return None
    try:
        from datetime import date, timezone, datetime as _dt
        return _dt.fromtimestamp(int(ts) / 1000, tz=timezone.utc).date()
    except Exception:
        return None


def _ts_to_datetime(ts):
    """Timestamp ms → datetime (UTC)."""
    if not ts:
        return None
    try:
        from datetime import timezone, datetime as _dt
        return _dt.fromtimestamp(int(ts) / 1000, tz=timezone.utc).replace(tzinfo=None)
    except Exception:
        return None
