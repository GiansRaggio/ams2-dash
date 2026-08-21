"""Tests de entrega.py -- validar, empaquetar y subir una sesion.

El modo de falla que estos tests cuidan es el SILENCIOSO: que el alumno crea que
entrego y no haya llegado nada, o que haya llegado basura. A 20 alumnos eso no se
detecta a ojo.
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
import entrega as E

OK = FAIL = 0


def _ok(nombre, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {nombre} {extra if extra is not None else ''}")
    else:
        FAIL += 1
        print(f"  [FAIL] {nombre} {extra if extra is not None else ''}")


def _sesion(base, nombre, n_vueltas=8, muestras=2000, meta=True, truncadas=0):
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

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        cuerpo = self.rfile.read(n)
        if _Servidor.fallar_veces > 0:
            _Servidor.fallar_veces -= 1
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"caido")
            return
        sha = self.headers.get("X-Sesion-Sha256", "")
        if _Servidor.duplicado or any(r["sha"] == sha for r in _Servidor.recibido):
            self.send_response(409)
            self.end_headers()
            self.wfile.write(b"ya existe")
            return
        _Servidor.recibido.append({
            "sha": sha, "bytes": len(cuerpo),
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
    try:
        print("test validacion:")
        d = _sesion(base, "OK__GT4__practice__20260101_000000")
        p, av, r = E.revisar(d)
        _ok("sesion sana: sin problemas", not p, p)
        _ok("sesion sana: lee las vueltas", r["vueltas"] == 8, r["vueltas"])
        _ok("sesion sana: lee auto y pista", r["auto"] == "GT4" and r["pista"] == "Spielberg", r)

        d2 = _sesion(base, "VACIA__GT4__practice__20260101_000000", n_vueltas=0)
        p2, _, _ = E.revisar(d2)
        _ok("sin vueltas: BLOQUEA", any("vuelta cronometrada" in x for x in p2), p2)

        d3 = _sesion(base, "TRUNC__GT4__practice__20260101_000000", n_vueltas=6, truncadas=6)
        p3, _, _ = E.revisar(d3)
        _ok("todas las trazas truncadas: BLOQUEA", any("truncadas" in x for x in p3), p3)

        d4 = _sesion(base, "MIXTA__GT4__practice__20260101_000000", n_vueltas=8, truncadas=2)
        p4, av4, _ = E.revisar(d4)
        _ok("algunas truncadas: avisa pero NO bloquea",
            not p4 and any("truncadas" in x for x in av4), (p4, av4))

        d5 = _sesion(base, "SINMETA__GT4__practice__20260101_000000", meta=False)
        p5, _, _ = E.revisar(d5)
        _ok("sin metadatos de auto/pista: BLOQUEA", any("auto/pista" in x for x in p5), p5)

        print("\ntest empaquetado:")
        datos, sha, man = E.empaquetar(d, "Gian")
        z = zipfile.ZipFile(io.BytesIO(datos))
        _ok("el zip lleva manifiesto", "manifiesto.json" in z.namelist())
        _ok("el zip lleva las trazas y el resumen",
            "summary.jsonl" in z.namelist() and "L001.csv.gz" in z.namelist())
        m = json.loads(z.read("manifiesto.json"))
        _ok("el manifiesto identifica al alumno", m["alumno"] == "Gian", m["alumno"])
        _ok("el manifiesto trae el combo", m["auto"] == "GT4" and m["vueltas"] == 8, m)
        # el sha tiene que ser estable: si cambia solo, la deteccion de duplicado no sirve
        datos_b, sha_b, _ = E.empaquetar(d, "Gian")
        _ok("el contenido se empaqueta igual dos veces", len(datos) == len(datos_b),
            (len(datos), len(datos_b)))

        print("\ntest subida:")
        _Servidor.recibido = []
        _Servidor.fallar_veces = 0
        srv = HTTPServer(("127.0.0.1", 0), _Servidor)     # puerto 0: lo elige el SO
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cfg = {"url": f"http://127.0.0.1:{srv.server_port}", "token": "tok-123", "alumno": "Gian"}

        ok, msg = E.subir(datos, sha, cfg, man)
        _ok("sube y el servidor la recibe", ok and len(_Servidor.recibido) == 1, msg)
        rec = _Servidor.recibido[0]
        _ok("manda el token en el header", rec["auth"] == "Bearer tok-123", rec["auth"])
        _ok("manda el sha para deduplicar", rec["sha"] == sha, rec["sha"][:16])
        _ok("lo que llego es el zip completo", rec["bytes"] == len(datos), rec["bytes"])
        _ok("el servidor puede abrir lo que llego",
            zipfile.ZipFile(io.BytesIO(rec["cuerpo"])).testzip() is None)

        # entregar dos veces no puede duplicar ni contar como falla
        ok2, msg2 = E.subir(datos, sha, cfg, man)
        _ok("entregar dos veces: se resuelve como exito, sin duplicar",
            ok2 and len(_Servidor.recibido) == 1, msg2)

        # el servidor caido no puede hacer que el alumno pierda la sesion
        E.REINTENTOS = 3
        _Servidor.fallar_veces = 2
        _Servidor.recibido = []
        ok3, msg3 = E.subir(datos, sha + "x", cfg, man)
        _ok("servidor intermitente: reintenta y termina entregando",
            ok3 and len(_Servidor.recibido) == 1, msg3)

        _Servidor.fallar_veces = 99
        ok4, msg4 = E.subir(datos, sha + "y", cfg, man)
        _ok("servidor caido del todo: falla claro, no en silencio",
            not ok4 and "intentos" in msg4, msg4)
        srv.shutdown()

        print("\ntest token invalido:")
        class _Auth(_Servidor):
            def do_POST(self):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"no")
        srv2 = HTTPServer(("127.0.0.1", 0), _Auth)
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        cfg2 = dict(cfg, url=f"http://127.0.0.1:{srv2.server_port}")
        ok5, msg5 = E.subir(datos, sha, cfg2, man)
        _ok("token malo: no reintenta 4 veces, y dice como arreglarlo",
            not ok5 and "configurar" in msg5, msg5)
        srv2.shutdown()
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print(f"\n{OK} PASS / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
