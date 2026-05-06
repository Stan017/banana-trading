"""
gunicorn.conf.py — Configuración de producción para TradeBot AI
Ejecutar con: gunicorn -c gunicorn.conf.py app_flask:app
"""

import multiprocessing

# ── Workers ──────────────────────────────────────────────────
# UN solo worker con múltiples threads.
# Motivo: rate limiting y caché de régimen son in-memory.
# Con múltiples workers, cada uno tendría su propio estado
# y un usuario podría hacer N×10 req/min (bypassear el límite).
# Con threads en un solo proceso, todo comparte la misma memoria.
workers     = 1
worker_class = "gthread"
threads     = 4    # hasta 4 requests simultáneas

# ── Timeouts ─────────────────────────────────────────────────
timeout          = 120   # Claude API puede tardar ~30s, con margen
keepalive        = 5
graceful_timeout = 30

# ── Binding ──────────────────────────────────────────────────
# Railway inyecta $PORT dinámicamente — nunca hardcodear 5000
import os as _os
bind = f"0.0.0.0:{_os.environ.get('PORT', '5000')}"

# ── Logging ──────────────────────────────────────────────────
accesslog  = "-"   # stdout
errorlog   = "-"   # stderr
loglevel   = "warning"

# ── Seguridad ────────────────────────────────────────────────
# Limita tamaño de request body (evita DoS con payloads gigantes)
limit_request_line   = 4096
limit_request_fields = 100
