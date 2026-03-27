import os
import re
import time
import hmac
import logging
from datetime import date, datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_dance.contrib.google import make_google_blueprint, google
from dotenv import load_dotenv
from sqlalchemy import func
from models import db, Usuario, UsoDiario, HistorialChat, Journal

# ── Binance data ─────────────────────────────────────────────
try:
    from binance_data import get_contexto_mercado, get_precio_actual, get_resumen_sidebar, ACTIVOS, get_regimen_mercado, get_dxy, get_btc_dominance
except ImportError:
    def get_contexto_mercado(symbol="BTC/USDT"): return ""
    def get_precio_actual(symbol="BTC/USDT"): return None
    def get_resumen_sidebar(symbol="BTC/USDT"): return None
    def get_regimen_mercado(symbol="BTC/USDT"): return {"bloque_contexto": "", "regimen": "INDEFINIDO", "error": "Import fallido"}
    def get_dxy(): return None
    def get_btc_dominance(): return None
    ACTIVOS = {"BTC": "BTC/USDT"}

# ── Recursos compartidos (Qdrant, embedder, reranker, Claude, RAG) ──
# UNA sola instancia de todo — sin duplicados, sin doble conexión
from resources import (
    claude,
    buscar_contexto_con_regimen as buscar_contexto,
    build_system_prompt,
    necesita_datos_mercado,
    detectar_symbol,
    chunks_count as _chunks_count_inicial,
    recargar_kb,
    get_regimen_cached,
)

load_dotenv()

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# ── Logging ─────────────────────────────────────────────────
logging.basicConfig(
    filename="tradebot_errors.log",
    level=logging.ERROR,
    format="%(asctime)s — %(levelname)s — %(message)s"
)
logger = logging.getLogger(__name__)
# En desarrollo también logueamos a consola
if os.getenv("FLASK_ENV", "development") != "production":
    _console_handler = logging.StreamHandler()
    _console_handler.setLevel(logging.WARNING)
    _console_handler.setFormatter(logging.Formatter("%(asctime)s — %(levelname)s — %(message)s"))
    logger.addHandler(_console_handler)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "tradebot-secret-cambia-en-prod")

# ── Sesión con expiración ────────────────────────────────────
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_HTTPONLY"]    = True
app.config["SESSION_COOKIE_SAMESITE"]   = "Lax"

# ── Base de datos ────────────────────────────────────────────
DB_PATH = os.getenv("DATABASE_URL", "sqlite:///tradebot.db")
app.config["SQLALCHEMY_DATABASE_URI"]        = DB_PATH
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

# ── Flask-Login ──────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view = "login_page"

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Usuario, int(user_id))

# ── Google OAuth ─────────────────────────────────────────────
# OAUTHLIB — solo en desarrollo, NUNCA en producción
if os.getenv("FLASK_ENV", "development") != "production":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

google_bp = make_google_blueprint(
    client_id     = os.getenv("GOOGLE_CLIENT_ID"),
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET"),
    scope         = "openid email profile",
    redirect_to   = "google_login_finish",
)
app.register_blueprint(google_bp, url_prefix="/auth")

# Crear tablas al arrancar
with app.app_context():
    db.create_all()

# ── Blueprints ───────────────────────────────────────────────
from routes.journal_routes import journal_bp
app.register_blueprint(journal_bp)
from routes.admin_routes import admin_bp
app.register_blueprint(admin_bp)

# ============================================================
# SEGURIDAD — Rate limiting, daily limit, prompt injection
# ============================================================

_rate_data   = defaultdict(list)
IS_PROD      = os.getenv("FLASK_ENV", "development") == "production"
RATE_LIMIT   = 10 if IS_PROD else 100   # req/min
RATE_WINDOW  = 60    # segundos
# Cookie segura solo en producción (requiere HTTPS)
if IS_PROD:
    app.config["SESSION_COOKIE_SECURE"] = True

# Protección fuerza bruta en /login — max 5 intentos fallidos por IP
_login_attempts = defaultdict(list)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_BLOCK_WINDOW = 300  # 5 minutos de bloqueo

# Token secreto para endpoints admin
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
if not ADMIN_TOKEN:
    raise RuntimeError("❌ ADMIN_TOKEN no está definido en el .env")

# Patrones de prompt injection
INJECTION_PATTERNS = [
    r"ignora\s+(tus\s+)?instrucciones",
    r"ignore\s+(your\s+)?instructions",
    r"olvida\s+(todo|tus)",
    r"forget\s+(everything|your)",
    r"system\s*prompt",
    r"jailbreak",
    r"pretend\s+you",
    r"act\s+as\s+if",
    r"bypass",
    r"override",
    r"<\s*system\s*>",
    r"\[INST\]",
    r"###\s*instruction",
]

def get_client_ip() -> str:
    """Extrae IP real del cliente, compatible con proxies y Nginx"""
    for header in ("X-Forwarded-For", "X-Real-IP"):
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    return request.remote_addr or "unknown"

def check_rate_limit(ip: str) -> bool:
    """Rate limit por minuto"""
    now = time.time()
    _rate_data[ip] = [t for t in _rate_data[ip] if now - t < RATE_WINDOW]
    if len(_rate_data[ip]) >= RATE_LIMIT:
        return False
    _rate_data[ip].append(now)
    return True

def check_prompt_injection(texto: str) -> bool:
    """Retorna True si detecta intento de prompt injection"""
    lower = texto.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False

def check_brute_force(ip: str) -> bool:
    """Retorna True si la IP está bloqueada por demasiados intentos fallidos"""
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < LOGIN_BLOCK_WINDOW]
    return len(_login_attempts[ip]) >= LOGIN_MAX_ATTEMPTS

def register_failed_login(ip: str):
    """Registra un intento fallido de login"""
    _login_attempts[ip].append(time.time())

def clear_failed_logins(ip: str):
    """Limpia los intentos fallidos tras login exitoso"""
    _login_attempts[ip] = []

def is_valid_email(email: str) -> bool:
    """Validación básica de formato de email"""
    pattern = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email)) and len(email) <= 254

def check_admin_token() -> bool:
    """Verifica token secreto en header Authorization (comparación segura contra timing attacks)"""
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {ADMIN_TOKEN}"
    return hmac.compare_digest(auth, expected)

# ============================================================
# HELPERS
# ============================================================

def get_journal_stats(usuario_id: int) -> str:
    """Devuelve string con stats del journal para inyectar en el contexto del LLM"""
    try:
        total = Journal.query.filter_by(usuario_id=usuario_id).count()
        if total == 0:
            return ""
        wins   = Journal.query.filter_by(usuario_id=usuario_id, resultado="WIN").count()
        losses = Journal.query.filter_by(usuario_id=usuario_id, resultado="LOSS").count()
        wr     = round((wins / total) * 100)
        rr_avg = db.session.query(func.avg(Journal.rr_real)).filter(
            Journal.usuario_id == usuario_id,
            Journal.rr_real.isnot(None)
        ).scalar()
        rr_str = f"{float(rr_avg):.2f}" if rr_avg else "Sin datos"
        activo_top = db.session.query(
            Journal.activo, func.count(Journal.activo).label("cnt")
        ).filter_by(usuario_id=usuario_id).group_by(Journal.activo)\
         .order_by(func.count(Journal.activo).desc()).first()
        activo_str = activo_top[0] if activo_top else "Sin datos"
        sep = chr(0x2501) * 50
        return (
            f"\n{sep}\n"
            f"PERFIL DEL TRADER (datos reales del usuario)\n"
            f"{sep}\n"
            f"Trades registrados: {total} | Wins: {wins} | Losses: {losses}\n"
            f"Win Rate real:      {wr}%\n"
            f"R:R promedio:       {rr_str}\n"
            f"Activo mas operado: {activo_str}\n"
            f"Nota: Usar estos datos para personalizar el analisis.\n"
            f"{sep}"
        )
    except Exception as e:
        logger.error(f"Error obteniendo journal stats: {e}")
        return ""

# ============================================================
# LÍMITES POR PLAN
# ============================================================
PLAN_LIMITS = {
    "free": 10,   # consultas/día
    "pro" : 999,  # ilimitado
}

def check_plan_limit(usuario: Usuario) -> tuple[bool, str]:
    """Verifica si el usuario puede hacer más consultas hoy"""
    limite = PLAN_LIMITS.get(usuario.plan, 10)
    uso    = UsoDiario.get_o_crear(usuario.id)
    if uso.consultas >= limite:
        if usuario.plan == "free":
            return False, f"Límite diario del plan Free alcanzado ({limite} consultas). Upgrade a Pro para continuar."
        return False, "Límite diario alcanzado."
    uso.incrementar()
    return True, ""

# ============================================================
# RUTAS DE AUTENTICACIÓN
# ============================================================

@app.route("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/login", methods=["POST"])
def login_post():
    """Login con email y password"""
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
        logger.warning(f"Login fallido — IP: {ip} — email: {email}")
        return jsonify({"ok": False, "error": "Credenciales incorrectas"}), 401

    if not usuario.activo:
        return jsonify({"ok": False, "error": "Cuenta desactivada"}), 403

    clear_failed_logins(ip)
    login_user(usuario, remember=True)
    session.permanent = True
    usuario.actualizar_acceso()
    return jsonify({"ok": True, "redirect": "/"})

@app.route("/register", methods=["POST"])
def register():
    """Registro con email y password"""
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

    login_user(usuario, remember=True)
    session.permanent = True
    return jsonify({"ok": True, "redirect": "/"})

@app.route("/google_login_finish")
def google_login_finish():
    """Flask-dance llama aquí después de autenticar con Google"""
    if not google.authorized:
        logger.error("Google OAuth: no autorizado al llegar a google_login_finish")
        session["oauth_error"] = "No se pudo completar el login con Google. Intenta de nuevo."
        return redirect(url_for("login_page"))
    try:
        resp = google.get("/oauth2/v2/userinfo")
        if not resp.ok:
            logger.error(f"Google OAuth: userinfo falló — status {resp.status_code}")
            session["oauth_error"] = "Error obteniendo datos de Google. Intenta de nuevo."
            return redirect(url_for("login_page"))
        info  = resp.json()
        email = info.get("email", "").lower()

        if not email:
            session["oauth_error"] = "Google no devolvió un email válido."
            return redirect(url_for("login_page"))

        usuario = Usuario.query.filter_by(email=email).first()
        if not usuario:
            usuario = Usuario(
                email      = email,
                nombre     = info.get("name"),
                google_id  = info.get("id"),
                avatar_url = info.get("picture"),
            )
            db.session.add(usuario)
            db.session.commit()
        else:
            usuario.google_id  = info.get("id")
            usuario.avatar_url = info.get("picture")
            db.session.commit()

        login_user(usuario, remember=True)
        usuario.actualizar_acceso()
        return redirect(url_for("index"))

    except Exception as e:
        logger.error(f"Error en Google OAuth: {e}")
        session["oauth_error"] = "Error inesperado en login con Google. Intenta de nuevo."
        return redirect(url_for("login_page"))

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login_page"))

@app.route("/me")
@login_required
def me():
    return jsonify({
        "logueado": True,
        "usuario" : current_user.to_dict()
    })

# ============================================================
# RUTAS PRINCIPALES
# ============================================================

@app.route("/")
@login_required
def index():
    return render_template("index.html", chunks=_chunks_count_inicial)

@app.route("/info")
def info():
    return jsonify({"chunks": _chunks_count_inicial})

@app.route("/reload-kb", methods=["POST"])
def reload_kb():
    """Recarga la KB — requiere token admin en Authorization header"""
    if not check_admin_token():
        logger.error(f"Intento no autorizado a /reload-kb desde {get_client_ip()}")
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    try:
        nuevo_count = recargar_kb()
        return jsonify({"ok": True, "chunks": nuevo_count})
    except Exception as e:
        logger.error(f"Error en reload-kb: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "")

@app.after_request
def apply_security_headers(response):
    """Headers de seguridad — aplica a todas las respuestas"""
    response.headers["X-Frame-Options"]        = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    if IS_PROD:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https://lh3.googleusercontent.com https://lh4.googleusercontent.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    return response

@app.after_request
def apply_cors(response):
    """CORS centralizado — aplica a todas las rutas"""
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

@app.route("/mercado")
def mercado():
    """Datos compactos de todos los activos — paralelo con ThreadPoolExecutor"""
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

@app.route("/precio")
def precio():
    try:
        data = get_precio_actual()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/macro")
def macro():
    """DXY + BTC Dominance en tiempo real"""
    try:
        dxy  = get_dxy()
        btcd = get_btc_dominance()
        return jsonify({"dxy": dxy, "btcd": btcd})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/chat", methods=["POST"])
@login_required
def chat():
    ip  = get_client_ip()
    uid = current_user.email

    # ── Capa 1: Rate limit por minuto ──
    if not check_rate_limit(uid):
        logger.error(f"Rate limit excedido — usuario: {uid}")
        return jsonify({"error": "Demasiadas solicitudes. Espera un momento."}), 429

    # ── Capa 2: Límite por plan (DB) ──
    permitido, msg = check_plan_limit(current_user)
    if not permitido:
        return jsonify({"error": msg}), 429

    data     = request.json
    pregunta = data.get("pregunta", "").strip()

    if not pregunta:
        return jsonify({"error": "Pregunta vacía"}), 400

    # ── Capa 3: Límite de longitud ──
    if len(pregunta) > 1000:
        return jsonify({"error": "Pregunta demasiado larga. Máximo 1000 caracteres."}), 400

    # ── Capa 4: Prompt injection ──
    if check_prompt_injection(pregunta):
        logger.error(f"Prompt injection detectado — IP: {ip} — texto: {pregunta[:100]}")
        return jsonify({"error": "Consulta no permitida."}), 400

    # ── Pre-calentar caché del régimen — garantiza query expansion en RAG ──
    get_regimen_cached()

    # ── Historial desde DB — memoria persistente entre sesiones ──
    historial = HistorialChat.cargar(current_user.id)

    try:
        symbol = detectar_symbol(pregunta) if necesita_datos_mercado(pregunta) else None

        # ── Paralelizar RAG + datos mercado + system prompt ──────
        # Los 3 son independientes entre sí — el caché del régimen
        # ya está caliente gracias al pre-warm de arriba
        with ThreadPoolExecutor(max_workers=3) as executor:
            fut_contexto = executor.submit(buscar_contexto, pregunta)
            fut_sistema  = executor.submit(build_system_prompt)
            fut_mercado  = executor.submit(get_contexto_mercado, symbol) if symbol else None

        contexto      = fut_contexto.result()
        system_prompt = fut_sistema.result()

        datos_mercado = ""
        if fut_mercado:
            try:
                datos_mercado = fut_mercado.result()
            except Exception as e:
                logger.error(f"Error obteniendo datos de mercado: {e}")

        # ── Journal stats — DB, fuera del executor (necesita app context) ──
        journal_stats = get_journal_stats(current_user.id)

        mensaje_enriquecido = f"Conocimiento especializado:\n{contexto}\n\n{datos_mercado}\n\n{journal_stats}\n\nPregunta: {pregunta}"
        mensajes_api = historial + [{"role": "user", "content": mensaje_enriquecido}]

        resp = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2500,
            system=system_prompt,
            messages=mensajes_api
        )
        texto = resp.content[0].text

        # ── Guardar en DB ──
        HistorialChat.guardar(current_user.id, "user",      pregunta)
        HistorialChat.guardar(current_user.id, "assistant", texto)

    except Exception as e:
        logger.error(f"Error en /chat — IP: {ip} — error: {e}")
        return jsonify({"error": "Error interno. Intenta de nuevo."}), 500

    return jsonify({"respuesta": texto})


@app.route("/chat/limpiar", methods=["POST"])
@login_required
def chat_limpiar():
    """Borra el historial del usuario — botón 'Nueva conversación'"""
    HistorialChat.limpiar(current_user.id)
    return jsonify({"ok": True})


# ============================================================
# PERFIL DE USUARIO
# ============================================================

@app.route("/perfil")
@login_required
def perfil_page():
    return render_template("perfil.html")

@app.route("/perfil/datos")
@login_required
def perfil_datos():
    """Devuelve todos los datos necesarios para la página de perfil"""
    uso = UsoDiario.query.filter_by(
        usuario_id=current_user.id, fecha=date.today()
    ).first()
    consultas_hoy = uso.consultas if uso else 0
    limite        = 10 if current_user.plan == "free" else 999

    total_trades = Journal.query.filter_by(usuario_id=current_user.id).count()
    wins  = Journal.query.filter_by(usuario_id=current_user.id, resultado="WIN").count()
    losses= Journal.query.filter_by(usuario_id=current_user.id, resultado="LOSS").count()

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
            "ts": str(t.fecha_trade) if t.fecha_trade else ""
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

@app.route("/perfil/cambiar-password", methods=["POST"])
@login_required
def cambiar_password():
    """Cambia la contraseña del usuario — solo para cuentas email"""
    if current_user.google_id:
        return jsonify({"ok": False, "error": "Los usuarios de Google no pueden cambiar contraseña aquí."}), 400

    data   = request.json or {}
    actual = data.get("actual", "")
    nueva  = data.get("nueva", "")

    if not actual or not nueva:
        return jsonify({"ok": False, "error": "Campos incompletos"}), 400
    if len(nueva) < 8:
        return jsonify({"ok": False, "error": "La contraseña debe tener al menos 8 caracteres"}), 400
    if not current_user.check_password(actual):
        return jsonify({"ok": False, "error": "La contraseña actual es incorrecta"}), 401

    current_user.set_password(nueva)
    db.session.commit()
    logger.warning(f"Contraseña cambiada — usuario: {current_user.email}")
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("🤖 TradeBot corriendo en http://localhost:5000")
    app.run(debug=True, port=5000, use_reloader=False)
