"""
correlation.py — Correlation Matrix: BTC vs SPX + Gold
═══════════════════════════════════════════════════════
Sin API key. Fuentes:
  BTC closes  → Binance via get_velas() (ya en caché)
  SPX closes  → yfinance ^GSPC
  Gold closes → yfinance GC=F

Funciones exportadas:
  get_correlation_matrix()  → dict: corrs 30D/90D, régimen, narrativa
  get_corr_score(bias)      → dict: adj ±2, label, signals_ok, data
  get_corr_contexto()       → str para system prompt
  get_corr_resumen()        → dict completo para UI y /api/correlation
"""

import logging
import math
import time
import threading

logger = logging.getLogger(__name__)

_cache:  dict = {}
_lock = threading.Lock()


def _cget(key: str):
    with _lock:
        e = _cache.get(key)
        if e and time.time() < e["exp"]:
            return e["data"]
    return None


def _cset(key: str, data, ttl: int = 3600):
    with _lock:
        _cache[key] = {"data": data, "exp": time.time() + ttl}
    return data


# ── Data loaders ────────────────────────────────────────────

def _get_btc_closes(n: int = 120) -> dict:
    """
    BTC daily closes desde Binance. Returns {date_str: close}.
    date_str = 'YYYY-MM-DD' (primeros 10 chars de la fecha de vela).
    """
    try:
        from binance_data import get_velas
        velas = get_velas("BTC/USDT", "1d", n)
        result = {}
        for v in velas:
            fecha = v.get("fecha", "")
            close = v.get("close")
            if fecha and close:
                result[fecha[:10]] = float(close)  # '2026-04-14'
        return result
    except Exception as e:
        logger.warning(f"Correlation: BTC closes error: {e}")
        return {}


def _get_yf_closes(symbol: str, period: str = "6mo") -> dict:
    """
    Closes desde yfinance. Returns {date_str: close}.
    Sólo días de mercado (L-V). Maneja fines de semana automáticamente.
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period=period)
        if hist.empty:
            return {}
        return {
            idx.strftime("%Y-%m-%d"): float(row["Close"])
            for idx, row in hist.iterrows()
        }
    except Exception as e:
        logger.warning(f"Correlation: yfinance {symbol} error: {e}")
        return {}


# ── Correlation math ─────────────────────────────────────────

def _pearson(closes_a: dict, closes_b: dict, days: int) -> float | None:
    """
    Pearson r de retornos diarios sobre las últimas `days` fechas comunes.
    Ambas series deben tener al menos 10 datos solapados.
    """
    # Fechas comunes, ordenadas ascendente
    common = sorted(set(closes_a) & set(closes_b))
    if not common:
        return None

    # Tomar las últimas `days+1` fechas (necesitamos +1 para calcular N retornos)
    window = common[-(days + 1):]
    if len(window) < 11:  # necesitamos al menos 10 retornos
        window = common   # usar todo lo disponible

    a_closes = [closes_a[d] for d in window]
    b_closes = [closes_b[d] for d in window]

    # Retornos diarios
    ra = [(a_closes[i] - a_closes[i-1]) / a_closes[i-1] for i in range(1, len(a_closes))]
    rb = [(b_closes[i] - b_closes[i-1]) / b_closes[i-1] for i in range(1, len(b_closes))]

    n = len(ra)
    if n < 10:
        return None

    mean_a = sum(ra) / n
    mean_b = sum(rb) / n

    cov    = sum((ra[i] - mean_a) * (rb[i] - mean_b) for i in range(n)) / n
    var_a  = sum((x - mean_a) ** 2 for x in ra) / n
    var_b  = sum((x - mean_b) ** 2 for x in rb) / n

    if var_a < 1e-12 or var_b < 1e-12:
        return None

    r = cov / (math.sqrt(var_a) * math.sqrt(var_b))
    return round(max(-1.0, min(1.0, r)), 3)


def _spx_regime(corr: float | None) -> str:
    if corr is None:
        return "DESCONOCIDA"
    if corr >= 0.7:
        return "ALTA"
    if corr >= 0.4:
        return "MODERADA"
    if corr <= -0.3:
        return "INVERSA"
    return "BAJA"


# ══════════════════════════════════════════════════════════════
#  CORRELATION MATRIX — función principal
# ══════════════════════════════════════════════════════════════

def get_correlation_matrix() -> dict:
    """
    BTC-SPX y BTC-Gold en ventanas 30D y 90D.

    Returns dict:
        btc_spx_30d   float | None
        btc_spx_90d   float | None
        btc_gold_30d  float | None
        btc_gold_90d  float | None
        spx_regimen   str   ALTA | MODERADA | BAJA | INVERSA | DESCONOCIDA
        gold_narrative bool  True si BTC-Gold 30D >= 0.5
        error         str | None
    """
    cached = _cget("corr_matrix")
    if cached is not None:
        return cached

    try:
        btc  = _get_btc_closes(120)
        spx  = _get_yf_closes("^GSPC", "6mo")
        gold = _get_yf_closes("GC=F",  "6mo")

        if not btc:
            return _cset("corr_matrix",
                         {"error": "Sin datos BTC", "btc_spx_30d": None, "btc_gold_30d": None},
                         ttl=60)

        btc_spx_30d  = _pearson(btc, spx,  30)
        btc_spx_90d  = _pearson(btc, spx,  90)
        btc_gold_30d = _pearson(btc, gold, 30)
        btc_gold_90d = _pearson(btc, gold, 90)

        out = {
            "btc_spx_30d":    btc_spx_30d,
            "btc_spx_90d":    btc_spx_90d,
            "btc_gold_30d":   btc_gold_30d,
            "btc_gold_90d":   btc_gold_90d,
            "spx_regimen":    _spx_regime(btc_spx_30d),
            "gold_narrative": (btc_gold_30d is not None and btc_gold_30d >= 0.5),
            "spx_last":       None,  # spot price, rellenado abajo
            "gold_last":      None,
            "error":          None,
        }

        # Precio actual de SPX y Gold (para la UI)
        if spx:
            out["spx_last"] = round(list(spx.values())[-1], 2)
        if gold:
            out["gold_last"] = round(list(gold.values())[-1], 2)

        # Si algún valor clave es None (yfinance tardó en el cold-start),
        # cachear con TTL corto para reintentar pronto en vez de bloquear 1h.
        ttl = 3600 if (btc_spx_30d is not None and btc_gold_30d is not None) else 60
        return _cset("corr_matrix", out, ttl=ttl)

    except Exception as e:
        logger.warning(f"Correlation matrix error: {e}")
        return _cset("corr_matrix",
                     {"error": str(e), "btc_spx_30d": None, "btc_gold_30d": None},
                     ttl=60)


# ══════════════════════════════════════════════════════════════
#  SCORE MODIFIER para scanner._score_macro()
# ══════════════════════════════════════════════════════════════

def get_corr_score(bias: str) -> dict:
    """
    Modifier ±2 basado en correlación BTC-SPX y BTC-Gold.

    Lógica:
      BTC-SPX ≥ 0.7 + ALCISTA  → -2  (muy expuesto a selloff equities)
      BTC-SPX ≥ 0.7 + BAJISTA  →  0  (correlación alta ayuda al corto)
      BTC-SPX 0.4-0.7 + ALCISTA → -1  (correlación moderada, cierto riesgo macro)
      BTC-SPX ≤ -0.3            → +1  (movimiento idiosincrático, menos riesgo macro)
      BTC-Gold ≥ 0.5            → +1  (digital gold narrativa activa — safe haven bid)

    Returns: {adj, label, signals_ok, data}
    """
    matrix = get_correlation_matrix()

    adj      = 0
    labels   = []
    sig_ok   = 0
    corr_data: dict = {}

    spx  = matrix.get("btc_spx_30d")
    gold = matrix.get("btc_gold_30d")

    # ── BTC-SPX ───────────────────────────────────────────────
    if spx is not None and not matrix.get("error"):
        sig_ok += 1
        corr_data["btc_spx_30d"] = spx
        corr_data["spx_regimen"] = matrix.get("spx_regimen")

        if spx >= 0.7:
            if bias == "ALCISTA":
                adj -= 2
                labels.append(f"BTC-SPX {spx:+.2f} ALTA correlación — riesgo selloff equities")
            else:
                labels.append(f"BTC-SPX {spx:+.2f} ALTA correlación — acompaña caída mercados")
        elif spx >= 0.4:
            if bias == "ALCISTA":
                adj -= 1
                labels.append(f"BTC-SPX {spx:+.2f} correlación moderada")
            else:
                labels.append(f"BTC-SPX {spx:+.2f} correlación moderada")
        elif spx <= -0.3:
            adj += 1
            labels.append(f"BTC-SPX {spx:+.2f} inversa — movimiento idiosincrático")
        else:
            labels.append(f"BTC-SPX {spx:+.2f} decorrelado")

    # ── BTC-Gold ──────────────────────────────────────────────
    if gold is not None and not matrix.get("error"):
        sig_ok += 1
        corr_data["btc_gold_30d"] = gold
        corr_data["gold_narrative"] = matrix.get("gold_narrative", False)

        if gold >= 0.5:
            adj += 1
            labels.append(f"BTC-Gold {gold:+.2f} narrativa digital gold — safe haven bid")
        elif gold <= -0.3:
            labels.append(f"BTC-Gold {gold:+.2f} inversa — narrativa hedge rota")

    adj = max(-2, min(2, adj))

    return {
        "adj":        adj,
        "label":      " | ".join(labels) if labels else "Corr neutral",
        "signals_ok": sig_ok,
        "data":       corr_data,
    }


# ══════════════════════════════════════════════════════════════
#  CONTEXTO LLM
# ══════════════════════════════════════════════════════════════

def get_corr_contexto() -> str:
    """
    Línea compacta para inyectar en system prompt.
    Retorna "" si no hay datos.
    """
    matrix = get_correlation_matrix()
    if matrix.get("error") or matrix.get("btc_spx_30d") is None:
        return ""

    partes = []
    spx30  = matrix.get("btc_spx_30d")
    spx90  = matrix.get("btc_spx_90d")
    gold30 = matrix.get("btc_gold_30d")

    if spx30 is not None:
        trend = ""
        if spx90 is not None:
            delta = spx30 - spx90
            trend = " ↑" if delta > 0.1 else (" ↓" if delta < -0.1 else "")
        regime = matrix.get("spx_regimen", "")
        partes.append(f"BTC-SPX 30D:{spx30:+.2f}{trend} ({regime})")

    if gold30 is not None:
        suffix = " — digital gold" if matrix.get("gold_narrative") else ""
        partes.append(f"BTC-Gold 30D:{gold30:+.2f}{suffix}")

    if not partes:
        return ""

    return "CORRELACIONES (30D): " + " | ".join(partes)


# ══════════════════════════════════════════════════════════════
#  RESUMEN para UI y /api/correlation
# ══════════════════════════════════════════════════════════════

def get_corr_resumen() -> dict:
    """Dict completo con matrix + interpretaciones."""
    matrix = get_correlation_matrix()

    # Añadir etiquetas interpretativas para la UI
    spx30  = matrix.get("btc_spx_30d")
    gold30 = matrix.get("btc_gold_30d")

    regime_labels = {
        "ALTA":       "Alta — riesgo equities",
        "MODERADA":   "Moderada",
        "BAJA":       "Baja — decorrelado",
        "INVERSA":    "Inversa — idiosincrático",
        "DESCONOCIDA": "Sin datos",
    }

    return {
        **matrix,
        "spx_label":  regime_labels.get(matrix.get("spx_regimen", "DESCONOCIDA"), "Sin datos"),
        "gold_label": (
            "Digital gold activa" if matrix.get("gold_narrative")
            else ("Hedge rota" if gold30 is not None and gold30 <= -0.3 else "Neutral")
        ),
    }
