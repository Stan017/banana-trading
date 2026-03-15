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
