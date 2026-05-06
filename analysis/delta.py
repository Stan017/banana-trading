"""
analysis/delta.py — Delta por vela (order flow granular)
══════════════════════════════════════════════════════════
Extrae el delta individual de cada vela desde el CVD ya cacheado.

Delta por vela = taker_buy_vol - taker_sell_vol
  > 0 → presión compradora neta en esa vela
  < 0 → presión vendedora neta en esa vela

Jane Street:
  Tres velas consecutivas con delta negativo creciente mientras el precio sube
  = distribución activa = señal de short setup de alta convicción.
  Esto es lo que diferencia una entrada del "me parece que baja".

Interfaz:
  get_delta_per_candle(symbol, tf, n) → lista de últimas N velas con delta
  format_delta_context(deltas, tf)    → string compacto para inyectar en LLM
"""

from __future__ import annotations


def get_delta_per_candle(
    symbol: str = "BTC/USDT",
    tf: str = "4h",
    n: int = 6,
) -> list[dict]:
    """
    Retorna el delta individual de las últimas N velas del TF indicado.
    Usa el caché de get_cvd — sin llamada API adicional.

    Cada elemento:
      {"ts": int, "fecha": "MM-DD HH:MM", "delta": float, "bias": str}
    """
    try:
        from binance_data import get_cvd
        cvd = get_cvd(symbol, tf=tf)
        if cvd.get("error"):
            return []
        raw = cvd.get("deltas_por_vela", [])
        return raw[-n:] if len(raw) >= n else raw
    except Exception:
        return []


def format_delta_context(deltas: list[dict], tf: str = "4h") -> str:
    """
    Formatea los deltas en una línea compacta para el contexto del LLM.

    Ejemplos:
      "Delta últimas 3 velas 4H: -189 / -234 / -312 → distribución acelerando"
      "Delta últimas 3 velas 4H: +312 / +234 / +189 → absorción acelerando"
      "Delta últimas 3 velas 4H: +120 / -89 / +45 → flujo mixto"
    """
    if not deltas:
        return ""

    valores = [d["delta"] for d in deltas]
    n       = len(valores)
    tf_up   = tf.upper()

    # Formatear valores con signo
    vals_str = " / ".join(
        f"{'+' if v >= 0 else ''}{v:,.0f}" for v in valores
    )

    # Detectar patrón
    todos_neg     = all(v < 0 for v in valores)
    todos_pos     = all(v > 0 for v in valores)
    acelerando_neg = todos_neg and abs(valores[-1]) > abs(valores[0])
    acelerando_pos = todos_pos and abs(valores[-1]) > abs(valores[0])

    if acelerando_neg:
        patron = "distribución acelerando ⚠"
    elif acelerando_pos:
        patron = "absorción acelerando ✓"
    elif todos_neg:
        patron = "presión vendedora sostenida"
    elif todos_pos:
        patron = "presión compradora sostenida"
    else:
        patron = "flujo mixto"

    return f"Delta últimas {n} velas {tf_up}: {vals_str} → {patron}"
