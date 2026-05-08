"""
chat_routes.py — Blueprint del chat con el LLM
═══════════════════════════════════════════════
Endpoints:
    POST /chat                    → consulta principal al LLM con contexto completo
    POST /chat/limpiar            → borra historial del usuario
    GET  /chat/trade/<trade_id>   → abre chat con contexto de trade inyectado
"""

import re
import time
import logging
import threading
import secrets as _secrets
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, request, jsonify, render_template, redirect
from flask_login import login_required, current_user

import anthropic as _anthropic
from cryptography.fernet import Fernet as _Fernet, InvalidToken as _InvalidToken

from models import db, HistorialChat, Journal, ActiveTrigger
from resources import (
    claude,
    CLAUDE_MODEL,
    buscar_contexto_con_regimen as buscar_contexto,
    build_system_prompt,
    necesita_datos_mercado,
    detectar_symbol,
    chunks_count as _chunks_count_inicial,
    get_regimen_cached,
)
from config import FERNET_KEY as _FERNET_KEY, MODEL_MAP as _MODEL_MAP


def _get_claude_client(user):
    """
    Retorna el cliente Anthropic a usar para esta petición.
    Si el usuario Pro tiene BYOK configurado, usa su key cifrada.
    Si falla el descifrado, cae silenciosamente al cliente global.
    """
    if user.es_pro() and user.anthropic_key_enc:
        try:
            f      = _Fernet(_FERNET_KEY.encode() if isinstance(_FERNET_KEY, str) else _FERNET_KEY)
            api_key = f.decrypt(user.anthropic_key_enc.encode()).decode()
            return _anthropic.Anthropic(api_key=api_key)
        except (_InvalidToken, Exception) as exc:
            logger.error(f"BYOK: error descifrando key usuario {user.id}: {exc}")
    return claude  # fallback al cliente global

try:
    from binance_data import (
        get_contexto_mercado,
        get_l2_liquidity,
        get_liquidation_zones,
        get_btc_dxy_correlation,
        get_contexto_superior,
    )
except ImportError:
    def get_contexto_mercado(symbol="BTC/USDT", tf="4h"): return ""
    def get_l2_liquidity(symbol): return {"error": "Import fallido"}
    def get_liquidation_zones(symbol): return {}
    def get_btc_dxy_correlation(): return {"error": "Import fallido"}
    def get_contexto_superior(symbol="BTC/USDT", tf="4h"): return ""

try:
    from analysis.delta import get_delta_per_candle, format_delta_context
except ImportError:
    def get_delta_per_candle(symbol="BTC/USDT", tf="4h", n=3): return []
    def format_delta_context(deltas, tf="4h"): return ""

try:
    from scanner import evaluar_multitf as _evaluar_multitf
except ImportError:
    def _evaluar_multitf(symbol): return {"ok": False, "error": "Import fallido"}

try:
    from analysis.volume_profile import get_volume_profile, format_vp_context
except ImportError:
    def get_volume_profile(symbol="BTC/USDT", tf="4h", limit=200, bins=50): return {}
    def format_vp_context(vp, precio_actual=None): return ""

try:
    from scanner import evaluar_confluencias, evaluar_confluencias_ltf
except ImportError:
    def evaluar_confluencias(symbol):
        return {"setup_ok": False, "score": 0, "confluencias": [], "error": "Import fallido"}
    def evaluar_confluencias_ltf(symbol, tf="15m"):
        return {"setup_ok": False, "score": 0, "confluencias": [], "error": "Import fallido"}

from utils.helpers import (
    get_client_ip,
    check_rate_limit,
    check_plan_limit,
    check_prompt_injection,
    get_journal_stats,
    _get_trade_context,
    _trade_contexts,
    _TRADE_CTX_TTL,
)

logger = logging.getLogger(__name__)

chat_bp = Blueprint("chat_bp", __name__)


# ============================================================
# TRIGGER STATE MACHINE — helpers
# ============================================================

_TRIGGER_RE = re.compile(
    r"TRIGGER:\s*\[(LONG|SHORT)\]\s*\|"
    r"\s*precio:\s*([0-9]{4,7}(?:\.[0-9]+)?)\s*\|"
    r"\s*condicion:\s*(.+)",
    re.IGNORECASE,
)

def _extraer_y_guardar_trigger(texto: str, usuario_id: int, symbol: str) -> None:
    """
    Busca un TRIGGER emitido por el bot en `texto`.
    Formato esperado (al final de DECISIÓN):
        TRIGGER:[LONG/SHORT]|precio:XXXXX|condicion:texto libre
    Si encuentra uno válido, lo persiste en active_triggers.
    """
    try:
        m = _TRIGGER_RE.search(texto)
        if not m:
            return
        direccion  = m.group(1).upper()          # "LONG" o "SHORT"
        precio_str = m.group(2).replace(",", "")
        condicion  = m.group(3).strip()[:500]    # cap 500 chars
        precio     = float(precio_str)
        if precio <= 0:
            return

        trigger = ActiveTrigger(
            usuario_id      = usuario_id,
            condicion_texto = condicion,
            precio_nivel    = precio,
            direccion       = direccion,
            symbol          = symbol,
            activo          = True,
            disparado       = False,
            notificado_en_chat = False,
        )
        db.session.add(trigger)
        db.session.commit()
        logger.info(f"Trigger guardado: {direccion} ${precio:,.0f} — {symbol} — user {usuario_id}")
    except Exception as exc:
        logger.error(f"Error guardando trigger: {exc}")
        db.session.rollback()


@chat_bp.route("/chat/trade/<int:trade_id>")
@login_required
def chat_con_trade(trade_id: int):
    """Abre el chat con contexto de trade inyectado internamente — el usuario ve un mensaje limpio."""
    trade = db.session.get(Journal, trade_id)
    if not trade or trade.usuario_id != current_user.id:
        return redirect("/")

    from routes.journal_routes import formatear_trade_para_ia

    pal      = trade.apalancamiento or 1.0
    palstr   = f"{int(pal)}x" if pal > 1 else "spot"
    estado_str = "ABIERTO" if trade.estado == "ABIERTO" else f"CERRADO ({trade.resultado})"
    tipo_str = trade.tipo_trade or "SWING"

    # Para trades ABIERTOS: obtener precio actual y pasarlo a formatear_trade_para_ia
    _precio_actual = None
    if trade.estado == "ABIERTO":
        try:
            from binance_data import get_precio_actual
            sym = trade.activo or "BTC"
            if "/" not in sym:
                sym = sym + "/USDT"
            datos_precio = get_precio_actual(sym)
            if datos_precio and datos_precio.get("precio"):
                _precio_actual = float(datos_precio["precio"])
        except Exception:
            pass

    trade_dict = trade.to_dict()
    trade_formateado = formatear_trade_para_ia(trade_dict, precio_actual=_precio_actual)

    contexto_interno = (
        "[MODO: ANÁLISIS DE TRADE — REGLAS ESTRICTAS]\n"
        "ESTRUCTURA (en orden, sin repetir ideas entre secciones):\n"
        "1) Trade vs mercado actual\n"
        "2) Análisis retroactivo de la entrada\n"
        "3) Máx. 3 errores técnicos/psicológicos\n"
        "4) Una sugerencia concreta\n"
        "5) Contexto histórico estadístico — brevemente al final\n"
        "RESTRICCIONES: Una idea = una sección. Sin tablas redundantes. "
        "PROHIBIDO ABSOLUTO: escribir frases como 'el análisis del 2026-XX-XX', 'según el RAG', 'análisis histórico predijo', 'setup del YYYY-MM-DD', o cualquier referencia a fechas de análisis pasados. "
        "Sección 5 = SOLO datos estadísticos puros (día semana %, FOMC días, halving fase, CME gap nivel). Sin narrativa de 'análisis previos'. "
        "NUNCA uses 'ganancia en papel' — usa 'PnL no realizado'.\n"
        "USA LOS NÚMEROS PRE-CALCULADOS del bloque PNL — no recalcules.\n\n"
        + trade_formateado
    )
    # Token único por tab — evita contaminación entre pestañas del mismo usuario
    tab_token = _secrets.token_urlsafe(16)
    _trade_contexts[tab_token] = {"context": contexto_interno, "ts": time.time()}

    # Mensaje visible para el usuario — limpio y corto
    mensaje_display = f"Analiza mi trade: {trade.activo} {trade.direccion} {tipo_str} | {palstr} | Entrada ${trade.entrada:,.2f} | Estado {estado_str}"

    return render_template("index.html",
                           chunks=_chunks_count_inicial,
                           trade_mensaje=mensaje_display,
                           tab_token=tab_token)


@chat_bp.route("/chat", methods=["POST"])
@login_required
def chat():
    ip  = get_client_ip()
    uid = current_user.email

    # ── Capa 1: Rate limit por minuto ──
    if not check_rate_limit(uid):
        logger.error(f"Rate limit exceeded — user: {uid}")
        return jsonify({"error": "Too many requests. Wait a moment."}), 429

    # ── Capa 2: Límite por plan (DB) ──
    permitido, msg = check_plan_limit(current_user)
    if not permitido:
        return jsonify({"error": msg}), 429

    # ── Trigger disparado pendiente de notificar en chat ──
    _trigger_alerta = ""
    try:
        trg_disparado = ActiveTrigger.query.filter_by(
            usuario_id=current_user.id,
            disparado=True,
            notificado_en_chat=False,
        ).order_by(ActiveTrigger.disparado_en.desc()).first()
        if trg_disparado:
            _trigger_alerta = (
                f"[ALERTA TRIGGER DISPARADO] Tu trigger {trg_disparado.direccion} "
                f"${trg_disparado.precio_nivel:,.0f} en {trg_disparado.symbol} "
                f"se activó. Condición: {trg_disparado.condicion_texto}\n\n"
            )
            trg_disparado.notificado_en_chat = True
            db.session.commit()
    except Exception as _te:
        logger.error(f"Error leyendo trigger disparado: {_te}")

    data     = request.json
    pregunta = data.get("pregunta", "").strip()
    tf       = data.get("tf", "4h").lower()
    if tf not in ("15m", "1h", "4h", "1d"):
        tf = "4h"

    # ── Modelo seleccionado por el usuario ──────────────────────────────────
    _model_key = data.get("model", "haiku")
    # Sonnet solo para Pro (a menos que tenga BYOK — paga con su key)
    _has_byok  = bool(current_user.anthropic_key_enc)
    if _model_key == "sonnet" and not current_user.es_pro() and not _has_byok:
        _model_key = "haiku"   # downgrade silencioso
    _request_model = _MODEL_MAP.get(_model_key, CLAUDE_MODEL)

    if not pregunta:
        return jsonify({"error": "Empty message"}), 400

    # ── Layer 3: Length limit (visible user message only) ──
    if len(pregunta) > 1000:
        return jsonify({"error": "Message too long. Maximum 1000 characters."}), 400

    # ── Layer 4: Prompt injection ──
    if check_prompt_injection(pregunta):
        logger.error(f"Prompt injection detected — IP: {ip} — text: {pregunta[:100]}")
        return jsonify({"error": "Query not allowed."}), 400

    # ── Contexto de trade inyectado internamente (ruta /chat/trade/<id>) ──
    # Token único por tab — inmune a sobreescritura entre pestañas
    tab_token     = data.get("tab_token", "")
    trade_context = _get_trade_context(tab_token) if tab_token else None

    # ── Pre-calentar caché del régimen — garantiza query expansion en RAG ──
    get_regimen_cached()

    # ── Historial desde DB — memoria persistente entre sesiones ──
    historial = HistorialChat.cargar(current_user.id, tf=tf)

    try:
        symbol = detectar_symbol(pregunta) if necesita_datos_mercado(pregunta) else None

        # ── Paralelizar RAG + datos mercado + system prompt + scanner ──
        # Todos independientes entre sí — caché del régimen ya caliente
        scanner_symbol = symbol or "BTC/USDT"
        _scanner_fn = (
            lambda s: evaluar_confluencias_ltf(s, tf)
            if tf in ("15m", "1h")
            else evaluar_confluencias(s)
        )
        with ThreadPoolExecutor(max_workers=4) as executor:
            fut_contexto = executor.submit(buscar_contexto, pregunta)
            fut_sistema  = executor.submit(build_system_prompt, tf)
            fut_mercado  = executor.submit(get_contexto_mercado, symbol, tf) if symbol else None
            fut_scanner  = executor.submit(_scanner_fn, scanner_symbol)

        contexto      = fut_contexto.result()
        system_prompt = fut_sistema.result()

        datos_mercado = ""
        if fut_mercado:
            try:
                datos_mercado = fut_mercado.result()
            except Exception as e:
                logger.error(f"Error obteniendo datos de mercado: {e}")

        # ── Scanner confluencias — siempre activo (datos cacheados) ──
        scanner_contexto = ""
        try:
            sc = fut_scanner.result()
            confs         = sc.get("confluencias", [])
            alerta_valida = sc.get("alerta_valida", False)
            setup_ok      = sc.get("setup_ok", False)
            setup_pot     = sc.get("setup_potencial", False)
            bias          = sc.get("bias")
            falta         = sc.get("falta")
            score_total   = sc.get("score_total", 0)
            conviction    = sc.get("conviction", "BAJA")
            regimen       = sc.get("regimen", "INDEFINIDO")
            e             = sc.get("edge_desglose", {})

            if not sc.get("error") and confs:
                if alerta_valida:
                    estado = ("ALERTA VALIDA — SETUP " + str(bias)
                              + " | Score " + str(score_total) + "/100 (" + conviction + ")"
                              + " | Regimen: " + regimen)
                elif setup_ok or setup_pot:
                    tipo = "4/4" if setup_ok else "3/4 POTENCIAL"
                    estado = ("SETUP " + tipo + " " + str(bias)
                              + " FILTRADO por scoring — Score " + str(score_total) + "/100"
                              + " | Regimen: " + regimen + " (setup contratendencia o baja conviccion)")
                else:
                    estado = ("Sin setup — " + str(sc.get("score", 0)) + "/5 confluencias"
                              + " | Regimen: " + regimen)

                _scanner_label = f"LTF {tf.upper()}" if tf in ("15m", "1h") else "HTF 4H"
                lineas = ["SCANNER " + _scanner_label + " v3 (" + scanner_symbol + ") — " + estado + ":"]
                lineas.append("  Scoring: Macro " + str(sc.get("score_macro",0)) + "/35"
                              + " | Edge " + str(sc.get("score_edge",0)) + "/25"
                              + " | Tecnico " + str(sc.get("score_tecnico",0)) + "/40"
                              + " | TOTAL " + str(score_total) + "/100")
                if e:
                    lineas.append("  Kill zone: " + str(e.get("kill_zone","—"))
                                  + " | " + str(e.get("fomc","—"))
                                  + " | " + str(e.get("fng","—")))
                for c in confs:
                    icono = "SI" if c["ok"] else "NO"
                    lineas.append("  [" + icono + "] " + c["nombre"] + ": " + c["detalle"])
                if falta:
                    lineas.append("  Falta para 6/6: " + str(falta))
                # CVD — order flow
                cvd_bias  = sc.get("cvd_bias", "neutral")
                cvd_div   = sc.get("cvd_divergencia", False)
                cvd_delta = sc.get("cvd_delta")
                cvd_str = ("DIVERGENTE ⚠" if cvd_div
                           else ("ALCISTA" if cvd_bias == "bullish"
                                 else ("BAJISTA" if cvd_bias == "bearish" else "NEUTRO")))
                delta_str = (f" (delta {cvd_delta:+.0f} BTC)" if cvd_delta is not None else "")
                lineas.append("  [CVD Order Flow] " + cvd_str + delta_str)
                scanner_contexto = "\n".join(lineas)
        except Exception as e:
            logger.error(f"Error en scanner confluencias: {e}")

        # ── L2 Order Book + BTC-DXY Correlation ─────────────────
        l2_contexto = ""
        try:
            l2   = get_l2_liquidity(scanner_symbol)
            liq  = get_liquidation_zones(scanner_symbol)
            corr = get_btc_dxy_correlation()

            lineas_l2 = []

            if not l2.get("error"):
                imb  = l2.get("imbalance_pct", 50)
                nbid = l2.get("nearest_bid_wall")
                nask = l2.get("nearest_ask_wall")
                bd1  = l2.get("bid_depth_1pct", 0)
                ad1  = l2.get("ask_depth_1pct", 0)
                bid_str = (f"${nbid['price']:,.0f} (${nbid['usd']/1e6:.1f}M, -{nbid['dist_pct']:.1f}%)" if nbid else "Sin pared significativa")
                ask_str = (f"${nask['price']:,.0f} (${nask['usd']/1e6:.1f}M, +{nask['dist_pct']:.1f}%)" if nask else "Sin pared significativa")
                liq_l = liq.get("longs", {})
                liq_s = liq.get("shorts", {})
                lineas_l2 += [
                    "ORDER BOOK L2 (" + scanner_symbol + "):",
                    "  Imbalance: " + str(imb) + "% bid / " + str(round(100-imb,1)) + "% ask",
                    "  Pared soporte: " + bid_str,
                    "  Pared resistencia: " + ask_str,
                    "  Depth ±1%: bid $" + str(round(bd1/1e6,1)) + "M vs ask $" + str(round(ad1/1e6,1)) + "M",
                    "  Liq. estimada longs:  10x ~$" + str(liq_l.get('10x','?')) + " | 25x ~$" + str(liq_l.get('25x','?')) + " | 50x ~$" + str(liq_l.get('50x','?')),
                    "  Liq. estimada shorts: 10x ~$" + str(liq_s.get('10x','?')) + " | 25x ~$" + str(liq_s.get('25x','?')) + " | 50x ~$" + str(liq_s.get('50x','?')),
                    "  NOTA: zonas de liquidacion son estimacion matematica.",
                ]

            if not corr.get("error") and corr.get("corr_30d") is not None:
                c30 = corr["corr_30d"]
                c90 = corr.get("corr_90d")
                lectura  = corr.get("lectura", "")
                rotacion = c90 is not None and (c30 - c90) > 0.25
                lineas_l2 += [
                    "",
                    "CORRELACION BTC-DXY:",
                    "  30D: " + f"{c30:+.3f}" + (" | 90D: " + f"{c90:+.3f}" if c90 else ""),
                    "  Lectura: " + lectura,
                    ("  ⚠ ALERTA: correlacion girando positiva — posible ruptura de regimen" if rotacion else ""),
                ]

            if lineas_l2:
                l2_contexto = "\n".join(l for l in lineas_l2 if l is not None)

        except Exception as e:
            logger.error(f"Error en L2/corr contexto: {e}")

        # ── Order Flow: Delta por vela ─────────────────────────
        delta_contexto = ""
        try:
            deltas = get_delta_per_candle(scanner_symbol, tf, n=3)
            delta_contexto = format_delta_context(deltas, tf)
        except Exception as e:
            logger.error(f"Error en delta contexto: {e}")

        # ── Volume Profile: VPOC / VAH / VAL / HVN / LVN ─────
        vp_contexto = ""
        try:
            precio_actual_vp = None
            if fut_mercado:
                try:
                    _md = fut_mercado.result()
                    if isinstance(_md, dict):
                        precio_actual_vp = _md.get("precio") or _md.get("price")
                    elif isinstance(_md, str) and "Precio:" in _md:
                        import re as _re
                        _m = _re.search(r"Precio:\s*\$?([\d,]+(?:\.\d+)?)", _md)
                        if _m:
                            precio_actual_vp = float(_m.group(1).replace(",", ""))
                except Exception:
                    pass
            vp = get_volume_profile(scanner_symbol, tf)
            vp_contexto = format_vp_context(vp, precio_actual_vp)
        except Exception as e:
            logger.error(f"Error en volume profile contexto: {e}")

        # ── HTF Context Superior ──────────────────────────────
        htf_contexto = ""
        try:
            htf_contexto = get_contexto_superior(scanner_symbol, tf)
        except Exception as e:
            logger.error(f"Error en HTF contexto superior: {e}")

        # ── Multi-TF Alignment (4H + 1H + 15M) ──────────────────
        multitf_contexto = ""
        try:
            mtf_data = _evaluar_multitf(scanner_symbol)
            if mtf_data.get("ok") and mtf_data.get("alineacion"):
                htf_d  = mtf_data.get("htf", {})
                mtf_d  = mtf_data.get("mtf") or {}
                ltf_d  = mtf_data.get("ltf") or {}
                alin   = mtf_data["alineacion"]
                trg    = mtf_data.get("trigger", "")

                def _tf_line(label, d):
                    if not d:
                        return f"SCANNER {label}: sin datos"
                    return (f"SCANNER {label}: {d.get('bias','—')} "
                            f"| Score {d.get('score',0)}/100 ({d.get('conviction','—')}) "
                            f"| {d.get('confluencias_ok',0)}/8 conf")

                multitf_contexto = (
                    f"ALINEACIÓN MULTI-TF ({scanner_symbol}):\n"
                    f"  {_tf_line('HTF 4H', htf_d)}\n"
                    f"  {_tf_line('MTF 1H', mtf_d)}\n"
                    f"  {_tf_line('LTF 15M', ltf_d)}\n"
                    f"  ESTADO: {alin} — {trg}"
                )
        except Exception as e:
            logger.error(f"Error en multi-TF contexto: {e}")

        # ── Journal stats — DB, fuera del executor (necesita app context) ──
        journal_stats = get_journal_stats(current_user.id)

        # ── Helper: línea de sesiones para el contexto ───────────────────────
        def _build_session_line(sess):
            h   = sess.get("historico", {})
            hoy = sess.get("hoy", {})
            hist = (
                "Session Deep Dive -- historico " + str(h.get("dias_analizados", "?")) + " dias: "
                + "London rompe Asia High " + str(h.get("london_broke_high_rate")) + "% | "
                + "London rompe Asia Low "  + str(h.get("london_broke_low_rate"))  + "% | "
                + "NY rompe Asia High "     + str(h.get("ny_broke_high_rate"))     + "% | "
                + "NY rompe Asia Low "      + str(h.get("ny_broke_low_rate"))      + "% | "
                + "NY ret post London High sweep: " + str(h.get("ny_ret_after_london_high")) + "% "
                + "(" + str(h.get("ny_positive_after_london_high")) + "% alcista) | "
                + "NY ret post London Low sweep: "  + str(h.get("ny_ret_after_london_low"))  + "% "
                + "(" + str(h.get("ny_positive_after_london_low"))  + "% alcista)"
            )
            hoy_parts = []
            if hoy:
                hoy_parts.append(
                    "Sesion actual: " + str(hoy.get("sesion_actual"))
                    + " | Asia range hoy: $" + str(hoy.get("asia_high")) + " - $" + str(hoy.get("asia_low"))
                    + " (" + str(hoy.get("asia_range_pct")) + "%)"
                )
                sweeps_hoy = []
                if hoy.get("london_broke_high"): sweeps_hoy.append("London barrio Asia High")
                if hoy.get("london_broke_low"):  sweeps_hoy.append("London barrio Asia Low")
                if hoy.get("ny_broke_high"):     sweeps_hoy.append("NY barrio Asia High")
                if hoy.get("ny_broke_low"):      sweeps_hoy.append("NY barrio Asia Low")
                hoy_parts.append("Sweeps hoy: " + (", ".join(sweeps_hoy) if sweeps_hoy else "ninguno aun"))
            return hist + " || HOY: " + " | ".join(hoy_parts) if hoy_parts else hist

        # ── Edge Analytics — contexto estadístico histórico ──────────────────
        edge_contexto = ""
        try:
            from stats_engine import cargar_stats_cache
            s = cargar_stats_cache()

            dow     = s.get("day_of_week", {})
            vol     = s.get("volatilidad", {})
            sweep   = s.get("sweep_returns", {})
            pbm     = s.get("post_big_move", {})
            halving = s.get("halving", {})
            cme     = s.get("cme_gap", {})
            kz      = s.get("kill_zones", {})
            monthly = s.get("monthly_bias", {})
            wom     = s.get("week_of_month", {})
            fomc    = s.get("fomc", {})
            sess    = s.get("session_dive", {})

            sesgo_hoy     = dow.get("sesgo_hoy", {})
            sesgo_mes     = monthly.get("sesgo_mes_actual", {})
            sesgo_semana  = wom.get("sesgo_semana_actual", {})

            gap_activo = cme.get("gap_activo")
            if gap_activo:
                cme_txt = ("Si -- nivel $" + str(gap_activo.get("nivel_gap"))
                           + " (" + str(gap_activo.get("gap_tipo"))
                           + ", " + str(cme.get("distancia_al_gap_pct")) + "% de distancia)")
            else:
                cme_txt = "No hay gap activo"

            big_move_txt = (
                "HOY hay big move " + str(pbm.get("tipo_hoy"))
                + " (" + str(pbm.get("retorno_hoy_pct")) + "%)"
                if pbm.get("big_move_hoy") else "Sin big move hoy"
            )

            kz_txt = (
                "KILL ZONE ACTIVA -- mayor probabilidad de movimiento"
                if kz.get("es_kill_zone_activa") else "Fuera de kill zone principal"
            )

            fomc_h   = fomc.get("historico", {})
            fomc_txt = (
                "HOY ES DIA FOMC -- rango esperado "
                + str(fomc_h.get("range_expansion_ratio", "?")) + "x mayor al normal"
                if fomc.get("es_dia_fomc") else (
                    "SEMANA FOMC -- " + str(fomc.get("dias_para_proximo")) + " dias para el proximo ("
                    + str(fomc.get("proximo_fomc")) + ")"
                    if fomc.get("es_semana_fomc") else
                    "Proximo FOMC: " + str(fomc.get("proximo_fomc"))
                    + " (" + str(fomc.get("dias_para_proximo")) + " dias)"
                )
            )

            sep = "=" * 52
            edge_contexto = "\n".join([
                "",
                sep + " EDGE ANALYTICS -- CONTEXTO ESTADISTICO HISTORICO " + sep,
                ("Dia actual: " + str(dow.get("dia_actual"))
                 + " | Retorno promedio historico: " + str(sesgo_hoy.get("avg_return", "N/A"))
                 + "% | Tasa alcista: " + str(sesgo_hoy.get("positive_rate", "N/A"))
                 + "% | Mejor dia hist: " + str(dow.get("mejor_dia", "—"))
                 + " | Peor dia hist: " + str(dow.get("peor_dia", "—"))),
                ("Mes actual: " + str(monthly.get("mes_actual"))
                 + " | Retorno mensual hist: " + str(sesgo_mes.get("avg_monthly_return", "N/A"))
                 + "% | Win rate mensual: " + str(sesgo_mes.get("monthly_win_rate", "N/A"))
                 + "% | MTD: " + str(monthly.get("mtd_actual", "N/A")) + "%"
                 + " | Mejor mes hist: " + str(monthly.get("mejor_mes", "—"))),
                (str(wom.get("semana_actual_label", "Semana actual"))
                 + " | Avg ret historico esta semana del mes: " + str(sesgo_semana.get("avg_return", "N/A"))
                 + "% | Tasa alcista: " + str(sesgo_semana.get("positive_rate", "N/A"))
                 + "% | Mejor semana hist: " + str(wom.get("mejor_semana", "—"))
                 + " | Peor semana hist: " + str(wom.get("peor_semana", "—"))),
                ("Volatilidad actual (30D anualizada): " + str(vol.get("vol_30d_anualizada"))
                 + "% -- Percentil: " + str(vol.get("percentil_vol_actual"))
                 + "% (" + str(vol.get("interpretacion")) + ")"),
                ("Sweeps historicos -- Reversal rate global: " + str(sweep.get("global_reversal_rate"))
                 + "% | BSL: " + str(sweep.get("bsl", {}).get("reversal_rate"))
                 + "% | SSL: " + str(sweep.get("ssl", {}).get("reversal_rate")) + "%"),
                ("Post big move (>" + str(pbm.get("umbral_pct")) + "%): " + big_move_txt
                 + " | After big up 3D avg: " + str(pbm.get("after_big_up", {}).get("3d_avg"))
                 + "% | After big down 3D avg: " + str(pbm.get("after_big_down", {}).get("3d_avg")) + "%"),
                ("Fase del ciclo halving: " + str(halving.get("fase_ciclo"))
                 + " (" + str(halving.get("dias_desde_halving")) + " dias desde ultimo halving)"),
                ("CME Gap activo: " + cme_txt
                 + " | Fill rate historico: " + str(cme.get("fill_rate_historico")) + "%"),
                ("Kill zone activa ahora: " + str(kz.get("kill_zone_activa_ahora")) + " UTC | " + kz_txt),
                *([(fomc_txt
                 + " | Historico FOMC (" + str(fomc_h.get("total_fomc_analizados", 0)) + " eventos): "
                 + "rango expansion " + str(fomc_h.get("range_expansion_ratio")) + "x"
                 + " | post-1D avg: " + str(fomc_h.get("retorno_post1_avg")) + "%"
                 + " (" + str(fomc_h.get("positive_rate_post1")) + "% alcista)"
                 + " | post-3D avg: " + str(fomc_h.get("retorno_post3_avg")) + "%")]
                 if fomc.get("dias_para_proximo", 99) <= 5 else []),
                _build_session_line(sess),
                sep * 2,
            ])
        except Exception as e:
            logger.error(f"Error cargando edge stats: {e}")

        # Si viene de /chat/trade/<id>, prepend el contexto interno al final
        pregunta_claude = (
            f"{trade_context}\n\nPregunta del trader: {pregunta}"
            if trade_context else pregunta
        )

        # Inyección de formato — al final para máxima atención del modelo
        _es_analisis = necesita_datos_mercado(pregunta)
        # Detectar idioma del usuario por caracteres españoles
        _spanish_chars = set('áéíóúüñÁÉÍÓÚÜÑ¿¡')
        _is_english = not any(c in pregunta for c in _spanish_chars)
        if _es_analisis:
            if _is_english:
                _tf_nombres   = {"15m": "15 minutes", "1h": "1 hour", "4h": "4 hours", "1d": "Daily"}
                _fmt_reminder = (
                    f"\n\nRESPOND ENTIRELY IN ENGLISH. MANDATORY FORMAT — Analysis on {tf.upper()} ({_tf_nombres.get(tf, tf)}): "
                    "Respond ONLY with the 4 sections (MACRO, TECHNICAL, LIQUIDITY, DECISION). "
                    "One line per indicator. No scenarios. End with a follow-up question."
                )
            else:
                _tf_nombres   = {"15m": "15 minutos", "1h": "1 hora", "4h": "4 horas", "1d": "Daily"}
                _fmt_reminder = (
                    f"\n\nRESPONDE ENTERAMENTE EN ESPAÑOL. FORMATO OBLIGATORIO — Análisis en {tf.upper()} ({_tf_nombres.get(tf, tf)}): "
                    "Responde ÚNICAMENTE con las 4 secciones (MACRO, TÉCNICO, LIQUIDEZ, DECISIÓN). "
                    "Una línea por indicador. Sin escenarios. Termina con la pregunta de follow-up."
                )
        else:
            _fmt_reminder = (
                "\n\nRESPOND ENTIRELY IN ENGLISH." if _is_english
                else "\n\nRESPONDE ENTERAMENTE EN ESPAÑOL."
            )

        mensaje_enriquecido = (
            (_trigger_alerta if _trigger_alerta else "")
            + f"Conocimiento especializado:\n{contexto}\n\n"
            f"{datos_mercado}\n\n"
            f"{scanner_contexto}\n\n"
            f"{l2_contexto}\n\n"
            + (f"ORDER FLOW GRANULAR:\n{delta_contexto}\n\n" if delta_contexto else "")
            + (f"VOLUME PROFILE ({tf.upper()}):\n{vp_contexto}\n\n" if vp_contexto else "")
            + (f"CONTEXTO HTF SUPERIOR:\n{htf_contexto}\n\n" if htf_contexto else "")
            + (f"{multitf_contexto}\n\n" if multitf_contexto else "")
            + f"{journal_stats}\n\n"
            f"{edge_contexto}\n\n"
            f"Pregunta: {pregunta_claude}"
            f"{_fmt_reminder}"
        )
        mensajes_api = historial + [{"role": "user", "content": mensaje_enriquecido}]

        _claude_client = _get_claude_client(current_user)
        resp = _claude_client.messages.create(
            model=_request_model,
            max_tokens=2500,
            system=system_prompt,
            messages=mensajes_api
        )
        texto = resp.content[0].text

        # ── Extraer y guardar trigger si el bot emitió uno ──
        _extraer_y_guardar_trigger(texto, current_user.id, scanner_symbol)

        # ── Guardar en DB ──
        HistorialChat.guardar(current_user.id, "user",      pregunta, tf=tf)
        HistorialChat.guardar(current_user.id, "assistant", texto,    tf=tf)

    except Exception as e:
        logger.error(f"Error in /chat — IP: {ip} — error: {e}")
        return jsonify({"error": "Internal error. Please try again."}), 500

    return jsonify({"respuesta": texto})


@chat_bp.route("/chat/limpiar", methods=["POST"])
@login_required
def chat_limpiar():
    """Borra el historial del usuario — botón 'Nueva conversación'."""
    HistorialChat.limpiar(current_user.id)
    return jsonify({"ok": True})
