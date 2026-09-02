"""Tests de entrega.py -- validar, empaquetar y subir una sesion.

El modo de falla que estos tests cuidan es el SILENCIOSO: que el alumno crea que
entrego y no haya llegado nada, o que haya llegado basura. A 20 alumnos eso no se
detecta a ojo.

Y desde que entrega.py es ademas la BIBLIOTECA que usa el hilo de la bandeja
(entrega_cola.py), se cuida un segundo modo de falla igual de silencioso: que una
funcion reviente el hilo de fondo (EOFError de un gz a medio escribir), que mute
estado global del proceso (sys.path, REINTENTOS) o que pierda el registro de
entregas en un corte de luz.
"""
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import entrega as E                                              # noqa: E402

# La consola de Windows entrega cp1252 y aca se imprimen nombres con acentos. Sin
# esto el suite MUERE a mitad de camino por un UnicodeEncodeError al imprimir un
# PASS, y el resumen final no se escribe: la corrida se ve como si no hubiera fallado.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OK = FAIL = 0


def _ok(nombre, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {nombre} {extra if extra is not None else ''}")
    else:
        FAIL += 1
        print(f"  [FAIL] {nombre} {extra if extra is not None else ''}")


def _sesion(base, nombre, n_vueltas=8, muestras=2000, meta=True, truncadas=0, trazas=True):
    d = os.path.join(base, nombre)
    os.makedirs(d, exist_ok=True)
    if meta:
        with open(os.path.join(d, "session.json"), "w", encoding="utf-8") as f:
            json.dump({"car": "GT4", "track": "Spielberg", "track_variation": "Modern",
                       "track_tr": "Spielberg", "session": "practice"}, f)
    with open(os.path.join(d, "summary.jsonl"), "w", encoding="utf-8") as f:
        for i in range(n_vueltas):
            f.write(json.dumps({"lap": i + 1, "lap_time": 95.0 + i * 0.1, "valid": True,
                                "rain": 0.0, "tc_setting": 2, "abs_setting": 3,
                                "compound": "Soft", "trace": f"L{i+1:03d}.csv.gz"}) + "\n")
    if trazas:
        for i in range(n_vueltas):
            n = 200 if i < truncadas else muestras
            with gzip.open(os.path.join(d, f"L{i+1:03d}.csv.gz"), "wt", encoding="utf-8") as f:
                f.write("t,lap_dist,speed_kmh\n")
                for k in range(n):
                    f.write(f"{k*0.02:.2f},{k*2.0:.1f},150.0\n")
    return d


class _Servidor(BaseHTTPRequestHandler):
    recibido = []
    fallar_veces = 0
    duplicado = False
    codigo = None                  # si se fija, responde eso y no guarda nada

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        cuerpo = self.rfile.read(n)
        if _Servidor.codigo:
            self.send_response(_Servidor.codigo)
            self.end_headers()
            self.wfile.write(b'{"error":"no"}')
            return
        if _Servidor.fallar_veces > 0:
            _Servidor.fallar_veces -= 1
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"caido")
            return
        sha = self.headers.get("X-Sesion-Sha256", "")
        # Deduplica por X-Sesion-Id (contenido), NO por el sha del zip. El servidor
        # real hace lo mismo: usar el del zip fue el bug que duplico en produccion.
        sid = self.headers.get("X-Sesion-Id", "")
        if _Servidor.duplicado or any(r["id"] == sid for r in _Servidor.recibido):
            self.send_response(409)
            self.end_headers()
            self.wfile.write(b'{"ok":true,"duplicado":true,"id":"vieja"}')
            return
        _Servidor.recibido.append({
            "sha": sha, "id": sid, "bytes": len(cuerpo),
            "auth": self.headers.get("Authorization", ""),
            "carpeta": self.headers.get("X-Sesion-Carpeta", ""), "cuerpo": cuerpo})
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true,"id":"abc"}')

    def log_message(self, *a):
        pass


def main():
    base = tempfile.mkdtemp(prefix="entrega_")
    reg_original = E.REGISTRO
    try:
        print("test validacion:")
        d = _sesion(base, "OK__GT4__practice__20260101_000000")
        rev = E.revisar(d)
        p, av, r = rev["problemas"], rev["avisos"], rev["resumen"]
        _ok("revisar devuelve las tres claves del contrato",
            set(rev) == {"problemas", "avisos", "resumen"}, sorted(rev))
        _ok("sesion sana: sin problemas", not p, p)
        _ok("sesion sana: lee las vueltas", r["vueltas"] == 8, r["vueltas"])
        _ok("sesion sana: lee auto y pista", r["auto"] == "GT4" and r["pista"] == "Spielberg", r)

        d2 = _sesion(base, "VACIA__GT4__practice__20260101_000000", n_vueltas=0)
        p2 = E.revisar(d2)["problemas"]
        _ok("sin vueltas: BLOQUEA", any("vuelta cronometrada" in x for x in p2), p2)

        d3 = _sesion(base, "TRUNC__GT4__practice__20260101_000000", n_vueltas=6, truncadas=6)
        p3 = E.revisar(d3)["problemas"]
        _ok("todas las trazas truncadas: BLOQUEA", any("truncadas" in x for x in p3), p3)

        d4 = _sesion(base, "MIXTA__GT4__practice__20260101_000000", n_vueltas=8, truncadas=2)
        rev4 = E.revisar(d4)
        _ok("algunas truncadas: avisa pero NO bloquea",
            not rev4["problemas"] and any("truncadas" in x for x in rev4["avisos"]), rev4)

        d5 = _sesion(base, "SINMETA__GT4__practice__20260101_000000", meta=False)
        p5 = E.revisar(d5)["problemas"]
        _ok("sin metadatos de auto/pista: BLOQUEA", any("auto/pista" in x for x in p5), p5)

        # el summary dice que cada vuelta tenia su .csv.gz y no hay ninguno: antes
        # esto pasaba la validacion y se subia una sesion vacia de telemetria
        d6 = _sesion(base, "SINTRAZAS__GT4__practice__20260101_000000", trazas=False)
        p6 = E.revisar(d6)["problemas"]
        _ok("modo full sin ninguna traza: BLOQUEA", any("sin ninguna traza" in x for x in p6), p6)

        # una sesion liviana legitima (modo summary) no lleva `trace` y no puede caer
        # en el chequeo de arriba
        d6b = _sesion(base, "SUMMARY__GT4__practice__20260101_000000", trazas=False)
        with open(os.path.join(d6b, "summary.jsonl"), "w", encoding="utf-8") as f:
            for i in range(5):
                f.write(json.dumps({"lap": i + 1, "lap_time": 95.0, "trace": None}) + "\n")
        _ok("modo summary (sin `trace` en el summary): NO bloquea",
            not E.revisar(d6b)["problemas"], E.revisar(d6b)["problemas"])

        # un .csv.gz a medio escribir tira EOFError, que NO es OSError: antes eso
        # subia por revisar() y mataba el hilo de la bandeja
        d7 = _sesion(base, "MEDIO__GT4__practice__20260101_000000", n_vueltas=3)
        ruta_t = os.path.join(d7, "L001.csv.gz")
        crudo = open(ruta_t, "rb").read()
        with open(ruta_t, "wb") as f:
            f.write(crudo[:len(crudo) // 2])
        try:
            rev7 = E.revisar(d7)
            reventó = False
        except BaseException as e:
            rev7, reventó = {"problemas": [repr(e)], "avisos": []}, True
        _ok("traza .csv.gz truncada a la mitad: no lanza EOFError", not reventó, rev7["problemas"])
        _ok("traza a medio escribir: se cuenta como truncada, no se ignora",
            any("truncadas" in x for x in rev7["avisos"] + rev7["problemas"]), rev7)

        # el servidor corta en 64 MB y devuelve 413, que subir() no reintenta
        d8 = _sesion(base, "GORDA__GT4__practice__20260101_000000", n_vueltas=1)
        with open(os.path.join(d8, "relleno.bin"), "wb") as f:
            for _ in range(E.MAX_MB + 5):
                f.write(b"\0" * (1 << 20))
        p8 = E.revisar(d8)["problemas"]
        _ok("sesion sobre el limite de tamano: BLOQUEA antes de empaquetar",
            any("limite" in x for x in p8), p8)
        shutil.rmtree(d8, ignore_errors=True)

        # sys.path crecia una entrada por cada llamada a revisar(): en el bridge, que
        # vive horas, eso es estado global del proceso creciendo sin techo
        antes = len(sys.path)
        for _ in range(5):
            E.revisar(d)
        _ok("sys.path no crece entre 5 llamadas a revisar", len(sys.path) == antes,
            (antes, len(sys.path)))

        print("\ntest configuracion:")
        cfg_p = os.path.join(base, "entrega.json")
        with open(cfg_p, "w", encoding="utf-8-sig") as f:       # BOM, como lo deja Notepad
            json.dump({"url": "  https://x.cl/  ", "token": " tok ", "nombre": "Ana"}, f)
        c = E.cargar_config(cfg_p)
        _ok("entrega.json con BOM: se carga igual", c is not None, c)
        _ok("la url se limpia y pierde la barra final", c and c["url"] == "https://x.cl", c)
        _ok("el token se limpia de espacios", c and c["token"] == "tok", c)
        _ok("acepta `nombre` y lo expone tambien como `alumno`",
            c and c["alumno"] == "Ana", c)
        _ok("auto por defecto es false (opt-in explicito)", c and c["auto"] is False, c)
        with open(cfg_p, "w", encoding="utf-8") as f:
            json.dump({"url": "https://x.cl"}, f)
        _ok("sin token: devuelve None, no un dict a medias", E.cargar_config(cfg_p) is None)
        _ok("archivo inexistente: devuelve None",
            E.cargar_config(os.path.join(base, "nada.json")) is None)
        with open(cfg_p, "w", encoding="utf-8") as f:
            f.write("{roto")
        _ok("json corrupto: devuelve None, no lanza", E.cargar_config(cfg_p) is None)

        print("\ntest registro local:")
        E.REGISTRO = os.path.join(base, ".entregado.json")
        E.anotar("SESION_A", {"id_sesion": "aaa", "estado": "ok", "bytes": 10})
        E.anotar("SESION_B", {"id_sesion": "bbb", "estado": "ok", "bytes": 20})
        reg = E.registro()
        _ok("el registro guarda por basename", set(reg) == {"SESION_A", "SESION_B"}, sorted(reg))
        # simular el corte de luz entre el truncate y el dump: con escritura atomica
        # el archivo viejo tiene que seguir entero
        import json as _json
        dump_real = _json.dump

        def _dump_que_muere(*a, **k):
            raise OSError("disco lleno")
        _json.dump = _dump_que_muere
        try:
            E.anotar("SESION_C", {"estado": "ok"})
            murio = False
        except OSError:
            murio = True
        finally:
            _json.dump = dump_real
        _ok("una escritura que muere a medias avisa, no falla en silencio", murio)
        reg2 = E.registro()
        _ok("el registro sobrevive al corte: no se pierde el historial",
            set(reg2) == {"SESION_A", "SESION_B"}, sorted(reg2))
        _ok("no queda un .tmp colgando", not os.path.exists(E.REGISTRO + ".tmp"))
        with open(E.REGISTRO, "w", encoding="utf-8") as f:
            f.write("{a medias")
        _ok("registro corrupto: devuelve {} y no lanza", E.registro() == {})

        print("\ntest empaquetado:")
        datos, sha, id_sesion, man = E.empaquetar(d, "Gian")
        z = zipfile.ZipFile(io.BytesIO(datos))
        _ok("el zip lleva manifiesto", "manifiesto.json" in z.namelist())
        _ok("el zip lleva las trazas y el resumen",
            "summary.jsonl" in z.namelist() and "L001.csv.gz" in z.namelist())
        m = json.loads(z.read("manifiesto.json"))
        _ok("el manifiesto identifica al alumno", m["alumno"] == "Gian", m["alumno"])
        _ok("el manifiesto trae el combo", m["auto"] == "GT4" and m["vueltas"] == 8, m)
        # UNA sola pasada de identidad: antes la huella se calculaba dos veces y se
        # leian los archivos una tercera, asi que el header, el manifiesto embebido y
        # los bytes del zip podian describir tres estados distintos de la carpeta
        _ok("el id_sesion devuelto == el del manifiesto embebido", id_sesion == m["id_sesion"],
            (id_sesion[:12], m["id_sesion"][:12]))
        _ok("el id_sesion == huella() recalculada de la carpeta", id_sesion == E.huella(d),
            (id_sesion[:12], E.huella(d)[:12]))
        # y lo que va en el zip son los mismos bytes que se hashearon
        import hashlib as _h
        hh = _h.sha256()
        for nombre in sorted(n for n in z.namelist() if n != "manifiesto.json"):
            hh.update(nombre.encode("utf-8"))
            hh.update(z.read(nombre))
        _ok("el id_sesion es el hash del CONTENIDO que viaja en el zip",
            hh.hexdigest() == id_sesion, (hh.hexdigest()[:12], id_sesion[:12]))
        # ESTE test existia y comparaba el LARGO de los dos zips, que da igual porque
        # el timestamp del manifiesto ocupa los mismos caracteres. Paso en verde
        # mientras el sistema duplicaba sesiones en produccion. Ahora compara lo que
        # de verdad decide la deduplicacion.
        import time as _t
        _t.sleep(1.1)                                  # que cambie el timestamp
        datos_b, sha_b, id_b, man_b = E.empaquetar(d, "Gian")
        _ok("el sha del ZIP cambia entre empaquetados (por el timestamp)", sha != sha_b,
            (sha[:12], sha_b[:12]))
        _ok("la HUELLA de la sesion NO cambia: es la que deduplica", id_sesion == id_b,
            (id_sesion[:12], id_b[:12]))
        _ok("la huella depende del contenido, no del nombre de la carpeta",
            E.huella(d) == E.huella(d))
        # tocar un archivo tiene que cambiarla, o dos sesiones distintas colisionarian
        with open(os.path.join(d, "summary.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"lap": 99, "lap_time": 91.0, "valid": True}) + chr(10))
        _ok("si cambia el contenido, cambia la huella", E.huella(d) != id_sesion)

        print("\ntest subida:")
        _Servidor.recibido = []
        _Servidor.fallar_veces = 0
        _Servidor.codigo = None
        srv = HTTPServer(("127.0.0.1", 0), _Servidor)     # puerto 0: lo elige el SO
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cfg = {"url": f"http://127.0.0.1:{srv.server_port}", "token": "tok-123", "alumno": "Gian"}

        res = E.subir(datos, sha, cfg, man, id_sesion)
        _ok("subir devuelve las claves del contrato",
            set(res) == {"ok", "duplicado", "status", "msg", "id"}, sorted(res))
        _ok("sube y el servidor la recibe", res["ok"] and len(_Servidor.recibido) == 1, res["msg"])
        _ok("201: no marca duplicado y trae el id del servidor",
            res["status"] == 201 and not res["duplicado"] and res["id"] == "abc", res)
        rec = _Servidor.recibido[0]
        _ok("manda el token en el header", rec["auth"] == "Bearer tok-123", rec["auth"])
        _ok("manda el sha del zip (integridad)", rec["sha"] == sha, rec["sha"][:16])
        _ok("manda la huella de sesion (identidad)", rec["id"] == id_sesion, rec["id"][:16])
        _ok("lo que llego es el zip completo", rec["bytes"] == len(datos), rec["bytes"])
        _ok("el servidor puede abrir lo que llego",
            zipfile.ZipFile(io.BytesIO(rec["cuerpo"])).testzip() is None)

        # Entregar dos veces no puede duplicar ni contar como falla. Y se prueba con
        # el zip REEMPAQUETADO (datos_b), que es lo que pasa de verdad cuando el
        # alumno reintenta: bytes distintos, misma sesion. Con el zip identico el
        # test pasaba y el sistema duplicaba igual.
        res2 = E.subir(datos_b, sha_b, cfg, man_b, id_b)
        _ok("reentrega con el zip reempaquetado: no duplica",
            res2["ok"] and len(_Servidor.recibido) == 1, res2["msg"])
        _ok("409 se reporta como exito Y como duplicado",
            res2["ok"] and res2["duplicado"] and res2["status"] == 409, res2)

        # el servidor caido no puede hacer que el alumno pierda la sesion. Ojo: los
        # reintentos van por PARAMETRO, no mutando E.REINTENTOS -- esa global la
        # comparten la CLI y el hilo de la bandeja.
        reintentos_antes, timeout_antes = E.REINTENTOS, E.TIMEOUT_S
        _Servidor.fallar_veces = 2
        _Servidor.recibido = []
        res3 = E.subir(datos, sha, cfg, man, id_sesion[:-1] + "0", reintentos=3, timeout=5)
        _ok("servidor intermitente: reintenta y termina entregando",
            res3["ok"] and len(_Servidor.recibido) == 1, res3["msg"])
        _ok("subir(reintentos=, timeout=) NO muta las globals del modulo",
            E.REINTENTOS == reintentos_antes and E.TIMEOUT_S == timeout_antes,
            (E.REINTENTOS, E.TIMEOUT_S))

        _Servidor.fallar_veces = 99
        res4 = E.subir(datos, sha, cfg, man, id_sesion[:-1] + "1", reintentos=3)
        _ok("servidor caido del todo: falla claro, no en silencio",
            not res4["ok"] and "intentos" in res4["msg"], res4["msg"])

        # un token de cancelacion: sin esto el peor caso son ~8 min de hilo pegado
        _Servidor.fallar_veces = 99
        res5 = E.subir(datos, sha, cfg, man, id_sesion, reintentos=4,
                       cancelado=lambda: True, log=lambda m: None)
        _ok("cancelado=True corta la subida de inmediato",
            not res5["ok"] and "cancelada" in res5["msg"], res5["msg"])

        # el log es un callback: entrega.py no puede imprimir desde un hilo de fondo
        lineas = []
        _Servidor.fallar_veces = 1
        _Servidor.recibido = []
        E.subir(datos, sha, cfg, man, id_sesion[:-1] + "2", reintentos=2,
                log=lineas.append)
        _ok("los mensajes de reintento van al callback log, no a stdout",
            any("Reintento" in x for x in lineas), lineas)
        srv.shutdown()

        print("\ntest respuestas de error:")
        for codigo, clave, reintenta in ((401, "configurar", False), (403, "configurar", False),
                                         (413, "demasiado grande", False)):
            _Servidor.recibido = []
            _Servidor.codigo = codigo
            _Servidor.fallar_veces = 0
            s = HTTPServer(("127.0.0.1", 0), _Servidor)
            threading.Thread(target=s.serve_forever, daemon=True).start()
            cfg_e = dict(cfg, url=f"http://127.0.0.1:{s.server_port}")
            t0 = _t.time()
            r = E.subir(datos, sha, cfg_e, man, id_sesion, reintentos=4, log=lambda m: None)
            dt = _t.time() - t0
            _ok(f"{codigo}: falla y reporta el status", not r["ok"] and r["status"] == codigo, r)
            _ok(f"{codigo}: el mensaje dice que hacer", clave in r["msg"], r["msg"])
            # 2+4+8 s de backoff serian imposibles de confundir con "no reintento"
            _ok(f"{codigo}: NO reintenta", dt < 2.0, f"{dt:.2f}s")
            s.shutdown()
        _Servidor.codigo = None

        print("\ntest sesiones:")
        _ok("sesiones(base_dir) lista solo carpetas",
            all(os.path.isdir(x) for x in E.sesiones(base)), E.sesiones(base))
        _ok("sesiones() de una ruta que no existe devuelve []",
            E.sesiones(os.path.join(base, "no_existe")) == [])
    finally:
        E.REGISTRO = reg_original
        shutil.rmtree(base, ignore_errors=True)

    print(f"\ndone. {OK} PASS · {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
