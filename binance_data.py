import ccxt
from datetime import datetime

# ============================================================
# CONFIGURACIÓN — Sin API key, datos públicos gratis
# ============================================================
exchange      = ccxt.binance()                          # spot — precio, velas
exchange_fut  = ccxt.binance({                         # futuros — funding, OI
    'options': { 'defaultType': 'future' }
})

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
    """Precio actual con stats 24h"""
    ticker = exchange.fetch_ticker(symbol)
    return {
        "precio":      ticker["last"],
        "alto_24h":    ticker["high"],
        "bajo_24h":    ticker["low"],
        "volumen_24h": ticker["quoteVolume"],
        "cambio_24h":  ticker["percentage"],
    }

def get_velas(symbol="BTC/USDT", timeframe="4h", limite=220):
    """Velas OHLCV — 220 por defecto para tener historia suficiente para RSI 62"""
    velas = exchange.fetch_ohlcv(symbol, timeframe, limit=limite)
    return [{
        "fecha":   datetime.fromtimestamp(v[0]/1000).strftime("%Y-%m-%d %H:%M"),
        "open":    v[1],
        "high":    v[2],
        "low":     v[3],
        "close":   v[4],
        "volumen": v[5]
    } for v in velas]

def get_funding_rate(symbol="BTC/USDT"):
    """Funding rate actual de Binance Futures"""
    try:
        symbol_fut = symbol.replace("/", "")
        funding    = exchange_fut.fetch_funding_rate(symbol_fut)
        # FIX BUG 5: usar `is not None` en vez de `or` para no perder funding == 0.0
        rate = funding.get("fundingRate")
        if rate is None:
            rate = funding.get("lastFundingRate")
        return round(float(rate) * 100, 4) if rate is not None else None
    except Exception:
        return None

def get_open_interest(symbol="BTC/USDT"):
    """OI actual + cambio % en 4H y 24H"""
    try:
        symbol_fut = symbol.replace("/", "")
        oi_hist    = exchange_fut.fetch_open_interest_history(symbol_fut, "1h", limit=25)

        if oi_hist and len(oi_hist) >= 5:
            def oi_val(entry):
                return float(entry.get("openInterestAmount") or entry.get("openInterest") or 0)

            ahora   = oi_val(oi_hist[-1])
            hace4h  = oi_val(oi_hist[-5])
            hace24h = oi_val(oi_hist[0])

            cambio_4h  = round((ahora - hace4h)  / hace4h  * 100, 2) if hace4h  > 0 else 0
            cambio_24h = round((ahora - hace24h) / hace24h * 100, 2) if hace24h > 0 else 0

            return {
                "valor":      round(ahora, 2),
                "cambio_4h":  cambio_4h,
                "cambio_24h": cambio_24h,
            }
    except Exception:
        pass
    return {"valor": None, "cambio_4h": None, "cambio_24h": None}

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

        return {
            "regimen":         regimen,
            "emoji":           emoji,
            "sesgo":           sesgo_texto,
            "bloque_contexto": bloque_contexto.strip(),
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
