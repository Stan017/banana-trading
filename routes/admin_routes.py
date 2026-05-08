"""
admin_routes.py — Endpoints de administración de TradeBot AI
Requieren Authorization: Bearer <ADMIN_TOKEN> en el header.

Endpoints:
  GET  /admin/usuarios          → lista todos los usuarios
  POST /admin/set-plan          → cambia el plan de un usuario
  POST /admin/toggle-activo     → activa/desactiva un usuario
  GET  /admin/stats             → estadísticas globales de la app
"""

import hmac
import logging
from datetime import date

from flask import Blueprint, request, jsonify
from models import db, Usuario, UsoDiario, HistorialChat, Journal
from config import ADMIN_TOKEN

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# ── Helper — verificar token ─────────────────────────────────
def _check_token() -> bool:
    """Compara el Bearer token de forma segura contra timing attacks."""
    auth     = request.headers.get("Authorization", "")
    expected = f"Bearer {ADMIN_TOKEN}"
    return hmac.compare_digest(auth, expected)

def _unauthorized():
    logger.warning(f"Unauthorized admin access — IP: {request.remote_addr}")
    return jsonify({"ok": False, "error": "Unauthorized"}), 401


# ── GET /admin/usuarios ──────────────────────────────────────
@admin_bp.route("/usuarios", methods=["GET"])
def listar_usuarios():
    """
    Lista todos los usuarios con su plan y uso del día.
    Query params opcionales:
      ?plan=free|pro   → filtrar por plan
      ?activo=1|0      → filtrar por estado
    """
    if not _check_token():
        return _unauthorized()

    plan_filtro   = request.args.get("plan")
    activo_filtro = request.args.get("activo")

    query = Usuario.query
    if plan_filtro in ("free", "pro"):
        query = query.filter_by(plan=plan_filtro)
    if activo_filtro is not None:
        query = query.filter_by(activo=activo_filtro == "1")

    usuarios = query.order_by(Usuario.fecha_registro.desc()).all()

    resultado = []
    for u in usuarios:
        uso = UsoDiario.query.filter_by(
            usuario_id=u.id, fecha=date.today()
        ).first()
        resultado.append({
            "id"            : u.id,
            "email"         : u.email,
            "nombre"        : u.nombre,
            "plan"          : u.plan,
            "activo"        : u.activo,
            "fecha_registro": str(u.fecha_registro),
            "ultimo_acceso" : str(u.ultimo_acceso)[:16] if u.ultimo_acceso else None,
            "consultas_hoy" : uso.consultas if uso else 0,
        })

    return jsonify({
        "ok"      : True,
        "total"   : len(resultado),
        "usuarios": resultado,
    })


# ── POST /admin/set-plan ─────────────────────────────────────
@admin_bp.route("/set-plan", methods=["POST"])
def set_plan():
    """
    Cambia el plan de un usuario.
    Body JSON: { "email": "...", "plan": "free" | "pro" }
    """
    if not _check_token():
        return _unauthorized()

    data  = request.json or {}
    email = data.get("email", "").strip().lower()
    plan  = data.get("plan", "").strip().lower()

    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    if plan not in ("free", "pro"):
        return jsonify({"ok": False, "error": "plan must be 'free' or 'pro'"}), 400

    u = Usuario.query.filter_by(email=email).first()
    if not u:
        return jsonify({"ok": False, "error": f"User '{email}' not found"}), 404

    plan_anterior = u.plan
    u.plan = plan
    db.session.commit()

    logger.warning(f"Admin changed plan: {email} {plan_anterior} → {plan}")
    return jsonify({
        "ok"           : True,
        "email"        : u.email,
        "plan_anterior": plan_anterior,
        "plan_nuevo"   : u.plan,
    })


# ── POST /admin/toggle-activo ────────────────────────────────
@admin_bp.route("/toggle-activo", methods=["POST"])
def toggle_activo():
    """
    Activa o desactiva un usuario (ban/unban).
    Body JSON: { "email": "..." }
    """
    if not _check_token():
        return _unauthorized()

    data  = request.json or {}
    email = data.get("email", "").strip().lower()

    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    u = Usuario.query.filter_by(email=email).first()
    if not u:
        return jsonify({"ok": False, "error": f"User '{email}' not found"}), 404

    u.activo = not u.activo
    db.session.commit()

    accion = "enabled" if u.activo else "disabled"
    logger.warning(f"Admin {accion} user: {email}")
    return jsonify({
        "ok"    : True,
        "email" : u.email,
        "activo": u.activo,
        "accion": accion,
    })


# ── GET /admin/stats ─────────────────────────────────────────
@admin_bp.route("/stats", methods=["GET"])
def stats_globales():
    """
    Estadísticas globales de la app — dashboard rápido.
    """
    if not _check_token():
        return _unauthorized()

    total_usuarios  = Usuario.query.count()
    usuarios_pro    = Usuario.query.filter_by(plan="pro").count()
    usuarios_free   = Usuario.query.filter_by(plan="free").count()
    usuarios_activos = Usuario.query.filter_by(activo=True).count()
    total_trades    = Journal.query.count()
    total_mensajes  = HistorialChat.query.count()

    # Usuarios que consultaron hoy
    activos_hoy = UsoDiario.query.filter_by(fecha=date.today()).count()

    # Consultas totales hoy
    from sqlalchemy import func
    consultas_hoy = db.session.query(
        func.sum(UsoDiario.consultas)
    ).filter_by(fecha=date.today()).scalar() or 0

    return jsonify({
        "ok": True,
        "usuarios": {
            "total"  : total_usuarios,
            "pro"    : usuarios_pro,
            "free"   : usuarios_free,
            "activos": usuarios_activos,
        },
        "actividad_hoy": {
            "usuarios_activos": activos_hoy,
            "consultas_totales": int(consultas_hoy),
        },
        "contenido": {
            "trades_journal": total_trades,
            "mensajes_chat" : total_mensajes,
        },
    })
