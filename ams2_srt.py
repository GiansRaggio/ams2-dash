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
import sys
import struct
import zlib

CONTENEDORES = {b"F___", b"L___", b"Z___"}
RUEDAS = ("FL", "FR", "RL", "RR")          # orden verificado contra el dato
HERE = os.path.dirname(os.path.abspath(__file__))
# Las importaciones NO van a telemetry/: ese directorio es el corpus con el que se
# validan los analizadores (tyre_replay y compania). Una sesion ajena, con canales
# que no tenemos y ceros en el resto, envenenaria ese corpus en silencio.
IMPORT_DIR = os.path.join(HERE, "telemetry_importado")
# Tope del bloque descomprimido. Un archivo real de 33 vueltas son 40 MB; 512
# deja margen de sobra para una sesion larga y ataja la bomba de descompresion.
MAX_DESCOMPRIMIDO = 512 * 1024 * 1024


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
    """String con prefijo de largo u32. Devuelve (texto, offset siguiente).

    El largo viene del archivo, o sea de afuera: sin esta guarda un u32 corrupto
    corre el offset fuera del buffer y el siguiente unpack revienta con
    struct.error en vez de SrtError.
    """
    if off + 4 > len(buf):
        raise SrtError("string fuera del chunk")
    (n,) = struct.unpack_from("<I", buf, off)
    if n > len(buf) - off - 4:
        raise SrtError(f"largo de string invalido: {n} en un chunk de {len(buf)}")
    return buf[off + 4:off + 4 + n].decode("utf-8", "replace"), off + 4 + n


def _juntar(buf, off, fin, salida, prof=0):
    """Recorre el arbol juntando las cargas de los chunks hoja por tag.

    Con tope de profundidad: contenedores anidados a mano (F___/L___/Z___ uno
    dentro de otro) hacen RecursionError, que no es SrtError y rompe la promesa
    del modulo de fallar siempre igual.
    """
    if prof > 32:
        raise SrtError("anidamiento de contenedores absurdo")
    for tag, ini, n in _chunks(buf, off, fin):
        if tag in CONTENEDORES:
            _juntar(buf, ini + 4, ini + n, salida, prof + 1)
        else:
            salida.setdefault(tag, []).append(buf[ini:ini + n])


# ------------------------------------------------------------------- lectura ---
def leer(ruta):
    """Lee un .srt completo: metadatos, vueltas y muestras decodificadas.

    Todo lo que salga mal sale como SrtError: el archivo viene de un tercero y
    quien llame no tiene por que saber de zlib ni de struct.
    """
    try:
        return _leer(ruta)
    except SrtError:
        raise
    except (zlib.error, struct.error, RecursionError, UnicodeError, MemoryError) as e:
        raise SrtError(f"archivo .srt corrupto o truncado: {type(e).__name__}: {e}") from e


def _leer(ruta):
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
    if crudo > MAX_DESCOMPRIMIDO:
        raise SrtError(f"el archivo declara {crudo:,} bytes sin comprimir; "
                       f"el tope es {MAX_DESCOMPRIMIDO:,}")
    # ACOTADO A PROPOSITO: `zlib.decompress()` a secas descomprime todo antes de
    # que nadie valide nada, asi que un .srt ajeno de 300 KB puede reventar a
    # cientos de MB de RAM (medido: ratio 1028x). Se descomprime como maximo lo
    # que el archivo DECLARA, y si sobra stream es que mentia.
    obj = zlib.decompressobj()
    d = obj.decompress(z[16:], crudo)
    if obj.unconsumed_tail:
        raise SrtError("el stream comprimido es mas grande de lo que declara")
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


# ============================== ESCRITURA ==============================
# Descriptores de canal EXACTOS como los declara la app, sacados de un archivo
# real. Los 9 enteros de cada uno codifican unidad/precision/formato: no se
# entienden todos, pero se reproducen tal cual para que la app los interprete
# igual que los suyos. Instancias*componentes suma 155, el largo de la muestra.
CANALES_SRT = (
    ("lap_number", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("lap_distance", (1, 1, 1, 1, 1, 0, 0, 0, 3)),
    ("lap_time", (1, 1, 1, 1, 5, 0, 1, 0, 3)),
    ("lap_time_invalid", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("world_position", (1, 3, 1, 3, 1, 0, 0, 0, 3)),
    ("world_forward", (1, 3, 1, 3, 0, 0, 0, 0, 4)),
    ("world_right", (1, 3, 1, 3, 0, 0, 0, 0, 4)),
    ("velocity", (1, 3, 1, 2, 3, 3, 0, 1, 4)),
    ("gforce", (1, 3, 1, 2, 0, 0, 0, 1, 4)),
    ("angular_vel", (1, 3, 1, 2, 0, 0, 0, 1, 4)),
    ("pit_status", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("race_position", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("flags_status", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("throttle", (1, 1, 1, 1, 9, 4, 0, 0, 3)),
    ("brake", (1, 1, 1, 1, 9, 4, 0, 0, 3)),
    ("clutch", (1, 1, 1, 1, 9, 4, 0, 0, 3)),
    ("steering", (1, 1, 1, 1, 9, 4, 0, 0, 3)),
    ("filteredThrottle", (1, 1, 1, 1, 9, 4, 0, 0, 3)),
    ("filteredBrake", (1, 1, 1, 1, 9, 4, 0, 0, 3)),
    ("filteredClutch", (1, 1, 1, 1, 9, 4, 0, 0, 3)),
    ("filteredSteering", (1, 1, 1, 1, 9, 4, 0, 0, 3)),
    ("gear", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("rpm", (1, 1, 1, 1, 0, 0, 0, 0, 3)),
    ("rpm_perc", (1, 1, 1, 1, 9, 4, 0, 0, 3)),
    ("fuel", (1, 1, 1, 1, 6, 0, 0, 0, 3)),
    ("splitTime_ahead", (1, 1, 0, 1, 5, 0, 0, 0, 3)),
    ("splitTime_behind", (1, 1, 0, 1, 5, 0, 0, 0, 3)),
    ("oil_temp", (1, 1, 1, 1, 4, 0, 0, 0, 3)),
    ("oil_press", (1, 1, 1, 1, 8, 5, 0, 0, 3)),
    ("water_temp", (1, 1, 1, 1, 4, 0, 0, 0, 3)),
    ("water_press", (1, 1, 1, 1, 8, 5, 0, 0, 3)),
    ("fuel_press", (1, 1, 1, 1, 8, 5, 0, 0, 3)),
    ("boost_active", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("boost_level", (1, 1, 1, 1, 9, 4, 0, 0, 3)),
    ("terrain", (4, 1, 2, 1, 10, 0, 0, 0, 2)),
    ("tyre_isOnGround", (4, 1, 2, 1, 10, 0, 0, 0, 2)),
    ("tyre_y", (4, 1, 2, 1, 1, 0, 0, 0, 3)),
    ("tyre_rps", (4, 1, 2, 1, 0, 0, 0, 0, 3)),
    ("tyre_slipSpeed", (4, 1, 2, 1, 3, 0, 0, 0, 3)),
    ("tyre_temp", (4, 1, 2, 1, 4, 0, 0, 0, 3)),
    ("tyre_grip", (4, 1, 2, 1, 9, 4, 0, 0, 3)),
    ("tyre_heightAboveGround", (4, 1, 2, 1, 1, 0, 0, 0, 3)),
    ("tyre_latStiff", (4, 1, 2, 1, 0, 0, 0, 0, 2)),
    ("tyre_wear", (4, 1, 2, 1, 9, 4, 0, 0, 3)),
    ("tyre_tempTread", (4, 1, 2, 1, 4, 0, 0, 0, 3)),
    ("tyre_tempLayer", (4, 1, 2, 1, 4, 0, 0, 0, 3)),
    ("tyre_tempCarcass", (4, 1, 2, 1, 4, 0, 0, 0, 3)),
    ("tyre_tempRim", (4, 1, 2, 1, 4, 0, 0, 0, 3)),
    ("tyre_tempInternalAir", (4, 1, 2, 1, 4, 0, 0, 0, 3)),
    ("brake_temp", (4, 1, 2, 1, 4, 0, 0, 0, 3)),
    ("brake_damage", (4, 1, 2, 1, 9, 4, 0, 0, 3)),
    ("ride_height", (4, 1, 2, 1, 1, 0, 0, 0, 3)),
    ("wheel_yPos", (4, 1, 2, 1, 1, 0, 0, 0, 3)),
    ("susp_damage", (4, 1, 2, 1, 9, 4, 0, 0, 3)),
    ("susp_pos", (4, 1, 2, 1, 1, 0, 0, 0, 3)),
    ("susp_vel", (4, 1, 2, 1, 3, 0, 0, 0, 3)),
    ("tyre_press", (4, 1, 2, 1, 8, 6, 0, 0, 3)),
    ("aero_damage", (1, 1, 1, 1, 9, 4, 0, 0, 3)),
    ("engine_damage", (1, 1, 1, 1, 9, 4, 0, 0, 3)),
    ("engine_torque", (1, 1, 1, 1, 0, 0, 0, 0, 3)),
    ("engine_warning", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("speed_limiter", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("abs", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("handbrake", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("stability_ctrl", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("traction_ctrl", (1, 1, 1, 1, 10, 0, 0, 0, 2)),
    ("ambient_temp", (1, 1, 0, 1, 4, 0, 0, 0, 3)),
    ("track_temp", (1, 1, 0, 1, 4, 0, 0, 0, 3)),
    ("track_rain", (1, 1, 0, 1, 9, 4, 0, 0, 3)),
    ("track_wind", (1, 3, 0, 3, 3, 0, 0, 1, 4)),
    ("wing_setup", (2, 1, 3, 1, 9, 4, 0, 0, 3)),
)


def _u32(*v):
    return struct.pack(f"<{len(v)}I", *v)


def _f32(*v):
    return struct.pack(f"<{len(v)}f", *v)


def _str(s):
    b = str(s).encode("utf-8")
    return struct.pack("<I", len(b)) + b


def _chunk(tag, carga):
    return tag + struct.pack("<I", len(carga)) + carga


def _cont(tag, tipo, hijos):
    return _chunk(tag, tipo + b"".join(hijos))


def escribir(destino, meta, vueltas, nivel=6):
    """Genera un .srt que Sim Racing Telemetry puede abrir.

    `vueltas` es una lista de dicts con: tiempo (s), sectores (3 floats),
    sec_validos (3 bools) y muestras (lista de listas de 155 floats, en el orden
    de CANALES_SRT).

    Los campos que no sabemos interpretar (el 0x00010016 de `src`, el
    [1,1,4,2] de `dsdc`, los 9 enteros de cada canal) se emiten con los valores
    observados en un archivo real: reproducirlos es mas seguro que inventarlos.
    """
    guid = meta.get("guid") or os.urandom(16)
    # DOS marcas de tiempo distintas, no una: `hdr` lleva la de la grabacion y
    # `sess`/`hdrt` la del inicio de sesion. Escribir la misma en las tres deja
    # el archivo casi identico y por eso es facil no notarlo.
    ts = int(meta.get("ts") or 0)
    ts_sesion = int(meta.get("ts_sesion") or ts)
    n_val = sum(i * c for _, (i, c, *_r) in CANALES_SRT)

    # --- indice de vueltas (va sin comprimir, en la cabecera) ---
    lapl = []
    for k, v in enumerate(vueltas):
        s = list(v.get("sectores") or [0.0, 0.0, 0.0])[:3]
        s += [0.0] * (3 - len(s))
        ok = list(v.get("sec_validos") or [True, True, True])[:3]
        lapl.append(_chunk(b"lap ", _u32(0, k, 0) + _f32(v["tiempo"], *s)
                           + bytes(1 if x else 0 for x in ok)))

    cabecera = _cont(b"L___", b"hdrl", [
        _chunk(b"hdr ", _u32(0, 0, 0, 16) + guid + struct.pack("<Q", ts)),
        _chunk(b"src ", _u32(0, 0x00010016) + _str(meta.get("juego", "AMS2v1"))
               + _str(meta.get("build", "0")) + _u32(1)),
        _chunk(b"sess", _u32(0) + struct.pack("<Q", ts_sesion) + _u32(1)
               + struct.pack("<Q", len(vueltas)) + _str(meta.get("piloto", ""))),
        _chunk(b"trck", _u32(0) + _str(meta.get("pista", "")) + _u32(0)
               + _f32(meta.get("largo_m") or 0.0)
               + _u32(len(meta.get("sectores_m") or []))
               + _f32(*(meta.get("sectores_m") or []))),
        _chunk(b"veh ", _u32(0) + _str(meta.get("auto", "")) + _u32(0)),
        _cont(b"L___", b"lapl", lapl),
    ])

    # --- bloque de datos (comprimido) ---
    dsdp = [_chunk(b"dsdp", _u32(0) + _str(n) + _u32(*t)) for n, t in CANALES_SRT]
    hdrt = (_u32(0, 0, 0x00010016) + _str(meta.get("juego", "AMS2v1")) + _u32(1)
            + _str(meta.get("build", "0")) + _u32(1) + _str(meta.get("auto", ""))
            + _u32(0) + struct.pack("<Q", ts_sesion) + _u32(len(vueltas)))
    secs = list(meta.get("sectores_m") or [])
    interno_hdr = _cont(b"L___", b"hdrl", [
        _chunk(b"hdrt", hdrt),
        _cont(b"L___", b"dsd ", [_chunk(b"dsdh", _u32(0, 4, len(CANALES_SRT))),
                                 _chunk(b"dsdc", _u32(1, 1, 4, 2))] + dsdp),
        # La pista va DOS veces: en la cabecera sin comprimir (`trck`) y otra vez
        # aqui dentro. Omitirlo deja el archivo 77 bytes corto y, sobre todo,
        # sin la informacion de sectores que la app usa para partir la vuelta.
        _cont(b"L___", b"trd ", [
            _chunk(b"trdh", _u32(0) + _str(meta.get("pista", ""))
                   + _f32(meta.get("largo_m") or 0.0) + _u32(len(secs))),
            _chunk(b"trds", _f32(*secs)),
        ]),
    ])
    laps = []
    for k, v in enumerate(vueltas):
        m = v["muestras"]
        s = list(v.get("sectores") or [0.0, 0.0, 0.0])[:3]
        s += [0.0] * (3 - len(s))
        cuerpo = [_chunk(b"laph", _u32(0, k, 0, len(m), n_val)),
                  _chunk(b"laps", _f32(v["tiempo"], *s))]
        for fila in m:
            # la muestra arranca con la distancia repetida como clave de orden
            cuerpo.append(_chunk(b"lapd", _f32(fila[1], *fila)))
        laps.append(_cont(b"L___", b"lap ", cuerpo))
    interno = _chunk(b"F___", b"TD  " + interno_hdr + _cont(b"L___", b"tdat", laps))

    comprimido = zlib.compress(interno, nivel)
    z = _chunk(b"Z___", b"F___TD  " + _u32(len(interno)) + b"zlib" + comprimido)
    datos = _chunk(b"F___", b"SRT " + cabecera + z)
    with open(destino, "wb") as f:
        f.write(datos)
    return destino, len(datos)


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
    # PERMUTACION DE EJES DE CUERPO. SRT ordena sus vectores de cuerpo como
    # [LONGITUDINAL, LATERAL, VERTICAL] y los nuestros van [lateral, vertical,
    # longitudinal] (accel_x es lateral: "accel_x>0 => cargan las derechas",
    # fijado por temperatura en 69 sesiones, ver docs/ESTADO.md). Mapear por
    # indice identico deja la G de FRENADO en el canal LATERAL, y como el visor
    # y analyze_telemetry deciden con accel_x que rueda carga cada curva, el
    # veredicto de toda sesion importada sale invertido -- sin que nada se vea
    # raro, porque posicion, pedales y temperaturas siguen bien.
    # Medido sobre el archivo real: gforce[0] corr -0.669 con (acelerador-freno)
    # = longitudinal; gforce[1] corr -0.873 con el volante = lateral; gforce[2]
    # con sd 0.03 = vertical.
    "accel_x": ("gforce", 1, 9.80665),
    "accel_y": ("gforce", 2, 9.80665),
    "accel_z": ("gforce", 0, 9.80665),
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
    # misma permutacion: velocity[0] es la longitudinal (12..56 m/s, o sea la
    # velocidad). Entre [1] y [2] la evidencia es mas debil (ambas chicas), pero
    # solo [1] CAMBIA DE SIGNO, y una velocidad lateral tiene que cambiarlo entre
    # curvas a izquierda y a derecha; ademas es lo consistente con el orden que
    # gforce y angular_vel si fijan de forma concluyente.
    "local_vx": ("velocity", 1, 1.0),
    # SIGNO OPUESTO: nuestro local_vz (mLocalVelocity[2]) es NEGATIVO hacia
    # adelante -- medido, -63.6..-17.8 m/s con el auto avanzando -- y el
    # velocity[0] de SRT es positivo. Sin el -1 el export le llega a la app con
    # la velocidad en reversa.
    "local_vz": ("velocity", 0, -1.0),
    # rotaciones alrededor de esos mismos ejes: SRT da [roll, pitch, yaw] y
    # nosotros guardamos [pitch, yaw, roll]. El yaw se identifico solo:
    # angular_vel[2] corr -0.963 con el volante.
    "ang_vel_x": ("angular_vel", 1, 1.0),
    "ang_vel_y": ("angular_vel", 2, 1.0),
    "ang_vel_z": ("angular_vel", 0, 1.0),
    "abs_active": ("abs", None, 1.0),
}
_MAPA_RUEDA = {
    "tyre_temp": ("tyre_temp", 1.0),
    "brake_temp": ("brake_temp", 1.0),
    "tyre_wear": ("tyre_wear", 1.0),
    "susp_travel": ("susp_pos", 1.0),
    "susp_vel": ("susp_vel", 1.0),
    "tyre_slip": ("tyre_slipSpeed", 1.0),
    # UNIDAD DISTINTA: nuestro ride_h esta en CENTIMETROS (6.7..12.2 en un GT4,
    # que en metros seria absurdo) y el ride_height de SRT en metros (0.04..0.16).
    "ride_h": ("ride_height", 100.0),
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


# Inverso de _MAPA: como se arma cada canal SRT desde NUESTRA traza.
# Lo que no tenemos va en 0.0 -- la app lo mostrara plano, que es honesto.
def _constructor(d, n, ctes=None):
    """Devuelve una funcion k -> lista de 155 floats en el orden de CANALES_SRT.

    `ctes` son valores constantes para canales que no estan en la traza pero si
    en el resumen de la vuelta (temperaturas de ambiente y pista).
    """
    ctes = ctes or {}
    g = lambda c: d.get(c) or [0.0] * n
    esc = {c: g(c) for c in ("t", "lap_dist", "rpm", "gear", "throttle", "brake",
                             "clutch", "steer", "steer_f", "fuel_l", "water_t",
                             "oil_t", "engine_torque", "abs_active", "max_rpm",
                             "pos_x", "pos_y", "pos_z", "accel_x", "accel_y",
                             "accel_z", "ang_vel_x", "ang_vel_y", "ang_vel_z",
                             "local_vx", "local_vz")}
    rue = {b: [g(f"{b}_{c}") for c in RUEDAS] for b in _MAPA_RUEDA}
    G = 9.80665

    def fila(k):
        f = []
        for nom, (inst, comp, *_r) in CANALES_SRT:
            if nom == "lap_number":
                f.append(1.0)
            elif nom == "lap_distance":
                f.append(esc["lap_dist"][k])
            elif nom == "lap_time":
                f.append(esc["t"][k])
            elif nom == "world_position":
                # SRT ordena [x, z, y]: el vertical es el TERCERO (ver _MAPA)
                f += [esc["pos_x"][k], esc["pos_z"][k], esc["pos_y"][k]]
            elif nom == "velocity":
                # inverso del signo de _MAPA: hacia adelante debe salir POSITIVO
                f += [-esc["local_vz"][k], esc["local_vx"][k], 0.0]
            elif nom == "gforce":
                # inverso de la permutacion de _MAPA: SRT espera [long, lat, vert]
                f += [esc["accel_z"][k] / G, esc["accel_x"][k] / G, esc["accel_y"][k] / G]
            elif nom == "angular_vel":
                f += [esc["ang_vel_z"][k], esc["ang_vel_x"][k], esc["ang_vel_y"][k]]
            elif nom == "throttle" or nom == "filteredThrottle":
                f.append(esc["throttle"][k])
            elif nom == "brake" or nom == "filteredBrake":
                f.append(esc["brake"][k])
            elif nom == "clutch" or nom == "filteredClutch":
                f.append(esc["clutch"][k])
            elif nom == "steering":
                f.append(esc["steer"][k])
            elif nom == "filteredSteering":
                f.append(esc["steer_f"][k])
            elif nom == "gear":
                f.append(esc["gear"][k])
            elif nom == "rpm":
                f.append(esc["rpm"][k])
            elif nom == "rpm_perc":
                mx = esc["max_rpm"][k]
                f.append(esc["rpm"][k] / mx if mx else 0.0)
            elif nom == "fuel":
                f.append(esc["fuel_l"][k])
            elif nom == "water_temp":
                f.append(esc["water_t"][k])
            elif nom == "oil_temp":
                f.append(esc["oil_t"][k])
            elif nom == "engine_torque":
                f.append(esc["engine_torque"][k])
            elif nom == "abs":
                f.append(esc["abs_active"][k])
            elif nom == "tyre_press":
                f += [rue["tyre_press"][j][k] * 1000.0 for j in range(4)]   # Bar x100 -> Pa
            elif nom in ("terrain", "tyre_rps", "tyre_slipSpeed", "tyre_temp",
                         "tyre_grip", "tyre_wear", "brake_temp", "ride_height",
                         "susp_pos", "susp_vel"):
                base = {"tyre_slipSpeed": "tyre_slip", "ride_height": "ride_h",
                        "susp_pos": "susp_travel"}.get(nom, nom)
                fac = 0.01 if nom == "ride_height" else 1.0   # cm -> m
                f += [rue[base][j][k] * fac for j in range(4)]
            elif nom == "tyre_tempCarcass":
                f += [rue["carcass_t"][j][k] for j in range(4)]
            elif nom == "tyre_tempLayer" or nom == "tyre_tempTread":
                f += [rue["layer_t"][j][k] for j in range(4)]
            elif nom == "tyre_isOnGround":
                # NO va en 0.0: cero significaria "las 4 ruedas en el aire toda
                # la vuelta", que la app del otro leeria como una medicion real.
                # Un hueco honesto aca no existe (el formato no lo tiene), asi
                # que se pone el valor casi siempre cierto en vez del casi
                # siempre falso.
                f += [1.0] * 4
            elif nom in ctes:
                f += [float(ctes[nom])] * (inst * comp)
            else:
                f += [0.0] * (inst * comp)
        return f
    return fila


def exportar(carpeta, destino, piloto="", max_vueltas=None):
    """Convierte una sesion NUESTRA a .srt para que la abra otro piloto.

    Devuelve (ruta, n_vueltas). Solo exporta vueltas con traza.
    """
    import time
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import analyze_telemetry as AT
    import ams2_analysis as A

    meta_s = A._meta(carpeta)
    vs = [v for v in A._vueltas(carpeta) if v.get("traza") and v.get("tiempo")]
    if max_vueltas:
        vs = sorted(vs, key=lambda v: v["tiempo"])[:max_vueltas]
    if not vs:
        raise SrtError("la sesion no tiene vueltas con traza")

    largo = meta_s.get("track_length_m") or 0.0
    # ambiente y pista viven en el resumen, no en la traza. Mandarlos en 0.0
    # seria decirle a la app que se corrio a 0 grados.
    ctes_por_uid = {}
    for r in AT._read_jsonl(os.path.join(carpeta, "summary.jsonl")):
        c = {}
        if r.get("ambient_t") is not None:
            c["ambient_temp"] = r["ambient_t"]
        if r.get("track_t") is not None:
            c["track_temp"] = r["track_t"]
        if r.get("rain") is not None:
            c["track_rain"] = r["rain"]
        if c:
            ctes_por_uid[r.get("uid")] = c
    vueltas = []
    for v in vs:
        d = AT._read_trace(os.path.join(carpeta, v["traza"]))
        n = len(d["lap_dist"])
        fila = _constructor(d, n, ctes=ctes_por_uid.get(v.get("uid"), {}))
        sec = list(v.get("sectores") or [])[:3]
        sec += [0.0] * (3 - len(sec))
        vueltas.append({"tiempo": v["tiempo"], "sectores": sec,
                        "sec_validos": [v.get("valida", True)] * 3,
                        "muestras": [fila(k) for k in range(n)]})
    ms = int(time.time() * 1000)
    meta = {"guid": os.urandom(16), "ts": ms, "ts_sesion": ms,
            "juego": "AMS2v1", "build": str(meta_s.get("build") or "3398"),
            "piloto": piloto or "Gian",
            "pista": (meta_s.get("track") or "").replace("_", " "),
            "largo_m": largo,
            "sectores_m": [largo / 3, largo * 2 / 3, largo],
            "auto": (meta_s.get("car") or "").replace("_", " ")}
    ruta, _ = escribir(destino, meta, vueltas)
    return ruta, len(vueltas)
