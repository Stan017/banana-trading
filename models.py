import bcrypt
from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

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
            "id"            : self.id,
            "email"         : self.email,
            "nombre"        : self.nombre,
            "avatar_url"    : self.avatar_url,
            "plan"          : self.plan,
            "fecha_registro": str(self.fecha_registro),
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
    creado_en   = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index("ix_historial_usuario_fecha", "usuario_id", "creado_en"),
    )

    @staticmethod
    def cargar(usuario_id: int) -> list:
        """Carga los últimos MAX_MENSAJES mensajes del usuario como lista de dicts"""
        registros = (
            HistorialChat.query
            .filter_by(usuario_id=usuario_id)
            .order_by(HistorialChat.creado_en.asc())
            .limit(HistorialChat.MAX_MENSAJES)
            .all()
        )
        return [{"role": r.rol, "content": r.contenido} for r in registros]

    @staticmethod
    def guardar(usuario_id: int, rol: str, contenido: str):
        """Guarda un mensaje y limpia los más viejos si supera el límite"""
        nuevo = HistorialChat(
            usuario_id=usuario_id,
            rol=rol,
            contenido=contenido
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