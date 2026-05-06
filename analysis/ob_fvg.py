"""
analysis/ob_fvg.py
Detección algorítmica de Order Blocks y Fair Value Gaps.

TORVALDS: Funciones puras. Input: lista de velas. Output: lista de OBs/FVGs.
          Sin side effects. Sin llamadas a API. Solo matemática.

Estructura de vela esperada:
  {"fecha": str, "open": float, "high": float, "low": float, "close": float, "volumen": float}
"""

from __future__ import annotations


# ============================================================
# ORDER BLOCKS
# ============================================================

def detect_order_blocks(velas: list, n: int = 100, impulse_min: int = 2) -> list:
    """
    Detecta Order Blocks no mitigados en las últimas n velas.

    Un OB es la última vela OPUESTA antes de un movimiento impulsivo:
      - OB bajista = última vela alcista antes de 3+ velas bajistas consecutivas
      - OB alcista = última vela bajista antes de 3+ velas alcistas consecutivas

    Un OB está mitigado cuando el precio regresa al rango del OB después del impulso.

    Returns: lista de hasta 3 OBs no mitigados más cercanos al precio actual,
             ordenados por distancia absoluta.
    """
    if len(velas) < impulse_min + 2:
        return []

    velas = velas[-n:]
    precio_actual = velas[-1]["close"]
    obs = []

    for i in range(len(velas) - impulse_min):
        vela = velas[i]
        es_alcista = vela["close"] > vela["open"]
        es_bajista = vela["close"] < vela["open"]

        # ── Buscar impulso bajista a partir de i+1 ──────────────
        if es_alcista:
            consecutivas = 0
            for j in range(i + 1, min(i + 1 + impulse_min + 2, len(velas))):
                if velas[j]["close"] < velas[j]["open"]:
                    consecutivas += 1
                else:
                    break
            if consecutivas >= impulse_min:
                obs.append({
                    "tipo":   "bajista",
                    "high":   vela["high"],
                    "low":    vela["low"],
                    "inicio": vela["fecha"],
                    "_idx":   i,
                })

        # ── Buscar impulso alcista a partir de i+1 ───────────────
        if es_bajista:
            consecutivas = 0
            for j in range(i + 1, min(i + 1 + impulse_min + 2, len(velas))):
                if velas[j]["close"] > velas[j]["open"]:
                    consecutivas += 1
                else:
                    break
            if consecutivas >= impulse_min:
                obs.append({
                    "tipo":   "alcista",
                    "high":   vela["high"],
                    "low":    vela["low"],
                    "inicio": vela["fecha"],
                    "_idx":   i,
                })

    # ── Verificar mitigación ──────────────────────────────────────
    for ob in obs:
        idx      = ob.pop("_idx")
        mitigado = False

        # Paso 1: esperar a que el precio SALGA del rango del OB (confirma el impulso)
        precio_salio = False
        for j in range(idx + 1, len(velas)):
            if ob["tipo"] == "bajista":
                # El precio sale cuando baja por debajo del low del OB
                if velas[j]["low"] < ob["low"]:
                    precio_salio = True
                # Paso 2: una vez fuera, si vuelve a tocar el OB → mitigado
                if precio_salio and velas[j]["high"] >= ob["low"]:
                    mitigado = True
                    break
            else:
                # El precio sale cuando sube por encima del high del OB
                if velas[j]["high"] > ob["high"]:
                    precio_salio = True
                # Paso 2: una vez fuera, si vuelve a tocar el OB → mitigado
                if precio_salio and velas[j]["low"] <= ob["high"]:
                    mitigado = True
                    break

        ob["mitigado"]      = mitigado
        ob["distancia_pct"] = round(
            ((ob["high"] + ob["low"]) / 2 / precio_actual - 1) * 100, 2
        )

    # ── Filtrar: solo no mitigados, los 3 más cercanos ───────────
    activos = [ob for ob in obs if not ob["mitigado"]]
    activos.sort(key=lambda x: abs(x["distancia_pct"]))
    return activos[:3]


# ============================================================
# FAIR VALUE GAPS
# ============================================================

def detect_fvg(velas: list, n: int = 50) -> list:
    """
    Detecta Fair Value Gaps abiertos en las últimas n velas.

    Algoritmo:
      FVG bajista: high[i] < low[i-2]  → gap entre vela i-2 y vela i
      FVG alcista: low[i]  > high[i-2] → gap entre vela i-2 y vela i
      FVG "abierto" = el precio no ha regresado al rango del gap

    Returns: lista de FVGs abiertos ordenados por distancia al precio actual.
    """
    if len(velas) < 3:
        return []

    velas         = velas[-n:]
    precio_actual = velas[-1]["close"]
    fvgs          = []

    for i in range(2, len(velas)):
        v0, v2 = velas[i - 2], velas[i]

        # FVG bajista
        if v2["high"] < v0["low"]:
            fvgs.append({
                "tipo":         "bajista",
                "precio_sup":   v0["low"],
                "precio_inf":   v2["high"],
                "inicio":       v0["fecha"],
                "_idx_inicio":  i - 2,
                "_idx_vela":    i,
            })

        # FVG alcista
        elif v2["low"] > v0["high"]:
            fvgs.append({
                "tipo":         "alcista",
                "precio_sup":   v2["low"],
                "precio_inf":   v0["high"],
                "inicio":       v0["fecha"],
                "_idx_inicio":  i - 2,
                "_idx_vela":    i,
            })

    # ── Verificar si el FVG sigue abierto ────────────────────────
    for fvg in fvgs:
        idx_inicio = fvg.pop("_idx_inicio")
        idx_vela   = fvg.pop("_idx_vela")
        abierto    = True

        for j in range(idx_vela + 1, len(velas)):
            if fvg["tipo"] == "bajista":
                if velas[j]["high"] >= fvg["precio_inf"]:
                    abierto = False
                    break
            else:
                if velas[j]["low"] <= fvg["precio_sup"]:
                    abierto = False
                    break

        fvg["abierto"]      = abierto
        fvg["distancia_pct"] = round(
            ((fvg["precio_sup"] + fvg["precio_inf"]) / 2 / precio_actual - 1) * 100, 2
        )

    abiertos = [f for f in fvgs if f["abierto"]]
    abiertos.sort(key=lambda x: abs(x["distancia_pct"]))
    return abiertos[:3]
