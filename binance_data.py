import time
import logging
import ccxt
import requests as _requests
from datetime import datetime

logger = logging.getLogger(__name__)

# ============================================================
# CONFIGURACIÓN — Sin API key, datos públicos gratis
# ============================================================
exchange      = ccxt.bybit()                            # spot — precio, velas
exchange_fut  = ccxt.bybit({                           # futuros — funding, OI
    'options': { 'defaultType': 'linear' }
})

# ============================================================
# CACHÉ DE DATOS BASE — 60s TTL
# Elimina llamadas duplicadas dentro de la misma request
# ============================================================
_DATA_TTL      = 60   # segundos
_PRECIO_CACHE: dict = {}
_VELAS_CACHE:  dict = {}
_FUNDING_CACHE: dict = {}
_OI_CACHE:     dict = {}
_CVD_CACHE:    dict = {}
_TRADES_CACHE: dict = {}
_LS_CACHE:     dict = {}

_CACHE_MAXSIZE = 100  # máximo de entradas por caché

def _evict(cache: dict, ttl: float = _DATA_TTL):
    """Elimina entradas expiradas. Si sigue lleno, limpia el 50% más viejo."""
    now = time.time()
    expired = [k for k, v in cache.items() if now - v.get("ts", 0) > ttl]
    for k in expired:
        del cache[k]
    if len(cache) > _CACHE_MAXSIZE:
        sorted_keys = sorted(cache, key=lambda k: cache[k].get("ts", 0))
        for k in sorted_keys[:len(cache) // 2]:
            del cache[k]

# ============================================================
# HELPERS MATEMÁTICOS
# ============================================================

def calcular_ema(datos, periodo):
    """Exponential Moving Average"""
    if len(datos) < periodo:
        return None
    k = 2 / (periodo + 1)
    ema = sum(datos[:periodo]) / periodo
    for precio in datos[periodo:]:
        ema = precio * k + ema * (1 - k)
    return ema

def calcular_rsi(closes, periodo=62, suavizado=14):
    """
    RSI con parámetros del trader:
    - Período: 62
    - Fuente: Cierre
    - Suavizado: SMA 14 (Wilder)

    FIX: ahora el loop de Wilder usa `suavizado` correctamente
    en lugar de `periodo` para el suavizado final.
    """
    if len(closes) < periodo + 1:
        return None

    cambios   = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    ganancias = [max(c, 0) for c in cambios]
    perdidas  = [abs(min(c, 0)) for c in cambios]

    # Seed inicial con `periodo`
    avg_gan = sum(ganancias[:periodo]) / periodo
    avg_per = sum(perdidas[:periodo])  / periodo

    # FIX BUG 1: usar `suavizado` en el loop de Wilder, no `periodo`
    for i in range(periodo, len(ganancias)):
        avg_gan = (avg_gan * (suavizado - 1) + ganancias[i]) / suavizado
        avg_per = (avg_per * (suavizado - 1) + perdidas[i])  / suavizado

    if avg_per == 0:
        return 100.0

    rs  = avg_gan / avg_per
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)

def interpretar_rsi(rsi):
    """Niveles 60/40 del trader"""
    if rsi is None:
        return "Sin datos"
    if rsi >= 80:
        return "SOBRECOMPRA EXTREMA 🔴 — Trampa alcista probable"
    if rsi >= 60:
        return "ZONA ALTA ⚠️ — Momentum alcista, vigilar rechazo"
    if rsi >= 50:
        return "NEUTRAL ALCISTA — Por encima del equilibrio"
    if rsi >= 40:
        return "NEUTRAL BAJISTA — Por debajo del equilibrio"
    if rsi >= 20:
        return "ZONA BAJA ⚠️ — Momentum bajista, vigilar rebote"
    return "SOBREVENTA EXTREMA 🟢 — Trampa bajista probable"

def interpretar_funding(rate):
    """Umbrales del trader: neutro/sesgo/extremo/crítico"""
    if rate is None:
        return "Sin datos"
    abs_rate = abs(rate)
    if abs_rate <= 0.025:
        nivel = "NEUTRO ⚪ — Mercado equilibrado"
    elif abs_rate <= 0.05:
        nivel = "SESGO 🟡 — Retail apilando de un lado"
    elif abs_rate <= 0.15:
        nivel = "EXTREMO 🔴 — Trampa / liquidación masiva próxima"
    else:
        nivel = "CRÍTICO 💀 — Mercado crowded, reversión inminente"

    if rate < -0.025:
        nivel += " | Retail SHORT → Trampa alcista posible"
    elif rate > 0.025:
        nivel += " | Retail LONG → Trampa bajista posible"

    return nivel

def interpretar_oi(cambio_4h, cambio_24h, cambio_precio):
    """Combinación OI + Precio — lectura institucional"""
    if cambio_4h is None:
        return "Sin datos de OI"
    precio_sube = cambio_precio > 0
    if precio_sube and cambio_4h > 0:
        return "OI↑ + Precio↑ — Capital nuevo entrando, tendencia sostenible ✅"
    elif precio_sube and cambio_4h < 0:
        return "OI↓ + Precio↑ — Cierre de shorts forzados, rebote frágil ⚠️"
    elif not precio_sube and cambio_4h > 0:
        return "OI↑ + Precio↓ — Shorts masivos abriendo, trampa bajista posible 🔴"
    else:
        return "OI↓ + Precio↓ — Liquidaciones en cascada, movimiento limpio bajista 📉"

# ============================================================
# FUNCIONES DE DATOS
# ============================================================

def get_precio_actual(symbol="BTC/USDT"):
    """Precio actual con stats 24h — cacheado 60s"""
    ahora = time.time()
    if symbol in _PRECIO_CACHE and (ahora - _PRECIO_CACHE[symbol]["ts"]) < _DATA_TTL:
        return _PRECIO_CACHE[symbol]["data"]
    ticker = exchange.fetch_ticker(symbol)
    result = {
        "precio":      ticker["last"],
        "alto_24h":    ticker["high"],
        "bajo_24h":    ticker["low"],
        "volumen_24h": ticker["quoteVolume"],
        "cambio_24h":  ticker["percentage"],
    }
    _evict(_PRECIO_CACHE)
    _PRECIO_CACHE[symbol] = {"data": result, "ts": ahora}
    return result

def get_velas(symbol="BTC/USDT", timeframe="4h", limite=220):
    """Velas OHLCV — cacheadas 60s por (symbol, timeframe, limite).
    Intenta spot primero; si falla (geo-block, rate-limit) usa futuros."""
    ahora = time.time()
    key = (symbol, timeframe, limite)
    if key in _VELAS_CACHE and (ahora - _VELAS_CACHE[key]["ts"]) < _DATA_TTL:
        return _VELAS_CACHE[key]["data"]
    try:
        velas = exchange.fetch_ohlcv(symbol, timeframe, limit=limite)
    except Exception:
        velas = exchange_fut.fetch_ohlcv(symbol, timeframe, limit=limite)
    result = [{
        "fecha":   datetime.fromtimestamp(v[0]/1000).strftime("%Y-%m-%d %H:%M"),
        "open":    v[1],
        "high":    v[2],
        "low":     v[3],
        "close":   v[4],
        "volumen": v[5]
    } for v in velas]
    _evict(_VELAS_CACHE)
    _VELAS_CACHE[key] = {"data": result, "ts": ahora}
    return result

def get_funding_rate(symbol="BTC/USDT"):
    """Funding rate actual de Binance Futures — cacheado 60s"""
    ahora = time.time()
    if symbol in _FUNDING_CACHE and (ahora - _FUNDING_CACHE[symbol]["ts"]) < _DATA_TTL:
        return _FUNDING_CACHE[symbol]["data"]
    result = None
    try:
        symbol_fut = symbol + ":USDT"  # Bybit linear: BTC/USDT:USDT
        funding    = exchange_fut.fetch_funding_rate(symbol_fut)
        # FIX BUG 5: usar `is not None` en vez de `or` para no perder funding == 0.0
        rate = funding.get("fundingRate")
        if rate is None:
            rate = funding.get("lastFundingRate")
        result = round(float(rate) * 100, 4) if rate is not None else None
    except Exception:
        pass
    _evict(_FUNDING_CACHE)
    _FUNDING_CACHE[symbol] = {"data": result, "ts": ahora}
    return result

def get_open_interest(symbol="BTC/USDT"):
    """OI actual + cambio % en 4H y 24H — cacheado 60s"""
    ts_ahora = time.time()
    if symbol in _OI_CACHE and (ts_ahora - _OI_CACHE[symbol]["ts"]) < _DATA_TTL:
        return _OI_CACHE[symbol]["data"]
    result = {"valor": None, "cambio_4h": None, "cambio_24h": None}
    try:
        symbol_fut = symbol + ":USDT"  # Bybit linear: BTC/USDT:USDT
        oi_hist    = exchange_fut.fetch_open_interest_history(symbol_fut, "1h", limit=25)

        if oi_hist and len(oi_hist) >= 5:
            def oi_val(entry):
                return float(entry.get("openInterestAmount") or entry.get("openInterest") or 0)

            oi_actual = oi_val(oi_hist[-1])
            hace4h    = oi_val(oi_hist[-5])
            hace24h   = oi_val(oi_hist[0])

            cambio_4h  = round((oi_actual - hace4h)  / hace4h  * 100, 2) if hace4h  > 0 else 0
            cambio_24h = round((oi_actual - hace24h) / hace24h * 100, 2) if hace24h > 0 else 0

            result = {
                "valor":      round(oi_actual, 2),
                "cambio_4h":  cambio_4h,
                "cambio_24h": cambio_24h,
            }
    except Exception:
        pass
    _evict(_OI_CACHE)
    _OI_CACHE[symbol] = {"data": result, "ts": ts_ahora}
    return result

def get_long_short_ratio(symbol: str = "BTC/USDT") -> dict:
    """
    Long/Short ratio de cuentas en Binance Futures.
    Ratio > 1 = más longs que shorts.
    Cacheado 60s.
    """
    ahora = time.time()
    if symbol in _LS_CACHE and (ahora - _LS_CACHE[symbol]["ts"]) < _DATA_TTL:
        return _LS_CACHE[symbol]["data"]
    result = {"ratio": None, "long_pct": None, "short_pct": None,
              "lectura": "Sin datos", "error": None}
    try:
        sym_clean = symbol.replace("/", "")
        url = (
            f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
            f"?symbol={sym_clean}&period=5m&limit=1"
        )
        r = _requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        if data:
            entry     = data[0] if isinstance(data, list) else data
            ratio     = float(entry["longShortRatio"])
            long_pct  = float(entry["longAccount"]) * 100
            short_pct = float(entry["shortAccount"]) * 100
            result = {
                "ratio":     round(ratio, 3),
                "long_pct":  round(long_pct, 1),
                "short_pct": round(short_pct, 1),
                "lectura":   _interpretar_ls(ratio, long_pct),
                "error":     None,
            }
    except Exception as e:
        result["error"] = str(e)
    _evict(_LS_CACHE)
    _LS_CACHE[symbol] = {"data": result, "ts": ahora}
    return result


def _interpretar_ls(ratio: float, long_pct: float) -> str:
    if ratio is None:
        return "Sin datos"
    if long_pct >= 70:
        return f"L/S {ratio:.2f} — CROWDED LONG ({long_pct:.1f}% longs) — trampa bajista 🔴"
    elif long_pct >= 60:
        return f"L/S {ratio:.2f} — Long-biased ({long_pct:.1f}% longs) — cautela en largos ⚠️"
    elif long_pct <= 30:
        return f"L/S {ratio:.2f} — CROWDED SHORT ({long_pct:.1f}% longs) — trampa alcista 🟢"
    elif long_pct <= 40:
        return f"L/S {ratio:.2f} — Short-biased ({long_pct:.1f}% longs) — sesgo alcista ⚠️"
    else:
        return f"L/S {ratio:.2f} — Equilibrado ({long_pct:.1f}% longs) ⚪"


def get_funding_historia(symbol: str = "BTC/USDT", n: int = 4) -> dict:
    """
    Últimos N funding rates (cada 8h) para ver tendencia.
    """
    try:
        sym_clean = symbol.replace("/", "")
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym_clean}&limit={n}"
        r = _requests.get(url, timeout=8)
        r.raise_for_status()
        datos = r.json()
        rates = [round(float(d["fundingRate"]) * 100, 4)
                 for d in datos if d.get("fundingRate") is not None]
        if len(rates) < 2:
            return {"rates": rates, "tendencia": "sin datos"}
        if rates[-1] > rates[0] + 0.0005:
            tendencia = "subiendo"
        elif rates[-1] < rates[0] - 0.0005:
            tendencia = "bajando"
        else:
            tendencia = "estable"
        return {"rates": rates, "tendencia": tendencia}
    except Exception:
        return {"rates": [], "tendencia": "sin datos"}


# Cache para percentil de funding (se actualiza cada 6h)
_FUNDING_PERCENTIL_CACHE: dict = {}
_FUNDING_PERCENTIL_TTL = 21600  # 6 horas


def get_funding_percentil(symbol: str = "BTC/USDT", n_dias: int = 90) -> dict:
    """
    Percentil del funding rate actual vs histórico.
    Binance devuelve ~3 rates/día (cada 8h) → 90 días ≈ 270 muestras.

    Campos de retorno (todos los callers soportados):
        current / actual   float   rate actual (ya en %, ej. 0.0100)
        percentil_abs      float   % de registros con abs(rate) MENOR al actual
        percentil_dir      float   % de registros con rate MENOR al actual (direccional)
        percentil          float   alias de percentil_dir
        mean / media       float   media histórica
        std                float   desviación estándar
        n / n_datos        int     muestras usadas
        p5/p25/p75/p95     float   percentiles históricos de referencia
        lectura            str     interpretación human-readable
    """
    ahora = time.time()
    if symbol in _FUNDING_PERCENTIL_CACHE:
        cached = _FUNDING_PERCENTIL_CACHE[symbol]
        if (ahora - cached["ts"]) < _FUNDING_PERCENTIL_TTL:
            return cached["data"]

    _empty = {
        "percentil_abs": None, "percentil_dir": None,
        "current": None,       "actual": None,
        "percentil": None,     "mean": None,
        "media": None,         "std": None,
        "n": 0,                "n_datos": 0,
        "p5": None,            "p25": None,
        "p75": None,           "p95": None,
        "lectura": "Sin datos","error": None,
    }
    try:
        sym_clean = symbol.replace("/", "")
        limit = min(max(n_dias * 3, 500), 1000)  # al menos 500 datos (~166D)
        url = (
            f"https://fapi.binance.com/fapi/v1/fundingRate"
            f"?symbol={sym_clean}&limit={limit}"
        )
        r = _requests.get(url, timeout=10)
        r.raise_for_status()
        datos = r.json()

        rates_raw = [
            float(d["fundingRate"]) * 100
            for d in datos
            if d.get("fundingRate") is not None
        ]
        if len(rates_raw) < 10:
            _FUNDING_PERCENTIL_CACHE[symbol] = {"data": _empty, "ts": ahora}
            return _empty

        current = rates_raw[-1]
        hist    = rates_raw[:-1]

        n           = len(hist)
        abs_hist    = [abs(x) for x in hist]
        abs_current = abs(current)
        sorted_hist = sorted(hist)

        pct_abs = round(sum(1 for x in abs_hist if x < abs_current) / n * 100, 1)
        pct_dir = round(sum(1 for x in hist if x < current) / n * 100, 1)
        mean    = round(sum(hist) / n, 5)
        std     = round((sum((x - mean) ** 2 for x in hist) / n) ** 0.5, 5)

        p5  = round(sorted_hist[max(0, int(n * 0.05))], 4)
        p25 = round(sorted_hist[max(0, int(n * 0.25))], 4)
        p75 = round(sorted_hist[min(n-1, int(n * 0.75))], 4)
        p95 = round(sorted_hist[min(n-1, int(n * 0.95))], 4)

        # lectura interpretativa (compatible con _interpretar_funding_percentil)
        if pct_dir >= 95:
            zona = "EXTREMO HISTORICO POSITIVO (p>95) — longs crowded, trampa bajista inminente"
        elif pct_dir >= 75:
            zona = "ZONA ALTA (p75-95) — longs dominantes, cautela en largos"
        elif pct_dir >= 25:
            zona = "ZONA NORMAL (p25-75) — funding dentro del rango histórico habitual"
        elif pct_dir >= 5:
            zona = "ZONA BAJA (p5-25) — shorts dominantes, sesgo alcista"
        else:
            zona = "EXTREMO HISTORICO NEGATIVO (p<5) — shorts crowded, trampa alcista inminente"
        lectura = f"{current:+.4f}% | Percentil {pct_dir}% histórico | {zona}"

        result = {
            "percentil_abs": pct_abs, "percentil_dir": pct_dir,
            "current":  round(current, 5), "actual":  round(current, 5),
            "percentil": pct_dir,
            "mean":  mean,  "media": mean,
            "std":   std,
            "n":     n,     "n_datos": n,
            "p5": p5, "p25": p25, "p75": p75, "p95": p95,
            "lectura": lectura,
            "error": None,
        }
    except Exception as e:
        logger.debug(f"get_funding_percentil error: {e}")
        result = {**_empty, "error": str(e)}

    _FUNDING_PERCENTIL_CACHE[symbol] = {"data": result, "ts": ahora}
    return result


# ============================================================
# CONTEXTO COMPLETO PARA TRADEBOT
# ============================================================

def _fmt_obs(obs: list) -> str:
    """Formatea lista de OBs para el resumen de mercado."""
    lineas = []
    for ob in obs:
        lado = "OB BAJ" if ob["tipo"] == "bajista" else "OB ALC"
        lineas.append(
            f"{lado}: ${ob['low']:,.0f}–${ob['high']:,.0f} | dist {ob['distancia_pct']:+.1f}% | origen {ob['inicio']}"
        )
    return "\n".join(lineas)


def _fmt_fvgs(fvgs: list) -> str:
    """Formatea lista de FVGs para el resumen de mercado."""
    lineas = []
    for f in fvgs:
        lado = "FVG BAJ" if f["tipo"] == "bajista" else "FVG ALC"
        lineas.append(
            f"{lado}: ${f['precio_inf']:,.0f}–${f['precio_sup']:,.0f} | dist {f['distancia_pct']:+.1f}% | origen {f['inicio']}"
        )
    return "\n".join(lineas)


def _fmt_eqh_eql(eqh_eql: dict) -> str:
    """Formatea EQH/EQL para el resumen de mercado."""
    lineas = []
    for z in eqh_eql.get("eqh", []):
        lineas.append(
            f"EQH: ${z['precio']:,.0f} | {z['toques']} toques | dist {z['distancia_pct']:+.1f}%"
        )
    for z in eqh_eql.get("eql", []):
        lineas.append(
            f"EQL: ${z['precio']:,.0f} | {z['toques']} toques | dist {z['distancia_pct']:+.1f}%"
        )
    return "\n".join(lineas) if lineas else "Sin EQH/EQL detectados"


def get_contexto_mercado(symbol="BTC/USDT", tf="4h"):
    """
    Arma resumen completo del mercado para TradeBot.
    tf: timeframe del análisis — "15m", "1h", "4h" (default), "1d"
    EMA200 Daily siempre presente como referencia macro.
    """

    # ── Configuración por TF ──────────────────────────────────
    _TF_CONFIG = {
        "15m": {"limite": 200, "rsi_periodo": 14,  "rsi_suavizado": 14, "emas": [5,10,21,50,200]},
        "1h":  {"limite": 200, "rsi_periodo": 21,  "rsi_suavizado": 14, "emas": [5,10,21,50,200]},
        "4h":  {"limite": 220, "rsi_periodo": 62,  "rsi_suavizado": 14, "emas": [5,10,21,50,200]},
        "1d":  {"limite": 210, "rsi_periodo": 14,  "rsi_suavizado": 14, "emas": [20,50,200]},
    }
    cfg = _TF_CONFIG.get(tf, _TF_CONFIG["4h"])
    tf_label = tf.upper()

    precio     = get_precio_actual(symbol)
    velas_tf   = get_velas(symbol, tf, cfg["limite"])
    velas1d    = get_velas(symbol, "1d", 210)

    closes_tf = [v["close"] for v in velas_tf]
    closes1d  = [v["close"] for v in velas1d]
    precio_actual = precio["precio"]

    # ── EMAs del TF solicitado ──
    emas_vals = [calcular_ema(closes_tf, p) for p in cfg["emas"]]
    ema_lines = []
    for p, val in zip(cfg["emas"], emas_vals):
        if val:
            direccion = "↑ sobre" if precio_actual > val else "↓ bajo"
            ema_lines.append(f"EMA {p:<4} ${val:,.2f}  {direccion} EMA{p}")
        else:
            ema_lines.append(f"EMA {p:<4} Insuficientes datos")

    # ── EMA 200 Daily — referencia macro siempre presente ──
    ema200d = calcular_ema(closes1d, 200) if len(closes1d) >= 200 else None
    if ema200d is None:
        tendencia_macro = "SIN DATOS — insuficientes velas diarias"
    elif precio_actual > ema200d:
        tendencia_macro = "ALCISTA"
    else:
        tendencia_macro = "BAJISTA"

    # ── RSI del TF ──
    rsi = calcular_rsi(closes_tf, periodo=cfg["rsi_periodo"], suavizado=cfg["rsi_suavizado"])
    rsi_label = f"RSI {cfg['rsi_periodo']} (SMA {cfg['rsi_suavizado']})"

    # ── ATR 14 del TF ──
    atr = calcular_atr(velas_tf, periodo=14)

    # ── Funding + OI + L/S (siempre futuros, TF-independiente) ──
    funding      = get_funding_rate(symbol)
    funding_perc = get_funding_percentil(symbol)
    funding_hist = get_funding_historia(symbol)
    oi           = get_open_interest(symbol)
    ls           = get_long_short_ratio(symbol)

    # ── CVD con el TF correcto ──
    cvd = get_cvd(symbol, tf=tf)

    # ── Order Blocks + FVG + EQH/EQL ──
    try:
        from analysis.ob_fvg import detect_order_blocks, detect_fvg
        obs  = detect_order_blocks(velas_tf)
        fvgs = detect_fvg(velas_tf)
    except Exception:
        obs  = []
        fvgs = []

    try:
        from analysis.liquidity import detect_eqh_eql
        eqh_eql = detect_eqh_eql(velas_tf)
    except Exception:
        eqh_eql = {"eqh": [], "eql": []}

    # ── Volumen 24H vs promedio 20D — ambos en USD ──
    vols_20d_usd = [v["volumen"] * v["close"] for v in velas1d[-20:] if v.get("volumen") and v.get("close")]
    avg_vol_20d  = sum(vols_20d_usd) / len(vols_20d_usd) if vols_20d_usd else None
    vol_24h      = precio.get("volumen_24h", 0) or 0
    vol_vs_avg   = round(vol_24h / avg_vol_20d * 100, 1) if avg_vol_20d and avg_vol_20d > 0 else None
    vol_tag      = (" — ALTO" if vol_vs_avg and vol_vs_avg > 130
                    else " — BAJO" if vol_vs_avg and vol_vs_avg < 70
                    else " — normal") if vol_vs_avg else ""

    # ── Resumen ──
    resumen = f"""
=== DATOS DE MERCADO EN TIEMPO REAL [{tf_label}] ===
Fecha/Hora: {datetime.now().strftime("%Y-%m-%d %H:%M")}
Par: {symbol} | Timeframe análisis: {tf_label}

━━━ PRECIO ━━━━━━━━━━━━━━━━━━━━━━━━━━━
Actual:      ${precio_actual:,.2f} USDT
{('Cambio 24h:  ' + f"{precio['cambio_24h']:+.2f}%") if precio['cambio_24h'] is not None else 'Cambio 24h:  Sin datos'}
Alto 24h:    ${precio['alto_24h']:,.2f}
Bajo 24h:    ${precio['bajo_24h']:,.2f}
Volumen 24h: ${vol_24h:,.0f} USDT{(" (" + str(vol_vs_avg) + "% del promedio 20D" + vol_tag + ")") if vol_vs_avg else ""}

━━━ TENDENCIA MACRO (Daily) ━━━━━━━━━━
EMA 200 Daily: {"$" + f"{ema200d:,.2f}" if ema200d else "Sin datos"}
Dirección:     {tendencia_macro}

━━━ EMAs — {tf_label} ━━━━━━━━━━━━━━━━━━━━━━
{chr(10).join(ema_lines)}

━━━ {rsi_label} — {tf_label} ━━━━━━━━━
Valor:   {f"{rsi:.2f}" if rsi else "Sin datos"}
Lectura: {interpretar_rsi(rsi)}
Zona:    {"Sobre 60" if rsi and rsi > 60 else "Bajo 40" if rsi and rsi < 40 else "Neutro 40-60"}

━━━ ATR 14 — {tf_label} ━━━━━━━━━━━━━━━━━━
Valor:         {"$" + f"{atr:,.2f}" if atr else "Sin datos"}
SL largo  (1.0×ATR): {"$" + f"{precio_actual - atr:,.2f}" if atr else "—"}
SL largo  (1.5×ATR): {"$" + f"{precio_actual - atr * 1.5:,.2f}" if atr else "—"}
SL corto  (1.0×ATR): {"$" + f"{precio_actual + atr:,.2f}" if atr else "—"}
SL corto  (1.5×ATR): {"$" + f"{precio_actual + atr * 1.5:,.2f}" if atr else "—"}

━━━ FUNDING RATE ━━━━━━━━━━━━━━━━━━━━━
Valor:      {f"{funding:+.4f}%" if funding is not None else "Sin datos"}
Historial:  {" → ".join(f"{r:+.4f}%" for r in funding_hist['rates']) if funding_hist['rates'] else "Sin datos"} ({funding_hist['tendencia']})
Lectura:    {interpretar_funding(funding)}
Percentil:  {funding_perc['lectura']}

━━━ OPEN INTEREST ━━━━━━━━━━━━━━━━━━━━
Valor:      {f"${oi['valor']:,.2f}" if oi['valor'] is not None else "Sin datos"}
Cambio {("1H" if tf == "15m" else "4H"):3}:  {f"{oi['cambio_4h']:+.2f}%" if oi['cambio_4h'] is not None else "Sin datos"}
Cambio {"8H" if tf == "15m" else "24H"}:  {f"{oi['cambio_24h']:+.2f}%" if oi['cambio_24h'] is not None else "Sin datos"}
Lectura:    {interpretar_oi(oi['cambio_4h'], oi['cambio_24h'], precio['cambio_24h'] or 0)}

━━━ LONG/SHORT RATIO ━━━━━━━━━━━━━━━━━
Longs:      {f"{ls['long_pct']:.1f}%" if ls['long_pct'] is not None else "Sin datos"}
Shorts:     {f"{ls['short_pct']:.1f}%" if ls['short_pct'] is not None else "Sin datos"}
Lectura:    {ls['lectura']}

━━━ CVD — {tf_label} ━━━━━━━━━━━━━━━━━━━━━
Delta última vela: {f"{cvd['delta_ultima']:+,.2f} BTC" if cvd['delta_ultima'] is not None else "Sin datos"}
Bias {tf_label}:   {cvd['cvd_bias'].upper() if cvd['cvd_bias'] else "Sin datos"}
Divergencia:       {"⚠️ SÍ — precio y CVD no confirman" if cvd['divergencia'] else "No"}

━━━ ORDER BLOCKS — {tf_label} ━━━━━━━━━━━
{_fmt_obs(obs) if obs else "Sin OBs activos detectados"}

━━━ FAIR VALUE GAPS — {tf_label} ━━━━━━━━
{_fmt_fvgs(fvgs) if fvgs else "Sin FVGs abiertos detectados"}

━━━ EQUAL HIGHS / EQUAL LOWS — {tf_label} ━
{_fmt_eqh_eql(eqh_eql)}

━━━ ÚLTIMAS 5 VELAS — {tf_label} ━━━━━━━━━
{"Fecha":<18} {"Open":>10} {"High":>10} {"Low":>10} {"Close":>10}
{"-"*62}"""

    for v in velas_tf[-5:]:
        color = "+" if v["close"] > v["open"] else "-"
        resumen += f"\n[{color}] {v['fecha']:<16} ${v['open']:>9,.0f} ${v['high']:>9,.0f} ${v['low']:>9,.0f} ${v['close']:>9,.0f}"

    # Velas daily solo si el TF analizado NO es daily (evitar duplicar)
    if tf != "1d":
        resumen += f"""

━━━ ÚLTIMAS 5 VELAS DAILY (macro) ━━━━
{"Fecha":<18} {"Open":>10} {"High":>10} {"Low":>10} {"Close":>10}
{"-"*62}"""
        for v in velas1d[-5:]:
            color = "+" if v["close"] > v["open"] else "-"
            resumen += f"\n[{color}] {v['fecha']:<16} ${v['open']:>9,.0f} ${v['high']:>9,.0f} ${v['low']:>9,.0f} ${v['close']:>9,.0f}"

    resumen += "\n=== FIN DATOS DE MERCADO ==="

    # ── DXY + BTC.D macro ──
    try:
        resumen += "\n" + get_macro_contexto()
    except Exception:
        pass

    return resumen

# ============================================================
# TEST — python binance_data.py
# ============================================================
if __name__ == "__main__":
    print("🔄 Obteniendo datos de Binance...\n")
    print("  → Precio...", end=" ", flush=True)
    p = get_precio_actual()
    print(f"${p['precio']:,.2f}")

    print("  → Funding Rate...", end=" ", flush=True)
    fr = get_funding_rate()
    print(f"{fr}%" if fr is not None else "Sin datos")

    print("  → Open Interest...", end=" ", flush=True)
    oi = get_open_interest()
    print(f"4H: {oi['cambio_4h']}% | 24H: {oi['cambio_24h']}%")

    print("  → Velas + RSI + EMAs...", end=" ", flush=True)
    ctx = get_contexto_mercado()
    print("OK\n")
    print(ctx)
    print("\n✅ Todo funcionando!")
# ============================================================
# MULTI-ACTIVO — BTC, ETH, BNB, SOL
# ============================================================
ACTIVOS = {
    "BTC": "BTC/USDT",
    "ETH": "ETH/USDT",
    "BNB": "BNB/USDT",
    "SOL": "SOL/USDT",
}

def get_resumen_sidebar(symbol="BTC/USDT"):
    """
    Datos compactos para la sidebar:
    precio, cambio, funding, OI — rápido y liviano
    """
    try:
        precio  = get_precio_actual(symbol)
        funding = get_funding_rate(symbol)
        oi      = get_open_interest(symbol)
        velas4h = get_velas(symbol, "4h", 220)
        closes4h = [v["close"] for v in velas4h]
        rsi = calcular_rsi(closes4h, periodo=62, suavizado=14)

        cvd    = get_cvd(symbol, tf="4h")
        whales = get_large_trades(symbol)

        return {
            "symbol":      symbol,
            "precio":      precio["precio"],
            "cambio_24h":  precio["cambio_24h"],
            "alto_24h":    precio["alto_24h"],
            "bajo_24h":    precio["bajo_24h"],
            "funding":     funding,
            "oi_cambio_4h":  oi["cambio_4h"],
            "oi_cambio_24h": oi["cambio_24h"],
            "rsi":         rsi,
            "funding_label": interpretar_funding(funding),
            "oi_label":      interpretar_oi(oi["cambio_4h"], oi["cambio_24h"], precio["cambio_24h"] or 0),
            "rsi_label":     interpretar_rsi(rsi),
            "cvd_bias":      cvd.get("cvd_bias", "neutral"),
            "cvd_divergencia": cvd.get("divergencia", False),
            "cvd_delta_ultima": cvd.get("delta_ultima"),
            "cvd_cambio_pct":  cvd.get("cvd_cambio_pct"),
            "whale_count":   whales.get("whale_count", 0),
            "whale_bias":    whales.get("whale_bias", "neutral"),
            "whale_buy_vol": whales.get("buy_volume", 0),
            "whale_sell_vol": whales.get("sell_volume", 0),
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}

# ============================================================
# RÉGIMEN DE MERCADO — para system prompt dinámico
# ============================================================

def get_regimen_mercado(symbol: str = "BTC/USDT") -> dict:
    """
    Calcula el régimen macro actual del mercado en tiempo real.
    Usado por app_flask.py para inyectar contexto en el system prompt.

    Regímenes posibles:
    - BAJISTA_EXTREMO  : precio << EMA200d + RSI < 40 + EMAs bajistas
    - BAJISTA          : precio < EMA200d
    - RANGO            : precio ±3% de EMA200d
    - ALCISTA          : precio > EMA200d
    - ALCISTA_EXTREMO  : precio >> EMA200d + RSI > 60 + EMAs alcistas
    """
    try:
        precio_data  = get_precio_actual(symbol)
        velas4h      = get_velas(symbol, "4h", 220)
        velas1d      = get_velas(symbol, "1d", 210)
        closes4h     = [v["close"] for v in velas4h]
        closes1d     = [v["close"] for v in velas1d]
        precio        = precio_data["precio"]
        cambio_24h    = precio_data.get("cambio_24h") or 0

        # EMAs 4H
        ema5   = calcular_ema(closes4h, 5)
        ema21  = calcular_ema(closes4h, 21)
        ema50  = calcular_ema(closes4h, 50)
        ema200 = calcular_ema(closes4h, 200)

        # EMA200 Daily — el juez macro
        ema200d = calcular_ema(closes1d, 200) if len(closes1d) >= 200 else None

        # RSI 62 en 4H
        rsi = calcular_rsi(closes4h, periodo=62, suavizado=14)

        # Funding
        funding = get_funding_rate(symbol)

        # OI
        oi = get_open_interest(symbol)

        # ── Determinar régimen ───────────────────────────────
        if ema200d is None:
            regimen = "INDEFINIDO"
            emoji   = "❓"
        else:
            distancia_pct = ((precio - ema200d) / ema200d) * 100

            # EMAs 4H alineadas alcistas = precio sobre ema5, 21, 50
            emas_alcistas = all(
                e is not None and precio > e
                for e in [ema5, ema21, ema50]
            )
            emas_bajistas = all(
                e is not None and precio < e
                for e in [ema5, ema21, ema50]
            )

            if distancia_pct < -3:
                # Precio bajo EMA200d
                if rsi and rsi < 40 and emas_bajistas:
                    regimen = "BAJISTA_EXTREMO"
                    emoji   = "🔴"
                else:
                    regimen = "BAJISTA"
                    emoji   = "📉"
            elif distancia_pct > 3:
                # Precio sobre EMA200d
                if rsi and rsi > 60 and emas_alcistas:
                    regimen = "ALCISTA_EXTREMO"
                    emoji   = "🟢"
                else:
                    regimen = "ALCISTA"
                    emoji   = "📈"
            else:
                regimen = "RANGO"
                emoji   = "↔️"

        # ── Sesgo narrativo para el system prompt ────────────
        sesgo_map = {
            "BAJISTA_EXTREMO": (
                "Downtrend estructural profundo. Precio muy por debajo de EMA200 Daily con momentum bajista confirmado. "
                "Setups alcistas son contratendencia de alto riesgo. Priorizar shorts en rebotes o esperar capitulación."
            ),
            "BAJISTA": (
                "Downtrend estructural activo. Precio bajo EMA200 Daily — mercado bajista macro. "
                "Rebotes técnicos son oportunidades de short, no de compra. Cualquier long es contratendencia."
            ),
            "RANGO": (
                "Mercado en zona de decisión. Precio cerca de EMA200 Daily — sin dirección macro clara. "
                "Operar rangos con targets ajustados. Esperar ruptura confirmada antes de sesgar bias."
            ),
            "ALCISTA": (
                "Uptrend estructural activo. Precio sobre EMA200 Daily — mercado alcista macro. "
                "Pullbacks a EMAs son oportunidades de compra. Shorts son contratendencia."
            ),
            "ALCISTA_EXTREMO": (
                "Uptrend estructural fuerte con momentum confirmado. Precio sobre EMA200 Daily, EMAs alineadas, RSI en zona alta. "
                "Mercado en fase de expansión. Cuidado con sobrecompra — gestionar SL en movimiento."
            ),
            "INDEFINIDO": (
                "Datos de EMA200 Daily insuficientes. Operar con precaución extra — no hay contexto macro confirmado."
            ),
        }

        sesgo_texto = sesgo_map.get(regimen, "Sin sesgo disponible.")

        # ── Construir bloque para system prompt ─────────────
        ema200d_str = f"${ema200d:,.2f}" if ema200d else "Sin datos"
        distancia_str = f"{distancia_pct:+.1f}%" if ema200d else "N/A"
        rsi_str     = f"{rsi:.1f}" if rsi else "Sin datos"
        funding_str = f"{funding:+.4f}%" if funding is not None else "Sin datos"

        bloque_contexto = f"""
━━━ CONTEXTO MACRO EN TIEMPO REAL ({datetime.now().strftime("%Y-%m-%d %H:%M")}) ━━━
Activo de referencia: {symbol}
Régimen: {regimen} {emoji}
EMA200 Daily: {ema200d_str} | Precio actual: ${precio:,.2f} ({distancia_str})
RSI 62 (4H): {rsi_str}
Funding Rate: {funding_str}
OI 4H: {f"{oi['cambio_4h']:+.2f}%" if oi.get('cambio_4h') is not None else "Sin datos"}
Sesgo macro: {sesgo_texto}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

        # ── DXY + BTC.D — agregar al bloque_contexto ──
        try:
            macro_extra = get_macro_contexto()
            bloque_contexto = bloque_contexto.strip() + "\n" + macro_extra
        except Exception:
            bloque_contexto = bloque_contexto.strip()

        # ── HMM — régimen probabilístico ────────────────────────────────
        hmm_data = {}
        try:
            from hmm_regime import get_regimen_hmm, hmm_bloque_contexto
            hmm_data = get_regimen_hmm(symbol)
            if hmm_data.get("estado") != "INDEFINIDO":
                bloque_contexto = bloque_contexto.strip() + "\n" + hmm_bloque_contexto(hmm_data)
        except Exception as _hmm_err:
            logger.debug(f"HMM no disponible: {_hmm_err}")

        # ── On-Chain (Glassnode) — datos diarios ────────────────────────
        try:
            from onchain import get_onchain_contexto
            oc_bloque = get_onchain_contexto(precio_actual=precio)
            if oc_bloque:
                bloque_contexto = bloque_contexto.strip() + "\n" + oc_bloque
            else:
                logger.warning("On-chain: contexto vacío (API externa sin datos?)")
        except Exception as _oc_err:
            logger.warning(f"On-chain no disponible: {_oc_err}")

        # ── Volatility Surface (Deribit) ─────────────────────────────────
        try:
            from deribit_vol import get_vol_contexto
            vol_bloque = get_vol_contexto(spot=precio)
            if vol_bloque:
                bloque_contexto = bloque_contexto.strip() + "\n" + vol_bloque
        except Exception as _vol_err:
            logger.debug(f"Vol surface no disponible: {_vol_err}")

        # ── Correlation Matrix (SPX + Gold) ──────────────────────────────
        try:
            from correlation import get_corr_contexto
            corr_bloque = get_corr_contexto()
            if corr_bloque:
                bloque_contexto = bloque_contexto.strip() + "\n" + corr_bloque
            else:
                logger.warning("Correlation: contexto vacío (yfinance sin datos?)")
        except Exception as _corr_err:
            logger.warning(f"Correlation no disponible: {_corr_err}")

        return {
            "regimen":         regimen,
            "emoji":           emoji,
            "sesgo":           sesgo_texto,
            "bloque_contexto": bloque_contexto,
            "precio":          precio,
            "ema200d":         ema200d,
            "rsi":             rsi,
            "funding":         funding,
            "hmm":             hmm_data,
            "error":           None,
        }

    except Exception as e:
        return {
            "regimen":         "INDEFINIDO",
            "emoji":           "❓",
            "sesgo":           "Error obteniendo datos de mercado.",
            "bloque_contexto": "",
            "error":           str(e),
        }


# ============================================================
# FASE 1 — UPGRADES QUANT
# ============================================================

def calcular_atr(velas: list, periodo: int = 14) -> float | None:
    """
    Average True Range — volatilidad real del mercado.

    True Range = max(H-L, |H-Cp|, |L-Cp|)
    ATR = EMA(True Range, periodo)

    Uso:
        SL sugerido = entrada ± (1.5 × ATR)
        ATR alto    → mercado volátil → SL más amplio
        ATR bajo    → mercado comprimido → posible breakout
    """
    try:
        if len(velas) < periodo + 1:
            return None
        trs = []
        for i in range(1, len(velas)):
            h  = velas[i]["high"]
            l  = velas[i]["low"]
            cp = velas[i-1]["close"]
            tr = max(h - l, abs(h - cp), abs(l - cp))
            trs.append(tr)
        # EMA del True Range
        atr = calcular_ema(trs, periodo)
        return round(atr, 2) if atr else None
    except Exception:
        return None


def calcular_conviccion(
    precio: float,
    ema200d: float | None,
    rsi: float | None,
    emas_4h: list,
    funding: float | None,
    oi_cambio_4h: float | None,
) -> dict:
    """
    Score de convicción institucional 0-100.

    Convicción = Σ(peso_i × señal_normalizada_i) × 100

    Pesos (suman 1.0):
        EMA200 Daily  = 0.30  ← macro es lo más importante
        RSI 62        = 0.20
        EMAs 4H       = 0.20
        Funding Rate  = 0.15
        OI 4H         = 0.15

    Señales normalizadas [0, 1]:
        0.0 = máxima convicción BAJISTA
        0.5 = neutral
        1.0 = máxima convicción ALCISTA

    Retorna:
        score      : 0-100 (50 = neutral)
        direccion  : ALCISTA | BAJISTA | NEUTRAL
        conviccion : ALTA | MEDIA | BAJA
        detalle    : dict con contribución de cada factor
    """
    señales = {}

    # ── Factor 1: EMA200 Daily (peso 0.30) ───────────────────
    if ema200d and precio:
        distancia = (precio - ema200d) / ema200d  # [-1, +1] aprox
        # Normalizar: ±15% = extremo, clamp a [-0.15, +0.15]
        dist_clamp = max(-0.15, min(0.15, distancia))
        señal_ema200d = (dist_clamp + 0.15) / 0.30  # → [0, 1]
    else:
        señal_ema200d = 0.5  # neutral si no hay dato
    señales["ema200d"] = {"valor": señal_ema200d, "peso": 0.30}

    # ── Factor 2: RSI 62 (peso 0.20) ─────────────────────────
    if rsi is not None:
        # RSI 0-100 → normalizar: 30=bajista_extremo, 70=alcista_extremo
        rsi_clamp = max(20, min(80, rsi))
        señal_rsi = (rsi_clamp - 20) / 60  # → [0, 1]
    else:
        señal_rsi = 0.5
    señales["rsi"] = {"valor": señal_rsi, "peso": 0.20}

    # ── Factor 3: EMAs 4H alineadas (peso 0.20) ───────────────
    if emas_4h and precio:
        validas = [e for e in emas_4h if e is not None]
        if validas:
            sobre = sum(1 for e in validas if precio > e)
            señal_emas = sobre / len(validas)  # → [0, 1]
        else:
            señal_emas = 0.5
    else:
        señal_emas = 0.5
    señales["emas_4h"] = {"valor": señal_emas, "peso": 0.20}

    # ── Factor 4: Funding Rate (peso 0.15) ────────────────────
    # Funding negativo → retail short → señal ALCISTA
    # Funding positivo → retail long → señal BAJISTA
    if funding is not None:
        # Clamp a [-0.15%, +0.15%]
        f_clamp = max(-0.15, min(0.15, funding))
        # Invertir: funding negativo = alcista (señal alta)
        señal_funding = (-f_clamp + 0.15) / 0.30  # → [0, 1]
    else:
        señal_funding = 0.5
    señales["funding"] = {"valor": señal_funding, "peso": 0.15}

    # ── Factor 5: OI 4H (peso 0.15) ──────────────────────────
    # OI↑ = capital entrando (confirma tendencia)
    # OI↓ = capital saliendo (debilita tendencia)
    if oi_cambio_4h is not None:
        oi_clamp = max(-5.0, min(5.0, oi_cambio_4h))
        señal_oi = (oi_clamp + 5.0) / 10.0  # → [0, 1]
    else:
        señal_oi = 0.5
    señales["oi_4h"] = {"valor": señal_oi, "peso": 0.15}

    # ── Score final ponderado ─────────────────────────────────
    score_raw = sum(s["valor"] * s["peso"] for s in señales.values())
    score = round(score_raw * 100)  # 0-100

    # ── Interpretación ────────────────────────────────────────
    desviacion = abs(score - 50)  # 0=neutral, 50=extremo

    if score >= 65:
        direccion = "ALCISTA"
    elif score <= 35:
        direccion = "BAJISTA"
    else:
        direccion = "NEUTRAL"

    if desviacion >= 30:
        conviccion = "ALTA"
    elif desviacion >= 15:
        conviccion = "MEDIA"
    else:
        conviccion = "BAJA"

    return {
        "score":      score,
        "direccion":  direccion,
        "conviccion": conviccion,
        "señales":    señales,
        "label":      f"{direccion} — Convicción {conviccion} ({score}/100)",
    }


def calcular_p_min_breakeven(rr: float) -> float:
    """
    Probabilidad mínima de éxito para ser matemáticamente rentable.

    Fórmula de Jane Street / Kelly:
        P_min = 1 / (1 + R:R)

    Ejemplos:
        R:R 1:1 → P_min = 50.0%  (necesitas ganar 1 de cada 2)
        R:R 2:1 → P_min = 33.3%  (necesitas ganar 1 de cada 3)
        R:R 3:1 → P_min = 25.0%  (necesitas ganar 1 de cada 4)

    Si tu win rate histórico > P_min → estrategia matemáticamente viable
    """
    if rr <= 0:
        return 100.0
    return round(100 / (1 + rr), 1)


def calcular_kelly(p_win: float, rr: float, vol_percentil: float = None) -> dict:
    """
    Criterio de Kelly ajustado por volatilidad histórica.

    Kelly% = (P_win × RR - P_loss) / RR
    Kelly fraccional base = Kelly% / 2

    Ajuste por percentil de volatilidad realizada (Bridgewater risk parity):
        BAJA  (percentil < 25)  → kelly_frac × 1.0  (sin cambio)
        NORMAL(percentil 25-75) → kelly_frac × 0.75 (reducción moderada)
        ALTA  (percentil > 75)  → kelly_frac × 0.50 (reducción máxima)
    """
    try:
        p_loss = 1 - p_win
        kelly_decimal = (p_win * rr - p_loss) / rr
        kelly_pct  = round(kelly_decimal * 100, 1)
        kelly_frac = round(kelly_pct / 2, 1)

        # Ajuste por volatilidad
        vol_factor = 1.0
        vol_label  = None
        if vol_percentil is not None:
            if vol_percentil > 75:
                vol_factor = 0.50
                vol_label  = "ALTA"
            elif vol_percentil > 25:
                vol_factor = 0.75
                vol_label  = "NORMAL"
            else:
                vol_factor = 1.0
                vol_label  = "BAJA"

        kelly_frac_ajustado = round(kelly_frac * vol_factor, 1) if kelly_pct > 0 else 0.0

        if kelly_pct <= 0:
            interpretacion = "Edge negativo — esta estrategia pierde dinero a largo plazo."
        elif vol_label and vol_factor < 1.0:
            interpretacion = (
                f"Kelly ajustado por volatilidad {vol_label} (percentil {round(vol_percentil)}): "
                f"{kelly_frac_ajustado}% del capital por trade "
                f"(base {kelly_frac}% reducido a {int(vol_factor*100)}%)."
            )
        elif kelly_frac_ajustado <= 1:
            interpretacion = f"Kelly fraccional: {kelly_frac_ajustado}% del capital por trade. Edge muy pequeño."
        elif kelly_frac_ajustado <= 5:
            interpretacion = f"Kelly fraccional: {kelly_frac_ajustado}% del capital por trade. Edge moderado."
        else:
            interpretacion = f"Kelly fraccional: {kelly_frac_ajustado}% del capital por trade. Edge sólido — no exceder."

        return {
            "kelly_pct":            kelly_pct,
            "kelly_fraccional":     kelly_frac,
            "kelly_frac_ajustado":  kelly_frac_ajustado,
            "vol_factor":           vol_factor,
            "vol_label":            vol_label,
            "interpretacion":       interpretacion,
            "viable":               kelly_pct > 0,
        }
    except Exception:
        return {
            "kelly_pct": 0, "kelly_fraccional": 0,
            "kelly_frac_ajustado": 0, "vol_factor": 1.0,
            "vol_label": None, "interpretacion": "Error en cálculo", "viable": False
        }


# ============================================================
# FASE 3 — DXY + BTC DOMINANCE EN TIEMPO REAL
# ============================================================

_DXY_CACHE   = {"valor": None, "ts": 0}
_BTCD_CACHE  = {"valor": None, "ts": 0}
_CACHE_TTL   = 600  # 10 minutos

def get_dxy() -> dict:
    """
    DXY (US Dollar Index) via yfinance — gratis, sin API key.
    Ticker: DX-Y.NYB
    Cacheado 10 minutos para no spamear.
    """
    ahora = time.time()
    if _DXY_CACHE["valor"] and (ahora - _DXY_CACHE["ts"]) < _CACHE_TTL:
        return _DXY_CACHE["valor"]
    try:
        import yfinance as yf
        ticker = yf.Ticker("DX-Y.NYB")
        hist   = ticker.history(period="5d")
        if hist.empty or len(hist) < 2:
            raise ValueError("Sin datos de yfinance para DXY")
        dxy_ayer = float(hist["Close"].iloc[-2])
        dxy_hoy  = float(hist["Close"].iloc[-1])
        cambio   = round(((dxy_hoy - dxy_ayer) / dxy_ayer) * 100, 2)
        resultado = {
            "valor":   round(dxy_hoy, 2),
            "cambio":  cambio,
            "lectura": _interpretar_dxy(dxy_hoy, cambio),
            "error":   None,
        }
        _DXY_CACHE["valor"] = resultado
        _DXY_CACHE["ts"]    = ahora
        return resultado
    except Exception as e:
        return {"valor": None, "cambio": None, "lectura": "Sin datos", "error": str(e)}


def _interpretar_dxy(valor: float, cambio: float) -> str:
    """Interpreta el DXY en funcion de su nivel y movimiento."""
    if valor is None:
        return "Sin datos"
    # Niveles historicos de referencia
    if valor > 106:
        nivel = "DXY FUERTE (>106) — presion bajista sobre BTC"
    elif valor > 103:
        nivel = "DXY ELEVADO (103-106) — viento en contra para crypto"
    elif valor > 100:
        nivel = "DXY NEUTRAL-ALTO (100-103) — sin sesgo claro"
    elif valor > 97:
        nivel = "DXY MODERADO (97-100) — ambiente constructivo para risk-on"
    else:
        nivel = "DXY DEBIL (<97) — viento a favor para BTC"

    if cambio is not None:
        if cambio >= 0.3:
            mov = f"subiendo {cambio:+.2f}% hoy — presion bajista sobre crypto"
        elif cambio <= -0.3:
            mov = f"cayendo {cambio:+.2f}% hoy — impulso alcista para crypto"
        else:
            mov = f"estable ({cambio:+.2f}%)"
        return f"{nivel} | {mov}"
    return nivel


def get_btc_dominance() -> dict:
    """
    BTC Dominance via CoinGecko API publica (sin key).
    BTC.D = % del market cap total que representa Bitcoin.
    Cacheado 10 minutos.

    Interpretacion institucional:
        BTC.D subiendo → capital fluyendo a BTC (risk-off en crypto)
        BTC.D bajando  → capital rotando a altcoins (alt season)
        BTC.D > 55%    → dominancia alta, altcoins en riesgo
        BTC.D < 45%    → rotacion a alts, posible alt season
    """
    ahora = time.time()
    if _BTCD_CACHE["valor"] and (ahora - _BTCD_CACHE["ts"]) < _CACHE_TTL:
        return _BTCD_CACHE["valor"]
    try:
        url = "https://api.coingecko.com/api/v3/global"
        r   = _requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()["data"]
        btcd = round(data["market_cap_percentage"]["btc"], 2)
        resultado = {
            "valor":   btcd,
            "lectura": _interpretar_btcd(btcd),
            "error":   None,
        }
        _BTCD_CACHE["valor"] = resultado
        _BTCD_CACHE["ts"]    = ahora
        return resultado
    except Exception as e:
        return {"valor": None, "lectura": "Sin datos", "error": str(e)}


def _interpretar_btcd(btcd: float) -> str:
    """Interpreta BTC Dominance en clave institucional."""
    if btcd is None:
        return "Sin datos"
    if btcd >= 60:
        return f"BTC.D {btcd}% — Dominancia extrema. Alts en riesgo, capital refugiado en BTC"
    elif btcd >= 55:
        return f"BTC.D {btcd}% — Dominancia alta. Mercado risk-off dentro de crypto"
    elif btcd >= 50:
        return f"BTC.D {btcd}% — Dominancia moderada. BTC lidera, alts siguen"
    elif btcd >= 45:
        return f"BTC.D {btcd}% — Zona de transicion. Posible rotacion a altcoins"
    else:
        return f"BTC.D {btcd}% — Dominancia baja. Alt season activa o en formacion"


# ============================================================
# FEAR & GREED INDEX — Alternative.me (gratis, sin API key)
# ============================================================

_FNG_CACHE = {"valor": None, "ts": 0}
_FNG_TTL   = 3600   # 1 hora (se actualiza diario)

def get_fear_greed() -> dict:
    """
    Fear & Greed Index via Alternative.me.
    Escala 0-100: 0 = Miedo Extremo, 100 = Codicia Extrema.
    Trae los ultimos 30 dias: hist_7d (primeros 7) y hist_30d (todos).
    """
    ahora = time.time()
    if _FNG_CACHE["valor"] and (ahora - _FNG_CACHE["ts"]) < _FNG_TTL:
        return _FNG_CACHE["valor"]
    try:
        url = "https://api.alternative.me/fng/?limit=30"
        r   = _requests.get(url, timeout=8)
        r.raise_for_status()
        data    = r.json()["data"]
        actual  = data[0]
        valor   = int(actual["value"])
        clasif  = actual["value_classification"]
        hist_all  = [int(d["value"]) for d in data]
        hist7     = hist_all[:7]
        tendencia = hist7[0] - hist7[-1]   # positivo = mejorando vs hace 7D
        resultado = {
            "valor":         valor,
            "clasificacion": clasif,
            "tendencia_7d":  tendencia,
            "hist_7d":       hist7,
            "hist_30d":      hist_all,      # newest-first
            "lectura":       _interpretar_fng(valor, tendencia),
            "error":         None,
        }
        _FNG_CACHE["valor"] = resultado
        _FNG_CACHE["ts"]    = ahora
        return resultado
    except Exception as e:
        return {"valor": None, "clasificacion": None, "lectura": "Sin datos", "error": str(e)}


def _interpretar_fng(valor: int, tendencia: int) -> str:
    if valor is None:
        return "Sin datos"
    if valor <= 20:
        zona = "MIEDO EXTREMO — acumulacion institucional historica en estas zonas"
    elif valor <= 40:
        zona = "MIEDO — retail saliendo, smart money acumulando"
    elif valor <= 60:
        zona = "NEUTRO — sin sesgo de sentimiento claro"
    elif valor <= 80:
        zona = "CODICIA — retail entrando, reducir exposicion"
    else:
        zona = "CODICIA EXTREMA — zona de distribucion institucional, tops forman aqui"

    if tendencia > 10:
        trend_str = f"mejorando rapido (+{tendencia} pts en 7D)"
    elif tendencia > 3:
        trend_str = f"recuperando (+{tendencia} pts en 7D)"
    elif tendencia < -10:
        trend_str = f"deteriorando rapido ({tendencia} pts en 7D)"
    elif tendencia < -3:
        trend_str = f"cayendo ({tendencia} pts en 7D)"
    else:
        trend_str = "estable"

    return f"{valor}/100 — {zona} | Tendencia: {trend_str}"


# ============================================================
# CORRELACION ROLLING BTC-DXY — 30D y 90D (IMPORTANTISIMO)
# ============================================================

_CORR_CACHE = {"valor": None, "ts": 0}
_CORR_TTL   = 3600   # 1 hora

def get_btc_dxy_correlation() -> dict:
    """
    Pearson rolling correlation entre BTC y DXY (30D y 90D).

    Interpretacion:
        < -0.6  : correlacion inversa fuerte (relacion normal — DXY sube = BTC cae)
        -0.6/-0.3: correlacion inversa moderada
        -0.3/0.3 : decorrelacion — se mueven independientemente (señal de cambio)
        > 0.3   : correlacion positiva — raro, señal de risk-off generalizado
        > 0.6   : crisis total — todo cae junto

    ALERTA cuando corr_30d sube bruscamente vs corr_90d (correlacion girando positiva).
    """
    ahora = time.time()
    if _CORR_CACHE["valor"] and (ahora - _CORR_CACHE["ts"]) < _CORR_TTL:
        return _CORR_CACHE["valor"]
    try:
        import yfinance as yf
        import bisect
        dxy_ticker = yf.Ticker("DX-Y.NYB")
        # 150d para tener margen — DXY solo tiene ~107 trading days en 150 calendarios
        dxy_hist   = dxy_ticker.history(period="150d")
        if dxy_hist.empty or len(dxy_hist) < 30:
            raise ValueError("Sin datos DXY para correlacion")

        # BTC daily closes — 130 velas para cubrir 90D con margen
        btc_velas = get_velas("BTC/USDT", "1d", 130)

        # Mapa DXY: solo días de trading (lunes-viernes)
        dxy_map = {}
        for idx, row in dxy_hist.iterrows():
            d = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]
            dxy_map[d] = float(row["Close"])

        btc_map = {v["fecha"][:10]: v["close"] for v in btc_velas}

        # Forward-fill DXY para fines de semana y festivos:
        # cada fecha BTC busca el último precio DXY disponible (bisect O(n log n))
        dxy_fechas_ord = sorted(dxy_map.keys())

        btc_s = []
        dxy_s = []
        for btc_fecha in sorted(btc_map.keys()):
            idx = bisect.bisect_right(dxy_fechas_ord, btc_fecha) - 1
            if idx >= 0:
                btc_s.append(btc_map[btc_fecha])
                dxy_s.append(dxy_map[dxy_fechas_ord[idx]])

        if len(btc_s) < 30:
            raise ValueError("Datos alineados insuficientes para correlacion")

        def pearson(x, y):
            n = len(x)
            if n < 5:
                return None
            mx = sum(x) / n
            my = sum(y) / n
            num  = sum((x[i]-mx)*(y[i]-my) for i in range(n))
            den  = (sum((x[i]-mx)**2 for i in range(n)) * sum((y[i]-my)**2 for i in range(n))) ** 0.5
            return round(num / den, 3) if den else None

        n_datos  = len(btc_s)
        corr_30d = pearson(btc_s[-30:], dxy_s[-30:]) if n_datos >= 30 else None
        corr_90d = pearson(btc_s[-90:], dxy_s[-90:]) if n_datos >= 90 else None

        resultado = {
            "corr_30d":  corr_30d,
            "corr_90d":  corr_90d,
            "dias_datos": n_datos,
            "lectura":   _interpretar_correlacion(corr_30d, corr_90d),
            "error":     None,
        }
        _CORR_CACHE["valor"] = resultado
        _CORR_CACHE["ts"]    = ahora
        return resultado
    except Exception as e:
        return {"corr_30d": None, "corr_90d": None, "lectura": "Sin datos", "error": str(e)}


def _interpretar_correlacion(c30: float, c90: float) -> str:
    if c30 is None:
        return "Sin datos"
    if c30 < -0.6:
        estado = "INVERSA FUERTE (normal) — DXY sube = BTC cae"
    elif c30 < -0.3:
        estado = "INVERSA MODERADA — correlacion negativa activa"
    elif c30 < 0.3:
        estado = "DECORRELACION — BTC y DXY independientes (señal de regimen cambiando)"
    elif c30 < 0.6:
        estado = "CORRELACION POSITIVA — inusual, risk-off generalizado"
    else:
        estado = "CORRELACION POSITIVA FUERTE — crisis total, todo cae junto"

    alerta = ""
    if c90 is not None:
        delta = round(c30 - c90, 3)
        if delta > 0.25:
            alerta = " | ⚠️ ALERTA: correlacion girando positiva vs historico 90D"
        elif delta < -0.25:
            alerta = " | correlacion volviendo a inversa (normalizacion)"

    c90_str = f"{c90:+.3f}" if c90 is not None else "N/A"
    return f"30D: {c30:+.3f} | 90D: {c90_str} — {estado}{alerta}"


# ============================================================
# PERCENTIL FUNDING RATE HISTORICO
# ============================================================

# _FUND_PERC_CACHE y get_funding_percentil duplicada eliminados — fusionados en línea ~314


# ============================================================
# L2 ORDER BOOK — LIQUIDITY WALLS + IMBALANCE
# ============================================================

_L2_CACHE:  dict = {}
_L2_TTL     = 60   # 60s — order book cambia rápido

_WALL_THRESHOLD_USD = 200_000      # $200K — mínimo para llamarse "pared"
_WALL_TOP_N         = 6            # top 6 bids + top 6 asks


def get_l2_liquidity(symbol: str = "BTC/USDT") -> dict:
    """
    Snapshot L2 del order book de Binance Futures.
    Calcula: paredes grandes, imbalance bid/ask, soporte/resistencia más cercana.

    Endpoint: GET fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=500
    Cacheado 60s.

    Retorna:
        imbalance_pct   : % del volumen total que es bid (50 = neutral)
        imbalance_bias  : "bid" | "ask" | "neutral"
        top_bids        : lista [{price, usd, pct_of_book}] — paredes compradoras
        top_asks        : lista [{price, usd, pct_of_book}] — paredes vendedoras
        nearest_bid_wall: pared bid más grande dentro del 2% del precio
        nearest_ask_wall: pared ask más grande dentro del 2% del precio
        bid_depth_1pct  : USD total en bids dentro del 1% del precio
        ask_depth_1pct  : USD total en asks dentro del 1% del precio
        bid_depth_2pct  : USD total en bids dentro del 2% del precio
        ask_depth_2pct  : USD total en asks dentro del 2% del precio
    """
    ahora = time.time()
    if symbol in _L2_CACHE and (ahora - _L2_CACHE[symbol]["ts"]) < _L2_TTL:
        return _L2_CACHE[symbol]["data"]

    result = {
        "imbalance_pct": 50.0, "imbalance_bias": "neutral",
        "top_bids": [], "top_asks": [],
        "nearest_bid_wall": None, "nearest_ask_wall": None,
        "bid_depth_1pct": 0.0, "ask_depth_1pct": 0.0,
        "bid_depth_2pct": 0.0, "ask_depth_2pct": 0.0,
        "error": None,
    }
    try:
        sym_clean = symbol.replace("/", "")
        url = f"https://fapi.binance.com/fapi/v1/depth?symbol={sym_clean}&limit=500"
        r   = _requests.get(url, timeout=8)
        r.raise_for_status()
        data  = r.json()
        precio = get_precio_actual(symbol)["precio"]

        # Parsear bids y asks como (price, qty_usd)
        bids = [(float(p), float(q) * float(p)) for p, q in data.get("bids", [])]
        asks = [(float(p), float(q) * float(p)) for p, q in data.get("asks", [])]

        total_bid_usd = sum(v for _, v in bids)
        total_ask_usd = sum(v for _, v in asks)
        total_usd     = total_bid_usd + total_ask_usd

        imbalance_pct = round(total_bid_usd / total_usd * 100, 1) if total_usd > 0 else 50.0
        if imbalance_pct >= 58:
            imbalance_bias = "bid"
        elif imbalance_pct <= 42:
            imbalance_bias = "ask"
        else:
            imbalance_bias = "neutral"

        # Top N paredes por tamaño USD
        top_bids_sorted = sorted(bids, key=lambda x: x[1], reverse=True)[:_WALL_TOP_N]
        top_asks_sorted = sorted(asks, key=lambda x: x[1], reverse=True)[:_WALL_TOP_N]

        def fmt_walls(walls):
            return [
                {
                    "price": round(p, 1),
                    "usd":   round(v, 0),
                    "pct":   round(v / total_usd * 100, 1) if total_usd > 0 else 0,
                }
                for p, v in walls if v >= _WALL_THRESHOLD_USD
            ]

        top_bids_fmt = fmt_walls(top_bids_sorted)
        top_asks_fmt = fmt_walls(top_asks_sorted)

        # Pared más cercana dentro del 2% del precio actual
        rango_2pct = precio * 0.02
        bids_cercanas = [(p, v) for p, v in bids if precio - p <= rango_2pct and p < precio]
        asks_cercanas = [(p, v) for p, v in asks if p - precio <= rango_2pct and p > precio]

        nearest_bid = None
        if bids_cercanas:
            p_max, v_max = max(bids_cercanas, key=lambda x: x[1])
            if v_max >= _WALL_THRESHOLD_USD:
                nearest_bid = {
                    "price":    round(p_max, 1),
                    "usd":      round(v_max, 0),
                    "dist_pct": round((precio - p_max) / precio * 100, 2),
                }

        nearest_ask = None
        if asks_cercanas:
            p_max, v_max = max(asks_cercanas, key=lambda x: x[1])
            if v_max >= _WALL_THRESHOLD_USD:
                nearest_ask = {
                    "price":    round(p_max, 1),
                    "usd":      round(v_max, 0),
                    "dist_pct": round((p_max - precio) / precio * 100, 2),
                }

        # Depth acumulado ±1% y ±2%
        rango_1pct  = precio * 0.01
        bid_d1 = round(sum(v for p, v in bids if precio - p <= rango_1pct), 0)
        ask_d1 = round(sum(v for p, v in asks if p - precio <= rango_1pct), 0)
        bid_d2 = round(sum(v for p, v in bids if precio - p <= rango_2pct), 0)
        ask_d2 = round(sum(v for p, v in asks if p - precio <= rango_2pct), 0)

        result = {
            "imbalance_pct":    imbalance_pct,
            "imbalance_bias":   imbalance_bias,
            "top_bids":         top_bids_fmt,
            "top_asks":         top_asks_fmt,
            "nearest_bid_wall": nearest_bid,
            "nearest_ask_wall": nearest_ask,
            "bid_depth_1pct":   bid_d1,
            "ask_depth_1pct":   ask_d1,
            "bid_depth_2pct":   bid_d2,
            "ask_depth_2pct":   ask_d2,
            "precio_ref":       precio,
            "error":            None,
        }
    except Exception as e:
        result["error"] = str(e)

    _evict(_L2_CACHE)
    _L2_CACHE[symbol] = {"data": result, "ts": ahora}
    return result


def get_liquidation_zones(symbol: str = "BTC/USDT") -> dict:
    """
    Estima zonas de liquidación para longs y shorts desde el precio actual.
    Usa la fórmula de Binance Futures con margen de mantenimiento ~1%.

    IMPORTANTE: son proyecciones matemáticas desde el precio actual,
    NO posiciones históricas reales (eso es propietario de Coinglass).

    Long liquidado en:  entrada × (1 - 1/leverage + 0.005)  aprox
    Short liquidado en: entrada × (1 + 1/leverage - 0.005)  aprox
    """
    try:
        precio = get_precio_actual(symbol)["precio"]
        leverages = [10, 25, 50, 100]

        longs  = {}
        shorts = {}
        for lev in leverages:
            # Margen inicial = 1/leverage, mantenimiento ~0.5%
            liq_long  = round(precio * (1 - 1/lev + 0.005), 1)
            liq_short = round(precio * (1 + 1/lev - 0.005), 1)
            longs[f"{lev}x"]  = liq_long
            shorts[f"{lev}x"] = liq_short

        return {
            "precio_ref": precio,
            "longs":      longs,   # precios de liquidación bajando
            "shorts":     shorts,  # precios de liquidación subiendo
            "nota":       "Estimacion matematica — no posiciones reales",
            "error":      None,
        }
    except Exception as e:
        return {"longs": {}, "shorts": {}, "error": str(e)}


# Cache exclusivo para el heatmap (TTL 90s — necesita ser fresco pero no en cada tick)
_HEATMAP_CACHE: dict = {}
_HEATMAP_TTL = 90


def get_liquidation_heatmap(symbol: str = "BTC/USDT") -> dict:
    """
    Heatmap de liquidaciones estimado 100% con datos propios de Binance.
    Sin Coinglass ni API de pago.

    Combina 3 capas:
      1. Multi-leverage math (2x–125x) ponderado por frecuencia de uso real
      2. Volume Profile de últimas 500 velas 1H → dónde se acumulan posiciones
      3. Wick analysis → dónde ocurrieron liquidaciones reales en el pasado

    Retorna:
        precio_ref  : precio actual
        vp_poc      : Point of Control (precio con mayor volumen)
        vp_vah/val  : Value Area High / Low (70% del volumen)
        bins        : lista de dicts por bin de precio (0.5% wide):
            price       : precio central del bin
            pct         : % desde precio actual (negativo = debajo)
            d_long      : densidad liq long 0–1 (debajo del precio)
            d_short     : densidad liq short 0–1 (arriba del precio)
            vol         : volumen normalizado 0–1 en este nivel
            wicks       : nº de wicks históricos en este rango
        liq_levels  : lista de {side, lev, price, pct} — niveles clave
    """
    ahora = time.time()
    if symbol in _HEATMAP_CACHE and (ahora - _HEATMAP_CACHE[symbol]["ts"]) < _HEATMAP_TTL:
        return _HEATMAP_CACHE[symbol]["data"]

    empty = {
        "precio_ref": None, "vp_poc": None, "vp_vah": None, "vp_val": None,
        "bins": [], "liq_levels": [], "error": None,
    }
    try:
        sym_clean = symbol.replace("/", "")
        precio = get_precio_actual(symbol)["precio"]
        if not precio:
            empty["error"] = "Sin precio"
            return empty

        # ── Parámetros del heatmap ────────────────────────────────────────────
        BIN_SIZE  = 0.005   # 0.5% por bin
        RANGE     = 0.20    # ±20% desde precio actual
        n_bins    = int(RANGE * 2 / BIN_SIZE)          # 80 bins
        bin_prices = [
            precio * (1 - RANGE + i * BIN_SIZE + BIN_SIZE / 2)
            for i in range(n_bins)
        ]

        def price_to_bin(p):
            """Retorna índice de bin para un precio p. -1 si fuera de rango."""
            idx = int((p - precio * (1 - RANGE)) / (precio * BIN_SIZE))
            return idx if 0 <= idx < n_bins else -1

        vol_bins   = [0.0] * n_bins
        wick_bins  = [0]   * n_bins
        d_long     = [0.0] * n_bins
        d_short    = [0.0] * n_bins

        # ── Capa 1: Volume Profile ────────────────────────────────────────────
        # 500 velas 1H ≈ 20 días. Distribuir volumen uniformemente entre low-high.
        url_kl = (
            f"https://fapi.binance.com/fapi/v1/klines"
            f"?symbol={sym_clean}&interval=1h&limit=500"
        )
        r = _requests.get(url_kl, timeout=10)
        r.raise_for_status()
        klines = r.json()

        wick_events = []   # (wick_price, wick_size_pct, side)

        for k in klines:
            open_  = float(k[1])
            high   = float(k[2])
            low    = float(k[3])
            close  = float(k[4])
            vol    = float(k[5])
            rng    = high - low
            if rng <= 0:
                continue

            # Distribuir volumen linealmente entre low y high
            lo_bin = price_to_bin(low)
            hi_bin = price_to_bin(high)
            if lo_bin < 0: lo_bin = 0
            if hi_bin >= n_bins: hi_bin = n_bins - 1
            if hi_bin < lo_bin: continue
            bins_span = hi_bin - lo_bin + 1
            vol_per_bin = vol / bins_span
            for b in range(lo_bin, hi_bin + 1):
                vol_bins[b] += vol_per_bin

            # Wick analysis — identificar sweeps de liquidez
            body = abs(close - open_)
            bullish = close >= open_

            # Lower wick grande → swept longs (liquidaciones de longs debajo)
            lower_wick = min(open_, close) - low
            if rng > 0 and lower_wick / rng > 0.30:   # wick > 30% del rango
                wick_pct = lower_wick / low * 100
                wick_events.append((low, wick_pct, "long_sweep"))

            # Upper wick grande → swept shorts
            upper_wick = high - max(open_, close)
            if rng > 0 and upper_wick / rng > 0.30:
                wick_pct = upper_wick / high * 100
                wick_events.append((high, wick_pct, "short_sweep"))

        for (wick_p, wick_sz, side) in wick_events:
            b = price_to_bin(wick_p)
            if b < 0: continue
            wick_bins[b] += 1

        # ── Capa 2: Multi-leverage liquidation density ────────────────────────
        # Distribución de apalancamiento: 10x y 20x son los más comunes en Binance
        # Peso estimado: frecuencia observada en datos públicos de Binance
        LEVERAGES = {
            2: 0.3, 3: 0.5, 5: 1.5, 7: 1.2, 10: 5.0,
            15: 3.5, 20: 4.5, 25: 3.5, 50: 2.5,
            75: 1.0, 100: 1.5, 125: 0.8,
        }
        total_lev_weight = sum(LEVERAGES.values())

        liq_levels = []
        for lev, weight in LEVERAGES.items():
            mm = 0.005  # margen de mantenimiento ~0.5%
            liq_l = precio * (1 - 1/lev + mm)   # long liq (debajo)
            liq_s = precio * (1 + 1/lev - mm)   # short liq (arriba)
            norm_w = weight / total_lev_weight

            b_long  = price_to_bin(liq_l)
            b_short = price_to_bin(liq_s)

            # Spreads gaussianos: un nivel de leverage "derrama" a bins vecinos
            spread = 1   # ±1 bin alrededor del nivel principal
            for offset in range(-spread, spread + 1):
                w_factor = 1.0 if offset == 0 else 0.4
                bl = b_long  + offset
                bs = b_short + offset
                if 0 <= bl < n_bins:
                    d_long[bl]  += norm_w * w_factor
                if 0 <= bs < n_bins:
                    d_short[bs] += norm_w * w_factor

            # Registrar niveles clave (10x, 20x, 25x, 50x, 100x)
            if lev in (10, 20, 25, 50, 100):
                pct_l = round((liq_l / precio - 1) * 100, 1)
                pct_s = round((liq_s / precio - 1) * 100, 1)
                if -RANGE*100 <= pct_l <= RANGE*100:
                    liq_levels.append({"side": "long",  "lev": f"{lev}x",
                                       "price": round(liq_l, 0), "pct": pct_l})
                if -RANGE*100 <= pct_s <= RANGE*100:
                    liq_levels.append({"side": "short", "lev": f"{lev}x",
                                       "price": round(liq_s, 0), "pct": pct_s})

        liq_levels.sort(key=lambda x: x["price"], reverse=True)

        # ── Volume Profile → normalizar y calcular VP stats ───────────────────
        max_vol = max(vol_bins) or 1.0
        vol_norm = [v / max_vol for v in vol_bins]

        # POC = bin con mayor volumen
        poc_idx = vol_bins.index(max(vol_bins))
        vp_poc  = round(bin_prices[poc_idx], 0)

        # Value Area: 70% del volumen total
        total_vol  = sum(vol_bins)
        va_target  = total_vol * 0.70
        va_accum   = vol_bins[poc_idx]
        lo_va = hi_va = poc_idx
        while va_accum < va_target:
            expand_hi = (hi_va + 1 < n_bins)
            expand_lo = (lo_va - 1 >= 0)
            if not expand_hi and not expand_lo:
                break
            add_hi = vol_bins[hi_va + 1] if expand_hi else 0
            add_lo = vol_bins[lo_va - 1] if expand_lo else 0
            if expand_hi and (add_hi >= add_lo or not expand_lo):
                hi_va += 1
                va_accum += add_hi
            else:
                lo_va -= 1
                va_accum += add_lo
        vp_vah = round(bin_prices[hi_va], 0)
        vp_val = round(bin_prices[lo_va], 0)

        # ── Normalizar wick density ───────────────────────────────────────────
        max_wicks = max(wick_bins) or 1
        wick_norm = [w / max_wicks for w in wick_bins]

        # ── Mezclar las 3 capas: cada bin suma influencia de los 3 orígenes ──
        # Pesos: leverage math 50% | volume 30% | wicks 20%
        W_LEV = 0.50
        W_VOL = 0.30
        W_WCK = 0.20

        max_dl = max(d_long)  or 1.0
        max_ds = max(d_short) or 1.0
        dl_norm = [v / max_dl  for v in d_long]
        ds_norm = [v / max_ds  for v in d_short]

        bins_out = []
        current_bin = price_to_bin(precio)
        for i, bp in enumerate(bin_prices):
            pct = round((bp / precio - 1) * 100, 2)
            # Solo long liq en bins DEBAJO del precio; short ARRIBA
            dl_combined = (dl_norm[i] * W_LEV + vol_norm[i] * W_VOL + wick_norm[i] * W_WCK) if i < current_bin else 0.0
            ds_combined = (ds_norm[i] * W_LEV + vol_norm[i] * W_VOL + wick_norm[i] * W_WCK) if i > current_bin else 0.0

            bins_out.append({
                "price":   round(bp, 0),
                "pct":     pct,
                "d_long":  round(dl_combined, 4),
                "d_short": round(ds_combined, 4),
                "vol":     round(vol_norm[i], 4),
                "wicks":   wick_bins[i],
            })

        result = {
            "precio_ref": round(precio, 0),
            "vp_poc":     vp_poc,
            "vp_vah":     vp_vah,
            "vp_val":     vp_val,
            "bins":       bins_out,
            "liq_levels": liq_levels,
            "error":      None,
        }

    except Exception as e:
        logger.warning(f"get_liquidation_heatmap error: {e}")
        result = {**empty, "error": str(e)}

    _HEATMAP_CACHE[symbol] = {"data": result, "ts": ahora}
    return result


# ============================================================
# HEATMAP 2D HISTÓRICO — Phase 3
# Calcula zonas de liquidación para CADA vela usando solo
# matemática de leverage. Cero llamadas extra a Binance.
# ============================================================

# ── Grupos de leverage por timeframe ─────────────────────────────────
# Cada grupo tiene: key (nombre UI), levs (lista de (leverage, maint_margin)),
# w (peso relativo — aproxima % del OI de Binance en ese rango de leverage)
_LEV_GROUPS_BY_TF = {
    "15m": [
        {"key": "10x",    "levs": [(10,  0.005)],             "w": 0.40},
        {"key": "20-25x", "levs": [(20,  0.005), (25, 0.005)], "w": 0.38},
        {"key": "50x",    "levs": [(50,  0.005)],             "w": 0.15},
        {"key": "100x",   "levs": [(100, 0.005)],             "w": 0.07},
    ],
    "1h": [
        {"key": "10x",    "levs": [(10,  0.005)],             "w": 0.40},
        {"key": "20-25x", "levs": [(20,  0.005), (25, 0.005)], "w": 0.38},
        {"key": "50x",    "levs": [(50,  0.005)],             "w": 0.15},
        {"key": "100x",   "levs": [(100, 0.005)],             "w": 0.07},
    ],
    "4h": [
        {"key": "3x",   "levs": [(3,  0.015)], "w": 0.20},
        {"key": "5x",   "levs": [(5,  0.010)], "w": 0.30},
        {"key": "10x",  "levs": [(10, 0.005)], "w": 0.35},
        {"key": "25x",  "levs": [(25, 0.005)], "w": 0.15},
    ],
    "1d": [
        {"key": "3x",   "levs": [(3,  0.015)], "w": 0.20},
        {"key": "5x",   "levs": [(5,  0.010)], "w": 0.30},
        {"key": "10x",  "levs": [(10, 0.005)], "w": 0.35},
        {"key": "25x",  "levs": [(25, 0.005)], "w": 0.15},
    ],
}


def get_2d_heatmap(candles: list, precio_ref: float, tf: str = "4h") -> dict:
    """
    Heatmap 2D histórico para Market Depth.

    Retorna grids SEPARADOS por grupo de leverage (no mezclados).
    El frontend elige qué grupos mostrar y combina color según el heat.

    Args:
        candles   : lista de {time (unix), close}
        precio_ref: precio actual de referencia
        tf        : timeframe activo — determina qué leverages son relevantes

    Retorna:
        grids      : dict {lev_key → {long: 2D list, short: 2D list}}
                     cada celda es heat 0-1 normalizado dentro de su grupo
        lev_groups : lista ordenada de keys disponibles (para UI dinámica)
        times, closes, n_bins, bin_size, range, tf
    """
    empty = {
        'grids': {}, 'lev_groups': [],
        'times': [], 'closes': [],
        'n_bins': 0, 'bin_size': 0.0025, 'range': 0.20, 'tf': tf,
    }

    if not candles or not precio_ref:
        return empty

    BIN_SIZE = 0.0025   # 0.25% por bin → 160 bins
    RANGE    = 0.20     # ±20% desde el close de CADA vela
    n_bins   = int(RANGE * 2 / BIN_SIZE)   # 160

    # Kernel gaussiano ±2 bins (spread de liquidación en precio)
    GAUSS_OFFSETS = {0: 1.0, -1: 0.60, 1: 0.60, -2: 0.25, 2: 0.25}
    # Kernel gaussiano vertical (suavizado entre bins adyacentes)
    BLUR_K = {-1: 0.35, 0: 1.0, 1: 0.35}
    SMOOTH_W = 5   # velas de rolling sum temporal

    groups = _LEV_GROUPS_BY_TF.get(tf, _LEV_GROUPS_BY_TF["4h"])

    def price_to_bin(p, cp):
        lo  = cp * (1 - RANGE)
        idx = int((p - lo) / (cp * BIN_SIZE))
        return idx if 0 <= idx < n_bins else -1

    # ── Bins absolutos (precio fijo) → bandas HORIZONTALES estáticas ──
    ABS_RANGE = 0.40
    n_abs     = int(ABS_RANGE * 2 / BIN_SIZE)
    abs_lo    = precio_ref * (1 - ABS_RANGE)

    def abs_p2b(p):
        idx = int((p - abs_lo) / (precio_ref * BIN_SIZE))
        return idx if 0 <= idx < n_abs else -1

    raw_abs = {g["key"]: {"long": [0.0]*n_abs, "short": [0.0]*n_abs}
               for g in groups}

    # ── Acumular columnas crudas por grupo ────────────────────────────
    times  = []
    closes = []
    raw = {g["key"]: {"long": [], "short": []} for g in groups}

    # Solo las últimas 150 velas para bins absolutos — posiciones más recientes
    # y más probablemente aún abiertas. Más velas → todo se llena de heat.
    abs_candles_limit = 150

    for ci, c in enumerate(candles):
        price = c.get("close") or c.get("c", 0)
        if not price:
            continue
        times.append(c["time"])
        closes.append(price)
        use_for_abs = ci >= len(candles) - abs_candles_limit

        for g in groups:
            col_l = [0.0] * n_bins
            col_s = [0.0] * n_bins
            n_levs = len(g["levs"])
            for lev, mm in g["levs"]:
                liq_l = price * (1 - 1 / lev + mm)
                liq_s = price * (1 + 1 / lev - mm)
                w_lev = 1.0 / n_levs
                for off, wf in GAUSS_OFFSETS.items():
                    bl = price_to_bin(liq_l, price) + off
                    bs = price_to_bin(liq_s, price) + off
                    if 0 <= bl < n_bins: col_l[bl] += w_lev * wf
                    if 0 <= bs < n_bins: col_s[bs] += w_lev * wf

                # Bins absolutos — sin spread gaussiano, precio exacto de liq.
                # Este bloque está FUERA del for-off, por eso se omite "off==0".
                if use_for_abs:
                    if liq_l < precio_ref * 0.999:
                        al = abs_p2b(liq_l)
                        if 0 <= al < n_abs:
                            raw_abs[g["key"]]["long"][al] += w_lev
                    if liq_s > precio_ref * 1.001:
                        as_ = abs_p2b(liq_s)
                        if 0 <= as_ < n_abs:
                            raw_abs[g["key"]]["short"][as_] += w_lev
            raw[g["key"]]["long"].append(col_l)
            raw[g["key"]]["short"].append(col_s)

    if not times:
        return empty

    n_c = len(times)

    # ── Smoothing + normalización por grupo ───────────────────────────
    grids = {}
    for g in groups:
        key = g["key"]
        rl  = raw[key]["long"]
        rs  = raw[key]["short"]

        # Opción A: rolling sum temporal (5 velas)
        sm_l = [[0.0] * n_bins for _ in range(n_c)]
        sm_s = [[0.0] * n_bins for _ in range(n_c)]
        for bi in range(n_bins):
            wl, ws = [], []
            al = as_ = 0.0
            for ci in range(n_c):
                wl.append(rl[ci][bi]); al += rl[ci][bi]
                ws.append(rs[ci][bi]); as_ += rs[ci][bi]
                if len(wl) > SMOOTH_W:
                    al  -= wl.pop(0)
                    as_ -= ws.pop(0)
                sm_l[ci][bi] = al
                sm_s[ci][bi] = as_

        # Opción B: Gaussian blur vertical (entre bins de precio)
        bl_l = [[0.0] * n_bins for _ in range(n_c)]
        bl_s = [[0.0] * n_bins for _ in range(n_c)]
        for ci in range(n_c):
            for bi in range(n_bins):
                sl = ss = sw = 0.0
                for db, bw in BLUR_K.items():
                    nb = bi + db
                    if 0 <= nb < n_bins:
                        sl += sm_l[ci][nb] * bw
                        ss += sm_s[ci][nb] * bw
                        sw += bw
                if sw > 0:
                    bl_l[ci][bi] = sl / sw
                    bl_s[ci][bi] = ss / sw

        # Normalización por percentil 90 — evita que zonas alejadas (ej: 64k)
        # queden aplastadas a 0 por un pico muy alto cerca del precio actual.
        def _p90_norm(grid):
            vals = sorted(v for row in grid for v in row if v > 0)
            if not vals:
                return 1e-9
            idx = int(len(vals) * 0.90)
            p90 = vals[min(idx, len(vals)-1)]
            return max(p90, 1e-9)

        norm_l = _p90_norm(bl_l)
        norm_s = _p90_norm(bl_s)

        grids[key] = {
            "long":  [[round(min(v / norm_l, 1.0), 2) for v in row] for row in bl_l],
            "short": [[round(min(v / norm_s, 1.0), 2) for v in row] for row in bl_s],
            "w":     g["w"],
        }

    # ── Normalizar bins absolutos y armar agg_grids ───────────────────
    def _p90_1d(arr):
        vals = sorted(v for v in arr if v > 0)
        if not vals:
            return 1e-9
        return max(vals[min(int(len(vals) * 0.90), len(vals)-1)], 1e-9)

    agg_grids = {}
    for g in groups:
        key = g["key"]
        al  = raw_abs[key]["long"]
        as_ = raw_abs[key]["short"]
        max_al = max(al) or 1e-9
        max_as = max(as_) or 1e-9
        # Potencia 1.5: contraste moderado. Picos reales brillan, ruido se atenúa.
        # v=1.0 → 1.0 | v=0.7 → 0.59 | v=0.4 → 0.25 | v=0.2 → 0.09
        agg_grids[key] = {
            "long":  [round(min((v / max_al) ** 1.5, 1.0), 2) for v in al],
            "short": [round(min((v / max_as) ** 1.5, 1.0), 2) for v in as_],
        }

    return {
        "grids":        grids,
        "agg_grids":    agg_grids,
        "agg_n_bins":   n_abs,
        "agg_range":    ABS_RANGE,
        "agg_bin_size": BIN_SIZE,
        "lev_groups":   [g["key"] for g in groups],
        "times":        times,
        "closes":       closes,
        "n_bins":       n_bins,
        "bin_size":     BIN_SIZE,
        "range":        RANGE,
        "tf":           tf,
        "precio_ref":   precio_ref,
    }


# ============================================================
# HEATMAP 2D REAL — liquidaciones ejecutadas de Binance Futures
# Reemplaza el modelo teórico con datos reales del mercado.
# Endpoint: GET /fapi/v1/allForceOrders (público, sin auth)
# Límite histórico: ~7 días (restricción de Binance).
# ============================================================

_REAL_LIQ_CACHE: dict = {}
_REAL_LIQ_TTL = 300   # 5 minutos — datos actuales cambian, pasados no


def get_real_liq_heatmap(candles: list, tf: str = '4h',
                          symbol: str = 'BTC/USDT') -> dict:
    """
    Heatmap 2D con DATOS REALES de liquidaciones de Binance Futures.

    Cada vela recibe las liquidaciones ejecutadas en su ventana temporal.
    Los volúmenes reales en USDT reemplazan la estimación teórica por leverage.

    Side mapping (Binance Futures):
      BUY  order = short position liquidated  → grid_short
      SELL order = long  position liquidated  → grid_long

    Args:
        candles : lista [{time (unix s), close, ...}] — velas del chart
        tf      : timeframe activo ('15m','1h','4h','1d')
        symbol  : 'BTC/USDT' o 'BTCUSDT'

    Returns:
        Mismo formato que get_2d_heatmap() — frontend sin cambios.
    """
    BIN_SIZE = 0.0025
    RANGE    = 0.20
    n_bins   = int(RANGE * 2 / BIN_SIZE)   # 160 bins

    empty = {
        'grid_long': [], 'grid_short': [], 'times': [], 'closes': [],
        'n_bins': 0, 'bin_size': BIN_SIZE, 'range': RANGE,
    }

    if not candles:
        return empty

    # ── Cache ─────────────────────────────────────────────────────────
    ahora     = time.time()
    cache_key = f"{symbol}_{tf}_{candles[-1]['time']}"
    if cache_key in _REAL_LIQ_CACHE:
        c = _REAL_LIQ_CACHE[cache_key]
        if ahora - c['ts'] < _REAL_LIQ_TTL:
            return c['data']

    # ── Leer de liq_events.db (generado por depth_worker.py) ────────────
    # El endpoint REST /fapi/v1/allForceOrders está deprecado (HTTP 400).
    # Los datos reales vienen del WebSocket listener que escribe en SQLite.
    TF_MS = {'15m': 900_000, '1h': 3_600_000,
              '4h': 14_400_000, '1d': 86_400_000}
    tf_ms     = TF_MS.get(tf, 14_400_000)
    sym_clean = symbol.replace('/', '').replace(':USDT', '')   # BTCUSDT

    start_ms = candles[0]['time']  * 1000
    end_ms   = candles[-1]['time'] * 1000 + tf_ms

    all_orders: list = []
    try:
        import sqlite3 as _sq, os as _os
        _DB = _os.path.join(_os.path.dirname(__file__), 'liq_events.db')
        with _sq.connect(_DB, timeout=5) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            rows = conn.execute(
                """SELECT ts_ms, side, price, vol_usd
                   FROM liq_events
                   WHERE symbol = ?
                     AND ts_ms BETWEEN ? AND ?
                   ORDER BY ts_ms ASC""",
                (sym_clean, int(start_ms), int(end_ms)),
            ).fetchall()
        all_orders = [
            {'time': r[0] // 1000, 'side': r[1], 'price': r[2], 'vol_usd': r[3]}
            for r in rows
        ]
    except Exception as exc:
        logger.error("[real_liq_heatmap] sqlite error: %s", exc)
        return empty

    if not all_orders:
        return empty

    # ── Estructuras de salida ─────────────────────────────────────────
    times  = [c['time']                        for c in candles]
    closes = [c.get('close') or c.get('c', 0)  for c in candles]
    n_c    = len(candles)

    raw_long  = [[0.0] * n_bins for _ in range(n_c)]
    raw_short = [[0.0] * n_bins for _ in range(n_c)]

    def price_to_bin(p: float, ref: float) -> int:
        lo  = ref * (1 - RANGE)
        idx = int((p - lo) / (ref * BIN_SIZE))
        return idx if 0 <= idx < n_bins else -1

    # ── Mapear cada evento a su vela + bin ───────────────────────────
    # Formato SQLite: {time (unix s), side ('long'|'short'), price, vol_usd}
    for order in all_orders:
        order_t = order['time']          # ya en unix segundos
        price   = float(order['price'])
        vol_usd = float(order['vol_usd'])
        side    = order['side']          # 'long' | 'short'

        if not price or not vol_usd:
            continue

        # Encontrar la vela propietaria (búsqueda hacia atrás)
        ci = -1
        for i in range(n_c - 1, -1, -1):
            if order_t >= times[i]:
                ci = i
                break
        if ci < 0:
            continue

        ref = closes[ci]
        if not ref:
            continue

        bi = price_to_bin(price, ref)
        if bi < 0:
            continue

        if side == 'short':
            raw_short[ci][bi] += vol_usd
        else:
            raw_long[ci][bi]  += vol_usd

    # Verificar que hay señal real
    has_data = (any(any(v > 0 for v in col) for col in raw_long) or
                any(any(v > 0 for v in col) for col in raw_short))
    if not has_data:
        logger.warning("[real_liq_heatmap] órdenes recibidas pero sin mapeo válido al grid")
        return empty

    # ── Opción A: Rolling temporal smoothing ──────────────────────────
    SMOOTH_W = 5
    sm_long  = [[0.0] * n_bins for _ in range(n_c)]
    sm_short = [[0.0] * n_bins for _ in range(n_c)]
    for bi in range(n_bins):
        win_l: list[float] = []
        win_s: list[float] = []
        acc_l = acc_s = 0.0
        for ci in range(n_c):
            win_l.append(raw_long[ci][bi]);  acc_l += raw_long[ci][bi]
            win_s.append(raw_short[ci][bi]); acc_s += raw_short[ci][bi]
            if len(win_l) > SMOOTH_W:
                acc_l -= win_l.pop(0)
                acc_s -= win_s.pop(0)
            sm_long[ci][bi]  = acc_l
            sm_short[ci][bi] = acc_s
    raw_long, raw_short = sm_long, sm_short

    # ── Opción B: Gaussian blur vertical (eje precio) ─────────────────
    G = {-1: 0.35, 0: 1.0, 1: 0.35}
    bl_long  = [[0.0] * n_bins for _ in range(n_c)]
    bl_short = [[0.0] * n_bins for _ in range(n_c)]
    for ci in range(n_c):
        for bi in range(n_bins):
            sl = ss = sw = 0.0
            for db, w in G.items():
                nb = bi + db
                if 0 <= nb < n_bins:
                    sl += raw_long[ci][nb]  * w
                    ss += raw_short[ci][nb] * w
                    sw += w
            bl_long[ci][bi]  = sl / sw
            bl_short[ci][bi] = ss / sw
    raw_long, raw_short = bl_long, bl_short

    # ── Normalización global ──────────────────────────────────────────
    max_l = max(max(col) for col in raw_long)  or 1.0
    max_s = max(max(col) for col in raw_short) or 1.0

    grid_long  = [[round(v / max_l, 3) for v in col] for col in raw_long]
    grid_short = [[round(v / max_s, 3) for v in col] for col in raw_short]

    result = {
        'grid_long':  grid_long,
        'grid_short': grid_short,
        'times':      times,
        'closes':     closes,
        'n_bins':     n_bins,
        'bin_size':   BIN_SIZE,
        'range':      RANGE,
    }

    _REAL_LIQ_CACHE[cache_key] = {'data': result, 'ts': ahora}
    return result


# ============================================================
# ORDER FLOW — CVD + LARGE TRADES
# ============================================================

_CVD_TTL    = 120   # 2 minutos
_TRADES_TTL = 60    # 1 minuto


# Mapeo TF → (interval Binance, candles a pedir, candles para bias, candles para divergencia)
_CVD_TF_MAP = {
    "15m": ("15m", 32, 8,  4),   # 8h de 15m, divergencia 4 velas = 1h
    "1h":  ("1h",  24, 6,  4),   # 24h de 1h,  divergencia 4 velas = 4h
    "4h":  ("4h",  24, 6,  4),   # 4 días de 4h, divergencia 4 velas = 16h
    "1d":  ("1d",  14, 5,  3),   # 14 días daily, divergencia 3 velas = 3d
}


def get_cvd(symbol: str = "BTC/USDT", tf: str = "1h", limit: int = None) -> dict:
    """
    Cumulative Volume Delta (CVD) via Binance Futures klines raw.
    TF-aware: intervalo y ventana cambian según el timeframe activo.

    Delta por vela = taker_buy_base_vol - taker_sell_base_vol
    CVD = sum(deltas) — mide presión compradora vs vendedora acumulada.

    Divergencia CVD: precio sube pero CVD baja → movimiento no confirmado.
    """
    interval, default_limit, bias_n, div_n = _CVD_TF_MAP.get(tf, _CVD_TF_MAP["1h"])
    if limit is None:
        limit = default_limit

    ahora = time.time()
    key   = (symbol, tf, limit)
    if key in _CVD_CACHE and (ahora - _CVD_CACHE[key]["ts"]) < _CVD_TTL:
        return _CVD_CACHE[key]["data"]
    result = {"cvd_actual": None, "delta_ultima": None, "cvd_bias": "neutral",
              "divergencia": False, "cvd_cambio_pct": None, "tf": tf, "error": None}
    try:
        sym_clean = symbol.replace("/", "")
        url = (
            f"https://fapi.binance.com/fapi/v1/klines"
            f"?symbol={sym_clean}&interval={interval}&limit={limit}"
        )
        r = _requests.get(url, timeout=8)
        r.raise_for_status()
        klines = r.json()

        if len(klines) < 4:
            raise ValueError("Klines insuficientes para CVD")

        # Index 0=open_time(ms), 4=close, 5=vol_total, 9=taker_buy_base_asset_volume
        deltas      = []
        prices      = []
        open_times  = []
        for k in klines:
            total_vol   = float(k[5])
            taker_buy   = float(k[9])
            taker_sell  = total_vol - taker_buy
            delta       = taker_buy - taker_sell
            deltas.append(delta)
            prices.append(float(k[4]))   # close
            open_times.append(int(k[0])) # ms timestamp

        # CVD acumulado (desde la primera vela del bloque)
        cvd_series  = []
        acc = 0.0
        for d in deltas:
            acc += d
            cvd_series.append(acc)

        cvd_actual    = cvd_series[-1]
        cvd_inicial   = cvd_series[0]
        delta_ultima  = deltas[-1]

        # Bias según dirección del CVD en las últimas N velas (depende del TF)
        cvd_reciente = sum(deltas[-bias_n:])
        if cvd_reciente > 0:
            cvd_bias = "bullish"
        elif cvd_reciente < 0:
            cvd_bias = "bearish"
        else:
            cvd_bias = "neutral"

        # Divergencia: precio sube pero CVD baja (o vice versa) — últimas div_n velas
        precio_cambio = prices[-1] - prices[-div_n]
        cvd_cambio    = cvd_series[-1] - cvd_series[-div_n]
        divergencia   = (precio_cambio > 0 and cvd_cambio < 0) or \
                        (precio_cambio < 0 and cvd_cambio > 0)

        # Cambio % del CVD vs inicio del bloque
        if cvd_inicial != 0:
            cvd_cambio_pct = round((cvd_actual - cvd_inicial) / abs(cvd_inicial) * 100, 1)
        else:
            cvd_cambio_pct = None

        # Últimas 8 velas con delta individual (para analysis/delta.py)
        _import_dt = __import__("datetime")
        deltas_por_vela = [
            {
                "ts":    open_times[i],
                "fecha": _import_dt.datetime.utcfromtimestamp(open_times[i] / 1000).strftime("%m-%d %H:%M"),
                "delta": round(deltas[i], 2),
                "bias":  "bullish" if deltas[i] > 0 else ("bearish" if deltas[i] < 0 else "neutral"),
            }
            for i in range(max(0, len(deltas) - 8), len(deltas))
        ]

        result = {
            "cvd_actual":      round(cvd_actual, 2),
            "delta_ultima":    round(delta_ultima, 2),
            "cvd_bias":        cvd_bias,       # bullish / bearish / neutral
            "divergencia":     divergencia,    # True = precio y CVD no confirman
            "cvd_cambio_pct":  cvd_cambio_pct,
            "deltas_por_vela": deltas_por_vela,
            "tf":              tf,
            "error":           None,
        }
    except Exception as e:
        result["error"] = str(e)

    _evict(_CVD_CACHE)
    _CVD_CACHE[key] = {"data": result, "ts": ahora}
    return result


def get_large_trades(symbol: str = "BTC/USDT", min_usd: float = 500_000) -> dict:
    """
    Detecta whale trades en el tape (aggTrades).
    Filtra transacciones individuales >= min_usd en los últimos ~500 trades.

    Retorna:
        whale_count   : cuántos trades grandes hubo
        whale_bias    : buy / sell / neutral
        buy_volume    : volumen USD comprador de whales
        sell_volume   : volumen USD vendedor de whales
        last_trade    : dict con el trade más reciente grande (o None)
    """
    ahora = time.time()
    key   = (symbol, min_usd)
    if key in _TRADES_CACHE and (ahora - _TRADES_CACHE[key]["ts"]) < _TRADES_TTL:
        return _TRADES_CACHE[key]["data"]
    result = {"whale_count": 0, "whale_bias": "neutral",
              "buy_volume": 0.0, "sell_volume": 0.0,
              "last_trade": None, "error": None}
    try:
        sym_clean = symbol.replace("/", "")
        url = (
            f"https://fapi.binance.com/fapi/v1/aggTrades"
            f"?symbol={sym_clean}&limit=500"
        )
        r = _requests.get(url, timeout=8)
        r.raise_for_status()
        trades = r.json()

        # Obtener precio actual para calcular USD value
        precio = get_precio_actual(symbol)["precio"]

        buy_vol  = 0.0
        sell_vol = 0.0
        count    = 0
        last     = None

        for t in trades:
            qty    = float(t["q"])
            usd_val = qty * precio
            if usd_val < min_usd:
                continue
            is_sell = t["m"]   # m=True → maker buy = aggressor is seller
            count += 1
            if is_sell:
                sell_vol += usd_val
            else:
                buy_vol += usd_val
            if last is None or t["T"] > last["T"]:
                last = t

        if buy_vol + sell_vol > 0:
            if buy_vol > sell_vol * 1.2:
                bias = "buy"
            elif sell_vol > buy_vol * 1.2:
                bias = "sell"
            else:
                bias = "neutral"
        else:
            bias = "neutral"

        last_trade = None
        if last:
            is_sell    = last["m"]
            qty        = float(last["q"])
            last_trade = {
                "lado":   "SELL" if is_sell else "BUY",
                "qty":    round(qty, 4),
                "usd":    round(qty * precio, 0),
                "precio": float(last["p"]),
            }

        result = {
            "whale_count": count,
            "whale_bias":  bias,
            "buy_volume":  round(buy_vol, 0),
            "sell_volume": round(sell_vol, 0),
            "last_trade":  last_trade,
            "error":       None,
        }
    except Exception as e:
        result["error"] = str(e)

    _evict(_TRADES_CACHE)
    _TRADES_CACHE[key] = {"data": result, "ts": ahora}
    return result


# ============================================================
# MACRO CONTEXTO — DXY + BTC.D + F&G + CORRELACION BTC-DXY
# ============================================================

def get_macro_contexto() -> str:
    """
    Bloque de contexto macro: DXY + BTC.D + Fear & Greed + Correlacion BTC-DXY.
    Listo para inyectar en get_contexto_mercado() y get_regimen_mercado().
    """
    dxy  = get_dxy()
    btcd = get_btc_dominance()
    fng  = get_fear_greed()
    corr = get_btc_dxy_correlation()

    dxy_str  = f"{dxy['valor']:.2f}"   if dxy['valor']  else "Sin datos"
    btcd_str = f"{btcd['valor']:.2f}%" if btcd['valor'] else "Sin datos"
    fng_str  = f"{fng['valor']}/100"   if fng['valor']  else "Sin datos"

    return f"""
━━━ DXY + BTC DOMINANCE ━━━━━━━━━━━━━━━━━━
DXY (Dolar Index): {dxy_str}  {f"({dxy['cambio']:+.2f}% hoy)" if dxy['cambio'] is not None else ""}
Lectura:           {dxy['lectura']}

BTC Dominance:     {btcd_str}
Lectura:           {btcd['lectura']}

━━━ FEAR & GREED INDEX ━━━━━━━━━━━━━━━━━━━
Valor actual:      {fng_str}  ({fng.get('clasificacion', 'Sin datos')})
Lectura:           {fng['lectura']}

━━━ CORRELACION BTC-DXY ━━━━━━━━━━━━━━━━━━
Correlacion:       {corr['lectura']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""


# ============================================================
# CONTEXTO HTF SUPERIOR — resumen compacto del TF por encima
# ============================================================

# Tabla: TF solicitado → TFs superiores a resumir
_HTF_MAP = {
    "15m": ["1h", "4h"],
    "1h":  ["4h", "1d"],
    "4h":  ["1d"],
    "1d":  ["1w"],
}

# Periodos de EMA por TF (de más rápido a más lento)
_EMA_PERIODOS = {
    "15m": [9, 21, 50, 200],
    "1h":  [9, 21, 50, 200],
    "4h":  [5, 10, 21, 50, 200],
    "1d":  [20, 50, 200],
    "1w":  [20, 50, 200],
}

# Caché propio — 5 min (datos estructurales, cambian lento)
_HTFCTX_CACHE: dict = {}
_HTFCTX_TTL   = 300


def _resumir_tf(symbol: str, tf: str) -> str:
    """
    Resumen de UNA línea de un TF: bias EMAs, RSI, OI, CVD.
    Ejemplo: "MARCO 4H: BAJISTA — EMAs 4/5 bajistas | RSI 44 neutro | OI +1.1% bajista"
    """
    try:
        limit      = 220 if tf in ("4h",) else 200
        velas      = get_velas(symbol, tf, limit)
        if not velas or len(velas) < 50:
            return f"MARCO {tf.upper()}: Sin datos"

        closes = [v["close"] for v in velas]
        precio = closes[-1]

        # ── EMAs ──────────────────────────────────────────────
        periodos   = _EMA_PERIODOS.get(tf, [20, 50, 200])
        emas_bajo  = sum(
            1 for p in periodos
            if (ema_val := calcular_ema(closes, p)) and precio < ema_val
        )
        emas_total = len(periodos)
        emas_dir   = "bajistas" if emas_bajo > emas_total / 2 else "alcistas"

        # ── RSI ───────────────────────────────────────────────
        rsi_periodo = 14 if tf in ("15m", "1h", "1d", "1w") else 62
        rsi = calcular_rsi(closes, rsi_periodo, suavizado=3)
        if rsi is None:
            rsi_str = "RSI s/d"
        elif rsi > 60:
            rsi_str = f"RSI {rsi:.0f} sobrecomprado"
        elif rsi < 40:
            rsi_str = f"RSI {rsi:.0f} sobrevendido"
        else:
            rsi_str = f"RSI {rsi:.0f} neutro"

        # ── OI ────────────────────────────────────────────────
        oi = get_open_interest(symbol)
        if oi.get("cambio_4h") is not None:
            oi_val = oi["cambio_4h"]
            oi_dir = "alcista" if oi_val > 0 else "bajista"
            oi_str = f"OI {oi_val:+.1f}% {oi_dir}"
        else:
            oi_str = "OI s/d"

        # ── CVD ───────────────────────────────────────────────
        cvd = get_cvd(symbol, tf=tf)
        cvd_bias = cvd.get("cvd_bias", "neutral")
        cvd_str  = {"bullish": "CVD alcista", "bearish": "CVD bajista"}.get(cvd_bias, "CVD neutro")

        # ── Bias general ──────────────────────────────────────
        bias = "BAJISTA" if emas_bajo >= emas_total / 2 else "ALCISTA"

        return (
            f"MARCO {tf.upper()}: {bias} — "
            f"EMAs {emas_bajo}/{emas_total} {emas_dir} | "
            f"{rsi_str} | {oi_str} | {cvd_str}"
        )

    except Exception as e:
        return f"MARCO {tf.upper()}: Error ({e})"


def get_contexto_superior(symbol: str = "BTC/USDT", tf: str = "4h") -> str:
    """
    Retorna un bloque compacto con el contexto de los TFs superiores al actual.

    Tabla de TFs superiores:
      15m → 1H + 4H
      1H  → 4H + 1D
      4H  → 1D
      1D  → 1W

    Retorna "" si tf=4h y ya hay contexto macro, o si no hay TFs superiores definidos.

    Caché: 300s (cambios de estructura lentos)
    """
    tfs_superiores = _HTF_MAP.get(tf, [])
    if not tfs_superiores:
        return ""

    ahora = time.time()
    key   = (symbol, tf)
    if key in _HTFCTX_CACHE and (ahora - _HTFCTX_CACHE[key]["ts"]) < _HTFCTX_TTL:
        return _HTFCTX_CACHE[key]["data"]

    lineas = []
    for tf_sup in tfs_superiores:
        lineas.append(_resumir_tf(symbol, tf_sup))

    resultado = "\n".join(lineas)
    _HTFCTX_CACHE[key] = {"data": resultado, "ts": ahora}
    return resultado
