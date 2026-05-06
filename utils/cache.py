"""
utils/cache.py — CacheManager centralizado
════════════════════════════════════════════
Una sola fuente de verdad para todos los cachés en memoria del app.
Centraliza TTLs, permite limpiar caches desde un solo lugar,
y sustituye los patrones ad-hoc de función-atributo y dict manual.

USO:
    from utils.cache import CACHE_REGISTRY

    # Leer
    cached = CACHE_REGISTRY["scanner"].get()
    if cached:
        return jsonify(cached)

    # Escribir
    CACHE_REGISTRY["scanner"].set(payload)

    # Fallback en error (datos aunque estén expirados)
    stale = CACHE_REGISTRY["scanner"].get_stale()

TTLs registrados:
    scanner   → 120s   (/api/scanner — datos de confluencias)
    liquidity → 60s    (/api/liquidity — L2 order book)
    regimen   → 600s   (régimen de mercado — usado por RAG + scanner)

Nota: los cachés de binance_data.py (precio, velas, funding, OI, L/S, DXY,
FNG, correlación) mantienen su propio sistema de caché con eviction LRU
interno — no se migran aquí para no romper ese módulo.
"""

import time
import logging

logger = logging.getLogger(__name__)


class SimpleCache:
    """
    Caché en memoria con TTL por clave.
    Thread-safe para lecturas concurrentes (GIL de Python protege dict ops).
    No usa locks — las escrituras son atómicas por el GIL.
    """

    def __init__(self, ttl: float, name: str = ""):
        self.ttl   = ttl
        self.name  = name
        self._store: dict = {}

    def get(self, key: str = "default"):
        """
        Retorna datos si están frescos, None si expiraron o no existen.
        Uso principal: verificar si el caché es válido antes de ir a la API.
        """
        entry = self._store.get(key)
        if entry and (time.time() - entry["ts"]) < self.ttl:
            return entry["data"]
        return None

    def set(self, value, key: str = "default"):
        """Almacena valor con timestamp actual."""
        self._store[key] = {"data": value, "ts": time.time()}

    def get_stale(self, key: str = "default"):
        """
        Retorna datos aunque estén expirados.
        Uso: fallback en errores de red para no dejar al usuario sin datos.
        """
        entry = self._store.get(key)
        return entry["data"] if entry else None

    def is_fresh(self, key: str = "default") -> bool:
        """True si el caché existe y no ha expirado."""
        entry = self._store.get(key)
        return bool(entry and (time.time() - entry["ts"]) < self.ttl)

    def invalidate(self, key: str = "default"):
        """Elimina una entrada específica."""
        self._store.pop(key, None)

    def clear(self):
        """Limpia todas las entradas."""
        self._store.clear()
        logger.warning(f"Cache '{self.name}' limpiado")

    def age(self, key: str = "default") -> float | None:
        """Retorna segundos desde la última actualización, o None si no existe."""
        entry = self._store.get(key)
        return (time.time() - entry["ts"]) if entry else None

    def __repr__(self) -> str:
        keys  = list(self._store.keys())
        fresh = [k for k in keys if self.is_fresh(k)]
        return f"SimpleCache(name={self.name!r}, ttl={self.ttl}s, entries={len(keys)}, fresh={len(fresh)})"


# ============================================================
# REGISTRO CENTRAL DE CACHÉS
# ============================================================
# Fuente única de verdad para TTLs del app.
# Al añadir un nuevo caché en memoria, registrarlo aquí.

CACHE_REGISTRY: dict[str, SimpleCache] = {
    # ── API routes ───────────────────────────────────────────
    "scanner":         SimpleCache(ttl=120, name="scanner"),         # /api/scanner (HTF 4H)
    "scanner_multitf": SimpleCache(ttl=90,  name="scanner_multitf"), # /api/scanner/multitf
    "liquidity":       SimpleCache(ttl=60,  name="liquidity"),       # /api/liquidity

    # ── Contexto compartido (RAG + scanner) ──────────────────
    "regimen":         SimpleCache(ttl=600, name="regimen"),         # régimen de mercado BTC
}


def clear_all_caches():
    """Limpia todos los cachés registrados. Útil para debugging o tests."""
    for cache in CACHE_REGISTRY.values():
        cache.clear()
    logger.warning("Todos los cachés en memoria limpiados")


def cache_status() -> dict:
    """Retorna estado de todos los cachés — para endpoint de diagnóstico."""
    return {
        name: {
            "ttl_s":    cache.ttl,
            "fresh":    cache.is_fresh(),
            "age_s":    cache.age(),
        }
        for name, cache in CACHE_REGISTRY.items()
    }
