"""Tests de entrega_cola.py -- la bandeja de entrega que corre dentro del bridge.

Lo que se cuida aca no es que suba: es que NO suba cuando no debe, y que el hilo
siga vivo pase lo que pase.

  - una carpeta que todavia se esta escribiendo no se empaqueta NUNCA (esa es la
    causa raiz de las sesiones duplicadas en produccion)
  - la sesion en curso tampoco, aunque el mtime la haga parecer quieta
  - un token malo detiene la bandeja entera en vez de hacer 12 x 4 intentos
  - dos dashes abiertos no suben la misma sesion dos veces (lockfile)
  - status() no hace I/O: viaja dentro del state que sale a 30 Hz

Todo contra un servidor HTTP falso en localhost. Nunca contra el de la escuela.
"""
import json
import gzip
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import entrega as E                                              # noqa: E402
import entrega_cola as C                                         # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TOTAL = [0, 0]


def _ok(nombre, cond, extra=""):
    _TOTAL[0 if cond else 1] += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {nombre} {extra}")
    return cond


# --- sesion sintetica (mismo molde que test_entrega.py) -------------------------
def _sesion(base, nombre, n_vueltas=3, muestras=1200):
    d = os.path.join(base, nombre)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "session.json"), "w", encoding="utf-8") as f:
        json.dump({"car": "GT4", "track": "Spielberg", "track_tr": "Spielberg",
                   "session": "practice"}, f)
    with open(os.path.join(d, "summary.jsonl"), "w", encoding="utf-8") as f:
        for i in range(n_vueltas):
            f.write(json.dumps({"lap": i + 1, "lap_time": 95.0 + i * 0.1,
                                "trace": f"L{i+1:03d}.csv.gz"}) + "\n")
    for i in range(n_vueltas):
        with gzip.open(os.path.join(d, f"L{i+1:03d}.csv.gz"), "wt", encoding="utf-8") as f:
            f.write("t,lap_dist,speed_kmh\n")
            for k in range(muestras):
                f.write(f"{k*0.02:.2f},{k*2.0:.1f},150.0\n")
    return d


def _envejecer(carpeta, segundos=3600):
    """Corre el mtime hacia atras: es lo que hace que el gate de quiescencia la
    considere cerrada, sin tener que esperar dos minutos en un test."""
    t = time.time() - segundos
    for n in os.listdir(carpeta):
        os.utime(os.path.join(carpeta, n), (t, t))
    os.utime(carpeta, (t, t))


# --- servidor falso -------------------------------------------------------------
class _Srv(BaseHTTPRequestHandler):
    recibido = []
    codigo = None                  # None = comportamiento normal (201 / 409)
    lock = threading.Lock()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        with _Srv.lock:
            cod = _Srv.codigo
            sid = self.headers.get("X-Sesion-Id", "")
            carp = self.headers.get("X-Sesion-Carpeta", "")
            if cod is None:
                dup = any(r["id"] == sid for r in _Srv.recibido)
                _Srv.recibido.append({"id": sid, "carpeta": carp})
                cod = 409 if dup else 201
            else:
                _Srv.recibido.append({"id": sid, "carpeta": carp})
        self.send_response(cod)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true,"id":"srv-1"}')

    def log_message(self, *a):
        pass


def _levantar():
    s = HTTPServer(("127.0.0.1", 0), _Srv)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def _esperar(cond, limite=6.0):
    fin = time.time() + limite
    while time.time() < fin:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _cola(base, cfg_path, **kw):
    """Cola con los relojes bajados: el comportamiento es el mismo, la espera no."""
    c = C.ColaEntrega(base, cfg_path=cfg_path, log=lambda m: None, **kw)
    c.periodo_s = 0.02
    c.quiescencia_s = 60          # las carpetas del test se envejecen a mano
    c.reencolar_s = 0.05
    c.espera_pista_s = 0.05
    c.chequeo_cfg_s = 0.05
    c.backoff_red_s = 600         # el de verdad: se comprueba, no se acorta
    c.reintentos_subida = 1
    c.timeout_subida_s = 5
    return c


def _cfg(base, url, auto=False, desde=None):
    p = os.path.join(base, "entrega.json")
    d = {"url": url, "token": "tok-1", "alumno": "Ana", "auto": auto}
    if desde:
        d["desde"] = desde
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f)
    return p


# --------------------------------------------------------------------------------
def test_status_sin_io(base):
    cfg_p = os.path.join(base, "no_hay_config.json")
    print("\ntest status():")
    c = _cola(base, cfg_p)
    st = c.status()
    _ok("status() tiene exactamente las claves del contrato",
        set(st) == {"estado", "auto", "pendientes", "listas", "subiendo", "ultima",
                    "invalidas", "msg"}, sorted(st))
    _ok("sin config: estado sin_config", st["estado"] == "sin_config", st["estado"])
    _ok("el msg es una frase para pantalla", isinstance(st["msg"], str) and st["msg"], st["msg"])

    # se llama 30 veces por segundo dentro del broadcast: un listdir ahi es inaceptable
    real_listdir, real_getmtime = os.listdir, os.path.getmtime
    def _bum(*a, **k):
        raise AssertionError("status() hizo I/O")
    os.listdir = _bum
    os.path.getmtime = _bum
    try:
        st2 = c.status()
        sin_io = True
    except AssertionError:
        st2, sin_io = None, False
    finally:
        os.listdir, os.path.getmtime = real_listdir, real_getmtime
    _ok("status() no hace NADA de I/O", sin_io)

    # tiene que ser una copia: si el dash muta lo que recibe, no puede corromper la cola
    st3 = c.status()
    st3["listas"].append("basura")
    st3["estado"] = "mentira"
    _ok("status() devuelve una copia, no el dict interno",
        c.status()["listas"] == [] and c.status()["estado"] == "sin_config", c.status())


def test_gate_quiescencia(base, url):
    print("\ntest gate de quiescencia:")
    d = _sesion(base, "FRESCA__GT4__practice__20260101_000001")
    _Srv.recibido = []
    _Srv.codigo = None
    c = _cola(base, _cfg(base, url))
    c.encolar(d)
    c.start()
    try:
        _ok("una carpeta recien escrita llega a `lista`",
            _esperar(lambda: c.status()["listas"]), c.status())
        c.entregar_ahora()
        time.sleep(0.5)
        _ok("carpeta con mtime fresco: NO se empaqueta ni se sube", not _Srv.recibido,
            _Srv.recibido)
        _ok("y queda esperando, no se marca como fallada",
            c.status()["estado"] == "listas", c.status())
        _envejecer(d)
        _ok("con la carpeta quieta si se entrega", _esperar(lambda: len(_Srv.recibido) == 1),
            _Srv.recibido)
    finally:
        c.stop()


def test_gate_sesion_actual(base, url):
    print("\ntest gate de la sesion en curso:")
    d = _sesion(base, "ENCURSO__GT4__practice__20260101_000002")
    _envejecer(d)                       # mtime viejo a proposito: el otro gate es el que manda
    _Srv.recibido = []
    _Srv.codigo = None
    c = _cola(base, _cfg(base, url), sess_dir_actual=lambda: d)
    c.encolar(d)
    c.start()
    try:
        _esperar(lambda: c.status()["listas"])
        c.entregar_ahora()
        time.sleep(0.5)
        _ok("la sesion EN CURSO no se sube nunca, aunque el mtime sea viejo",
            not _Srv.recibido, _Srv.recibido)
    finally:
        c.stop()
    # y en cuanto deja de ser la actual, se entrega
    _Srv.recibido = []
    c2 = _cola(base, _cfg(base, url))
    c2.encolar(d)
    c2.start()
    try:
        _esperar(lambda: c2.status()["listas"])
        c2.entregar_ahora()
        _ok("cuando ya no es la sesion en curso, se entrega",
            _esperar(lambda: len(_Srv.recibido) == 1), _Srv.recibido)
    finally:
        c2.stop()


def test_gate_pista(base, url):
    print("\ntest gate de pista:")
    d = _sesion(base, "PISTA__GT4__practice__20260101_000003")
    _envejecer(d)
    _Srv.recibido = []
    _Srv.codigo = None
    en_pista = [True]
    c = _cola(base, _cfg(base, url), en_pista=lambda: en_pista[0])
    c.encolar(d)
    c.start()
    try:
        time.sleep(0.5)
        _ok("con el jugador en pista no se hace NADA pesado (ni revisar)",
            not c.status()["listas"] and not _Srv.recibido, c.status())
        en_pista[0] = False
        _esperar(lambda: c.status()["listas"])
        c.entregar_ahora()
        _ok("al salir de pista, la bandeja avanza sola",
            _esperar(lambda: len(_Srv.recibido) == 1), _Srv.recibido)
    finally:
        c.stop()


def test_auto_y_boton(base, url):
    print("\ntest auto vs. boton:")
    d = _sesion(base, "AUTO__GT4__practice__20260101_000004")
    _envejecer(d)
    _Srv.recibido = []
    _Srv.codigo = None
    p = _cfg(base, url, auto=False)
    c = _cola(base, p)
    c.encolar(d)
    c.start()
    try:
        _esperar(lambda: c.status()["listas"])
        time.sleep(0.4)
        _ok("con auto apagado, una sesion lista NO se sube sola", not _Srv.recibido,
            _Srv.recibido)
        _ok("el dash ve la sesion lista con su resumen",
            c.status()["listas"] and "vuelta" in c.status()["listas"][0]["resumen"],
            c.status()["listas"])
        _ok("y ve cuanto pesa, para poder avisar",
            c.status()["listas"][0]["bytes"] > 0, c.status()["listas"])
        _ok("pendientes cuenta lo que falta entregar", c.status()["pendientes"] == 1,
            c.status()["pendientes"])
        c.entregar_ahora()
        _ok("el boton entrega aunque auto este apagado",
            _esperar(lambda: len(_Srv.recibido) == 1), _Srv.recibido)
        _ok("despues de entregar, la bandeja queda vacia",
            _esperar(lambda: c.status()["pendientes"] == 0), c.status())
        _ok("y se muestra la ultima entrega",
            c.status()["ultima"] and c.status()["ultima"]["ok"], c.status()["ultima"])
    finally:
        c.stop()

    # con auto prendido sube sola
    d2 = _sesion(base, "AUTO2__GT4__practice__20260101_000005")
    _envejecer(d2)
    _Srv.recibido = []
    c2 = _cola(base, _cfg(base, url, auto=True))
    c2.encolar(d2)
    c2.start()
    try:
        _ok("con auto prendido, se entrega sola", _esperar(lambda: len(_Srv.recibido) == 1),
            _Srv.recibido)
        _ok("status refleja auto", c2.status()["auto"] is True, c2.status())
    finally:
        c2.stop()

    # set_auto persiste en el archivo: es el switch que el alumno prende desde el dash
    p3 = _cfg(base, url, auto=False)
    c3 = _cola(base, p3)
    c3.set_auto(True)
    with open(p3, encoding="utf-8-sig") as f:
        guardado = json.load(f)
    _ok("set_auto persiste en entrega.json", guardado.get("auto") is True, guardado)
    _ok("set_auto no pierde el token ni la url",
        guardado.get("token") == "tok-1" and guardado.get("url") == url, guardado)


def test_registro_y_reinicio(base, url):
    print("\ntest idempotencia entre arranques:")
    d = _sesion(base, "REINICIO__GT4__practice__20260101_000006")
    _envejecer(d)
    _Srv.recibido = []
    _Srv.codigo = None
    p = _cfg(base, url, auto=True, desde="2020-01-01T00:00:00")
    c = _cola(base, p)
    c.start()
    try:
        _ok("el barrido de arranque encuentra la sesion que nadie encolo",
            _esperar(lambda: len(_Srv.recibido) == 1), _Srv.recibido)
        # el anotar pasa DESPUES de que el servidor contesta: hay que esperarlo
        _ok("queda anotada en el registro local",
            _esperar(lambda: "REINICIO__GT4__practice__20260101_000006" in E.registro()),
            E.registro())
        _ok("el registro guarda el estado, no solo la fecha",
            E.registro()["REINICIO__GT4__practice__20260101_000006"]["estado"] == "ok",
            E.registro())
    finally:
        c.stop()
    # segundo arranque sobre la MISMA carpeta: un solo POST en total
    c2 = _cola(base, p)
    c2.start()
    try:
        time.sleep(0.6)
        _ok("dos arranques seguidos sobre la misma carpeta = UN solo POST",
            len(_Srv.recibido) == 1, _Srv.recibido)
    finally:
        c2.stop()


def test_barrido_filtros(base, url):
    print("\ntest filtros del barrido:")
    sub = tempfile.mkdtemp(prefix="barrido_")
    try:
        vieja = _sesion(sub, "VIEJA__GT4__practice__20250101_000000")
        nueva = _sesion(sub, "NUEVA__GT4__practice__20260101_000007")
        _envejecer(vieja, 40 * 86400)          # mas alla de los 14 dias
        _envejecer(nueva, 3600)
        _Srv.recibido = []
        _Srv.codigo = None
        c = _cola(sub, _cfg(sub, url, auto=False, desde="2020-01-01T00:00:00"))
        c.start()
        try:
            _esperar(lambda: c.status()["listas"])
            time.sleep(0.4)
            carpetas = [x["carpeta"] for x in c.status()["listas"]]
            _ok("el barrido ignora lo mas viejo que 14 dias",
                carpetas == ["NUEVA__GT4__practice__20260101_000007"], carpetas)
        finally:
            c.stop()

        # `desde`: lo grabado antes de configurar es historial personal, no se toca
        _Srv.recibido = []
        c2 = _cola(sub, _cfg(sub, url, auto=False,
                             desde="2099-01-01T00:00:00"))
        c2.start()
        try:
            time.sleep(0.4)
            _ok("el barrido respeta `desde`: no sube el historial anterior a configurar",
                c2.status()["pendientes"] == 0, c2.status())
        finally:
            c2.stop()

        # tope duro: la primera pasada no puede encolar 384 carpetas
        _Srv.recibido = []
        for i in range(6):
            _envejecer(_sesion(sub, f"T{i}__GT4__practice__2026010{i}_000000", n_vueltas=1), 3600)
        c3 = _cola(sub, _cfg(sub, url, auto=False, desde="2020-01-01T00:00:00"))
        c3.tope_barrido = 3
        c3.start()
        try:
            time.sleep(0.8)
            _ok("el barrido tiene tope duro", c3.status()["pendientes"] <= 3, c3.status())
        finally:
            c3.stop()

        # Carpetas VACIAS mas nuevas que las reales no pueden comerse el tope. Medido
        # en pista: cambiar de auto en el garage rota la sesion por cada auto y deja
        # una carpeta sin vueltas por segundo; 19 de esas dejaron fuera, en silencio,
        # a las 10 sesiones reales de la noche.
        sub2 = tempfile.mkdtemp(prefix="vacias_")
        try:
            real = _sesion(sub2, "REAL__GT4__race__20260101_200000", n_vueltas=4)
            _envejecer(real, 7200)                      # la real es la MAS VIEJA
            for i in range(8):
                v = _sesion(sub2, f"GARAGE{i}__GT4__qualify__20260101_23000{i}", n_vueltas=0)
                _envejecer(v, 3600 - i)                 # todas mas nuevas que la real
            _Srv.recibido = []
            c4 = _cola(sub2, _cfg(sub2, url, auto=False, desde="2020-01-01T00:00:00"))
            c4.tope_barrido = 3
            c4.start()
            try:
                _esperar(lambda: c4.status()["listas"] or c4.status()["invalidas"])
                time.sleep(0.6)
                listas = [x["carpeta"] for x in c4.status()["listas"]]
                _ok("las carpetas sin vueltas no cuentan para el tope: la real entra igual",
                    listas == ["REAL__GT4__race__20260101_200000"], listas)
                _ok("y las vacias ni siquiera se encolan",
                    c4.status()["pendientes"] == 1 and not c4.status()["invalidas"],
                    (c4.status()["pendientes"], len(c4.status()["invalidas"])))
            finally:
                c4.stop()
        finally:
            shutil.rmtree(sub2, ignore_errors=True)
    finally:
        shutil.rmtree(sub, ignore_errors=True)


def test_respuestas(base, url):
    print("\ntest respuestas del servidor:")
    # 409: el servidor ya la tenia -> exito, y se anota igual
    d = _sesion(base, "DUP__GT4__practice__20260101_000008")
    _envejecer(d)
    _Srv.recibido = []
    _Srv.codigo = 409
    c = _cola(base, _cfg(base, url, auto=True))
    c.encolar(d)
    c.start()
    try:
        _ok("409 se trata como entregada, no como error",
            _esperar(lambda: c.status()["ultima"] and c.status()["ultima"]["ok"]),
            c.status())
        _ok("409 se anota en el registro como duplicado",
            _esperar(lambda: E.registro().get("DUP__GT4__practice__20260101_000008", {})
                     .get("estado") == "duplicado"),
            E.registro().get("DUP__GT4__practice__20260101_000008"))
    finally:
        c.stop()

    # 413: demasiado grande -> invalida, no se reintenta para siempre
    d2 = _sesion(base, "GRANDE__GT4__practice__20260101_000009")
    _envejecer(d2)
    _Srv.recibido = []
    _Srv.codigo = 413
    c2 = _cola(base, _cfg(base, url, auto=True))
    c2.encolar(d2)
    c2.start()
    try:
        _ok("413 deja la sesion como invalida con motivo visible",
            _esperar(lambda: c2.status()["invalidas"]), c2.status())
    finally:
        c2.stop()

    # 500: reintenta y luego se va al backoff propio de la cola (10 min, no 14 s)
    d3 = _sesion(base, "CAIDO__GT4__practice__20260101_000010")
    _envejecer(d3)
    _Srv.recibido = []
    _Srv.codigo = 500
    c3 = _cola(base, _cfg(base, url, auto=True))
    c3.encolar(d3)
    c3.start()
    try:
        _ok("servidor caido: la sesion vuelve a la bandeja, no se pierde",
            _esperar(lambda: c3.status()["ultima"] and not c3.status()["ultima"]["ok"]),
            c3.status())
        it = c3._items["CAIDO__GT4__practice__20260101_000010"]
        _ok("y espera el backoff largo de la cola, no los 14 s de subir()",
            it["proximo"] - time.time() > 500, it["proximo"] - time.time())
        _ok("el mensaje al alumno dice que se reintenta solo",
            "reintento" in (c3.status()["ultima"]["msg"] or "").lower(), c3.status()["ultima"])
    finally:
        c3.stop()
    _Srv.codigo = None


def test_token(base, url):
    print("\ntest token rechazado:")
    d1 = _sesion(base, "TOK1__GT4__practice__20260101_000011")
    d2 = _sesion(base, "TOK2__GT4__practice__20260101_000012")
    _envejecer(d1)
    _envejecer(d2)
    _Srv.recibido = []
    _Srv.codigo = 401
    p = _cfg(base, url, auto=True)
    c = _cola(base, p)
    c.encolar(d1)
    c.encolar(d2)
    c.start()
    try:
        _ok("401 detiene la bandeja entera con estado pegajoso `token`",
            _esperar(lambda: c.status()["estado"] == "token"), c.status())
        time.sleep(0.6)
        _ok("con el token malo NO se machaca al servidor (1 POST, no 2 x 4)",
            len(_Srv.recibido) == 1, _Srv.recibido)
        _ok("el mensaje apunta a la credencial",
            "credencial" in c.status()["msg"], c.status()["msg"])
        _ok("las sesiones siguen en la bandeja, no se marcan invalidas",
            c.status()["pendientes"] == 2 and not c.status()["invalidas"], c.status())
        # el alumno pega un token nuevo: la cola tiene que reanudarse sola
        _Srv.codigo = None
        _Srv.recibido = []
        time.sleep(0.05)
        _cfg(base, url, auto=True)                 # reescribe el archivo => cambia el mtime
        _ok("al cambiar entrega.json la bandeja se reanuda sola",
            _esperar(lambda: len(_Srv.recibido) == 2, 8.0), _Srv.recibido)
    finally:
        c.stop()
        _Srv.codigo = None


def test_lockfile(base, url):
    print("\ntest lockfile:")
    sub = tempfile.mkdtemp(prefix="lock_")
    try:
        d = _sesion(sub, "LOCK__GT4__practice__20260101_000013")
        _envejecer(d)
        _Srv.recibido = []
        _Srv.codigo = None
        p = _cfg(sub, url, auto=True)
        c1 = _cola(sub, p)
        c1.start()
        c2 = _cola(sub, p)
        c2.encolar(d)
        c2.start()
        try:
            _ok("el lockfile existe donde dice el contrato",
                os.path.exists(os.path.join(sub, ".entrega.lock")))
            if C.msvcrt is None:
                _ok("(fuera de Windows el lock es no-op: se salta)", True)
                _ok("(idem)", True)
            else:
                _ok("la segunda instancia queda inerte", c2._inerte, c2._inerte)
                _ok("y lo dice como estado off", c2.status()["estado"] == "off", c2.status())
            time.sleep(0.4)
            _ok("una instancia inerte no sube nada por su cuenta",
                len(_Srv.recibido) <= 1, _Srv.recibido)
        finally:
            c2.stop()
            c1.stop()
        # liberado el lock, una instancia nueva si arranca
        c3 = _cola(sub, p)
        c3.start()
        try:
            _ok("al soltar el lock, otra instancia lo toma", not c3._inerte)
        finally:
            c3.stop()
    finally:
        shutil.rmtree(sub, ignore_errors=True)


def test_robustez(base, url):
    print("\ntest robustez del hilo:")
    d1 = _sesion(base, "BOOM__GT4__practice__20260101_000014")
    d2 = _sesion(base, "SANA__GT4__practice__20260101_000015")
    _envejecer(d1)
    _envejecer(d2)
    _Srv.recibido = []
    _Srv.codigo = None
    real = E.revisar

    def _revisar_que_revienta(carpeta):
        if "BOOM" in carpeta:
            raise RuntimeError("boom")
        return real(carpeta)

    E.revisar = _revisar_que_revienta
    c = _cola(base, _cfg(base, url, auto=True))
    c.encolar(d1)
    c.encolar(d2)
    c.start()
    try:
        _ok("un item que revienta no mata el hilo: el siguiente igual se entrega",
            _esperar(lambda: any(r["carpeta"] == "SANA__GT4__practice__20260101_000015"
                                 for r in _Srv.recibido), 8.0), _Srv.recibido)
        _ok("y el hilo sigue vivo", c._hilo.is_alive())
    finally:
        E.revisar = real
        c.stop()

    # una sesion invalida se muestra con su motivo y no se reintenta en loop.
    # Raiz aparte: aca se afirma que NO llego nada, y el barrido de arranque
    # levantaria la BOOM de mas arriba (que quedo sin entregar) y ensuciaria el test.
    b2 = os.path.join(base, "invalida")
    os.makedirs(b2, exist_ok=True)
    d3 = _sesion(b2, "MALA__GT4__practice__20260101_000016", n_vueltas=0)
    _envejecer(d3)
    _Srv.recibido = []
    c2 = _cola(b2, _cfg(b2, url, auto=True))
    c2.encolar(d3)
    c2.start()
    try:
        _ok("una sesion sin vueltas queda invalida con motivo en castellano",
            _esperar(lambda: c2.status()["invalidas"]), c2.status())
        time.sleep(0.4)
        _ok("y no se sube igual", not _Srv.recibido, _Srv.recibido)
    finally:
        c2.stop()

    # carpeta que ya no esta: no puede tumbar nada
    b3 = os.path.join(base, "borrada")
    os.makedirs(b3, exist_ok=True)
    c3 = _cola(b3, _cfg(b3, url, auto=True))
    c3.encolar(os.path.join(b3, "NO_EXISTE__GT4__practice__20260101_000017"))
    c3.start()
    try:
        _ok("una carpeta borrada se marca invalida en vez de reventar",
            _esperar(lambda: c3.status()["invalidas"]), c3.status())
    finally:
        c3.stop()


def main():
    base = tempfile.mkdtemp(prefix="cola_")
    reg_original = E.REGISTRO
    E.REGISTRO = os.path.join(base, ".entregado.json")
    srv = _levantar()
    url = f"http://127.0.0.1:{srv.server_port}"
    try:
        # Cada test en su PROPIA raiz: el barrido de arranque mira toda la carpeta
        # base, asi que compartirla hace que un test se coma las sesiones que dejo
        # el anterior -- y el falso positivo se ve como bug del producto.
        for i, t in enumerate((test_status_sin_io, test_gate_quiescencia,
                               test_gate_sesion_actual, test_gate_pista,
                               test_auto_y_boton, test_registro_y_reinicio,
                               test_barrido_filtros, test_respuestas, test_token,
                               test_lockfile, test_robustez)):
            raiz = os.path.join(base, f"t{i}")
            os.makedirs(raiz, exist_ok=True)
            if t is test_status_sin_io:
                t(raiz)
            else:
                t(raiz, url)
    finally:
        srv.shutdown()
        E.REGISTRO = reg_original
        shutil.rmtree(base, ignore_errors=True)
    print(f"\ndone. {_TOTAL[0]} PASS · {_TOTAL[1]} FAIL")
    return 1 if _TOTAL[1] else 0


if __name__ == "__main__":
    sys.exit(main())
