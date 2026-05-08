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
        return jsonify({"ok": False, "error": "Email and password required"}), 400

    if check_brute_force(ip):
        logger.warning(f"Brute force blocked — IP: {ip}")
        return jsonify({"ok": False, "error": "Too many failed attempts. Wait 5 minutes."}), 429

    usuario = Usuario.query.filter_by(email=email).first()
    if not usuario or not usuario.check_password(password):
        register_failed_login(ip)
        _email_log = email[:3] + "***" if IS_PROD else email
        logger.warning(f"Login failed — IP: {ip} — email: {_email_log}")
        return jsonify({"ok": False, "error": "Invalid credentials"}), 401

    if not usuario.activo:
        return jsonify({"ok": False, "error": "Account disabled"}), 403

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
        return jsonify({"ok": False, "error": "Too many requests. Wait a moment."}), 429

    data     = request.json or {}
    email    = data.get("email", "").strip().lower()
    nombre   = data.get("nombre", "").strip()
    password = data.get("password", "")

    if not email or not password or not nombre:
        return jsonify({"ok": False, "error": "All fields are required"}), 400

    if not is_valid_email(email):
        return jsonify({"ok": False, "error": "Invalid email format"}), 400

    if len(password) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters"}), 400

    if len(nombre) < 2 or len(nombre) > 50:
        return jsonify({"ok": False, "error": "Name must be between 2 and 50 characters"}), 400

    if Usuario.query.filter_by(email=email).first():
        return jsonify({"ok": False, "error": "This email is already registered"}), 409

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
        return jsonify({"ok": False, "error": "2FA session expired. Please log in again."}), 400

    data = request.json or {}
    code = data.get("code", "").strip().replace(" ", "")

    if not code or len(code) != 6 or not code.isdigit():
        return jsonify({"ok": False, "error": "Invalid code — must be 6 digits."}), 400

    usuario = db.session.get(Usuario, uid)
    if not usuario or not usuario.totp_secret:
        session.pop("pending_2fa_uid", None)
        return jsonify({"ok": False, "error": "Session error. Please log in again."}), 400

    totp = pyotp.TOTP(usuario.totp_secret)
    if not totp.verify(code, valid_window=1):   # ±30 sec tolerance
        logger.warning(f"2FA failed — user: {usuario.id}")
        return jsonify({"ok": False, "error": "Incorrect or expired code."}), 401

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
