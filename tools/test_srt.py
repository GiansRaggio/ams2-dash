#!/usr/bin/env python3
r"""Tests del lector de .srt (Sim Racing Telemetry) y de su importacion al visor.

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
    inval = [f for f in trazas if f[0] == "X"]
    res_uids = {r.get("uid") for r in AT._read_jsonl(os.path.join(carpeta, "summary.jsonl"))}
    ok("las trazas X son exactamente las que NO estan en el resumen",
       len(inval) == n_v - len(res_uids), f"{len(inval)} X, {n_v - len(res_uids)} fuera del resumen")
    d = AT._read_trace(os.path.join(carpeta, sorted(f for f in trazas if f[0] == "L")[0]))
    import ams2_telemetry as T
    ok("la traza tiene NUESTRA cabecera completa", list(d.keys()) == list(T.HEADER), len(d))
    ok("el largo integrado del trazado importado cuadra con la pista",
       abs(sum(math.hypot(d["pos_x"][k+1]-d["pos_x"][k], d["pos_z"][k+1]-d["pos_z"][k])
               for k in range(len(d["pos_x"])-1)) - m["largo_m"]) / m["largo_m"] < 0.05)
    import shutil
    shutil.rmtree(dest, ignore_errors=True)

    print("\nEJES DE CUERPO (el bug que Fable encontro):")
    # SRT ordena sus vectores de cuerpo [longitudinal, lateral, vertical] y los
    # nuestros van [lateral, vertical, longitudinal]. Mapear por indice identico
    # deja la G de FRENADO en el canal LATERAL, y como analyze_telemetry decide
    # con accel_x que rueda carga cada curva, el veredicto sale invertido. No se
    # ve raro: posicion, pedales y temperaturas siguen bien. Estas assertions
    # miran la HUELLA FISICA, que es lo unico que lo delata.
    def _corr(u, v):
        mu = sum(u) / len(u); mv = sum(v) / len(v)
        du = sum((x - mu) ** 2 for x in u) ** 0.5
        dv = sum((x - mv) ** 2 for x in v) ** 0.5
        return sum((x - mu) * (y - mv) for x, y in zip(u, v)) / (du * dv) if du and dv else 0.0

    import gzip as _gz, csv as _csv, tempfile as _tf, shutil as _sh
    dimp = _tf.mkdtemp(prefix="srtejes_")
    cimp, _, _ = ams2_srt.importar(MUESTRA, destino=dimp)
    tr = sorted(f for f in os.listdir(cimp) if f.endswith(".csv.gz"))[0]
    col = {}
    with _gz.open(os.path.join(cimp, tr), "rt", encoding="utf-8") as f:
        r = _csv.DictReader(f)
        for k in r.fieldnames:
            col[k] = []
        for row in r:
            for k in r.fieldnames:
                col[k].append(float(row[k]))
    pedal = [a - b for a, b in zip(col["throttle"], col["brake"])]
    ok("accel_z (nuestro LONGITUDINAL) sigue al pedal",
       abs(_corr(col["accel_z"], pedal)) > 0.4,
       f'corr={_corr(col["accel_z"], pedal):+.3f}')
    ok("accel_x (nuestro LATERAL) NO sigue al pedal",
       abs(_corr(col["accel_x"], pedal)) < 0.35,
       f'corr={_corr(col["accel_x"], pedal):+.3f}')
    ok("accel_x (LATERAL) sigue al volante",
       abs(_corr(col["accel_x"], col["steer"])) > 0.5,
       f'corr={_corr(col["accel_x"], col["steer"]):+.3f}')
    ok("ang_vel_y (nuestro YAW) sigue al volante",
       abs(_corr(col["ang_vel_y"], col["steer"])) > 0.7,
       f'corr={_corr(col["ang_vel_y"], col["steer"]):+.3f}')
    vlong = max(abs(x) for x in col["local_vz"])
    vlat = max(abs(x) for x in col["local_vx"])
    ok("local_vz (LONGITUDINAL) es de orden de la velocidad y local_vx (lateral) no",
       vlong > 10 and vlat < vlong / 4, f"long max {vlong:.1f} m/s · lat max {vlat:.1f}")
    _sh.rmtree(dimp, ignore_errors=True)

    print("\nENTRADA NO CONFIABLE (el .srt llega de un tercero):")
    import zlib as _zl
    raw = open(MUESTRA, "rb").read()
    d2 = _tf.mkdtemp(prefix="srtmal_")

    def _falla_limpio(nombre, datos):
        ruta = os.path.join(d2, "x.srt")
        open(ruta, "wb").write(datos)
        try:
            ams2_srt.leer(ruta)
            ok(nombre, False, "no lanzo nada")
        except ams2_srt.SrtError:
            ok(nombre, True)
        except Exception as e:                      # noqa: BLE001
            ok(nombre, False, f"lanzo {type(e).__name__} en vez de SrtError")

    _falla_limpio("archivo truncado a la mitad", raw[:len(raw) // 2])
    _falla_limpio("solo la cabecera", raw[:1565])
    _falla_limpio("no es un .srt", b"esto no es un srt" * 100)
    _falla_limpio("stream zlib corrupto",
                  raw[:1565 + 40] + bytes(len(raw) - 1565 - 40))
    malo = bytearray(raw)
    malo[76:80] = (2 ** 31).to_bytes(4, "little")   # largo de string absurdo en 'src '
    _falla_limpio("largo de string absurdo", bytes(malo))
    # bomba de descompresion: poco en disco, gigas al abrir
    bomba = _zl.compress(bytes(400 * 1024 * 1024), 9)
    z = b"F___TD  " + (400 * 1024 * 1024).to_bytes(4, "little") + b"zlib" + bomba
    cab = raw[:1557]
    cuerpo = cab[20:] + b"Z___" + len(z).to_bytes(4, "little") + z
    _falla_limpio("bomba de descompresion (declara 400 MB)",
                  b"F___" + (len(cuerpo) + 4).to_bytes(4, "little") + b"SRT " + cuerpo)
    _sh.rmtree(d2, ignore_errors=True)

    print("\nESCRITURA - reconstruir el archivo original (la prueba dura):")
    # Si se puede volver a emitir el .srt de otro y el bloque de datos sale
    # IDENTICO byte a byte, el escritor produce el formato de verdad y no una
    # aproximacion que quizas la app acepte.
    import struct, zlib, tempfile
    b = open(MUESTRA, "rb").read()
    cab = {}
    for tag, ini, nn in ams2_srt._chunks(b, 12, len(b)):
        if tag in ams2_srt.CONTENEDORES and b[ini:ini + 4] == b"hdrl":
            ams2_srt._juntar(b, ini + 4, ini + nn, cab)
    guid = cab[b"hdr "][0][16:32]
    (ts,) = struct.unpack_from("<Q", cab[b"hdr "][0], 32)
    (tss,) = struct.unpack_from("<Q", cab[b"sess"][0], 4)
    val = [(q[28], q[29], q[30]) for q in cab[b"lap "]]
    vv = [{"tiempo": v["tiempo"], "sectores": v["sectores"],
           "sec_validos": [bool(x) for x in val[j]],
           "muestras": [list(struct.unpack_from("<" + str(s["n_valores"]) + "f",
                                                s["_datos"], o))
                        for o, _ in v["muestras"]]}
          for j, v in enumerate(s["vueltas"])]
    meta2 = dict(m)
    meta2.update(guid=guid, ts=ts, ts_sesion=tss)
    tmp = os.path.join(tempfile.mkdtemp(), "r.srt")
    ams2_srt.escribir(tmp, meta2, vv)
    nb = open(tmp, "rb").read()
    oi = zlib.decompress(b[1565 + 16:])
    ni = zlib.decompress(nb[1565 + 16:])
    ok("el bloque de datos sale IDENTICO byte a byte", oi == ni,
       str(len(oi)) + " vs " + str(len(ni)) + " bytes")
    ok("la cabecera sale identica salvo el tamano de archivo",
       nb[8:1557] == b[8:1557], "(el u32 de tamano cambia por el nivel de zlib)")

    print("\nEXPORTAR una sesion NUESTRA y releerla:")
    import ams2_analysis as A
    propias = [d for d in A._dirs()
               if A._meta(d).get("origen") != "srt"
               and [v for v in A._vueltas(d) if v.get("traza") and v.get("tiempo")]]
    if not propias:
        # sin sesiones propias no hay nada que exportar: es ausencia de datos
        # (clon fresco), no un defecto -- mismo criterio que el archivo de muestra
        print("  (sin sesiones propias con trazas; nada que exportar)")
    else:
        out = os.path.join(tempfile.mkdtemp(), "mio.srt")
        ruta, nv = ams2_srt.exportar(propias[0], out, piloto="Test", max_vueltas=2)
        ok("genera el archivo", os.path.getsize(ruta) > 1000, str(nv) + " vueltas")
        s2 = ams2_srt.leer(ruta)
        ok("se relee con nuestro propio lector", len(s2["vueltas"]) == nv)
        ok("conserva piloto y auto",
           s2["meta"]["piloto"] == "Test" and bool(s2["meta"]["auto"]))
        c2 = ams2_srt.como_dict(s2, 0)
        n2 = len(c2["lap_distance"])
        ok("acelerador y freno siguen en 0..1",
           max(c2["throttle"]) <= 1.001 and max(c2["brake"]) <= 1.001)
        ok("presiones exportadas en pascal (1-3 bar al releer)",
           all(1.0 < x / 100000 < 3.5 for x in c2["tyre_press"][n2 // 2]),
           [round(x / 100000, 2) for x in c2["tyre_press"][n2 // 2]])
        P2 = c2["world_position"]
        largo2 = sum(math.hypot(P2[k + 1][0] - P2[k][0], P2[k + 1][1] - P2[k][1])
                     for k in range(n2 - 1))
        L2 = s2["meta"]["largo_m"]
        ok("el trazado exportado cierra el largo (ejes bien puestos)",
           bool(L2) and abs(largo2 - L2) / L2 < 0.05,
           str(round(largo2)) + " m vs " + str(round(L2)))
        alt2 = [q[2] for q in P2]
        ok("la altura quedo en el componente 2, no mezclada con el plano",
           max(alt2) - min(alt2) < largo2 * 0.1,
           str(round(max(alt2) - min(alt2))) + " m")
        ok("la velocidad longitudinal exportada sale POSITIVA hacia adelante",
           max(q[0] for q in c2["velocity"]) > 10,
           f'velocity[0] max {max(q[0] for q in c2["velocity"]):.1f} m/s')
        ok("la altura al piso exportada esta en metros, no en cm",
           all(0.01 < x < 0.5 for x in c2["ride_height"][n2 // 2]),
           [round(x, 3) for x in c2["ride_height"][n2 // 2]])
        ok("tyre_isOnGround no dice que el auto volo toda la vuelta",
           all(x > 0.5 for x in c2["tyre_isOnGround"][n2 // 2]))

    print(f"\n{'TODO VERDE' if not _fallos else str(_fallos) + ' FALLO(S)'}")
    return 1 if _fallos else 0


if __name__ == "__main__":
    sys.exit(main())
