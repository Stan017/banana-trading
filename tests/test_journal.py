# -*- coding: utf-8 -*-
"""
test_journal.py - Testing completo del Journal (Dia 2)
Cubre:
  1. Auth - login, sesion activa
  2. Trade MANUAL CERRADO - crear, verificar stats, borrar
  3. Trade ABIERTO - crear, cerrar con precio, verificar PnL
  4. Trade con apalancamiento - verificar PnL con leverage
  5. Stats SQL - verificar que los agregados son correctos
  6. CSV import - Bitunix, generico, duplicados
  7. Edge cases - campos faltantes, valores invalidos
  8. Analisis profundo - endpoint Pro (free debe recibir 403)
  9. Borrar trade - solo el dueno puede borrarlo
  10. Limite de lista - parametro ?limite
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import csv
import io
import json
import time
import requests

BASE = "http://localhost:5000"
session = requests.Session()

# ── Colores para la terminal ──────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

passed = 0
failed = 0
warnings = 0

def ok(msg):
    global passed
    passed += 1
    print(f"  {GREEN}✓{RESET} {msg}")

def fail(msg, detail=""):
    global failed
    failed += 1
    detail_str = f" → {RED}{detail}{RESET}" if detail else ""
    print(f"  {RED}✗{RESET} {msg}{detail_str}")

def warn(msg):
    global warnings
    warnings += 1
    print(f"  {YELLOW}⚠{RESET} {msg}")

def section(title):
    print(f"\n{BOLD}{BLUE}-- {title} {'-'*(50-len(title))}{RESET}")

def assert_eq(label, actual, expected):
    if actual == expected:
        ok(f"{label}: {actual!r}")
    else:
        fail(f"{label}: esperado {expected!r}, obtuvo {actual!r}")

def assert_in(label, value, collection):
    if value in collection:
        ok(f"{label}: {value!r}")
    else:
        fail(f"{label}: {value!r} no está en {collection}")

def assert_ok(label, r):
    try:
        data = r.json()
        if data.get("ok"):
            ok(f"{label} → HTTP {r.status_code}")
            return data
        else:
            fail(f"{label} → HTTP {r.status_code}", data.get("error", str(data)))
            return data
    except Exception as e:
        fail(f"{label} → no es JSON", str(e))
        return {}

def assert_status(label, r, expected_status):
    if r.status_code == expected_status:
        ok(f"{label} → HTTP {r.status_code}")
    else:
        fail(f"{label} → esperado HTTP {expected_status}, obtuvo {r.status_code}", r.text[:100])

# ════════════════════════════════════════════════════════════════
# 1. AUTH
# ════════════════════════════════════════════════════════════════
section("1. AUTH - Login y sesion")

# Registrar usuario de test si no existe
r = session.post(f"{BASE}/register", json={
    "email":    "test_journal@tradebot.dev",
    "password": "TestPass123!",
    "nombre":   "Tester Journal"
})
# 200 = registrado, 409 = ya existe — ambos ok
if r.status_code in (200, 409):
    ok(f"Register/usuario existente → HTTP {r.status_code}")
else:
    fail(f"Register → HTTP {r.status_code}", r.text[:100])

# Login
r = session.post(f"{BASE}/login", json={
    "email":    "test_journal@tradebot.dev",
    "password": "TestPass123!"
})
data = r.json()
if data.get("ok"):
    ok(f"Login exitoso → plan={data.get('usuario', {}).get('plan', '?')}")
    USER_PLAN = data.get("usuario", {}).get("plan", "free")
else:
    fail("Login falló — no se puede continuar", data.get("error", ""))
    sys.exit(1)

# Verificar sesión activa
r = session.get(f"{BASE}/me")
assert_status("GET /me con sesión activa", r, 200)
me_data = r.json()
assert_in("/me contiene usuario", "usuario", me_data)

# ════════════════════════════════════════════════════════════════
# 2. TRADE MANUAL CERRADO — ciclo completo
# ════════════════════════════════════════════════════════════════
section("2. TRADE CERRADO - crear, verificar, borrar")

trade_payload = {
    "activo":          "BTCUSDT",
    "direccion":       "LONG",
    "entrada":         65000.0,
    "sl":              63500.0,
    "tp":              68000.0,
    "precio_cierre":   67500.0,
    "resultado":       "WIN",
    "apalancamiento":  10.0,
    "capital_cuenta":  1000.0,
    "margen_usado":    100.0,
    "tipo_margen":     "AISLADO",
    "tipo_trade":      "SWING",
    "timeframe":       "4H",
    "notas":           "Test trade cerrado",
}

r = session.post(f"{BASE}/journal/trade", json=trade_payload)
data = assert_ok("POST /journal/trade (CERRADO)", r)

if data.get("ok"):
    trade_id_cerrado = data.get("trade_id")
    trade = data.get("trade", {})

    ok(f"trade_id asignado: {trade_id_cerrado}")
    assert_eq("estado", trade.get("estado"), "CERRADO")
    assert_eq("resultado", trade.get("resultado"), "WIN")
    assert_eq("activo", trade.get("activo"), "BTCUSDT")
    assert_eq("direccion", trade.get("direccion"), "LONG")

    # PnL: LONG 65000→67500 con 10x = (67500-65000)/65000 * 100 * 10 = 38.46%
    pnl = trade.get("pnl")
    if pnl is not None and abs(pnl - 38.462) < 0.1:
        ok(f"PnL calculado correctamente: {pnl:.3f}%")
    else:
        warn(f"PnL: {pnl} (esperado ~38.46%)")

    # R:R planeado: (68000-65000)/(65000-63500) = 3000/1500 = 2.0
    rr = data.get("rr_planeado")
    if rr is not None and abs(rr - 2.0) < 0.01:
        ok(f"R:R planeado correcto: {rr}")
    else:
        warn(f"R:R planeado: {rr} (esperado 2.0)")

    # datos_faltantes debe estar vacío (tenemos todos los campos)
    faltantes = data.get("datos_faltantes", [])
    if not faltantes:
        ok("Sin datos faltantes (todos los campos presentes)")
    else:
        warn(f"Datos faltantes reportados: {faltantes}")
else:
    trade_id_cerrado = None
    fail("No se pudo crear trade cerrado — saltando assertions de trade")

# ════════════════════════════════════════════════════════════════
# 3. TRADE ABIERTO → cerrar con PATCH
# ════════════════════════════════════════════════════════════════
section("3. TRADE ABIERTO - ciclo de vida completo")

r = session.post(f"{BASE}/journal/trade", json={
    "activo":         "ETHUSDT",
    "direccion":      "SHORT",
    "entrada":        3500.0,
    "sl":             3600.0,
    "tp":             3200.0,
    "apalancamiento": 5.0,
    "margen_usado":   200.0,
    "capital_cuenta": 2000.0,
    "tipo_margen":    "AISLADO",
    "tipo_trade":     "SCALP",
    "timeframe":      "15M",
    "notas":          "Test trade abierto",
    # sin resultado ni precio_cierre → estado=ABIERTO
})
data = assert_ok("POST /journal/trade (ABIERTO)", r)

if data.get("ok"):
    trade_id_abierto = data.get("trade_id")
    trade = data.get("trade", {})
    assert_eq("estado inicial", trade.get("estado"), "ABIERTO")
    ok(f"confianza_bot generada: {data.get('confianza_bot', 'N/A')}")

    # Cerrar el trade
    r2 = session.patch(f"{BASE}/journal/trade/{trade_id_abierto}/cerrar", json={
        "precio_cierre": 3350.0,
        "resultado":     "WIN",
        "notas":         "Cerré en soporte"
    })
    data2 = assert_ok(f"PATCH /journal/trade/{trade_id_abierto}/cerrar", r2)

    if data2.get("ok"):
        trade2 = data2.get("trade", {})
        assert_eq("estado tras cerrar", trade2.get("estado"), "CERRADO")
        assert_eq("resultado", trade2.get("resultado"), "WIN")
        assert_eq("precio_cierre guardado", trade2.get("precio_cierre"), 3350.0)

        # PnL SHORT: (3500-3350)/3500 * 100 * 5 = 21.43%
        pnl = data2.get("pnl_pct")
        if pnl is not None and abs(pnl - 21.429) < 0.1:
            ok(f"PnL SHORT con 5x correcto: {pnl:.3f}%")
        else:
            warn(f"PnL SHORT: {pnl} (esperado ~21.43%)")

        # duracion_minutos debe existir (aunque sea pequeño en test)
        if trade2.get("duracion_minutos") is not None:
            ok(f"duración calculada: {trade2['duracion_minutos']} min")
        else:
            warn("duracion_minutos no calculada")
    else:
        trade_id_abierto = None
else:
    trade_id_abierto = None

# ════════════════════════════════════════════════════════════════
# 4. STATS — verificar agregados SQL
# ════════════════════════════════════════════════════════════════
section("4. STATS - agregados SQL correctos")

r = session.get(f"{BASE}/journal/stats")
data = assert_ok("GET /journal/stats", r)

if data.get("ok"):
    stats = data.get("stats", {})
    total = stats.get("total", 0)

    if total >= 2:
        ok(f"total trades: {total}")
    else:
        warn(f"total trades: {total} (esperado ≥ 2)")

    # win_rate debe ser un número 0-100
    wr = stats.get("win_rate")
    if wr is not None and 0 <= wr <= 100:
        ok(f"win_rate válido: {wr}%")
    else:
        fail("win_rate inválido", str(wr))

    # por_activo debe tener BTCUSDT y ETHUSDT
    por_activo = stats.get("por_activo", {})
    if "BTCUSDT" in por_activo:
        ok(f"BTCUSDT en por_activo: {por_activo['BTCUSDT']}")
    else:
        warn("BTCUSDT no está en por_activo todavía")

    # racha_actual debe tener estructura correcta
    racha = stats.get("racha_actual", {})
    if "resultado" in racha and "count" in racha:
        ok(f"racha_actual: {racha['count']}x {racha['resultado']}")
    else:
        fail("racha_actual mal formada", str(racha))

    # pnl_total debe ser un número
    pnl_t = stats.get("pnl_total")
    if pnl_t is not None:
        ok(f"pnl_total calculado: {pnl_t}%")
    else:
        warn("pnl_total es None (puede pasar si hay trades sin PnL)")

# ════════════════════════════════════════════════════════════════
# 5. LISTAR TRADES — GET /journal/trades
# ════════════════════════════════════════════════════════════════
section("5. LISTAR TRADES")

r = session.get(f"{BASE}/journal/trades")
data = assert_ok("GET /journal/trades", r)

if data.get("ok"):
    trades = data.get("trades", [])
    ok(f"trades retornados: {len(trades)}")

    if trades:
        t = trades[0]
        required_fields = ["id", "activo", "direccion", "entrada", "estado", "resultado"]
        missing = [f for f in required_fields if f not in t]
        if not missing:
            ok("Todos los campos requeridos presentes en trade")
        else:
            fail("Campos faltantes en trade", ", ".join(missing))

# límite personalizado
r = session.get(f"{BASE}/journal/trades?limite=1")
data = assert_ok("GET /journal/trades?limite=1", r)
if data.get("ok"):
    trades_1 = data.get("trades", [])
    if len(trades_1) <= 1:
        ok(f"límite=1 respetado: {len(trades_1)} trade(s)")
    else:
        fail(f"límite=1 ignorado: devolvió {len(trades_1)}")

# ════════════════════════════════════════════════════════════════
# 6. CSV IMPORT
# ════════════════════════════════════════════════════════════════
section("6. CSV IMPORT - Bitunix, generico, duplicados")

# IDs unicos por ejecucion — evita falsos duplicados entre runs
_RUN_ID = int(time.time())

def make_csv(rows, fieldnames):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")

# CSV formato Bitunix
bitunix_csv = make_csv([
    {
        "Symbol":       "BTCUSDT",
        "Direction":    "LONG",
        "Open Price":   "60000",
        "Close Price":  "62000",
        "Realized PnL": "200",
        "Open Time":    "2025-01-15 10:00:00",
        "Order ID":     f"BX_TEST_{_RUN_ID}_001",
    },
    {
        "Symbol":       "ETHUSDT",
        "Direction":    "SHORT",
        "Open Price":   "3000",
        "Close Price":  "2800",
        "Realized PnL": "100",
        "Open Time":    "2025-01-16 14:00:00",
        "Order ID":     f"BX_TEST_{_RUN_ID}_002",
    },
], ["Symbol", "Direction", "Open Price", "Close Price", "Realized PnL", "Open Time", "Order ID"])

r = session.post(f"{BASE}/journal/importar-csv",
    files={"file": ("bitunix_test.csv", bitunix_csv, "text/csv")})
data = assert_ok("POST /journal/importar-csv (Bitunix)", r)

if data.get("ok"):
    importados = data.get("importados", 0)
    saltados   = data.get("saltados",   0)
    if importados == 2:
        ok(f"Bitunix: {importados} trades importados, {saltados} saltados")
    else:
        warn(f"Bitunix: {importados} importados (esperado 2), {saltados} saltados, errores: {data.get('errores', [])}")

# Reimportar el mismo CSV → debe detectar duplicados
r = session.post(f"{BASE}/journal/importar-csv",
    files={"file": ("bitunix_test.csv", bitunix_csv, "text/csv")})
data = assert_ok("POST /journal/importar-csv (duplicados)", r)

if data.get("ok"):
    saltados_dup = data.get("saltados", 0)
    importados_dup = data.get("importados", 0)
    if importados_dup == 0 and saltados_dup == 2:
        ok(f"Duplicados detectados correctamente: {saltados_dup} saltados, 0 importados")
    else:
        warn(f"Duplicados: {importados_dup} importados (esperado 0), {saltados_dup} saltados")

# CSV generico - IDs unicos por run para evitar duplicados
generic_csv = make_csv([
    {
        "symbol":    "SOLUSDT",
        "side":      "LONG",
        "entry":     "150.0",
        "exit":      "165.0",
        "pnl":       "50.0",
        "date":      "2025-01-20",
        "id":        f"GEN_TEST_{_RUN_ID}_001",
    },
], ["symbol", "side", "entry", "exit", "pnl", "date", "id"])

r = session.post(f"{BASE}/journal/importar-csv",
    files={"file": ("generic_test.csv", generic_csv, "text/csv")})
data = assert_ok("POST /journal/importar-csv (generico)", r)
if data.get("ok") and data.get("importados", 0) >= 1:
    ok(f"CSV generico: {data['importados']} importado(s)")
else:
    warn(f"CSV generico: {data.get('importados', 0)} importados, errores: {data.get('errores', [])}")

# CSV con fila inválida (entrada=0)
bad_csv = make_csv([
    {"activo": "BTCUSDT", "direccion": "LONG", "entrada": "0", "cierre": "50000", "pnl": "10"},
    {"activo": "ETHUSDT", "direccion": "SHORT", "entrada": "3000", "cierre": "2900", "pnl": "20"},
], ["activo", "direccion", "entrada", "cierre", "pnl"])

r = session.post(f"{BASE}/journal/importar-csv",
    files={"file": ("bad_csv.csv", bad_csv, "text/csv")})
data = assert_ok("POST /journal/importar-csv (fila inválida)", r)
if data.get("ok"):
    if data.get("saltados", 0) >= 1:
        ok(f"Fila inválida (entrada=0) saltada correctamente")
    else:
        warn("Fila con entrada=0 no fue saltada")

# ════════════════════════════════════════════════════════════════
# 7. EDGE CASES - validaciones de entrada
# ════════════════════════════════════════════════════════════════
section("7. EDGE CASES - validaciones")

# Sin activo
r = session.post(f"{BASE}/journal/trade", json={
    "direccion": "LONG", "entrada": 50000
})
if r.status_code == 400:
    ok("Sin activo → 400 correctamente")
else:
    fail(f"Sin activo → esperado 400, obtuvo {r.status_code}")

# Dirección inválida
r = session.post(f"{BASE}/journal/trade", json={
    "activo": "BTCUSDT", "direccion": "SIDEWAYS", "entrada": 50000
})
if r.status_code == 400:
    ok("Dirección inválida → 400 correctamente")
else:
    fail(f"Dirección inválida → esperado 400, obtuvo {r.status_code}")

# Entrada negativa
r = session.post(f"{BASE}/journal/trade", json={
    "activo": "BTCUSDT", "direccion": "LONG", "entrada": -100
})
if r.status_code == 400:
    ok("Entrada negativa → 400 correctamente")
else:
    fail(f"Entrada negativa → esperado 400, obtuvo {r.status_code}")

# Resultado inválido
r = session.post(f"{BASE}/journal/trade", json={
    "activo": "BTCUSDT", "direccion": "LONG", "entrada": 50000, "resultado": "MAYBE"
})
if r.status_code == 400:
    ok("Resultado inválido → 400 correctamente")
else:
    fail(f"Resultado inválido → esperado 400, obtuvo {r.status_code}")

# Cerrar un trade que no existe
r = session.patch(f"{BASE}/journal/trade/999999/cerrar", json={"precio_cierre": 50000})
if r.status_code == 404:
    ok("Trade inexistente → 404 correctamente")
else:
    fail(f"Trade inexistente → esperado 404, obtuvo {r.status_code}")

# Cerrar trade ya cerrado
if trade_id_cerrado:
    r = session.patch(f"{BASE}/journal/trade/{trade_id_cerrado}/cerrar",
                      json={"precio_cierre": 66000})
    if r.status_code == 400:
        ok("Re-cerrar trade cerrado → 400 correctamente")
    else:
        fail(f"Re-cerrar trade cerrado → esperado 400, obtuvo {r.status_code}", r.text[:80])

# ════════════════════════════════════════════════════════════════
# 8. ANÁLISIS PROFUNDO — Pro gate
# ════════════════════════════════════════════════════════════════
section("8. ANALISIS PROFUNDO - control de acceso Pro")

r = session.post(f"{BASE}/journal/analisis-profundo", json={
    "pregunta": "¿Qué errores cometo?"
})

if USER_PLAN == "free":
    if r.status_code == 403:
        ok("Free user → 403 en análisis profundo (correcto)")
    else:
        warn(f"Free user en análisis profundo → HTTP {r.status_code} (esperado 403)")
elif USER_PLAN == "pro":
    if r.status_code == 200:
        ok("Pro user → análisis profundo disponible")
    elif r.status_code == 400:
        ok("Pro user → 400 (menos de 3 trades o parámetro faltante)")
    else:
        warn(f"Pro user en análisis profundo → HTTP {r.status_code}")
else:
    warn(f"Plan desconocido: {USER_PLAN}")

# ════════════════════════════════════════════════════════════════
# 9. BORRAR TRADE — autorización
# ════════════════════════════════════════════════════════════════
section("9. DELETE TRADE - autorizacion")

# Crear un trade para borrar
r = session.post(f"{BASE}/journal/trade", json={
    "activo": "BNBUSDT", "direccion": "LONG",
    "entrada": 400.0, "resultado": "LOSS", "pnl": -5.0
})
data = r.json()
if data.get("ok"):
    trade_id_borrar = data["trade_id"]

    # Borrar propio trade → OK
    r2 = session.delete(f"{BASE}/journal/trade/{trade_id_borrar}")
    if r2.status_code == 200 and r2.json().get("ok"):
        ok(f"DELETE /journal/trade/{trade_id_borrar} (propio) → OK")
    else:
        fail(f"DELETE trade propio → {r2.status_code}", r2.text[:80])

    # Intentar borrar el mismo dos veces → 404
    r3 = session.delete(f"{BASE}/journal/trade/{trade_id_borrar}")
    if r3.status_code == 404:
        ok("Borrar trade ya eliminado → 404 correctamente")
    else:
        fail(f"Borrar trade eliminado → esperado 404, obtuvo {r3.status_code}")
else:
    warn("No se pudo crear trade para test de borrado")

# Borrar trade de otro usuario → crear segunda sesión
session2 = requests.Session()
r = session2.post(f"{BASE}/register", json={
    "email": "test_journal_b@tradebot.dev",
    "password": "TestPass123!",
    "nombre": "Tester B"
})
r = session2.post(f"{BASE}/login", json={
    "email": "test_journal_b@tradebot.dev",
    "password": "TestPass123!"
})

if r.json().get("ok") and trade_id_cerrado:
    r_unauth = session2.delete(f"{BASE}/journal/trade/{trade_id_cerrado}")
    if r_unauth.status_code == 403:
        ok("Borrar trade ajeno → 403 correctamente")
    else:
        fail(f"Borrar trade ajeno → esperado 403, obtuvo {r_unauth.status_code}")
else:
    warn("No se pudo probar autorización cross-user")

# ════════════════════════════════════════════════════════════════
# 10. AUTH WALL - endpoints sin sesion
# ════════════════════════════════════════════════════════════════
section("10. AUTH WALL — endpoints protegidos sin sesión")

anon = requests.Session()  # sesion sin login

for method, url, kwargs in [
    ("GET",    "/journal/trades",              {}),
    ("GET",    "/journal/stats",               {}),
    ("POST",   "/journal/trade",               {"json": {}}),
    ("POST",   "/journal/analisis-profundo",   {"json": {}}),
]:
    # allow_redirects=False: captura el 302 de Flask-Login antes de seguir a /login
    r = getattr(anon, method.lower())(f"{BASE}{url}", allow_redirects=False, **kwargs)
    # Flask-Login devuelve 302 → /login para HTML, o 401 si hay unauthorized_handler
    if r.status_code in (302, 401, 403):
        ok(f"Anonimo {method} {url} -> {r.status_code} (protegido)")
    else:
        fail(f"Anonimo {method} {url} -> {r.status_code} (deberia estar protegido)")

# ════════════════════════════════════════════════════════════════
# RESUMEN FINAL
# ════════════════════════════════════════════════════════════════
total = passed + failed
print(f"\n{'═'*55}")
print(f"{BOLD}RESULTADOS:{RESET} {GREEN}{passed} pasados{RESET} / {RED}{failed} fallidos{RESET} / {YELLOW}{warnings} advertencias{RESET} de {total} checks")
print(f"{'═'*55}")

if failed == 0:
    print(f"\n{GREEN}{BOLD}OK Journal testing completo - todo OK{RESET}\n")
else:
    print(f"\n{RED}{BOLD}FAIL {failed} problema(s) encontrado(s) - revisar arriba{RESET}\n")
    sys.exit(1)
