"""
ws_monitor.py — WebSocket real-time monitor: triggers + order flow
══════════════════════════════════════════════════════════════════
Dos streams por symbol:
  · miniTicker  → precio en tiempo real para disparar triggers
  · aggTrade    → trades ejecutados → rolling buy/sell volume (1m y 5m)

Flujo:
    1. Al iniciar: lee todos los ActiveTrigger activos → suscribe streams
    2. miniTicker tick: compara precio contra triggers del symbol
    3. aggTrade tick: acumula buy/sell USD en deque rolling
    4. Cada 60s: refresca symbols (nuevos triggers desde el chat)
    5. Reconexión automática con backoff en caso de desconexión

Uso:
    from ws_monitor import WSTriggerMonitor, get_monitor
    monitor = WSTriggerMonitor(app)
    monitor.start()

    # Desde cualquier módulo:
    flow = get_monitor().get_order_flow("BTC/USDT")
"""

import json
import time
import logging
import threading
from collections import deque
from datetime import datetime, timezone

import websocket

logger = logging.getLogger(__name__)

# Singleton global para acceso desde rutas sin imports circulares
_monitor_instance = None


def get_monitor():
    """Devuelve la instancia activa del WSTriggerMonitor (o None si no arrancó)."""
    return _monitor_instance


class WSTriggerMonitor:
    WS_BASE          = "wss://stream.binance.com:9443/stream"
    RECONNECT_DELAY  = 5    # segundos entre reconexiones
    REFRESH_INTERVAL = 60   # segundos entre refresh de symbols activos
    MAX_RECONNECTS   = 10   # intentos consecutivos antes de loggear warning

    # Rolling buffer: guarda hasta 10 min de trades para ventanas 1m/5m
    _BUFFER_MAX  = 20_000   # ~10 min a 30 trades/s
    _WIN_1M      = 60       # segundos
    _WIN_5M      = 300

    def __init__(self, app):
        global _monitor_instance
        self.app             = app
        self._ws             = None
        self._running        = False
        self._symbols: set   = set()
        self._lock           = threading.Lock()
        self._last_prices    = {}   # symbol -> float
        self._last_refresh   = 0.0
        self._reconnect_count= 0

        # Order flow: deque de (ts_s, usd_vol, is_buy, symbol)
        # thread-safe para append; usamos _of_lock para iteración
        self._of_buf:  deque = deque(maxlen=self._BUFFER_MAX)
        self._of_lock          = threading.Lock()

        _monitor_instance = self

    # ──────────────────────────────────────────────────────────
    # PUBLIC API
    # ──────────────────────────────────────────────────────────

    def start(self):
        """Inicia el monitor en un hilo daemon — no bloquea."""
        self._running = True
        t = threading.Thread(target=self._run_loop, name="ws-trigger-monitor", daemon=True)
        t.start()
        logger.info("WSTriggerMonitor iniciado")
        return t

    def stop(self):
        """Detiene el monitor limpiamente."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        logger.info("WSTriggerMonitor detenido")

    def get_price(self, symbol: str) -> float | None:
        """Devuelve el último precio conocido para un symbol."""
        return self._last_prices.get(symbol)

    def get_order_flow(self, symbol: str = "BTC/USDT") -> dict:
        """
        Calcula buy/sell volume acumulado en ventanas rolling de 1m y 5m.

        Retorna:
            ratio_1m   : % volumen comprador en el último minuto (50 = neutral)
            ratio_5m   : ídem para 5 minutos
            bias_1m    : "buy" | "sell" | "neutral"
            bias_5m    : "buy" | "sell" | "neutral"
            buy_vol_1m : USD comprador último minuto
            sell_vol_1m: USD vendedor último minuto
            buy_vol_5m : USD comprador últimos 5 minutos
            sell_vol_5m: USD vendedor últimos 5 minutos
            delta_1m   : buy - sell USD (1m)
            delta_5m   : buy - sell USD (5m)
            trades_1m  : nº de trades en la ventana 1m
            trades_5m  : nº de trades en la ventana 5m
            updated_at : timestamp (epoch) del último trade registrado
        """
        now = time.time()
        cutoff_1m = now - self._WIN_1M
        cutoff_5m = now - self._WIN_5M

        buy_1m = sell_1m = 0.0
        buy_5m = sell_5m = 0.0
        n_1m   = n_5m    = 0
        last_ts = 0.0

        sym_norm = symbol.upper().replace("/", "")  # BTCUSDT

        with self._of_lock:
            for ts, usd, is_buy, sym in self._of_buf:
                if sym != sym_norm:
                    continue
                if ts < cutoff_5m:
                    continue
                if is_buy:
                    buy_5m += usd
                else:
                    sell_5m += usd
                n_5m += 1
                if ts >= cutoff_1m:
                    if is_buy:
                        buy_1m += usd
                    else:
                        sell_1m += usd
                    n_1m += 1
                if ts > last_ts:
                    last_ts = ts

        def _ratio(b, s):
            total = b + s
            return round(b / total * 100, 1) if total > 0 else 50.0

        def _bias(ratio):
            if ratio >= 58:   return "buy"
            if ratio <= 42:   return "sell"
            return "neutral"

        r1 = _ratio(buy_1m, sell_1m)
        r5 = _ratio(buy_5m, sell_5m)

        return {
            "ratio_1m":    r1,
            "ratio_5m":    r5,
            "bias_1m":     _bias(r1),
            "bias_5m":     _bias(r5),
            "buy_vol_1m":  round(buy_1m,  0),
            "sell_vol_1m": round(sell_1m, 0),
            "buy_vol_5m":  round(buy_5m,  0),
            "sell_vol_5m": round(sell_5m, 0),
            "delta_1m":    round(buy_1m  - sell_1m, 0),
            "delta_5m":    round(buy_5m  - sell_5m, 0),
            "trades_1m":   n_1m,
            "trades_5m":   n_5m,
            "updated_at":  round(last_ts, 1),
        }

    # ──────────────────────────────────────────────────────────
    # INTERNALS
    # ──────────────────────────────────────────────────────────

    def _get_active_symbols(self) -> set:
        """Lee symbols con triggers activos desde DB."""
        with self.app.app_context():
            try:
                from models import ActiveTrigger
                triggers = ActiveTrigger.query.filter_by(
                    activo=True, disparado=False
                ).all()
                syms = {t.symbol or "BTC/USDT" for t in triggers}
                return syms or {"BTC/USDT"}   # siempre suscribir BTC como mínimo
            except Exception as e:
                logger.error(f"WS: error leyendo triggers: {e}")
                return {"BTC/USDT"}

    def _to_symbol(self, raw: str) -> str:
        """'BTCUSDT' → 'BTC/USDT'  (solo pares /USDT por ahora)"""
        if raw.endswith("USDT") and len(raw) > 4:
            return raw[:-4] + "/USDT"
        return raw

    def _build_url(self, symbols: set) -> str:
        """Construye URL con miniTicker + aggTrade por symbol."""
        streams = []
        for s in sorted(symbols):
            base = s.replace("/", "").lower()
            streams.append(f"{base}@miniTicker")
            streams.append(f"{base}@aggTrade")
        return f"{self.WS_BASE}?streams={'/'.join(streams)}"

    # ──────────────────────────────────────────────────────────
    # WS CALLBACKS
    # ──────────────────────────────────────────────────────────

    def _on_open(self, ws):
        self._reconnect_count = 0
        logger.info(f"WS Binance conectado — {len(self._symbols)} symbols: {self._symbols}")

    def _on_error(self, ws, error):
        logger.error(f"WS error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        logger.warning(f"WS cerrado — code={close_status_code} msg={close_msg}")

    def _on_message(self, ws, raw):
        try:
            data        = json.loads(raw)
            stream_name = data.get("stream", "").lower()   # "btcusdt@miniticker"
            stream_data = data.get("data", data)

            if "@aggtrade" in stream_name:
                self._on_agg_trade(stream_data)
            else:
                # miniTicker (o cualquier otro stream)
                symbol_raw = stream_data.get("s", "")
                price_str  = stream_data.get("c", "0")
                if not symbol_raw or not price_str:
                    return

                symbol = self._to_symbol(symbol_raw)
                price  = float(price_str)
                if price <= 0:
                    return

                self._last_prices[symbol] = price
                self._check_triggers(symbol, price)

            # Refresh de subscripciones si hay nuevos triggers
            if time.time() - self._last_refresh > self.REFRESH_INTERVAL:
                self._refresh_subscriptions()

        except Exception as e:
            logger.error(f"WS on_message error: {e}")

    # ──────────────────────────────────────────────────────────
    # ORDER FLOW (aggTrade)
    # ──────────────────────────────────────────────────────────

    def _on_agg_trade(self, d: dict):
        """
        Procesa un mensaje aggTrade de Binance Futures.
        Campos clave:
            s  : symbol  (ej. "BTCUSDT")
            p  : price   (str)
            q  : qty BTC (str)
            m  : isBuyerMaker (bool) — True = aggressive seller, False = aggressive buyer
            T  : trade time ms
        """
        try:
            sym     = d.get("s", "")
            price   = float(d.get("p", 0))
            qty     = float(d.get("q", 0))
            is_maker = d.get("m", True)   # True = seller es maker → venta agresiva
            ts      = d.get("T", 0) / 1000.0   # → epoch segundos

            if price <= 0 or qty <= 0 or not sym:
                return

            usd_vol = price * qty
            is_buy  = not is_maker   # agressive buy = buyer es taker

            with self._of_lock:
                self._of_buf.append((ts, usd_vol, is_buy, sym))

        except Exception as e:
            logger.debug(f"_on_agg_trade error: {e}")

    # ──────────────────────────────────────────────────────────
    # TRIGGER LOGIC
    # ──────────────────────────────────────────────────────────

    def _check_triggers(self, symbol: str, precio: float):
        """Chequea todos los triggers activos para `symbol` contra `precio`."""
        with self.app.app_context():
            try:
                from models import db, ActiveTrigger, Notificacion

                triggers = ActiveTrigger.query.filter_by(
                    symbol=symbol, activo=True, disparado=False
                ).all()

                for t in triggers:
                    if t.precio_nivel is None:
                        continue

                    disparado = False
                    if t.direccion == "LONG" and precio >= t.precio_nivel:
                        disparado = True
                    elif t.direccion == "SHORT" and precio <= t.precio_nivel:
                        disparado = True
                    elif t.direccion is None:
                        # Sin dirección: cruce ±0.15%
                        disparado = abs(precio - t.precio_nivel) / t.precio_nivel * 100 < 0.15

                    if not disparado:
                        continue

                    # ── Marcar como disparado ────────────────
                    t.disparado    = True
                    t.disparado_en = datetime.now(timezone.utc)
                    t.activo       = False

                    # ── Notificación in-app ──────────────────
                    n = Notificacion(
                        usuario_id = t.usuario_id,
                        tipo       = "trigger",
                        nivel      = "AMARILLO",
                        titulo     = f"Trigger activado — ${t.precio_nivel:,.0f}",
                        mensaje    = (
                            f"{t.condicion_texto}\n"
                            f"Precio actual: ${precio:,.2f} | "
                            f"Nivel: ${t.precio_nivel:,.0f}"
                        ),
                    )
                    db.session.add(n)
                    db.session.commit()

                    # ── Telegram ─────────────────────────────
                    try:
                        from notificaciones import _telegram
                        dir_label = t.direccion or "NIVEL"
                        _telegram(
                            f"*TRIGGER {dir_label} ACTIVADO*\n"
                            f"_{t.condicion_texto}_\n"
                            f"Precio: `${precio:,.2f}` | Nivel: `${t.precio_nivel:,.0f}`\n"
                            f"Symbol: {symbol}"
                        )
                    except Exception as te:
                        logger.error(f"WS telegram error: {te}")

                    logger.info(
                        f"Trigger #{t.id} disparado via WS — "
                        f"{t.direccion} {symbol} ${precio:,.0f} (nivel ${t.precio_nivel:,.0f})"
                    )

            except Exception as e:
                logger.error(f"WS _check_triggers error: {e}")
                try:
                    from models import db
                    db.session.rollback()
                except Exception:
                    pass

    # ──────────────────────────────────────────────────────────
    # SUBSCRIPTION MANAGEMENT
    # ──────────────────────────────────────────────────────────

    def _refresh_subscriptions(self):
        """
        Compara symbols actuales con los de DB.
        Si cambiaron (nuevo trigger), cierra el WS para reconectar
        con la nueva lista de streams.
        """
        self._last_refresh = time.time()
        new_symbols = self._get_active_symbols()
        if new_symbols != self._symbols:
            logger.info(
                f"WS: nuevos symbols detectados "
                f"{self._symbols} → {new_symbols}. Reconectando..."
            )
            self._symbols = new_symbols
            if self._ws:
                try:
                    self._ws.close()  # Fuerza reconexión con streams actualizados
                except Exception:
                    pass

    # ──────────────────────────────────────────────────────────
    # MAIN LOOP
    # ──────────────────────────────────────────────────────────

    def _run_loop(self):
        """
        Loop principal del monitor.
        Se reconecta automáticamente con backoff exponencial (cap 60s).
        """
        while self._running:
            try:
                self._symbols = self._get_active_symbols()
                url = self._build_url(self._symbols)
                logger.info(f"WS: conectando — {url[:80]}...")

                self._ws = websocket.WebSocketApp(
                    url,
                    on_open    = self._on_open,
                    on_message = self._on_message,
                    on_error   = self._on_error,
                    on_close   = self._on_close,
                )
                # run_forever bloquea hasta que se cierre
                self._ws.run_forever(
                    ping_interval = 30,
                    ping_timeout  = 10,
                )

            except Exception as e:
                logger.error(f"WS run_loop error: {e}")

            if not self._running:
                break

            # Backoff exponencial: 5s → 10s → 20s → ... → 60s cap
            self._reconnect_count += 1
            delay = min(self.RECONNECT_DELAY * (2 ** min(self._reconnect_count - 1, 3)), 60)
            if self._reconnect_count >= self.MAX_RECONNECTS:
                logger.warning(
                    f"WS: {self._reconnect_count} reconexiones consecutivas. "
                    f"Esperando {delay}s..."
                )
            else:
                logger.info(f"WS: reconectando en {delay}s...")
            time.sleep(delay)
