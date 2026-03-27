import time
import ccxt
import requests as _requests
from datetime import datetime

# ============================================================
# CONFIGURACIÓN — Sin API key, datos públicos gratis
# ============================================================
exchange      = ccxt.binance()                          # spot — precio, velas
exchange_fut  = ccxt.binance({                         # futuros — funding, OI
    'options': { 'defaultType': 'future' }
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
    _PRECIO_CACHE[symbol] = {"data": result, "ts": ahora}
    return result

def get_velas(symbol="BTC/USDT", timeframe="4h", limite=220):
    """Velas OHLCV — cacheadas 60s por (symbol, timeframe, limite)"""
    ahora = time.time()
    key = (symbol, timeframe, limite)
    if key in _VELAS_CACHE and (ahora - _VELAS_CACHE[key]["ts"]) < _DATA_TTL:
        return _VELAS_CACHE[key]["data"]
    velas = exchange.fetch_ohlcv(symbol, timeframe, limit=limite)
    result = [{
        "fecha":   datetime.fromtimestamp(v[0]/1000).strftime("%Y-%m-%d %H:%M"),
        "open":    v[1],
        "high":    v[2],
        "low":     v[3],
        "close":   v[4],
        "volumen": v[5]
    } for v in velas]
    _VELAS_CACHE[key] = {"data": result, "ts": ahora}
    return result

def get_funding_rate(symbol="BTC/USDT"):
    """Funding rate actual de Binance Futures — cacheado 60s"""
    ahora = time.time()
    if symbol in _FUNDING_CACHE and (ahora - _FUNDING_CACHE[symbol]["ts"]) < _DATA_TTL:
        return _FUNDING_CACHE[symbol]["data"]
    result = None
    try:
        symbol_fut = symbol.replace("/", "")
        funding    = exchange_fut.fetch_funding_rate(symbol_fut)
        # FIX BUG 5: usar `is not None` en vez de `or` para no perder funding == 0.0
        rate = funding.get("fundingRate")
        if rate is None:
            rate = funding.get("lastFundingRate")
        result = round(float(rate) * 100, 4) if rate is not None else None
    except Exception:
        pass
    _FUNDING_CACHE[symbol] = {"data": result, "ts": ahora}
    return result

def get_open_interest(symbol="BTC/USDT"):
    """OI actual + cambio % en 4H y 24H — cacheado 60s"""
    ts_ahora = time.time()
    if symbol in _OI_CACHE and (ts_ahora - _OI_CACHE[symbol]["ts"]) < _DATA_TTL:
        return _OI_CACHE[symbol]["data"]
    result = {"valor": None, "cambio_4h": None, "cambio_24h": None}
    try:
        symbol_fut = symbol.replace("/", "")
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
    _OI_CACHE[symbol] = {"data": result, "ts": ts_ahora}
    return result

# ============================================================
# CONTEXTO COMPLETO PARA TRADEBOT
# ============================================================

def get_contexto_mercado(symbol="BTC/USDT"):
    """Arma resumen completo del mercado para TradeBot"""

    precio  = get_precio_actual(symbol)
    velas4h = get_velas(symbol, "4h", 220)
    velas1d = get_velas(symbol, "1d", 210)

    closes4h = [v["close"] for v in velas4h]
    closes1d = [v["close"] for v in velas1d]

    precio_actual = precio["precio"]

    # ── EMAs 4H ──
    ema5   = calcular_ema(closes4h, 5)
    ema10  = calcular_ema(closes4h, 10)
    ema21  = calcular_ema(closes4h, 21)
    ema50  = calcular_ema(closes4h, 50)
    ema200 = calcular_ema(closes4h, 200)

    # ── EMA 200 Daily — contexto macro ──
    ema200d = calcular_ema(closes1d, 200) if len(closes1d) >= 200 else None
    # FIX BUG 4: distinguir "sin datos" de bajista real
    if ema200d is None:
        tendencia_macro = "SIN DATOS ❓ — Insuficientes velas diarias"
    elif precio_actual > ema200d:
        tendencia_macro = "ALCISTA 📈"
    else:
        tendencia_macro = "BAJISTA 📉"

    # ── RSI 62 en 4H ──
    rsi_4h = calcular_rsi(closes4h, periodo=62, suavizado=14)

    # ── Funding Rate ──
    funding = get_funding_rate(symbol)

    # ── Open Interest ──
    oi = get_open_interest(symbol)

    # ── Resumen ──
    resumen = f"""
=== DATOS DE MERCADO EN TIEMPO REAL ===
Fecha/Hora: {datetime.now().strftime("%Y-%m-%d %H:%M")}
Par: {symbol}

━━━ PRECIO ━━━━━━━━━━━━━━━━━━━━━━━━━━━
Actual:      ${precio_actual:,.2f} USDT
{('Cambio 24h:  ' + f"{precio['cambio_24h']:+.2f}%") if precio['cambio_24h'] is not None else 'Cambio 24h:  Sin datos'}
Alto 24h:    ${precio['alto_24h']:,.2f}
Bajo 24h:    ${precio['bajo_24h']:,.2f}
Volumen 24h: ${precio['volumen_24h']:,.0f} USDT

━━━ TENDENCIA MACRO ━━━━━━━━━━━━━━━━━━
EMA 200 Daily: {"$" + f"{ema200d:,.2f}" if ema200d else "Insuficientes datos"}
Dirección:     {tendencia_macro}

━━━ EMAs — 4H ━━━━━━━━━━━━━━━━━━━━━━━━
EMA 5:    ${ema5:,.2f}   {"↑ sobre" if precio_actual > ema5   else "↓ bajo"} EMA5
EMA 10:   ${ema10:,.2f}  {"↑ sobre" if precio_actual > ema10  else "↓ bajo"} EMA10
EMA 21:   ${ema21:,.2f}  {"↑ sobre" if precio_actual > ema21  else "↓ bajo"} EMA21
EMA 50:   ${ema50:,.2f}  {"↑ sobre" if precio_actual > ema50  else "↓ bajo"} EMA50
EMA 200:  {"$" + f"{ema200:,.2f}" + ("  ↑ sobre EMA200" if precio_actual > ema200 else "  ↓ bajo EMA200") if ema200 else "Insuficientes datos"}

━━━ RSI 62 (SMA 14) — 4H ━━━━━━━━━━━━━
Valor:   {rsi_4h if rsi_4h else "Sin datos"}
Lectura: {interpretar_rsi(rsi_4h)}
Zona:    {"⬆️ Sobre 60" if rsi_4h and rsi_4h > 60 else "⬇️ Bajo 40" if rsi_4h and rsi_4h < 40 else "↔️ Neutro 40-60"}

━━━ FUNDING RATE ━━━━━━━━━━━━━━━━━━━━━
Valor:   {f"{funding:+.4f}%" if funding is not None else "Sin datos"}
Lectura: {interpretar_funding(funding)}

━━━ OPEN INTEREST ━━━━━━━━━━━━━━━━━━━━
Valor:      {f"${oi['valor']:,.2f}" if oi['valor'] is not None else "Sin datos"}
Cambio 4H:  {f"{oi['cambio_4h']:+.2f}%" if oi['cambio_4h'] is not None else "Sin datos"}
Cambio 24H: {f"{oi['cambio_24h']:+.2f}%" if oi['cambio_24h'] is not None else "Sin datos"}
Lectura:    {interpretar_oi(oi['cambio_4h'], oi['cambio_24h'], precio['cambio_24h'] or 0)}

━━━ VELAS 4H RECIENTES ━━━━━━━━━━━━━━━
{"Fecha":<18} {"Open":>10} {"High":>10} {"Low":>10} {"Close":>10}
{"-"*62}"""

    for v in velas4h[-5:]:
        color = "🟢" if v["close"] > v["open"] else "🔴"
        resumen += f"\n{color} {v['fecha']:<16} ${v['open']:>9,.0f} ${v['high']:>9,.0f} ${v['low']:>9,.0f} ${v['close']:>9,.0f}"

    resumen += f"""

━━━ VELAS DAILY RECIENTES ━━━━━━━━━━━━
{"Fecha":<18} {"Open":>10} {"High":>10} {"Low":>10} {"Close":>10}
{"-"*62}"""

    for v in velas1d[-5:]:
        color = "🟢" if v["close"] > v["open"] else "🔴"
        resumen += f"\n{color} {v['fecha']:<16} ${v['open']:>9,.0f} ${v['high']:>9,.0f} ${v['low']:>9,.0f} ${v['close']:>9,.0f}"

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

        return {
            "regimen":         regimen,
            "emoji":           emoji,
            "sesgo":           sesgo_texto,
            "bloque_contexto": bloque_contexto,
            "precio":          precio,
            "ema200d":         ema200d,
            "rsi":             rsi,
            "funding":         funding,
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


def calcular_kelly(p_win: float, rr: float) -> dict:
    """
    Criterio de Kelly — tamaño óptimo de posición.

    Kelly% = (P_win × RR - P_loss) / RR
           = (P_win × RR - (1 - P_win)) / RR

    Donde:
        P_win = probabilidad de ganar (0-1)
        RR    = Risk:Reward ratio
        P_loss = 1 - P_win

    Kelly fraccional (recomendado): Kelly% / 2
    Nunca usar Kelly% completo — demasiado agresivo para crypto

    Retorna:
        kelly_pct      : % óptimo teórico (0-100)
        kelly_fraccional: % recomendado = kelly/2
        interpretacion : texto para el usuario
    """
    try:
        p_loss = 1 - p_win
        kelly_decimal = (p_win * rr - p_loss) / rr
        kelly_pct = round(kelly_decimal * 100, 1)
        kelly_frac = round(kelly_pct / 2, 1)

        if kelly_pct <= 0:
            interpretacion = "Edge negativo — esta estrategia pierde dinero a largo plazo."
        elif kelly_frac <= 1:
            interpretacion = f"Kelly fraccional: {kelly_frac}% del capital por trade. Edge muy pequeño."
        elif kelly_frac <= 5:
            interpretacion = f"Kelly fraccional: {kelly_frac}% del capital por trade. Edge moderado."
        else:
            interpretacion = f"Kelly fraccional: {kelly_frac}% del capital por trade. Edge sólido — no exceder."

        return {
            "kelly_pct":       kelly_pct,
            "kelly_fraccional": kelly_frac,
            "interpretacion":  interpretacion,
            "viable":          kelly_pct > 0,
        }
    except Exception:
        return {"kelly_pct": 0, "kelly_fraccional": 0, "interpretacion": "Error en cálculo", "viable": False}


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


def get_macro_contexto() -> str:
    """
    Bloque de contexto macro combinado DXY + BTC.D.
    Listo para inyectar en get_contexto_mercado() y get_regimen_mercado().
    """
    dxy  = get_dxy()
    btcd = get_btc_dominance()

    dxy_str  = f"{dxy['valor']:.2f}"  if dxy['valor']  else "Sin datos"
    btcd_str = f"{btcd['valor']:.2f}%" if btcd['valor'] else "Sin datos"

    return f"""
━━━ DXY + BTC DOMINANCE ━━━━━━━━━━━━━━━━━━
DXY (Dolar Index): {dxy_str}  {f"({dxy['cambio']:+.2f}% hoy)" if dxy['cambio'] is not None else ""}
Lectura:           {dxy['lectura']}

BTC Dominance:     {btcd_str}
Lectura:           {btcd['lectura']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""
