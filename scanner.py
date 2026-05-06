"""
scanner.py — Scanner HTF de confluencias para TradeBot
v3.0 — Sistema de scoring por capas (Macro + Edge + Técnico)
BTC = free | ETH, BNB, SOL = premium (futuro)
"""
from binance_data import (
    get_precio_actual, get_velas, get_funding_rate,
    get_open_interest, calcular_rsi, calcular_ema,
    interpretar_rsi, interpretar_funding, interpretar_oi,
    get_cvd, get_large_trades,
    get_dxy, get_btc_dominance, get_fear_greed,
    get_l2_liquidity, get_btc_dxy_correlation,
    get_long_short_ratio, get_funding_historia
)
from datetime import datetime, timezone, date as date_type
from analysis.ob_fvg    import detect_order_blocks, detect_fvg
from analysis.liquidity import detect_eqh_eql
import time

# ── Importar régimen cacheado de resources ───────────────────
try:
    from resources import get_regimen_cached
    _REGIMEN_DISPONIBLE = True
except Exception:
    _REGIMEN_DISPONIBLE = False

# ── FOMC 2025-2026 (fechas oficiales Fed) ────────────────────
FOMC_DATES = [
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-16",
]

# ── Config ──────────────────────────────────────────────────
ACTIVOS_FREE    = ["BTC/USDT"]
ACTIVOS_PREMIUM = ["ETH/USDT", "BNB/USDT", "SOL/USDT"]

# Thresholds de confluencia
RSI_ZONA_ALTA  = 60
RSI_ZONA_BAJA  = 40
FUNDING_SESGO  = 0.025   # por encima de esto ya no es neutro
EMAS_MINIMAS   = 3        # mínimo de EMAs alineadas sobre 5

# ══════════════════════════════════════════════════════════════
# CAPA 0 — RÉGIMEN MACRO (35 pts)
# Usa get_regimen_cached() — caché 10 min, cero llamadas extra
# Score es DIRECCIONAL: penaliza setups contratendencia macro
# ══════════════════════════════════════════════════════════════

def _score_macro(bias: str) -> tuple:
    """
    Retorna (score 0-35, regimen_str, detalle_str, extra_data_dict).

    Base: régimen alineado con bias (0-31 pts)
    DXY modifier:  ±3 — DXY subiendo penaliza ALCISTA, premia BAJISTA
    BTC.D modifier: ±2 — BTC.D alto premia ALCISTA en BTC, penaliza BAJISTA
    Resultado clampeado 0-35.
    """
    if not _REGIMEN_DISPONIBLE:
        return 17, "INDEFINIDO", "Régimen no disponible", {"hmm": {}, "onchain": {}, "vol": {}, "corr": {}}
    try:
        regimen_data = get_regimen_cached()
        regimen = regimen_data.get("regimen", "INDEFINIDO")
    except Exception:
        return 17, "INDEFINIDO", "Error obteniendo régimen", {"hmm": {}, "onchain": {}, "vol": {}, "corr": {}}

    # Base score por alineación régimen/bias (0-31 — deja margen para modifiers)
    if bias == "ALCISTA":
        score_map = {
            "ALCISTA_EXTREMO": 31, "ALCISTA": 25, "RANGO": 16,
            "BAJISTA": 5, "BAJISTA_EXTREMO": 0, "INDEFINIDO": 13,
        }
    else:  # BAJISTA
        score_map = {
            "BAJISTA_EXTREMO": 31, "BAJISTA": 25, "RANGO": 16,
            "ALCISTA": 5, "ALCISTA_EXTREMO": 0, "INDEFINIDO": 13,
        }

    base = score_map.get(regimen, 13)

    detalle_base = {
        31: "Régimen confirma dirección",
        25: "Régimen alineado",
        16: "Mercado en rango",
        5:  "Setup CONTRATENDENCIA",
        0:  "Régimen extremo contrario — bloqueado",
        13: "Régimen indefinido",
    }.get(base, f"Régimen: {regimen}")

    extras = []

    # ── DXY modifier (±3) ────────────────────────────────────
    dxy_adj = 0
    try:
        dxy = get_dxy()
        cambio = dxy.get("cambio")
        if cambio is not None and dxy.get("error") is None:
            if cambio >= 0.5:
                dxy_adj = -3 if bias == "ALCISTA" else +3
                extras.append(f"DXY {cambio:+.2f}% {'↓ penaliza' if bias == 'ALCISTA' else '↑ suma'}")
            elif cambio >= 0.2:
                dxy_adj = -1 if bias == "ALCISTA" else +1
                extras.append(f"DXY {cambio:+.2f}% leve")
            elif cambio <= -0.5:
                dxy_adj = +3 if bias == "ALCISTA" else -3
                extras.append(f"DXY {cambio:+.2f}% {'↑ suma' if bias == 'ALCISTA' else '↓ penaliza'}")
            elif cambio <= -0.2:
                dxy_adj = +1 if bias == "ALCISTA" else -1
                extras.append(f"DXY {cambio:+.2f}% leve")
    except Exception:
        pass

    # ── BTC.D modifier (±2) ──────────────────────────────────
    btcd_adj = 0
    try:
        btcd = get_btc_dominance()
        valor = btcd.get("valor")
        if valor is not None and btcd.get("error") is None:
            if valor >= 55:
                btcd_adj = +2 if bias == "ALCISTA" else -2
                extras.append(f"BTC.D {valor:.1f}% {'↑ capital en BTC' if bias == 'ALCISTA' else '↑ peso contrario'}")
            elif valor <= 45:
                btcd_adj = -2 if bias == "ALCISTA" else +2
                extras.append(f"BTC.D {valor:.1f}% {'↓ alt season' if bias == 'ALCISTA' else '↓ favorece short'}")
    except Exception:
        pass

    # ── BTC-DXY Correlation modifier (±2) ────────────────────
    # Correlación normal: inversa fuerte (< -0.5) = régimen estable, setup más fiable
    # Decorrelación / positiva = régimen cambiando, reduce convicción
    # Alerta: corr_30d girando positiva vs corr_90d = señal de ruptura
    corr_adj = 0
    corr_label = ""
    try:
        corr = get_btc_dxy_correlation()
        c30 = corr.get("corr_30d")
        c90 = corr.get("corr_90d")
        if c30 is not None and corr.get("error") is None:
            # Alerta de rotación — corr girando positiva vs histórico 90D
            rotacion = (c90 is not None and (c30 - c90) > 0.25)

            if c30 < -0.5:
                # Correlación inversa fuerte = normal, régimen claro → ligero boost
                corr_adj = +1
                corr_label = f"BTC-DXY {c30:+.2f} inversa fuerte — régimen estable"
            elif c30 < -0.2:
                # Inversa moderada → neutral
                corr_label = f"BTC-DXY {c30:+.2f} inversa moderada"
            elif c30 < 0.2:
                # Decorrelación — incertidumbre de régimen → penalizar
                corr_adj = -1
                corr_label = f"BTC-DXY {c30:+.2f} decorrelación — régimen cambiando"
            else:
                # Correlación positiva = risk-off generalizado, malo para ALCISTA
                corr_adj = -2 if bias == "ALCISTA" else 0
                corr_label = f"BTC-DXY {c30:+.2f} ⚠ correlación positiva — risk-off"

            if rotacion:
                corr_adj -= 1
                corr_label += " | rotando positiva vs 90D"

            if corr_label:
                extras.append(corr_label)
    except Exception:
        pass

    # ── HMM modifier (±3) ────────────────────────────────────────
    # Confirma/contradice el régimen determinístico desde datos aprendidos
    hmm_adj   = 0
    hmm_data  = {}
    try:
        from hmm_regime import get_regimen_hmm
        hmm_data  = get_regimen_hmm()
        hmm_estado = hmm_data.get("estado", "INDEFINIDO")
        hmm_conf   = hmm_data.get("confianza", 0.0)
        hmm_trans  = hmm_data.get("transicion", False)

        if hmm_estado != "INDEFINIDO" and hmm_conf >= 0.50:
            # HIGH_VOL siempre reduce — mercado impredecible
            if hmm_estado == "HIGH_VOL":
                hmm_adj = -2
                extras.append(f"HMM HIGH_VOL ({hmm_conf:.0%}) — sizing reducido")

            else:
                # Alineación HMM ↔ bias del setup
                hmm_alcista = hmm_estado == "ALCISTA"
                hmm_bajista = hmm_estado == "BAJISTA"
                setup_alcista = (bias == "ALCISTA")

                confirma = (setup_alcista and hmm_alcista) or (not setup_alcista and hmm_bajista)
                contradice = (setup_alcista and hmm_bajista) or (not setup_alcista and hmm_alcista)

                if confirma and hmm_conf >= 0.70:
                    hmm_adj = +3
                    extras.append(f"HMM confirma {hmm_estado} ({hmm_conf:.0%})")
                elif confirma:
                    hmm_adj = +1
                    extras.append(f"HMM alinea {hmm_estado} ({hmm_conf:.0%})")
                elif contradice:
                    hmm_adj = -3
                    extras.append(f"HMM contradice — detecta {hmm_estado} ({hmm_conf:.0%})")
                # LATERAL con setup direccional = neutral, sin modifier

            # Penalización adicional si hay transición reciente (señal inestable)
            if hmm_trans and hmm_adj != 0:
                hmm_adj -= 1
                extras.append("HMM transición reciente")

    except Exception:
        pass

    # ── On-Chain modifier (±3) ───────────────────────────────────────
    # aSOPR + NUPL + Exchange Flow vs dirección del setup
    oc_adj  = 0
    oc_data = {}
    try:
        from onchain import get_onchain_score
        precio_ref = None
        try:
            precio_ref = get_precio_actual("BTC/USDT").get("precio")
        except Exception:
            pass
        oc = get_onchain_score(bias, precio_actual=precio_ref)
        oc_adj  = oc["adj"]
        oc_data = oc.get("data", {})
        if oc["signals_ok"] > 0 and oc_adj != 0:
            extras.append(f"On-chain adj {oc_adj:+d}: {oc['label']}")
    except Exception:
        pass

    # ── Vol Surface modifier (±3) ────────────────────────────────────
    # DVOL HIGH/LOW + Term Structure BACKWARDATION + IV Skew extremo
    vol_adj  = 0
    vol_data = {}
    try:
        from deribit_vol import get_vol_score
        precio_vol = None
        try:
            precio_vol = get_precio_actual("BTC/USDT").get("precio")
        except Exception:
            pass
        vs = get_vol_score(bias, spot=precio_vol)
        vol_adj  = vs["adj"]
        vol_data = vs.get("data", {})
        if vs["signals_ok"] > 0 and vol_adj != 0:
            extras.append(f"Vol adj {vol_adj:+d}: {vs['label']}")
    except Exception:
        pass

    # ── Correlation modifier (±2) ─────────────────────────────────────
    # BTC-SPX alta = exposición riesgo equities | BTC-Gold >= 0.5 = safe haven bid
    corr_ma_adj  = 0
    corr_data    = {}
    try:
        from correlation import get_corr_score
        cs = get_corr_score(bias)
        corr_ma_adj = cs["adj"]
        corr_data   = cs.get("data", {})
        if cs["signals_ok"] > 0 and corr_ma_adj != 0:
            extras.append(f"Corr adj {corr_ma_adj:+d}: {cs['label']}")
    except Exception:
        pass

    score = max(0, min(35, base + dxy_adj + btcd_adj + corr_adj + hmm_adj + oc_adj + vol_adj + corr_ma_adj))
    detalle = detalle_base
    if extras:
        detalle += " | " + " | ".join(extras)

    return score, regimen, detalle, {"hmm": hmm_data, "onchain": oc_data, "vol": vol_data, "corr": corr_data}


# ══════════════════════════════════════════════════════════════
# CAPA 1 — EDGE ANALYTICS (25 pts)
# Kill zone: matemática pura (hora UTC), sin caché, sin API
# FOMC: fechas estáticas, sin API
# Volatilidad: calculada desde closes1d ya fetcheados en Capa 2
# ══════════════════════════════════════════════════════════════

def _kill_zone_score() -> tuple:
    """Retorna (score 0-10, sesion_str). Costo: cero — solo hora UTC."""
    hora = datetime.now(timezone.utc).hour
    # Overlap London/NY es la ventana de mayor liquidez
    if 13 <= hora < 17:
        return 10, "London/NY Overlap"
    elif 8 <= hora < 13:
        return 8, "London Open"
    elif 17 <= hora < 22:
        return 6, "NY Session"
    elif 0 <= hora < 3:
        return 5, "Asia Open"
    else:
        return 2, "Off-hours"


def _fomc_score() -> tuple:
    """Retorna (score 0-8, detalle_str). Costo: cero — fechas estáticas."""
    hoy = datetime.now(timezone.utc).date()
    min_delta = min(
        abs((date_type.fromisoformat(d) - hoy).days)
        for d in FOMC_DATES
    )
    if min_delta <= 1:
        return 0, f"⚠️ FOMC en {min_delta}d — señales degradadas"
    elif min_delta <= 3:
        return 3, f"FOMC en {min_delta}d — precaución"
    else:
        return 8, f"Sin FOMC próximo ({min_delta}d)"


def _vol_score(closes1d: list) -> tuple:
    """
    Retorna (score 0-5, detalle_str).
    Volatilidad extrema >85%ile = erráticos. Muy baja <15%ile = fakeouts.
    """
    if len(closes1d) < 60:
        return 3, "Vol sin datos suficientes"

    changes = [
        abs(closes1d[i] - closes1d[i-1]) / closes1d[i-1] * 100
        for i in range(1, len(closes1d))
    ]
    recent_avg = sum(changes[-14:]) / 14
    sorted_full = sorted(changes)
    rank = sum(1 for x in sorted_full if x <= recent_avg) / len(sorted_full) * 100

    if rank > 85:
        return 1, f"Vol muy alta ({rank:.0f}%ile) — movimientos erráticos"
    elif rank > 70:
        return 3, f"Vol alta ({rank:.0f}%ile)"
    elif rank < 15:
        return 2, f"Vol muy baja ({rank:.0f}%ile) — liquidez débil"
    else:
        return 5, f"Vol normal ({rank:.0f}%ile)"


def _fng_score() -> tuple:
    """
    Fear & Greed Index — score 0-4 pts.
    Extremos del mercado (miedo o codicia) = momento de alta convicción.
    Neutral = menor edge estadístico.
    """
    try:
        fng = get_fear_greed()
        valor = fng.get("valor")
        if valor is None or fng.get("error"):
            return 2, "F&G sin datos"
        if valor <= 15 or valor >= 85:
            return 4, f"F&G {valor} — extremo, momento institucional"
        elif valor <= 25 or valor >= 75:
            return 3, f"F&G {valor} — {'miedo' if valor <= 25 else 'codicia'} fuerte"
        elif valor <= 40 or valor >= 60:
            return 2, f"F&G {valor} — {'miedo' if valor <= 40 else 'codicia'} moderado"
        else:
            return 1, f"F&G {valor} — neutro, menor edge"
    except Exception:
        return 2, "F&G sin datos"


def _score_vp(symbol: str, tf: str, precio: float, bias: str) -> tuple:
    """
    Retorna (modifier int, label str).
    Usa Volume Profile del TF actual para ajustar el score técnico.

    Posición del precio (mutuamente excluyentes, de mayor a menor prioridad):
      En LVN (±0.5%):          +10  — cruces rápidos, momentum esperado
      Cerca de VPOC (±0.3%):   +3   — fair value, posible reversión
      Dentro de VAL-VAH:        +5   — value area, comportamiento predecible

    Bias modifier (independiente de posición):
      Sobre VAH + ALCISTA:     +5  |  Sobre VAH + BAJISTA:    -5
      Bajo VAL  + BAJISTA:     +5  |  Bajo VAL  + ALCISTA:    -5
    """
    try:
        from analysis.volume_profile import get_volume_profile
        vp = get_volume_profile(symbol, tf)
        if vp.get("error") or not vp.get("vpoc"):
            return 0, "VP sin datos"

        vpoc = vp["vpoc"]
        vah  = vp["value_area_high"]
        val  = vp["value_area_low"]
        lvn  = vp.get("lvn", [])

        modifier = 0
        partes   = [f"VPOC ${vpoc:,.0f}"]

        # ── Posición del precio ───────────────────────────────
        en_lvn  = any(abs(precio - l) / precio * 100 < 0.5 for l in lvn)
        en_vpoc = abs(precio - vpoc) / precio * 100 < 0.3

        if en_lvn:
            modifier += 10
            partes.append("en LVN +10")
        elif en_vpoc:
            modifier += 3
            partes.append("en VPOC +3")
        elif val <= precio <= vah:
            modifier += 5
            partes.append("en Value Area +5")

        # ── Bias modifier ─────────────────────────────────────
        if precio > vah:
            adj = +5 if bias == "ALCISTA" else -5
            modifier += adj
            partes.append(f"sobre VAH {adj:+d}")
        elif precio < val:
            adj = +5 if bias == "BAJISTA" else -5
            modifier += adj
            partes.append(f"bajo VAL {adj:+d}")

        return modifier, "VP: " + " | ".join(partes)

    except Exception as e:
        return 0, f"VP error: {e}"


def _score_delta(symbol: str, tf: str, bias: str) -> tuple:
    """
    Retorna (modifier int, label str).
    Penaliza el score técnico cuando el delta de velas contradice el bias.

    Distribución acelerando + bias ALCISTA:  -5 (señal de venta oculta)
    Acumulación acelerando + bias BAJISTA:   -5 (señal de compra oculta)
    Delta sostenido contrario al bias:       -2
    Delta confirma o es mixto:               0
    """
    try:
        from analysis.delta import get_delta_per_candle
        velas_delta = get_delta_per_candle(symbol, tf, n=3)
        if len(velas_delta) < 2:
            return 0, "Delta sin datos"

        valores = [d["delta"] for d in velas_delta]
        todos_neg      = all(v < 0 for v in valores)
        todos_pos      = all(v > 0 for v in valores)
        acelerando_neg = todos_neg and abs(valores[-1]) > abs(valores[0])
        acelerando_pos = todos_pos and abs(valores[-1]) > abs(valores[0])

        if acelerando_neg and bias == "ALCISTA":
            return -5, "Delta distribución acelerando ⚠ contradice ALCISTA"
        elif acelerando_pos and bias == "BAJISTA":
            return -5, "Delta acumulación acelerando ⚠ contradice BAJISTA"
        elif todos_neg and bias == "ALCISTA":
            return -2, "Delta negativo sostenido vs bias ALCISTA"
        elif todos_pos and bias == "BAJISTA":
            return -2, "Delta positivo sostenido vs bias BAJISTA"
        elif acelerando_neg and bias == "BAJISTA":
            return 0, "Delta distribución confirma BAJISTA"
        elif acelerando_pos and bias == "ALCISTA":
            return 0, "Delta acumulación confirma ALCISTA"
        else:
            return 0, "Delta mixto"

    except Exception:
        return 0, "Delta sin datos"


def _score_edge(closes1d: list) -> tuple:
    """
    Retorna (score 0-25, dict con desglose).
    Kill zone: 0-9 | FOMC: 0-7 | Vol: 0-5 | Fear&Greed: 0-4 = 25 max
    """
    kz_score, kz_label     = _kill_zone_score()
    fomc_score, fomc_label = _fomc_score()
    vol_score, vol_label   = _vol_score(closes1d)
    fng_score, fng_label   = _fng_score()

    # Escalar kill_zone a 0-9 (era 0-10)
    kz_score = min(9, kz_score)
    # Escalar fomc a 0-7 (era 0-8)
    fomc_score = min(7, fomc_score)

    total = kz_score + fomc_score + vol_score + fng_score
    return total, {
        "kill_zone": kz_label,   "kz_pts":   kz_score,
        "fomc":      fomc_label, "fomc_pts": fomc_score,
        "vol":       vol_label,  "vol_pts":  vol_score,
        "fng":       fng_label,  "fng_pts":  fng_score,
    }


# ══════════════════════════════════════════════════════════════
# SCORE TÉCNICO — Capa 2 (40 pts)
# Mapea el score 0-5 de confluencias al peso de 40 pts
# ══════════════════════════════════════════════════════════════

def _score_tecnico(score_conf: int, setup_ok: bool, setup_potencial: bool,
                   ob_modifier: int = 0, vp_modifier: int = 0, delta_modifier: int = 0) -> int:
    if setup_ok:          base = 40  # 8/8 mismo bias
    elif setup_potencial: base = 30  # 7/8 mismo bias
    elif score_conf == 7: base = 26
    elif score_conf == 6: base = 22
    elif score_conf == 5: base = 18
    elif score_conf == 4: base = 14
    elif score_conf == 3: base = 9
    elif score_conf == 2: base = 5
    elif score_conf == 1: base = 2
    else:                 base = 0
    return max(0, min(40, base + ob_modifier + vp_modifier + delta_modifier))


# ══════════════════════════════════════════════════════════════
# CONVICTION LABEL
# ══════════════════════════════════════════════════════════════

def _conviction_label(score_total: int) -> str:
    if score_total >= 85: return "INSTITUCIONAL"
    if score_total >= 70: return "ALTA"
    if score_total >= 55: return "MEDIA"
    return "BAJA"


# ── Evaluador de confluencias ────────────────────────────────

def evaluar_confluencias(symbol: str) -> dict:
    """
    Evalúa las 6 confluencias para un símbolo (RSI, Funding, OI, EMAs, CVD, L2).
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
        funding      = get_funding_rate(symbol)
        funding_hist = get_funding_historia(symbol)

        # ── L/S Ratio ──
        ls = get_long_short_ratio(symbol)

        # ── OI ──
        oi = get_open_interest(symbol)

        # ── Estructura de precio (OBs / FVGs / EQH-EQL) ──
        obs     = detect_order_blocks(velas4h, n=100, impulse_min=2)
        fvgs    = detect_fvg(velas4h, n=50)
        eqh_eql = detect_eqh_eql(velas4h, n=100, tolerancia=0.15, min_toques=2)

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
            hist_trend = funding_hist.get("tendencia", "sin datos")
            trend_str  = (f" | tendencia {hist_trend}"
                          if hist_trend not in ("sin datos", "estable") else "")
            if funding > 0:
                c2_bias    = "BAJISTA"
                c2_detalle = f"Funding +{funding:.4f}% — Retail LONG, sesgo bajista{trend_str}"
            else:
                c2_bias    = "ALCISTA"
                c2_detalle = f"Funding {funding:.4f}% — Retail SHORT, sesgo alcista{trend_str}"
        else:
            hist_trend = funding_hist.get("tendencia", "sin datos")
            c2_ok      = False
            c2_bias    = None
            c2_detalle = f"Funding {funding:.4f}% — Neutro ({hist_trend})"

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
        # CONFLUENCIA 5 — CVD confirma presión
        # ════════════════════════════════════
        cvd_data = get_cvd(symbol, tf="4h")
        cvd_bias_raw = cvd_data.get("cvd_bias", "neutral")
        cvd_div      = cvd_data.get("divergencia", False)
        cvd_error    = cvd_data.get("error")

        if cvd_error:
            c5_ok      = False
            c5_bias    = None
            c5_detalle = f"CVD sin datos ({cvd_error[:40]})"
        elif cvd_div:
            # Divergencia = precio y volumen no confirman → confluencia no aplica
            c5_ok      = False
            c5_bias    = None
            c5_detalle = "CVD divergente — precio y flujo no confirman"
        elif cvd_bias_raw == "bullish":
            c5_ok      = True
            c5_bias    = "ALCISTA"
            delta_str  = f"{cvd_data.get('delta_ultima', 0):+,.0f}" if cvd_data.get("delta_ultima") is not None else "?"
            c5_detalle = f"CVD alcista — flujo comprador neto (δ {delta_str} BTC)"
        elif cvd_bias_raw == "bearish":
            c5_ok      = True
            c5_bias    = "BAJISTA"
            delta_str  = f"{cvd_data.get('delta_ultima', 0):+,.0f}" if cvd_data.get("delta_ultima") is not None else "?"
            c5_detalle = f"CVD bajista — flujo vendedor neto (δ {delta_str} BTC)"
        else:
            c5_ok      = False
            c5_bias    = None
            c5_detalle = "CVD neutro — sin presión dominante"

        # ════════════════════════════════════
        # CONFLUENCIA 6 — L2 Pressure
        # Imbalance > 58% en un lado = presión real en el libro
        # Pared cercana (≤1.5%) en dirección del setup = soporte/resistencia real
        # ════════════════════════════════════
        l2 = get_l2_liquidity(symbol)
        l2_error = l2.get("error")

        if l2_error:
            c6_ok      = False
            c6_bias    = None
            c6_detalle = f"L2 sin datos ({l2_error[:40]})"
        else:
            imb     = l2.get("imbalance_pct", 50.0)
            imb_bias = l2.get("imbalance_bias", "neutral")
            nbid    = l2.get("nearest_bid_wall")
            nask    = l2.get("nearest_ask_wall")

            if imb_bias == "bid" and imb >= 58:
                c6_ok      = True
                c6_bias    = "ALCISTA"
                wall_str   = f" | soporte ${nbid['price']:,.0f} (${nbid['usd']/1e6:.1f}M)" if nbid else ""
                c6_detalle = f"L2 {imb:.0f}% bid — presión compradora{wall_str}"
            elif imb_bias == "ask" and imb <= 42:
                c6_ok      = True
                c6_bias    = "BAJISTA"
                wall_str   = f" | resistencia ${nask['price']:,.0f} (${nask['usd']/1e6:.1f}M)" if nask else ""
                c6_detalle = f"L2 {100-imb:.0f}% ask — presión vendedora{wall_str}"
            else:
                c6_ok      = False
                c6_bias    = None
                c6_detalle = f"L2 neutro {imb:.0f}% bid — sin presión dominante"

        # ════════════════════════════════════
        # CONFLUENCIA 7 — L/S Ratio
        # long_pct >= 65 = crowded long → sesgo BAJISTA
        # long_pct <= 35 = crowded short → sesgo ALCISTA
        # zona 40-60 = equilibrado, no cuenta como confluencia
        # ════════════════════════════════════
        ls_long  = ls.get("long_pct")
        ls_ratio = ls.get("ratio")

        if ls_long is None or ls.get("error"):
            c7_ok      = False
            c7_bias    = None
            c7_detalle = "L/S sin datos"
        elif ls_long >= 65:
            c7_ok      = True
            c7_bias    = "BAJISTA"
            c7_detalle = f"L/S {ls_ratio:.2f} — Crowded LONG ({ls_long:.1f}%) trampa bajista ⚠️"
        elif ls_long <= 35:
            c7_ok      = True
            c7_bias    = "ALCISTA"
            c7_detalle = f"L/S {ls_ratio:.2f} — Crowded SHORT ({ls_long:.1f}%) trampa alcista ⚠️"
        else:
            c7_ok      = False
            c7_bias    = None
            c7_detalle = f"L/S {ls_ratio:.2f} — Equilibrado ({ls_long:.1f}% longs) sin señal"

        # ════════════════════════════════════
        # CONFLUENCIA 8 — EQH/EQL magnético cercano
        # Zona 3+ toques dentro del 1.5% = imán de liquidez relevante
        # ════════════════════════════════════
        eqh_list = eqh_eql.get("eqh", [])
        eql_list = eqh_eql.get("eql", [])
        eqh_cercano = next((z for z in eqh_list if z["toques"] >= 3 and abs(z["distancia_pct"]) <= 1.5), None)
        eql_cercano = next((z for z in eql_list if z["toques"] >= 3 and abs(z["distancia_pct"]) <= 1.5), None)

        if eqh_cercano:
            c8_ok      = True
            c8_bias    = "BAJISTA"
            c8_detalle = f"EQH ${eqh_cercano['precio']:,.0f} — {eqh_cercano['toques']} toques a {eqh_cercano['distancia_pct']:+.2f}% (stop hunt bajista)"
        elif eql_cercano:
            c8_ok      = True
            c8_bias    = "ALCISTA"
            c8_detalle = f"EQL ${eql_cercano['precio']:,.0f} — {eql_cercano['toques']} toques a {eql_cercano['distancia_pct']:+.2f}% (stop hunt alcista)"
        else:
            c8_ok      = False
            c8_bias    = None
            c8_detalle = "Sin EQH/EQL con 3+ toques dentro del 1.5%"

        # ════════════════════════════════════
        # EVALUACIÓN FINAL — 8/8 + mismo bias
        # ════════════════════════════════════
        confluencias = [
            {"nombre": "RSI",     "ok": c1_ok, "bias": c1_bias, "detalle": c1_detalle},
            {"nombre": "Funding", "ok": c2_ok, "bias": c2_bias, "detalle": c2_detalle},
            {"nombre": "OI",      "ok": c3_ok, "bias": c3_bias, "detalle": c3_detalle},
            {"nombre": "EMAs",    "ok": c4_ok, "bias": c4_bias, "detalle": c4_detalle},
            {"nombre": "CVD",     "ok": c5_ok, "bias": c5_bias, "detalle": c5_detalle},
            {"nombre": "L2",      "ok": c6_ok, "bias": c6_bias, "detalle": c6_detalle},
            {"nombre": "L/S",     "ok": c7_ok, "bias": c7_bias, "detalle": c7_detalle},
            {"nombre": "EQH/EQL", "ok": c8_ok, "bias": c8_bias, "detalle": c8_detalle},
        ]

        todas_ok   = all(c["ok"] for c in confluencias)
        biases     = [c["bias"] for c in confluencias if c["bias"]]
        bias_final = biases[0] if biases else None
        mismo_bias = len(set(biases)) == 1 if biases else False
        score      = sum(1 for c in confluencias if c["ok"])

        setup_ok        = todas_ok and mismo_bias
        # 7/8 con mismo bias → setup potencial
        biases_ok       = [c["bias"] for c in confluencias if c["ok"] and c["bias"]]
        mismo_bias_ok   = len(set(biases_ok)) == 1 if biases_ok else False
        setup_potencial = (not setup_ok) and score == 7 and mismo_bias_ok

        # Qué confluencia falta para completar el setup
        falta = None
        if setup_potencial:
            falta_conf = [c for c in confluencias if not c["ok"]]
            if falta_conf:
                falta = falta_conf[0]["nombre"] + ": " + falta_conf[0]["detalle"]

        bias_resultado = bias_final if (setup_ok or setup_potencial) else None

        # ── OB modifier: ±3pts si hay OB no mitigado alineado con bias dentro del 3% ──
        ob_modifier = 0
        if bias_resultado:
            for ob in obs:
                if abs(ob["distancia_pct"]) <= 3.0:
                    if bias_resultado == "ALCISTA" and ob["tipo"] == "alcista":
                        ob_modifier = 3
                        break
                    elif bias_resultado == "BAJISTA" and ob["tipo"] == "bajista":
                        ob_modifier = 3
                        break
                    elif (bias_resultado == "ALCISTA" and ob["tipo"] == "bajista") or \
                         (bias_resultado == "BAJISTA" and ob["tipo"] == "alcista"):
                        ob_modifier = -3
                        break

        # ════════════════════════════════════
        # SCORING POR CAPAS (v3)
        # Solo se calcula si hay bias — sin setup no tiene sentido puntuar
        # ════════════════════════════════════
        if bias_resultado:
            s_macro, regimen_str, macro_detalle, _xd = _score_macro(bias_resultado)
            hmm_data  = _xd.get("hmm", {})
            oc_data   = _xd.get("onchain", {})
            vol_data  = _xd.get("vol", {})
            corr_data = _xd.get("corr", {})
            s_edge, edge_desglose               = _score_edge(closes1d)
            vp_mod,    _vp_lbl    = _score_vp(symbol, "4h", precio, bias_resultado)
            delta_mod, _delta_lbl = _score_delta(symbol, "4h", bias_resultado)
            s_tec                               = _score_tecnico(score, setup_ok, setup_potencial, ob_modifier, vp_mod, delta_mod)
            score_total                         = s_macro + s_edge + s_tec
            conviction                          = _conviction_label(score_total)
        else:
            # Sin setup técnico — igual calculamos contexto para mostrarlo en UI
            try:
                from resources import get_regimen_cached as _get_reg
                regimen_str = _get_reg().get("regimen", "INDEFINIDO")
            except Exception:
                regimen_str = "INDEFINIDO"
            _, edge_desglose = _score_edge(closes1d)
            s_macro      = 0
            macro_detalle = "Sin setup técnico"
            s_edge       = 0
            s_tec        = 0
            score_total  = 0
            conviction   = "BAJA"
            hmm_data     = {}
            oc_data      = {}
            vol_data     = {}
            corr_data    = {}

        # Alerta válida: necesita setup técnico Y convicción mínima ALTA (≥70)
        alerta_valida = (setup_ok or setup_potencial) and score_total >= 70

        return {
            # ── Datos de mercado ──
            "symbol":           symbol,
            "precio":           precio,
            "cambio_24h":       cambio_24h,
            "rsi":              rsi,
            "funding":          funding,
            "oi_4h":            oi_4h,
            "ema200d":          ema200d,
            # ── Confluencias técnicas (Capa 2) ──
            "confluencias":     confluencias,
            "setup_ok":         setup_ok,
            "setup_potencial":  setup_potencial,
            "falta":            falta,
            "bias":             bias_resultado,
            "score":            score,
            # ── Scoring por capas (v3) ──
            "score_macro":      s_macro,
            "score_edge":       s_edge,
            "score_tecnico":    s_tec,
            "score_total":      score_total,
            "conviction":       conviction,
            "alerta_valida":    alerta_valida,
            "regimen":          regimen_str,
            "edge_desglose":    edge_desglose,
            "macro_detalle":    macro_detalle,
            # ── Order Flow (CVD) ──
            "cvd_bias":         cvd_bias_raw,
            "cvd_divergencia":  cvd_div,
            "cvd_delta":        cvd_data.get("delta_ultima"),
            # ── Posicionamiento (L/S) ──
            "ls_ratio":         ls_ratio,
            "ls_long_pct":      ls_long,
            # ── Estructura de precio ──
            "obs":              obs,
            "fvgs":             fvgs,
            "eqh_eql":          eqh_eql,
            # ── HMM ──
            "hmm":              hmm_data,
            # ── On-Chain ──
            "onchain":          oc_data,
            # ── Volatility Surface ──
            "vol":              vol_data,
            # ── Correlations ──
            "corr":             corr_data,
            # ── Meta ──
            "timestamp":        datetime.now().strftime("%Y-%m-%d %H:%M"),
            "error":            None,
        }

    except Exception as e:
        return {
            "symbol": symbol, "setup_ok": False,
            "error": str(e), "score": 0,
        }


def evaluar_confluencias_ltf(symbol: str, tf: str = "15m") -> dict:
    """
    Versión LTF del scorer — mismas 8 confluencias pero con parámetros
    adecuados para timeframes cortos (15m / 1h).

    Diferencias vs evaluar_confluencias (4H):
      - RSI periodo 14 (no 62)
      - Velas del TF solicitado (no 4H fijo)
      - CVD del TF solicitado
      - Sin ema200d (datos diarios no relevantes para LTF scorer)
    """
    rsi_periodo = 14 if tf in ("15m", "1h") else 21

    try:
        precio_data = get_precio_actual(symbol)
        velas_tf    = get_velas(symbol, tf, 200)
        velas1d     = get_velas(symbol, "1d", 210)
        closes_tf   = [v["close"] for v in velas_tf]
        closes1d    = [v["close"] for v in velas1d]
        precio      = precio_data["precio"]
        cambio_24h  = precio_data["cambio_24h"] or 0

        ema5   = calcular_ema(closes_tf, 5)
        ema10  = calcular_ema(closes_tf, 10)
        ema21  = calcular_ema(closes_tf, 21)
        ema50  = calcular_ema(closes_tf, 50)
        ema200 = calcular_ema(closes_tf, 200)

        rsi          = calcular_rsi(closes_tf, periodo=rsi_periodo, suavizado=3)
        funding      = get_funding_rate(symbol)
        funding_hist = get_funding_historia(symbol)
        ls           = get_long_short_ratio(symbol)
        oi           = get_open_interest(symbol)
        obs          = detect_order_blocks(velas_tf, n=100, impulse_min=2)
        fvgs         = detect_fvg(velas_tf, n=50)
        eqh_eql      = detect_eqh_eql(velas_tf, n=100, tolerancia=0.15, min_toques=2)

        # ── Las 8 confluencias — idénticas al HTF ──
        # C1 — RSI
        if rsi is None:
            c1_ok, c1_bias, c1_detalle = False, None, "RSI sin datos"
        elif rsi >= RSI_ZONA_ALTA:
            c1_ok, c1_bias, c1_detalle = True, "BAJISTA", f"RSI {rsi} — Zona alta ≥{RSI_ZONA_ALTA}"
        elif rsi <= RSI_ZONA_BAJA:
            c1_ok, c1_bias, c1_detalle = True, "ALCISTA", f"RSI {rsi} — Zona baja ≤{RSI_ZONA_BAJA}"
        else:
            c1_ok, c1_bias, c1_detalle = False, None, f"RSI {rsi} — Neutro"

        # C2 — Funding
        if funding is None:
            c2_ok, c2_bias, c2_detalle = False, None, "Funding sin datos"
        elif abs(funding) > FUNDING_SESGO:
            hist_trend = funding_hist.get("tendencia", "sin datos")
            trend_str  = f" | {hist_trend}" if hist_trend not in ("sin datos", "estable") else ""
            if funding > 0:
                c2_ok, c2_bias, c2_detalle = True, "BAJISTA", f"Funding +{funding:.4f}%{trend_str}"
            else:
                c2_ok, c2_bias, c2_detalle = True, "ALCISTA", f"Funding {funding:.4f}%{trend_str}"
        else:
            c2_ok, c2_bias, c2_detalle = False, None, f"Funding {funding:.4f}% — Neutro"

        # C3 — OI
        oi_4h = oi.get("cambio_4h")
        if oi_4h is None:
            c3_ok, c3_bias, c3_detalle = False, None, "OI sin datos"
        else:
            precio_sube, oi_sube = cambio_24h > 0, oi_4h > 0
            if precio_sube and oi_sube:
                c3_ok, c3_bias, c3_detalle = True, "ALCISTA", f"OI↑ {oi_4h:+.2f}% + Precio↑"
            elif not precio_sube and not oi_sube:
                c3_ok, c3_bias, c3_detalle = True, "BAJISTA", f"OI↓ {oi_4h:+.2f}% + Precio↓"
            elif precio_sube and not oi_sube:
                c3_ok, c3_bias, c3_detalle = False, None, f"OI↓ {oi_4h:+.2f}% + Precio↑ — frágil"
            else:
                c3_ok, c3_bias, c3_detalle = False, None, f"OI↑ {oi_4h:+.2f}% + Precio↓ — mixto"

        # C4 — EMAs
        emas = [e for e in [ema5, ema10, ema21, ema50, ema200] if e is not None]
        if len(emas) < EMAS_MINIMAS:
            c4_ok, c4_bias, c4_detalle = False, None, "EMAs insuficientes"
        else:
            sobre = sum(1 for e in emas if precio > e)
            bajo  = sum(1 for e in emas if precio < e)
            if sobre >= EMAS_MINIMAS:
                c4_ok, c4_bias, c4_detalle = True, "ALCISTA", f"Precio sobre {sobre}/{len(emas)} EMAs"
            elif bajo >= EMAS_MINIMAS:
                c4_ok, c4_bias, c4_detalle = True, "BAJISTA", f"Precio bajo {bajo}/{len(emas)} EMAs"
            else:
                c4_ok, c4_bias, c4_detalle = False, None, f"EMAs mixtas — sin alineación"

        # C5 — CVD
        cvd_data     = get_cvd(symbol, tf=tf)
        cvd_bias_raw = cvd_data.get("cvd_bias", "neutral")
        cvd_div      = cvd_data.get("divergencia", False)
        if cvd_data.get("error"):
            c5_ok, c5_bias, c5_detalle = False, None, "CVD sin datos"
        elif cvd_div:
            c5_ok, c5_bias, c5_detalle = False, None, "CVD divergente"
        elif cvd_bias_raw == "bullish":
            delta_str = f"{cvd_data.get('delta_ultima', 0):+,.0f}" if cvd_data.get("delta_ultima") is not None else "?"
            c5_ok, c5_bias, c5_detalle = True, "ALCISTA", f"CVD alcista (δ {delta_str})"
        elif cvd_bias_raw == "bearish":
            delta_str = f"{cvd_data.get('delta_ultima', 0):+,.0f}" if cvd_data.get("delta_ultima") is not None else "?"
            c5_ok, c5_bias, c5_detalle = True, "BAJISTA", f"CVD bajista (δ {delta_str})"
        else:
            c5_ok, c5_bias, c5_detalle = False, None, "CVD neutro"

        # C6 — L2
        l2 = get_l2_liquidity(symbol)
        if l2.get("error"):
            c6_ok, c6_bias, c6_detalle = False, None, "L2 sin datos"
        else:
            imb      = l2.get("imbalance_pct", 50.0)
            imb_bias = l2.get("imbalance_bias", "neutral")
            nbid     = l2.get("nearest_bid_wall")
            nask     = l2.get("nearest_ask_wall")
            if imb_bias == "bid" and imb >= 58:
                wall_str = f" | soporte ${nbid['price']:,.0f}" if nbid else ""
                c6_ok, c6_bias, c6_detalle = True, "ALCISTA", f"L2 {imb:.0f}% bid{wall_str}"
            elif imb_bias == "ask" and imb <= 42:
                wall_str = f" | resist ${nask['price']:,.0f}" if nask else ""
                c6_ok, c6_bias, c6_detalle = True, "BAJISTA", f"L2 {100-imb:.0f}% ask{wall_str}"
            else:
                c6_ok, c6_bias, c6_detalle = False, None, f"L2 neutro {imb:.0f}% bid"

        # C7 — L/S
        ls_long  = ls.get("long_pct")
        ls_ratio = ls.get("ratio")
        if ls_long is None or ls.get("error"):
            c7_ok, c7_bias, c7_detalle = False, None, "L/S sin datos"
        elif ls_long >= 65:
            c7_ok, c7_bias, c7_detalle = True, "BAJISTA", f"L/S {ls_ratio:.2f} — Crowded LONG ({ls_long:.1f}%)"
        elif ls_long <= 35:
            c7_ok, c7_bias, c7_detalle = True, "ALCISTA", f"L/S {ls_ratio:.2f} — Crowded SHORT ({ls_long:.1f}%)"
        else:
            c7_ok, c7_bias, c7_detalle = False, None, f"L/S {ls_ratio:.2f} — Equilibrado"

        # C8 — EQH/EQL
        eqh_list = eqh_eql.get("eqh", [])
        eql_list = eqh_eql.get("eql", [])
        eqh_c    = next((z for z in eqh_list if z["toques"] >= 3 and abs(z["distancia_pct"]) <= 1.5), None)
        eql_c    = next((z for z in eql_list if z["toques"] >= 3 and abs(z["distancia_pct"]) <= 1.5), None)
        if eqh_c:
            c8_ok, c8_bias, c8_detalle = True, "BAJISTA", f"EQH ${eqh_c['precio']:,.0f} — {eqh_c['toques']} toques"
        elif eql_c:
            c8_ok, c8_bias, c8_detalle = True, "ALCISTA", f"EQL ${eql_c['precio']:,.0f} — {eql_c['toques']} toques"
        else:
            c8_ok, c8_bias, c8_detalle = False, None, "Sin EQH/EQL relevante"

        confluencias = [
            {"nombre": "RSI",     "ok": c1_ok, "bias": c1_bias, "detalle": c1_detalle},
            {"nombre": "Funding", "ok": c2_ok, "bias": c2_bias, "detalle": c2_detalle},
            {"nombre": "OI",      "ok": c3_ok, "bias": c3_bias, "detalle": c3_detalle},
            {"nombre": "EMAs",    "ok": c4_ok, "bias": c4_bias, "detalle": c4_detalle},
            {"nombre": "CVD",     "ok": c5_ok, "bias": c5_bias, "detalle": c5_detalle},
            {"nombre": "L2",      "ok": c6_ok, "bias": c6_bias, "detalle": c6_detalle},
            {"nombre": "L/S",     "ok": c7_ok, "bias": c7_bias, "detalle": c7_detalle},
            {"nombre": "EQH/EQL", "ok": c8_ok, "bias": c8_bias, "detalle": c8_detalle},
        ]

        biases      = [c["bias"] for c in confluencias if c["bias"]]
        bias_final  = biases[0] if biases else None
        score       = sum(1 for c in confluencias if c["ok"])
        biases_ok   = [c["bias"] for c in confluencias if c["ok"] and c["bias"]]
        mismo_bias  = len(set(biases_ok)) == 1 if biases_ok else False
        todas_ok    = all(c["ok"] for c in confluencias)
        setup_ok    = todas_ok and mismo_bias
        setup_potencial = (not setup_ok) and score == 7 and mismo_bias
        bias_resultado  = bias_final if (setup_ok or setup_potencial) else bias_final

        falta = None
        if setup_potencial:
            falta_conf = [c for c in confluencias if not c["ok"]]
            if falta_conf:
                falta = falta_conf[0]["nombre"] + ": " + falta_conf[0]["detalle"]

        ob_modifier = 0
        if bias_resultado:
            for ob in obs:
                if abs(ob["distancia_pct"]) <= 3.0:
                    if bias_resultado == "ALCISTA" and ob["tipo"] == "alcista":
                        ob_modifier = 3; break
                    elif bias_resultado == "BAJISTA" and ob["tipo"] == "bajista":
                        ob_modifier = 3; break
                    elif ((bias_resultado == "ALCISTA" and ob["tipo"] == "bajista") or
                          (bias_resultado == "BAJISTA" and ob["tipo"] == "alcista")):
                        ob_modifier = -3; break

        if bias_resultado:
            s_macro, regimen_str, macro_detalle, _xd = _score_macro(bias_resultado)
            hmm_data  = _xd.get("hmm", {})
            oc_data   = _xd.get("onchain", {})
            vol_data  = _xd.get("vol", {})
            corr_data = _xd.get("corr", {})
            s_edge, edge_desglose               = _score_edge(closes1d)
            vp_mod,    _   = _score_vp(symbol, tf, precio, bias_resultado)
            delta_mod, _   = _score_delta(symbol, tf, bias_resultado)
            s_tec           = _score_tecnico(score, setup_ok, setup_potencial, ob_modifier, vp_mod, delta_mod)
            score_total     = s_macro + s_edge + s_tec
            conviction      = _conviction_label(score_total)
        else:
            try:
                from resources import get_regimen_cached as _get_reg
                regimen_str = _get_reg().get("regimen", "INDEFINIDO")
            except Exception:
                regimen_str = "INDEFINIDO"
            _, edge_desglose = _score_edge(closes1d)
            s_macro = s_edge = s_tec = score_total = 0
            macro_detalle = "Sin setup técnico"
            conviction    = "BAJA"
            hmm_data      = {}
            oc_data       = {}
            vol_data      = {}
            corr_data     = {}

        return {
            "symbol": symbol, "tf": tf, "precio": precio,
            "confluencias": confluencias, "score": score,
            "setup_ok": setup_ok, "setup_potencial": setup_potencial,
            "falta": falta, "bias": bias_resultado,
            "score_macro": s_macro, "score_edge": s_edge, "score_tecnico": s_tec,
            "score_total": score_total, "conviction": conviction,
            "regimen": regimen_str, "edge_desglose": edge_desglose,
            "macro_detalle": macro_detalle,
            "cvd_bias": cvd_bias_raw, "cvd_divergencia": cvd_div,
            "obs": obs, "fvgs": fvgs, "eqh_eql": eqh_eql,
            "hmm": hmm_data,
            "onchain": oc_data,
            "vol": vol_data,
            "corr": corr_data,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "error": None,
        }

    except Exception as e:
        return {"symbol": symbol, "tf": tf, "setup_ok": False, "error": str(e), "score": 0}


def _reescalar_tf(raw: dict, pesos: tuple) -> tuple[int, int, int, int]:
    """
    Reescala sub-scores de un TF según pesos (mac, edg, tec).
    Retorna (score_total, s_mac, s_edg, s_tec).
    """
    mac_w, edg_w, tec_w = pesos
    s_mac = round(raw.get("score_macro",   0) / 35 * mac_w)
    s_edg = round(raw.get("score_edge",    0) / 25 * edg_w)
    s_tec = round(raw.get("score_tecnico", 0) / 40 * tec_w)
    return min(100, s_mac + s_edg + s_tec), s_mac, s_edg, s_tec


def _tf_payload(raw: dict, tf: str, score: int, s_mac: int, s_edg: int, s_tec: int) -> dict:
    return {
        "tf":              tf,
        "bias":            raw.get("bias"),
        "score":           score,
        "score_macro":     s_mac,
        "score_edge":      s_edg,
        "score_tecnico":   s_tec,
        "conviction":      _conviction_label(score),
        "confluencias_ok": raw.get("score", 0),
        "setup_ok":        raw.get("setup_ok", False),
        "falta":           raw.get("falta"),
    }


def evaluar_multitf(symbol: str = "BTC/USDT") -> dict:
    """
    Corre el scorer en 3 TFs en paralelo: HTF 4H + MTF 1H + LTF 15M.
    Pesos institucionales:
        HTF 4H  → original (Macro 35 / Edge 25 / Técnico 40)
        MTF 1H  → Macro 20 / Edge 20 / Técnico 60
        LTF 15M → Macro 15 / Edge 20 / Técnico 65
    Alineación: CONFLUENCIA TRIPLE / CONFLUENCIA / ESPERA / DIVERGENTE / INDEFINIDO
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as ex:
        fut_htf = ex.submit(evaluar_confluencias,     symbol)
        fut_mtf = ex.submit(evaluar_confluencias_ltf, symbol, "1h")
        fut_ltf = ex.submit(evaluar_confluencias_ltf, symbol, "15m")

    htf     = fut_htf.result()
    mtf_raw = fut_mtf.result()
    ltf_raw = fut_ltf.result()

    # ── Reescalar scores por TF ───────────────────────────────
    htf_score = htf.get("score_total", 0)
    mtf_score, s_mac_mtf, s_edg_mtf, s_tec_mtf = _reescalar_tf(mtf_raw, (20, 20, 60))
    ltf_score, s_mac_ltf, s_edg_ltf, s_tec_ltf = _reescalar_tf(ltf_raw, (15, 20, 65))

    htf_bias = htf.get("bias")
    mtf_bias = mtf_raw.get("bias")
    ltf_bias = ltf_raw.get("bias")

    htf_err  = bool(htf.get("error"))
    mtf_err  = bool(mtf_raw.get("error"))
    ltf_err  = bool(ltf_raw.get("error"))

    # ── Lógica de alineación 3 TFs ────────────────────────────
    def _all_same(*biases):
        valid = [b for b in biases if b]
        return len(valid) >= 2 and len(set(valid)) == 1

    if htf_err or (mtf_err and ltf_err):
        alineacion = "INDEFINIDO"
        trigger    = "Datos insuficientes para alineación multi-TF"

    elif _all_same(htf_bias, mtf_bias, ltf_bias) and htf_score >= 55 and mtf_score >= 55 and ltf_score >= 55:
        alineacion = "CONFLUENCIA TRIPLE"
        trigger    = f"4H + 1H + 15M {htf_bias} — máxima convicción, buscar entrada en 15M"

    elif _all_same(htf_bias, mtf_bias) and htf_score >= 55 and mtf_score >= 55:
        if ltf_err or not ltf_bias:
            alineacion = "ESPERA"
            trigger    = f"4H + 1H {htf_bias} confirmados — aguardar setup 15M"
        elif ltf_bias == htf_bias:
            alineacion = "CONFLUENCIA"
            trigger    = f"4H + 1H {htf_bias} fuertes + 15M alineando — entrada en confirmación"
        else:
            alineacion = "ESPERA"
            trigger    = f"4H + 1H {htf_bias} — 15M diverge ({ltf_bias}), aguardar flip"

    elif htf_bias and mtf_bias and htf_bias != mtf_bias:
        alineacion = "DIVERGENTE"
        trigger    = f"4H {htf_bias} contradice 1H {mtf_bias} — estructura en conflicto, no operar"

    elif _all_same(htf_bias, ltf_bias) and not mtf_bias:
        alineacion = "ESPERA"
        trigger    = f"4H + 15M {htf_bias} pero 1H sin setup — esperar confirmación intermedia"

    elif htf_bias and not mtf_bias and not ltf_bias:
        alineacion = "ESPERA"
        falta = htf.get("falta") or "setup en 1H"
        trigger = f"4H {htf_bias} definido — 1H y 15M sin confirmar. Esperar: {falta}"

    else:
        alineacion = "INDEFINIDO"
        trigger    = "Sin bias definido en 4H"

    return {
        "ok":        True,
        "symbol":    symbol,
        "htf":       _tf_payload(htf,     "4h",  htf_score,
                                 htf.get("score_macro", 0),
                                 htf.get("score_edge", 0),
                                 htf.get("score_tecnico", 0)),
        "mtf":       None if mtf_err else _tf_payload(mtf_raw, "1h",  mtf_score, s_mac_mtf, s_edg_mtf, s_tec_mtf),
        "ltf":       None if ltf_err else _tf_payload(ltf_raw, "15m", ltf_score, s_mac_ltf, s_edg_ltf, s_tec_ltf),
        "alineacion": alineacion,
        "trigger":    trigger,
        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M"),
        "error":      None,
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
    print(f"Bias:    {res.get('bias') or '—'}")
    print(f"\n── Scoring por capas ──────────────────")
    print(f"  Capa 0 Macro:   {res.get('score_macro',0):>3}/35  ({res.get('regimen','?')})")
    print(f"  Capa 1 Edge:    {res.get('score_edge',0):>3}/25  ({res.get('edge_desglose',{}).get('kill_zone','?')})")
    print(f"  Capa 2 Técnico: {res.get('score_tecnico',0):>3}/40  ({res.get('score',0)}/7 confluencias)")
    print(f"  ─────────────────────────────────────")
    print(f"  TOTAL:          {res.get('score_total',0):>3}/100 → {res.get('conviction','?')}")
    print(f"\nAlerta válida: {'✅ SÍ' if res.get('alerta_valida') else '❌ NO'}")
    print(f"\nConfluencias técnicas:")
    for c in res.get("confluencias", []):
        icono = "✅" if c["ok"] else "❌"
        print(f"  {icono} {c['nombre']}: {c['detalle']}")
    if res.get("edge_desglose"):
        e = res["edge_desglose"]
        print(f"\nEdge Analytics:")
        print(f"  Kill zone: {e.get('kill_zone')} ({e.get('kz_pts')} pts)")
        print(f"  FOMC:      {e.get('fomc')} ({e.get('fomc_pts')} pts)")
        print(f"  Vol:       {e.get('vol')} ({e.get('vol_pts')} pts)")


# ============================================================
# FASE 3 — BACKTESTING DEL SCANNER
# ============================================================

def backtest_scanner(symbol: str = "BTC/USDT", dias: int = 30) -> dict:
    """
    Backtesting del scanner sobre datos historicos 4H.
    Evalua RSI + EMAs (funding/OI no disponibles en historico).
    Cuando detecta setup, mira las 3 velas siguientes para calcular resultado.
    """
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
