"""
analysis/liquidity.py
Detección algorítmica de Equal Highs (EQH) y Equal Lows (EQL).

TORVALDS: Función pura. Input: lista de velas. Output: dict con EQH y EQL.
          Sin side effects. Sin llamadas a API. Solo matemática.

Estructura de vela esperada:
  {"fecha": str, "open": float, "high": float, "low": float, "close": float}
"""

from __future__ import annotations


def detect_eqh_eql(
    velas:      list,
    n:          int   = 100,
    tolerancia: float = 0.15,   # % — máxima diferencia para considerar "igual"
    min_toques: int   = 2,      # mínimo de toques para clasificar como zona
) -> dict:
    """
    Detecta zonas de Equal Highs y Equal Lows.

    EQH: dos o más máximos dentro de `tolerancia`% entre sí → liquidez de longs arriba
    EQL: dos o más mínimos dentro de `tolerancia`% entre sí → liquidez de shorts abajo

    Más toques = zona más densa = imán de precio más fuerte.

    Returns:
        {
            "eqh": [{"precio": float, "toques": int, "distancia_pct": float, "primera_fecha": str}],
            "eql": [{"precio": float, "toques": int, "distancia_pct": float, "primera_fecha": str}]
        }
    """
    if len(velas) < 2:
        return {"eqh": [], "eql": []}

    velas         = velas[-n:]
    precio_actual = velas[-1]["close"]

    highs = [(v["high"], v["fecha"]) for v in velas]
    lows  = [(v["low"],  v["fecha"]) for v in velas]

    eqh = _agrupar_niveles(highs, tolerancia, min_toques, precio_actual)
    eql = _agrupar_niveles(lows,  tolerancia, min_toques, precio_actual)

    # Ordenar por distancia absoluta al precio actual
    eqh.sort(key=lambda x: abs(x["distancia_pct"]))
    eql.sort(key=lambda x: abs(x["distancia_pct"]))

    return {"eqh": eqh[:3], "eql": eql[:3]}


def _agrupar_niveles(
    puntos:     list,   # list of (precio, fecha)
    tolerancia: float,
    min_toques: int,
    precio_actual: float,
) -> list:
    """
    Agrupa precios que están dentro de `tolerancia`% entre sí.
    Cada grupo con >= min_toques entradas es una zona EQH/EQL.
    """
    usados  = [False] * len(puntos)
    zonas   = []

    for i, (precio_i, fecha_i) in enumerate(puntos):
        if usados[i]:
            continue

        grupo_precios = [precio_i]
        grupo_fechas  = [fecha_i]

        for j in range(i + 1, len(puntos)):
            if usados[j]:
                continue
            precio_j, fecha_j = puntos[j]
            diff_pct = abs(precio_j - precio_i) / precio_i * 100
            if diff_pct <= tolerancia:
                grupo_precios.append(precio_j)
                grupo_fechas.append(fecha_j)
                usados[j] = True

        if len(grupo_precios) >= min_toques:
            precio_zona = sum(grupo_precios) / len(grupo_precios)
            zonas.append({
                "precio":         round(precio_zona, 2),
                "toques":         len(grupo_precios),
                "distancia_pct":  round((precio_zona / precio_actual - 1) * 100, 2),
                "primera_fecha":  grupo_fechas[0],
            })
            usados[i] = True

    return zonas
