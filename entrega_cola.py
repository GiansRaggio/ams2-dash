"""Bandeja de entrega: la cola que empaqueta y sube sesiones sin molestar al dash.

El modelo es BANDEJA, no subida automatica: el bridge detecta que una sesion cerro,
esta cola la valida y la deja "lista", y el alumno la entrega con un toque desde el
dash. Hay un switch `auto` (opt-in explicito, default false) que sube todo lo listo
solo. Nunca al reves: subir sin preguntar la carrera privada del jueves de alguien
no se puede deshacer, y lo que se pierde no es una sesion, es la confianza del curso.

Tres condiciones NO negociables, porque este hilo vive dentro del proceso del dash
que sirve estado a 30 Hz:

  1. Nunca corre en el event loop: todo el trabajo pesado esta en UN hilo daemon.
  2. Nunca toma el lock del TelemetryLogger: se comunica por `encolar()` y por
     accesores baratos que el bridge le pasa como callables.
  3. Nunca hace CPU con el jugador en pista. `empaquetar` de una sesion de 19 MB se
     lleva el GIL ~1.4 s (medido en este PC); el repo ya documenta que la presion
     del dash tira el juego de 160 a 60 fps. Si `en_pista()` da True, se espera.

Y una cuarta que no es negociable por otra razon: `status()` se llama 30 veces por
segundo desde el broadcast, asi que NO hace I/O. Devuelve una copia de un dict que
el hilo mantiene precomputado bajo lock.
"""
import copy
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:                  # idempotente: el bridge nos importa una vez
    sys.path.insert(0, _HERE)
import entrega as E                                              # noqa: E402

try:
    import msvcrt                                                # noqa: F401
except ImportError:                     # el dash es Windows-only; en Linux corren los tests
    msvcrt = None

QUIESCENCIA_S = 120        # dos minutos muda antes de considerarla cerrada
REENCOLAR_S = 60           # si el gate de quiescencia falla, se reintenta a esto
BACKOFF_RED_S = 600        # tras agotar los reintentos de subir(): 10 min, no 14 s
ESPERA_PISTA_S = 5         # con el jugador en pista no se hace nada pesado
CHEQUEO_CFG_S = 30         # cada cuanto se relee entrega.json cuando algo falta
TIMEOUT_SUBIDA_S = 60      # 4 x 120 s del default serian 8 min de hilo pegado
DIAS_BARRIDO = 14          # el barrido de arranque no mira mas atras que esto
TOPE_BARRIDO = 20          # ni encola mas que esto en la primera pasada
NOMBRE_LOCK = ".entrega.lock"


def _norm(p):
    return os.path.normcase(os.path.abspath(p)) if p else None


class ColaEntrega:
    """Cola FIFO serial con un solo hilo daemon. Ver el docstring del modulo."""

    def __init__(self, base_dir, sess_dir_actual=None, en_pista=None, log=None, cfg_path=None):
        self.base_dir = base_dir
        self._sess_dir_actual = sess_dir_actual or (lambda: None)
        self._en_pista = en_pista or (lambda: False)
        self._log_cb = log or (lambda m: print(m))
        self.cfg_path = cfg_path or E.CONFIG

        # tunables por instancia: los tests los bajan sin tocar las globals del modulo
        self.quiescencia_s = QUIESCENCIA_S
        self.reencolar_s = REENCOLAR_S
        self.backoff_red_s = BACKOFF_RED_S
        self.espera_pista_s = ESPERA_PISTA_S
        self.chequeo_cfg_s = CHEQUEO_CFG_S
        self.timeout_subida_s = TIMEOUT_SUBIDA_S
        self.reintentos_subida = None      # None = el default de entrega.REINTENTOS
        self.dias_barrido = DIAS_BARRIDO
        self.tope_barrido = TOPE_BARRIDO
        self.periodo_s = 1.0

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._hilo = None
        self._fh_lock = None            # el file object del lockfile: hay que sostenerlo
        self._inerte = False            # otro dash ya tiene el lockfile

        self._orden = []                # basenames en orden FIFO
        self._items = {}                # basename -> dict de estado del item
        self._cfg = None
        self._cfg_mtime = None
        self._token_malo = False
        self._ultimo_chequeo_cfg = 0.0
        self._barrido_hecho = False
        self._subiendo = None
        self._ultima = None
        self._snap = None
        self._recalcular()

    # ------------------------------------------------------------------ util
    def log(self, msg):
        try:
            self._log_cb(f"[entrega] {msg}")
        except Exception:
            pass                        # un log que revienta no puede matar la cola

    # ------------------------------------------------------------------ API
    def start(self):
        """Arranca el hilo daemon. Toma el lockfile primero: dos dashes (o un dash y
        un `python entrega.py` a mano) subiendo la misma sesion al mismo tiempo es el
        TOCTOU que tumba al servidor. El guard por netstat de start-dash.bat es una
        carrera y ademas no cubre el caso de la CLI."""
        with self._lock:
            if self._hilo is not None:
                return
            if not self._tomar_lock():
                self._inerte = True
                self.log("otra instancia ya tiene la bandeja; esta queda inerte")
                self._recalcular()
                return
            self._hilo = threading.Thread(target=self._loop, name="entrega", daemon=True)
        self._hilo.start()

    def stop(self):
        """Solo para los tests y para un cierre ordenado: el hilo es daemon, no hace
        falta pararlo para que el proceso muera."""
        self._stop.set()
        h = self._hilo
        if h is not None:
            h.join(timeout=5)
        self._soltar_lock()

    def encolar(self, carpeta):
        """Thread-safe. La llama el bridge al rotar una sesion: SOLO encola, el
        trabajo pesado es del hilo."""
        if not carpeta:
            return
        base = os.path.basename(os.path.normpath(carpeta))
        with self._lock:
            it = self._items.get(base)
            if it is not None:
                if it["estado"] in ("entregada",):
                    return
                it["carpeta"] = carpeta
                it["proximo"] = 0.0
                if it["estado"] == "invalida":
                    it["estado"] = "pendiente"   # crecio: vale la pena revisarla de nuevo
                self._recalcular()
                return
            self._items[base] = {"carpeta": carpeta, "base": base, "estado": "pendiente",
                                 "motivo": "", "resumen": "", "bytes": 0, "proximo": 0.0,
                                 "forzada": False, "msg": ""}
            self._orden.append(base)
            self._recalcular()

    def entregar_ahora(self, carpeta=None):
        """carpeta=None -> todas las listas. Marca las sesiones como forzadas, asi
        suben aunque `auto` este apagado (que es el caso normal: el alumno apreto el
        boton)."""
        with self._lock:
            if carpeta:
                base = os.path.basename(os.path.normpath(carpeta))
                it = self._items.get(base)
                if it is None:
                    ruta = carpeta if os.path.isdir(carpeta) else os.path.join(self.base_dir, base)
                    self.encolar(ruta)
                    it = self._items.get(base)
                objetivo = [it] if it else []
            else:
                objetivo = [self._items[b] for b in self._orden
                            if self._items[b]["estado"] in ("pendiente", "lista")]
            for it in objetivo:
                if it["estado"] in ("lista", "pendiente", "error"):
                    it["forzada"] = True
                    it["proximo"] = 0.0
                    if it["estado"] == "error":
                        it["estado"] = "lista"
            self._recalcular()

    def set_auto(self, on):
        """Persiste el switch en entrega.json. Si todavia no hay config, se guarda
        igual: el alumno puede prender `auto` antes de pegar el token."""
        on = bool(on)
        with self._lock:
            raw = self._leer_cfg_raw()
            raw["auto"] = on
            try:
                E.guardar_config(raw, self.cfg_path)
            except OSError as e:
                self.log(f"no pude guardar el switch auto: {e}")
                return
            self._cfg_mtime = self._mtime_cfg()
            if self._cfg is not None:
                self._cfg["auto"] = on
            self.log(f"auto = {on}")
            self._recalcular()

    def recargar_config(self):
        """Relee entrega.json. La llama el dash despues de que el alumno pega el
        token: es lo que saca a la cola del estado pegajoso `token`."""
        with self._lock:
            antes = self._cfg
            self._cfg = E.cargar_config(self.cfg_path)
            self._cfg_mtime = self._mtime_cfg()
            self._ultimo_chequeo_cfg = time.time()
            if self._cfg is not None and (self._token_malo or antes is None):
                self._token_malo = False
                for b in self._orden:                 # otra credencial merece otro intento
                    it = self._items[b]
                    if it["estado"] in ("error",):
                        it["estado"] = "lista"
                    it["proximo"] = 0.0
            self._recalcular()
        return self._cfg is not None

    def status(self):
        """COPIA de un dict precomputado. CERO I/O: esto viaja dentro del state que
        el bridge emite a 30 Hz."""
        with self._lock:
            return copy.deepcopy(self._snap)

    # ------------------------------------------------------------------ lockfile
    def _tomar_lock(self):
        ruta = os.path.join(self.base_dir, NOMBRE_LOCK)
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            fh = open(ruta, "a+b")
        except OSError as e:
            self.log(f"no pude abrir el lockfile ({e}); sigo sin el")
            return True                 # sin lockfile es peor no arrancar que arrancar
        if msvcrt is None:
            self._fh_lock = fh          # fuera de Windows queda como marca, sin bloqueo
            return True
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return False
        self._fh_lock = fh
        return True

    def _soltar_lock(self):
        fh, self._fh_lock = self._fh_lock, None
        if fh is None:
            return
        try:
            if msvcrt is not None:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ hilo
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                # el hilo NO se muere por un item malo: si se muere, el alumno pierde
                # la bandeja entera y no se entera nunca
                self.log("error inesperado en el ciclo:\n" + traceback.format_exc())
            self._stop.wait(self.periodo_s)

    def _tick(self):
        if not self._asegurar_config():
            return
        if self._token_malo:
            return
        if not self._barrido_hecho:
            self._barrido()
            self._barrido_hecho = True
        it = self._proximo_item()
        if it is None:
            with self._lock:
                self._recalcular()
            return
        if self._en_pista():
            with self._lock:
                it["proximo"] = time.time() + self.espera_pista_s
                self._recalcular()
            return
        try:
            if it["estado"] == "pendiente":
                self._revisar_item(it)
            else:
                self._subir_item(it)
        except Exception:
            self.log(f"{it['base']}: fallo procesando\n" + traceback.format_exc())
            with self._lock:
                it["estado"] = "error"
                it["msg"] = "algo fallo al preparar la sesion"
                it["proximo"] = time.time() + self.backoff_red_s
                self._recalcular()

    def _asegurar_config(self):
        """Devuelve True si hay config utilizable. Relee entrega.json cada 30 s
        mientras falte o mientras el token este marcado como malo: es la unica forma
        de que el alumno salga del pozo pegando un token nuevo."""
        ahora = time.time()
        with self._lock:
            necesita = self._cfg is None or self._token_malo
            if not necesita:
                return True
            if ahora - self._ultimo_chequeo_cfg < self.chequeo_cfg_s:
                return False
        # el mtime cambio => el alumno toco el archivo => vale la pena releer
        mt = self._mtime_cfg()
        with self._lock:
            self._ultimo_chequeo_cfg = ahora
            if self._token_malo and mt == self._cfg_mtime:
                return False
        self.recargar_config()
        with self._lock:
            return self._cfg is not None and not self._token_malo

    def _proximo_item(self):
        ahora = time.time()
        auto = bool((self._cfg or {}).get("auto"))
        with self._lock:
            for b in self._orden:
                it = self._items[b]
                if it["proximo"] > ahora:
                    continue
                if it["estado"] == "pendiente":
                    return it
                if it["estado"] == "lista" and (auto or it["forzada"]):
                    return it
        return None

    # ------------------------------------------------------------------ pasos
    def _revisar_item(self, it):
        carpeta = it["carpeta"]
        if not os.path.isdir(carpeta):
            with self._lock:
                it["estado"] = "invalida"
                it["motivo"] = "la carpeta ya no esta en el PC"
                self._recalcular()
            return
        rev = E.revisar(carpeta)
        with self._lock:
            it["resumen"] = E.resumen_corto(rev["resumen"])
            it["bytes"] = rev["resumen"].get("bytes") or 0
            if rev["problemas"]:
                it["estado"] = "invalida"
                it["motivo"] = rev["problemas"][0]
                self.log(f"{it['base']}: no sirve para entregar -- {it['motivo']}")
            else:
                it["estado"] = "lista"
                self.log(f"{it['base']}: lista para entregar ({it['resumen']})")
            self._recalcular()

    def _quieta(self, carpeta):
        """Gate de quiescencia. Dos condiciones independientes; el reloj es el mtime
        del disco, que ya existe, y no un contador nuevo dentro del logger."""
        actual = _norm(self._sess_dir_actual())
        if actual and actual == _norm(carpeta):
            return False
        try:
            nombres = os.listdir(carpeta)
        except OSError:
            return False
        ult = 0.0
        for n in nombres:
            try:
                ult = max(ult, os.path.getmtime(os.path.join(carpeta, n)))
            except OSError:
                continue
        try:
            ult = max(ult, os.path.getmtime(carpeta))
        except OSError:
            pass
        return (time.time() - ult) > self.quiescencia_s

    def _subir_item(self, it):
        carpeta = it["carpeta"]
        if not self._quieta(carpeta):
            with self._lock:
                it["proximo"] = time.time() + self.reencolar_s
                self._recalcular()
            return
        if it["base"] in E.registro():
            with self._lock:
                it["estado"] = "entregada"
                it["msg"] = "ya estaba entregada"
                self._recalcular()
            return
        cfg = self._cfg
        with self._lock:
            it["estado"] = "subiendo"
            self._subiendo = {"carpeta": it["base"],
                              "desde": datetime.now().isoformat(timespec="seconds")}
            self._recalcular()
        try:
            datos, sha, id_sesion, man = E.empaquetar(carpeta, cfg.get("alumno", ""))
            res = E.subir(datos, sha, cfg, man, id_sesion,
                          timeout=self.timeout_subida_s, reintentos=self.reintentos_subida,
                          log=self.log,
                          cancelado=self._stop.is_set)
        finally:
            with self._lock:
                self._subiendo = None
        self._resolver(it, res, len(datos), id_sesion)

    def _resolver(self, it, res, n_bytes, id_sesion=""):
        ahora = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            if res["ok"]:
                it["estado"] = "entregada"
                it["msg"] = res["msg"]
                it["forzada"] = False
                self._ultima = {"carpeta": it["base"], "cuando": ahora,
                                "ok": True,
                                "msg": "ya estaba en la escuela" if res["duplicado"]
                                       else "entregada"}
                self.log(f"{it['base']}: {res['msg']}")
                try:
                    E.anotar(it["base"], {"id_sesion": id_sesion,
                                          "cuando": ahora,
                                          "estado": "duplicado" if res["duplicado"] else "ok",
                                          "bytes": n_bytes})
                except OSError as e:
                    self.log(f"{it['base']}: entregada pero no pude anotarla ({e})")
            elif res["status"] in (401, 403):
                # pegajoso: 12 sesiones x 4 intentos con un token malo es puro ruido
                self._token_malo = True
                self._cfg_mtime = self._mtime_cfg()
                self._ultimo_chequeo_cfg = time.time()
                it["estado"] = "lista"
                it["msg"] = res["msg"]
                self._ultima = {"carpeta": it["base"], "cuando": ahora, "ok": False,
                                "msg": "revisa tu credencial"}
                self.log("credencial rechazada: la bandeja queda detenida hasta que "
                         "cambie entrega.json")
            elif res["status"] == 413:
                it["estado"] = "invalida"
                it["motivo"] = "la sesion es demasiado grande para el servidor"
                self._ultima = {"carpeta": it["base"], "cuando": ahora, "ok": False,
                                "msg": it["motivo"]}
            else:
                it["estado"] = "lista"
                it["msg"] = res["msg"]
                it["proximo"] = time.time() + self.backoff_red_s
                self._ultima = {"carpeta": it["base"], "cuando": ahora, "ok": False,
                                "msg": "no pude conectarme con la escuela; lo reintento solo"}
                self.log(f"{it['base']}: {res['msg']} -- reintento en "
                         f"{int(self.backoff_red_s / 60)} min")
            self._recalcular()

    # ------------------------------------------------------------------ barrido
    def _barrido(self):
        """Sesiones viejas que nadie encolo: la ultima del dia no rota nunca (el
        jugador sale al menu y ya), asi que sin este barrido no se entrega jamas."""
        reg = E.registro()
        desde = self._ts_desde()
        corte = time.time() - self.dias_barrido * 86400
        actual = _norm(self._sess_dir_actual())
        cand = []
        for ruta in E.sesiones(self.base_dir):
            base = os.path.basename(os.path.normpath(ruta))
            if base in reg or base in self._items:
                continue
            if _norm(ruta) == actual:
                continue
            try:
                mt = os.path.getmtime(ruta)
            except OSError:
                continue
            if mt < corte or (desde is not None and mt < desde):
                continue
            if not self._quieta(ruta):
                continue
            cand.append(ruta)
        cand = cand[-self.tope_barrido:]        # sesiones() ordena por mtime asc
        for ruta in cand:
            self.encolar(ruta)
        if cand:
            self.log(f"barrido de arranque: {len(cand)} sesion(es) sin entregar")

    def _ts_desde(self):
        d = (self._cfg or {}).get("desde")
        if not d:
            return None
        try:
            return datetime.fromisoformat(str(d)).timestamp()
        except ValueError:
            return None

    # ------------------------------------------------------------------ config
    def _mtime_cfg(self):
        try:
            return os.path.getmtime(self.cfg_path)
        except OSError:
            return None

    def _leer_cfg_raw(self):
        """El json crudo, sin la validacion de cargar_config: set_auto tiene que
        poder escribir el switch aunque todavia no haya token."""
        try:
            with open(self.cfg_path, encoding="utf-8-sig") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    # ------------------------------------------------------------------ snapshot
    def _recalcular(self):
        """Reconstruye el dict que devuelve status(). Se llama SIEMPRE bajo _lock y
        siempre desde el hilo o desde una API publica: nunca desde el event loop."""
        listas, invalidas, pendientes = [], [], 0
        for b in self._orden:
            it = self._items[b]
            if it["estado"] == "lista":
                listas.append({"carpeta": b, "resumen": it["resumen"], "bytes": it["bytes"]})
                pendientes += 1
            elif it["estado"] in ("pendiente", "subiendo"):
                pendientes += 1
            elif it["estado"] == "invalida":
                invalidas.append({"carpeta": b, "motivo": it["motivo"]})

        auto = bool((self._cfg or {}).get("auto"))
        if self._inerte:
            estado, msg = "off", "otro dash tiene la bandeja abierta"
        elif self._cfg is None:
            estado, msg = "sin_config", "todavia no configuraste la entrega"
        elif self._token_malo:
            estado, msg = "token", "revisa tu credencial: la escuela no la acepto"
        elif self._subiendo:
            estado, msg = "subiendo", "entregando tu sesion..."
        elif listas:
            estado = "listas"
            msg = (f"{len(listas)} sesiones listas para entregar" if len(listas) > 1
                   else "1 sesion lista para entregar")
        else:
            estado = "idle"
            if self._ultima and self._ultima["ok"]:
                msg = f"{self._ultima['msg']} a las {self._ultima['cuando'][11:16]}"
            elif self._ultima:
                msg = self._ultima["msg"]
            else:
                msg = "no hay nada pendiente"
        if auto and estado in ("idle", "listas"):
            msg += " · auto"

        self._snap = {"estado": estado, "auto": auto, "pendientes": pendientes,
                      "listas": listas, "subiendo": self._subiendo,
                      "ultima": self._ultima, "invalidas": invalidas, "msg": msg}
