import logging
import bcrypt
from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

logger = logging.getLogger(__name__)

db = SQLAlchemy()

class Usuario(UserMixin, db.Model):
    __tablename__ = "usuarios"

    id             = db.Column(db.Integer, primary_key=True)
    email          = db.Column(db.Text, unique=True, nullable=False)
    nombre         = db.Column(db.Text, nullable=True)
    password_hash  = db.Column(db.Text, nullable=True)   # NULL si usa Google
    google_id      = db.Column(db.Text, nullable=True)   # NULL si usa email
    avatar_url     = db.Column(db.Text, nullable=True)
    plan           = db.Column(db.Text, default="free")  # free | pro
    fecha_registro = db.Column(db.Date, default=date.today)
    ultimo_acceso  = db.Column(db.DateTime, default=datetime.utcnow)
    activo         = db.Column(db.Boolean, default=True)

    # ── API keys de exchanges ─────────────────────────────────
    bitunix_api_key    = db.Column(db.Text, nullable=True)
    bitunix_secret_key = db.Column(db.Text, nullable=True)

    # ── 2FA TOTP ──────────────────────────────────────────────
    totp_secret     = db.Column(db.Text, nullable=True)       # Base32 secret (None = 2FA off)
    totp_habilitado = db.Column(db.Boolean, default=False)

    # ── BYOK — Bring Your Own Key (solo Pro) ─────────────────
    anthropic_key_enc = db.Column(db.Text, nullable=True)     # API key cifrada con Fernet

    # Relación con uso diario
    uso = db.relationship("UsoDiario", backref="usuario", lazy=True)

    def set_password(self, password: str):
        """Hashea y guarda la contraseña con bcrypt"""
        self.password_hash = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt()
        ).decode("utf-8")

    def check_password(self, password: str) -> bool:
        """Verifica la contraseña contra el hash guardado"""
        if not self.password_hash:
            return False
        return bcrypt.checkpw(
            password.encode("utf-8"),
            self.password_hash.encode("utf-8")
        )

    def es_pro(self) -> bool:
        return self.plan == "pro"

    def actualizar_acceso(self):
        self.ultimo_acceso = datetime.utcnow()
        db.session.commit()

    def to_dict(self) -> dict:
        return {
            "id"                  : self.id,
            "email"               : self.email,
            "nombre"              : self.nombre,
            "avatar_url"          : self.avatar_url,
            "plan"                : self.plan,
            "fecha_registro"      : str(self.fecha_registro),
            "bitunix_configurado" : bool(self.bitunix_api_key and self.bitunix_secret_key),
            "totp_habilitado"     : bool(self.totp_habilitado),
            "byok_configurado"    : bool(self.anthropic_key_enc),
        }


class UsoDiario(db.Model):
    __tablename__ = "uso_diario"

    id          = db.Column(db.Integer, primary_key=True)
    usuario_id  = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)
    fecha       = db.Column(db.Date, default=date.today)
    consultas   = db.Column(db.Integer, default=0)

    __table_args__ = (
        db.UniqueConstraint("usuario_id", "fecha", name="uq_usuario_fecha"),
    )

    @staticmethod
    def get_o_crear(usuario_id: int) -> "UsoDiario":
        """Obtiene o crea el registro de uso del día actual"""
        hoy = date.today()
        registro = UsoDiario.query.filter_by(
            usuario_id=usuario_id,
            fecha=hoy
        ).first()
        if not registro:
            registro = UsoDiario(usuario_id=usuario_id, fecha=hoy)
            db.session.add(registro)
            db.session.commit()
        return registro

    def incrementar(self) -> int:
        """Incrementa el contador y retorna el nuevo valor"""
        self.consultas += 1
        db.session.commit()
        return self.consultas


class Suscripcion(db.Model):
    __tablename__ = "suscripciones"

    id           = db.Column(db.Integer, primary_key=True)
    usuario_id   = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)
    stripe_id    = db.Column(db.Text, nullable=True)   # para Stripe V2.0
    plan         = db.Column(db.Text, nullable=False)
    fecha_inicio = db.Column(db.Date, default=date.today)
    fecha_vence  = db.Column(db.Date, nullable=True)
    activa       = db.Column(db.Boolean, default=True)


class HistorialChat(db.Model):
    """
    Memoria persistente del chat por usuario.
    Guarda los últimos MAX_MENSAJES mensajes para que el bot
    recuerde el contexto entre sesiones.
    """
    __tablename__ = "historial_chat"

    MAX_MENSAJES = 40  # máximo de mensajes guardados por usuario (20 turnos)

    id          = db.Column(db.Integer, primary_key=True)
    usuario_id  = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)
    rol         = db.Column(db.Text, nullable=False)   # "user" | "assistant"
    contenido   = db.Column(db.Text, nullable=False)
    tf          = db.Column(db.String(4), nullable=True, default="4h")  # "15m"|"1h"|"4h"|"1d"
    creado_en   = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index("ix_historial_usuario_fecha", "usuario_id", "creado_en"),
    )

    @staticmethod
    def cargar(usuario_id: int, tf: str = "4h") -> list:
        """
        Carga los últimos MAX_MENSAJES mensajes del usuario.
        - Mismo TF → incluye tal cual
        - TF distinto → incluye con label para que el modelo no mezcle señales
        """
        registros = (
            HistorialChat.query
            .filter_by(usuario_id=usuario_id)
            .order_by(HistorialChat.creado_en.asc())
            .limit(HistorialChat.MAX_MENSAJES)
            .all()
        )
        resultado = []
        for r in registros:
            tf_msg = r.tf or "4h"
            if tf_msg == tf:
                resultado.append({"role": r.rol, "content": r.contenido})
            else:
                contenido_labeled = f"[análisis {tf_msg.upper()} anterior] {r.contenido}"
                resultado.append({"role": r.rol, "content": contenido_labeled})
        return resultado

    @staticmethod
    def guardar(usuario_id: int, rol: str, contenido: str, tf: str = "4h"):
        """Guarda un mensaje y limpia los más viejos si supera el límite"""
        nuevo = HistorialChat(
            usuario_id=usuario_id,
            rol=rol,
            contenido=contenido,
            tf=tf,
        )
        db.session.add(nuevo)
        db.session.flush()  # obtener el id sin commit

        # Limpiar mensajes viejos — mantener solo los últimos MAX_MENSAJES
        total = HistorialChat.query.filter_by(usuario_id=usuario_id).count()
        if total > HistorialChat.MAX_MENSAJES:
            exceso = total - HistorialChat.MAX_MENSAJES
            ids_viejos = (
                db.session.query(HistorialChat.id)
                .filter_by(usuario_id=usuario_id)
                .order_by(HistorialChat.creado_en.asc())
                .limit(exceso)
                .subquery()
            )
            HistorialChat.query.filter(
                HistorialChat.id.in_(ids_viejos)
            ).delete(synchronize_session=False)

        db.session.commit()

    @staticmethod
    def limpiar(usuario_id: int):
        """Borra todo el historial del usuario — útil para el botón 'Nueva conversación'"""
        HistorialChat.query.filter_by(usuario_id=usuario_id).delete()
        db.session.commit()

class Journal(db.Model):
    """
    Registro de trades del usuario.
    Soporta trades ABIERTOS (en vigencia) y CERRADOS.
    Los trades abiertos reciben confianza_bot al entrar.
    El ia_feedback se genera solo al cerrar.
    """
    __tablename__ = "journal"

    id          = db.Column(db.Integer, primary_key=True)
    usuario_id  = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)

    # ── Datos del trade ──────────────────────────────────────
    activo       = db.Column(db.Text, nullable=False)              # BTCUSDT
    direccion    = db.Column(db.Text, nullable=False)              # LONG | SHORT
    entrada      = db.Column(db.Float, nullable=False)             # precio entrada
    sl           = db.Column(db.Float, nullable=True)              # stop loss
    tp           = db.Column(db.Float, nullable=True)              # take profit
    resultado    = db.Column(db.Text, nullable=True)               # WIN | LOSS | BE
    pnl          = db.Column(db.Float, nullable=True)              # % ganancia/pérdida
    rr_planeado  = db.Column(db.Float, nullable=True)              # R:R antes de entrar
    rr_real      = db.Column(db.Float, nullable=True)              # R:R real obtenido
    timeframe    = db.Column(db.Text, nullable=True)               # 4H | 1D | 1H
    notas        = db.Column(db.Text, nullable=True)               # observaciones
    fecha_trade  = db.Column(db.Date, default=date.today)
    creado_en    = db.Column(db.DateTime, default=datetime.utcnow)

    # ── Ciclo de vida del trade ───────────────────────────────
    estado           = db.Column(db.Text, default="CERRADO")   # ABIERTO | CERRADO
    precio_cierre    = db.Column(db.Float, nullable=True)
    pnl_real         = db.Column(db.Float, nullable=True)      # USDT (no %)
    duracion_minutos = db.Column(db.Integer, nullable=True)
    fecha_cierre     = db.Column(db.DateTime, nullable=True)

    # ── Origen ───────────────────────────────────────────────
    fuente            = db.Column(db.Text, default="MANUAL")   # MANUAL | BITUNIX | BINANCE | CSV
    exchange_trade_id = db.Column(db.Text, nullable=True)      # ID único del exchange

    # ── Riesgo ───────────────────────────────────────────────
    apalancamiento = db.Column(db.Float, default=1.0)           # 1.0 = spot | 10.0 = 10x futures
    capital_cuenta = db.Column(db.Float, nullable=True)         # balance total USDT al momento del trade
    margen_usado   = db.Column(db.Float, nullable=True)         # USDT comprometidos en este trade
    tipo_margen    = db.Column(db.Text, default="AISLADO")      # AISLADO | CRUZADO

    # ── Estilo de trade ──────────────────────────────────────
    tipo_trade     = db.Column(db.Text, default="SWING")        # SCALP | SWING | POSITION

    # ── Análisis IA ──────────────────────────────────────────
    confianza_bot = db.Column(db.Integer, nullable=True)        # 0-100, solo al abrir (ABIERTO)
    ia_feedback   = db.Column(db.Text, nullable=True)           # feedback al cerrar
    ia_analizado  = db.Column(db.Boolean, default=False)

    __table_args__ = (
        db.Index("ix_journal_usuario_fecha", "usuario_id", "fecha_trade"),
    )

    def to_dict(self) -> dict:
        return {
            "id"               : self.id,
            "activo"           : self.activo,
            "direccion"        : self.direccion,
            "entrada"          : self.entrada,
            "sl"               : self.sl,
            "tp"               : self.tp,
            "resultado"        : self.resultado,
            "pnl"              : self.pnl,
            "pnl_real"         : self.pnl_real,
            "rr_planeado"      : self.rr_planeado,
            "rr_real"          : self.rr_real,
            "timeframe"        : self.timeframe,
            "notas"            : self.notas,
            "fecha_trade"      : str(self.fecha_trade),
            "fecha_cierre"     : self.fecha_cierre.strftime("%Y-%m-%d %H:%M") if self.fecha_cierre else None,
            "creado_en"        : self.creado_en.strftime("%Y-%m-%d %H:%M"),
            "estado"           : self.estado or "CERRADO",
            "precio_cierre"    : self.precio_cierre,
            "duracion_minutos" : self.duracion_minutos,
            "fuente"           : self.fuente or "MANUAL",
            "apalancamiento"   : self.apalancamiento or 1.0,
            "capital_cuenta"   : self.capital_cuenta,
            "margen_usado"     : self.margen_usado,
            "tipo_margen"      : self.tipo_margen or "AISLADO",
            "tipo_trade"       : self.tipo_trade or "SWING",
            "confianza_bot"    : self.confianza_bot,
            "ia_feedback"      : self.ia_feedback,
        }

    # ── Métodos de consulta ──────────────────────────────────

    @staticmethod
    def listar(usuario_id: int, limite: int = 50) -> list:
        """Últimos N trades del usuario, más recientes primero"""
        registros = (
            Journal.query
            .filter_by(usuario_id=usuario_id)
            .order_by(Journal.fecha_trade.desc(), Journal.creado_en.desc())
            .limit(limite)
            .all()
        )
        return [r.to_dict() for r in registros]

    @staticmethod
    def stats(usuario_id: int) -> dict:
        """
        Calcula estadísticas agregadas del usuario via SQL — sin cargar objetos en memoria.
        3 queries: (1) agregados globales, (2) win rate por activo, (3) racha actual.
        Escala a cualquier cantidad de trades sin impacto en RAM.
        """
        from sqlalchemy import func, case

        # ── Query 1: agregados globales — una sola pasada por la tabla ────────
        agg = db.session.query(
            func.count(Journal.id).label("total"),
            func.sum(case((Journal.resultado == "WIN",  1), else_=0)).label("wins"),
            func.sum(case((Journal.resultado == "LOSS", 1), else_=0)).label("losses"),
            func.sum(case((Journal.resultado == "BE",   1), else_=0)).label("be"),
            func.avg(Journal.rr_real).label("rr_prom"),
            func.sum(Journal.pnl).label("pnl_total"),
        ).filter(Journal.usuario_id == usuario_id).one()

        total = int(agg.total or 0)
        if total == 0:
            return {"total": 0}

        wins      = int(agg.wins      or 0)
        losses    = int(agg.losses    or 0)
        be        = int(agg.be        or 0)
        rr_prom   = round(float(agg.rr_prom),   2) if agg.rr_prom   is not None else None
        pnl_total = round(float(agg.pnl_total), 2) if agg.pnl_total is not None else None

        # ── Query 2: win rate por activo — GROUP BY, sin iterar en Python ─────
        activos_raw = db.session.query(
            Journal.activo,
            func.count(Journal.id).label("total"),
            func.sum(case((Journal.resultado == "WIN", 1), else_=0)).label("wins"),
        ).filter(
            Journal.usuario_id == usuario_id
        ).group_by(Journal.activo).all()

        activos = {}
        for row in activos_raw:
            t = int(row.total or 0)
            w = int(row.wins  or 0)
            activos[row.activo] = {
                "total":    t,
                "wins":     w,
                "win_rate": round(w / t * 100, 1) if t > 0 else 0,
            }

        # ── Query 3: racha actual — solo columna resultado, ordenada DESC ─────
        # Carga máximo 50 filas (solo el campo resultado, no objetos completos)
        ultimos = (
            db.session.query(Journal.resultado)
            .filter(Journal.usuario_id == usuario_id, Journal.resultado.isnot(None))
            .order_by(Journal.creado_en.desc())
            .limit(50)
            .all()
        )

        racha      = 0
        ultimo_res = None
        if ultimos:
            ultimo_res = ultimos[0][0]
            for (res,) in ultimos:
                if res == ultimo_res:
                    racha += 1
                else:
                    break

        return {
            "total"       : total,
            "wins"        : wins,
            "losses"      : losses,
            "be"          : be,
            "win_rate"    : round(wins / total * 100, 1) if total > 0 else 0,
            "rr_promedio" : rr_prom,
            "pnl_total"   : pnl_total,
            "por_activo"  : activos,
            "racha_actual": {"resultado": ultimo_res, "count": racha},
        }


class Notificacion(db.Model):
    """
    Notificaciones in-app para el usuario.
    El scheduler las crea; el frontend las lee vía AJAX.
    """
    __tablename__ = "notificaciones"

    id         = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)
    tipo       = db.Column(db.Text, nullable=False)   # liquidacion | sl | scanner4 | scanner3 | regimen | funding | briefing
    nivel      = db.Column(db.Text, default="INFO")   # ROJO | AMARILLO | INFO
    titulo     = db.Column(db.Text, nullable=False)
    mensaje    = db.Column(db.Text, nullable=False)
    leida      = db.Column(db.Boolean, default=False)
    creada_en  = db.Column(db.DateTime, default=datetime.utcnow)
    trade_id   = db.Column(db.Integer, db.ForeignKey("journal.id"), nullable=True)  # trade relacionado

    __table_args__ = (
        db.Index("ix_notif_usuario_leida", "usuario_id", "leida"),
    )

    def to_dict(self) -> dict:
        return {
            "id"       : self.id,
            "tipo"     : self.tipo,
            "nivel"    : self.nivel,
            "titulo"   : self.titulo,
            "mensaje"  : self.mensaje,
            "leida"    : self.leida,
            "creada_en": self.creada_en.strftime("%Y-%m-%d %H:%M"),
            "trade_id" : self.trade_id,
        }


class ActiveTrigger(db.Model):
    """
    Triggers de mercado emitidos por el bot — state machine.
    El bot emite "Trigger: condición $precio" en el análisis.
    El scheduler verifica si el precio cruzó el nivel y notifica al usuario.
    """
    __tablename__ = "active_triggers"

    id                 = db.Column(db.Integer, primary_key=True)
    usuario_id         = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)
    condicion_texto    = db.Column(db.Text, nullable=False)   # texto completo del trigger
    precio_nivel       = db.Column(db.Float, nullable=True)   # precio parseado ($68,321 → 68321.0)
    direccion          = db.Column(db.Text, nullable=True)    # "LONG" | "SHORT" | None
    symbol             = db.Column(db.Text, default="BTC/USDT")
    activo             = db.Column(db.Boolean, default=True)
    disparado          = db.Column(db.Boolean, default=False)
    notificado_en_chat = db.Column(db.Boolean, default=False)
    creado_en          = db.Column(db.DateTime, default=datetime.utcnow)
    disparado_en       = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index("ix_trigger_usuario_activo", "usuario_id", "activo"),
    )


def run_journal_migrations(engine):
    """
    Añade las nuevas columnas al journal si no existen.
    SQLite no soporta IF NOT EXISTS en ALTER TABLE —
    capturamos la excepción si la columna ya existe.
    """
    nuevas_columnas = [
        "ALTER TABLE journal ADD COLUMN estado TEXT DEFAULT 'CERRADO'",
        "ALTER TABLE journal ADD COLUMN precio_cierre FLOAT",
        "ALTER TABLE journal ADD COLUMN pnl_real FLOAT",
        "ALTER TABLE journal ADD COLUMN duracion_minutos INTEGER",
        "ALTER TABLE journal ADD COLUMN fecha_cierre DATETIME",
        "ALTER TABLE journal ADD COLUMN fuente TEXT DEFAULT 'MANUAL'",
        "ALTER TABLE journal ADD COLUMN exchange_trade_id TEXT",
        "ALTER TABLE journal ADD COLUMN confianza_bot INTEGER",
        "ALTER TABLE journal ADD COLUMN apalancamiento FLOAT DEFAULT 1.0",
        "ALTER TABLE journal ADD COLUMN capital_cuenta FLOAT",
        "ALTER TABLE journal ADD COLUMN margen_usado FLOAT",
        "ALTER TABLE journal ADD COLUMN tipo_margen TEXT DEFAULT 'AISLADO'",
        "ALTER TABLE journal ADD COLUMN tipo_trade TEXT DEFAULT 'SWING'",
        "ALTER TABLE notificaciones ADD COLUMN trade_id INTEGER REFERENCES journal(id)",
        "ALTER TABLE usuarios ADD COLUMN bitunix_api_key TEXT",
        "ALTER TABLE usuarios ADD COLUMN bitunix_secret_key TEXT",
        "ALTER TABLE historial_chat ADD COLUMN tf TEXT DEFAULT '4h'",
        "ALTER TABLE usuarios ADD COLUMN totp_secret TEXT",
        "ALTER TABLE usuarios ADD COLUMN totp_habilitado INTEGER DEFAULT 0",
        "ALTER TABLE usuarios ADD COLUMN anthropic_key_enc TEXT",
    ]
    with engine.connect() as conn:
        for sql in nuevas_columnas:
            try:
                conn.execute(db.text(sql))
                conn.commit()
            except Exception as _e:
                logger.debug("Migration skip (columna ya existe): %s", _e)


def run_trigger_migrations(engine):
    """Crea la tabla active_triggers si no existe y añade columnas nuevas."""
    create_sql = """
    CREATE TABLE IF NOT EXISTS active_triggers (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id         INTEGER NOT NULL REFERENCES usuarios(id),
        condicion_texto    TEXT    NOT NULL,
        precio_nivel       REAL,
        direccion          TEXT,
        symbol             TEXT    DEFAULT 'BTC/USDT',
        activo             INTEGER DEFAULT 1,
        disparado          INTEGER DEFAULT 0,
        notificado_en_chat INTEGER DEFAULT 0,
        creado_en          DATETIME DEFAULT CURRENT_TIMESTAMP,
        disparado_en       DATETIME
    )
    """
    with engine.connect() as conn:
        try:
            conn.execute(db.text(create_sql))
            conn.commit()
        except Exception as _e:
            logger.debug("Migration skip (tabla ya existe): %s", _e)
