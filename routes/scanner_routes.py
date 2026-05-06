"""
scanner_routes.py — Blueprint de scanner, liquidez y edge analytics
════════════════════════════════════════════════════════════════════
Endpoints:
    GET /api/scanner      → scanner de confluencias v3 (caché 2 min)
    GET /api/liquidity    → L2 order book + liquidation zones (caché 60s)
    GET /edge             → página de edge analytics
    GET /api/edge/stats   → stats históricas completas como JSON
    GET /api/fng          → Fear & Greed Index — 30 días (caché 1h)
    GET /depth            → Market Depth — chart visual (TradingView + canvas)
    GET /api/depth/data   → candles + heatmap + L2 + liq zones en una llamada
"""

import logging
from datetime import datetime

from flask import Blueprint, jsonify, render_template
from flask_login import login_required
from utils.cache import CACHE_REGISTRY

try:
    from scanner import evaluar_confluencias, evaluar_multitf
except ImportError:
    def evaluar_confluencias(symbol):
        return {"setup_ok": False, "score": 0, "confluencias": [], "error": "Import fallido"}
    def evaluar_multitf(symbol):
        return {"ok": False, "error": "Import fallido"}

try:
    from binance_data import get_l2_liquidity, get_liquidation_zones, get_liquidation_heatmap
except ImportError:
    def get_l2_liquidity(symbol): return {"error": "Import fallido"}
    def get_liquidation_zones(symbol): return {}
    def get_liquidation_heatmap(symbol): return {"bins": [], "error": "Import fallido"}

logger = logging.getLogger(__name__)

scanner_bp = Blueprint("scanner_api", __name__)


@scanner_bp.route("/api/scanner")
@login_required
def api_scanner():
    """Scanner de confluencias v3 con scoring por capas. Caché 2 min."""
    cached = CACHE_REGISTRY["scanner"].get()
    if cached:
        return jsonify(cached)
    try:
        res = evaluar_confluencias("BTC/USDT")
        if res.get("error"):
            return jsonify({"ok": False, "error": res["error"]}), 500
        payload = {
            "ok":              True,
            "symbol":          res["symbol"],
            "precio":          res["precio"],
            "bias":            res.get("bias"),
            "regimen":         res.get("regimen", "INDEFINIDO"),
            "score":           res.get("score", 0),
            "score_macro":     res.get("score_macro", 0),
            "score_edge":      res.get("score_edge", 0),
            "score_tecnico":   res.get("score_tecnico", 0),
            "score_total":     res.get("score_total", 0),
            "conviction":      res.get("conviction", "BAJA"),
            "alerta_valida":   res.get("alerta_valida", False),
            "setup_ok":        res.get("setup_ok", False),
            "setup_potencial": res.get("setup_potencial", False),
            "falta":           res.get("falta"),
            "confluencias":    res.get("confluencias", []),
            "edge_desglose":   res.get("edge_desglose", {}),
            "macro_detalle":   res.get("macro_detalle", ""),
            "cvd_bias":        res.get("cvd_bias", "neutral"),
            "cvd_divergencia": res.get("cvd_divergencia", False),
            "cvd_delta":       res.get("cvd_delta"),
            "obs":             res.get("obs", []),
            "fvgs":            res.get("fvgs", []),
            "eqh_eql":         res.get("eqh_eql", {"eqh": [], "eql": []}),
            "hmm":             res.get("hmm", {}),
            "onchain":         res.get("onchain", {}),
            "vol":             res.get("vol", {}),
            "corr":            res.get("corr", {}),
            "timestamp":       res.get("timestamp", ""),
        }
        CACHE_REGISTRY["scanner"].set(payload)
        return jsonify(payload)
    except Exception as e:
        logger.error(f"Error en /api/scanner: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@scanner_bp.route("/api/liquidity")
@login_required
def api_liquidity():
    """L2 order book snapshot + liquidation zones. Caché 60s."""
    cached = CACHE_REGISTRY["liquidity"].get()
    if cached:
        return jsonify(cached)
    try:
        l2  = get_l2_liquidity("BTC/USDT")
        liq = get_liquidation_zones("BTC/USDT")
        if l2.get("error"):
            return jsonify({"ok": False, "error": l2["error"]}), 500
        payload = {
            "ok":               True,
            "imbalance_pct":    l2["imbalance_pct"],
            "imbalance_bias":   l2["imbalance_bias"],
            "top_bids":         l2["top_bids"],
            "top_asks":         l2["top_asks"],
            "nearest_bid_wall": l2["nearest_bid_wall"],
            "nearest_ask_wall": l2["nearest_ask_wall"],
            "bid_depth_1pct":   l2["bid_depth_1pct"],
            "ask_depth_1pct":   l2["ask_depth_1pct"],
            "bid_depth_2pct":   l2["bid_depth_2pct"],
            "ask_depth_2pct":   l2["ask_depth_2pct"],
            "precio_ref":       l2["precio_ref"],
            "liq_longs":        liq.get("longs", {}),
            "liq_shorts":       liq.get("shorts", {}),
            "liq_nota":         liq.get("nota", ""),
        }
        CACHE_REGISTRY["liquidity"].set(payload)
        return jsonify(payload)
    except Exception as e:
        logger.error(f"Error en /api/liquidity: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@scanner_bp.route("/edge")
@login_required
def edge_analytics():
    return render_template("edge.html")


@scanner_bp.route("/liquidity")
@login_required
def liquidity_map():
    return render_template("liquidity.html")


@scanner_bp.route("/api/edge/stats")
@login_required
def api_edge_stats():
    """Devuelve todas las edge stats como JSON para el frontend."""
    try:
        from stats_engine import cargar_stats_cache
        stats = cargar_stats_cache()
        return jsonify({"ok": True, "stats": stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@scanner_bp.route("/api/liquidity/heatmap")
@login_required
def api_liquidity_heatmap():
    """
    Heatmap de liquidaciones estimado con datos propios de Binance.
    Combina: leverage math (2x-125x) + volume profile 500H + wick analysis.
    Caché 90s en binance_data.
    """
    try:
        data = get_liquidation_heatmap("BTC/USDT")
        if data.get("error") and not data.get("bins"):
            return jsonify({"ok": False, "error": data["error"]}), 500
        return jsonify({"ok": True, **data})
    except Exception as e:
        logger.error(f"Error en /api/liquidity/heatmap: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@scanner_bp.route("/api/order-flow")
@login_required
def api_order_flow():
    """
    Order Flow Imbalance en tiempo real via WebSocket aggTrade.
    Ventanas rolling: 1 minuto y 5 minutos.

    Retorna:
        ratio_1m / ratio_5m  : % volumen comprador (50 = neutral)
        bias_1m  / bias_5m   : "buy" | "sell" | "neutral"
        buy_vol_1m / sell_vol_1m : USD en la ventana de 1m
        buy_vol_5m / sell_vol_5m : USD en la ventana de 5m
        delta_1m / delta_5m  : diferencia buy - sell USD
        trades_1m / trades_5m: nº de trades en cada ventana
        updated_at           : epoch del último trade procesado
        active               : bool — True si el WS está recibiendo datos
    """
    try:
        from ws_monitor import get_monitor
        monitor = get_monitor()
        if monitor is None:
            return jsonify({"ok": False, "error": "WS monitor no iniciado"}), 503

        flow = monitor.get_order_flow("BTC/USDT")
        # Considerar "activo" si recibimos trades en los últimos 30s
        import time as _t
        active = flow["updated_at"] > 0 and (_t.time() - flow["updated_at"]) < 30

        return jsonify({"ok": True, "active": active, **flow})
    except Exception as e:
        logger.error(f"Error en /api/order-flow: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@scanner_bp.route("/api/fng")
@login_required
def api_fng():
    """Fear & Greed Index — 30 días de historial. Caché 1h en binance_data."""
    try:
        from binance_data import get_fear_greed
        data = get_fear_greed()
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@scanner_bp.route("/api/scanner/multitf")
@login_required
def api_scanner_multitf():
    """
    Scanner multi-TF: HTF (4H) + LTF (15M) en paralelo.
    Retorna alineación CONFLUENCIA / ESPERA / DIVERGENTE + trigger.
    Caché 90s.
    """
    cached = CACHE_REGISTRY["scanner_multitf"].get()
    if cached:
        return jsonify(cached)
    try:
        res = evaluar_multitf("BTC/USDT")
        if res.get("error") and not res.get("htf"):
            return jsonify({"ok": False, "error": res["error"]}), 500
        CACHE_REGISTRY["scanner_multitf"].set(res)
        return jsonify(res)
    except Exception as e:
        logger.error(f"Error en /api/scanner/multitf: {e}")
        stale = CACHE_REGISTRY["scanner_multitf"].get_stale()
        if stale:
            return jsonify(stale)
        return jsonify({"ok": False, "error": str(e)}), 500


@scanner_bp.route("/api/onchain")
@login_required
def api_onchain():
    """
    Métricas on-chain: NUPL, MVRV, Realized Price (aprox SMA365D).
    Fuente: Binance histórico 1D — sin API externa, siempre disponible. Caché 4h.
    """
    try:
        from onchain import get_onchain_resumen
        from binance_data import get_precio_actual
        try:
            precio = get_precio_actual("BTC/USDT").get("precio")
        except Exception:
            precio = None
        data = get_onchain_resumen(precio_actual=precio)
        return jsonify({"ok": True, **data})
    except Exception as e:
        logger.error(f"Error en /api/onchain: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@scanner_bp.route("/api/correlation")
@login_required
def api_correlation():
    """
    Correlation Matrix: BTC vs SPX (^GSPC) + Gold (GC=F).
    Ventanas 30D y 90D. Fuente: yfinance. Caché 1h interno.
    """
    try:
        from correlation import get_corr_resumen
        data = get_corr_resumen()
        return jsonify({"ok": True, **data})
    except Exception as e:
        logger.error(f"Error en /api/correlation: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@scanner_bp.route("/api/vol/surface")
@login_required
def api_vol_surface():
    """
    Volatility Surface: DVOL index + term structure + IV skew proxy.
    Fuente: Deribit Public API (sin autenticación). Caché 5 min interno.
    """
    try:
        from deribit_vol import get_vol_resumen
        from binance_data import get_precio_actual
        try:
            precio = get_precio_actual("BTC/USDT").get("precio")
        except Exception:
            precio = None
        data = get_vol_resumen(spot=precio)
        return jsonify({"ok": True, **data})
    except Exception as e:
        logger.error(f"Error en /api/vol/surface: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500



@scanner_bp.route("/api/regime/hmm")
@login_required
def api_regime_hmm():
    """
    Estado actual del régimen HMM + probabilidades de cada estado.
    Debug endpoint — muestra qué está viendo el modelo en tiempo real.
    """
    try:
        from hmm_regime import get_regimen_hmm, hmm_bloque_contexto, get_detector
        resultado = get_regimen_hmm("BTC/USDT")
        det = get_detector()
        return jsonify({
            "ok":           True,
            "ready":        det.is_ready,
            "trained_at":   det.trained_at,
            "estado":       resultado["estado"],
            "confianza":    resultado["confianza"],
            "probabilidades": resultado["probabilidades"],
            "recientes":    resultado["recientes"],
            "transicion":   resultado["transicion"],
            "conf_trend":   resultado["conf_trend"],
            "bloque_llm":   hmm_bloque_contexto(resultado),
            "error":        resultado.get("error"),
        })
    except Exception as e:
        logger.error(f"Error en /api/regime/hmm: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@scanner_bp.route("/api/context-debug")
@login_required
def api_context_debug():
    """
    Debug: muestra los 3 bloques de contexto nuevos (delta, VP, HTF)
    que se inyectan al LLM en cada consulta de chat.

    Query params:
      ?symbol=BTC/USDT  (default: BTC/USDT)
      ?tf=4h            (default: 4h)
    """
    from flask import request as _req
    symbol = _req.args.get("symbol", "BTC/USDT").upper()
    tf     = _req.args.get("tf", "4h").lower()
    if tf not in ("15m", "1h", "4h", "1d"):
        tf = "4h"

    resultado = {"ok": True, "symbol": symbol, "tf": tf}

    # ── Delta por vela ────────────────────────────────────────
    try:
        from analysis.delta import get_delta_per_candle, format_delta_context
        deltas = get_delta_per_candle(symbol, tf, n=6)
        resultado["delta"] = {
            "texto": format_delta_context(deltas, tf),
            "velas": deltas,
        }
    except Exception as e:
        resultado["delta"] = {"error": str(e)}

    # ── Volume Profile ────────────────────────────────────────
    try:
        from analysis.volume_profile import get_volume_profile, format_vp_context
        vp = get_volume_profile(symbol, tf)
        resultado["volume_profile"] = {
            "texto": format_vp_context(vp),
            "datos": vp,
        }
    except Exception as e:
        resultado["volume_profile"] = {"error": str(e)}

    # ── HTF Context Superior ──────────────────────────────────
    try:
        from binance_data import get_contexto_superior
        htf = get_contexto_superior(symbol, tf)
        resultado["htf"] = {"texto": htf}
    except Exception as e:
        resultado["htf"] = {"error": str(e)}

    return jsonify(resultado)


# ══════════════════════════════════════════════════════════════════════
# MARKET DEPTH — chart visual con TradingView + canvas overlay
# ══════════════════════════════════════════════════════════════════════

@scanner_bp.route("/depth")
@login_required
def depth_view():
    return render_template("depth.html")


@scanner_bp.route("/api/depth/data")
@login_required
def api_depth_data():
    """
    Agrega todos los datos para el Market Depth en una sola llamada.

    Query params:
      ?tf=4h      (default: 4h) — 15m | 1h | 4h | 1d
      ?symbol=BTC/USDT (default: BTC/USDT)

    Retorna:
      candles[]     → OHLCV formateado para TradingView (time en Unix segundos)
      heatmap{}     → bins de liquidación + VP + liq_levels
      l2{}          → top bids/asks, nearest walls, imbalance
      liq_zones{}   → niveles por leverage (longs/shorts)
      precio_actual → float
      tf, symbol
    """
    from flask import request as _req
    symbol = _req.args.get("symbol", "BTC/USDT").upper()
    tf     = _req.args.get("tf", "4h").lower()
    if tf not in ("15m", "1h", "4h", "1d"):
        tf = "4h"

    # Límite de velas por TF — suficiente para un buen rango visual
    _LIMITE = {"15m": 400, "1h": 400, "4h": 300, "1d": 300}
    limite = _LIMITE.get(tf, 300)

    try:
        from binance_data import (
            get_velas, get_precio_actual,
            get_liquidation_heatmap, get_l2_liquidity, get_liquidation_zones,
            get_real_liq_heatmap, get_2d_heatmap,
        )

        precio_data = get_precio_actual(symbol)
        precio_actual = precio_data.get("precio")

        # ── Velas → formato TradingView (time en Unix segundos) ──────────
        velas_raw = get_velas(symbol, tf, limite)
        candles = []
        volumes = []
        for v in velas_raw:
            try:
                ts = int(datetime.strptime(v["fecha"], "%Y-%m-%d %H:%M").timestamp())
            except Exception:
                try:
                    ts = int(datetime.strptime(v["fecha"][:10], "%Y-%m-%d").timestamp())
                except Exception:
                    continue
            candles.append({
                "time":  ts,
                "open":  v["open"],
                "high":  v["high"],
                "low":   v["low"],
                "close": v["close"],
            })
            volumes.append({
                "time":  ts,
                "value": v["volumen"],
                "color": "#26a69a" if v["close"] >= v["open"] else "#ef5350",
            })

        # ── Heatmap de liquidaciones + VP ────────────────────────────────
        heatmap = get_liquidation_heatmap(symbol)

        # ── L2 order book ─────────────────────────────────────────────────
        l2 = get_l2_liquidity(symbol)
        l2_clean = {
            "imbalance_pct":    l2.get("imbalance_pct"),
            "imbalance_bias":   l2.get("imbalance_bias"),
            "nearest_bid_wall": l2.get("nearest_bid_wall"),
            "nearest_ask_wall": l2.get("nearest_ask_wall"),
            "bid_depth_1pct":   l2.get("bid_depth_1pct"),
            "ask_depth_1pct":   l2.get("ask_depth_1pct"),
            "top_bids":         l2.get("top_bids", [])[:5],
            "top_asks":         l2.get("top_asks", [])[:5],
            "precio_ref":       l2.get("precio_ref"),
        } if not l2.get("error") else {}

        # ── Liquidation zones ─────────────────────────────────────────────
        liq_zones = get_liquidation_zones(symbol)

        # ── Heatmap 2D ────────────────────────────────────────────────────
        # Usa datos reales de liquidaciones si depth_worker.py está activo
        # y liq_events.db tiene datos. Si no, va directo al modelo teórico.
        # (El endpoint REST /fapi/v1/allForceOrders está deprecado — HTTP 400)
        import os as _os
        _DB_PATH = _os.path.join(_os.path.dirname(__file__), '..', 'liq_events.db')
        _db_ready = _os.path.exists(_DB_PATH) and _os.path.getsize(_DB_PATH) > 8192

        if _db_ready:
            grid2d = get_real_liq_heatmap(candles, tf=tf, symbol=symbol)
            if not grid2d.get('agg_grids') and precio_actual:   # fallback: formato incompatible
                grid2d = get_2d_heatmap(candles, precio_actual, tf=tf)
        else:
            grid2d = get_2d_heatmap(candles, precio_actual, tf=tf) if precio_actual else {}

        return jsonify({
            "ok":            True,
            "symbol":        symbol,
            "tf":            tf,
            "precio_actual": precio_actual,
            "candles":       candles,
            "volumes":       volumes,
            "heatmap":       heatmap,
            "l2":            l2_clean,
            "liq_zones":     liq_zones,
            "grid2d":        grid2d,
        })

    except Exception as e:
        logger.error(f"Error en /api/depth/data: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
