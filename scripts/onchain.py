"""
onchain.py — On-Chain Intelligence (100% Binance)
══════════════════════════════════════════════════
100% gratuito — sin API key, sin registro, sin dependencias externas.

Fuente única: Binance histórico (365D de closes diarios)
  Realized Price ≈ SMA 365D del precio BTC
  MVRV           = Precio actual / RP_aprox
  NUPL           = (Precio - RP_aprox) / Precio

No es el cálculo UTXO exacto de Glassnode, pero es direccionalmente
correcto y suficiente para la toma de decisiones en trading de futuros.
"""

import time
import logging
import threading

logger = logging.getLogger(__name__)

# ── Cache en memoria — 4 horas ────────────────────────────────────────
_lock  = threading.Lock()
_cache: dict = {}
_TTL   = 4 * 3600


def _cget(key: str):
    with _lock:
        e = _cache.get(key)
        if e and (time.time() - e["ts"]) < _TTL:
            return e["data"]
    return None


def _cset(key: str, data):
    with _lock:
        _cache[key] = {"ts": time.time(), "data": data}


# ══════════════════════════════════════════════════════════════════════
# FUENTE — Binance histórico (cero costo, siempre disponible)
# Realized Price ≈ SMA 365D | MVRV = precio / RP | NUPL = (P-RP)/P
# ══════════════════════════════════════════════════════════════════════

def _get_precio_historia() -> list[float]:
    """Trae closes diarios de BTC/USDT desde Binance (caché 4h)."""
    cached = _cget("binance_1d_closes")
    if cached is not None:
        return cached

    try:
        from binance_data import get_velas
        velas = get_velas("BTC/USDT", "1d", 365)
        closes = [v["close"] for v in velas if v.get("close")]
        _cset("binance_1d_closes", closes)
        return closes
    except Exception as e:
        logger.debug(f"Error obteniendo historia Binance: {e}")
        return []


def get_realized_price(precio_actual: float = None) -> dict:
    """
    Realized Price aproximado = SMA 365D del precio BTC.
    Metodología estándar para estimar la base de costo del mercado
    sin acceso a datos UTXO.
    """
    closes = _get_precio_historia()
    if len(closes) < 30:
        return {"disponible": False, "error": "Insuficientes datos históricos"}

    rp = sum(closes) / len(closes)

    dist_pct = None
    if precio_actual and rp > 0:
        dist_pct = round(((precio_actual - rp) / rp) * 100, 2)

    return {
        "disponible":  True,
        "precio":      round(rp, 2),
        "dist_pct":    dist_pct,
        "n_dias":      len(closes),
        "metodologia": "SMA 365D",
    }


def get_mvrv(precio_actual: float = None) -> dict:
    """
    MVRV aproximado = Precio actual / Realized Price (SMA 365D).
    >2.4 históricamente = zona de techo | <1.0 = zona de suelo.
    Umbrales ajustados para el ciclo actual de BTC.
    """
    closes = _get_precio_historia()
    if len(closes) < 30:
        return {"disponible": False, "error": "Insuficientes datos históricos"}

    rp = sum(closes) / len(closes)

    # Usar precio actual o último close disponible
    precio = precio_actual or closes[-1]
    actual = precio / rp

    if actual > 3.5:
        label = "zona techo histórico"; signal = -2; emoji = "🔴"
    elif actual > 2.4:
        label = "sobrevaluado";         signal = -1; emoji = "🟠"
    elif actual >= 1.0:
        label = "fair value";           signal =  0; emoji = "🟡"
    elif actual >= 0.8:
        label = "infravaluado";         signal = +1; emoji = "🟢"
    else:
        label = "zona suelo histórico"; signal = +2; emoji = "💎"

    return {
        "disponible": True,
        "valor":      round(actual, 2),
        "label":      label,
        "signal":     signal,
        "emoji":      emoji,
    }


def get_nupl(precio_actual: float = None) -> dict:
    """
    NUPL aproximado = (Precio - RP) / Precio
    Fases del ciclo de mercado. Umbrales históricos BTC.
    """
    closes = _get_precio_historia()
    if len(closes) < 30:
        return {"disponible": False, "error": "Insuficientes datos históricos"}

    rp     = sum(closes) / len(closes)
    precio = precio_actual or closes[-1]
    actual = (precio - rp) / precio

    # Calcular tendencia comparando vs hace 30 días (aprox)
    if len(closes) >= 30:
        rp_30d   = sum(closes[:-30]) / max(len(closes) - 30, 1)
        nupl_30d = (closes[-30] - rp_30d) / closes[-30] if closes[-30] else actual
        trend    = ("subiendo" if actual > nupl_30d + 0.03
                    else "bajando" if actual < nupl_30d - 0.03
                    else "estable")
    else:
        trend = "estable"

    if actual < -0.25:
        phase = "Capitulación";    signal = +3; emoji = "💎"; color = "#22c55e"
    elif actual < 0:
        phase = "Fear/Hope";       signal = +1; emoji = "🟢"; color = "#86efac"
    elif actual < 0.25:
        phase = "Optimismo";       signal =  0; emoji = "🟡"; color = "#fbbf24"
    elif actual < 0.50:
        phase = "Creencia";        signal = -1; emoji = "🟠"; color = "#f97316"
    elif actual < 0.75:
        phase = "Euforia";         signal = -2; emoji = "🔴"; color = "#ef4444"
    else:
        phase = "Euforia Extrema"; signal = -3; emoji = "🚨"; color = "#991b1b"

    return {
        "disponible": True,
        "valor":      round(actual, 4),
        "phase":      phase,
        "trend":      trend,
        "signal":     signal,
        "emoji":      emoji,
        "color":      color,
        "aprox":      True,   # flag para UI
    }


# ══════════════════════════════════════════════════════════════════════
# SCORE COMPUESTO PARA SCANNER  (NUPL 50% + MVRV 35% + Addr 15%)
# ══════════════════════════════════════════════════════════════════════

def get_onchain_score(bias: str, precio_actual: float = None) -> dict:
    nupl = get_nupl(precio_actual)
    mvrv = get_mvrv(precio_actual)
    rp   = get_realized_price(precio_actual)

    signals_ok = 0
    raw_bull   = 0.0
    parts      = []

    if nupl.get("disponible"):
        raw_bull   += nupl["signal"] * 0.60   # NUPL: peso principal
        signals_ok += 1
        parts.append(f"NUPL {nupl['phase']}")

    if mvrv.get("disponible"):
        raw_bull   += mvrv["signal"] * 0.40   # MVRV: peso secundario
        signals_ok += 1
        parts.append(f"MVRV {mvrv['valor']} ({mvrv['label']})")

    if signals_ok == 0:
        return {"adj": 0, "label": "On-chain N/D", "data": {}, "signals_ok": 0}

    adj_raw = raw_bull if bias == "ALCISTA" else -raw_bull
    adj     = max(-3, min(3, round(adj_raw)))

    if rp.get("disponible") and precio_actual and rp.get("dist_pct") is not None:
        parts.append(f"RP ~${rp['precio']:,.0f} ({rp['dist_pct']:+.1f}%)")

    return {
        "adj":        adj,
        "label":      " | ".join(parts),
        "data":       {"nupl": nupl, "mvrv": mvrv, "realized_price": rp},
        "signals_ok": signals_ok,
    }


# ══════════════════════════════════════════════════════════════════════
# BLOQUE TEXTO PARA SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════

def get_onchain_contexto(precio_actual: float = None) -> str:
    nupl = get_nupl(precio_actual)
    mvrv = get_mvrv(precio_actual)
    rp   = get_realized_price(precio_actual)

    lines = []

    if nupl.get("disponible"):
        lines.append(
            f"NUPL~: {nupl['valor']:.3f} — {nupl['phase']} {nupl['emoji']} "
            f"(trend: {nupl['trend']}, aprox SMA365)"
        )
    if mvrv.get("disponible"):
        lines.append(f"MVRV~: {mvrv['valor']} {mvrv['emoji']} — {mvrv['label']}")

    if rp.get("disponible"):
        dist = rp.get("dist_pct")
        if dist is not None:
            status = "sobre" if dist > 0 else "bajo"
            lines.append(
                f"Realized Price~: ${rp['precio']:,.0f} (SMA{rp['n_dias']}D) — "
                f"precio {status} RP en {abs(dist):.1f}%"
            )

    if not lines:
        return ""

    return "ON-CHAIN (aprox):\n" + "\n".join(f"  {l}" for l in lines)


# ══════════════════════════════════════════════════════════════════════
# RESUMEN COMPLETO PARA UI / API
# ══════════════════════════════════════════════════════════════════════

def get_onchain_resumen(precio_actual: float = None) -> dict:
    nupl = get_nupl(precio_actual)
    mvrv = get_mvrv(precio_actual)
    rp   = get_realized_price(precio_actual)

    metricas_ok = sum(1 for m in [nupl, mvrv, rp] if m.get("disponible"))

    return {
        "disponible":     metricas_ok > 0,
        "sin_api_key":    True,
        "fuente":         "Binance 365D SMA (sin API externa)",
        "metricas_ok":    metricas_ok,
        "nupl":           nupl,
        "mvrv":           mvrv,
        "realized_price": rp,
    }
