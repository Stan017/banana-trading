"""
analysis/volume_profile.py — Volume Profile: VPOC / HVN / LVN / Value Area
═════════════════════════════════════════════════════════════════════════════
Cálculo puro sobre los datos de velas existentes (sin API adicional).

VPOC  — Volume Point of Control: precio con más volumen negociado
HVN   — High Volume Node: zonas de aceptación (precio se mueve lento)
LVN   — Low Volume Node: zonas de rechazo (precio se mueve rápido)
VAH   — Value Area High: límite superior del 70% del volumen
VAL   — Value Area Low:  límite inferior del 70% del volumen

Jane Street:
  El precio tiende a regresar al VPOC (fair value).
  Sobre VAH = sobrecomprado relativo → buscar distribución.
  Bajo VAL  = sobrevendido relativo → buscar absorción.
  LVN = el precio los cruza rápido → SL placement óptimo.

Caché: 600s (se recalcula cada 10 min — intensivo en cómputo)

Interfaz:
  calcular_vp(velas, bins)        → dict con VPOC/HVN/LVN/VAH/VAL (función pura)
  get_volume_profile(symbol, tf)  → wrapper cacheado sobre calcular_vp
  format_vp_context(vp, precio)   → string compacto para el LLM
"""

from __future__ import annotations
import time
import logging

logger = logging.getLogger(__name__)

# Caché interno (TTL 600s)
_VP_CACHE: dict = {}
_VP_TTL = 600


# ── Función pura ─────────────────────────────────────────────────────────────

def calcular_vp(velas: list[dict], bins: int = 50) -> dict:
    """
    Calcula el Volume Profile desde una lista de velas OHLCV.
    Función pura: sin side effects, sin llamadas a API.

    Cada vela debe tener: high, low, volume (o vol).
    """
    if not velas or len(velas) < 5:
        return {"error": "Insuficientes velas para Volume Profile"}

    try:
        # Extraer high, low, volume de cada vela
        highs  = [float(v.get("high",  v.get("h", 0))) for v in velas]
        lows   = [float(v.get("low",   v.get("l", 0))) for v in velas]
        vols   = [float(v.get("volume", v.get("vol", v.get("v", 0)))) for v in velas]

        precio_min = min(lows)
        precio_max = max(highs)
        if precio_max <= precio_min:
            return {"error": "Rango de precio inválido"}

        rango     = precio_max - precio_min
        bin_size  = rango / bins

        # Inicializar histograma de volumen
        histogram = [0.0] * bins

        for i, vela in enumerate(velas):
            h   = highs[i]
            l   = lows[i]
            vol = vols[i]
            if vol <= 0:
                continue

            # Bins que toca esta vela
            bin_lo = int((l - precio_min) / bin_size)
            bin_hi = int((h - precio_min) / bin_size)
            bin_lo = max(0, min(bin_lo, bins - 1))
            bin_hi = max(0, min(bin_hi, bins - 1))

            n_bins_touched = (bin_hi - bin_lo) + 1
            vol_per_bin    = vol / n_bins_touched

            for b in range(bin_lo, bin_hi + 1):
                histogram[b] += vol_per_bin

        # VPOC — bin con mayor volumen
        vpoc_bin   = histogram.index(max(histogram))
        vpoc_price = precio_min + (vpoc_bin + 0.5) * bin_size

        # Value Area (70% del volumen total, acumulando desde VPOC hacia afuera)
        vol_total  = sum(histogram)
        va_target  = vol_total * 0.70
        va_vol     = histogram[vpoc_bin]
        lo_ptr     = vpoc_bin
        hi_ptr     = vpoc_bin

        while va_vol < va_target:
            can_go_lo = lo_ptr > 0
            can_go_hi = hi_ptr < bins - 1

            if not can_go_lo and not can_go_hi:
                break

            vol_lo = histogram[lo_ptr - 1] if can_go_lo else -1
            vol_hi = histogram[hi_ptr + 1] if can_go_hi else -1

            if vol_lo >= vol_hi and can_go_lo:
                lo_ptr -= 1
                va_vol += histogram[lo_ptr]
            elif can_go_hi:
                hi_ptr += 1
                va_vol += histogram[hi_ptr]
            else:
                lo_ptr -= 1
                va_vol += histogram[lo_ptr]

        val = precio_min + lo_ptr * bin_size
        vah = precio_min + (hi_ptr + 1) * bin_size

        # HVN y LVN — percentiles del histograma
        sorted_vols = sorted(histogram)
        p30 = sorted_vols[int(bins * 0.30)]
        p70 = sorted_vols[int(bins * 0.70)]

        hvn_prices = []
        lvn_prices = []
        for b, vol_b in enumerate(histogram):
            precio_centro = precio_min + (b + 0.5) * bin_size
            if vol_b >= p70:
                hvn_prices.append(round(precio_centro, 1))
            elif vol_b <= p30:
                lvn_prices.append(round(precio_centro, 1))

        # Compactar — agrupar niveles cercanos (dentro de 0.3% entre sí)
        def _compactar(niveles: list[float], tolerancia_pct: float = 0.3) -> list[float]:
            if not niveles:
                return []
            result = [niveles[0]]
            for p in niveles[1:]:
                if abs(p - result[-1]) / result[-1] * 100 > tolerancia_pct:
                    result.append(p)
            return result

        hvn_compacto = _compactar(hvn_prices)[:5]
        lvn_compacto = _compactar(lvn_prices)[:5]

        return {
            "vpoc":            round(vpoc_price, 1),
            "value_area_high": round(vah, 1),
            "value_area_low":  round(val, 1),
            "hvn":             hvn_compacto,
            "lvn":             lvn_compacto,
            "precio_min":      round(precio_min, 1),
            "precio_max":      round(precio_max, 1),
            "vol_total":       round(vol_total, 2),
            "error":           None,
        }

    except Exception as e:
        logger.error(f"Error calculando Volume Profile: {e}")
        return {"error": str(e)}


# ── Wrapper cacheado ──────────────────────────────────────────────────────────

def get_volume_profile(
    symbol: str = "BTC/USDT",
    tf: str = "4h",
    limit: int = 200,
    bins: int = 50,
) -> dict:
    """
    Volume Profile cacheado (600s) para un símbolo y TF.
    Obtiene velas desde binance_data y aplica calcular_vp.
    """
    ahora = time.time()
    key   = (symbol, tf, limit, bins)

    if key in _VP_CACHE and (ahora - _VP_CACHE[key]["ts"]) < _VP_TTL:
        return _VP_CACHE[key]["data"]

    try:
        from binance_data import get_velas
        velas = get_velas(symbol, tf, limit)
        if not velas:
            result = {"error": "Sin velas disponibles"}
        else:
            result = calcular_vp(velas, bins=bins)
    except Exception as e:
        result = {"error": str(e)}

    _VP_CACHE[key] = {"data": result, "ts": ahora}
    return result


# ── Formatter para el LLM ─────────────────────────────────────────────────────

def format_vp_context(vp: dict, precio_actual: float | None = None) -> str:
    """
    Formatea el Volume Profile en 2-3 líneas compactas para el LLM.

    Ejemplo:
      "VPOC: $67,200 | VAH: $67,800 | VAL: $66,600
       Precio vs estructura: ENTRE VAL y VPOC → dentro del valor
       LVN cercanos: $66,100 / $68,200 (cruces rápidos esperados)"
    """
    if not vp or vp.get("error"):
        return ""

    vpoc = vp.get("vpoc")
    vah  = vp.get("value_area_high")
    val  = vp.get("value_area_low")
    hvn  = vp.get("hvn", [])
    lvn  = vp.get("lvn", [])

    if not vpoc:
        return ""

    lineas = [
        f"VPOC: ${vpoc:,.0f} | VAH: ${vah:,.0f} | VAL: ${val:,.0f}"
    ]

    if precio_actual:
        if precio_actual > vah:
            posicion = "SOBRE VAH → sobrecomprado relativo, buscar distribución"
        elif precio_actual < val:
            posicion = "BAJO VAL → sobrevendido relativo, buscar absorción"
        elif precio_actual > vpoc:
            posicion = "entre VPOC y VAH → dentro del valor, sesgo alcista"
        else:
            posicion = "entre VAL y VPOC → dentro del valor, sesgo bajista"
        lineas.append(f"Precio vs estructura: {posicion}")

    if lvn:
        lvn_str = " / ".join(f"${p:,.0f}" for p in lvn[:3])
        lineas.append(f"LVN (cruces rápidos): {lvn_str}")

    return "\n".join(lineas)
