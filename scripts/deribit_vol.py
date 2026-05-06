"""
deribit_vol.py — Volatility Surface usando Deribit Public API
═════════════════════════════════════════════════════════════
No autenticación requerida — endpoints completamente públicos.

Endpoints usados:
  /api/v2/public/get_volatility_index_data  → DVOL histórico (30D)
  /api/v2/public/get_book_summary_by_currency → IV + strikes de todas las opciones (bulk)

Funciones exportadas:
  get_dvol()                  → dict: valor, pctil 30D, régimen HIGH/NORMAL/LOW
  get_term_structure(spot)    → dict: iv_7d, iv_30d, iv_90d, forma
  get_iv_skew(spot)           → dict: rr_30d, sesgo (proxy ±10% OTM)
  get_vol_score(bias, spot)   → dict: adj ±3, label, signals_ok, data
  get_vol_contexto(spot)      → str inyectable en system prompt
  get_vol_resumen(spot)       → dict completo para UI y /api/vol/surface
"""

import logging
import math as _math
import time
import threading
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_BASE    = "https://www.deribit.com/api/v2/public"
_TIMEOUT = 12

# ── Caché thread-safe por clave/TTL ──────────────────────────
_cache:      dict = {}
_cache_lock = threading.Lock()


def _cget(key: str):
    with _cache_lock:
        e = _cache.get(key)
        if e and time.time() < e["exp"]:
            return e["data"]
    return None


def _cset(key: str, data, ttl: int = 300):
    with _cache_lock:
        _cache[key] = {"data": data, "exp": time.time() + ttl}
    return data


# ══════════════════════════════════════════════════════════════
#  HV FALLBACK — Historical Volatility desde Binance
#  Usado automáticamente cuando Deribit no está disponible.
#  HV = std(log_returns, N) × √365 × 100
# ══════════════════════════════════════════════════════════════

def _calc_hv(days: int = 21) -> float | None:
    """Historical Volatility annualizada usando closes diarios de Binance. Caché 4h."""
    key    = f"hv_{days}"
    cached = _cget(key)
    if cached is not None:
        return cached

    try:
        from binance_data import get_velas
        velas  = get_velas("BTC/USDT", "1d", days + 3)
        closes = [v["close"] for v in velas if v.get("close")]
        if len(closes) < days + 1:
            return _cset(key, None, 300)

        returns  = [_math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        returns  = returns[-days:]
        mean     = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / max(len(returns) - 1, 1)
        hv       = round(_math.sqrt(variance) * _math.sqrt(365) * 100, 1)
        return _cset(key, hv, 14400)   # 4h TTL
    except Exception as e:
        logger.debug(f"HV{days} calc error: {e}")
        return _cset(key, None, 60)


def _get_hv_surface() -> dict:
    """
    Superficie de volatilidad histórica usando Binance (fallback de Deribit).
    Calcula HV7 / HV21 / HV60 y determina forma de la curva.
    """
    hv7  = _calc_hv(7)
    hv21 = _calc_hv(21)
    hv60 = _calc_hv(60)

    if hv21 is None:
        return {"disponible": False, "error": "Sin datos Binance para HV"}

    # Régimen por umbrales fijos BTC (histórico)
    regimen = (
        "HIGH_VOL" if hv21 > 80 else
        "LOW_VOL"  if hv21 < 35 else
        "NORMAL"
    )

    # Forma de curva: HV7 vs HV21 (>5pp diferencia = señal)
    forma = "DESCONOCIDA"
    if hv7 is not None:
        diff  = hv7 - hv21
        forma = (
            "BACKWARDATION" if diff > 5  else   # near > far = estrés
            "CONTANGO"      if diff < -5 else   # near < far = calma
            "FLAT"
        )

    return {
        "disponible": True,
        "hv7":        hv7,
        "hv21":       hv21,
        "hv60":       hv60,
        "regimen":    regimen,
        "forma":      forma,
    }


def _req(endpoint: str, params: dict = None) -> dict:
    """GET a Deribit Public API. Lanza excepción en error."""
    r = requests.get(f"{_BASE}/{endpoint}", params=params or {}, timeout=_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"].get("message", "Deribit API error"))
    return body.get("result", {})


# ── Parser de nombres de instrumentos ────────────────────────
_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_name(name: str) -> dict | None:
    """
    'BTC-27JUN25-100000-C' → {strike, option_type, expiry_ts_ms}
    Retorna None si el formato no es válido.
    """
    try:
        parts = name.split("-")
        if len(parts) != 4 or parts[0] != "BTC":
            return None
        exp_str = parts[1]  # e.g. "27JUN25"
        month   = _MONTH_MAP.get(exp_str[2:5].upper())
        if not month:
            return None
        day  = int(exp_str[:2])
        year = 2000 + int(exp_str[5:7])
        # Deribit options expire at 08:00 UTC
        dt   = datetime(year, month, day, 8, 0, 0, tzinfo=timezone.utc)
        return {
            "strike":       float(parts[2]),
            "option_type":  "call" if parts[3] == "C" else "put",
            "expiry_ts_ms": int(dt.timestamp() * 1000),
        }
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
#  DVOL — Deribit BTC Volatility Index
# ══════════════════════════════════════════════════════════════

def get_dvol() -> dict:
    """
    DVOL actual + percentil vs 30D + régimen de volatilidad.

    Returns dict:
        valor      float | None   DVOL actual (IV 30D, %)
        pctil      float          Percentil vs últimos 30D (0-100)
        regimen    str            HIGH_VOL | NORMAL | LOW_VOL
        min_30d    float
        max_30d    float
        media_30d  float
        error      str | None
    """
    cached = _cget("dvol")
    if cached is not None:
        return cached

    try:
        now_ms   = int(time.time() * 1000)
        start_ms = now_ms - 30 * 86_400 * 1_000

        result = _req("get_volatility_index_data", {
            "currency":        "BTC",
            "start_timestamp": start_ms,
            "end_timestamp":   now_ms,
            "resolution":      "3600",   # 1h bars
        })
        data = result.get("data", [])
        if not data:
            return _cset("dvol", {"error": "Sin datos DVOL", "valor": None}, 60)

        # Formato: [[ts_ms, open, high, low, close], ...]
        closes = [row[4] for row in data if row[4] is not None and row[4] > 0]
        if not closes:
            return _cset("dvol", {"error": "DVOL vacío", "valor": None}, 60)

        actual = closes[-1]
        mn, mx = min(closes), max(closes)
        media  = sum(closes) / len(closes)
        rango  = mx - mn
        pctil  = ((actual - mn) / rango * 100) if rango > 0.5 else 50.0

        regimen = (
            "HIGH_VOL" if pctil >= 75 else
            "LOW_VOL"  if pctil <= 25 else
            "NORMAL"
        )

        out = {
            "valor":     round(actual, 1),
            "pctil":     round(pctil, 1),
            "regimen":   regimen,
            "min_30d":   round(mn, 1),
            "max_30d":   round(mx, 1),
            "media_30d": round(media, 1),
            "error":     None,
        }
        return _cset("dvol", out, 3600)  # 1h TTL

    except Exception as e:
        logger.debug(f"Deribit DVOL error: {e}")
        return _cset("dvol", {"error": str(e), "valor": None}, 60)


# ══════════════════════════════════════════════════════════════
#  BOOK SUMMARY — carga bulk de todas las opciones BTC
# ══════════════════════════════════════════════════════════════

def _get_book_summary() -> list:
    """
    get_book_summary_by_currency — devuelve todas las opciones BTC con mark_iv.
    Caché 5 min para reutilizar en term_structure y skew.
    """
    cached = _cget("book_summary")
    if cached is not None:
        return cached

    try:
        result = _req("get_book_summary_by_currency", {
            "currency": "BTC",
            "kind":     "option",
        })
        books = result if isinstance(result, list) else []
        return _cset("book_summary", books, 300)
    except Exception as e:
        logger.debug(f"Deribit book_summary error: {e}")
        return _cset("book_summary", [], 60)


def _build_expiry_map(books: list, now_ms: int, min_days: int = 3) -> dict:
    """
    Construye {expiry_ts_ms: [{strike, option_type, iv}]} filtrando vencimientos
    demasiado próximos (< min_days) y entradas sin IV válida.
    """
    expiry_map: dict = {}
    min_exp = now_ms + min_days * 86_400 * 1_000

    for b in books:
        name   = b.get("instrument_name", "")
        parsed = _parse_name(name)
        if not parsed:
            continue
        if parsed["expiry_ts_ms"] <= min_exp:
            continue
        iv = b.get("mark_iv")
        if iv is None or iv <= 0:
            continue

        key = parsed["expiry_ts_ms"]
        if key not in expiry_map:
            expiry_map[key] = []
        expiry_map[key].append({
            "strike":      parsed["strike"],
            "option_type": parsed["option_type"],
            "iv":          float(iv),
        })

    return expiry_map


# ══════════════════════════════════════════════════════════════
#  TERM STRUCTURE — IV ATM en 7D / 30D / 90D
# ══════════════════════════════════════════════════════════════

def get_term_structure(spot: float) -> dict:
    """
    IV del call ATM más cercano a 7D, 30D y 90D.
    Forma: CONTANGO | BACKWARDATION | FLAT | DESCONOCIDA

    Returns dict:
        iv_7d   float | None
        iv_30d  float | None
        iv_90d  float | None
        forma   str
        detalle str
        error   str | None
    """
    key    = f"ts_{int(spot) // 1000}"
    cached = _cget(key)
    if cached is not None:
        return cached

    try:
        now_ms = int(time.time() * 1000)
        books  = _get_book_summary()
        if not books:
            return {"error": "Sin datos book summary", "forma": "DESCONOCIDA"}

        expiry_map = _build_expiry_map(books, now_ms, min_days=3)
        if not expiry_map:
            return {"error": "Sin vencimientos válidos", "forma": "DESCONOCIDA"}

        def atm_iv(target_days: int) -> float | None:
            target_ms = now_ms + target_days * 86_400 * 1_000
            best_exp  = min(expiry_map, key=lambda k: abs(k - target_ms))
            calls     = [o for o in expiry_map[best_exp] if o["option_type"] == "call"]
            if not calls:
                return None
            atm = min(calls, key=lambda o: abs(o["strike"] - spot))
            return round(atm["iv"], 1)

        iv_7d  = atm_iv(7)
        iv_30d = atm_iv(30)
        iv_90d = atm_iv(90)

        forma   = "DESCONOCIDA"
        detalle = ""

        if iv_7d is not None and iv_30d is not None:
            if iv_7d > iv_30d * 1.05:
                forma   = "BACKWARDATION"
                detalle = f"7D {iv_7d}% > 30D {iv_30d}% — miedo near-term"
            elif iv_90d is not None and iv_90d > iv_30d * 1.05:
                forma   = "CONTANGO"
                detalle = f"30D {iv_30d}% < 90D {iv_90d}% — estructura normal"
            else:
                forma   = "FLAT"
                detalle = f"Estructura plana: 7D {iv_7d}% ≈ 30D {iv_30d}%"

        out = {
            "iv_7d":   iv_7d,
            "iv_30d":  iv_30d,
            "iv_90d":  iv_90d,
            "forma":   forma,
            "detalle": detalle,
            "error":   None,
        }
        return _cset(key, out, 300)

    except Exception as e:
        logger.debug(f"Deribit term_structure error: {e}")
        return {"error": str(e), "forma": "DESCONOCIDA"}


# ══════════════════════════════════════════════════════════════
#  IV SKEW — Proxy ±10% OTM en vencimiento ~30D
# ══════════════════════════════════════════════════════════════

def get_iv_skew(spot: float) -> dict:
    """
    Skew proxy: IV put (≈ spot*0.90) vs IV call (≈ spot*1.10) en ~30D.
    Aproxima el 25-delta risk reversal sin llamadas ticker individuales.

    RR = IV_put - IV_call
    Negativo → puts caros → sesgo bajista implícito del mercado
    Positivo → calls caros → sesgo alcista implícito

    Returns dict:
        rr_30d       float | None    (put_iv - call_iv)
        sesgo        str             PUTS_CAROS | CALLS_CAROS | NEUTRAL
        put_iv       float | None
        call_iv      float | None
        put_strike   float | None
        call_strike  float | None
        error        str | None
    """
    key    = f"skew_{int(spot) // 1000}"
    cached = _cget(key)
    if cached is not None:
        return cached

    try:
        now_ms = int(time.time() * 1000)
        books  = _get_book_summary()
        if not books:
            return {"error": "Sin datos", "rr_30d": None, "sesgo": "NEUTRAL"}

        expiry_map = _build_expiry_map(books, now_ms, min_days=10)
        if not expiry_map:
            return {"error": "Sin vencimientos ≥10D", "rr_30d": None, "sesgo": "NEUTRAL"}

        # Target ~30D
        target_ms = now_ms + 30 * 86_400 * 1_000
        best_exp  = min(expiry_map, key=lambda k: abs(k - target_ms))
        opts_30d  = expiry_map[best_exp]

        puts  = [o for o in opts_30d if o["option_type"] == "put"]
        calls = [o for o in opts_30d if o["option_type"] == "call"]

        # Strikes objetivo: ±10% del spot (aproximación 25-delta)
        tgt_put  = spot * 0.90
        tgt_call = spot * 1.10

        best_put  = min(puts,  key=lambda o: abs(o["strike"] - tgt_put))  if puts  else None
        best_call = min(calls, key=lambda o: abs(o["strike"] - tgt_call)) if calls else None

        put_iv    = round(best_put["iv"],  1) if best_put  else None
        call_iv   = round(best_call["iv"], 1) if best_call else None

        rr_30d = None
        sesgo  = "NEUTRAL"
        if put_iv is not None and call_iv is not None:
            rr_30d = round(put_iv - call_iv, 1)
            if rr_30d <= -3:
                sesgo = "PUTS_CAROS"
            elif rr_30d >= 3:
                sesgo = "CALLS_CAROS"

        out = {
            "rr_30d":     rr_30d,
            "sesgo":      sesgo,
            "put_iv":     put_iv,
            "call_iv":    call_iv,
            "put_strike":  best_put["strike"]  if best_put  else None,
            "call_strike": best_call["strike"] if best_call else None,
            "error":      None,
        }
        return _cset(key, out, 300)

    except Exception as e:
        logger.debug(f"Deribit skew error: {e}")
        return {"error": str(e), "rr_30d": None, "sesgo": "NEUTRAL"}


# ══════════════════════════════════════════════════════════════
#  SCORE MODIFIER para scanner._score_macro()
# ══════════════════════════════════════════════════════════════

def get_vol_score(bias: str, spot: float = None) -> dict:
    """
    Modifier ±3 para integrar en _score_macro().

    Lógica:
      HIGH_VOL + ALCISTA  → -3  (vol alta destroza setups direccionales largos)
      HIGH_VOL + BAJISTA  → -1  (algo de vol ayuda al corto, pero sigue siendo riesgo)
      LOW_VOL             → -1  (breakouts falsos, falta momentum)
      BACKWARDATION       → -2  (miedo near-term independiente de bias)
      RR < -5 + ALCISTA   → -2  (mercado pagando puts, sesgo implícito contrario)
      RR < -5 + BAJISTA   → +1  (confirma dirección)
      RR >  5 + BAJISTA   → -2  (mercado pagando calls, sesgo contrario)
      RR >  5 + ALCISTA   → +1  (confirma dirección)

    Returns:
        adj        int      Modifier clampeado [-3, +3]
        label      str      Descripción concatenada
        signals_ok int      Cuántas señales disponibles
        data       dict     vol_data para incluir en payload del scanner
    """
    adj      = 0
    labels   = []
    sig_ok   = 0
    vol_data: dict = {}

    # ── DVOL ──────────────────────────────────────────────────
    dvol = get_dvol()
    if dvol.get("valor") is not None and not dvol.get("error"):
        sig_ok += 1
        vol_data.update({
            "dvol":         dvol["valor"],
            "dvol_pctil":   dvol["pctil"],
            "dvol_regimen": dvol["regimen"],
        })
        if dvol["regimen"] == "HIGH_VOL":
            adj += -3 if bias == "ALCISTA" else -1
            labels.append(
                f"DVOL {dvol['valor']}% ({dvol['pctil']:.0f}%ile) HIGH_VOL "
                f"— {'reduce setup alcista' if bias == 'ALCISTA' else 'vol alta'}"
            )
        elif dvol["regimen"] == "LOW_VOL":
            adj -= 1
            labels.append(f"DVOL {dvol['valor']}% ({dvol['pctil']:.0f}%ile) LOW_VOL — momentum débil")

    # ── Term Structure ─────────────────────────────────────────
    if spot:
        ts = get_term_structure(spot)
        if not ts.get("error") and ts.get("forma") not in (None, "DESCONOCIDA"):
            sig_ok += 1
            vol_data.update({
                "iv_7d":   ts.get("iv_7d"),
                "iv_30d":  ts.get("iv_30d"),
                "iv_90d":  ts.get("iv_90d"),
                "ts_forma": ts["forma"],
            })
            if ts["forma"] == "BACKWARDATION":
                adj -= 2
                labels.append(
                    f"Term struct BACKWARDATION (7D {ts.get('iv_7d','?')}% > 30D {ts.get('iv_30d','?')}%)"
                )

        # ── IV Skew ────────────────────────────────────────────
        skew = get_iv_skew(spot)
        if not skew.get("error") and skew.get("rr_30d") is not None:
            sig_ok += 1
            vol_data.update({
                "rr_30d":     skew["rr_30d"],
                "skew_sesgo": skew["sesgo"],
            })
            rr = skew["rr_30d"]
            if rr <= -5:
                if bias == "ALCISTA":
                    adj -= 2
                    labels.append(f"RR {rr:+.1f}% puts caros — sesgo bajista mercado, contradice ALCISTA")
                else:
                    adj += 1
                    labels.append(f"RR {rr:+.1f}% puts caros — confirma sesgo BAJISTA")
            elif rr >= 5:
                if bias == "BAJISTA":
                    adj -= 2
                    labels.append(f"RR {rr:+.1f}% calls caros — sesgo alcista mercado, contradice BAJISTA")
                else:
                    adj += 1
                    labels.append(f"RR {rr:+.1f}% calls caros — confirma sesgo ALCISTA")

    adj = max(-3, min(3, adj))

    return {
        "adj":        adj,
        "label":      " | ".join(labels) if labels else "Vol neutral",
        "signals_ok": sig_ok,
        "data":       vol_data,
    }


# ══════════════════════════════════════════════════════════════
#  CONTEXTO LLM — bloque para inyectar en system prompt
# ══════════════════════════════════════════════════════════════

def get_vol_contexto(spot: float = None) -> str:
    """
    Línea de contexto compacta para el system prompt.
    Intenta Deribit primero; fallback automático a HV desde Binance si Deribit falla.
    Siempre retorna algo útil mientras Binance esté disponible.
    """
    partes = []
    fuente = "Deribit"

    # ── Intento principal: Deribit ────────────────────────────
    dvol = get_dvol()
    if dvol.get("valor") is not None and not dvol.get("error"):
        regime_labels = {
            "HIGH_VOL": "VOL ALTA",
            "LOW_VOL":  "VOL BAJA",
            "NORMAL":   "normal",
        }
        partes.append(
            f"DVOL {dvol['valor']}% ({dvol['pctil']:.0f}%ile 30D — "
            f"{regime_labels.get(dvol['regimen'], dvol['regimen'])})"
        )

    if spot:
        ts = get_term_structure(spot)
        if ts.get("error"):
            logger.warning(f"Vol term structure no disponible: {ts['error']}")
        elif ts.get("iv_30d"):
            partes.append(
                f"Term struct {ts['forma']}: "
                f"7D={ts.get('iv_7d', '?')}% 30D={ts['iv_30d']}% 90D={ts.get('iv_90d', '?')}%"
            )

        skew = get_iv_skew(spot)
        if skew.get("error"):
            logger.warning(f"Vol IV skew no disponible: {skew['error']}")
        elif skew.get("rr_30d") is not None:
            sesgo_labels = {
                "PUTS_CAROS":  "sesgo bajista implícito",
                "CALLS_CAROS": "sesgo alcista implícito",
                "NEUTRAL":     "skew neutral",
            }
            partes.append(
                f"25D-RR proxy {skew['rr_30d']:+.1f}% "
                f"({sesgo_labels.get(skew['sesgo'], skew['sesgo'])})"
            )

    # ── Fallback: HV desde Binance cuando Deribit no responde ─
    if not partes:
        hv = _get_hv_surface()
        if hv.get("disponible"):
            fuente = "HV~"
            regime_labels = {
                "HIGH_VOL": "VOL ALTA",
                "LOW_VOL":  "VOL BAJA",
                "NORMAL":   "normal",
            }
            partes.append(
                f"HV21 {hv['hv21']}% (vol realizada — "
                f"{regime_labels.get(hv['regimen'], hv['regimen'])})"
            )
            if hv.get("hv7"):
                hv60_str = f" 60D≈{hv['hv60']}%" if hv.get("hv60") else ""
                partes.append(
                    f"Curva HV {hv['forma']}: 7D≈{hv['hv7']}% 21D≈{hv['hv21']}%{hv60_str}"
                )
            logger.warning("Deribit no disponible — usando HV fallback desde Binance")

    if not partes:
        return ""

    return f"VOLATILIDAD IMPLÍCITA ({fuente}): " + " | ".join(partes)


# ══════════════════════════════════════════════════════════════
#  RESUMEN COMPLETO — para UI y /api/vol/surface
# ══════════════════════════════════════════════════════════════

def get_vol_resumen(spot: float = None) -> dict:
    """Dict completo: DVOL + term_structure + skew + HV fallback para UI."""
    dvol = get_dvol()
    ts   = get_term_structure(spot) if spot else {}
    skew = get_iv_skew(spot)        if spot else {}
    hv   = _get_hv_surface()

    return {
        "dvol":           dvol,
        "term_structure": ts,
        "skew":           skew,
        "hv_surface":     hv,
        "deribit_ok":     dvol.get("valor") is not None and not dvol.get("error"),
    }
