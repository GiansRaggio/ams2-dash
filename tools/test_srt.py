"""Tests del puente .srt (Sim Racing Telemetry): escritura -> lectura.

Sin corpus: las vueltas son sinteticas. Lo que se protege es lo que se rompio
sin que nadie lo notara -- que una vuelta anulada llegue al otro piloto
marcada como anulada -- mas la ida y vuelta basica del formato.

    .venv\\Scripts\\python.exe tools\\test_srt.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ams2_srt as S      # noqa: E402

FALLOS = 0


def check(cond, msg):
    global FALLOS
    print(("[PASS] " if cond else "[FAIL] ") + msg)
    if not cond:
        FALLOS += 1


def _traza(n=40, largo=800.0):
    """Una vuelta minima: distancia, tiempo y pedales. Lo demas queda en 0.0."""
    return {
        "lap_dist": [largo * k / (n - 1) for k in range(n)],
        "t": [0.05 * k for k in range(n)],
        "throttle": [1.0] * n, "brake": [0.0] * n,
        "pos_x": [float(k) for k in range(n)], "pos_z": [0.0] * n,
    }


def _exportar(tmp, vueltas_spec):
    """vueltas_spec: lista de (tiempo, valida, sec_validos|None)."""
    vueltas = []
    for tiempo, valida, sv in vueltas_spec:
        d = _traza()
        fila = S._constructor(d, len(d["lap_dist"]), invalida=not valida)
        vueltas.append({"tiempo": tiempo, "sectores": [tiempo / 3] * 3,
                        "sec_validos": sv if sv is not None else [valida] * 3,
                        "muestras": [fila(k) for k in range(len(d["lap_dist"]))]})
    meta = {"juego": "AMS2v1", "build": "test", "piloto": "test",
            "pista": "Pista (Test)", "largo_m": 800.0,
            "sectores_m": [800 / 3, 1600 / 3, 800.0], "auto": "Auto (Clase)"}
    ruta = os.path.join(tmp, "t.srt")
    S.escribir(ruta, meta, vueltas)
    return ruta


def test_ida_y_vuelta():
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _exportar(tmp, [(70.5, True, None), (69.9, False, None)])
        s = S.leer(ruta)
        check(len(s["vueltas"]) == 2, "se leen las 2 vueltas escritas")
        check(abs(s["vueltas"][0]["tiempo"] - 70.5) < 1e-3, "el tiempo de vuelta sobrevive la ida y vuelta")
        c = S.como_dict(s, 0)
        check(len(c["lap_distance"]) == 40, "40 muestras por vuelta")
        check(abs(c["lap_distance"][-1] - 800.0) < 1e-3, "la distancia llega al largo de pista")
        check(s["meta"].get("pista") == "Pista (Test)", "el nombre de pista sobrevive")


def test_vuelta_nula_marcada():
    """La regresion: `lap_time_invalid` salia en 0.0 para todas las vueltas, y la
    app del otro piloto (y nuestro importar) leen ESE canal para tachar."""
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _exportar(tmp, [(70.5, True, None), (69.9, False, None)])
        s = S.leer(ruta)
        limpia = S.como_dict(s, 0)["lap_time_invalid"]
        nula = S.como_dict(s, 1)["lap_time_invalid"]
        check(all(x < 0.5 for x in limpia), "la vuelta limpia no lleva marca de nula")
        check(all(x > 0.5 for x in nula), "la vuelta anulada lleva la marca en TODAS las muestras")
        # el mismo criterio que usa importar()
        check(any(x > 0.5 for x in nula) and not any(x > 0.5 for x in limpia),
              "importar() la clasificaria como X (nula) y la otra como L")


def test_indice_sectores():
    """El indice `lapl` lleva un byte de validez por sector; se escribe lo que
    dice sectors.jsonl, no [valida]*3."""
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _exportar(tmp, [(69.9, False, [True, False, True])])
        with open(ruta, "rb") as f:
            b = f.read()
        i = b.find(b"lap ")
        check(i > 0, "el indice de vueltas existe")
        # chunk 'lap ': 4 tag + 4 largo + 3*u32 + 4*f32 + 3 bytes de validez
        flags = b[i + 8 + 12 + 16: i + 8 + 12 + 16 + 3]
        check(flags == bytes([1, 0, 1]), f"validez por sector escrita tal cual (S2 nulo): {list(flags)}")


def test_orden_mas_rapidas():
    """--vueltas N: las limpias van primero aunque una nula sea mas rapida."""
    vs = [{"tiempo": 68.0, "valida": False}, {"tiempo": 70.0, "valida": True},
          {"tiempo": 69.0, "valida": True}]
    orden = sorted(vs, key=lambda v: (not v.get("valida", True), v["tiempo"]))
    check([v["tiempo"] for v in orden] == [69.0, 70.0, 68.0],
          "las N mas rapidas ordenan limpias primero, la nula al final")


if __name__ == "__main__":
    for nombre, fn in list(globals().items()):
        if nombre.startswith("test_") and callable(fn):
            print(f"\n-- {nombre}")
            fn()
    print(f"\n{'OK' if not FALLOS else f'{FALLOS} FALLO(S)'}")
    sys.exit(1 if FALLOS else 0)
