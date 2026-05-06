"""
main_routes.py — Blueprint de rutas principales
═════════════════════════════════════════════════
Endpoints:
    GET  /landing     → página de aterrizaje (pública)
    GET  /            → app principal (login required)
    GET  /info        → count de chunks de la KB
    POST /reload-kb   → recarga la knowledge base (requiere ADMIN_TOKEN)
    GET  /mercado     → resumen de todos los activos (paralelo)
    GET  /precio      → precio actual de BTC
    GET  /macro       → DXY + BTC dominance
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, jsonify, render_template
from flask_login import login_required, current_user

try:
    from binance_data import (
        get_precio_actual,
        get_resumen_sidebar,
        ACTIVOS,
        get_dxy,
        get_btc_dominance,
    )
except ImportError:
    def get_precio_actual(symbol="BTC/USDT"): return None
    def get_resumen_sidebar(symbol="BTC/USDT"): return None
    def get_dxy(): return None
    def get_btc_dominance(): return None
    ACTIVOS = {"BTC": "BTC/USDT"}

from resources import chunks_count as _chunks_count_inicial, recargar_kb
from utils.helpers import check_admin_token, get_client_ip

logger = logging.getLogger(__name__)

main_bp = Blueprint("main", __name__)


@main_bp.route("/landing")
def landing():
    return render_template("landing.html")


@main_bp.route("/")
@login_required
def index():
    return render_template("index.html", chunks=_chunks_count_inicial)


@main_bp.route("/info")
def info():
    return jsonify({"chunks": _chunks_count_inicial})


@main_bp.route("/reload-kb", methods=["POST"])
def reload_kb():
    """Recarga la KB — requiere token admin en Authorization header."""
    if not check_admin_token():
        logger.error(f"Intento no autorizado a /reload-kb desde {get_client_ip()}")
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    try:
        nuevo_count = recargar_kb()
        return jsonify({"ok": True, "chunks": nuevo_count})
    except Exception as e:
        logger.error(f"Error en reload-kb: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@main_bp.route("/mercado")
def mercado():
    """Datos compactos de todos los activos — paralelo con ThreadPoolExecutor."""
    try:
        resultados = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            futuros = {
                executor.submit(get_resumen_sidebar, symbol): nombre
                for nombre, symbol in ACTIVOS.items()
            }
            for futuro in as_completed(futuros):
                nombre = futuros[futuro]
                try:
                    resultados[nombre] = futuro.result(timeout=10)
                except Exception as e:
                    resultados[nombre] = {"symbol": ACTIVOS[nombre], "error": str(e)}
        return jsonify(resultados)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@main_bp.route("/precio")
def precio():
    try:
        data = get_precio_actual()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@main_bp.route("/api/me")
@login_required
def api_me():
    """Datos básicos del usuario autenticado — para chips de avatar en todas las páginas."""
    return jsonify({
        "email":  current_user.email,
        "nombre": current_user.nombre or current_user.email.split("@")[0],
        "plan":   current_user.plan or "free",
        "avatar": getattr(current_user, "avatar_url", None),
    })


@main_bp.route("/macro")
def macro():
    """DXY + BTC Dominance en tiempo real."""
    try:
        dxy  = get_dxy()
        btcd = get_btc_dominance()
        return jsonify({"dxy": dxy, "btcd": btcd})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
