import os
import time
import logging
import threading
from datetime import timedelta
from flask import Flask, request, jsonify, session, redirect, url_for
from flask_login import LoginManager, current_user
from flask_dance.contrib.google import make_google_blueprint, google
from models import db, Usuario, Notificacion
from email_service import enviar_bienvenida
import config  # load_dotenv() se ejecuta aquí, antes que cualquier os.getenv
from config import (
    IS_PROD,
    FLASK_SECRET_KEY,
    DATABASE_URL,
    ALLOWED_ORIGIN,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
)

# ── Binance data ─────────────────────────────────────────────
try:
    from binance_data import get_precio_actual, ACTIVOS
except ImportError:
    def get_precio_actual(symbol="BTC/USDT"): return None
    ACTIVOS = {"BTC": "BTC/USDT"}

# ── Scanner de confluencias ───────────────────────────────────
try:
    from scanner import evaluar_confluencias
except ImportError:
    def evaluar_confluencias(symbol):
        return {"setup_ok": False, "score": 0, "confluencias": [], "error": "Import fallido"}

# ── Recursos compartidos (Qdrant, embedder, reranker, Claude, RAG) ──
from resources import (
    claude,
    CLAUDE_MODEL,
    buscar_contexto_con_regimen as buscar_contexto,
    build_system_prompt,
    necesita_datos_mercado,
    detectar_symbol,
    chunks_count as _chunks_count_inicial,
    recargar_kb,
    get_regimen_cached,
)

# ── Logging ─────────────────────────────────────────────────
from logging.handlers import RotatingFileHandler as _RotatingFileHandler

_log_fmt      = logging.Formatter("%(asctime)s — %(levelname)s — %(message)s")
_file_handler = _RotatingFileHandler(
    "tradebot_errors.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setLevel(logging.ERROR)
_file_handler.setFormatter(_log_fmt)

logging.basicConfig(level=logging.ERROR, handlers=[_file_handler])
logger = logging.getLogger(__name__)

if not IS_PROD:
    _console_handler = logging.StreamHandler()
    _console_handler.setLevel(logging.WARNING)
    _console_handler.setFormatter(_log_fmt)
    logger.addHandler(_console_handler)

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

# ── Límite de upload (CSV, imágenes) — 5 MB ─────────────────
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# ── Sesión con expiración ────────────────────────────────────
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_HTTPONLY"]   = True
app.config["SESSION_COOKIE_SAMESITE"]  = "Lax"
app.config["SESSION_COOKIE_SECURE"]    = IS_PROD   # solo HTTPS en producción

# ── Base de datos ────────────────────────────────────────────
app.config["SQLALCHEMY_DATABASE_URI"]        = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
if DATABASE_URL.startswith("sqlite"):
    # SQLite — un solo hilo, timeout generoso
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 20},
        "pool_pre_ping": True,
    }
else:
    # PostgreSQL — connection pool para gunicorn multi-worker
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_size":    5,
        "pool_recycle": 300,   # recicla conexiones cada 5 min (evita idle timeouts)
        "pool_pre_ping": True, # detecta conexiones muertas antes de usarlas
        "max_overflow": 2,
    }
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "auth.login_page"

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Usuario, int(user_id))

# ── Rutas API → 401 JSON en vez de redirect 302 ──────────────
_API_PREFIXES = ("/journal/", "/api/", "/bitunix/", "/admin/",
                 "/scanner/", "/chat/", "/profile/")

@login_manager.unauthorized_handler
def unauthorized():
    is_api = (
        request.is_json
        or request.path.startswith(_API_PREFIXES)
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )
    if is_api:
        return jsonify({"ok": False, "error": "Autenticación requerida"}), 401
    return redirect(url_for("auth.login_page"))

# ── Google OAuth ─────────────────────────────────────────────
if not IS_PROD:
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

google_bp = make_google_blueprint(
    client_id     = GOOGLE_CLIENT_ID,
    client_secret = GOOGLE_CLIENT_SECRET,
    scope         = "openid email profile",
    redirect_to   = "google_login_finish",
)
app.register_blueprint(google_bp, url_prefix="/auth")

# Tablas — se crean en el segundo app_context() al final del archivo

# ── Blueprints ───────────────────────────────────────────────
from routes.journal_routes  import journal_bp
from routes.admin_routes    import admin_bp
from routes.bitunix_routes  import bitunix_bp
from routes.auth_routes     import auth_bp
from routes.main_routes     import main_bp
from routes.scanner_routes  import scanner_bp
from routes.chat_routes     import chat_bp
from routes.profile_routes  import profile_bp

app.register_blueprint(journal_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(bitunix_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(main_bp)
app.register_blueprint(scanner_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(profile_bp)

# ── Headers de seguridad con Flask-Talisman ──────────────────
from flask_talisman import Talisman

_CSP = {
    "default-src": ["'self'"],
    "script-src" : ["'self'", "'unsafe-inline'"],
    "style-src"  : ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
    "font-src"   : ["'self'", "https://fonts.gstatic.com"],
    "img-src"    : ["'self'", "data:", "https://lh3.googleusercontent.com",
                    "https://lh4.googleusercontent.com", "https:"],
    "connect-src": ["'self'"],
    "frame-ancestors": ["'none'"],
    "object-src" : ["'none'"],
    "base-uri"   : ["'self'"],
}

Talisman(
    app,
    force_https=IS_PROD,
    strict_transport_security=IS_PROD,
    session_cookie_secure=IS_PROD,
    content_security_policy=_CSP,
    content_security_policy_nonce_in=[],
    x_content_type_options=True,
    x_xss_protection=True,
    frame_options="DENY",
    referrer_policy="strict-origin-when-cross-origin",
)

# ============================================================
# GOOGLE LOGIN FINISH — se queda aquí para no cambiar redirect_to
# ============================================================

@app.route("/google_login_finish")
def google_login_finish():
    """Flask-dance llama aquí después de autenticar con Google."""
    if not google.authorized:
        logger.error("Google OAuth: no autorizado al llegar a google_login_finish")
        session["oauth_error"] = "No se pudo completar el login con Google. Intenta de nuevo."
        return redirect(url_for("auth.login_page"))
    try:
        resp = google.get("/oauth2/v2/userinfo")
        if not resp.ok:
            logger.error(f"Google OAuth: userinfo falló — status {resp.status_code}")
            session["oauth_error"] = "Error obteniendo datos de Google. Intenta de nuevo."
            return redirect(url_for("auth.login_page"))
        info  = resp.json()
        email = info.get("email", "").lower()

        if not email:
            session["oauth_error"] = "Google no devolvió un email válido."
            return redirect(url_for("auth.login_page"))

        usuario  = Usuario.query.filter_by(email=email).first()
        if not usuario:
            usuario = Usuario(
                email      = email,
                nombre     = info.get("name"),
                google_id  = info.get("id"),
                avatar_url = info.get("picture"),
            )
            db.session.add(usuario)
            db.session.commit()
            threading.Thread(target=enviar_bienvenida, args=(email, info.get("name")), daemon=True).start()
        else:
            usuario.google_id  = info.get("id")
            usuario.avatar_url = info.get("picture")
            db.session.commit()

        from flask_login import login_user
        login_user(usuario, remember=True)
        usuario.actualizar_acceso()
        return redirect(url_for("main.index"))

    except Exception as e:
        logger.error(f"Error en Google OAuth: {e}")
        session["oauth_error"] = "Error inesperado en login con Google. Intenta de nuevo."
        return redirect(url_for("auth.login_page"))


# ============================================================
# AFTER-REQUEST HOOKS
# ============================================================

@app.after_request
def apply_security_headers(response):
    """
    Headers de seguridad — aplica a todas las respuestas.
    CSP usa unsafe-inline porque los templates tienen <script> inline (17 bloques).
    Para eliminar unsafe-inline en el futuro: mover JS a archivos externos en /static.
    """
    # Clickjacking — SAMEORIGIN permite embeber en tu propio dominio si hace falta
    response.headers["X-Frame-Options"]        = "SAMEORIGIN"
    # MIME sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Referrer limitado — no filtra URL completa a terceros
    response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    # Deshabilita APIs del browser que no usa la app
    response.headers["Permissions-Policy"]     = (
        "camera=(), microphone=(), geolocation=(), "
        "payment=(), usb=(), bluetooth=()"
    )
    # CSP — restringe fuentes de contenido, permite inline por arquitectura actual
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' fonts.googleapis.com; "
        "font-src 'self' fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https: wss:; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )
    if IS_PROD:
        # HSTS — fuerza HTTPS por 1 año (Railway provee TLS automático)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

@app.after_request
def apply_cors(response):
    """CORS centralizado — aplica a todas las rutas."""
    origin = request.headers.get("Origin", "")
    if ALLOWED_ORIGIN:
        if origin == ALLOWED_ORIGIN:
            response.headers["Access-Control-Allow-Origin"] = origin
    else:
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ============================================================
# SCHEDULERS
# ============================================================

def _scheduler_edge_stats():
    while True:
        time.sleep(86400)
        try:
            from stats_engine import calcular_todas_las_stats
            calcular_todas_las_stats()
            logger.info("Edge stats actualizadas")
        except Exception as e:
            logger.error(f"Error en scheduler edge stats: {e}")

_t_edge = threading.Thread(target=_scheduler_edge_stats, daemon=True)
_t_edge.start()

try:
    from notificaciones import run_scheduler as _run_notif_scheduler
    _t_notif = threading.Thread(target=_run_notif_scheduler, args=(app,), daemon=True)
    _t_notif.start()
    logger.info("Scheduler de notificaciones iniciado")
except Exception as _e:
    logger.warning(f"Scheduler notificaciones no iniciado: {_e}")

# ── WebSocket trigger monitor — reacción instantánea a precio ──
try:
    from ws_monitor import WSTriggerMonitor
    _ws_monitor = WSTriggerMonitor(app)
    _ws_monitor.start()
    logger.info("WSTriggerMonitor iniciado")
except Exception as _e:
    logger.warning(f"WSTriggerMonitor no iniciado: {_e}")

# ── HMM Regime Detector — entrenamiento en background al arrancar ──
try:
    from hmm_regime import inicializar_hmm_background
    inicializar_hmm_background()
except Exception as _e:
    logger.warning(f"HMM no iniciado: {_e}")


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/") or request.is_json:
        return jsonify({"error": "Not found"}), 404
    from flask import Response
    return Response("Not found", status=404, mimetype="text/plain")

@app.errorhandler(500)
def server_error(e):
    logger.error(f"Error 500 — {request.path} — {e}")
    if request.path.startswith("/api/") or request.is_json:
        return jsonify({"error": "Internal server error"}), 500
    from flask import Response
    return Response("Internal server error", status=500, mimetype="text/plain")

@app.errorhandler(429)
def too_many_requests(e):
    return jsonify({"error": "Too many requests. Wait a moment."}), 429

@app.errorhandler(413)
def payload_too_large(e):
    return jsonify({"error": "Archivo demasiado grande. Máximo 5 MB."}), 413


with app.app_context():
    db.create_all()   # crea todas las tablas del ORM si no existen
    # Migraciones manuales solo en SQLite (columnas añadidas en versiones anteriores).
    # En PostgreSQL db.create_all() ya crea el schema completo desde los modelos.
    if DATABASE_URL.startswith("sqlite"):
        from models import run_journal_migrations, run_trigger_migrations
        run_journal_migrations(db.engine)
        run_trigger_migrations(db.engine)

if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)
