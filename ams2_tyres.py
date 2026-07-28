#!/usr/bin/env python3
"""Analizador de gomas para AMS2 (sincronico, alimentado del bridge).

Cubre lo que el bloque de gomas del director de estrategia NO cubre: el CORTE TERMICO
por profundidad (superficie / masa / carcasa), la distribucion lateral interior-exterior,
la presion en caliente y el diferencial contra la presion objetivo. El desgaste y las
vueltas que le quedan a cada goma NO se recalculan aca: llegan ya resueltos desde
StrategyEngine (wear_vec / eol_vec), que detecta si mTyreWear crece o decrece con el uso
y mantiene el EMA de ritmo por rueda. Una sola verdad para las dos vistas.

CANALES: todo lo de aca se MIDIO en vivo, no se supuso. Dos sondas: tools/tyre_probe.py
(Mercedes-AMG GT4 en Snetterton, lisos, 2807 frames @187Hz) y una pasada larga posterior
(16.790 frames a 34-243 km/h + 5.747 muestras del corte termico). Lo que se aprendio:

1. mAirPressure viene en Bar x100  -> /100 = bar. (165.2 crudo = 1.65 bar = 24.0 psi)

2. UNIDADES, la trampa: mTyreLayerTemp, mTyreCarcassTemp y mTyreTreadTemp vienen en
   KELVIN (-273.15 = C); mTyreTemp (el bulk) viene ya en CELSIUS. Mezclarlas son 273
   grados de error, asi que cada lectura de abajo dice explicitamente su unidad.

3. CORTE TERMICO por profundidad (medias de la sonda larga, ya en C):

           layer   bulk   tread   carcasa
      FL    43.6   48.8    48.9    57.9
      FR    43.9   48.8    48.9    56.9
      RL    49.7   52.6    52.8    59.2
      RR    48.8   52.3    52.5    58.5

   - mTyreLayerTemp difiere del bulk 2.8-5.2 C  -> canal PROPIO real (la piel) -> t_surf
   - mTyreCarcassTemp difiere 6.1-9.1 C         -> canal PROPIO real          -> carcass
   - mTyreTreadTemp difiere 0.08-0.25 C         -> ES el bulk en Kelvin: redundante,
     NO se lee. (Cuidado con el nombre: "tread" suena a superficie y no lo es.)
   - mTyreTemp (bulk directo)                   -> t_bulk
   O sea el instrumento se llena con TRES mediciones independientes, no con la misma
   tres veces disfrazada.

4. La CARCASA es el UNICO canal termico confiable. Barrido de 58 sesiones grabadas (a jul-2026; el corpus crece)
   (22+ autos, 22+ pistas; tools/tyre_replay.py da el conteo vivo) sobre la sonda original:
   - carcasa VIVA en el 100% de las sesiones, con rango intra-vuelta de 5-30 C.
   - bulk/layer/left/right (el modelo de superficie) MUERTOS en ~10% de las
     sesiones: se quedan pegados cerca del ambiente (rango <2.5 C por vuelta)
     mientras la carcasa marca 75-130. No es por auto: el MISMO auto en la misma
     pista sale vivo en una sesion y muerto en la siguiente (bug del juego) ->
     la deteccion tiene que ser POR SESION, en vivo (_surf_alive, abajo).
   - el offset carcasa-bulk NO es una constante fisica: +12 a +57 C entre autos
     vivos (y +65..+88 en sesiones muertas). En pista de verdad la carcasa corre
     SIEMPRE decenas de grados sobre la superficie: "azul arriba, rojo abajo" es
     el estado permanente, no una firma.
   - la "ventana operativa 70-100" salio de UNA sesion GT4 con pista fria. En 8 de
     10 sesiones la mediana de carcasa esta SOBRE 100 (GT3/protos 104-143) y los
     historicos livianos viven en 52-90. Un umbral absoluto dispara "hot" en
     conduccion normal en la mayoria de los autos y "cold" en el resto.
   CONSECUENCIA (v3.1): no existe escala absoluta que generalice entre autos, asi
   que aca NO se emite ningun veredicto contra tabla. Todo veredicto termico es
   AUTO-REFERENCIAL, y todo sale de la carcasa:
   - rel   = carcasa de la esquina - media de las 4 (estructura: que rueda trabaja
             mas EN ESTE AUTO; suma cero, no puede saturar en rojo las cuatro).
   - tdev  = la ESTRUCTURA se movio: rel de la esquina contra su propia norma (EMA
             lenta ~2.5 min de rel). Restar la media de las 4 cancela el warm-up y
             el cool-down (drift comun) por construccion; lo que queda es "esta
             esquina se separo del resto mas rapido de lo normal" -- el trompo que
             cocino la RL, la rueda que dejo de trabajar. Se probo primero contra
             la carcasa ABSOLUTA con mediana de desviaciones: disparaba durante el
             warm-up en pistas direccionales (la asimetria normal construyendose);
             sobre rel eso desaparece solo.
   - trend = calentando/estable/enfriando global (media de las desviaciones).
   - warm  = la carcasa esta cerca de SU PROPIO plateau (no de un 60 fijo).
   t_surf y t_bulk se emiten SIN estado y SIN color de fondo: no tienen ventana
   medida, no se les regala una, y ademas a veces estan muertos.

5. mTyreTempCenter es BYTE-IDENTICO a mTyreTemp (bulk) en las 4 esquinas, confirmado en
   DOS sondas (delta 0.00). El "centro" NO es una tercera medicion del piso de la goma
   -> se dejo de leer; el bulk se toma directo de mTyreTemp. Consecuencias:
      -> aca NO se deriva presion del perfil centro-vs-hombros (el clasico "centro
         caliente = sobreinflado"): ese diagnostico necesita un centro real. Ademas el
         offset centro-vs-hombros salio sospechosamente uniforme (-2.6/-2.7/-2.8/-2.75 C
         en las 4 esquinas), que huele a artefacto y no a fisica.
      -> El diferencial de presion sale del OBJETIVO configurable, que es honesto.

6. mTyreTempLeft/Right SI se pueblan, pero estan en marco ABSOLUTO del auto (izquierda/
   derecha de la PISTA), no relativo a la rueda. Se dedujo del dato: el borde interior
   salio mas caliente en las 4 esquinas de forma ESPEJADA (FL caliente a la derecha, FR
   caliente a la izquierda, idem atras) -- firma clasica de camber negativo, y solo
   cuadra si L/R son absolutos. De ahi el mapeo INNER_IS_RIGHT de abajo. El spread
   interior-exterior SI es medicion real de distribucion lateral -> camber.

6b. ...pero SOLO en la rueda que la pista CARGA, y la cargada es la de spread BAJO.
   Medido sobre las 69 sesiones con vueltas del corpus: la asimetria izquierda-derecha
   del spread correlaciona 0.85 con la DIRECCIONALIDAD de la pista, no con el camber.
   Atribuyendo la carga por TEMPERATURA (la goma que trabaja es la que se calienta:
   corr -0.895 entre el indice de accel_x y el calor izq-der), la rueda cargada tiene
   spread mediano +5.0 y la descargada +8.2 -- una diferencia de -3.0 C.
   La fisica cierra: la goma de AFUERA es la cargada, el rolido de la carroceria se come
   su camber negativo estatico y le calienta el hombro EXTERIOR, asi que su spread baja.
   Esa es LA medicion de camber que sirve ("tengo suficiente camber estatico para
   sobrevivir al rolido?"). La de adentro conserva su camber, muestra un spread alto y
   no significa nada.
   Goiania con el Audi R8 GT3 (cargan las izquierdas, 106/114 C contra 87/96): FL +2 /
   FR +9 / RL +1 / RR +8. Las cargadas son las de +2 y +1. Una ventana absoluta centrada
   en la distribucion de la DESCARGADA --como la [3,12] original-- acusa "poco camber
   neg." en el 27% de los ejes cargados del corpus, para siempre y sin arreglo posible
   por mas camber que le meta el piloto. Ese era el bug reportado.
   Otras dos cosas medidas al mismo tiempo, que descartan alternativas plausibles:
      -> normalizar el spread por el nivel termico del borde NO ayuda, ni entre autos
         (CV 0.69 -> 0.67) ni dentro del mismo auto (desvio relativo p50 14.6% -> 16.8%,
         PEOR). La dispersion es de construccion y direccionalidad, no de unidades.
      -> segmentar por G lateral instantaneo tampoco: el modelo de bordes de AMS2 esta
         tan filtrado que la mediana en curva cargada, en recta y descargada difiere
         menos de 0.5 C. Lo que sirve es el indice ACUMULADO de la tanda, no el gate
         instantaneo. Como efecto util, el valor congelado en boxes representa bien la
         tanda entera.
   Ver LAT_LOAD / DIR_NEUTRAL / CAMBER_* abajo.

7. mTyreGrip NO se usa: se midio instantaneo (corr -0.725 con deslizamiento, -0.769 con
   G lateral; 0.457 en recta vs 0.104 en curva). Es margen sin usar EN ESTE INSTANTE,
   no estado de la goma -> parpadearia con la exigencia sin informar nada.

8. mTerrain esta vivo (codigos 0,3,10,11,32,40,46) pero SIN MAPEAR -> no se emite nada
   con el. Pintarlo como "sucio" seria inventar.

Referencia del caveat: SimHub #632 (inner/outer cruzados / center = surface temp).

No corre hilo propio: el bridge llama update(d, wear) por frame y payload(eol, stint)
al emitir.
"""
import json
import math
import os
import time

CORNERS = ("FL", "FR", "RL", "RR")
BAR_TO_PSI = 14.5038
KELVIN = 273.15

# FL(0) y RL(2) son ruedas IZQUIERDAS -> su borde interior es el canal "Right".
# FR(1) y RR(3) son derechas -> su borde interior es el canal "Left". Ver nota 3a.
INNER_IS_RIGHT = (True, False, True, False)

MIN_SPEED = 15.0         # km/h: bajo esto no es lectura "en marcha"
EMA = 0.06               # suavizado de presion/temps (la presion oscila con la carga)

# --- veredicto auto-referencial (v3.1): constantes validadas con tools/tyre_replay.py
# sobre TODAS las sesiones grabadas, no contra el mock ---
TAU_SLOW = 150.0         # s: EMA lenta = "la norma de ESTA goma hoy" (absoluta y de rel)
DEV_REL = 7.0            # C que la estructura (rel) debe separarse de su norma para
                         # opinar. Ruido de plateau medido: +-1..3; evento real (RL
                         # cocinada en Buenos Aires): +13. El warm-up direccional mas
                         # violento del archivo (MINI en Interlagos) llega a ~5.
JUMP_C = 20.0            # C de salto SIMULTANEO en las 4 carcasas entre frames =
                         # cambio de sesion o de gomas, no un evento de la goma.
                         # Medido en vivo: 125->58 C en un frame al rotar sesion.
MIN_NORM_S = 120.0       # s rodados antes de que exista "norma" de la cual desviarse
TREND_EPS = 4.0          # C de desviacion media para decir calentando/enfriando
WARM_TIME = 180.0        # s rodados minimos antes de declarar warm
WARM_ENTER = -5.0        # warm: media rapida >= media lenta - 5 (cerca del plateau)
WARM_EXIT = -15.0        # se pierde warm: media rapida < media lenta - 15 (histeresis:
                         # una vuelta de traffic/lift baja ~10 y NO debe apagar el delta)
# Deteccion del modelo de superficie muerto (bug de AMS2, ~10% de las sesiones):
# bulk plano (rango <2.5/vuelta) pegado cerca del ambiente con la carcasa a 75-130.
SURF_RANGE_MIN = 3.0     # C: rango minimo de bulk en la ventana para considerarlo vivo
SURF_OFFSET = 45.0       # C: carcasa-bulk mayor que esto + rango plano = muerto
SURF_BUCKET_S = 60.0     # s por balde; la ventana efectiva de rango es 60-120 s

# --- camber: la senal SOLO existe en la rueda que la pista CARGA (ver nota 6b) ---
# accel_x > 0 => cargan las DERECHAS. Signo fijado por temperatura (corr -0.895), NO
# por suspension: mas mSuspensionTravel es rueda EXTENDIDA, o sea descargada.
LAT_LOAD = 6.0            # m/s2 de G lateral para contar la curva como cargada
DIR_NEUTRAL = 0.15        # |indice| para DECLARAR un lado dominante (Spa, Kansai,
                          # Hungaroring miden 0.00-0.14; Goiania 1.00, Cascavel 0.65)
DIR_EXIT = 0.10           # ...y para SOLTARLO. La histeresis no es opcional: con un
                          # umbral solo, las pistas que viven cerca de 0.15 hacian
                          # parpadear el veredicto de dos ruedas decenas de veces por
                          # tanda (33 y 37 cambios en 11 min en Termas; 21/58 sesiones)
CAMBER_MIN_LOAD_S = 60.0  # s acumulados de curva cargada antes de decidir el lado Y de
                          # opinar. NO es arbitrario: medido contra el corpus, el lado
                          # que declara el indice a los 20 s coincide con el de la tanda
                          # completa solo el 55% de las veces (una moneda al aire), y con
                          # histeresis ese enganche temprano se congelaba el resto de la
                          # tanda. A los 60 s sube a 81% y todavia alcanza en 64 de 69
                          # sesiones; a 90 s daria 90% pero 14 sesiones se quedarian sin
                          # veredicto. Las que quedan fuera son qualys de 1-2 vueltas.
# Referencia auto-referencial: el spread del eje cargado de la tanda ANTERIOR del mismo
# auto EN LA MISMA PISTA. La pista es parte de la llave y no un detalle: el ruido
# inter-tanda medido del mismo auto es p90 2.1 C dentro de la misma pista contra 4.3
# mezclando circuitos, o sea que sin cualificar por pista el instrumento reporta el
# cambio de trazado como si fuera un cambio de setup.
CAMBER_DELTA = 3.0        # entre el p90 (2.1) y el p95 (4.4) de la misma pista
# Ventana ABSOLUTA de respaldo, y solo mientras el auto no tenga tanda previa. Sale de
# la distribucion del eje CARGADO en sesiones vivas: p05 -0.2, p10 +0.5, mediana +5.2,
# p90 +9.5, p95 +10.0 (n=130 ejes). Con [0,10] acusa 6% por abajo y 4% por arriba.
# LA VENTANA VIEJA ERA EL BUG: [3,12] acusa el 27% de los ejes CARGADOS del corpus de
# "poco camber neg.", porque estaba centrada en la distribucion de la rueda DESCARGADA
# (+8.2 de mediana) en vez de la cargada (+5.2). De ahi que el veredicto saliera igual
# con el camber al maximo. Ademas ya no existe un veredicto de "poco": el corpus no
# autoriza a decirle a este piloto que le falta camber. Solo se opina en los extremos --
# spread negativo (el hombro exterior mas caliente que el interior) o sobre +10.
# Igual la reemplaza la referencia propia apenas existe una.
CAMBER_OK_LO = 0.0
CAMBER_OK_HI = 10.0

# Punto de partida para lisos GT3/GT4; el piloto lo ajusta por auto con el boton objetivo.
# 1.90 NO es una intuicion, es la interseccion de tres fuentes independientes:
#   - Reiza (CrimsonEminence, staff): "default setups aim for hot pressures in the
#     1.8-1.9 bar range for GT3 cars".
#   - El corpus grabado: 45 sesiones GT3/GT4, 180 esquinas con la goma caliente ->
#     min 1.68, p10 1.80, MEDIANA 1.89, p90 2.02, max 2.21 bar (27.4 psi de mediana).
#   - La especificacion real Pirelli GT4: 2.0 bar en caliente, minimo de inflado 1.4 bar
#     -- y ese 1.4 es exactamente el minimo que AMS2 deja poner en el setup, o sea el
#     juego esta modelando esa especificacion.
# EL VALOR ANTERIOR (1.75) ERA EL PISO, NO EL OBJETIVO: 169 de las 180 esquinas del
# corpus corren POR ENCIMA de el. Consecuencia real reportada en pista: el piloto bajo
# la presion en frio hasta 1.5 (con 1.4 de minimo) persiguiendo 1.75 en caliente, no
# llego --las traseras se le quedaban en 1.9-- y el auto empeoro. El objetivo era
# inalcanzable por debajo del minimo del auto.
# OJO con la semantica: AMS2 no modela una presion optima fija (a diferencia de ACC).
# La correcta se determina por el perfil de temperatura del neumatico... que aca NO se
# puede construir porque mTyreTempCenter es byte-identico al bulk (ver nota 5). Por eso
# el objetivo es configurable y esto es solo un punto de partida honesto.
# Se eligio 1.90 y no 1.85 midiendo el veredicto que produce cada uno sobre el corpus
# (tolerancia +-0.03): con 1.75 sale "SACAR" en 166 de 180 esquinas -- un instrumento
# que dice siempre lo mismo no informa; con 1.85 sigue sesgado (98 SACAR / 29 AGREGAR);
# con 1.90 queda balanceado (57 SACAR / 68 AGREGAR / 55 en objetivo). Y 1.90 es a la vez
# la mediana medida (1.89) y el tope del rango que da Reiza.
DEFAULT_TARGET_BAR = 1.90
PRESS_TOL = 0.03            # +-bar que se considera "en objetivo"
TARGETS_FILE = "tyre_targets.json"
# Clave RESERVADA dentro de tyre_targets.json para las referencias de camber. El resto
# del archivo es {nombre de auto: bar}; ningun auto se llama asi, y target() sigue
# leyendo su float sin enterarse. Evita un segundo archivo que mantener en sincronia.
CAMBER_KEY = "_camber"


def _s(buf):
    return bytes(buf).split(b"\x00")[0].decode("utf-8", "replace")


class TyreAnalyzer:
    """Estado de gomas por esquina + diferencial contra la presion objetivo."""

    def __init__(self, base_dir=None):
        self._dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self._targets = self._load_targets()
        self._car = ""
        self._track = ""              # mTrackLocation: parte de la llave de camber
        self._compound = ""
        self._live = False
        self._press = [None] * 4      # bar (EMA)
        self._t_in = [None] * 4       # C, borde interior (EMA)
        self._t_out = [None] * 4      # C, borde exterior (EMA)
        # Corte por profundidad: piel -> masa -> carcasa (ver notas 3 y 4)
        self._t_surf = [None] * 4     # C, superficie / mTyreLayerTemp (EMA)
        self._t_bulk = [None] * 4     # C, masa / mTyreTemp (EMA)
        self._carcass = [None] * 4    # C (EMA rapida: "ahora")
        self._brake = [None] * 4      # C (EMA)
        self._wear = [None] * 4       # 0..1, resuelto por StrategyEngine
        # --- estado del veredicto auto-referencial (v3.1) ---
        self._slow = [None] * 4       # C, EMA lenta de carcasa (tau ~150 s): la norma
        self._rel_slow = [None] * 4   # C, EMA lenta de rel (estructura): norma de tdev
        # Lo que la goma HIZO en pista, para leerlo despues en boxes. Sin esto el
        # piloto vuelve al box, mira el dash y ya se enfrio/reseteo todo -- que es
        # justo cuando puede mirarlo. Los bordes ademas se ponen en 0 fuera de pista,
        # asi que el veredicto de camber salia siempre "poco camber neg." (0 < umbral).
        self._edges_stale = [False] * 4  # el borde mostrado es de pista, no de ahora
        self._peak_carc = [None] * 4  # C, maxima carcasa alcanzada rodando
        self._in_track = [None] * 4   # C, ultimo borde interior valido EN PISTA
        self._out_track = [None] * 4  # C, idem exterior
        self._jump = False            # las 4 carcasas saltaron juntas -> sesion/gomas nuevas
        self._prev_carc = [None] * 4  # lectura CRUDA anterior, para detectar ese salto
        # --- direccionalidad de la pista (que lado carga) y referencia de camber ---
        self._t_izq = 0.0             # s rodados cargando las ruedas IZQUIERDAS
        self._t_der = 0.0             # s idem derechas
        # OJO: se llama _lado y no _dir porque self._dir ES EL DIRECTORIO BASE. Pisarlo
        # mandaba tyre_targets.json a un directorio llamado "=" y _save_targets se comia
        # el OSError en silencio: la referencia no se persistia nunca.
        self._lado = "="              # lado cargado vigente (con histeresis, ver dir_side)
        self._cam_prev = {}           # {"F": C, "R": C} del cierre de la tanda anterior
        self._runtime = 0.0           # s acumulados en marcha (>MIN_SPEED)
        self._last_now = None         # monotonic de la ultima ingesta (para dt)
        self._warm = False            # con histeresis (WARM_ENTER/WARM_EXIT)
        # deteccion de modelo de superficie muerto: rango de bulk en 2 baldes rotativos
        self._surf_alive = True
        self._bkt_t0 = 0.0            # runtime al que abrio el balde actual
        self._bkt_cur = [[None, None] for _ in range(4)]   # [min,max] bulk crudo
        self._bkt_prev = [[None, None] for _ in range(4)]

    # ---------------- objetivo de presion (persistido por auto) ----------------
    def _load_targets(self):
        try:
            with open(os.path.join(self._dir, TARGETS_FILE), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_targets(self):
        try:
            tmp = os.path.join(self._dir, TARGETS_FILE + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._targets, f, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(self._dir, TARGETS_FILE))
        except OSError:
            pass

    def target(self):
        """Presion objetivo en caliente (bar) del auto montado.

        Valida isfinite Y rango aunque el valor venga del archivo: float("nan") NO
        lanza, y un NaN aca envenena todo el broadcast -- json.dumps escribe el
        literal NaN (JSON invalido), el JSON.parse del navegador tira SyntaxError y
        el dash COMPLETO (posicion, fuel, tiempos) deja de actualizarse, no solo gomas.
        """
        try:
            v = float(self._targets.get(self._car, DEFAULT_TARGET_BAR))
        except (TypeError, ValueError):
            return DEFAULT_TARGET_BAR
        return v if (math.isfinite(v) and 0.5 <= v <= 4.0) else DEFAULT_TARGET_BAR

    def set_target(self, bar):
        """Fija el objetivo del auto actual y lo persiste. Devuelve el valor aplicado."""
        try:
            v = float(bar)
        except (TypeError, ValueError):
            return self.target()
        if not (math.isfinite(v) and 0.5 <= v <= 4.0):   # rango sano en bar
            return self.target()
        if self._car:
            self._targets[self._car] = round(v, 2)
            self._save_targets()
        return round(v, 2)

    # ------------- referencia de camber (persistida por pista + auto) -------------
    def _cam_key(self):
        """Llave de la referencia. La PISTA va en la llave porque el spread del eje
        cargado del mismo auto cambia mas entre circuitos (ruido p90 4.3 C) que dentro
        de uno (2.1 C): sin ella, cambiar de trazado se reporta como cambio de setup."""
        return f"{self._track}|{self._car}" if self._track else self._car

    def _load_camber(self):
        """Spread de eje cargado con que cerro la tanda anterior en esta pista y auto."""
        base = self._targets.get(CAMBER_KEY)
        if not isinstance(base, dict):
            return {}
        v = base.get(self._cam_key())
        if not isinstance(v, dict):
            return {}
        out = {}
        for eje in ("F", "R"):
            try:
                x = float(v[eje])
            except (KeyError, TypeError, ValueError):
                continue
            # Rango sano: un valor corrupto en el archivo no debe poder mover la banda
            # verde a un lugar absurdo ni envenenar el payload con NaN.
            if math.isfinite(x) and -30.0 <= x <= 60.0:
                out[eje] = x
        return out

    def _save_camber(self):
        """Cierra la tanda: el spread del eje cargado queda de referencia para la
        proxima. Este es el nucleo auto-referencial. El veredicto util no es "tu camber
        esta mal" --el corpus no autoriza esa afirmacion en la mayoria de los autos--
        sino "lo moviste y esto cambio", que es justo el loop de setup del piloto.
        Se llama al ROTAR de sesion/gomas (_jump), al cambiar de auto o de pista, y al
        cerrar el bridge (close()) -- sin esto ultimo, matar el dash o cerrar AMS2
        pierde la tanda entera, que es justo cuando mas duele."""
        if not self._car:
            return
        ahora = self._camber_now()
        if not ahora:
            return                      # sin derecho a opinar no se guarda basura
        base = self._targets.get(CAMBER_KEY)
        if not isinstance(base, dict):
            base = self._targets[CAMBER_KEY] = {}
        k = self._cam_key()
        prev = base.get(k)
        if not isinstance(prev, dict):
            prev = {}
        # Solo se pisa el eje que SI se midio: si la pista cargo un lado y una rueda
        # quedo sin bordes validos, el otro eje conserva su referencia vieja.
        for eje, v in ahora.items():
            prev[eje] = round(v, 1)
        base[k] = prev
        self._save_targets()

    def close(self):
        """Cierre ordenado del bridge: persiste la tanda en curso. Publico para que
        bridge_shm lo llame al apagar."""
        self._save_camber()

    def surf_alive(self):
        """El modelo termico de banda (bulk/layer/left/right) esta VIVO en ESTA sesion?

        Publico: lo consume ams2_strategy a traves del bridge para el detector de
        crossover, que lee mTyreTemp y no debe re-implementar esta deteccion (la
        misma razon por la que aca no se recalcula el desgaste). Ver _surf_check.
        """
        return self._surf_alive

    def reset(self):
        """Olvida las lecturas suavizadas (no toca los objetivos guardados)."""
        for a in (self._press, self._t_in, self._t_out, self._t_surf, self._t_bulk,
                  self._carcass, self._brake, self._wear, self._slow, self._rel_slow):
            for i in range(4):
                a[i] = None
        self._runtime = 0.0
        self._last_now = None
        self._warm = False
        self._surf_alive = True
        self._bkt_t0 = 0.0
        self._bkt_cur = [[None, None] for _ in range(4)]
        self._bkt_prev = [[None, None] for _ in range(4)]
        # La direccionalidad es de la PISTA: al cambiar de auto/sesion no aplica.
        self._t_izq = self._t_der = 0.0
        self._lado = "="
        self._peak_carc = [None] * 4     # el pico es DE LA TANDA (el comentario de
                                         # update() ya lo afirmaba, pero no ocurria)
        self._cam_prev = self._load_camber()

    # ---------------- ingesta ----------------
    def update(self, d, wear=None, now=None):
        """Un snapshot de SHM. `wear` = vector 0..1 ya resuelto por StrategyEngine.
        `now` = reloj monotonico en segundos; inyectable para que tyre_replay.py pueda
        recorrer una sesion grabada a tiempo comprimido con ESTE mismo codigo."""
        if d is None:
            self._live = False
            return
        # Cambio de auto O DE PISTA: las lecturas viejas no aplican. La pista importa
        # tanto como el auto -- la direccionalidad es una propiedad del trazado, y la
        # referencia de camber se guarda por (pista, auto). Mismo patron que
        # ams2_strategy.py:215, que ya usa sig = (mTrackLocation, mCarName).
        car = _s(d.mCarName)
        track = _s(getattr(d, "mTrackLocation", b"")) or self._track
        if (car and car != self._car) or track != self._track:
            self._save_camber()             # cierra la tanda que SALE (con la llave vieja)
            if car:
                self._car = car
            self._track = track
            self.reset()
        self._compound = _s(d.mTyreCompound[0])
        self._live = (d.mSpeed * 3.6) >= MIN_SPEED
        # dt real entre snapshots, capado: una pausa de menus no debe pegarle un
        # alfa gigante a la EMA lenta ni sumar "tiempo rodado" que no existio.
        if now is None:
            now = time.monotonic()
        dt = 0.0 if self._last_now is None else min(max(now - self._last_now, 0.0), 0.5)
        self._last_now = now
        if self._live:
            self._runtime += dt

        # Los canales se toman UNA vez y con tolerancia a que falten: si una version del
        # juego (o un mock) no trae uno, ese canal queda en None y el resto sigue vivo.
        # Sin esto un AttributeError aca lo traga el try/except del bridge y la pagina se
        # queda CONGELADA en el frame anterior, sin avisar -- el peor modo de falla.
        ch = {}
        for k in ("mAirPressure", "mTyreLayerTemp", "mTyreTemp", "mTyreCarcassTemp",
                  "mBrakeTempCelsius", "mTyreTempLeft", "mTyreTempRight"):
            ch[k] = getattr(d, k, None)
        if ch["mTyreCarcassTemp"] is None:      # sin carcasa no hay nada que decir
            return
        _nan = float("nan")
        get = lambda k, c: (ch[k][c] if ch[k] is not None else _nan)   # noqa: E731

        # Salto SIMULTANEO en las 4 carcasas = la sesion roto o cambiaron las gomas.
        # No es un evento de la goma y la norma lenta queda colgada en los valores
        # viejos, asi que sin esto la alarma dispara en las 4 esquinas a la vez.
        crudas = [get('mTyreCarcassTemp', c) - KELVIN for c in range(4)]
        prev = self._prev_carc
        if all(p is not None for p in prev) and all(math.isfinite(v) for v in crudas):
            saltos = [abs(v - p) for v, p in zip(crudas, prev)]
            self._jump = min(saltos) >= JUMP_C      # las CUATRO, no una sola
        else:
            self._jump = False
        if self._jump:
            # Rotar de sesion o calzar gomas cierra la tanda: lo medido hasta aca pasa a
            # ser la referencia contra la que se compara la siguiente. Es lo que hace que
            # el piloto vea "cambiaste el camber y el eje delantero se movio +2.8" al
            # salir a la carrera despues de tocar el setup en la qualy.
            self._save_camber()
            self._cam_prev = self._load_camber()
            self._t_izq = self._t_der = 0.0      # sesion nueva -> direccionalidad nueva
            self._lado = "="
            # El gate de "goma en regimen" tiene que volver a cumplirse: sin esto, tras
            # los primeros 120 s de vida del bridge la condicion queda satisfecha PARA
            # SIEMPRE y un out-lap de 20 s alcanza para pisar la referencia buena.
            self._runtime = 0.0
            # Y warm TAMBIEN, que es el que desbloquea el delta de presion. Sin esto el
            # estado sobrevive el cierre de tanda: abajo se re-siembran las DOS EMAs de
            # carcasa al mismo valor frio, o sea gap = 0, que nunca cae bajo WARM_EXIT,
            # asi que _warm se quedaba en True con la goma helada. Medido en vivo:
            # carcasa 38-52 C y warm=True en un GT4 cuyo plateau es ~115. Consecuencia
            # real: el dash mostraba el delta de presion sobre la presion FRIA y mandaba
            # a AGREGAR cuando en caliente ya estaba sobre el objetivo -- el piloto
            # subio presiones siguiendolo y el auto empeoro.
            # (El enfriamiento normal SI lo caza WARM_EXIT: ahi la EMA rapida baja mas
            # rapido que la lenta y el gap se hace negativo. El agujero era solo el
            # cierre de tanda, donde ambas saltan juntas.)
            self._warm = False
            # re-sembrar: la norma vieja ya no describe nada. Sin esto la alarma
            # seguiria sonando los ~2.5 min que tarda la EMA lenta en alcanzar.
            self._slow = list(crudas)
            self._rel_slow = [None] * 4
            self._carcass = list(crudas)
        self._prev_carc = [v if math.isfinite(v) else None for v in crudas]

        # Tiempo rodado cargando cada lado. accel_x > 0 => cargan las DERECHAS.
        # El signo se fijo por TEMPERATURA, no por la suspension: corr(indice de accel_x,
        # calor izquierdas-derechas) = -0.895 sobre 69 sesiones. La goma que trabaja es la
        # que se calienta, y eso no depende de ninguna convencion que haya que adivinar.
        # OJO -- aca hubo un error: la primera version dedujo el signo de mSuspensionTravel
        # suponiendo "mas travel = mas comprimido = mas cargado", y es AL REVES
        # (corr(calor izq-der, travel izq-der) = -0.872: mas travel = rueda EXTENDIDA, o
        # sea descargada). Con esa suposicion el veredicto se apagaba justo en las ruedas
        # que trabajan. Si algun dia hay que re-verificar esto, usa la temperatura.
        lat = getattr(d, "mLocalAcceleration", None)
        if self._live and lat is not None and dt > 0.0:
            try:
                ax = float(lat[0])
            except (TypeError, ValueError, IndexError):
                ax = _nan
            if math.isfinite(ax):
                if ax > LAT_LOAD:
                    self._t_der += dt
                elif ax < -LAT_LOAD:
                    self._t_izq += dt

        for c in range(4):
            # Lecturas absolutas: validas tambien parado (reflejan enfriamiento). Cada
            # canal por separado: un NaN puntual en uno no descarta los otros.
            self._ema(self._press, c, get('mAirPressure', c) / 100.0)
            # Corte por profundidad. OJO unidades (nota 2): layer y carcass en KELVIN,
            # mTyreTemp ya en Celsius. mTyreTreadTemp no se lee (es el bulk en K).
            self._ema(self._t_surf, c, get('mTyreLayerTemp', c) - KELVIN)
            self._ema(self._t_bulk, c, get('mTyreTemp', c))
            carc = get('mTyreCarcassTemp', c) - KELVIN
            self._ema(self._carcass, c, carc)
            # EMA lenta: la "norma" de esta goma. Alfa por TIEMPO (dt/tau), no por
            # frame: asi el tau son segundos de verdad a cualquier Hz del bridge.
            if math.isfinite(carc) and dt > 0.0:
                a = dt / TAU_SLOW
                self._slow[c] = carc if self._slow[c] is None \
                    else self._slow[c] + a * (carc - self._slow[c])
            elif math.isfinite(carc) and self._slow[c] is None:
                self._slow[c] = carc
            # rango de bulk crudo en marcha -> deteccion de modelo de superficie muerto
            if self._live and math.isfinite(get('mTyreTemp', c)):
                b = self._bkt_cur[c]
                v = get('mTyreTemp', c)
                b[0] = v if b[0] is None else min(b[0], v)
                b[1] = v if b[1] is None else max(b[1], v)
            self._ema(self._brake, c, get('mBrakeTempCelsius', c))
            left, right = get('mTyreTempLeft', c), get('mTyreTempRight', c)
            inner, outer = (right, left) if INNER_IS_RIGHT[c] else (left, right)
            # Los bordes se van a 0.0 EXACTO fuera de pista (garage/menu). Ese centinela
            # NO se le da de comer a la EMA: si entrara, la lectura decaeria suave hacia
            # cero y el veredicto de camber que el piloto ve AL VOLVER AL BOX --que es
            # cuando por fin puede mirar el dash-- saldria de un spread de 0, o sea
            # "poco camber neg." siempre, sin importar el camber real del auto.
            # No alimentandola, la EMA queda congelada en lo ultimo medido rodando.
            bordes_ok = (inner != 0.0 and outer != 0.0
                         and math.isfinite(inner) and math.isfinite(outer))
            if bordes_ok:
                self._ema(self._t_in, c, inner)
                self._ema(self._t_out, c, outer)
                self._edges_stale[c] = False
            elif self._t_in[c] is not None:
                self._edges_stale[c] = True     # lo que se muestra es DE PISTA, no de ahora
            # Pico de carcasa de la tanda: lo que la goma ALCANZO, no lo que le queda
            # cuando por fin la miras. Se resetea con el auto (reset()) y al saltar.
            if self._live and math.isfinite(carc):
                self._peak_carc[c] = (carc if self._peak_carc[c] is None
                                      else max(self._peak_carc[c], carc))
            if wear is not None:
                try:
                    w = float(wear[c])
                    if math.isfinite(w):
                        self._wear[c] = min(max(w, 0.0), 1.0)
                except (TypeError, ValueError, IndexError):
                    pass

        # norma de la ESTRUCTURA: EMA lenta de rel (carcasa de la esquina - media).
        # Se alimenta con las EMAs rapidas, que ya filtran el ruido de frame.
        avail = [t for t in self._carcass if t is not None]
        if len(avail) >= 2 and dt > 0.0:
            mean = sum(avail) / len(avail)
            a = dt / TAU_SLOW
            for c in range(4):
                if self._carcass[c] is None:
                    continue
                rel = self._carcass[c] - mean
                self._rel_slow[c] = rel if self._rel_slow[c] is None \
                    else self._rel_slow[c] + a * (rel - self._rel_slow[c])

        self._surf_check()
        self._warm_check()

    @staticmethod
    def _ema(arr, c, v):
        if not math.isfinite(v):
            return
        arr[c] = v if arr[c] is None else arr[c] + EMA * (v - arr[c])

    # ---------------- modelo de superficie vivo/muerto ----------------
    def _surf_check(self):
        """AMS2 a veces NO corre el modelo termico de banda (bulk/layer/left/right
        quedan pegados cerca del ambiente) aunque la carcasa siga viva. Es por sesion,
        no por auto: el mismo coche sale vivo hoy y muerto manana. Firma medida en las
        sesiones malas: rango de bulk <2.5 C por vuelta con carcasa 65-90 C por encima.
        Vivo de verdad: rango 5-30 C por vuelta y offset 12-57 C (las DOS condiciones
        tienen margen contra el corpus grabado; ver tools/tyre_replay.py)."""
        if self._runtime - self._bkt_t0 >= SURF_BUCKET_S:      # rotar balde
            self._bkt_prev = self._bkt_cur
            self._bkt_cur = [[None, None] for _ in range(4)]
            self._bkt_t0 = self._runtime
        rng = 0.0
        seen = False
        for c in range(4):
            los = [b[c][0] for b in (self._bkt_cur, self._bkt_prev) if b[c][0] is not None]
            his = [b[c][1] for b in (self._bkt_cur, self._bkt_prev) if b[c][1] is not None]
            if los and his:
                seen = True
                rng = max(rng, max(his) - min(los))
        if not seen:
            return
        if self._surf_alive:
            # para declarar muerto: ventana completa (2 baldes), plano Y lejos de carcasa
            carc = [t for t in self._carcass if t is not None]
            bulk = [t for t in self._t_bulk if t is not None]
            if (self._runtime >= 2 * SURF_BUCKET_S and rng < SURF_RANGE_MIN
                    and carc and bulk
                    and (sum(carc) / len(carc) - sum(bulk) / len(bulk)) > SURF_OFFSET):
                self._surf_alive = False
        elif rng >= 2 * SURF_RANGE_MIN:
            # resucitar exige el DOBLE de rango que el que declara muerto: el canal
            # muerto igual deriva 2-3 C entre vueltas y sin esta histeresis parpadea
            # (medido: Kansai race y Jerez 0628 terminaban "vivos" por un balde suelto)
            self._surf_alive = True

    # ---------------- warm auto-referencial ----------------
    def _warm_check(self):
        """La presion solo decide setup con la goma en SU temperatura de trabajo, y esa
        no es un numero fijo (plateau de carcasa: 55-73 en un Vee, 104-143 en un GT3).
        warm = la media rapida esta cerca de la media LENTA (su propio plateau), con
        piso de tiempo rodado e histeresis para que una vuelta de traffic no lo apague."""
        fast = [t for t in self._carcass if t is not None]
        slow = [t for t in self._slow if t is not None]
        if not fast or not slow:
            self._warm = False
            return
        gap = sum(fast) / len(fast) - sum(slow) / len(slow)
        if self._warm:
            if gap < WARM_EXIT:
                self._warm = False
        elif self._runtime >= WARM_TIME and gap >= WARM_ENTER:
            self._warm = True

    # ---------------- salida ----------------
    def payload(self, eol=None, stint=None):
        """Estado para el dash. `eol` = vector de StrategyEngine.eol_vec() (vueltas que
        le quedan a cada rueda, None donde no hay derecho a opinar) y `stint` = vueltas
        del stint. Ambos opcionales: sin ellos el payload sale con eol/stint en null."""
        target = self.target()
        devs = self._devs()
        rels = self._rels()
        corners = []
        for c in range(4):
            p, car = self._press[c], self._carcass[c]
            # Modelo de superficie muerto -> los canales de banda NO se emiten: un 25
            # junto a una carcasa de 110 no es un dato, es el bug del juego. La esquina
            # queda en em-dash (el frontend ya es null-safe) y sin veredicto de camber.
            if self._surf_alive:
                ti, to = self._t_in[c], self._t_out[c]
                tsurf, tbulk = self._t_surf[c], self._t_bulk[c]
            else:
                ti = to = tsurf = tbulk = None
            # BORDES EN CERO EXACTO = centinela, no medicion. Un neumatico real nunca
            # marca 0.0 C en el borde, ni siquiera frio (el ambiente anda en 15-30).
            # Pasa con el auto FUERA DE PISTA (garage/menu): AMS2 deja de correr el
            # modelo de banda y los pone en 0, mientras carcasa y piel siguen con
            # valores plausibles -- por eso _surf_check no lo ve, y hace bien: el canal
            # NO esta muerto, solo esta sin actualizar. Verificado en la traza del Uno
            # Classic B: manejando esos mismos canales leen 59-72 C con rango real.
            # Sin este corte, un spread de 0.0 se colaba a _camber_hint y la pagina
            # dictaba "poco camber neg." a partir de nada.
            if ti == 0.0 and to == 0.0:
                ti = to = None
            # Fuera de pista se cae de vuelta a lo ULTIMO medido rodando: es lo que el
            # piloto viene a ver cuando por fin puede mirar el dash (en el box). Se
            # marca como congelado para que la UI no lo presente como lectura de ahora.
            frozen = self._edges_stale[c] and ti is not None
            spread = self._spread(c)
            cam_txt, cam_st, cam_band = self._camber_hint(c, spread)
            corners.append({
                "name": CORNERS[c],
                "press": round(p, 2) if p is not None else None,
                "psi": round(p * BAR_TO_PSI, 1) if p is not None else None,
                "delta": round(target - p, 2) if p is not None else None,
                "pstat": self._pstat(p, target),
                "t_in": round(ti) if ti is not None else None,
                "t_out": round(to) if to is not None else None,
                # Corte por profundidad: piel -> masa -> carcasa. Sin estado ninguno:
                # las escalas absolutas no generalizan entre autos (ver nota 4).
                "t_surf": round(tsurf) if tsurf is not None else None,
                "t_bulk": round(tbulk) if tbulk is not None else None,
                "spread": round(spread, 1) if spread is not None else None,
                # camber: texto para la linea de veredicto. cstat: como pintarlo
                # (ok/warn/idle). cband: [lo,hi] contra lo que se esta juzgando -- con
                # tanda previa es la referencia propia, sin ella la ventana de respaldo,
                # y null cuando no hay derecho a opinar (no se pinta verde ninguno).
                "camber": cam_txt,
                "cstat": cam_st,
                "cband": ([round(cam_band[0], 1), round(cam_band[1], 1)]
                          if cam_band else None),
                # de pista, no de ahora: la UI lo marca para no presentarlo como actual
                "frozen": frozen,
                # lo que la goma ALCANZO rodando (no decae al parar)
                "peak": (round(self._peak_carc[c]) if self._peak_carc[c] is not None else None),
                "carcass": round(car) if car is not None else None,
                # rel: estructura (esta esquina vs la media de las 4, C). tdev: la
                # esquina se salio de SU norma y de lo que hacen las otras tres.
                "rel": rels[c],
                "tdev": devs[c],
                "wear": round(self._wear[c] * 100, 1) if self._wear[c] is not None else None,
                "eol": self._eol(eol, c),
                "brake": round(self._brake[c]) if self._brake[c] is not None else None,
            })
        return {
            "live": self._live,
            "car": self._car,
            "compound": self._compound,
            "target": round(target, 2),
            # La presion solo sirve para decidir setup con la goma en SU temperatura
            # de trabajo (el plateau propio, no un umbral fijo). Ver _warm_check.
            "warm": self._warm,
            "trend": self._trend(),
            "surf_dead": not self._surf_alive,
            "stint": self._int_or_none(stint),
            # Que lado carga la pista ('I'/'D'/'='). Explica por que la mitad de los
            # veredictos de camber se apagan en un circuito direccional.
            "dir": self.dir_side(),
            "corners": corners,
            "axle": self._axle(),
        }

    # ---------------- señales auto-referenciales ----------------
    def _rels(self):
        """Estructura termica: carcasa de cada esquina contra la media de las 4.
        Suma cero por construccion -> no puede saturar en rojo (o azul) las cuatro
        a la vez, que fue exactamente el modo de falla de la escala absoluta."""
        avail = [t for t in self._carcass if t is not None]
        if len(avail) < 2:
            return [None] * 4
        mean = sum(avail) / len(avail)
        return [round(t - mean, 1) if t is not None else None for t in self._carcass]

    def _devs(self):
        """Veredicto por esquina: "hot"/"cold"/None. Dispara si la ESTRUCTURA se movio:
        el rel de la esquina (carcasa - media de las 4) se separo mas de DEV_REL de su
        propia norma lenta. El drift comun (warm-up, cool-down, vuelta de traffic) se
        cancela solo al restar la media; lo que dispara es la esquina que se fue del
        resto mas rapido de lo que la norma la sigue."""
        # Con el auto DETENIDO no se opina: el patron termico deja de venir de la
        # conduccion y se aplana solo, asi que el rel se desploma contra su norma y
        # dispara. Medido en vivo: 8 de 19 alarmas de una sesion de 40 min salieron
        # bajo 15 km/h (boxes y grilla). El replay no podia verlo porque concatena
        # SOLO trazas de vueltas limpias: nunca ve un pit ni una salida de garage.
        if not self._live:
            return [None] * 4
        # Salto discontinuo = cambio de sesion / de gomas, no un evento de la goma.
        # (las 4 carcasas pasaron de ~125 C a ~50 C en un frame al cambiar de sesion)
        if self._jump:
            return [None] * 4
        if self._runtime < MIN_NORM_S:
            return [None] * 4
        avail = [t for t in self._carcass if t is not None]
        if len(avail) < 2:
            return [None] * 4
        mean = sum(avail) / len(avail)
        out = [None] * 4
        for c in range(4):
            if self._carcass[c] is None or self._rel_slow[c] is None:
                continue
            v = (self._carcass[c] - mean) - self._rel_slow[c]
            if v >= DEV_REL:
                out[c] = "hot"
            elif v <= -DEV_REL:
                out[c] = "cold"
        return out

    def _trend(self):
        """Estado global: "heat"/"stable"/"cool" segun la desviacion media contra la
        norma lenta. None hasta tener con que comparar (primeros ~45 s rodados)."""
        if self._runtime < 45.0:
            return None
        pairs = [(f, s) for f, s in zip(self._carcass, self._slow)
                 if f is not None and s is not None]
        if not pairs:
            return None
        gap = sum(f - s for f, s in pairs) / len(pairs)
        if gap >= TREND_EPS:
            return "heat"
        if gap <= -TREND_EPS:
            return "cool"
        return "stable"

    @staticmethod
    def _eol(vec, c):
        """Vueltas a fin de vida de esa rueda, o None. El vector viene de OTRO modulo,
        asi que todo lo dudoso (falta, no es numero, es NaN/inf) se vuelve None: un
        NaN aca envenena el broadcast entero -- json.dumps escribe el literal NaN, el
        JSON.parse del navegador tira y el dash COMPLETO deja de actualizarse."""
        if vec is None:
            return None
        try:
            v = float(vec[c])
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        return round(v, 1) if math.isfinite(v) else None

    @staticmethod
    def _int_or_none(v):
        """int() tolerante: None, basura, NaN (ValueError) e inf (OverflowError) -> None."""
        try:
            return int(v)
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _pstat(p, target):
        if p is None:
            return None
        d = p - target
        if abs(d) <= PRESS_TOL:
            return "ok"
        return "high" if d > 0 else "low"

    # ---------------- camber ----------------
    def _spread(self, c):
        """Distribucion lateral interior-vs-exterior (C). None si no hay medicion:
        superficie muerta, o bordes en el centinela 0.0 de fuera de pista."""
        if not self._surf_alive:
            return None
        ti, to = self._t_in[c], self._t_out[c]
        if ti is None or to is None or (ti == 0.0 and to == 0.0):
            return None
        return ti - to

    def dir_side(self):
        """Que lado CARGA esta pista: 'I' izquierdas, 'D' derechas, '=' sin dominante.

        CON HISTERESIS. El indice acumulado de una pista intermedia se pasea alrededor
        de DIR_NEUTRAL toda la tanda, y con un solo umbral el veredicto de dos ruedas
        prendia y apagaba a cada cruce: medido, 33 y 37 cambios en una carrera de 11 min
        en Termas, y 21 de 58 sesiones del corpus parpadeaban. Entrar cuesta mas que
        salir, asi que una vez decidido el lado hace falta que el indice caiga bien
        adentro de la zona neutra para soltarlo.

        Publico porque es la razon por la que media parrilla de veredictos se apaga:
        conviene poder inspeccionarlo desde afuera (tests y replay)."""
        tot = self._t_izq + self._t_der
        # MINIMO DE MUESTRA antes de enganchar un lado. Con las primeras curvas el
        # indice acumulado satura en +-1.00 con nada de evidencia, y la histeresis lo
        # dejaria enganchado el resto de la tanda: medido, Spa y Termas (indice final
        # 0.12, o sea neutras) quedaban declaradas direccionales por sus primeras
        # curvas. El mismo umbral que habilita el veredicto habilita la decision.
        if tot < CAMBER_MIN_LOAD_S:
            return self._lado
        idx = (self._t_izq - self._t_der) / tot
        umbral = DIR_EXIT if self._lado != "=" else DIR_NEUTRAL
        if abs(idx) >= umbral:
            self._lado = "I" if idx > 0 else "D"
        elif self._lado != "=" and abs(idx) < DIR_EXIT:
            self._lado = "="
        return self._lado

    def _cargada(self, c):
        """Trabaja esta esquina lo suficiente como para que su spread hable de camber?"""
        lado = self.dir_side()
        if lado == "=":
            return True
        return c in ((0, 2) if lado == "I" else (1, 3))

    def _camber_ok(self):
        """Hay derecho a opinar de camber en ESTA tanda? Sin superficie viva no hay
        canal; sin tiempo rodado no hay goma en regimen; sin curva cargada acumulada
        el indice direccional todavia no significa nada."""
        return (self._surf_alive
                and self._runtime >= MIN_NORM_S
                and (self._t_izq + self._t_der) >= CAMBER_MIN_LOAD_S)

    def _camber_now(self):
        """Spread por eje medido en la rueda que la pista carga (media de ambas si la
        pista no tiene lado dominante). {} si no hay derecho a opinar."""
        if not self._camber_ok():
            return {}
        lado = self.dir_side()
        out = {}
        for eje, (izq, der) in (("F", (0, 1)), ("R", (2, 3))):
            cs = (izq,) if lado == "I" else (der,) if lado == "D" else (izq, der)
            vals = [s for s in (self._spread(c) for c in cs) if s is not None]
            if vals:
                out[eje] = sum(vals) / len(vals)
        return out

    def _camber_hint(self, c, spread):
        """(texto, estado, banda) de la esquina c.

        estado: None sin medicion · "idle" hay numero pero no es medicion de camber
        (rueda descargada o tanda sin derecho a opinar) · "ok" · "warn".
        banda: [lo, hi] en C contra lo que se esta juzgando, para que la UI pinte el
        verde donde de verdad esta el criterio en vez de una franja fija que miente."""
        if spread is None:
            return None, None, None
        # ORDEN: primero el derecho a opinar de la TANDA, despues el de la rueda. Al
        # reves, "sin carga" se publicaba a partir de un indice calculado sobre unos
        # pocos segundos -- basta una curva para saturarlo en +-1.00 -- y aparecia y
        # desaparecia antes de que el instrumento estuviera armado siquiera.
        if not self._camber_ok():
            # Se dice POR QUE no hay veredicto. Un rombo hueco mudo no se distingue de
            # una falla, y el piloto no tiene como saber si esperar o si algo se rompio.
            return ("calentando" if self._runtime < MIN_NORM_S else "sin curvas aun",
                    "idle", None)
        if not self._cargada(c):
            # La pista carga el otro lado. Esta goma no trabaja, el rolido no le come el
            # camber y su spread NO mide camber. Se muestra el numero, no se opina.
            return "sin carga", "idle", None
        prev = self._cam_prev.get("F" if c < 2 else "R")
        if prev is not None:
            # Auto-referencial: contra la propia tanda anterior de ESTE auto.
            d = spread - prev
            return (f"{'+' if d >= 0 else '-'}{abs(d):.1f}° vs previa",
                    "warn" if abs(d) >= CAMBER_DELTA else "ok",
                    [prev - CAMBER_DELTA, prev + CAMBER_DELTA])
        # Ventana de respaldo. No existe veredicto de "poco camber": el corpus no
        # autoriza esa afirmacion (ver CAMBER_OK_LO). Solo se opina en los extremos.
        banda = [CAMBER_OK_LO, CAMBER_OK_HI]
        if spread > CAMBER_OK_HI:
            return "mucho camber neg.", "warn", banda
        if spread < CAMBER_OK_LO:
            return "falta camber neg.", "warn", banda
        return "camber ok", "ok", banda

    def _axle(self):
        """Sesgo termico delantero-vs-trasero en carcasa: dice que eje trabaja mas."""
        f = [t for t in self._carcass[:2] if t is not None]
        r = [t for t in self._carcass[2:] if t is not None]
        if not f or not r:
            return {}
        fa, ra = sum(f) / len(f), sum(r) / len(r)
        diff = ra - fa
        if diff >= 8:
            bias = "tren trasero trabajando mas"
        elif diff <= -8:
            bias = "tren delantero trabajando mas"
        else:
            bias = "ejes parejos"
        return {"front": round(fa), "rear": round(ra), "diff": round(diff), "bias": bias}
