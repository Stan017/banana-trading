"""
backtest_scorer.py — Backtest offline del scoring COMPLETO (MACRO + EDGE + TECNICO)
═════════════════════════════════════════════════════════════════════════════════════
Evalúa el scorer de 3 capas contra datos históricos de Binance.

MACRO  (0-35 pts) — datos disponibles históricamente:
    - EMA200 Daily: precio sobre/bajo tendencia primaria  → 15 pts
    - Funding rate: sesgo de mercado de futuros          →  5 pts
    - Volatilidad relativa: ATR / precio                 →  5 pts
    - Tendencia macro 4H (EMA50 slope)                   → 10 pts

EDGE   (0-25 pts) — señales de oportunidad:
    - Kill zone (horario NY/London)                      → 10 pts
    - CVD momentum (dirección sostenida N velas)         →  8 pts
    - Volume spike (volumen > 1.5x media 20)             →  7 pts

TECNICO (0-40 pts) — confirmación técnica:
    - EMAs 9/21/200 cruce                                → 15 pts
    - RSI 14                                             → 10 pts
    - CVD bias acumulado                                 →  8 pts
    - VPOC posición                                      →  5 pts
    - Funding sign                                       →  2 pts

EXCLUIDOS (sin histórico limpio):
    - OI delta, L2 book, liquidation zones, delta tick-by-tick
    - DXY, BTC.D (requieren feed externo)
    - Fear & Greed (no disponible históricamente en ccxt)

Uso:
    python backtest_scorer.py [--symbol BTC/USDT] [--tf 4h] [--dias 180] [--fwd 12] [--thresh 65]
"""

import argparse
import csv
import sys
from datetime import datetime, timezone, timedelta

try:
    import ccxt
except ImportError:
    print("[ERROR] pip install ccxt  — necesario para backtest")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    print("[ERROR] pip install numpy  — necesario para backtest")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════
# CICLOS BTC — datos verificados (documento ciclosDeBitcoin.txt)
# ══════════════════════════════════════════════════════════════
# Fases:
#   ACCUM  → entre bear bottom y halving (acumulación silenciosa)
#   EARLY  → halving hasta +6 meses (consolidación post-halving)
#   BULL   → +6 meses post-halving hasta ATH (markup explosivo)
#   BEAR   → ATH hasta bear bottom (distribución + crash)

_UTC = timezone.utc

_HALVINGS = [
    datetime(2012, 11, 28, tzinfo=_UTC),
    datetime(2016,  7,  9, tzinfo=_UTC),
    datetime(2020,  5, 11, tzinfo=_UTC),
    datetime(2024,  4, 20, tzinfo=_UTC),
]

_ATHS = [
    datetime(2013, 11, 29, tzinfo=_UTC),   # ~$1,156
    datetime(2017, 12, 17, tzinfo=_UTC),   # ~$19,783
    datetime(2021, 11, 10, tzinfo=_UTC),   # ~$68,789
    datetime(2025, 10,  6, tzinfo=_UTC),   # ~$126,198
]

_BEAR_BOTTOMS = [
    datetime(2015,  1, 14, tzinfo=_UTC),   # ~$152
    datetime(2018, 12, 15, tzinfo=_UTC),   # ~$3,122
    datetime(2022, 11, 21, tzinfo=_UTC),   # ~$15,500
    # Ciclo 4: bottom estimado oct-dic 2026 — usamos dic 2026 como placeholder
    datetime(2026, 12,  1, tzinfo=_UTC),
]

# Eventos ordenados cronológicamente con etiqueta de la FASE que INICIA
_CYCLE_EVENTS = sorted([
    *[("ACCUM_START",  b) for b in _BEAR_BOTTOMS],
    *[("HALVING",      h) for h in _HALVINGS],
    *[("ATH",          a) for a in _ATHS],
], key=lambda x: x[1])


def get_cycle_phase(ts_ms: int) -> str:
    """
    Devuelve la fase del ciclo BTC para un timestamp en milisegundos.
    ACCUM / EARLY / BULL / BEAR
    """
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=_UTC)

    # Buscar el evento más reciente anterior a dt
    last_event = None
    for label, event_dt in _CYCLE_EVENTS:
        if event_dt <= dt:
            last_event = (label, event_dt)
        else:
            break

    if last_event is None:
        return "ACCUM"  # antes del primer evento conocido

    label, event_dt = last_event

    if label == "ACCUM_START":
        return "ACCUM"
    elif label == "HALVING":
        # Primeros 6 meses post-halving = EARLY, luego BULL
        dias = (dt - event_dt).days
        return "EARLY" if dias < 180 else "BULL"
    elif label == "ATH":
        return "BEAR"

    return "DESCONOCIDO"


# ══════════════════════════════════════════════════════════════
# FETCHER
# ══════════════════════════════════════════════════════════════

def fetch_ohlcv(symbol: str, tf: str, dias: int) -> list:
    """Descarga OHLCV paginando si necesita más de 1000 velas."""
    exchange   = ccxt.binance({"enableRateLimit": True})
    tf_ms      = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
    ms_per_bar = tf_ms.get(tf, 14_400_000)
    total      = int(dias * 86_400_000 / ms_per_bar)
    since      = int((datetime.now(tz=_UTC).timestamp() * 1000) - dias * 86_400_000)

    all_bars = []
    while len(all_bars) < total:
        batch = exchange.fetch_ohlcv(symbol, timeframe=tf, since=since, limit=1000)
        if not batch:
            break
        all_bars += batch
        since = batch[-1][0] + ms_per_bar
        if len(batch) < 1000:
            break

    # Deduplicar y ordenar por timestamp
    seen = {}
    for bar in all_bars:
        seen[bar[0]] = bar
    result = sorted(seen.values(), key=lambda x: x[0])
    print(f"  {len(result)} velas {tf} descargadas ({dias} dias)")
    return result


def fetch_ohlcv_daily(symbol: str, dias: int = 500) -> list:
    """Velas diarias para EMA200D — paginado."""
    exchange = ccxt.binance({"enableRateLimit": True})
    since    = int((datetime.now(tz=_UTC).timestamp() * 1000) - dias * 86_400_000)
    all_bars = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe="1d", since=since, limit=1000)
        if not batch:
            break
        all_bars += batch
        since = batch[-1][0] + 86_400_000
        if len(batch) < 1000:
            break
    seen   = {}
    for bar in all_bars:
        seen[bar[0]] = bar
    result = sorted(seen.values(), key=lambda x: x[0])
    print(f"  {len(result)} velas 1D descargadas")
    return result


def fetch_funding(symbol: str, limit: int = 500) -> dict:
    try:
        exchange = ccxt.binance({"enableRateLimit": True})
        fsym  = symbol.replace("/", "")
        rates = exchange.fetch_funding_rate_history(fsym, limit=limit)
        return {int(r["timestamp"]): r["fundingRate"] for r in rates if r.get("fundingRate") is not None}
    except Exception as e:
        print(f"[WARN] Funding history no disponible: {e}")
        return {}


# ══════════════════════════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════════════════════════

def ema(values: list, periodo: int) -> list:
    k   = 2 / (periodo + 1)
    out = [None] * len(values)
    for i, v in enumerate(values):
        if i == 0:
            out[i] = v
        else:
            prev   = out[i - 1] if out[i - 1] is not None else v
            out[i] = v * k + prev * (1 - k)
    return out


def rsi(closes: list, periodo: int = 14) -> list:
    out           = [None] * len(closes)
    gains, losses = [], []
    avg_g = avg_l = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
        if i < periodo:
            continue
        if i == periodo:
            avg_g = sum(gains[-periodo:]) / periodo
            avg_l = sum(losses[-periodo:]) / periodo
        else:
            avg_g = (avg_g * (periodo - 1) + gains[-1]) / periodo
            avg_l = (avg_l * (periodo - 1) + losses[-1]) / periodo
        rs     = avg_g / avg_l if avg_l else float("inf")
        out[i] = 100 - 100 / (1 + rs)
    return out


def atr(highs: list, lows: list, closes: list, periodo: int = 14) -> list:
    trs  = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i]  - closes[i - 1])))
    return ema(trs, periodo)


def cvd_delta_series(opens: list, closes: list, volumes: list) -> list:
    """Delta acumulado por vela (+ = compra, - = venta)."""
    return [v if c >= o else -v for o, c, v in zip(opens, closes, volumes)]


def cvd_bias_arr(deltas: list) -> list:
    ema20 = ema(deltas, 20)
    return [("bullish" if e and e > 0 else "bearish") for e in ema20]


def cvd_momentum(deltas: list, window: int = 5) -> list:
    """Cuántas velas consecutivas el delta va en la misma dirección."""
    result = [0] * len(deltas)
    for i in range(window, len(deltas)):
        seg  = deltas[i - window:i]
        pos  = sum(1 for d in seg if d > 0)
        neg  = sum(1 for d in seg if d < 0)
        if pos >= window - 1:
            result[i] = 1   # momentum alcista
        elif neg >= window - 1:
            result[i] = -1  # momentum bajista
    return result


def volume_spike(volumes: list, window: int = 20) -> list:
    result = [False] * len(volumes)
    for i in range(window, len(volumes)):
        avg = sum(volumes[i - window:i]) / window
        result[i] = volumes[i] > avg * 1.5
    return result


def vpoc_bias_arr(highs: list, lows: list, closes: list,
                  volumes: list, window: int = 100, bins: int = 50) -> list:
    result = [None] * len(closes)
    for i in range(window, len(closes)):
        seg_h = highs[i - window:i]
        seg_l = lows[i - window:i]
        seg_v = volumes[i - window:i]
        pmin, pmax = min(seg_l), max(seg_h)
        if pmax == pmin:
            result[i] = "at_vpoc"
            continue
        bsize   = (pmax - pmin) / bins
        vbins   = [0.0] * bins
        for j in range(len(seg_h)):
            b = min(int(((seg_h[j] + seg_l[j]) / 2 - pmin) / bsize), bins - 1)
            vbins[b] += seg_v[j]
        vpoc_px = pmin + (vbins.index(max(vbins)) + 0.5) * bsize
        dist    = (closes[i] - vpoc_px) / vpoc_px * 100
        result[i] = "above_vpoc" if dist > 1.0 else "below_vpoc" if dist < -1.0 else "at_vpoc"
    return result


def ema200d_bias_arr(daily_closes: list, ts_4h: list, ts_daily: list) -> list:
    """
    Mapea EMA200 diaria a cada vela 4H.
    Retorna 'above' | 'below' | None por cada vela 4H.
    """
    ema200d = ema(daily_closes, 200)
    # Construir lookup: ts_dia -> ema200d valor
    daily_lookup = {}
    for i, ts in enumerate(ts_daily):
        daily_lookup[ts // (86400 * 1000) * (86400 * 1000)] = ema200d[i]

    result = []
    for ts in ts_4h:
        day_ts = ts // (86400 * 1000) * (86400 * 1000)
        val    = daily_lookup.get(day_ts)
        if val is None:
            # Buscar el día más cercano anterior
            candidates = [d for d in daily_lookup if d <= day_ts]
            val = daily_lookup[max(candidates)] if candidates else None
        result.append(val)
    return result


def is_kill_zone(ts_ms: int) -> bool:
    """NY session 13-17 UTC | London 07-12 UTC."""
    hour = (ts_ms // (3600 * 1000)) % 24
    return 7 <= hour <= 12 or 13 <= hour <= 17


# ══════════════════════════════════════════════════════════════
# SCORERS — 3 capas
# ══════════════════════════════════════════════════════════════

def score_macro(
    close: float, ema200d: float | None,
    funding: float | None,
    atr_val: float | None, ema50_slope: float,
) -> tuple[int, str]:
    """MACRO 0–35 pts."""
    score = 0
    bias  = {"ALCISTA": 0, "BAJISTA": 0}

    # EMA200D (15 pts)
    if ema200d is not None:
        if close > ema200d * 1.005:
            score += 15; bias["ALCISTA"] += 15
        elif close < ema200d * 0.995:
            score += 15; bias["BAJISTA"] += 15
        else:
            score += 7  # zona de transición

    # Funding (5 pts)
    if funding is not None:
        if funding > 0.0005:
            score += 5; bias["BAJISTA"] += 5
        elif funding < -0.0005:
            score += 5; bias["ALCISTA"] += 5

    # Volatilidad relativa ATR/precio (5 pts) — alta vol = oportunidad
    if atr_val and close:
        rel = atr_val / close * 100
        if rel > 1.5:
            score += 5
        elif rel > 0.8:
            score += 3

    # Tendencia macro EMA50 slope (10 pts)
    if ema50_slope > 0:
        score += 10; bias["ALCISTA"] += 10
    elif ema50_slope < 0:
        score += 10; bias["BAJISTA"] += 10

    score = max(0, min(35, score))
    label = "ALCISTA" if bias["ALCISTA"] >= bias["BAJISTA"] else "BAJISTA"
    return score, label


def score_edge(
    ts_ms: int, cvd_mom: int, vol_spike: bool,
) -> tuple[int, str]:
    """EDGE 0–25 pts."""
    score = 0

    # Kill zone (10 pts)
    if is_kill_zone(ts_ms):
        score += 10

    # CVD momentum sostenido (8 pts)
    if cvd_mom != 0:
        score += 8

    # Volume spike (7 pts)
    if vol_spike:
        score += 7

    score = max(0, min(25, score))
    return score, ("ALCISTA" if cvd_mom > 0 else "BAJISTA" if cvd_mom < 0 else "NEUTRO")


def score_tecnico(
    ema9: float, ema21: float, ema200: float, rsi_val: float | None,
    cvd: str, vpoc_pos: str | None, funding: float | None,
) -> tuple[int, str]:
    """TECNICO 0–40 pts."""
    score = 0
    bias  = {"ALCISTA": 0, "BAJISTA": 0}

    # EMAs (15 pts)
    if ema9 > ema21 > ema200:
        score += 15; bias["ALCISTA"] += 15
    elif ema9 < ema21 < ema200:
        score += 15; bias["BAJISTA"] += 15
    elif ema9 > ema21:
        score += 8; bias["ALCISTA"] += 8
    elif ema9 < ema21:
        score += 8; bias["BAJISTA"] += 8

    # RSI (10 pts)
    if rsi_val is not None:
        if rsi_val < 40:
            score += 10; bias["BAJISTA"] += 10
        elif rsi_val > 60:
            score += 10; bias["ALCISTA"] += 10
        elif rsi_val < 50:
            score += 5; bias["BAJISTA"] += 5
        elif rsi_val > 50:
            score += 5; bias["ALCISTA"] += 5

    # CVD bias (8 pts)
    if cvd == "bullish":
        score += 8; bias["ALCISTA"] += 8
    elif cvd == "bearish":
        score += 8; bias["BAJISTA"] += 8

    # VPOC (5 pts)
    if vpoc_pos == "above_vpoc":
        score += 3; bias["ALCISTA"] += 3
    elif vpoc_pos == "below_vpoc":
        score += 3; bias["BAJISTA"] += 3
    elif vpoc_pos == "at_vpoc":
        score += 5

    # Funding confirmación (2 pts)
    if funding is not None:
        if funding > 0.0005:
            score += 2; bias["BAJISTA"] += 2
        elif funding < -0.0005:
            score += 2; bias["ALCISTA"] += 2

    score = max(0, min(40, score))
    label = "ALCISTA" if bias["ALCISTA"] >= bias["BAJISTA"] else "BAJISTA"
    return score, label


# ══════════════════════════════════════════════════════════════
# BACKTEST RUNNER
# ══════════════════════════════════════════════════════════════

def run_backtest(symbol: str = "BTC/USDT", tf: str = "4h", dias: int = 180,
                 forward_candles: int = 6, threshold: int = 65) -> None:

    print(f"\nBacktest COMPLETO (MACRO+EDGE+TECNICO) — {symbol} {tf} | {dias} dias | fwd {forward_candles} velas | threshold {threshold}/100")
    print("Descargando velas 4H...")
    ohlcv = fetch_ohlcv(symbol, tf, dias)
    if len(ohlcv) < 200:
        print(f"[ERROR] Solo {len(ohlcv)} velas — necesita al menos 200"); return

    ts_arr  = [c[0] for c in ohlcv]
    opens   = [c[1] for c in ohlcv]
    highs   = [c[2] for c in ohlcv]
    lows    = [c[3] for c in ohlcv]
    closes  = [c[4] for c in ohlcv]
    volumes = [c[5] for c in ohlcv]

    print("Descargando velas 1D para EMA200D...")
    daily_ohlcv  = fetch_ohlcv_daily(symbol, 250)
    daily_ts     = [c[0] for c in daily_ohlcv]
    daily_closes = [c[4] for c in daily_ohlcv]

    print(f"  {len(ohlcv)} velas 4H | {len(daily_ohlcv)} velas 1D. Descargando funding...")
    fund_map = fetch_funding(symbol, limit=500)

    # ── Calcular todos los indicadores ────────────────────────
    print("Calculando indicadores...")
    ema9_arr    = ema(closes, 9)
    ema21_arr   = ema(closes, 21)
    ema200_arr  = ema(closes, 200)
    ema50_arr   = ema(closes, 50)
    rsi_arr     = rsi(closes, 14)
    atr_arr     = atr(highs, lows, closes, 14)
    deltas      = cvd_delta_series(opens, closes, volumes)
    cvd_arr     = cvd_bias_arr(deltas)
    cvd_mom_arr = cvd_momentum(deltas, window=5)
    vol_spk_arr = volume_spike(volumes, window=20)
    vpoc_arr    = vpoc_bias_arr(highs, lows, closes, volumes, window=100)
    ema200d_arr = ema200d_bias_arr(daily_closes, ts_arr, daily_ts)

    print("Ejecutando scorer MACRO + EDGE + TECNICO...")
    rows    = []
    start_i = 210  # warm-up completo

    for i in range(start_i, len(closes) - forward_candles):
        ts = ts_arr[i]

        # Funding más cercano (±8h)
        fund_val = None
        for delta_ms in [0, 8*3600000, -8*3600000, 16*3600000]:
            fund_val = fund_map.get(ts + delta_ms)
            if fund_val is not None:
                break

        # EMA50 slope (pendiente normalizada)
        ema50_slope = (ema50_arr[i] - ema50_arr[i - 6]) / ema50_arr[i - 6] if i >= 6 and ema50_arr[i - 6] else 0

        sc_macro, bias_macro = score_macro(
            close       = closes[i],
            ema200d     = ema200d_arr[i],
            funding     = fund_val,
            atr_val     = atr_arr[i],
            ema50_slope = ema50_slope,
        )
        sc_edge, bias_edge = score_edge(
            ts_ms    = ts,
            cvd_mom  = cvd_mom_arr[i],
            vol_spike= vol_spk_arr[i],
        )
        sc_tec, bias_tec = score_tecnico(
            ema9    = ema9_arr[i],
            ema21   = ema21_arr[i],
            ema200  = ema200_arr[i],
            rsi_val = rsi_arr[i],
            cvd     = cvd_arr[i],
            vpoc_pos= vpoc_arr[i],
            funding = fund_val,
        )

        score_total = sc_macro + sc_edge + sc_tec  # 0–100

        # Bias de consenso (2 de 3)
        biases = [bias_macro, bias_edge, bias_tec]
        bias   = "ALCISTA" if biases.count("ALCISTA") >= 2 else "BAJISTA"

        ret_fwd = (closes[i + forward_candles] - closes[i]) / closes[i] * 100

        rows.append({
            "ts":           datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "close":        round(closes[i], 1),
            "score_macro":  sc_macro,
            "score_edge":   sc_edge,
            "score_tec":    sc_tec,
            "score_total":  score_total,
            "bias":         bias,
            "kill_zone":    is_kill_zone(ts),
            "cvd_mom":      cvd_mom_arr[i],
            "vol_spike":    vol_spk_arr[i],
            "rsi":          round(rsi_arr[i], 1) if rsi_arr[i] else None,
            "funding":      round(fund_val * 100, 4) if fund_val is not None else None,
            "cycle_phase":  get_cycle_phase(ts),
            "ret_fwd_pct":  round(ret_fwd, 3),
            "es_signal":    score_total >= threshold,
            "signal_ok":    (bias == "ALCISTA" and ret_fwd > 0.5) or (bias == "BAJISTA" and ret_fwd < -0.5),
        })

    # ── Estadísticas ──────────────────────────────────────────
    scores   = np.array([r["score_total"] for r in rows])
    rets     = np.array([r["ret_fwd_pct"] for r in rows])
    signals  = [r for r in rows if r["es_signal"]]
    hits     = [r for r in signals if r["signal_ok"]]
    no_sig   = [r for r in rows if not r["es_signal"]]

    sep = "=" * 62
    print(f"\n{sep}")
    print(f"  RESULTADOS — {len(rows)} velas evaluadas")
    print(f"{sep}")
    print(f"  Score promedio global:       {scores.mean():.1f}/100")
    print(f"  Score mediana:               {np.median(scores):.1f}/100")
    print(f"  Score max / min:             {scores.max()} / {scores.min()}")
    print(f"  Señales (>={threshold}/100): {len(signals)} ({len(signals)/len(rows)*100:.1f}%)")

    if signals:
        print(f"  Precision (signal_ok):       {len(hits)/len(signals)*100:.1f}%")
        avg_ret_s  = np.mean([r["ret_fwd_pct"] for r in signals])
        avg_ret_ns = np.mean([r["ret_fwd_pct"] for r in no_sig]) if no_sig else 0
        print(f"  Retorno medio CON señal:     {avg_ret_s:+.2f}%")
        print(f"  Retorno medio SIN señal:     {avg_ret_ns:+.2f}%")
        print(f"  Edge lift:                   {avg_ret_s - avg_ret_ns:+.2f}%")

    # Tabla por bins de score total
    print(f"\n  Retorno promedio por bin score total (fwd {forward_candles} velas = {forward_candles*4}h):")
    print(f"  {'Score':>10} | {'N':>5} | {'Ret avg':>9} | {'% pos':>7} | {'% neg':>7}")
    print(f"  {'-'*10}-+-{'-'*5}-+-{'-'*9}-+-{'-'*7}-+-{'-'*7}")
    for lo, hi in [(0,40),(40,50),(50,60),(60,65),(65,70),(70,80),(80,101)]:
        subset = [r for r in rows if lo <= r["score_total"] < hi]
        if not subset:
            continue
        ret_avg = np.mean([r["ret_fwd_pct"] for r in subset])
        pct_pos = sum(1 for r in subset if r["ret_fwd_pct"] > 0) / len(subset) * 100
        pct_neg = 100 - pct_pos
        marker  = " <-- SEÑAL" if lo >= threshold else ""
        print(f"  [{lo:>3}–{hi:>3})   | {len(subset):>5} | {ret_avg:>+8.2f}% | {pct_pos:>6.1f}% | {pct_neg:>6.1f}%{marker}")

    # ── Split por BIAS — núcleo del análisis ─────────────────
    print(f"\n  {'=' * 58}")
    print(f"  ANALISIS POR BIAS — todas las velas (sin filtro threshold)")
    print(f"  {'=' * 58}")
    for bias_label, ret_ok_fn in [
        ("ALCISTA", lambda r: r["ret_fwd_pct"] > 0.5),
        ("BAJISTA", lambda r: r["ret_fwd_pct"] < -0.5),
    ]:
        subset_bias = [r for r in rows if r["bias"] == bias_label]
        if not subset_bias:
            continue
        print(f"\n  Bias {bias_label} — {len(subset_bias)} velas totales:")
        print(f"  {'Score':>10} | {'N':>5} | {'Ret avg':>9} | {'% correcto':>11} | {'Kill zone%':>10}")
        print(f"  {'-'*10}-+-{'-'*5}-+-{'-'*9}-+-{'-'*11}-+-{'-'*10}")
        for lo, hi in [(50,65),(65,75),(75,80),(80,90),(90,101)]:
            seg = [r for r in subset_bias if lo <= r["score_total"] < hi]
            if not seg:
                continue
            ret_avg  = np.mean([r["ret_fwd_pct"] for r in seg])
            pct_ok   = sum(1 for r in seg if ret_ok_fn(r)) / len(seg) * 100
            pct_kz   = sum(1 for r in seg if r["kill_zone"]) / len(seg) * 100
            marker   = " <-- HONEYPOT" if lo >= 75 and pct_ok >= 55 else ""
            print(f"  [{lo:>3}–{hi:>3})   | {len(seg):>5} | {ret_avg:>+8.2f}% | {pct_ok:>10.1f}% | {pct_kz:>9.1f}%{marker}")

    # ── Kill zone dentro de señales ≥threshold ────────────────
    print(f"\n  {'=' * 58}")
    print(f"  KILL ZONE EFFECT (señales >= {threshold})")
    print(f"  {'=' * 58}")
    for bias_label in ["ALCISTA", "BAJISTA"]:
        sig_b  = [r for r in signals if r["bias"] == bias_label]
        kz_b   = [r for r in sig_b if r["kill_zone"]]
        nkz_b  = [r for r in sig_b if not r["kill_zone"]]
        if not sig_b:
            continue
        print(f"\n  {bias_label}:")
        if kz_b:
            ret_ok_fn = (lambda r: r["ret_fwd_pct"] > 0.5) if bias_label == "ALCISTA" else (lambda r: r["ret_fwd_pct"] < -0.5)
            print(f"    Con kill zone: N={len(kz_b):>3} | ret avg {np.mean([r['ret_fwd_pct'] for r in kz_b]):+.2f}% | {sum(1 for r in kz_b if ret_ok_fn(r))/len(kz_b)*100:.1f}% correcto")
        if nkz_b:
            ret_ok_fn = (lambda r: r["ret_fwd_pct"] > 0.5) if bias_label == "ALCISTA" else (lambda r: r["ret_fwd_pct"] < -0.5)
            print(f"    Sin kill zone: N={len(nkz_b):>3} | ret avg {np.mean([r['ret_fwd_pct'] for r in nkz_b]):+.2f}% | {sum(1 for r in nkz_b if ret_ok_fn(r))/len(nkz_b)*100:.1f}% correcto")

    # ══════════════════════════════════════════════════════════
    # ANALISIS POR FASE DE CICLO BTC
    # ══════════════════════════════════════════════════════════
    print(f"\n  {'=' * 58}")
    print(f"  ANALISIS POR FASE DE CICLO (datos: ciclosDeBitcoin.txt)")
    print(f"  {'=' * 58}")
    print(f"  Fase    | N total | Señales | Ret avg | % correcto BAJISTA | % correcto ALCISTA")
    print(f"  --------|---------|---------|---------|--------------------|-----------------")

    for fase in ["ACCUM", "EARLY", "BULL", "BEAR"]:
        fase_rows = [r for r in rows if r["cycle_phase"] == fase]
        if not fase_rows:
            print(f"  {fase:<7} | sin datos")
            continue

        fase_sig   = [r for r in fase_rows if r["es_signal"]]
        baj_sig    = [r for r in fase_sig  if r["bias"] == "BAJISTA"]
        alc_sig    = [r for r in fase_sig  if r["bias"] == "ALCISTA"]
        ret_avg    = np.mean([r["ret_fwd_pct"] for r in fase_rows])

        pct_baj = (sum(1 for r in baj_sig if r["ret_fwd_pct"] < -0.5) / len(baj_sig) * 100) if baj_sig else None
        pct_alc = (sum(1 for r in alc_sig if r["ret_fwd_pct"] >  0.5) / len(alc_sig) * 100) if alc_sig else None

        baj_str = f"{pct_baj:>5.1f}% (N={len(baj_sig)})" if pct_baj is not None else "  sin señales  "
        alc_str = f"{pct_alc:>5.1f}% (N={len(alc_sig)})" if pct_alc is not None else "  sin señales  "
        marker  = " <<< HONEYPOT" if fase == "BEAR" and pct_baj and pct_baj >= 55 else ""

        print(f"  {fase:<7} | {len(fase_rows):>7} | {len(fase_sig):>7} | {ret_avg:>+6.2f}% | {baj_str:<20}| {alc_str}{marker}")

    # ── Régimen obligatorio: BAJISTA solo en BEAR ──────────────
    print(f"\n  {'=' * 58}")
    print(f"  FILTRO REGIMEN OBLIGATORIO — BAJISTA solo en fase BEAR")
    print(f"  {'=' * 58}")
    bear_baj = [r for r in rows if r["cycle_phase"] == "BEAR"
                and r["bias"] == "BAJISTA" and r["es_signal"]]
    if bear_baj:
        pct_ok   = sum(1 for r in bear_baj if r["ret_fwd_pct"] < -0.5) / len(bear_baj) * 100
        ret_mean = np.mean([r["ret_fwd_pct"] for r in bear_baj])
        kz_only  = [r for r in bear_baj if r["kill_zone"]]
        pct_kz   = (sum(1 for r in kz_only if r["ret_fwd_pct"] < -0.5) / len(kz_only) * 100) if kz_only else 0

        print(f"  BEAR + BAJISTA + score>={threshold}:          N={len(bear_baj)} | ret {ret_mean:+.2f}% | {pct_ok:.1f}% correcto")
        print(f"  BEAR + BAJISTA + score>={threshold} + KZ:     N={len(kz_only)} | {pct_kz:.1f}% correcto")

        verdict = "VERDE" if pct_ok >= 55 else "AMARILLO" if pct_ok >= 48 else "ROJO"
        print(f"\n  VEREDICTO: {verdict}")
        if verdict == "VERDE":
            print(f"  55%+ alcanzado -> threshold {threshold} en BEAR valido para produccion")
        elif verdict == "AMARILLO":
            print(f"  48-55% -> edge existe pero debil. Considera bajar threshold o esperar mas datos")
        else:
            print(f"  <48% -> sin edge significativo en este rango. Revisar threshold o indicadores")
    else:
        print(f"  Sin señales BAJISTA en fase BEAR con threshold {threshold}")

    # ── Guardar CSV ────────────────────────────────────────────
    outfile = f"backtest_results_{symbol.replace('/','')}_full_{tf}_fwd{forward_candles}.csv"
    with open(outfile, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n  CSV guardado: {outfile}")
    print(f"{sep}\n")


# ══════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest scorer COMPLETO TradeBot AI")
    parser.add_argument("--symbol",  default="BTC/USDT")
    parser.add_argument("--tf",      default="4h")
    parser.add_argument("--dias",    default=180, type=int)
    parser.add_argument("--fwd",     default=12,  type=int, help="Velas forward (default 12 = 48h en 4H)")
    parser.add_argument("--thresh",  default=65,  type=int, help="Score threshold señal (default 65/100)")
    args = parser.parse_args()

    run_backtest(
        symbol=args.symbol,
        tf=args.tf,
        dias=args.dias,
        forward_candles=args.fwd,
        threshold=args.thresh,
    )
