"""
hmm_regime.py — Detección de régimen de mercado con Hidden Markov Models
═════════════════════════════════════════════════════════════════════════
Reemplaza la detección determinística (reglas EMA/RSI) por un modelo
probabilístico que APRENDE los estados del mercado desde datos reales.

4 estados ocultos aprendidos automáticamente:
  ALCISTA  — retornos positivos, volatilidad moderada
  BAJISTA  — retornos negativos, volatilidad media-alta
  LATERAL  — retornos cercanos a cero, volatilidad baja
  HIGH_VOL — volatilidad extrema independiente de dirección

Features por vela diaria (5 dimensiones):
  log_return   — retorno logarítmico diario
  abs_return   — magnitud del movimiento
  vol_ratio    — rolling 5d std / rolling 20d std (compresión/expansión)
  momentum_14d — % cambio precio en 14 días
  range_pct    — (high - low) / close × 100

Uso:
    from hmm_regime import get_regimen_hmm
    result = get_regimen_hmm()
    # → {"estado": "ALCISTA", "confianza": 0.87, "probabilidades": {...}, ...}
"""

import os
import time
import pickle
import logging
import threading
import numpy as np

logger = logging.getLogger(__name__)

MODEL_PATH       = os.path.join(os.path.dirname(__file__), "hmm_model.pkl")
MODEL_MAX_AGE_S  = 7 * 86400   # reentrenar cada 7 días
N_STATES         = 4
N_TRAIN_DAYS     = 1825        # ~5 años de datos diarios

# Cache en memoria del resultado HMM (10 min)
_hmm_cache: dict  = {}
_HMM_CACHE_TTL    = 600        # segundos

# Singleton del detector — se inicializa en background al arrancar Flask
_detector = None
_detector_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# CLASE PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

class HMMRegimeDetector:
    """
    Wrapper de GaussianHMM con auto-labeling de estados y cache.
    Thread-safe para lectura concurrente desde Flask.
    """

    def __init__(self):
        self.model        = None
        self.state_labels: dict = {}   # {int → str}
        self.trained_at   = None
        self._lock        = threading.Lock()

    # ── Feature engineering ──────────────────────────────────────────────────

    def _build_features(self, ohlcv_list: list) -> np.ndarray:
        """
        Recibe lista de dicts {open, high, low, close, volumen} ordenada ASC.
        Retorna ndarray (n, 5) limpio de NaNs.
        """
        closes = np.array([v["close"] for v in ohlcv_list], dtype=float)
        highs  = np.array([v["high"]  for v in ohlcv_list], dtype=float)
        lows   = np.array([v["low"]   for v in ohlcv_list], dtype=float)
        n      = len(closes)

        log_ret = np.zeros(n)
        log_ret[1:] = np.log(closes[1:] / closes[:-1])

        abs_ret = np.abs(log_ret)

        # Rolling std con ventana deslizante manual (numpy puro, sin pandas)
        def rolling_std(arr, w):
            out = np.full(n, np.nan)
            for i in range(w - 1, n):
                out[i] = arr[i - w + 1:i + 1].std()
            return out

        vol5  = rolling_std(log_ret, 5)
        vol20 = rolling_std(log_ret, 20)
        vol_ratio = np.where(vol20 > 0, vol5 / vol20, 1.0)

        momentum = np.full(n, np.nan)
        momentum[14:] = (closes[14:] / closes[:-14] - 1) * 100

        range_pct = (highs - lows) / closes * 100

        X = np.column_stack([log_ret, abs_ret, vol_ratio, momentum, range_pct])

        # Eliminar filas con NaN (primeras ~20 velas)
        mask = ~np.isnan(X).any(axis=1)
        return X[mask].astype(np.float64)

    # ── Auto-labeling ────────────────────────────────────────────────────────

    def _label_states(self):
        """
        Asigna ALCISTA / BAJISTA / LATERAL / HIGH_VOL a los estados aprendidos
        basándose en las medias de las distribuciones de emisión.

        Feature index:
          0 = log_return   1 = abs_return
          2 = vol_ratio    3 = momentum_14d   4 = range_pct
        """
        means = self.model.means_          # (N_STATES, 5)
        assigned = {}
        remaining = list(range(N_STATES))

        # HIGH_VOL: mayor volatilidad absoluta (abs_return + range_pct)
        vol_score = means[:, 1] + means[:, 4] * 0.4
        hv = int(np.argmax(vol_score))
        assigned[hv] = "HIGH_VOL"
        remaining.remove(hv)

        # ALCISTA: mayor log_return + momentum positivo
        ret_score = np.array([means[i, 0] + means[i, 3] * 0.05 for i in remaining])
        bull = remaining[int(np.argmax(ret_score))]
        assigned[bull] = "ALCISTA"
        remaining.remove(bull)

        # BAJISTA: menor log_return + momentum negativo
        ret_score2 = np.array([means[i, 0] + means[i, 3] * 0.05 for i in remaining])
        bear = remaining[int(np.argmin(ret_score2))]
        assigned[bear] = "BAJISTA"
        remaining.remove(bear)

        # LATERAL: el que queda
        lat = remaining[0]
        assigned[lat] = "LATERAL"

        self.state_labels = assigned
        logger.info(f"HMM state labels: { {v:k for k,v in assigned.items()} }")

    # ── Entrenamiento ────────────────────────────────────────────────────────

    def fit(self, ohlcv_list: list):
        """
        Entrena el GaussianHMM con datos históricos.
        ohlcv_list: lista de dicts {open, high, low, close, volumen} diarios.
        """
        from hmmlearn.hmm import GaussianHMM

        X = self._build_features(ohlcv_list)
        if len(X) < 200:
            raise ValueError(f"Datos insuficientes para entrenar HMM: {len(X)} muestras")

        logger.info(f"Entrenando HMM con {len(X)} muestras ({N_STATES} estados)...")

        model = GaussianHMM(
            n_components   = N_STATES,
            covariance_type= "diag",
            n_iter         = 300,
            tol            = 1e-4,
            random_state   = 42,
            verbose        = False,
        )
        model.fit(X)

        with self._lock:
            self.model      = model
            self.trained_at = time.time()
            self._label_states()

        self._save()
        logger.info("HMM entrenamiento completado ✓")
        return self

    # ── Predicción ───────────────────────────────────────────────────────────

    def predict(self, ohlcv_list: list) -> dict:
        """
        Predice el régimen actual (última vela de la lista).
        Retorna dict con estado, probabilidades, confianza y señal de transición.
        """
        with self._lock:
            if self.model is None:
                return _empty_result("Modelo no entrenado")

        X = self._build_features(ohlcv_list)
        if len(X) < 30:
            return _empty_result("Datos insuficientes para predicción")

        try:
            with self._lock:
                hidden_states = self.model.predict(X)
                state_probs   = self.model.predict_proba(X)
                labels        = dict(self.state_labels)

            current_idx   = int(hidden_states[-1])
            current_probs = state_probs[-1]
            current_label = labels.get(current_idx, "INDEFINIDO")
            confianza     = float(current_probs[current_idx])

            probs_labeled = {
                labels.get(i, f"S{i}"): round(float(p), 3)
                for i, p in enumerate(current_probs)
            }

            # Últimas 5 velas para detectar transición reciente
            recientes = [labels.get(int(s), "?") for s in hidden_states[-5:]]
            transicion = len(recientes) >= 2 and recientes[-1] != recientes[-2]

            # Tendencia de probabilidad: ¿la confianza sube o baja?
            if len(state_probs) >= 5:
                conf_trend = float(state_probs[-1][current_idx]) - float(state_probs[-5][current_idx])
            else:
                conf_trend = 0.0

            return {
                "estado":        current_label,
                "confianza":     round(confianza, 3),
                "probabilidades": probs_labeled,
                "recientes":     recientes,
                "transicion":    transicion,
                "conf_trend":    round(conf_trend, 3),
                "error":         None,
            }
        except Exception as e:
            logger.error(f"HMM predict error: {e}")
            return _empty_result(str(e))

    # ── Persistencia ─────────────────────────────────────────────────────────

    def _save(self):
        try:
            with self._lock:
                data = {
                    "model":        self.model,
                    "state_labels": self.state_labels,
                    "trained_at":   self.trained_at,
                }
            with open(MODEL_PATH, "wb") as f:
                pickle.dump(data, f)
            logger.info(f"HMM guardado en {MODEL_PATH}")
        except Exception as e:
            logger.warning(f"Error guardando HMM: {e}")

    def load(self) -> bool:
        """Carga modelo del disco. Retorna True si el cache es válido."""
        if not os.path.exists(MODEL_PATH):
            return False
        try:
            with open(MODEL_PATH, "rb") as f:
                data = pickle.load(f)
            age = time.time() - data.get("trained_at", 0)
            if age > MODEL_MAX_AGE_S:
                logger.info(f"HMM cache expirado ({age/86400:.1f}d). Necesita reentrenamiento.")
                return False
            with self._lock:
                self.model        = data["model"]
                self.state_labels = data["state_labels"]
                self.trained_at   = data["trained_at"]
            logger.info(f"HMM cargado del cache ({age/3600:.1f}h de antigüedad)")
            return True
        except Exception as e:
            logger.warning(f"Error cargando HMM cache: {e}")
            return False

    @property
    def is_ready(self) -> bool:
        with self._lock:
            return self.model is not None


# ─────────────────────────────────────────────────────────────────────────────
# API PÚBLICA
# ─────────────────────────────────────────────────────────────────────────────

def _empty_result(error: str) -> dict:
    return {
        "estado": "INDEFINIDO", "confianza": 0.0,
        "probabilidades": {}, "recientes": [],
        "transicion": False, "conf_trend": 0.0, "error": error,
    }


def get_detector() -> HMMRegimeDetector:
    """Devuelve el singleton del detector (thread-safe). Carga cache si existe."""
    global _detector
    with _detector_lock:
        if _detector is None:
            det = HMMRegimeDetector()
            det.load()   # intenta cargar del disco; no bloquea si no hay cache
            _detector = det
    return _detector


def get_regimen_hmm(symbol: str = "BTC/USDT") -> dict:
    """
    Obtiene el régimen HMM actual con cache de 10 minutos.
    Si el modelo no está entrenado, retorna estado INDEFINIDO sin bloquear.
    """
    ahora = time.time()
    cached = _hmm_cache.get(symbol)
    if cached and (ahora - cached["ts"]) < _HMM_CACHE_TTL:
        return cached["data"]

    det = get_detector()
    if not det.is_ready:
        result = _empty_result("Modelo en entrenamiento — disponible en breve")
        _hmm_cache[symbol] = {"data": result, "ts": ahora}
        return result

    try:
        from binance_data import get_velas
        velas = get_velas(symbol, "1d", 210)
        result = det.predict(velas)
    except Exception as e:
        result = _empty_result(str(e))

    _hmm_cache[symbol] = {"data": result, "ts": ahora}
    return result


def inicializar_hmm_background(symbol: str = "BTC/USDT"):
    """
    Inicia en un hilo daemon la carga (o entrenamiento si no hay cache)
    del modelo HMM. Llamar desde app_flask.py al arrancar.
    No bloquea el servidor.
    """
    def _worker():
        det = get_detector()
        if det.load():
            logger.info("HMM listo (cargado del cache)")
            return
        # No hay cache válido → entrenar desde cero
        try:
            logger.info("HMM: descargando datos históricos para entrenamiento...")
            from stats_engine import fetch_ohlcv_binance
            df = fetch_ohlcv_binance(interval="1d", limit=N_TRAIN_DAYS)
            # Convertir DataFrame a lista de dicts compatible con _build_features
            velas = [
                {"open": r["open"], "high": r["high"],
                 "low": r["low"],   "close": r["close"], "volumen": r["volume"]}
                for _, r in df.iterrows()
            ]
            det.fit(velas)
        except Exception as e:
            logger.error(f"HMM entrenamiento fallido: {e}")

    t = threading.Thread(target=_worker, name="hmm-trainer", daemon=True)
    t.start()
    logger.info("HMM: hilo de inicialización arrancado")
    return t


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS DE TEXTO PARA EL LLM
# ─────────────────────────────────────────────────────────────────────────────

# Narrativa por estado para inyectar en el system prompt
_HMM_NARRATIVA = {
    "ALCISTA": (
        "HMM confirma régimen ALCISTA aprendido desde datos históricos. "
        "El modelo detecta patrón de retornos positivos y volatilidad moderada "
        "consistente con fases de uptrend sostenido."
    ),
    "BAJISTA": (
        "HMM detecta régimen BAJISTA. El modelo identifica patrón de retornos "
        "negativos con volatilidad media-alta, característico de bear markets "
        "y correcciones estructurales."
    ),
    "LATERAL": (
        "HMM detecta régimen LATERAL. El mercado muestra baja volatilidad y "
        "retornos cercanos a cero — acumulación o distribución. "
        "Rangos ajustados, esperar catalizador."
    ),
    "HIGH_VOL": (
        "HMM detecta régimen HIGH_VOL. Volatilidad extrema sin dirección clara — "
        "típico de eventos de liquidación masiva, noticias macro o cambio de régimen. "
        "Reducir sizing, evitar apalancamiento."
    ),
    "INDEFINIDO": (
        "HMM en calibración — régimen no determinado aún."
    ),
}

_HMM_EMOJI = {
    "ALCISTA": "📈", "BAJISTA": "📉",
    "LATERAL": "↔️", "HIGH_VOL": "🌪️", "INDEFINIDO": "❓",
}


def hmm_bloque_contexto(resultado: dict) -> str:
    """Genera línea de contexto para el bloque del system prompt."""
    if resultado.get("error") and resultado["estado"] == "INDEFINIDO":
        return "HMM Régimen: calibrando..."

    estado    = resultado["estado"]
    conf      = resultado["confianza"]
    emoji     = _HMM_EMOJI.get(estado, "")
    narrativa = _HMM_NARRATIVA.get(estado, "")
    trans_txt = " ⚡ TRANSICIÓN DETECTADA" if resultado.get("transicion") else ""
    conf_txt  = f"{'↑' if resultado.get('conf_trend', 0) > 0.05 else '↓' if resultado.get('conf_trend', 0) < -0.05 else '→'} {conf:.0%}"

    probs = resultado.get("probabilidades", {})
    probs_str = " | ".join(f"{k}: {v:.0%}" for k, v in sorted(probs.items(), key=lambda x: -x[1]))

    return (
        f"HMM Régimen: {estado} {emoji} ({conf_txt}){trans_txt}\n"
        f"  Probs: [{probs_str}]\n"
        f"  {narrativa}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI — entrenamiento manual
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    print("Descargando datos históricos...")
    from stats_engine import fetch_ohlcv_binance
    df = fetch_ohlcv_binance("1d", N_TRAIN_DAYS)
    velas = [
        {"open": r["open"], "high": r["high"],
         "low":  r["low"],  "close": r["close"], "volumen": r["volume"]}
        for _, r in df.iterrows()
    ]
    det = HMMRegimeDetector()
    det.fit(velas)

    print("\nPredicción actual:")
    pred = det.predict(velas[-210:])
    print(json.dumps(pred, indent=2))
    print("\nBloque LLM:")
    import sys
    sys.stdout.buffer.write((hmm_bloque_contexto(pred) + "\n").encode("utf-8"))
