"""
scanner.py — Scanner HTF de confluencias para TradeBot
Solo genera alerta cuando hay 4/4 confluencias
BTC = free | ETH, BNB, SOL = premium (futuro)
"""
from binance_data import (
    get_precio_actual, get_velas, get_funding_rate,
    get_open_interest, calcular_rsi, calcular_ema,
    interpretar_rsi, interpretar_funding, interpretar_oi
)
from datetime import datetime

# ── Config ──────────────────────────────────────────────────
ACTIVOS_FREE    = ["BTC/USDT"]
ACTIVOS_PREMIUM = ["ETH/USDT", "BNB/USDT", "SOL/USDT"]

# Thresholds de confluencia
RSI_ZONA_ALTA  = 60
RSI_ZONA_BAJA  = 40
FUNDING_SESGO  = 0.025   # por encima de esto ya no es neutro
EMAS_MINIMAS   = 3        # mínimo de EMAs alineadas sobre 5

# ── Evaluador de confluencias ────────────────────────────────

def evaluar_confluencias(symbol: str) -> dict:
    """
    Evalúa las 4 confluencias para un símbolo.
    Retorna dict con resultado y detalle de cada confluencia.
    """
    try:
        precio_data = get_precio_actual(symbol)
        velas4h     = get_velas(symbol, "4h", 220)
        velas1d     = get_velas(symbol, "1d", 210)
        closes4h    = [v["close"] for v in velas4h]
        closes1d    = [v["close"] for v in velas1d]
        precio      = precio_data["precio"]
        cambio_24h  = precio_data["cambio_24h"] or 0

        # ── EMAs 4H ──
        ema5   = calcular_ema(closes4h, 5)
        ema10  = calcular_ema(closes4h, 10)
        ema21  = calcular_ema(closes4h, 21)
        ema50  = calcular_ema(closes4h, 50)
        ema200 = calcular_ema(closes4h, 200)
        ema200d = calcular_ema(closes1d, 200) if len(closes1d) >= 200 else None

        # ── RSI ──
        rsi = calcular_rsi(closes4h, periodo=62, suavizado=14)

        # ── Funding ──
        funding = get_funding_rate(symbol)

        # ── OI ──
        oi = get_open_interest(symbol)

        # ════════════════════════════════════
        # CONFLUENCIA 1 — RSI en zona extrema
        # ════════════════════════════════════
        if rsi is None:
            c1_ok    = False
            c1_bias  = None
            c1_detalle = "RSI sin datos"
        elif rsi >= RSI_ZONA_ALTA:
            c1_ok    = True
            c1_bias  = "BAJISTA"
            c1_detalle = f"RSI {rsi} — Zona alta ≥{RSI_ZONA_ALTA}"
        elif rsi <= RSI_ZONA_BAJA:
            c1_ok    = True
            c1_bias  = "ALCISTA"
            c1_detalle = f"RSI {rsi} — Zona baja ≤{RSI_ZONA_BAJA}"
        else:
            c1_ok    = False
            c1_bias  = None
            c1_detalle = f"RSI {rsi} — Neutro ({RSI_ZONA_BAJA}-{RSI_ZONA_ALTA})"

        # ════════════════════════════════════
        # CONFLUENCIA 2 — Funding con sesgo
        # ════════════════════════════════════
        if funding is None:
            c2_ok      = False
            c2_bias    = None
            c2_detalle = "Funding sin datos"
        elif abs(funding) > FUNDING_SESGO:
            c2_ok = True
            if funding > 0:
                c2_bias    = "BAJISTA"   # retail long → trampa bajista
                c2_detalle = f"Funding +{funding:.4f}% — Retail LONG, sesgo bajista"
            else:
                c2_bias    = "ALCISTA"   # retail short → trampa alcista
                c2_detalle = f"Funding {funding:.4f}% — Retail SHORT, sesgo alcista"
        else:
            c2_ok      = False
            c2_bias    = None
            c2_detalle = f"Funding {funding:.4f}% — Neutro, sin sesgo"

        # ════════════════════════════════════
        # CONFLUENCIA 3 — OI confirma dirección
        # ════════════════════════════════════
        oi_4h = oi.get("cambio_4h")
        if oi_4h is None:
            c3_ok      = False
            c3_bias    = None
            c3_detalle = "OI sin datos"
        else:
            precio_sube = cambio_24h > 0
            oi_sube     = oi_4h > 0
            if precio_sube and oi_sube:
                c3_ok      = True
                c3_bias    = "ALCISTA"
                c3_detalle = f"OI↑ {oi_4h:+.2f}% + Precio↑ — Capital nuevo entrando"
            elif not precio_sube and not oi_sube:
                c3_ok      = True
                c3_bias    = "BAJISTA"
                c3_detalle = f"OI↓ {oi_4h:+.2f}% + Precio↓ — Liquidaciones en cascada"
            elif precio_sube and not oi_sube:
                c3_ok      = False
                c3_bias    = None
                c3_detalle = f"OI↓ {oi_4h:+.2f}% + Precio↑ — Rebote frágil, sin confirmación"
            else:
                c3_ok      = False
                c3_bias    = None
                c3_detalle = f"OI↑ {oi_4h:+.2f}% + Precio↓ — Señal mixta"

        # ════════════════════════════════════
        # CONFLUENCIA 4 — EMAs alineadas
        # ════════════════════════════════════
        emas = [e for e in [ema5, ema10, ema21, ema50, ema200] if e is not None]
        if len(emas) < EMAS_MINIMAS:
            c4_ok      = False
            c4_bias    = None
            c4_detalle = "EMAs insuficientes"
        else:
            sobre = sum(1 for e in emas if precio > e)
            bajo  = sum(1 for e in emas if precio < e)
            if sobre >= EMAS_MINIMAS:
                c4_ok      = True
                c4_bias    = "ALCISTA"
                c4_detalle = f"Precio sobre {sobre}/{len(emas)} EMAs — alineación alcista"
            elif bajo >= EMAS_MINIMAS:
                c4_ok      = True
                c4_bias    = "BAJISTA"
                c4_detalle = f"Precio bajo {bajo}/{len(emas)} EMAs — alineación bajista"
            else:
                c4_ok      = False
                c4_bias    = None
                c4_detalle = f"EMAs mixtas ({sobre} sobre, {bajo} bajo) — sin alineación"

        # ════════════════════════════════════
        # EVALUACIÓN FINAL — 4/4 + mismo bias
        # ════════════════════════════════════
        confluencias = [
            {"nombre": "RSI",     "ok": c1_ok, "bias": c1_bias, "detalle": c1_detalle},
            {"nombre": "Funding", "ok": c2_ok, "bias": c2_bias, "detalle": c2_detalle},
            {"nombre": "OI",      "ok": c3_ok, "bias": c3_bias, "detalle": c3_detalle},
            {"nombre": "EMAs",    "ok": c4_ok, "bias": c4_bias, "detalle": c4_detalle},
        ]

        todas_ok    = all(c["ok"] for c in confluencias)
        biases      = [c["bias"] for c in confluencias if c["bias"]]
        bias_final  = biases[0] if biases else None
        mismo_bias  = len(set(biases)) == 1 if biases else False
        setup_ok    = todas_ok and mismo_bias

        return {
            "symbol":       symbol,
            "precio":       precio,
            "cambio_24h":   cambio_24h,
            "rsi":          rsi,
            "funding":      funding,
            "oi_4h":        oi_4h,
            "ema200d":      ema200d,
            "confluencias": confluencias,
            "setup_ok":     setup_ok,
            "bias":         bias_final if setup_ok else None,
            "score":        sum(1 for c in confluencias if c["ok"]),
            "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M"),
            "error":        None,
        }

    except Exception as e:
        return {
            "symbol": symbol, "setup_ok": False,
            "error": str(e), "score": 0,
        }


def escanear_free() -> list:
    """Escanea solo BTC — versión free"""
    resultados = []
    for symbol in ACTIVOS_FREE:
        resultado = evaluar_confluencias(symbol)
        resultados.append(resultado)
    return resultados


def escanear_premium() -> list:
    """Escanea ETH, BNB, SOL — versión premium"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    resultados = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futuros = {executor.submit(evaluar_confluencias, s): s
                   for s in ACTIVOS_PREMIUM}
        for f in as_completed(futuros):
            try:
                resultados.append(f.result(timeout=15))
            except Exception as e:
                resultados.append({"symbol": futuros[f], "error": str(e), "setup_ok": False})
    return resultados


# ── Test ─────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🔍 Escaneando BTC...\n")
    res = evaluar_confluencias("BTC/USDT")
    print(f"Symbol:  {res['symbol']}")
    print(f"Precio:  ${res['precio']:,.2f}")
    print(f"Score:   {res['score']}/4")
    print(f"Setup:   {'✅ ACTIVO' if res['setup_ok'] else '❌ Sin setup'}")
    if res['bias']:
        print(f"Bias:    {res['bias']}")
    print("\nConfluencias:")
    for c in res.get("confluencias", []):
        icono = "✅" if c["ok"] else "❌"
        print(f"  {icono} {c['nombre']}: {c['detalle']}")


# ============================================================
# FASE 3 — BACKTESTING DEL SCANNER
# ============================================================

def backtest_scanner(symbol: str = "BTC/USDT", dias: int = 30) -> dict:
    """
    Backtesting del scanner sobre datos historicos 4H.
    Evalua RSI + EMAs (funding/OI no disponibles en historico).
    Cuando detecta setup, mira las 3 velas siguientes para calcular resultado.
    """
    from binance_data import get_velas, calcular_ema, calcular_rsi

    try:
        velas_necesarias = min(dias * 6 + 220, 1000)
        velas4h = get_velas(symbol, "4h", velas_necesarias)
        velas1d = get_velas(symbol, "1d", 210)

        if len(velas4h) < 220:
            return {"ok": False, "error": "Datos historicos insuficientes",
                    "symbol": symbol, "dias": dias}

        n_backtest  = min(dias * 6, len(velas4h) - 220)
        velas_hist  = velas4h[:-n_backtest]
        velas_test  = velas4h[-n_backtest:]

        setups_detectados = []
        ventana_hist = list(velas_hist)

        for i, vela in enumerate(velas_test):
            ventana_hist.append(vela)
            closes = [v["close"] for v in ventana_hist]
            precio = vela["close"]

            if len(closes) < 205:
                continue

            ema5   = calcular_ema(closes, 5)
            ema10  = calcular_ema(closes, 10)
            ema21  = calcular_ema(closes, 21)
            ema50  = calcular_ema(closes, 50)
            ema200 = calcular_ema(closes, 200)
            rsi    = calcular_rsi(closes, periodo=62, suavizado=14)

            # C1: RSI en zona extrema
            c1_ok   = rsi is not None and (rsi >= 55 or rsi <= 45)
            c1_bias = "BAJISTA" if (rsi and rsi >= 55) else ("ALCISTA" if (rsi and rsi <= 45) else None)

            # C2: EMAs alineadas
            emas = [e for e in [ema5, ema10, ema21, ema50, ema200] if e is not None]
            c2_ok = False; c2_bias = None
            if len(emas) >= 3:
                sobre = sum(1 for e in emas if precio > e)
                bajo  = sum(1 for e in emas if precio < e)
                if sobre >= 3:
                    c2_ok = True; c2_bias = "ALCISTA"
                elif bajo >= 3:
                    c2_ok = True; c2_bias = "BAJISTA"

            if not (c1_ok and c2_ok and c1_bias == c2_bias):
                continue

            bias = c1_bias
            velas_sig = velas_test[i+1:i+4]
            if len(velas_sig) < 3:
                continue

            atr_simple   = abs(vela["high"] - vela["low"])
            max_sig      = max(v["high"] for v in velas_sig)
            min_sig      = min(v["low"]  for v in velas_sig)

            if bias == "ALCISTA":
                target   = precio + atr_simple * 1.5
                stoploss = precio - atr_simple * 1.0
                gano     = max_sig >= target
                perdio   = min_sig <= stoploss
            else:
                target   = precio - atr_simple * 1.5
                stoploss = precio + atr_simple * 1.0
                gano     = min_sig <= target
                perdio   = max_sig >= stoploss

            if gano and not perdio:
                resultado = "WIN"
            elif perdio and not gano:
                resultado = "LOSS"
            elif gano and perdio:
                resultado = "WIN"
            else:
                resultado = "NEUTRAL"

            setups_detectados.append({
                "fecha":    vela["fecha"],
                "precio":   precio,
                "bias":     bias,
                "rsi":      round(rsi, 1) if rsi else None,
                "resultado": resultado,
                "target":   round(target, 2),
                "stoploss": round(stoploss, 2),
            })

        total    = len(setups_detectados)
        wins     = sum(1 for s in setups_detectados if s["resultado"] == "WIN")
        losses   = sum(1 for s in setups_detectados if s["resultado"] == "LOSS")
        neutral  = sum(1 for s in setups_detectados if s["resultado"] == "NEUTRAL")
        win_rate = round((wins / total) * 100, 1) if total > 0 else 0

        alcistas = [s for s in setups_detectados if s["bias"] == "ALCISTA"]
        bajistas = [s for s in setups_detectados if s["bias"] == "BAJISTA"]
        wr_alc   = round(sum(1 for s in alcistas if s["resultado"]=="WIN") / len(alcistas) * 100, 1) if alcistas else 0
        wr_baj   = round(sum(1 for s in bajistas if s["resultado"]=="WIN") / len(bajistas) * 100, 1) if bajistas else 0

        return {
            "ok":                True,
            "symbol":            symbol,
            "dias":              dias,
            "velas_evaluadas":   n_backtest,
            "setups_detectados": total,
            "wins":              wins,
            "losses":            losses,
            "neutral":           neutral,
            "win_rate":          win_rate,
            "win_rate_alcistas": wr_alc,
            "win_rate_bajistas": wr_baj,
            "nota":              "Backtest con RSI + EMAs (funding/OI no disponibles en historico)",
            "setups":            setups_detectados[-10:],
        }

    except Exception as e:
        return {"ok": False, "error": str(e), "symbol": symbol, "dias": dias}


def formatear_backtest(resultado: dict) -> str:
    """Formatea el resultado del backtest como texto legible."""
    if not resultado.get("ok"):
        return f"Error en backtest: {resultado.get('error', 'desconocido')}"
    r = resultado
    lineas = [
        f"\n=== BACKTEST SCANNER - {r['symbol']} ({r['dias']} dias) ===",
        f"Velas evaluadas:   {r['velas_evaluadas']}",
        f"Setups detectados: {r['setups_detectados']}",
        f"Wins: {r['wins']} | Losses: {r['losses']} | Neutral: {r['neutral']}",
        f"Win Rate total:    {r['win_rate']}%",
        f"Win Rate ALCISTAS: {r['win_rate_alcistas']}%",
        f"Win Rate BAJISTAS: {r['win_rate_bajistas']}%",
        f"Nota: {r['nota']}",
        "\nUltimos setups:",
    ]
    for s in r.get("setups", [])[-5:]:
        icono = "OK" if s["resultado"] == "WIN" else "XX" if s["resultado"] == "LOSS" else "--"
        lineas.append(f"  [{icono}] {s['fecha']} | {s['bias']:<8} | RSI {s['rsi']} | precio ${s['precio']:,.0f}")
    lineas.append("=" * 50)
    return "\n".join(lineas)
