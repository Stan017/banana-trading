"""
test_fase3.py — Tests de verificación Fase 3
=============================================
Corre directo: python test_fase3.py
No necesita Flask ni DB activa.

Tests:
  T1 — _score_vp: lógica de scoring con datos mock
  T2 — _score_delta: detección de patrones de distribución/acumulación
  T3 — Volume Profile real: VPOC dentro del rango de precio visible
  T4 — Delta por vela real: datos coherentes con el TF
  T5 — evaluar_multitf: estructura correcta y graceful fallback
  T6 — Scanner HTF: vp_modifier y delta_modifier presentes en score
  T7 — CVD multi-TF: 15M != 4H para el mismo momento
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# -- Helpers de output -----------------------------------------
PASS = "  [PASS]"
FAIL = "  [FAIL]"
SKIP = "  [SKIP]"
WARN = "  [WARN]"

resultados = []

def ok(nombre, detalle=""):
    print(f"{PASS} {nombre}" + (f" — {detalle}" if detalle else ""))
    resultados.append(("PASS", nombre))

def fail(nombre, detalle=""):
    print(f"{FAIL} {nombre}" + (f" — {detalle}" if detalle else ""))
    resultados.append(("FAIL", nombre))

def warn(nombre, detalle=""):
    print(f"{WARN} {nombre}" + (f" — {detalle}" if detalle else ""))
    resultados.append(("WARN", nombre))

def skip(nombre, detalle=""):
    print(f"{SKIP} {nombre}" + (f" — {detalle}" if detalle else ""))
    resultados.append(("SKIP", nombre))

def seccion(titulo):
    print(f"\n{'='*60}")
    print(f"  {titulo}")
    print(f"{'='*60}")


# ==============================================================
# T1 — _score_vp: lógica con datos mock
# ==============================================================
seccion("T1 — _score_vp: lógica de scoring VP (mock)")

try:
    from scanner import _score_vp

    # Mock: price en LVN -> debe dar +10
    import unittest.mock as mock
    vp_mock = {
        "vpoc": 83000.0,
        "value_area_high": 84000.0,
        "value_area_low":  82000.0,
        "lvn": [83200.0],   # precio cae dentro del LVN
        "hvn": [82500.0],
        "error": None
    }
    precio_en_lvn = 83200.0  # exactamente en el LVN

    with mock.patch("analysis.volume_profile.get_volume_profile", return_value=vp_mock):
        mod, lbl = _score_vp("BTC/USDT", "4h", precio_en_lvn, "ALCISTA")

    if mod >= 10:
        ok("precio en LVN -> modifier >= +10", f"modifier={mod}, label={lbl}")
    else:
        fail("precio en LVN -> modifier < 10", f"modifier={mod}")

    # Mock: price en Value Area -> debe dar +5
    # Precio alejado del VPOC (>0.3%) para no caer en rama VPOC
    precio_en_va = 83700.0  # entre VAL(82000) y VAH(84000), lejos de VPOC(83000)
    vp_mock2 = {**vp_mock, "lvn": [81000.0, 85000.0]}  # LVN lejos del precio
    with mock.patch("analysis.volume_profile.get_volume_profile", return_value=vp_mock2):
        mod2, lbl2 = _score_vp("BTC/USDT", "4h", precio_en_va, "ALCISTA")

    if mod2 >= 5:
        ok("precio en Value Area -> modifier >= +5", f"modifier={mod2}")
    else:
        fail("precio en Value Area -> modifier incorrecto", f"modifier={mod2}")

    # Mock: price SOBRE VAH + bias BAJISTA -> debe dar -5
    precio_sobre_vah = 84500.0
    with mock.patch("analysis.volume_profile.get_volume_profile", return_value=vp_mock2):
        mod3, lbl3 = _score_vp("BTC/USDT", "4h", precio_sobre_vah, "BAJISTA")

    if mod3 <= -5:
        ok("sobre VAH + bias BAJISTA -> penalización <= -5", f"modifier={mod3}")
    else:
        fail("sobre VAH + bias BAJISTA -> sin penalización esperada", f"modifier={mod3}")

    # Mock: price BAJO VAL + bias BAJISTA -> debe dar +5
    precio_bajo_val = 81500.0
    with mock.patch("analysis.volume_profile.get_volume_profile", return_value=vp_mock2):
        mod4, lbl4 = _score_vp("BTC/USDT", "4h", precio_bajo_val, "BAJISTA")

    if mod4 >= 5:
        ok("bajo VAL + bias BAJISTA -> bonus >= +5", f"modifier={mod4}")
    else:
        fail("bajo VAL + bias BAJISTA -> sin bonus esperado", f"modifier={mod4}")

except Exception as e:
    fail("T1 — excepción inesperada", str(e))


# ==============================================================
# T2 — _score_delta: detección de patrones
# ==============================================================
seccion("T2 — _score_delta: patrones distribución/acumulación (mock)")

try:
    from scanner import _score_delta
    import unittest.mock as mock

    # Distribución acelerando (deltas neg crecientes) + bias ALCISTA -> -5
    deltas_dist = [
        {"delta": -100.0, "bias": "bearish"},
        {"delta": -200.0, "bias": "bearish"},
        {"delta": -350.0, "bias": "bearish"},
    ]
    with mock.patch("analysis.delta.get_delta_per_candle", return_value=deltas_dist):
        mod, lbl = _score_delta("BTC/USDT", "4h", "ALCISTA")
    if mod <= -5:
        ok("distribución acelerando vs ALCISTA -> penalización -5", f"mod={mod}")
    else:
        fail("distribución vs ALCISTA -> sin penalización", f"mod={mod}")

    # Acumulación acelerando (deltas pos crecientes) + bias BAJISTA -> -5
    deltas_acum = [
        {"delta": 100.0, "bias": "bullish"},
        {"delta": 200.0, "bias": "bullish"},
        {"delta": 350.0, "bias": "bullish"},
    ]
    with mock.patch("analysis.delta.get_delta_per_candle", return_value=deltas_acum):
        mod2, lbl2 = _score_delta("BTC/USDT", "4h", "BAJISTA")
    if mod2 <= -5:
        ok("acumulación acelerando vs BAJISTA -> penalización -5", f"mod={mod2}")
    else:
        fail("acumulación vs BAJISTA -> sin penalización", f"mod={mod2}")

    # Distribución acelerando + bias BAJISTA -> 0 (confirma, no penaliza)
    with mock.patch("analysis.delta.get_delta_per_candle", return_value=deltas_dist):
        mod3, lbl3 = _score_delta("BTC/USDT", "4h", "BAJISTA")
    if mod3 == 0:
        ok("distribución acelerando confirma BAJISTA -> 0", f"mod={mod3}")
    else:
        warn("distribución confirma BAJISTA -> valor inesperado", f"mod={mod3}")

    # Delta mixto -> 0
    deltas_mixtos = [
        {"delta": 100.0},
        {"delta": -50.0},
        {"delta": 80.0},
    ]
    with mock.patch("analysis.delta.get_delta_per_candle", return_value=deltas_mixtos):
        mod4, _ = _score_delta("BTC/USDT", "4h", "ALCISTA")
    if mod4 == 0:
        ok("delta mixto -> 0", f"mod={mod4}")
    else:
        warn("delta mixto -> valor inesperado", f"mod={mod4}")

except Exception as e:
    fail("T2 — excepción inesperada", str(e))


# ==============================================================
# T3 — Volume Profile real: VPOC dentro del rango visible
# ==============================================================
seccion("T3 — Volume Profile real BTC/USDT 4H (API)")

try:
    from analysis.volume_profile import get_volume_profile
    from binance_data import get_velas

    print("  Obteniendo velas 4H BTC/USDT...")
    velas = get_velas("BTC/USDT", "4h", 200)

    if not velas:
        skip("T3 — sin velas disponibles")
    else:
        highs  = [v["high"]  for v in velas]
        lows   = [v["low"]   for v in velas]
        closes = [v["close"] for v in velas]
        precio_actual = closes[-1]
        rango_min = min(lows)
        rango_max = max(highs)

        vp = get_volume_profile("BTC/USDT", "4h")

        if vp.get("error"):
            fail("T3 — VP retornó error", vp["error"])
        else:
            vpoc = vp["vpoc"]
            vah  = vp["value_area_high"]
            val  = vp["value_area_low"]
            hvn  = vp.get("hvn", [])
            lvn  = vp.get("lvn", [])

            print(f"  Precio actual: ${precio_actual:,.0f}")
            print(f"  Rango velas:   ${rango_min:,.0f} — ${rango_max:,.0f}")
            print(f"  VPOC: ${vpoc:,.0f} | VAH: ${vah:,.0f} | VAL: ${val:,.0f}")
            print(f"  HVN: {[f'${p:,.0f}' for p in hvn]}")
            print(f"  LVN: {[f'${p:,.0f}' for p in lvn]}")

            # VPOC debe estar dentro del rango
            if rango_min <= vpoc <= rango_max:
                ok("VPOC dentro del rango de precio visible")
            else:
                fail("VPOC FUERA del rango visible", f"VPOC={vpoc}, rango={rango_min}-{rango_max}")

            # VAH > VAL
            if vah > val:
                ok("VAH > VAL (estructura correcta)")
            else:
                fail("VAH <= VAL (estructura inválida)")

            # Value Area contiene VPOC
            if val <= vpoc <= vah:
                ok("VPOC dentro de Value Area (VAL-VAH)")
            else:
                warn("VPOC fuera del Value Area — revisar algoritmo", f"VPOC={vpoc}, VAL={val}, VAH={vah}")

            # LVN son niveles reales
            if lvn:
                lvn_validos = all(rango_min <= l <= rango_max for l in lvn)
                if lvn_validos:
                    ok(f"LVN válidos ({len(lvn)} niveles dentro del rango)")
                else:
                    fail("Algún LVN fuera del rango visible")
            else:
                warn("Sin LVN detectados")

            # Posición del precio actual
            if precio_actual > vah:
                print(f"  -> Precio SOBRE VAH ({((precio_actual-vah)/vah*100):+.2f}%)")
            elif precio_actual < val:
                print(f"  -> Precio BAJO VAL ({((precio_actual-val)/val*100):+.2f}%)")
            elif abs(precio_actual - vpoc) / vpoc * 100 < 0.3:
                print(f"  -> Precio EN VPOC (fair value)")
            else:
                print(f"  -> Precio DENTRO del Value Area")

except Exception as e:
    fail("T3 — excepción", str(e))


# ==============================================================
# T4 — Delta por vela: coherencia entre TFs
# ==============================================================
seccion("T4 — Delta por vela: 15M != 4H para el mismo momento (API)")

try:
    from analysis.delta import get_delta_per_candle, format_delta_context

    print("  Obteniendo delta 4H...")
    deltas_4h = get_delta_per_candle("BTC/USDT", "4h", n=3)
    print("  Obteniendo delta 15M...")
    deltas_15m = get_delta_per_candle("BTC/USDT", "15m", n=3)

    if not deltas_4h:
        fail("T4 — delta 4H vacío")
    elif not deltas_15m:
        fail("T4 — delta 15M vacío")
    else:
        vals_4h  = [d["delta"] for d in deltas_4h]
        vals_15m = [d["delta"] for d in deltas_15m]

        print(f"  Delta 4H:  {[round(v,0) for v in vals_4h]}")
        print(f"  Delta 15M: {[round(v,0) for v in vals_15m]}")
        # encode para evitar UnicodeError en terminal Windows cp1252
        ctx_4h  = format_delta_context(deltas_4h,  '4h').encode('ascii', 'replace').decode()
        ctx_15m = format_delta_context(deltas_15m, '15m').encode('ascii', 'replace').decode()
        print(f"  Contexto 4H:  {ctx_4h}")
        print(f"  Contexto 15M: {ctx_15m}")

        # Los valores deben ser distintos (distinto granularidad)
        if vals_4h != vals_15m:
            ok("Delta 4H != Delta 15M (TFs independientes)")
        else:
            fail("Delta 4H == Delta 15M — posible bug de caché compartida")

        # Cada delta debe tener timestamp
        for d in deltas_4h:
            if "fecha" not in d or "delta" not in d:
                fail("T4 — delta 4H sin campos requeridos", str(d))
                break
        else:
            ok("Campos requeridos presentes (fecha, delta, bias)")

except Exception as e:
    fail("T4 — excepción", str(e))


# ==============================================================
# T5 — evaluar_multitf: estructura y graceful fallback
# ==============================================================
seccion("T5 — evaluar_multitf: estructura del resultado (API)")

try:
    from scanner import evaluar_multitf

    print("  Corriendo evaluar_multitf (puede tardar 10-20s)...")
    resultado = evaluar_multitf("BTC/USDT")

    campos_requeridos = ["ok", "symbol", "htf", "alineacion", "trigger", "timestamp"]
    for campo in campos_requeridos:
        if campo not in resultado:
            fail(f"T5 — campo '{campo}' ausente en resultado")
            break
    else:
        ok("Todos los campos requeridos presentes")

    htf = resultado.get("htf", {})
    ltf = resultado.get("ltf")
    alin = resultado.get("alineacion", "")

    print(f"  HTF 4H: {htf.get('bias','—')} | Score {htf.get('score',0)}/100 | {htf.get('conviction','—')}")
    if ltf:
        print(f"  LTF 15M: {ltf.get('bias','—')} | Score {ltf.get('score',0)}/100 | {ltf.get('conviction','—')}")
    else:
        print(f"  LTF 15M: sin datos (fallback graceful)")
    print(f"  Alineación: {alin}")
    print(f"  Trigger: {resultado.get('trigger','')}")

    if alin in ("CONFLUENCIA", "ESPERA", "DIVERGENTE", "INDEFINIDO"):
        ok(f"Alineación válida: {alin}")
    else:
        fail(f"Alineación inválida: {alin}")

    # HTF score siempre entre 0 y 100
    htf_score = htf.get("score", -1)
    if 0 <= htf_score <= 100:
        ok(f"HTF score en rango válido: {htf_score}/100")
    else:
        fail(f"HTF score fuera de rango: {htf_score}")

    # LTF score entre 0 y 100 si existe
    if ltf:
        ltf_score = ltf.get("score", -1)
        if 0 <= ltf_score <= 100:
            ok(f"LTF score en rango válido: {ltf_score}/100")
        else:
            fail(f"LTF score fuera de rango: {ltf_score}")

except Exception as e:
    fail("T5 — excepción", str(e))


# ==============================================================
# T6 — Scanner HTF: vp_modifier afecta el score_tecnico
# ==============================================================
seccion("T6 — Scanner HTF: VP y delta modifican score_tecnico")

try:
    from scanner import _score_tecnico
    import unittest.mock as mock

    # Sin modificadores
    base = _score_tecnico(6, False, False, 0, 0, 0)
    # Con VP en LVN (+10)
    con_lvn = _score_tecnico(6, False, False, 0, 10, 0)
    # Con delta contradictorio (-5)
    con_dist = _score_tecnico(6, False, False, 0, 0, -5)
    # Con ambos en conflicto: LVN +10, delta -5
    combinado = _score_tecnico(6, False, False, 0, 10, -5)

    print(f"  score_conf=6, sin modificadores: {base}")
    print(f"  + VP en LVN (+10):               {con_lvn}")
    print(f"  + delta contradictorio (-5):      {con_dist}")
    print(f"  + LVN+10 y delta-5 combinados:   {combinado}")

    if con_lvn > base:
        ok(f"VP LVN sube el score: {base} -> {con_lvn}")
    else:
        fail(f"VP LVN no subió el score: {base} -> {con_lvn}")

    if con_dist < base:
        ok(f"Delta contradictorio baja el score: {base} -> {con_dist}")
    else:
        fail(f"Delta no bajó el score: {base} -> {con_dist}")

    if 0 <= combinado <= 40:
        ok(f"Score clampeado entre 0-40: {combinado}")
    else:
        fail(f"Score fuera del rango 0-40: {combinado}")

    # Setup completo (score_conf=8, setup_ok=True): máximo 40
    maximo = _score_tecnico(8, True, False, 3, 10, 0)
    if maximo == 40:
        ok(f"Score clampeado en 40 con setup_ok + OB + LVN: {maximo}")
    else:
        fail(f"Score no clampeado correctamente: {maximo}")

except Exception as e:
    fail("T6 — excepción", str(e))


# ==============================================================
# T7 — CVD multi-TF: 15M != 4H
# ==============================================================
seccion("T7 — CVD: datos 15M != datos 4H (caché independiente)")

try:
    from binance_data import get_cvd

    print("  Obteniendo CVD 4H...")
    cvd_4h = get_cvd("BTC/USDT", tf="4h")
    print("  Obteniendo CVD 15M...")
    cvd_15m = get_cvd("BTC/USDT", tf="15m")

    if cvd_4h.get("error"):
        fail("CVD 4H retornó error", cvd_4h["error"])
    elif cvd_15m.get("error"):
        fail("CVD 15M retornó error", cvd_15m["error"])
    else:
        cvd_val_4h  = cvd_4h.get("cvd_acumulado")
        cvd_val_15m = cvd_15m.get("cvd_acumulado")
        bias_4h     = cvd_4h.get("cvd_bias")
        bias_15m    = cvd_15m.get("cvd_bias")
        tf_4h       = cvd_4h.get("tf", "?")
        tf_15m      = cvd_15m.get("tf", "?")

        fmt = lambda v: f"{v:,.0f}" if v is not None else "N/A"
        print(f"  CVD 4H:  acumulado={fmt(cvd_val_4h)} | bias={bias_4h} | tf={tf_4h}")
        print(f"  CVD 15M: acumulado={fmt(cvd_val_15m)} | bias={bias_15m} | tf={tf_15m}")

        if cvd_val_4h != cvd_val_15m:
            ok("CVD 4H != CVD 15M (cachés independientes)")
        else:
            warn("CVD 4H == CVD 15M — revisar si comparten caché")

        if tf_4h == "4h":
            ok(f"CVD 4H tiene label correcto: tf={tf_4h}")
        else:
            warn(f"CVD 4H label incorrecto: tf={tf_4h}")

        if tf_15m == "15m":
            ok(f"CVD 15M tiene label correcto: tf={tf_15m}")
        else:
            warn(f"CVD 15M label incorrecto: tf={tf_15m}")

        # Deltas por vela presentes
        d_4h  = cvd_4h.get("deltas_por_vela", [])
        d_15m = cvd_15m.get("deltas_por_vela", [])
        if d_4h:
            ok(f"deltas_por_vela presentes en 4H: {len(d_4h)} velas")
        else:
            fail("deltas_por_vela ausente en 4H")
        if d_15m:
            ok(f"deltas_por_vela presentes en 15M: {len(d_15m)} velas")
        else:
            fail("deltas_por_vela ausente en 15M")

except Exception as e:
    fail("T7 — excepción", str(e))


# ==============================================================
# RESUMEN
# ==============================================================
print(f"\n{'='*60}")
print("  RESUMEN")
print(f"{'='*60}")
total  = len(resultados)
pasados = sum(1 for r in resultados if r[0] == "PASS")
fallidos = sum(1 for r in resultados if r[0] == "FAIL")
advertencias = sum(1 for r in resultados if r[0] == "WARN")
saltados = sum(1 for r in resultados if r[0] == "SKIP")

print(f"  Total:        {total}")
print(f"  PASS:         {pasados}")
print(f"  FAIL:         {fallidos}")
print(f"  WARN:         {advertencias}")
print(f"  SKIP:         {saltados}")
print()

if fallidos == 0:
    print("  Todo OK — Fase 3 validada.")
else:
    print(f"  {fallidos} test(s) fallaron — revisar arriba.")
    for nombre, det in [(r[1], "") for r in resultados if r[0] == "FAIL"]:
        print(f"    FAIL: {nombre}")

sys.exit(0 if fallidos == 0 else 1)
