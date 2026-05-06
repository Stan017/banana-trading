"""
stats_engine.py — Motor de estadísticas históricas para TradeBot AI.
Corre una vez al día. Guarda resultados en DB o JSON cache.
Inyectado en el chat como contexto adicional.
"""

import logging
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import json, os

logger = logging.getLogger(__name__)


# ─── 1. DESCARGA DE DATOS ────────────────────────────────────────────────────

def fetch_ohlcv_binance(interval="1h", limit=1000) -> pd.DataFrame:
    """
    Descarga velas de Binance sin API key con paginación automática.
    Binance limita a 1000 velas por petición — si limit > 1000 hace
    múltiples requests hacia atrás en el tiempo.

    Retorna DataFrame con columnas: open, high, low, close, volume
    index: DatetimeIndex UTC.
    """
    import requests
    BATCH = 1000
    url   = "https://api.binance.com/api/v3/klines"
    all_data = []
    end_time = None

    remaining = limit
    while remaining > 0:
        batch = min(remaining, BATCH)
        params = {"symbol": "BTCUSDT", "interval": interval, "limit": batch}
        if end_time is not None:
            params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if not data:
            break
        all_data = data + all_data          # datos más antiguos al frente
        end_time  = int(data[0][0]) - 1    # siguiente petición termina antes del más antiguo
        remaining -= len(data)
        if len(data) < batch:              # Binance devolvió menos de lo pedido = no hay más
            break

    df = pd.DataFrame(all_data, columns=[
        "timestamp","open","high","low","close","volume",
        "close_time","quote_vol","trades","taker_buy_base",
        "taker_buy_quote","ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df = df.set_index("timestamp")
    df = df[~df.index.duplicated(keep="last")]   # eliminar duplicados de solapamiento
    return df[["open","high","low","close","volume"]]


# ─── 2. KILL ZONES ───────────────────────────────────────────────────────────

def calcular_kill_zones(df_1h: pd.DataFrame) -> dict:
    """
    Calcula el movimiento promedio (%) por hora del día UTC.
    Las kill zones son las horas con mayor movimiento promedio.

    Sesiones UTC:
      Asia:    00:00 - 08:00
      London:  08:00 - 13:00
      NY:      13:00 - 22:00
    """
    df = df_1h.copy()
    df["hour"] = df.index.hour
    df["move_pct"] = ((df["high"] - df["low"]) / df["low"]) * 100

    avg_by_hour = df.groupby("hour")["move_pct"].mean().round(4)

    # Clasificar cada hora en su sesión
    def sesion(h):
        if 0 <= h < 8:   return "Asia"
        if 8 <= h < 13:  return "London"
        if 13 <= h < 22: return "NY"
        return "Off"

    result = {
        "avg_move_by_hour": avg_by_hour.to_dict(),
        "top_5_kill_zones": avg_by_hour.nlargest(5).to_dict(),
        "sesion_avg": {
            "Asia":   round(df[df["hour"].between(0,7)]["move_pct"].mean(), 4),
            "London": round(df[df["hour"].between(8,12)]["move_pct"].mean(), 4),
            "NY":     round(df[df["hour"].between(13,21)]["move_pct"].mean(), 4),
        }
    }

    # Kill zone activa AHORA
    hora_actual = datetime.now(timezone.utc).hour
    result["kill_zone_activa_ahora"] = sesion(hora_actual)
    result["hora_utc_actual"] = hora_actual
    result["es_kill_zone_activa"] = hora_actual in avg_by_hour.nlargest(5).index.tolist()

    return result


# ─── 3. SWEEP RETURNS ────────────────────────────────────────────────────────

def calcular_sweep_returns(df_1h: pd.DataFrame) -> dict:
    """
    Detecta sweeps de liquidez y calcula retorno promedio posterior.

    Sweep = precio rompe un swing high/low previo y REVIERTE en la misma vela
            o en las siguientes N velas.

    Tipos:
      BSL (Buy Side Liquidity): precio supera un swing high previo
      SSL (Sell Side Liquidity): precio cae bajo un swing low previo

    Reversal = precio vuelve al otro lado del nivel en las siguientes 4 velas.
    """
    df = df_1h.copy()
    window = 20  # lookback para swing high/low

    bsl_reversals = []
    ssl_reversals = []
    bsl_returns_4h = []
    ssl_returns_4h = []

    for i in range(window, len(df) - 8):
        prev_highs = df["high"].iloc[i-window:i]
        prev_lows  = df["low"].iloc[i-window:i]
        curr_high  = df["high"].iloc[i]
        curr_low   = df["low"].iloc[i]
        curr_close = df["close"].iloc[i]

        # BSL sweep: rompe swing high previo
        swing_high = prev_highs.max()
        if curr_high > swing_high:
            # ¿Revirtió? close de la vela actual < swing_high
            reversed_bsl = curr_close < swing_high
            bsl_reversals.append(reversed_bsl)
            # Retorno 4 velas después
            future_close = df["close"].iloc[i+4]
            ret_4h = ((future_close - curr_close) / curr_close) * 100
            bsl_returns_4h.append(ret_4h)

        # SSL sweep: rompe swing low previo
        swing_low = prev_lows.min()
        if curr_low < swing_low:
            reversed_ssl = curr_close > swing_low
            ssl_reversals.append(reversed_ssl)
            future_close = df["close"].iloc[i+4]
            ret_4h = ((future_close - curr_close) / curr_close) * 100
            ssl_returns_4h.append(ret_4h)

    total_bsl = len(bsl_reversals)
    total_ssl = len(ssl_reversals)
    total = total_bsl + total_ssl
    total_reversals = sum(bsl_reversals) + sum(ssl_reversals)

    return {
        "total_sweeps": total,
        "global_reversal_rate": round((total_reversals / total * 100) if total > 0 else 0, 1),
        "bsl": {
            "total": total_bsl,
            "reversal_rate": round((sum(bsl_reversals) / total_bsl * 100) if total_bsl > 0 else 0, 1),
            "avg_return_4h": round(np.mean(bsl_returns_4h) if bsl_returns_4h else 0, 3),
        },
        "ssl": {
            "total": total_ssl,
            "reversal_rate": round((sum(ssl_reversals) / total_ssl * 100) if total_ssl > 0 else 0, 1),
            "avg_return_4h": round(np.mean(ssl_returns_4h) if ssl_returns_4h else 0, 3),
        }
    }


# ─── 4. POST BIG MOVE ANALYSIS ───────────────────────────────────────────────

def calcular_post_big_move(df_1d: pd.DataFrame, umbral_pct: float = 4.0) -> dict:
    """
    Qué pasa después de un movimiento grande (> umbral_pct en 1D).
    Retorna retornos promedio en 1D, 3D, 7D después del big move.

    umbral_pct: mínimo % de movimiento para considerarse "big move"
    """
    df = df_1d.copy()
    df["return_1d"] = df["close"].pct_change() * 100

    big_up   = df[df["return_1d"] >  umbral_pct]
    big_down = df[df["return_1d"] < -umbral_pct]

    def retornos_futuros(events, df, dias=[1, 3, 7]):
        results = {}
        for d in dias:
            rets = []
            for idx in events.index:
                pos = df.index.get_loc(idx)
                if pos + d < len(df):
                    ret = ((df["close"].iloc[pos+d] - df["close"].iloc[pos])
                           / df["close"].iloc[pos]) * 100
                    rets.append(ret)
            results[f"{d}d_avg"] = round(np.mean(rets) if rets else 0, 2)
            results[f"{d}d_positive_rate"] = round(
                (sum(1 for r in rets if r > 0) / len(rets) * 100) if rets else 0, 1
            )
        return results

    # ¿Hay un big move HOY?
    ultimo_ret = df["return_1d"].iloc[-1]
    big_move_hoy = abs(ultimo_ret) > umbral_pct
    tipo_hoy = "UP" if ultimo_ret > umbral_pct else ("DOWN" if ultimo_ret < -umbral_pct else "NONE")

    return {
        "umbral_pct": umbral_pct,
        "big_up_count": len(big_up),
        "big_down_count": len(big_down),
        "after_big_up": retornos_futuros(big_up, df),
        "after_big_down": retornos_futuros(big_down, df),
        "big_move_hoy": big_move_hoy,
        "tipo_hoy": tipo_hoy,
        "retorno_hoy_pct": round(ultimo_ret, 2),
    }


# ─── 5. DAY OF WEEK BIAS ─────────────────────────────────────────────────────

def calcular_day_of_week(df_1d: pd.DataFrame) -> dict:
    """
    Retorno promedio y tasa alcista por día de la semana.
    0=Lunes, 6=Domingo
    """
    df = df_1d.copy()
    df["return_1d"] = df["close"].pct_change() * 100
    df["dow"] = df.index.dayofweek
    nombres = {0:"Lunes",1:"Martes",2:"Miércoles",3:"Jueves",
               4:"Viernes",5:"Sábado",6:"Domingo"}

    result = {}
    for dow in range(7):
        subset = df[df["dow"] == dow]["return_1d"].dropna()
        if len(subset) == 0:
            continue
        result[nombres[dow]] = {
            "avg_return": round(subset.mean(), 3),
            "positive_rate": round((subset > 0).mean() * 100, 1),
            "count": len(subset)
        }

    dia_actual = nombres[datetime.now().weekday()]
    result["dia_actual"] = dia_actual
    result["sesgo_hoy"] = result.get(dia_actual, {})

    # Mejor y peor día para acceso rápido del LLM
    dias_data = {k: v for k, v in result.items()
                 if isinstance(v, dict) and "avg_return" in v}
    if dias_data:
        result["mejor_dia"] = max(dias_data, key=lambda d: dias_data[d]["avg_return"])
        result["peor_dia"]  = min(dias_data, key=lambda d: dias_data[d]["avg_return"])

    return result


# ─── 6. MONTHLY BIAS ─────────────────────────────────────────────────────────

def calcular_monthly_bias(df_1d: pd.DataFrame) -> dict:
    """
    Retorno promedio de BTC por mes del año (historial completo).
    Incluye retorno mensual total (compuesto) + performance MTD.
    """
    df = df_1d.copy()
    df["return_1d"] = df["close"].pct_change() * 100
    df["month"] = df.index.month
    nombres = {1:"Enero",2:"Febrero",3:"Marzo",4:"Abril",5:"Mayo",
               6:"Junio",7:"Julio",8:"Agosto",9:"Septiembre",
               10:"Octubre",11:"Noviembre",12:"Diciembre"}

    # Retorno mensual total: resamplear a monthly y calcular % change
    df_m = df["close"].resample("ME").last()   # último close de cada mes
    df_m_ret = df_m.pct_change() * 100         # retorno del mes entero
    df_m_ret.index = df_m_ret.index.month      # index = número de mes (1-12)

    result = {}
    for m in range(1, 13):
        subset_d = df[df["month"] == m]["return_1d"].dropna()
        subset_m = df_m_ret[df_m_ret.index == m].dropna()
        if len(subset_d) == 0:
            continue
        result[nombres[m]] = {
            "avg_daily_return":   round(subset_d.mean(), 3),
            "avg_monthly_return": round(float(subset_m.mean()), 2) if len(subset_m) else None,
            "positive_rate":      round((subset_d > 0).mean() * 100, 1),
            "monthly_win_rate":   round((subset_m > 0).mean() * 100, 1) if len(subset_m) else None,
            "count":              len(subset_d),
            "meses_analizados":   len(subset_m),
        }

    mes_actual = nombres[datetime.now().month]
    result["mes_actual"] = mes_actual
    result["sesgo_mes_actual"] = result.get(mes_actual, {})

    # Mejor y peor mes (por retorno mensual total)
    meses_con_ret = {k: v for k, v in result.items()
                     if isinstance(v, dict) and v.get("avg_monthly_return") is not None}
    if meses_con_ret:
        result["mejor_mes"] = max(meses_con_ret, key=lambda m: meses_con_ret[m]["avg_monthly_return"])
        result["peor_mes"]  = min(meses_con_ret, key=lambda m: meses_con_ret[m]["avg_monthly_return"])

    # MTD: rendimiento desde el 1º del mes actual hasta hoy
    try:
        hoy = datetime.now(timezone.utc)
        inicio_mes = pd.Timestamp(hoy.year, hoy.month, 1, tz="UTC")
        df_mtd = df[df.index >= inicio_mes]["close"]
        if len(df_mtd) >= 2:
            mtd = round((float(df_mtd.iloc[-1]) - float(df_mtd.iloc[0])) / float(df_mtd.iloc[0]) * 100, 2)
            result["mtd_actual"] = mtd
        else:
            result["mtd_actual"] = None
    except Exception:
        result["mtd_actual"] = None

    return result


# ─── 7. SEMANA DEL MES ───────────────────────────────────────────────────────

def calcular_week_of_month(df_1d: pd.DataFrame) -> dict:
    """
    Retorno promedio de BTC por semana del mes (1-5).
    Semana = (día_del_mes - 1) // 7 + 1  →  1=días 1-7, 2=días 8-14, etc.

    Patrones conocidos:
      - Semana 4/5 a menudo es de vencimientos de futuros y opciones CME
      - Semana 1 suele arrancar con momentum del mes anterior
    """
    df = df_1d.copy()
    df["return_1d"]     = df["close"].pct_change() * 100
    df["week_of_month"] = ((df.index.day - 1) // 7 + 1).astype(int)
    etiquetas = {1: "Semana 1 (días 1-7)",  2: "Semana 2 (días 8-14)",
                 3: "Semana 3 (días 15-21)", 4: "Semana 4 (días 22-28)",
                 5: "Semana 5 (días 29-31)"}

    result = {}
    for w in range(1, 6):
        subset = df[df["week_of_month"] == w]["return_1d"].dropna()
        if len(subset) < 5:
            continue
        result[etiquetas[w]] = {
            "avg_return":   round(subset.mean(), 3),
            "positive_rate": round((subset > 0).mean() * 100, 1),
            "count":        len(subset),
        }

    # Semana actual
    hoy = datetime.now()
    semana_num = (hoy.day - 1) // 7 + 1
    semana_key = etiquetas.get(semana_num)
    result["semana_actual_num"] = semana_num
    result["semana_actual_label"] = semana_key
    result["sesgo_semana_actual"] = result.get(semana_key, {})

    # Mejor / peor semana
    semanas_data = {k: v for k, v in result.items()
                    if isinstance(v, dict) and "avg_return" in v}
    if semanas_data:
        result["mejor_semana"] = max(semanas_data, key=lambda s: semanas_data[s]["avg_return"])
        result["peor_semana"]  = min(semanas_data, key=lambda s: semanas_data[s]["avg_return"])

    return result


# ─── 8. VOLATILIDAD ─────────────────────────────────────────────────────────

def calcular_volatilidad(df_1d: pd.DataFrame) -> dict:
    """
    Volatilidad realizada actual vs histórica.
    Percentil de volatilidad: ¿estamos en zona de alta/baja vol?
    """
    df = df_1d.copy()
    df["return_1d"] = df["close"].pct_change()

    vol_7d  = df["return_1d"].tail(7).std() * np.sqrt(365) * 100
    vol_30d = df["return_1d"].tail(30).std() * np.sqrt(365) * 100
    vol_hist = df["return_1d"].std() * np.sqrt(365) * 100

    # Rolling 30d vol para percentil
    rolling_vol = df["return_1d"].rolling(30).std() * np.sqrt(365) * 100
    percentil_actual = round(
        (rolling_vol.dropna() < vol_30d).mean() * 100, 1
    )

    return {
        "vol_7d_anualizada": round(vol_7d, 1),
        "vol_30d_anualizada": round(vol_30d, 1),
        "vol_historica_anualizada": round(vol_hist, 1),
        "percentil_vol_actual": percentil_actual,
        "interpretacion": (
            "ALTA" if percentil_actual > 75 else
            "BAJA" if percentil_actual < 25 else
            "NORMAL"
        )
    }


# ─── 8. CME GAP DETECTOR ─────────────────────────────────────────────────────

def detectar_cme_gap(df_1h: pd.DataFrame) -> dict:
    """
    Detecta si hay un gap del CME abierto actualmente.
    CME cierra viernes ~22:00 UTC y abre domingo ~23:00 UTC.
    Gap = diferencia entre el cierre del viernes y apertura del domingo.

    Históricamente los CME gaps tienen ~80% de fill rate.
    """
    df = df_1h.copy()
    precio_actual = float(df["close"].iloc[-1])

    # Buscar el último fin de semana
    df_weekend = df[df.index.dayofweek.isin([4, 6])]  # viernes y domingo

    gaps_encontrados = []
    viernes = df[df.index.dayofweek == 4]
    domingos = df[df.index.dayofweek == 6]

    for fecha_v in viernes.index[-8:]:
        close_viernes = df.loc[fecha_v, "close"] if fecha_v in df.index else None
        if close_viernes is None:
            continue
        # Buscar el domingo siguiente
        domingos_sig = domingos[domingos.index > fecha_v]
        if len(domingos_sig) == 0:
            continue
        fecha_d = domingos_sig.index[0]
        open_domingo = df.loc[fecha_d, "open"] if fecha_d in df.index else None
        if open_domingo is None:
            continue

        gap_pct = ((open_domingo - close_viernes) / close_viernes) * 100
        if abs(gap_pct) > 0.3:  # solo gaps > 0.3%
            # ¿Está llenado?
            datos_post = df[df.index > fecha_d]["close"]
            llenado = False
            for precio in datos_post:
                if gap_pct > 0 and precio <= close_viernes:
                    llenado = True; break
                if gap_pct < 0 and precio >= close_viernes:
                    llenado = True; break

            gaps_encontrados.append({
                "fecha": str(fecha_v.date()),
                "close_viernes": round(float(close_viernes), 0),
                "open_domingo": round(float(open_domingo), 0),
                "gap_pct": round(gap_pct, 2),
                "gap_tipo": "UP" if gap_pct > 0 else "DOWN",
                "llenado": llenado,
                "nivel_gap": round(float(close_viernes), 0),
            })

    # Gap activo (no llenado más reciente)
    gaps_activos = [g for g in gaps_encontrados if not g["llenado"]]
    gap_activo = gaps_activos[-1] if gaps_activos else None

    distancia_pct = None
    if gap_activo:
        distancia_pct = round(
            ((gap_activo["nivel_gap"] - precio_actual) / precio_actual) * 100, 2
        )

    return {
        "precio_actual": precio_actual,
        "gap_activo": gap_activo,
        "distancia_al_gap_pct": distancia_pct,
        "gaps_recientes": gaps_encontrados[-5:],
        "fill_rate_historico": round(
            sum(1 for g in gaps_encontrados if g["llenado"]) /
            len(gaps_encontrados) * 100 if gaps_encontrados else 0, 1
        )
    }


# ─── 9. HALVING CYCLE ────────────────────────────────────────────────────────

def calcular_halving_cycle() -> dict:
    """
    Días desde el último halving.
    Halvings conocidos:
      2012-11-28, 2016-07-09, 2020-05-11, 2024-04-19
    """
    halvings = [
        datetime(2012, 11, 28),
        datetime(2016, 7, 9),
        datetime(2020, 5, 11),
        datetime(2024, 4, 19),
    ]
    hoy = datetime.utcnow()
    ultimo_halving = max(h for h in halvings if h <= hoy)
    dias_desde = (hoy - ultimo_halving).days
    proximo_halving_aprox = datetime(2028, 4, 15)  # estimado
    dias_para_proximo = (proximo_halving_aprox - hoy).days

    # Fase del ciclo
    if dias_desde < 180:
        fase = "POST-HALVING TEMPRANO (0-6 meses) — históricamente acumulación"
    elif dias_desde < 540:
        fase = "BULL RUN HISTÓRICO (6-18 meses post-halving)"
    elif dias_desde < 900:
        fase = "DISTRIBUCIÓN / TECHO (18-30 meses post-halving)"
    else:
        fase = "BEAR MARKET / PRE-HALVING (>30 meses post-halving)"

    return {
        "ultimo_halving": str(ultimo_halving.date()),
        "dias_desde_halving": dias_desde,
        "fase_ciclo": fase,
        "proximo_halving_estimado": str(proximo_halving_aprox.date()),
        "dias_para_proximo": dias_para_proximo,
    }


# ─── 10. SESSION DEEP DIVE ───────────────────────────────────────────────────

def calcular_session_deep_dive(df_1h: pd.DataFrame) -> dict:
    """
    Analiza el comportamiento de sesiones Asia / London / NY.

    Sesiones UTC:
      Asia:   00:00 - 07:59
      London: 08:00 - 12:59
      NY:     13:00 - 21:59

    Métricas históricas (por día):
      - % días que London rompe High de Asia
      - % días que London rompe Low de Asia
      - % días que London rompe AMBOS (trampa doble)
      - % días que NY rompe High de Asia
      - % días que NY rompe Low de Asia
      - Retorno promedio de London cuando barre High vs Low de Asia
      - Ratio de rango London / rango Asia

    Datos del día actual:
      - Rango de Asia de hoy (si ya cerró 08:00 UTC)
      - Si London ya rompió el high/low (si es >=08:00)
      - Si NY ya rompió el high/low (si es >=13:00)
    """
    df = df_1h.copy()
    df["day"]  = df.index.floor("D")   # Timestamp UTC medianoche — comparación fiable
    df["hour"] = df.index.hour

    dias = df["day"].unique()

    # Acumuladores históricos
    london_broke_high   = []
    london_broke_low    = []
    london_broke_both   = []
    ny_broke_high       = []
    ny_broke_low        = []
    ret_post_london_high = []   # retorno de la sesión NY cuando London barrió high
    ret_post_london_low  = []   # retorno de la sesión NY cuando London barrió low
    london_vs_asia_ratio = []   # rango London / rango Asia

    for dia in dias[:-1]:  # excluir el día actual (puede estar incompleto)
        sub = df[df["day"] == dia]

        asia   = sub[sub["hour"].between(0, 7)]
        london = sub[sub["hour"].between(8, 12)]
        ny     = sub[sub["hour"].between(13, 21)]

        if asia.empty or london.empty:
            continue

        asia_high = asia["high"].max()
        asia_low  = asia["low"].min()
        asia_range = asia_high - asia_low
        if asia_range == 0:
            continue

        lon_high = london["high"].max()
        lon_low  = london["low"].min()
        lon_range = lon_high - lon_low

        broke_high = lon_high > asia_high
        broke_low  = lon_low  < asia_low

        london_broke_high.append(broke_high)
        london_broke_low.append(broke_low)
        london_broke_both.append(broke_high and broke_low)
        london_vs_asia_ratio.append(lon_range / asia_range)

        # Retorno de sesión NY después del sweep de London
        if not ny.empty:
            ny_open  = ny["open"].iloc[0]
            ny_close = ny["close"].iloc[-1]
            ny_ret   = ((ny_close - ny_open) / ny_open) * 100
            if broke_high:
                ret_post_london_high.append(ny_ret)
            if broke_low:
                ret_post_london_low.append(ny_ret)

            # NY rompiendo Asia (independiente de London)
            ny_broke_high.append(ny["high"].max() > asia_high)
            ny_broke_low.append(ny["low"].min() < asia_low)

    n = len(london_broke_high)

    historico = {
        "dias_analizados":            n,
        "london_broke_high_rate":     round(sum(london_broke_high) / n * 100, 1) if n else None,
        "london_broke_low_rate":      round(sum(london_broke_low)  / n * 100, 1) if n else None,
        "london_broke_both_rate":     round(sum(london_broke_both) / n * 100, 1) if n else None,
        "ny_broke_high_rate":         round(sum(ny_broke_high)     / len(ny_broke_high) * 100, 1) if ny_broke_high else None,
        "ny_broke_low_rate":          round(sum(ny_broke_low)      / len(ny_broke_low)  * 100, 1) if ny_broke_low  else None,
        "avg_london_vs_asia_ratio":   round(float(np.mean(london_vs_asia_ratio)), 2) if london_vs_asia_ratio else None,
        "ny_ret_after_london_high":   round(float(np.mean(ret_post_london_high)), 3) if ret_post_london_high else None,
        "ny_ret_after_london_low":    round(float(np.mean(ret_post_london_low)),  3) if ret_post_london_low  else None,
        "ny_positive_after_london_high": round(sum(1 for r in ret_post_london_high if r > 0) / len(ret_post_london_high) * 100, 1) if ret_post_london_high else None,
        "ny_positive_after_london_low":  round(sum(1 for r in ret_post_london_low  if r > 0) / len(ret_post_london_low)  * 100, 1) if ret_post_london_low  else None,
    }

    # ── Datos del día actual ──────────────────────────────────────────────────
    hora_utc   = datetime.now(timezone.utc).hour
    hoy_ts     = pd.Timestamp(datetime.now(timezone.utc)).floor("D")
    sub_hoy    = df[df["day"] == hoy_ts]

    asia_hoy   = sub_hoy[sub_hoy["hour"].between(0, 7)]
    london_hoy = sub_hoy[sub_hoy["hour"].between(8, 12)]
    ny_hoy     = sub_hoy[sub_hoy["hour"].between(13, 21)]

    asia_high_hoy = float(asia_hoy["high"].max())  if not asia_hoy.empty  else None
    asia_low_hoy  = float(asia_hoy["low"].min())   if not asia_hoy.empty  else None
    asia_range_hoy_pct = round(
        (asia_high_hoy - asia_low_hoy) / asia_low_hoy * 100, 3
    ) if asia_high_hoy and asia_low_hoy else None

    lon_broke_high_hoy = bool(london_hoy["high"].max() > asia_high_hoy) if not london_hoy.empty and asia_high_hoy else None
    lon_broke_low_hoy  = bool(london_hoy["low"].min()  < asia_low_hoy)  if not london_hoy.empty and asia_low_hoy  else None
    ny_broke_high_hoy  = bool(ny_hoy["high"].max() > asia_high_hoy)     if not ny_hoy.empty     and asia_high_hoy else None
    ny_broke_low_hoy   = bool(ny_hoy["low"].min()  < asia_low_hoy)      if not ny_hoy.empty     and asia_low_hoy  else None

    def sesion_actual(h):
        if 0  <= h < 8:  return "Asia"
        if 8  <= h < 13: return "London"
        if 13 <= h < 22: return "NY"
        return "Off"

    hoy_data = {
        "sesion_actual":        sesion_actual(hora_utc),
        "hora_utc":             hora_utc,
        "asia_high":            round(asia_high_hoy, 0) if asia_high_hoy else None,
        "asia_low":             round(asia_low_hoy,  0) if asia_low_hoy  else None,
        "asia_range_pct":       asia_range_hoy_pct,
        "london_broke_high":    lon_broke_high_hoy,
        "london_broke_low":     lon_broke_low_hoy,
        "ny_broke_high":        ny_broke_high_hoy,
        "ny_broke_low":         ny_broke_low_hoy,
    }

    return {"historico": historico, "hoy": hoy_data}


# ─── 11. FOMC IMPACT ─────────────────────────────────────────────────────────

def calcular_fomc_impact(df_1d: pd.DataFrame) -> dict:
    """
    Analiza el comportamiento histórico de BTC alrededor de fechas FOMC.
    Fechas hardcodeadas (decisión = miércoles del meeting).

    Calcula:
      - Próximo FOMC y días restantes
      - Si estamos en "semana FOMC" (≤5 días antes)
      - Expansión de rango en día FOMC vs día normal
      - Retornos promedio: 2D pre, día FOMC, 1D post, 3D post
      - Tasa alcista post-FOMC
    """
    # Fechas de decisión FOMC (2020-2026)
    fomc_fechas = [
        # 2020
        datetime(2020, 1, 29), datetime(2020, 3, 3),  datetime(2020, 3, 15),
        datetime(2020, 4, 29), datetime(2020, 6, 10), datetime(2020, 7, 29),
        datetime(2020, 9, 16), datetime(2020, 11, 5), datetime(2020, 12, 16),
        # 2021
        datetime(2021, 1, 27), datetime(2021, 3, 17), datetime(2021, 4, 28),
        datetime(2021, 6, 16), datetime(2021, 7, 28), datetime(2021, 9, 22),
        datetime(2021, 11, 3), datetime(2021, 12, 15),
        # 2022
        datetime(2022, 1, 26), datetime(2022, 3, 16), datetime(2022, 5, 4),
        datetime(2022, 6, 15), datetime(2022, 7, 27), datetime(2022, 9, 21),
        datetime(2022, 11, 2), datetime(2022, 12, 14),
        # 2023
        datetime(2023, 2, 1),  datetime(2023, 3, 22), datetime(2023, 5, 3),
        datetime(2023, 6, 14), datetime(2023, 7, 26), datetime(2023, 9, 20),
        datetime(2023, 11, 1), datetime(2023, 12, 13),
        # 2024
        datetime(2024, 1, 31), datetime(2024, 3, 20), datetime(2024, 5, 1),
        datetime(2024, 6, 12), datetime(2024, 7, 31), datetime(2024, 9, 18),
        datetime(2024, 11, 7), datetime(2024, 12, 18),
        # 2025
        datetime(2025, 1, 29), datetime(2025, 3, 19), datetime(2025, 5, 7),
        datetime(2025, 6, 18), datetime(2025, 7, 30), datetime(2025, 9, 17),
        datetime(2025, 10, 29), datetime(2025, 12, 10),
        # 2026
        datetime(2026, 1, 28), datetime(2026, 3, 18), datetime(2026, 4, 29),
        datetime(2026, 6, 10), datetime(2026, 7, 29), datetime(2026, 9, 16),
        datetime(2026, 10, 28), datetime(2026, 12, 9),
    ]

    hoy = datetime.utcnow().date()

    # Próximo FOMC
    futuros = [f for f in fomc_fechas if f.date() >= hoy]
    proximo = futuros[0] if futuros else None
    dias_para_proximo = (proximo.date() - hoy).days if proximo else None

    # Último FOMC
    pasados = [f for f in fomc_fechas if f.date() < hoy]
    ultimo = pasados[-1] if pasados else None
    dias_desde_ultimo = (hoy - ultimo.date()).days if ultimo else None

    es_dia_fomc    = dias_para_proximo == 0
    es_semana_fomc = dias_para_proximo is not None and dias_para_proximo <= 5

    # ── Análisis histórico ────────────────────────────────────────────────────
    df = df_1d.copy()
    df["return_1d"] = df["close"].pct_change() * 100
    df["range_pct"] = ((df["high"] - df["low"]) / df["close"]) * 100

    avg_range_normal = df["range_pct"].mean()

    retornos_fomc   = []   # retorno en el propio día FOMC
    retornos_pre2   = []   # retorno 2 días ANTES del FOMC
    retornos_post1  = []   # retorno 1 día DESPUÉS
    retornos_post3  = []   # retorno 3 días DESPUÉS
    rangos_fomc     = []   # expansión de rango en día FOMC

    # Convertir índice a fechas para búsqueda
    df_dates = df.index.normalize()

    for fecha in fomc_fechas:
        ts = pd.Timestamp(fecha, tz="UTC")
        mask = df_dates == ts.normalize()
        if not mask.any():
            continue
        pos = df.index.get_loc(df.index[mask][0])

        rangos_fomc.append(df["range_pct"].iloc[pos])
        retornos_fomc.append(df["return_1d"].iloc[pos])

        if pos >= 2:
            retornos_pre2.append(df["return_1d"].iloc[pos - 2])
        if pos + 1 < len(df):
            retornos_post1.append(df["return_1d"].iloc[pos + 1])
        if pos + 3 < len(df):
            retornos_post3.append(df["return_1d"].iloc[pos + 3])

    n = len(retornos_fomc)
    avg_rango_fomc = round(np.mean(rangos_fomc), 2) if rangos_fomc else None
    range_expansion = round(avg_rango_fomc / avg_range_normal, 2) if avg_rango_fomc and avg_range_normal else None

    return {
        "proximo_fomc":        str(proximo.date()) if proximo else None,
        "dias_para_proximo":   dias_para_proximo,
        "ultimo_fomc":         str(ultimo.date()) if ultimo else None,
        "dias_desde_ultimo":   dias_desde_ultimo,
        "es_dia_fomc":         es_dia_fomc,
        "es_semana_fomc":      es_semana_fomc,
        "historico": {
            "total_fomc_analizados":   n,
            "avg_range_fomc_pct":      avg_rango_fomc,
            "avg_range_normal_pct":    round(avg_range_normal, 2),
            "range_expansion_ratio":   range_expansion,
            "retorno_dia_fomc_avg":    round(np.mean(retornos_fomc),  3) if retornos_fomc  else None,
            "retorno_pre2_avg":        round(np.mean(retornos_pre2),  3) if retornos_pre2  else None,
            "retorno_post1_avg":       round(np.mean(retornos_post1), 3) if retornos_post1 else None,
            "retorno_post3_avg":       round(np.mean(retornos_post3), 3) if retornos_post3 else None,
            "positive_rate_post1":     round(sum(1 for r in retornos_post1 if r > 0) / len(retornos_post1) * 100, 1) if retornos_post1 else None,
            "positive_rate_post3":     round(sum(1 for r in retornos_post3 if r > 0) / len(retornos_post3) * 100, 1) if retornos_post3 else None,
        }
    }


# ─── 11. ORQUESTADOR PRINCIPAL ───────────────────────────────────────────────

def calcular_todas_las_stats() -> dict:
    """
    Descarga datos y calcula todas las estadísticas.
    Guarda en cache JSON. Llamar una vez al día via scheduler o manualmente.
    """
    logger.info("Descargando datos historicos para Edge Analytics...")
    df_1h = fetch_ohlcv_binance(interval="1h", limit=3000)   # ~125 días horario (3 requests)
    df_1d = fetch_ohlcv_binance(interval="1d", limit=1825)   # ~5 años diario (2 requests)

    logger.info("Calculando estadisticas Edge Analytics...")
    stats = {
        "calculado_en": datetime.utcnow().isoformat(),
        "kill_zones":      calcular_kill_zones(df_1h),
        "sweep_returns":   calcular_sweep_returns(df_1h),
        "post_big_move":   calcular_post_big_move(df_1d),
        "day_of_week":     calcular_day_of_week(df_1d),
        "monthly_bias":    calcular_monthly_bias(df_1d),
        "week_of_month":   calcular_week_of_month(df_1d),
        "volatilidad":     calcular_volatilidad(df_1d),
        "cme_gap":         detectar_cme_gap(df_1h),
        "halving":         calcular_halving_cycle(),
        "fomc":            calcular_fomc_impact(df_1d),
        "session_dive":    calcular_session_deep_dive(df_1h),
    }

    # Guardar cache
    cache_path = os.path.join(os.path.dirname(__file__), "edge_stats_cache.json")
    with open(cache_path, "w") as f:
        json.dump(stats, f, indent=2, default=str)
    logger.info(f"Edge stats guardadas en {cache_path}")
    return stats


def cargar_stats_cache(ttl_horas: int = 24) -> dict:
    """Carga el cache JSON. Recalcula si no existe o tiene más de ttl_horas."""
    cache_path = os.path.join(os.path.dirname(__file__), "edge_stats_cache.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        calculado_en = cached.get("calculado_en")
        if calculado_en:
            try:
                ts = datetime.fromisoformat(calculado_en)
                if (datetime.utcnow() - ts).total_seconds() < ttl_horas * 3600:
                    return cached
            except ValueError:
                pass
    return calcular_todas_las_stats()


if __name__ == "__main__":
    stats = calcular_todas_las_stats()
    print(json.dumps(stats, indent=2, default=str))
