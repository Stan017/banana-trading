"""
bitunix_routes.py — Blueprint /api/bitunix
══════════════════════════════════════════
Endpoints:
    GET  /api/bitunix/status          → ¿tiene claves configuradas y válidas?
    POST /api/bitunix/keys            → guardar / actualizar API keys
    DELETE /api/bitunix/keys          → borrar API keys
    GET  /api/bitunix/balance         → balance de la cuenta
    POST /api/bitunix/sync            → sincronizar posiciones abiertas + historial
"""

import logging
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from models import db, Journal
import bitunix_client as bx

log = logging.getLogger(__name__)

bitunix_bp = Blueprint("bitunix", __name__, url_prefix="/api/bitunix")


# ────────────────────────────────────────────────────────────
# Helper — obtiene claves del usuario actual
# ────────────────────────────────────────────────────────────

def _get_keys():
    return current_user.bitunix_api_key, current_user.bitunix_secret_key

def _keys_ok() -> bool:
    k, s = _get_keys()
    return bool(k and s)


# ────────────────────────────────────────────────────────────
# GET /api/bitunix/status
# ────────────────────────────────────────────────────────────

@bitunix_bp.get("/status")
@login_required
def status():
    if not _keys_ok():
        return jsonify({"ok": True, "configurado": False})
    try:
        balance = bx.get_account(*_get_keys())
        return jsonify({"ok": True, "configurado": True, "balance": balance})
    except Exception as e:
        log.warning("bitunix status error uid=%s: %s", current_user.id, e)
        return jsonify({"ok": False, "configurado": True,
                        "error": "Invalid keys or network error"}), 200


# ────────────────────────────────────────────────────────────
# POST /api/bitunix/keys
# ────────────────────────────────────────────────────────────

@bitunix_bp.post("/keys")
@login_required
def save_keys():
    data       = request.get_json(silent=True) or {}
    api_key    = (data.get("api_key") or "").strip()
    secret_key = (data.get("secret_key") or "").strip()

    if not api_key or not secret_key:
        return jsonify({"ok": False, "error": "api_key and secret_key are required"}), 400

    # Max length to avoid garbage input
    if len(api_key) > 128 or len(secret_key) > 128:
        return jsonify({"ok": False, "error": "Key too long"}), 400

    # Validate before saving
    if not bx.validate_keys(api_key, secret_key):
        return jsonify({"ok": False,
                        "error": "Invalid keys or missing read permissions"}), 400

    current_user.bitunix_api_key    = api_key
    current_user.bitunix_secret_key = secret_key
    db.session.commit()
    return jsonify({"ok": True, "mensaje": "Keys saved successfully"})


# ────────────────────────────────────────────────────────────
# DELETE /api/bitunix/keys
# ────────────────────────────────────────────────────────────

@bitunix_bp.delete("/keys")
@login_required
def delete_keys():
    current_user.bitunix_api_key    = None
    current_user.bitunix_secret_key = None
    db.session.commit()
    return jsonify({"ok": True, "mensaje": "Keys deleted"})


# ────────────────────────────────────────────────────────────
# GET /api/bitunix/balance
# ────────────────────────────────────────────────────────────

@bitunix_bp.get("/balance")
@login_required
def balance():
    if not _keys_ok():
        return jsonify({"ok": False, "error": "API keys not configured"}), 400
    try:
        data = bx.get_account(*_get_keys())
        return jsonify({"ok": True, "balance": data})
    except Exception as e:
        log.error("bitunix balance error uid=%s: %s", current_user.id, e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ────────────────────────────────────────────────────────────
# POST /api/bitunix/sync
# ────────────────────────────────────────────────────────────

@bitunix_bp.post("/sync")
@login_required
def sync():
    """
    Sincroniza posiciones abiertas y cerradas de Bitunix al Journal.
    - Omite duplicados (exchange_trade_id ya existe para este usuario).
    - Retorna conteo de nuevos trades importados.
    """
    if not _keys_ok():
        return jsonify({"ok": False, "error": "API keys not configured"}), 400

    data       = request.get_json(silent=True) or {}
    solo_abiertas = data.get("solo_abiertas", False)
    limit_hist    = min(int(data.get("limit", 50)), 100)

    try:
        nuevos   = 0
        errores  = []

        # ── 1. Posiciones abiertas ──────────────────────────
        try:
            abiertas = bx.get_open_positions(*_get_keys())
        except Exception as e:
            abiertas = []
            errores.append(f"Open positions: {e}")

        for pos in abiertas:
            if _ya_existe(pos.get("exchange_trade_id")):
                continue
            _guardar_trade(pos)
            nuevos += 1

        # ── 2. Historial (cerradas) ─────────────────────────
        if not solo_abiertas:
            try:
                cerradas = bx.get_history_positions(*_get_keys(), limit=limit_hist)
            except Exception as e:
                cerradas = []
                errores.append(f"History: {e}")

            for pos in cerradas:
                if _ya_existe(pos.get("exchange_trade_id")):
                    continue
                _guardar_trade(pos)
                nuevos += 1

        db.session.commit()

        return jsonify({
            "ok"     : True,
            "nuevos" : nuevos,
            "errores": errores,
            "mensaje": f"{nuevos} trade(s) imported" + (
                f". Warnings: {len(errores)}" if errores else ""
            ),
        })

    except Exception as e:
        db.session.rollback()
        log.error("bitunix sync error uid=%s: %s", current_user.id, e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ────────────────────────────────────────────────────────────
# Helpers internos
# ────────────────────────────────────────────────────────────

def _ya_existe(exchange_trade_id: str | None) -> bool:
    """Retorna True si ese exchange_trade_id ya está en el journal del usuario."""
    if not exchange_trade_id:
        return False
    return Journal.query.filter_by(
        usuario_id=current_user.id,
        exchange_trade_id=exchange_trade_id
    ).first() is not None


def _guardar_trade(pos: dict):
    """Crea una entrada en Journal a partir del dict normalizado de bx."""
    trade = Journal(
        usuario_id        = current_user.id,
        activo            = pos.get("activo", "DESCONOCIDO"),
        direccion         = pos.get("direccion", "LONG"),
        entrada           = pos.get("entrada") or 0.0,
        sl                = pos.get("sl"),
        tp                = pos.get("tp"),
        precio_cierre     = pos.get("precio_cierre"),
        pnl_real          = pos.get("pnl_real"),
        estado            = pos.get("estado", "CERRADO"),
        apalancamiento    = pos.get("apalancamiento") or 1.0,
        margen_usado      = pos.get("margen_usado"),
        tipo_margen       = pos.get("tipo_margen", "AISLADO"),
        fecha_trade       = pos.get("fecha_trade"),
        fecha_cierre      = pos.get("fecha_cierre"),
        duracion_minutos  = pos.get("duracion_minutos"),
        rr_planeado       = pos.get("rr_planeado"),
        fuente            = "BITUNIX",
        exchange_trade_id = pos.get("exchange_trade_id"),
        tipo_trade        = pos.get("tipo_trade", "SCALP"),
    )
    db.session.add(trade)
