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

Sin dependencias fuera de la stdlib: el alumno ya batallo instalando el dash.
"""
import gzip
import hashlib
import io
import json
import os
import ssl
import sys
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


# ---------------------------------------------------------------- configuracion
def cargar_config():
    """Servidor y token. El token identifica al alumno; se lo da la escuela."""
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def configurar():
    cfg = cargar_config()
    print("Configuracion de entrega (Enter deja lo que ya esta)\n")
    url = input(f"  Servidor [{cfg.get('url', '')}]: ").strip() or cfg.get("url", "")
    tok = input(f"  Tu token [{'*' * 8 if cfg.get('token') else ''}]: ").strip() or cfg.get("token", "")
    nom = input(f"  Tu nombre [{cfg.get('alumno', '')}]: ").strip() or cfg.get("alumno", "")
    if not url or not tok:
        print("\nFalta el servidor o el token. Pideselos a tu instructor.")
        return 1
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump({"url": url.rstrip("/"), "token": tok, "alumno": nom}, f, indent=2)
    print(f"\nListo, guardado en {CONFIG}")
    print("Ese archivo tiene tu token: no lo compartas ni lo subas a ningun lado.")
    return 0


# ---------------------------------------------------------------- validacion
def _vueltas(carpeta):
    p = os.path.join(carpeta, "summary.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("lap_time"):
                out.append(r)
    return out


def _muestras(ruta):
    """Filas de una traza .csv.gz, sin cargarla entera en memoria."""
    try:
        with gzip.open(ruta, "rt", encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)
    except OSError:
        return -1


def revisar(carpeta):
    """(problemas, avisos, resumen). `problemas` significa que no vale la pena subir."""
    problemas, avisos = [], []
    meta = {}
    p_meta = os.path.join(carpeta, "session.json")
    if os.path.exists(p_meta):
        try:
            with open(p_meta, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            pass
    if not meta.get("track") or not meta.get("car"):
        problemas.append("la sesion no tiene datos de auto/pista: el dash no alcanzo a "
                         "leerlos del juego")

    vs = _vueltas(carpeta)
    if not vs:
        problemas.append("no hay ninguna vuelta cronometrada limpia en esta sesion")

    trazas = [f for f in os.listdir(carpeta) if f.endswith(".csv.gz")] if os.path.isdir(carpeta) else []
    truncadas = []
    for t in trazas:
        n = _muestras(os.path.join(carpeta, t))
        if 0 <= n < MUESTRAS_MINIMAS:
            truncadas.append((t, n))
    if truncadas and len(truncadas) == len(trazas):
        problemas.append(f"las {len(trazas)} trazas estan truncadas: el PC no dio abasto "
                         f"al grabar y los datos no sirven para analizar")
    elif truncadas:
        avisos.append(f"{len(truncadas)} de {len(trazas)} trazas quedaron truncadas "
                      f"(PC justo de CPU); esas vueltas se van a descartar")

    # condiciones mezcladas: se sube igual, pero conviene que lo sepa
    try:
        sys.path.insert(0, os.path.join(HERE, "tools"))
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
               "pista_tr": meta.get("track_tr"), "sesion": meta.get("session")}
    return problemas, avisos, resumen


# ---------------------------------------------------------------- empaquetado
def empaquetar(carpeta, alumno=""):
    """Zip en memoria con la sesion + un manifiesto. Devuelve (bytes, sha256, manifiesto).

    El manifiesto va DENTRO del zip a proposito: el servidor lo lee al desempaquetar
    y no hay dos fuentes de verdad que se puedan contradecir.
    """
    _, _, resumen = revisar(carpeta)
    manifiesto = {
        "version": 1,
        "alumno": alumno,
        "carpeta": os.path.basename(os.path.normpath(carpeta)),
        "generado": datetime.now().isoformat(timespec="seconds"),
        **resumen,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifiesto.json", json.dumps(manifiesto, ensure_ascii=False, indent=2))
        for nombre in sorted(os.listdir(carpeta)):
            ruta = os.path.join(carpeta, nombre)
            if os.path.isfile(ruta):
                z.write(ruta, arcname=nombre)
    datos = buf.getvalue()
    return datos, hashlib.sha256(datos).hexdigest(), manifiesto


# ---------------------------------------------------------------- registro local
def _registro():
    if os.path.exists(REGISTRO):
        try:
            with open(REGISTRO, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def _anotar(carpeta, sha, respuesta):
    reg = _registro()
    reg[os.path.basename(os.path.normpath(carpeta))] = {
        "sha256": sha, "cuando": datetime.now().isoformat(timespec="seconds"),
        "respuesta": respuesta}
    try:
        with open(REGISTRO, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- subida
def subir(datos, sha, cfg, manifiesto):
    """POST con reintento y espera creciente. Devuelve (ok, mensaje).

    El servidor responde 409 si ya tiene ese sha: entregar dos veces la misma sesion
    no puede duplicarla ni contar como falla.
    """
    url = f"{cfg['url']}/v1/sesiones"
    espera = 2
    ultimo = ""
    for intento in range(1, REINTENTOS + 1):
        req = urllib.request.Request(url, data=datos, method="POST")
        req.add_header("Authorization", f"Bearer {cfg['token']}")
        req.add_header("Content-Type", "application/zip")
        req.add_header("X-Sesion-Sha256", sha)
        req.add_header("X-Sesion-Carpeta", manifiesto["carpeta"])
        try:
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=ctx) as r:
                cuerpo = r.read().decode("utf-8", "replace")[:400]
                return True, f"recibido ({r.status})"
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("utf-8", "replace")[:300]
            if e.code == 409:
                return True, "el servidor ya tenia esta sesion"
            if e.code in (401, 403):
                return False, ("tu token no es valido o expiro. Corre "
                               "`python entrega.py --configurar` con el que te dio la escuela")
            if 400 <= e.code < 500:
                return False, f"el servidor rechazo la sesion ({e.code}): {cuerpo}"
            ultimo = f"error del servidor ({e.code})"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            ultimo = f"no se pudo conectar ({e})"
        if intento < REINTENTOS:
            print(f"    intento {intento} fallo: {ultimo}. Reintento en {espera}s...")
            time.sleep(espera)
            espera *= 2
    return False, f"{ultimo} despues de {REINTENTOS} intentos"


# ---------------------------------------------------------------- sesiones
def sesiones():
    if not os.path.isdir(TELEM):
        return []
    ds = [os.path.join(TELEM, d) for d in os.listdir(TELEM)
          if os.path.isdir(os.path.join(TELEM, d))]
    return sorted(ds, key=os.path.getmtime)


def _informe(carpeta, problemas, avisos, resumen):
    print(f"\n  Sesion: {os.path.basename(os.path.normpath(carpeta))}")
    if resumen.get("auto"):
        print(f"  {resumen.get('pista_tr') or resumen.get('pista')} · {resumen['auto']}"
              f" · {resumen['vueltas']} vueltas limpias", end="")
        print(f" · mejor {resumen['mejor_s']:.3f}s" if resumen.get("mejor_s") else "")
    for p in problemas:
        print(f"  [!] {p}")
    for a in avisos:
        print(f"  [ojo] {a}")
    if not problemas and not avisos:
        print("  todo en orden")


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
        reg = _registro()
        objetivo = [c for c in todas if os.path.basename(os.path.normpath(c)) not in reg]
        if not objetivo:
            print("No hay sesiones pendientes de entregar.")
            return 0
    else:
        objetivo = [a.carpeta or todas[-1]]

    cfg = cargar_config()
    if not a.revisar and (not cfg.get("url") or not cfg.get("token")):
        print("Todavia no configuraste la entrega.")
        print("Corre:  python entrega.py --configurar")
        return 1

    fallos = 0
    for carpeta in objetivo:
        if not os.path.isdir(carpeta):
            print(f"\n  No encuentro la carpeta: {carpeta}")
            fallos += 1
            continue
        problemas, avisos, resumen = revisar(carpeta)
        _informe(carpeta, problemas, avisos, resumen)
        if problemas:
            print("  --> no se entrega: los datos no sirven para analizar.")
            print("      Avisale a tu instructor si esto se repite.")
            fallos += 1
            continue
        if a.revisar:
            continue
        datos, sha, manifiesto = empaquetar(carpeta, cfg.get("alumno", ""))
        print(f"  subiendo {len(datos)/1024/1024:.1f} MB...")
        ok, msg = subir(datos, sha, cfg, manifiesto)
        if ok:
            _anotar(carpeta, sha, msg)
            print(f"  --> entregada: {msg}")
        else:
            print(f"  --> NO se pudo entregar: {msg}")
            print("      La sesion sigue en tu PC; se puede reintentar despues.")
            fallos += 1
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
