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


def _sesion_con_boxes(tmp):
    """Una carrera minima como la deja el grabador: largada (S), una limpia (L), la
    vuelta de la parada y la out-lap (P, anunciadas en trace_boxes) y otra limpia."""
    import gzip
    import json
    d = os.path.join(tmp, "Pista__Auto__race__20261025_000000")
    os.makedirs(d)
    with open(os.path.join(d, "session.json"), "w", encoding="utf-8") as f:
        json.dump({"track": "Pista", "track_variation": "Test", "car": "Auto",
                   "car_class": "Clase", "session": "race", "track_length_m": 800.0}, f)
    t = _traza()
    cols = list(t)

    def traza(nombre):
        with gzip.open(os.path.join(d, nombre), "wt", encoding="utf-8") as f:
            f.write(",".join(cols) + "\n")
            for k in range(len(t["t"])):
                f.write(",".join(str(t[c][k]) for c in cols) + "\n")
        return nombre

    tl = [
        {"type": "lap", "lap": 1, "lap_time": 75.0, "kind": "start", "pit": False, "out": False,
         "invalid": False, "uid": None, "trace": traza("S001_75.000s.csv.gz")},
        {"type": "lap", "lap": 2, "lap_time": 70.0, "kind": "flying", "pit": False, "out": False,
         "invalid": False, "uid": 1, "trace": traza("L002_70.000s.csv.gz")},
        {"type": "lap", "lap": 3, "lap_time": 74.0, "kind": "pit", "pit": True, "out": False,
         "invalid": False, "uid": None, "trace": None, "trace_boxes": traza("P003_74.000s.csv.gz")},
        {"type": "lap", "lap": 4, "lap_time": 95.0, "kind": "out", "pit": False, "out": True,
         "invalid": False, "uid": None, "trace": None, "trace_boxes": traza("P004_95.000s.csv.gz")},
        {"type": "lap", "lap": 5, "lap_time": 69.5, "kind": "flying", "pit": False, "out": False,
         "invalid": False, "uid": 2, "trace": traza("L005_69.500s.csv.gz")},
    ]
    with open(os.path.join(d, "timeline.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in tl) + "\n")
    with open(os.path.join(d, "summary.jsonl"), "w", encoding="utf-8") as f:
        for uid, lap, lt, tr in ((1, 2, 70.0, "L002_70.000s.csv.gz"), (2, 5, 69.5, "L005_69.500s.csv.gz")):
            f.write(json.dumps({"uid": uid, "lap": lap, "lap_time": lt, "trace": tr,
                                "ambient_t": 22.0 + uid, "track_t": 30.0, "rain": 0.0,
                                "sectors": [lt / 3] * 3}) + "\n")
    return d


def test_carrera_con_boxes_sale_completa():
    """La regresion: una carrera de 43 vueltas con 3 paradas se exportaba con 35,
    porque las vueltas de entrada y salida de boxes no tenian traza."""
    with tempfile.TemporaryDirectory() as tmp:
        d = _sesion_con_boxes(tmp)
        ruta, n = S.exportar(d, os.path.join(tmp, "c.srt"))
        s = S.leer(ruta)
        check(n == 5 and len(s["vueltas"]) == 5, f"salen las 5 vueltas, boxes incluidas ({n})")
        tiempos = [round(v["tiempo"], 1) for v in s["vueltas"]]
        check(tiempos == [75.0, 70.0, 74.0, 95.0, 69.5], f"en el orden de la carrera {tiempos}")
        nulas = [any(x > 0.5 for x in S.como_dict(s, i)["lap_time_invalid"]) for i in range(5)]
        check(nulas == [False, False, True, True, False],
              f"las de boxes van como nulas y las demas no {nulas}")
        amb = [S.como_dict(s, i)["ambient_temp"][0] for i in range(5)]
        check(amb[2] == amb[3] == 23.0, f"las de boxes heredan el ambiente de la anterior, no 0.0 {amb}")
        ruta3, n3 = S.exportar(d, os.path.join(tmp, "r.srt"), max_vueltas=3)
        t3 = sorted(round(v["tiempo"], 1) for v in S.leer(ruta3)["vueltas"])
        check(t3 == [69.5, 70.0, 75.0], f"--vueltas N nunca elige una de boxes {t3}")


def test_entrega_no_sube_trazas_de_boxes():
    """Las P### existen para el .srt; a la escuela no viajan ni cuentan para el tope."""
    import entrega as E
    with tempfile.TemporaryDirectory() as tmp:
        d = _sesion_con_boxes(tmp)
        nombres = [n for n, _ in E._archivos(d)]
        check(not [n for n in nombres if n.startswith("P")] and "L002_70.000s.csv.gz" in nombres,
              f"el zip lleva las de pista y ninguna P### {nombres}")
        sin_p = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d) if not f.startswith("P"))
        check(E._bytes_carpeta(d) == sin_p, "el tope de MB mide lo que viaja")


if __name__ == "__main__":
    for nombre, fn in list(globals().items()):
        if nombre.startswith("test_") and callable(fn):
            print(f"\n-- {nombre}")
            fn()
    print(f"\n{'OK' if not FALLOS else f'{FALLOS} FALLO(S)'}")
    sys.exit(1 if FALLOS else 0)
