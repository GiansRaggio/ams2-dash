"""Entrega de una sesion a la escuela: valida, empaqueta y sube.

Existe porque a 20 alumnos "comprime la carpeta y mandala por Discord" no funciona
dos veces seguidas. Y el modo de falla es SILENCIOSO: el alumno cree que entrego,
el archivo nunca llego o llego roto, y se descubre dos clases despues -- cuando ya
perdio ese punto de su curva de progreso.

Por eso valida ANTES de subir y le dice al alumno en castellano que esta mal.
Descubrir la traza truncada al minuto sirve; descubrirla en el analisis, no.

Uso:
    python entrega.py                      # la ultima sesion
    python entrega.py <carpeta>            # una en particular
    python entrega.py --revisar            # solo valida, no sube
    python entrega.py --configurar         # deja guardados servidor y token

Ademas de la CLI, este modulo es una BIBLIOTECA: lo importa `entrega_cola.py` desde
un hilo de fondo del bridge. Por eso ninguna funcion de aca imprime por su cuenta
(reciben un callback `log=`), ninguna lee `sys.argv`, ninguna llama a `sys.exit` y
ninguna muta globals del modulo -- las globals son compartidas con la CLI y con los
tests, y mutarlas desde un hilo es una carrera esperando ocurrir.

Sin dependencias fuera de la stdlib: el alumno ya batallo instalando el dash.
"""
import gzip
import hashlib
import io
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TELEM = os.path.join(HERE, "telemetry")
CONFIG = os.path.join(HERE, "entrega.json")
REGISTRO = os.path.join(HERE, "telemetry", ".entregado.json")

MUESTRAS_MINIMAS = 1000        # bajo esto la traza esta truncada, no es una vuelta corta
REINTENTOS = 4
TIMEOUT_S = 120
MAX_MB = 60                    # el servidor corta en 64: queda margen para el manifiesto

# El registro local es un read-modify-write sobre un archivo compartido entre el hilo
# de la cola y la CLI. Sin este lock dos escrituras concurrentes se pisan y el
# historial de entregas desaparece en silencio (el lector se traga el ValueError).
_LOCK_REGISTRO = threading.Lock()


def _log_print(msg):
    print(msg)


# ---------------------------------------------------------------- configuracion
def cargar_config(ruta=None):
    """Servidor y token. El token identifica al alumno; se lo da la escuela.

    Devuelve None si falta lo esencial (url o token), NO un dict vacio: quien llama
    tiene que poder distinguir "sin configurar" de "configurado con la url en blanco".

    Se abre con utf-8-sig porque el alumno pega el token con el Notepad y el BOM que
    Notepad deja hace explotar json.load con encoding utf-8 -- y eso quedaba como
    "sin configurar" en silencio, sin que nadie entendiera por que. Mismo patron que
    read_player_name en ams2_shm.py.
    """
    p = ruta or CONFIG
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    url = str(cfg.get("url") or "").strip().rstrip("/")
    tok = str(cfg.get("token") or "").strip()
    if not url or not tok:
        return None
    cfg = dict(cfg)
    cfg["url"] = url
    cfg["token"] = tok
    # el nombre del alumno se escribio historicamente como "alumno"; el contrato del
    # dash lo llama "nombre". Se normalizan los dos para que nadie tenga que adivinar.
    nom = str(cfg.get("alumno") or cfg.get("nombre") or "").strip()
    cfg["alumno"] = cfg["nombre"] = nom
    cfg["auto"] = bool(cfg.get("auto", False))
    return cfg


def guardar_config(cfg, ruta=None):
    """Escritura atomica de entrega.json. La cola la usa para persistir `auto`."""
    p = ruta or CONFIG
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def configurar():
    cfg = cargar_config() or {}
    print("Configuracion de entrega (Enter deja lo que ya esta)\n")
    url = input(f"  Servidor [{cfg.get('url', '')}]: ").strip() or cfg.get("url", "")
    tok = input(f"  Tu token [{'*' * 8 if cfg.get('token') else ''}]: ").strip() or cfg.get("token", "")
    nom = input(f"  Tu nombre [{cfg.get('alumno', '')}]: ").strip() or cfg.get("alumno", "")
    if not url or not tok:
        print("\nFalta el servidor o el token. Pideselos a tu instructor.")
        return 1
    nuevo = {"url": url.rstrip("/"), "token": tok, "alumno": nom, "nombre": nom,
             "auto": bool(cfg.get("auto", False)),
             # `desde` es la linea de corte del barrido de arranque: lo que se grabo
             # ANTES de configurar es historial personal del alumno y no se sube solo.
             "desde": cfg.get("desde") or datetime.now().isoformat(timespec="seconds")}
    guardar_config(nuevo)
    print(f"\nListo, guardado en {CONFIG}")
    print("Ese archivo tiene tu token: no lo compartas ni lo subas a ningun lado.")
    return 0


# ---------------------------------------------------------------- validacion
def _agregar_tools_al_path():
    """Idempotente a proposito: esto vivia dentro de revisar() y metia una entrada
    NUEVA en sys.path en CADA llamada (medido: 3 llamadas -> 3 duplicados). En un
    proceso largo como el bridge eso crece sin techo y muta estado global compartido
    con todos los hilos."""
    tools = os.path.join(HERE, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)


def _vueltas(carpeta):
    p = os.path.join(carpeta, "summary.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("lap_time"):
                    out.append(r)
    except OSError:
        return []
    return out


def _muestras(ruta):
    """Filas de una traza .csv.gz, sin cargarla entera en memoria.

    Caza EOFError ademas de OSError: un .csv.gz a medio escribir tira 'Compressed
    file ended before the end-of-stream marker was reached', que es EOFError y NO es
    OSError. Sin esto la excepcion sube por revisar() y mata el hilo de la cola, que
    es justo el que lee carpetas recien cerradas.
    """
    try:
        with gzip.open(ruta, "rt", encoding="utf-8", errors="replace") as f:
            return max(0, sum(1 for _ in f) - 1)
    except (OSError, EOFError, ValueError):
        return -1


def _bytes_carpeta(carpeta):
    total = 0
    try:
        nombres = os.listdir(carpeta)
    except OSError:
        return 0
    for nombre in nombres:
        ruta = os.path.join(carpeta, nombre)
        try:
            if os.path.isfile(ruta):
                total += os.path.getsize(ruta)
        except OSError:
            continue
    return total


def revisar(carpeta):
    """{"problemas", "avisos", "resumen"}. `problemas` significa que no vale la pena subir."""
    problemas, avisos = [], []
    meta = {}
    p_meta = os.path.join(carpeta, "session.json")
    if os.path.exists(p_meta):
        try:
            with open(p_meta, encoding="utf-8-sig") as f:
                meta = json.load(f)
            if not isinstance(meta, dict):
                meta = {}
        except (OSError, ValueError):
            pass
    if not meta.get("track") or not meta.get("car"):
        problemas.append("la sesion no tiene datos de auto/pista: el dash no alcanzo a "
                         "leerlos del juego")

    vs = _vueltas(carpeta)
    if not vs:
        problemas.append("no hay ninguna vuelta cronometrada limpia en esta sesion")

    try:
        trazas = [f for f in os.listdir(carpeta) if f.endswith(".csv.gz")] if os.path.isdir(carpeta) else []
    except OSError:
        trazas = []

    # Modo full sin trazas: el summary dice que cada vuelta tenia su .csv.gz y no hay
    # ninguno. Antes esto pasaba la validacion y se subia una sesion vacia de
    # telemetria, porque la condicion de "todas truncadas" es falsa cuando no hay
    # ninguna. En modo summary `trace` viene None, asi que una sesion liviana
    # legitima no cae aca.
    if not trazas and any(v.get("trace") for v in vs):
        problemas.append("la sesion quedo sin ninguna traza: el dash no alcanzo a "
                         "guardarlas y no hay nada que analizar")

    truncadas = []
    for t in trazas:
        n = _muestras(os.path.join(carpeta, t))
        if n < 0:
            truncadas.append((t, 0))       # ilegible: a medio escribir o corrupta
        elif n < MUESTRAS_MINIMAS:
            truncadas.append((t, n))
    if truncadas and len(truncadas) == len(trazas):
        problemas.append(f"las {len(trazas)} trazas estan truncadas: el PC no dio abasto "
                         f"al grabar y los datos no sirven para analizar")
    elif truncadas:
        avisos.append(f"{len(truncadas)} de {len(trazas)} trazas quedaron truncadas "
                      f"(PC justo de CPU); esas vueltas se van a descartar")

    # El servidor corta en 64 MB y devuelve 413, que subir() no reintenta: mejor
    # decirselo al alumno antes de gastar 1 s de CPU empaquetando para nada.
    tam = _bytes_carpeta(carpeta)
    if tam > MAX_MB * 1024 * 1024:
        problemas.append(f"la sesion pesa {tam/1024/1024:.0f} MB y el limite son "
                         f"{MAX_MB} MB: avisale a tu instructor")

    # condiciones mezcladas: se sube igual, pero conviene que lo sepa
    try:
        _agregar_tools_al_path()
        import analyze_telemetry as AT
        _, av = AT.comparabilidad(carpeta)
        for a in av:
            if "CAMBIO" in a:
                avisos.append(a)
    except Exception:
        pass                                   # el analizador es opcional para entregar

    resumen = {"vueltas": len(vs), "trazas": len(trazas),
               "mejor_s": round(min(v["lap_time"] for v in vs), 3) if vs else None,
               "auto": meta.get("car"), "pista": meta.get("track"),
               "variante": meta.get("track_variation"),
               "pista_tr": meta.get("track_tr"), "sesion": meta.get("session"),
               "bytes": tam}
    return {"problemas": problemas, "avisos": avisos, "resumen": resumen}


def resumen_corto(resumen):
    """Una linea para la pantalla del alumno: '19 vueltas - mejor 1:28.728'."""
    n = resumen.get("vueltas") or 0
    txt = f"{n} vuelta{'s' if n != 1 else ''}"
    s = resumen.get("mejor_s")
    if s:
        txt += f" · mejor {int(s // 60)}:{s % 60:06.3f}"
    return txt


# ---------------------------------------------------------------- empaquetado
def _archivos(carpeta):
    """Snapshot ordenado de los archivos regulares. Un solo listdir: la identidad de
    la sesion y lo que va al zip tienen que salir de la MISMA lista."""
    out = []
    for nombre in sorted(os.listdir(carpeta)):
        ruta = os.path.join(carpeta, nombre)
        if os.path.isfile(ruta):
            out.append((nombre, ruta))
    return out


def huella(carpeta):
    """Identidad de la SESION: hash de los archivos que la componen.

    No se usa el hash del zip para esto. El manifiesto lleva la hora de
    empaquetado, asi que el zip sale distinto cada vez que se genera -- y el
    servidor veia cada reintento como una sesion nueva y la duplicaba. Esto paso
    en produccion con un test en verde: el test comparaba el LARGO de los dos zips
    (identico, porque el timestamp ocupa los mismos caracteres) en vez del hash.
    """
    h = hashlib.sha256()
    for nombre, ruta in _archivos(carpeta):
        h.update(nombre.encode("utf-8"))
        with open(ruta, "rb") as f:
            for bloque in iter(lambda: f.read(1 << 20), b""):
                h.update(bloque)
    return h.hexdigest()


def empaquetar(carpeta, alumno=""):
    """Zip en memoria con la sesion + un manifiesto.

    Devuelve (bytes, sha_zip, id_sesion, manifiesto). El manifiesto va DENTRO del
    zip a proposito: el servidor lo lee al desempaquetar y no hay dos fuentes de
    verdad que se puedan contradecir.

    UNA sola pasada de identidad: cada bloque de 1 MB alimenta al mismo tiempo el
    sha256 de la sesion y el zip, y el manifiesto se escribe al FINAL con la huella
    ya cerrada. Antes se hacian tres pasadas (huella para el manifiesto, lectura
    para el zip, huella otra vez para el header X-Sesion-Id) y si la carpeta crecia
    entre medio quedaban tres estados distintos: el header decia una cosa, el
    manifiesto embebido otra y los bytes del zip una tercera. Ahora el id_sesion es,
    por construccion, el hash de lo que efectivamente se manda.
    """
    resumen = revisar(carpeta)["resumen"]
    h = hashlib.sha256()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nombre, ruta in _archivos(carpeta):
            h.update(nombre.encode("utf-8"))
            zi = zipfile.ZipInfo.from_file(ruta, arcname=nombre)
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(ruta, "rb") as src, z.open(zi, "w") as dst:
                for bloque in iter(lambda: src.read(1 << 20), b""):
                    h.update(bloque)
                    dst.write(bloque)
        id_sesion = h.hexdigest()
        manifiesto = {
            "version": 1,
            "alumno": alumno,
            "carpeta": os.path.basename(os.path.normpath(carpeta)),
            "generado": datetime.now().isoformat(timespec="seconds"),
            "id_sesion": id_sesion,
            **resumen,
        }
        # al final y no al principio: el orden de entradas del zip da igual (el
        # servidor la busca por nombre) y asi la huella ya esta cerrada.
        z.writestr("manifiesto.json", json.dumps(manifiesto, ensure_ascii=False, indent=2))
    datos = buf.getvalue()
    return datos, hashlib.sha256(datos).hexdigest(), id_sesion, manifiesto


# ---------------------------------------------------------------- registro local
def registro():
    """Lectura tolerante: un archivo corrupto devuelve {} y nunca revienta."""
    if os.path.exists(REGISTRO):
        try:
            with open(REGISTRO, encoding="utf-8-sig") as f:
                r = json.load(f)
            if isinstance(r, dict):
                return r
        except (OSError, ValueError):
            pass
    return {}


def anotar(basename, info):
    """Registra una entrega. Se llavea por BASENAME de carpeta, no por huella: una
    carpeta que crecio no es una sesion nueva.

    Atomica (tmp + os.replace) y bajo lock. El truncate directo de antes dejaba el
    archivo vacio si el proceso moria entre el open("w") y el json.dump, y el lector
    se traga el ValueError: el historial de entregas completo desaparecia en
    silencio, que con semantica --todas significa re-subir todo.
    """
    with _LOCK_REGISTRO:
        reg = registro()
        reg[basename] = info
        tmp = REGISTRO + ".tmp"
        try:
            os.makedirs(os.path.dirname(REGISTRO), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(reg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, REGISTRO)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------- subida
def _id_respuesta(cuerpo):
    try:
        d = json.loads(cuerpo)
        return d.get("id") if isinstance(d, dict) else None
    except ValueError:
        return None


def subir(datos, sha, cfg, manifiesto, id_sesion=None, *,
          timeout=None, reintentos=None, log=None, cancelado=None):
    """POST con reintento y espera creciente.

    Devuelve {"ok", "duplicado", "status", "msg", "id"}.

    Manda DOS hashes: el del zip para que el servidor verifique que llego completo,
    y el de la sesion para que sepa si ya la tiene. El servidor responde 409 si ya
    la tenia: entregar dos veces no puede duplicarla ni contar como falla.

    `timeout` y `reintentos` son PARAMETROS con default a las globals. La cola baja
    el timeout a 60 s porque el peor caso de una red que cuelga (4 x 120 s) son ocho
    minutos de hilo pegado; antes tunearlos obligaba a mutar globals del modulo, que
    son compartidas con la CLI y con los tests.
    """
    log = log or _log_print
    timeout = TIMEOUT_S if timeout is None else timeout
    n_max = max(1, REINTENTOS if reintentos is None else reintentos)
    url = f"{cfg['url']}/v1/sesiones"
    espera = 2
    ultimo = ""
    ultimo_status = 0
    for intento in range(1, n_max + 1):
        if cancelado and cancelado():
            return {"ok": False, "duplicado": False, "status": 0,
                    "msg": "entrega cancelada", "id": None}
        req = urllib.request.Request(url, data=datos, method="POST")
        req.add_header("Authorization", f"Bearer {cfg['token']}")
        req.add_header("Content-Type", "application/zip")
        req.add_header("X-Sesion-Sha256", sha)
        req.add_header("X-Sesion-Id", id_sesion or manifiesto.get("id_sesion", ""))
        req.add_header("X-Sesion-Carpeta", manifiesto["carpeta"])
        try:
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                cuerpo = r.read().decode("utf-8", "replace")[:400]
                return {"ok": True, "duplicado": False, "status": r.status,
                        "msg": f"recibido ({r.status})", "id": _id_respuesta(cuerpo)}
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("utf-8", "replace")[:300]
            if e.code == 409:
                return {"ok": True, "duplicado": True, "status": 409,
                        "msg": "el servidor ya tenia esta sesion", "id": _id_respuesta(cuerpo)}
            if e.code in (401, 403):
                return {"ok": False, "duplicado": False, "status": e.code, "id": None,
                        "msg": ("tu token no es valido o expiro. Corre "
                                "`python entrega.py --configurar` con el que te dio la escuela")}
            if e.code == 413:
                return {"ok": False, "duplicado": False, "status": 413, "id": None,
                        "msg": f"la sesion es demasiado grande para el servidor: {cuerpo}"}
            if 400 <= e.code < 500:
                return {"ok": False, "duplicado": False, "status": e.code, "id": None,
                        "msg": f"el servidor rechazo la sesion ({e.code}): {cuerpo}"}
            ultimo = f"error del servidor ({e.code})"
            ultimo_status = e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            ultimo = f"no se pudo conectar ({e})"
            ultimo_status = 0
        if intento < n_max:
            log(f"    intento {intento} fallo: {ultimo}. Reintento en {espera}s...")
            # el sleep se corta en tramos de 0.25 s para que `cancelado` no tenga que
            # esperar los 8 s del ultimo backoff antes de que el hilo se entere
            fin = time.time() + espera
            while True:
                resto = fin - time.time()
                if resto <= 0:
                    break
                if cancelado and cancelado():
                    return {"ok": False, "duplicado": False, "status": ultimo_status,
                            "msg": "entrega cancelada", "id": None}
                time.sleep(min(0.25, resto))
            espera *= 2
    return {"ok": False, "duplicado": False, "status": ultimo_status, "id": None,
            "msg": f"{ultimo} despues de {n_max} intentos"}


# ---------------------------------------------------------------- sesiones
def sesiones(base_dir=None):
    base = base_dir or TELEM
    if not os.path.isdir(base):
        return []
    try:
        ds = [os.path.join(base, d) for d in os.listdir(base)
              if os.path.isdir(os.path.join(base, d))]
    except OSError:
        return []
    try:
        return sorted(ds, key=os.path.getmtime)
    except OSError:                    # una carpeta que se borro a mitad del sort
        return sorted(ds)


def _informe(carpeta, problemas, avisos, resumen, log=None):
    log = log or _log_print
    log(f"\n  Sesion: {os.path.basename(os.path.normpath(carpeta))}")
    if resumen.get("auto"):
        linea = (f"  {resumen.get('pista_tr') or resumen.get('pista')} · {resumen['auto']}"
                 f" · {resumen['vueltas']} vueltas limpias")
        if resumen.get("mejor_s"):
            linea += f" · mejor {resumen['mejor_s']:.3f}s"
        log(linea)
    for p in problemas:
        log(f"  [!] {p}")
    for a in avisos:
        log(f"  [ojo] {a}")
    if not problemas and not avisos:
        log("  todo en orden")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Entrega una sesion a la escuela.")
    ap.add_argument("carpeta", nargs="?", help="carpeta de sesion (por defecto, la ultima)")
    ap.add_argument("--revisar", action="store_true", help="solo valida, no sube nada")
    ap.add_argument("--configurar", action="store_true", help="guarda servidor y token")
    ap.add_argument("--todas", action="store_true", help="entrega todas las que falten")
    a = ap.parse_args()

    if a.configurar:
        return configurar()

    todas = sesiones()
    if not todas:
        print(f"No hay sesiones en {TELEM}. Maneja con el dash grabando y vuelve.")
        return 1

    if a.todas:
        reg = registro()
        objetivo = [c for c in todas if os.path.basename(os.path.normpath(c)) not in reg]
        if not objetivo:
            print("No hay sesiones pendientes de entregar.")
            return 0
    else:
        objetivo = [a.carpeta or todas[-1]]

    cfg = cargar_config()
    if not a.revisar and not cfg:
        print("Todavia no configuraste la entrega.")
        print("Corre:  python entrega.py --configurar")
        return 1

    fallos = 0
    for carpeta in objetivo:
        if not os.path.isdir(carpeta):
            print(f"\n  No encuentro la carpeta: {carpeta}")
            fallos += 1
            continue
        rev = revisar(carpeta)
        problemas, avisos, resumen = rev["problemas"], rev["avisos"], rev["resumen"]
        _informe(carpeta, problemas, avisos, resumen)
        if problemas:
            print("  --> no se entrega: los datos no sirven para analizar.")
            print("      Avisale a tu instructor si esto se repite.")
            fallos += 1
            continue
        if a.revisar:
            continue
        datos, sha, id_sesion, manifiesto = empaquetar(carpeta, cfg.get("alumno", ""))
        print(f"  subiendo {len(datos)/1024/1024:.1f} MB...")
        res = subir(datos, sha, cfg, manifiesto, id_sesion)
        if res["ok"]:
            try:
                anotar(os.path.basename(os.path.normpath(carpeta)),
                       {"id_sesion": id_sesion,
                        "cuando": datetime.now().isoformat(timespec="seconds"),
                        "estado": "duplicado" if res["duplicado"] else "ok",
                        "bytes": len(datos)})
            except OSError as e:
                print(f"  [ojo] no pude anotar la entrega en el registro: {e}")
            print(f"  --> entregada: {res['msg']}")
        else:
            print(f"  --> NO se pudo entregar: {res['msg']}")
            print("      La sesion sigue en tu PC; se puede reintentar despues.")
            fallos += 1
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
