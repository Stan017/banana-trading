"""
profile_routes.py — Blueprint de perfil y notificaciones
══════════════════════════════════════════════════════════
Endpoints:
    GET    /perfil                              → página de perfil
    GET    /perfil/datos                        → datos del perfil como JSON
    POST   /perfil/cambiar-password             → cambiar contraseña (solo email users)
    GET    /api/notificaciones                  → últimas 20 notificaciones
    POST   /api/notificaciones/<id>/leer        → marcar una como leída
    POST   /api/notificaciones/leer-todas       → marcar todas como leídas
    DELETE /api/notificaciones/<id>             → eliminar una notificación
    DELETE /api/notificaciones/eliminar-todas   → eliminar todas las notificaciones
"""

import io
import base64
import logging
from datetime import date

import pyotp
import qrcode
from cryptography.fernet import Fernet, InvalidToken

from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user
from sqlalchemy import func

from models import db, Journal, UsoDiario, HistorialChat, Notificacion
from config import FERNET_KEY

logger = logging.getLogger(__name__)

profile_bp = Blueprint("profile", __name__)


@profile_bp.route("/perfil")
@login_required
def perfil_page():
    return render_template("perfil.html")


@profile_bp.route("/perfil/datos")
@login_required
def perfil_datos():
    """Devuelve todos los datos necesarios para la página de perfil."""
    uso = UsoDiario.query.filter_by(
        usuario_id=current_user.id, fecha=date.today()
    ).first()
    consultas_hoy = uso.consultas if uso else 0
    limite        = 10 if current_user.plan == "free" else 999

    total_trades = Journal.query.filter_by(usuario_id=current_user.id).count()
    wins   = Journal.query.filter_by(usuario_id=current_user.id, resultado="WIN").count()
    losses = Journal.query.filter_by(usuario_id=current_user.id, resultado="LOSS").count()

    rr_avg = db.session.query(func.avg(Journal.rr_real)).filter(
        Journal.usuario_id == current_user.id,
        Journal.rr_real.isnot(None)
    ).scalar()

    reciente = []

    trades = Journal.query.filter_by(usuario_id=current_user.id)\
        .order_by(Journal.fecha_trade.desc()).limit(3).all()
    for t in trades:
        emoji = "✅" if t.resultado == "WIN" else "❌" if t.resultado == "LOSS" else "➖"
        reciente.append({
            "tipo" : "trade",
            "texto": f"{emoji} {t.activo} {t.direccion} — {t.resultado or 'Pendiente'}",
            "fecha": str(t.fecha_trade)[:10] if t.fecha_trade else "—",
            "ts":    str(t.fecha_trade) if t.fecha_trade else ""
        })

    mensajes = HistorialChat.query.filter_by(
        usuario_id=current_user.id, rol="user"
    ).order_by(HistorialChat.id.desc()).limit(3).all()
    for m in mensajes:
        texto = m.contenido[:60] + "..." if len(m.contenido) > 60 else m.contenido
        reciente.append({
            "tipo" : "chat",
            "texto": texto,
            "fecha": str(m.creado_en)[:16] if m.creado_en else "Chat",
            "ts"   : str(m.creado_en) if m.creado_en else str(m.id)
        })

    reciente = sorted(reciente, key=lambda x: x["ts"], reverse=True)[:5]

    return jsonify({
        "ok": True,
        "uso": {
            "consultas_hoy": consultas_hoy,
            "limite"       : limite,
        },
        "stats": {
            "total_trades": total_trades,
            "wins"        : wins,
            "losses"      : losses,
            "rr_promedio" : round(float(rr_avg), 2) if rr_avg else None,
        },
        "actividad_reciente": reciente,
    })


@profile_bp.route("/perfil/cambiar-password", methods=["POST"])
@login_required
def cambiar_password():
    """Cambia la contraseña del usuario — solo para cuentas email."""
    if current_user.google_id:
        return jsonify({"ok": False, "error": "Google users cannot change their password here."}), 400

    data   = request.json or {}
    actual = data.get("actual", "")
    nueva  = data.get("nueva", "")

    if not actual or not nueva:
        return jsonify({"ok": False, "error": "Missing fields"}), 400
    if len(nueva) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters"}), 400
    if not current_user.check_password(actual):
        return jsonify({"ok": False, "error": "Current password is incorrect"}), 401

    from utils.helpers import IS_PROD
    current_user.set_password(nueva)
    db.session.commit()
    _email_log = current_user.email[:3] + "***" if IS_PROD else current_user.email
    logger.warning(f"Password changed — user: {_email_log}")
    return jsonify({"ok": True})


# ── Notificaciones ────────────────────────────────────────────────────────────

@profile_bp.route("/api/notificaciones")
@login_required
def api_notificaciones_get():
    """Devuelve las últimas 20 notificaciones del usuario (no leídas primero)."""
    notifs = (
        Notificacion.query
        .filter_by(usuario_id=current_user.id)
        .order_by(Notificacion.leida.asc(), Notificacion.creada_en.desc())
        .limit(20)
        .all()
    )
    no_leidas = Notificacion.query.filter_by(
        usuario_id=current_user.id, leida=False
    ).count()
    return jsonify({
        "ok": True,
        "no_leidas": no_leidas,
        "notificaciones": [n.to_dict() for n in notifs],
    })


@profile_bp.route("/api/notificaciones/<int:nid>/leer", methods=["POST"])
@login_required
def api_notificacion_leer(nid):
    """Marca una notificación como leída."""
    n = Notificacion.query.filter_by(id=nid, usuario_id=current_user.id).first()
    if n:
        n.leida = True
        db.session.commit()
    return jsonify({"ok": True})


@profile_bp.route("/api/notificaciones/leer-todas", methods=["POST"])
@login_required
def api_notificaciones_leer_todas():
    """Marca todas las notificaciones del usuario como leídas."""
    Notificacion.query.filter_by(
        usuario_id=current_user.id, leida=False
    ).update({"leida": True})
    db.session.commit()
    return jsonify({"ok": True})


@profile_bp.route("/api/notificaciones/<int:nid>", methods=["DELETE"])
@login_required
def api_notificacion_eliminar(nid):
    """Elimina una notificación del usuario."""
    n = Notificacion.query.filter_by(id=nid, usuario_id=current_user.id).first()
    if n:
        db.session.delete(n)
        db.session.commit()
    return jsonify({"ok": True})


@profile_bp.route("/api/notificaciones/eliminar-todas", methods=["DELETE"])
@login_required
def api_notificaciones_eliminar_todas():
    """Elimina todas las notificaciones del usuario."""
    Notificacion.query.filter_by(usuario_id=current_user.id).delete()
    db.session.commit()
    return jsonify({"ok": True})


# ============================================================
# 2FA — TOTP (Google Authenticator compatible)
# ============================================================

@profile_bp.route("/perfil/2fa/setup")
@login_required
def setup_2fa():
    """
    Genera un nuevo secreto TOTP y devuelve el QR como imagen base64.
    El secreto se guarda en sesión — NO en DB hasta que el usuario confirme.
    """
    secret = pyotp.random_base32()

    # Guardar en sesión temporalmente (el usuario aún no ha verificado)
    from flask import session
    session["pending_totp_secret"] = secret

    # Construir OTP provisioning URI
    app_name  = "TradeBot AI"
    totp_uri  = pyotp.totp.TOTP(secret).provisioning_uri(
        name=current_user.email,
        issuer_name=app_name,
    )

    # Generar imagen QR en memoria
    img = qrcode.make(totp_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return jsonify({
        "ok"    : True,
        "qr"    : f"data:image/png;base64,{qr_b64}",
        "secret": secret,   # backup manual por si no pueden escanear
    })


@profile_bp.route("/perfil/2fa/enable", methods=["POST"])
@login_required
def enable_2fa():
    """
    Confirma el TOTP con el código que el usuario ve en el autenticador.
    Solo entonces guarda el secreto en DB y activa 2FA.
    """
    from flask import session
    secret = session.get("pending_totp_secret")
    if not secret:
        return jsonify({"ok": False, "error": "Setup session expired. Please restart setup."}), 400

    data = request.json or {}
    code = data.get("code", "").strip().replace(" ", "")

    if not code or len(code) != 6 or not code.isdigit():
        return jsonify({"ok": False, "error": "Code must be 6 digits."}), 400

    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=1):
        return jsonify({"ok": False, "error": "Incorrect code. Verify your device clock."}), 401

    current_user.totp_secret     = secret
    current_user.totp_habilitado = True
    db.session.commit()
    session.pop("pending_totp_secret", None)

    logger.warning(f"2FA enabled — user: {current_user.id}")
    return jsonify({"ok": True, "mensaje": "2FA enabled successfully."})


@profile_bp.route("/perfil/2fa/disable", methods=["POST"])
@login_required
def disable_2fa():
    """
    Desactiva 2FA previa verificación del código actual.
    Requiere: {"code": "123456"}
    """
    if not current_user.totp_habilitado or not current_user.totp_secret:
        return jsonify({"ok": False, "error": "2FA is not enabled."}), 400

    data = request.json or {}
    code = data.get("code", "").strip().replace(" ", "")

    if not code or len(code) != 6 or not code.isdigit():
        return jsonify({"ok": False, "error": "Code must be 6 digits."}), 400

    totp = pyotp.TOTP(current_user.totp_secret)
    if not totp.verify(code, valid_window=1):
        return jsonify({"ok": False, "error": "Incorrect code."}), 401

    current_user.totp_secret     = None
    current_user.totp_habilitado = False
    db.session.commit()

    logger.warning(f"2FA disabled — user: {current_user.id}")
    return jsonify({"ok": True, "mensaje": "2FA disabled."})


# ============================================================
# BYOK — Bring Your Own Key (solo usuarios Pro)
# ============================================================

def _fernet() -> Fernet:
    return Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)


@profile_bp.route("/perfil/byok", methods=["POST"])
@login_required
def byok_set():
    """
    Guarda la API key de Anthropic cifrada con Fernet.
    Solo disponible para usuarios Pro.
    Requiere: {"api_key": "sk-ant-..."}
    """
    if not current_user.es_pro():
        return jsonify({
            "ok"     : False,
            "error"  : "BYOK is exclusive to the Pro plan.",
            "upgrade": True,
        }), 403

    data    = request.json or {}
    api_key = data.get("api_key", "").strip()

    if not api_key:
        return jsonify({"ok": False, "error": "Empty API key."}), 400

    # Basic format validation (sk-ant- or sk-)
    if not (api_key.startswith("sk-ant-") or api_key.startswith("sk-")):
        return jsonify({"ok": False, "error": "Invalid API key format. Must start with sk-ant-"}), 400

    if len(api_key) < 20 or len(api_key) > 200:
        return jsonify({"ok": False, "error": "API key length out of range."}), 400

    try:
        f   = _fernet()
        enc = f.encrypt(api_key.encode()).decode()
        current_user.anthropic_key_enc = enc
        db.session.commit()
    except Exception as exc:
        logger.error(f"Error encrypting BYOK key — user: {current_user.id} — {exc}")
        return jsonify({"ok": False, "error": "Internal error saving the key."}), 500

    logger.warning(f"BYOK key saved — user: {current_user.id}")
    return jsonify({"ok": True, "mensaje": "API key saved successfully. It will be used in your requests."})


@profile_bp.route("/perfil/byok", methods=["DELETE"])
@login_required
def byok_delete():
    """Elimina la BYOK key del usuario."""
    current_user.anthropic_key_enc = None
    db.session.commit()
    return jsonify({"ok": True, "mensaje": "API key deleted. System key will be used."})


@profile_bp.route("/perfil/byok/status")
@login_required
def byok_status():
    """Estado de la BYOK key — sin devolver la key en ningún caso."""
    return jsonify({
        "ok"             : True,
        "byok_configurado": bool(current_user.anthropic_key_enc),
        "es_pro"         : current_user.es_pro(),
    })
