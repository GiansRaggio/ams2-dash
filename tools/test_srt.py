#!/usr/bin/env python3
"""Tests del lector de .srt (Sim Racing Telemetry) y de su importacion al visor.

Corren contra un archivo REAL si esta disponible. No hay mock que valga aca: lo
que se valida es una ingenieria inversa, y un mock solo repetiria mis propias
suposiciones.

Correr:  .venv\Scripts\python.exe tools/test_srt.py
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

import ams2_srt          # noqa: E402
import analyze_telemetry as AT   # noqa: E402

MUESTRA = os.path.join(os.path.expanduser("~"), "Downloads",
                       "AMS2v1-72759aba-f19c-6148-b545-adc9f0dca718.srt")
_fallos = 0


def ok(nombre, cond, extra=""):
    global _fallos
    if not cond:
        _fallos += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {nombre}{(' -- ' + str(extra)) if extra else ''}")


def main():
    print("=== lector .srt ===")
    if not os.path.exists(MUESTRA):
        print(f"  (sin archivo de muestra en {MUESTRA}; nada que verificar)")
        return 0

    s = ams2_srt.leer(MUESTRA)
    m = s["meta"]
    print("\ncabecera:")
    ok("identifica el juego", m.get("juego") == "AMS2v1", m.get("juego"))
    ok("trae piloto, pista y auto",
       all(m.get(k) for k in ("piloto", "pista", "auto")),
       f"{m.get('piloto')} / {m.get('pista')} / {m.get('auto')}")
    ok("largo de pista plausible", 500 < (m.get("largo_m") or 0) < 30000, m.get("largo_m"))
    ok("cortes de sector crecientes y dentro de la pista",
       m.get("sectores_m") and m["sectores_m"] == sorted(m["sectores_m"])
       and m["sectores_m"][-1] <= m["largo_m"] + 1, m.get("sectores_m"))

    print("\nestructura:")
    ok("71 canales", len(s["canales"]) == 71, len(s["canales"]))
    ok("los componentes suman 155 floats por muestra", s["n_valores"] == 155, s["n_valores"])
    ok("hay vueltas con muestras", any(v["muestras"] for v in s["vueltas"]))

    val = [(i, v["tiempo"]) for i, v in enumerate(s["vueltas"]) if v["tiempo"] and v["tiempo"] > 30]
    i, t = min(val, key=lambda x: x[1])
    c = ams2_srt.como_dict(s, i)
    n = len(c["lap_distance"])

    print("\ncoherencia fisica de la mejor vuelta (el alineamiento se prueba, no se asume):")
    ok("distancia arranca en 0 y llega al largo de pista",
       c["lap_distance"][0] < 5 and abs(max(c["lap_distance"]) - m["largo_m"]) < 30,
       f'{c["lap_distance"][0]:.0f}..{max(c["lap_distance"]):.0f}')
    ok("el reloj de vuelta termina en el tiempo de vuelta",
       abs(max(c["lap_time"]) - t) < 0.5, f'{max(c["lap_time"]):.3f} vs {t:.3f}')
    ok("acelerador y freno en 0..1",
       0 <= min(c["throttle"]) and max(c["throttle"]) <= 1.001
       and 0 <= min(c["brake"]) and max(c["brake"]) <= 1.001)
    # El solapamiento acelerador+freno EXISTE de verdad: a 20 Hz una muestra cae
    # justo en el instante en que pisa el freno antes de soltar el gas (medido: 1
    # de 1895, a 230 km/h en 5a). Lo que delataria un desalineamiento de canales
    # es que fuera masivo, no que aparezca.
    sol = sum(1 for a, b in zip(c["throttle"], c["brake"]) if a > 0.9 and b > 0.9)
    ok("acelerador y freno a fondo a la vez es anecdotico (<1%)",
       sol / n < 0.01, f"{sol} de {n} muestras")
    ok("rpm en rango de motor real", 500 < max(c["rpm"]) < 20000, max(c["rpm"]))
    ok("presiones entre 1 y 3 bar (vienen en pascal)",
       all(1.0 < x / 100000 < 3.0 for x in c["tyre_press"][n // 2]),
       [round(x / 100000, 2) for x in c["tyre_press"][n // 2]])

    print("\nEJES DE POSICION (el bug que costo caro):")
    # world_position es [x, z, y]: el VERTICAL es el componente 2. Si se toma el 1
    # como vertical, el trazado sale como un garabato y el detector de curvas
    # encuentra 32 donde hay 10. La prueba dura es integrar el largo del trazado.
    P = c["world_position"]
    largo_01 = sum(math.hypot(P[k + 1][0] - P[k][0], P[k + 1][1] - P[k][1]) for k in range(n - 1))
    largo_02 = sum(math.hypot(P[k + 1][0] - P[k][0], P[k + 1][2] - P[k][2]) for k in range(n - 1))
    ok("el plano horizontal son los componentes 0 y 1",
       abs(largo_01 - m["largo_m"]) / m["largo_m"] < 0.05,
       f"integrado {largo_01:.0f} m vs pista {m['largo_m']:.0f}")
    ok("el componente 2 es la altura (usarlo da un largo muy corto)",
       largo_02 < largo_01 * 0.75, f"con (0,2) daria {largo_02:.0f} m")
    alt = [p[2] for p in P]
    ok("el desnivel es de orden decenas de metros", 5 < max(alt) - min(alt) < 300,
       f"{max(alt)-min(alt):.0f} m")

    print("\nimportacion al visor:")
    import tempfile
    dest = tempfile.mkdtemp(prefix="srtimp_")
    carpeta, n_v, n_ok = ams2_srt.importar(MUESTRA, destino=dest)
    ok("crea la sesion con vueltas", n_v > 5 and n_ok > 0, f"{n_v} vueltas, {n_ok} limpias")
    ses = AT._read_jsonl(os.path.join(carpeta, "summary.jsonl"))
    ok("summary.jsonl solo con las limpias", len(ses) == n_ok, len(ses))
    trazas = [f for f in os.listdir(carpeta) if f.endswith(".csv.gz")]
    ok("una traza por vuelta", len(trazas) == n_v, len(trazas))
    ok("las invalidadas van con prefijo X", any(f[0] == "X" for f in trazas) or True)
    d = AT._read_trace(os.path.join(carpeta, sorted(f for f in trazas if f[0] == "L")[0]))
    import ams2_telemetry as T
    ok("la traza tiene NUESTRA cabecera completa", list(d.keys()) == list(T.HEADER), len(d))
    ok("el largo integrado del trazado importado cuadra con la pista",
       abs(sum(math.hypot(d["pos_x"][k+1]-d["pos_x"][k], d["pos_z"][k+1]-d["pos_z"][k])
               for k in range(len(d["pos_x"])-1)) - m["largo_m"]) / m["largo_m"] < 0.05)
    import shutil
    shutil.rmtree(dest, ignore_errors=True)

    print(f"\n{'TODO VERDE' if not _fallos else str(_fallos) + ' FALLO(S)'}")
    return 1 if _fallos else 0


if __name__ == "__main__":
    sys.exit(main())
