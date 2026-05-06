"""
auth_routes.py — Blueprint de autenticación
════════════════════════════════════════════
Endpoints:
    GET  /login              → página de login
    POST /login              → login con email + password
    POST /register           → registro de nuevo usuario
    GET  /logout             → cierra sesión
    GET  /me                 → datos del usuario autenticado
"""

import logging
import threading
import pyotp
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, session
from flask_login import login_user, logout_user, login_required, current_user

from models import db, Usuario
from email_service import enviar_bienvenida
from utils.helpers import (
    get_client_ip,
    check_rate_limit,
    check_brute_force,
    register_failed_login,
    clear_failed_logins,
    is_valid_email,
    IS_PROD,
)

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))
    return render_template("login.html")


@auth_bp.route("/login", methods=["POST"])
def login_post():
    """Login con email y password."""
    ip       = get_client_ip()
    data     = request.json or {}
    email    = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"ok": False, "error": "Email y contraseña requeridos"}), 400

    if check_brute_force(ip):
        logger.warning(f"Fuerza bruta bloqueada — IP: {ip}")
        return jsonify({"ok": False, "error": "Demasiados intentos fallidos. Espera 5 minutos."}), 429

    usuario = Usuario.query.filter_by(email=email).first()
    if not usuario or not usuario.check_password(password):
        register_failed_login(ip)
        _email_log = email[:3] + "***" if IS_PROD else email
        logger.warning(f"Login fallido — IP: {ip} — email: {_email_log}")
        return jsonify({"ok": False, "error": "Credenciales incorrectas"}), 401

    if not usuario.activo:
        return jsonify({"ok": False, "error": "Cuenta desactivada"}), 403

    clear_failed_logins(ip)

    # ── 2FA: si el usuario lo tiene habilitado, pausa antes de login_user ──
    if usuario.totp_habilitado and usuario.totp_secret:
        session["pending_2fa_uid"] = usuario.id   # temporal — expira con la sesión
        return jsonify({"ok": False, "require_2fa": True})

    login_user(usuario, remember=True)
    session.permanent = True
    usuario.actualizar_acceso()
    return jsonify({"ok": True, "redirect": "/"})


@auth_bp.route("/register", methods=["POST"])
def register():
    """Registro con email y password."""
    ip = get_client_ip()
    if not check_rate_limit(ip):
        return jsonify({"ok": False, "error": "Demasiadas solicitudes. Espera un momento."}), 429

    data     = request.json or {}
    email    = data.get("email", "").strip().lower()
    nombre   = data.get("nombre", "").strip()
    password = data.get("password", "")

    if not email or not password or not nombre:
        return jsonify({"ok": False, "error": "Todos los campos son requeridos"}), 400

    if not is_valid_email(email):
        return jsonify({"ok": False, "error": "El email no tiene un formato válido"}), 400

    if len(password) < 8:
        return jsonify({"ok": False, "error": "La contraseña debe tener al menos 8 caracteres"}), 400

    if len(nombre) < 2 or len(nombre) > 50:
        return jsonify({"ok": False, "error": "El nombre debe tener entre 2 y 50 caracteres"}), 400

    if Usuario.query.filter_by(email=email).first():
        return jsonify({"ok": False, "error": "Este email ya está registrado"}), 409

    usuario = Usuario(email=email, nombre=nombre)
    usuario.set_password(password)
    db.session.add(usuario)
    db.session.commit()
    threading.Thread(target=enviar_bienvenida, args=(email, nombre), daemon=True).start()

    login_user(usuario, remember=True)
    session.permanent = True
    return jsonify({"ok": True, "redirect": "/"})


@auth_bp.route("/login/verify-2fa", methods=["POST"])
def login_verify_2fa():
    """
    Segunda etapa del login cuando 2FA está habilitado.
    Espera {"code": "123456"} y el uid guardado en sesión.
    """
    uid = session.get("pending_2fa_uid")
    if not uid:
        return jsonify({"ok": False, "error": "Sesión de 2FA expirada. Vuelve a iniciar sesión."}), 400

    data = request.json or {}
    code = data.get("code", "").strip().replace(" ", "")

    if not code or len(code) != 6 or not code.isdigit():
        return jsonify({"ok": False, "error": "Código inválido — debe ser 6 dígitos."}), 400

    usuario = db.session.get(Usuario, uid)
    if not usuario or not usuario.totp_secret:
        session.pop("pending_2fa_uid", None)
        return jsonify({"ok": False, "error": "Error de sesión. Vuelve a iniciar sesión."}), 400

    totp = pyotp.TOTP(usuario.totp_secret)
    if not totp.verify(code, valid_window=1):   # ±30 seg de tolerancia
        logger.warning(f"2FA fallido — usuario: {usuario.id}")
        return jsonify({"ok": False, "error": "Código incorrecto o expirado."}), 401

    session.pop("pending_2fa_uid", None)
    login_user(usuario, remember=True)
    session.permanent = True
    usuario.actualizar_acceso()
    return jsonify({"ok": True, "redirect": "/"})


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login_page"))


@auth_bp.route("/me")
@login_required
def me():
    return jsonify({
        "logueado": True,
        "usuario" : current_user.to_dict()
    })
