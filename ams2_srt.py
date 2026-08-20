#!/usr/bin/env python3
"""Lector del formato .srt de Sim Racing Telemetry (AMS2), e importador al
formato de este repo para poder abrir vueltas AJENAS en nuestro visor.

Sirve para comparar tu trazada contra la de otro piloto que grabe con esa app.

EL FORMATO, ingenieria inversa verificada byte a byte contra un archivo real
(AMS2v1-72759aba..., 15.7 MB, 33 vueltas en Spielberg con un Alpine A110 GT4):

  Contenedor tipo RIFF. Cada chunk es  <FourCC><u32 tamano><carga>.
  Los tags F___ / L___ / Z___ son CONTENEDORES: su carga empieza con un FourCC
  de tipo y sigue con hijos. Los strings van con prefijo de largo u32.

    F___ <SRT >                      firma; el u32 es (tamano de archivo - 8)
      hdr    GUID de la grabacion + timestamp
      src    "AMS2v1" + build del juego
      sess   timestamp + nombre del piloto
      trck   nombre de pista + largo en metros
      veh    nombre del auto
      L___ <lapl>   indice: un 'lap ' de 31 b por vuelta
      Z___          <F___><TD  ><u32 tamano crudo>"zlib" + stream zlib
        L___ <hdrl>
          dsdh   [0, ruedas=4, n_canales=71]
          dsdp   x71: <u32 id><u32 largo><nombre><u32 instancias><u32 componentes>...
        L___ <tdat>
          L___ <lap > x N
            laph   [.., .., .., n_muestras, n_valores=155]
            laps   4 floats: tiempo de vuelta + 3 sectores
            lapd   x n_muestras: UNA MUESTRA de 624 bytes

  MUESTRA = <f32 distancia (clave repetida)> + 155 f32 en el orden de los dsdp.
  Cada canal ocupa instancias*componentes floats: 1 los escalares, 3 los
  vectores, 4 los de rueda. La suma da 155 exactos, que es lo que declara laph.

  COMO SE VERIFICO el alineamiento (no se asumio): decodificando con offset 4
  los valores son fisicamente coherentes -- acelerador 1.0 con freno 0.0 en 5a a
  6574 rpm, presiones de 1.66-1.74 bar, distancia dentro del largo de la pista.
  Con offset 0 todo sale corrido un float y absurdo (acelerador 0 con freno 1 en
  punto muerto). El orden de ruedas es FL, FR, RL, RR: las presiones y las temps
  de carcasa salen mas altas atras, que es la firma conocida de este auto.

Uso:
    python ams2_srt.py <archivo.srt>              # resumen
    python ams2_srt.py <archivo.srt> --importar   # deja la sesion en el visor
"""
from __future__ import annotations

import os
import struct
import zlib

CONTENEDORES = {b"F___", b"L___", b"Z___"}
RUEDAS = ("FL", "FR", "RL", "RR")          # orden verificado contra el dato
HERE = os.path.dirname(os.path.abspath(__file__))
# Las importaciones NO van a telemetry/: ese directorio es el corpus con el que se
# validan los analizadores (tyre_replay y compania). Una sesion ajena, con canales
# que no tenemos y ceros en el resto, envenenaria ese corpus en silencio.
IMPORT_DIR = os.path.join(HERE, "telemetry_importado")


class SrtError(Exception):
    pass


# --------------------------------------------------------------- contenedor ---
def _chunks(buf, off, fin):
    """Itera (tag, inicio_carga, tamano) de los chunks entre off y fin."""
    while off + 8 <= fin:
        tag = buf[off:off + 4]
        (n,) = struct.unpack_from("<I", buf, off + 4)
        if not all(32 <= c < 127 for c in tag) or off + 8 + n > fin:
            raise SrtError(f"chunk invalido en {off}: {tag!r} tamano {n}")
        yield tag, off + 8, n
        off += 8 + n


def _texto(buf, off):
    """String con prefijo de largo u32. Devuelve (texto, offset siguiente)."""
    (n,) = struct.unpack_from("<I", buf, off)
    return buf[off + 4:off + 4 + n].decode("utf-8", "replace"), off + 4 + n


def _juntar(buf, off, fin, salida):
    """Recorre el arbol juntando las cargas de los chunks hoja por tag."""
    for tag, ini, n in _chunks(buf, off, fin):
        if tag in CONTENEDORES:
            _juntar(buf, ini + 4, ini + n, salida)
        else:
            salida.setdefault(tag, []).append(buf[ini:ini + n])


# ------------------------------------------------------------------- lectura ---
def leer(ruta):
    """Lee un .srt completo: metadatos, vueltas y muestras decodificadas."""
    with open(ruta, "rb") as f:
        b = f.read()
    if b[:4] != b"F___" or b[8:12] != b"SRT ":
        raise SrtError("no parece un .srt de Sim Racing Telemetry")

    cab = {}
    z = None
    for tag, ini, n in _chunks(b, 12, len(b)):
        if tag == b"Z___":
            z = b[ini:ini + n]
        elif tag in CONTENEDORES:
            _juntar(b, ini + 4, ini + n, cab)
        else:
            cab.setdefault(tag, []).append(b[ini:ini + n])

    meta = {"archivo": os.path.basename(ruta)}
    if b"src " in cab:
        meta["juego"], o = _texto(cab[b"src "][0], 8)
        meta["build"], _ = _texto(cab[b"src "][0], o)
    if b"sess" in cab:
        meta["piloto"], _ = _texto(cab[b"sess"][0], 24)
    if b"trck" in cab:
        meta["pista"], o = _texto(cab[b"trck"][0], 4)   # ojo: aqui el largo va en el offset 4
        # entre el nombre y el largo hay 4 bytes de relleno; despues del largo
        # vienen las distancias de corte de los 3 sectores, que la app usa para
        # dividir la vuelta (medido: 1239 / 2912 / 4307.5 m en Spielberg).
        p = cab[b"trck"][0]
        (meta["largo_m"],) = struct.unpack_from("<f", p, o + 4)
        (n_sec,) = struct.unpack_from("<I", p, o + 8)
        if n_sec and o + 12 + 4 * n_sec <= len(p):
            meta["sectores_m"] = list(struct.unpack_from(f"<{n_sec}f", p, o + 12))
    if b"veh " in cab:
        meta["auto"], _ = _texto(cab[b"veh "][0], 4)    # idem (src usa 8 y sess usa 24)

    if z is None:
        raise SrtError("no hay bloque de datos comprimido")
    if z[12:16] != b"zlib":
        raise SrtError(f"compresion no soportada: {z[12:16]!r}")
    (crudo,) = struct.unpack_from("<I", z, 8)
    d = zlib.decompress(z[16:])
    if len(d) != crudo:
        raise SrtError(f"descompresion inconsistente: {len(d)} != {crudo}")

    canales, vueltas = [], []
    for tag, ini, n in _chunks(d, 12, len(d)):
        if tag != b"L___":
            continue
        tipo = d[ini:ini + 4]
        if tipo == b"hdrl":
            for t2, i2, n2 in _chunks(d, ini + 4, ini + n):
                if t2 == b"L___" and d[i2:i2 + 4] == b"dsd ":
                    for t3, i3, n3 in _chunks(d, i2 + 4, i2 + n2):
                        if t3 == b"dsdp":
                            nom, o = _texto(d, i3 + 4)
                            inst, comp = struct.unpack_from("<2I", d, o)
                            canales.append((nom, inst, comp))
        elif tipo == b"tdat":
            for t2, i2, n2 in _chunks(d, ini + 4, ini + n):
                if t2 != b"L___" or d[i2:i2 + 4] != b"lap ":
                    continue
                v = {"tiempo": None, "sectores": None, "muestras": []}
                for t3, i3, n3 in _chunks(d, i2 + 4, i2 + n2):
                    if t3 == b"laps":
                        f4 = struct.unpack_from("<4f", d, i3)
                        v["tiempo"], v["sectores"] = f4[0], list(f4[1:])
                    elif t3 == b"lapd":
                        v["muestras"].append((i3 + 4, n3 - 4))
                vueltas.append(v)

    if not canales:
        raise SrtError("no se encontro la definicion de canales")
    n_val = sum(i * c for _, i, c in canales)
    esperado = 4 + n_val * 4
    for v in vueltas:
        for _, n in v["muestras"]:
            if n + 4 != esperado:
                raise SrtError(f"muestra de {n+4} b, se esperaban {esperado}")

    return {"meta": meta, "canales": canales, "n_valores": n_val,
            "vueltas": vueltas, "_datos": d}


def como_dict(srt, vuelta):
    """Una vuelta como {canal: serie}. Los canales de rueda dan listas de 4."""
    d = srt["_datos"]
    v = srt["vueltas"][vuelta]
    n = srt["n_valores"]
    filas = [struct.unpack_from(f"<{n}f", d, off) for off, _ in v["muestras"]]
    out, pos = {}, 0
    for nom, inst, comp in srt["canales"]:
        k = inst * comp
        if k == 1:
            out[nom] = [f[pos] for f in filas]
        else:
            out[nom] = [list(f[pos:pos + k]) for f in filas]
        pos += k
    return out


# ---------------------------------------------------------------- importar ---
# Mapa canal-nuestro -> (canal SRT, indice dentro del canal, factor).
# indice None = escalar. Las dos conversiones de unidad estan MEDIDAS, no supuestas:
#   gforce viene en g          (rango -1.1..2.1)  -> nosotros usamos m/s2
#   tyre_press viene en pascal (182000)           -> nosotros Bar x100 (182)
_MAPA = {
    "t": ("lap_time", None, 1.0),
    "lap_dist": ("lap_distance", None, 1.0),
    "rpm": ("rpm", None, 1.0),
    "gear": ("gear", None, 1.0),
    "throttle": ("throttle", None, 1.0),
    "brake": ("brake", None, 1.0),
    "clutch": ("clutch", None, 1.0),
    "steer": ("steering", None, 1.0),
    "steer_f": ("filteredSteering", None, 1.0),
    "accel_x": ("gforce", 0, 9.80665),
    "accel_y": ("gforce", 1, 9.80665),
    "accel_z": ("gforce", 2, 9.80665),
    # OJO CON EL ORDEN DE LOS EJES: SRT guarda world_position como [x, z, y] --
    # el VERTICAL es el componente 2, no el 1 como en la shared memory de AMS2.
    # Se detecto midiendo rangos en una vuelta de Spielberg: [0]=791 m, [1]=1250 m,
    # [2]=64 m, y 64 m es justo el desnivel del circuito. La prueba dura es el
    # largo integrado del trazado: con los componentes (0,2) da 1952 m y con
    # (0,1) da 4310, contra 4305 de lap_distance. Mapearlo mal dibuja un eje
    # horizontal contra el perfil de altura: el trazado sale como un garabato y
    # el detector de curvas encuentra 32 donde hay 10.
    "pos_x": ("world_position", 0, 1.0),
    "pos_y": ("world_position", 2, 1.0),
    "pos_z": ("world_position", 1, 1.0),
    "water_t": ("water_temp", None, 1.0),
    "oil_t": ("oil_temp", None, 1.0),
    "fuel_l": ("fuel", None, 1.0),
    "engine_torque": ("engine_torque", None, 1.0),
    "local_vx": ("velocity", 0, 1.0),
    "local_vz": ("velocity", 2, 1.0),
    "ang_vel_x": ("angular_vel", 0, 1.0),
    "ang_vel_y": ("angular_vel", 1, 1.0),
    "ang_vel_z": ("angular_vel", 2, 1.0),
    "abs_active": ("abs", None, 1.0),
}
_MAPA_RUEDA = {
    "tyre_temp": ("tyre_temp", 1.0),
    "brake_temp": ("brake_temp", 1.0),
    "tyre_wear": ("tyre_wear", 1.0),
    "susp_travel": ("susp_pos", 1.0),
    "susp_vel": ("susp_vel", 1.0),
    "tyre_slip": ("tyre_slipSpeed", 1.0),
    "ride_h": ("ride_height", 1.0),
    "tyre_press": ("tyre_press", 0.001),
    "tyre_rps": ("tyre_rps", 1.0),
    "terrain": ("terrain", 1.0),
    "carcass_t": ("tyre_tempCarcass", 1.0),
    "tyre_grip": ("tyre_grip", 1.0),
    "layer_t": ("tyre_tempLayer", 1.0),
}
# Lo que la app del otro piloto NO graba y por eso va en 0. Se declara en
# session.json para que nadie lea un cero como si fuera una medicion.
AUSENTES = ("brake_bias", "yaw", "pitch", "roll", "max_rpm",
            "tyre_t_in", "tyre_t_mid", "tyre_t_out")


def importar(ruta, destino=None, minimo_s=30.0):
    """Convierte un .srt a una carpeta de sesion de este repo, para el visor.

    Devuelve la ruta creada. Solo importa vueltas cronometradas de mas de
    `minimo_s` (las de out/in lap de la app vienen con tiempos absurdos).
    """
    import csv as _csv
    import gzip as _gzip
    import json as _json
    import ams2_telemetry as T

    srt = leer(ruta)
    m = srt["meta"]
    destino = destino or IMPORT_DIR
    os.makedirs(destino, exist_ok=True)
    limpio = lambda s: "".join(c if c.isalnum() else "_" for c in str(s)).strip("_")
    nombre = f"{limpio(m.get('pista'))}__{limpio(m.get('auto'))}__{limpio(m.get('piloto'))}"
    carpeta = os.path.join(destino, nombre)
    os.makedirs(carpeta, exist_ok=True)

    presentes = sorted(set(_MAPA) | {f"{k}_{c}" for k in _MAPA_RUEDA for c in RUEDAS}
                       | {"speed_kmh"})
    with open(os.path.join(carpeta, "session.json"), "w", encoding="utf-8") as f:
        _json.dump({
            "track": m.get("pista"), "car": m.get("auto"), "session": "importada",
            "track_length_m": round(m.get("largo_m") or 0, 1),
            "started": None, "rate_hz": None,
            "channels": T.HEADER,
            "origen": "srt", "piloto": m.get("piloto"),
            "juego": m.get("juego"), "build": m.get("build"),
            "sectores_m": m.get("sectores_m"),
            "canales_ausentes": list(AUSENTES),
            "nota": ("Importado de Sim Racing Telemetry. Los canales ausentes van en "
                     "0.0: NO son mediciones. Esta carpeta vive fuera de telemetry/ "
                     "para no contaminar el corpus con el que se validan los analizadores."),
        }, f, ensure_ascii=False, indent=1)

    tl, res, uid = [], [], 0
    for i, v in enumerate(srt["vueltas"]):
        t = v["tiempo"]
        if not t or t < minimo_s or not v["muestras"]:
            continue
        c = como_dict(srt, i)
        n = len(c["lap_distance"])
        invalida = any(x > 0.5 for x in c["lap_time_invalid"])
        uid += 1
        pre = "X" if invalida else "L"
        arch = f"{pre}{uid:03d}_{t:.3f}s.csv.gz"
        with _gzip.open(os.path.join(carpeta, arch), "wt", newline="", encoding="utf-8") as f:
            w = _csv.writer(f, lineterminator="\n")
            w.writerow(T.HEADER)
            for k in range(n):
                fila = []
                for col in T.HEADER:
                    if col == "speed_kmh":
                        vx, vy, vz = c["velocity"][k]
                        fila.append(round((vx * vx + vy * vy + vz * vz) ** 0.5 * 3.6, 2))
                        continue
                    if col in _MAPA:
                        nom, idx, esc = _MAPA[col]
                        val = c[nom][k] if idx is None else c[nom][k][idx]
                        fila.append(round(val * esc, 5))
                        continue
                    base, _, esq = col.rpartition("_")
                    if base in _MAPA_RUEDA and esq in RUEDAS:
                        nom, esc = _MAPA_RUEDA[base]
                        fila.append(round(c[nom][k][RUEDAS.index(esq)] * esc, 5))
                    else:
                        fila.append(0.0)
                w.writerow(fila)
        rec = {"uid": uid, "lap": uid, "lap_time": round(t, 3),
               "sectors": [round(x, 3) for x in (v["sectores"] or [])],
               "samples": n, "trace": arch}
        tl.append({"type": "lap", "kind": "invalid" if invalida else "flying",
                   "pit": False, "out": False, "invalid": invalida, **rec})
        if not invalida:
            res.append({**rec, "valid": True})

    with open(os.path.join(carpeta, "timeline.jsonl"), "w", encoding="utf-8") as f:
        for r in tl:
            f.write(_json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(carpeta, "summary.jsonl"), "w", encoding="utf-8") as f:
        for r in res:
            f.write(_json.dumps(r, ensure_ascii=False) + "\n")
    return carpeta, len(tl), len(res)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("archivo")
    ap.add_argument("--importar", action="store_true")
    a = ap.parse_args()
    s = leer(a.archivo)
    m = s["meta"]
    print(f"{m.get('pista')} · {m.get('auto')} · piloto {m.get('piloto')} "
          f"· {m.get('juego')} build {m.get('build')}")
    print(f"largo {m.get('largo_m'):.1f} m · {len(s['canales'])} canales · "
          f"{len(s['vueltas'])} vueltas")
    buenas = [(i, v["tiempo"]) for i, v in enumerate(s["vueltas"])
              if v["tiempo"] and v["tiempo"] > 30]
    if buenas:
        i, t = min(buenas, key=lambda x: x[1])
        print(f"mejor: vuelta {i} en {t:.3f} s")
    if a.importar:
        carpeta, n, ok = importar(a.archivo)
        print(f"\nimportado -> {carpeta}\n{n} vueltas ({ok} limpias)")


if __name__ == "__main__":
    main()
