#!/usr/bin/env python3
"""Servidor de mentira para revisar la pagina de GOMAS sin estar en pista.

Sirve el MISMO index.html por HTTP (:8081) y emite el MISMO JSON que bridge_shm por
WebSocket (:8766), pero alimentado con escenarios armados a mano. Corre en puertos
propios para no pelearse con el bridge real (:8080 / :8765), que puede estar vivo.

TRES DECISIONES QUE IMPORTAN:

1. El objeto `tyres` NO se inventa aca: se construye una instancia real de
   ams2_tyres.TyreAnalyzer, se le escriben las lecturas suavizadas y se llama a su
   payload(). Asi el mock emite por definicion lo mismo que emite el bridge (mismas
   claves, mismos redondeos, mismos veredictos pstat/tdev/camber/axle). Si alguien
   cambia payload(), el mock cambia solo -- no queda validando una mentira.
   PERO OJO (leccion v3): el mock valida la MECANICA de la pagina, no el regimen
   termico. Los niveles de los escenarios estan calcados de sesiones reales de
   telemetry/; si cambias la logica termica, la verdad la dicta
   tools/tyre_replay.py contra las sesiones grabadas, no esta pantalla.

2. index.html tiene el puerto del WS hardcodeado (ws://<host>:8765). Como el mock vive
   en 8766, el HTML se reescribe AL VUELO al servirlo. El archivo del repo no se toca.

3. EL CONTRATO v3 (t_surf / t_bulk / eol / stint) NO SE DA POR SENTADO. Los agentes
   trabajan en paralelo, asi que el mock no asume que ams2_tyres ya lee la capa
   superficial ni que payload() ya acepta eol/stint. Escribe las lecturas en los
   atributos que el analizador REALMENTE tenga, le pasa a payload() solo los kwargs
   que su firma acepte, y lo que igual falte en el payload lo RELLENA el mock,
   avisando UNA vez por consola cual clave inyecto. Nunca pisa un valor que el backend
   ya haya calculado.
   Estado al escribir esto: el backend v3 YA aterrizo (`_t_surf`, `_t_bulk`,
   `payload(eol, stint)`), o sea el relleno esta dormido y no se imprime nada. Si
   alguna vez ves "[mock] backend sin corners[].eol", el dato que estas mirando lo
   invento el mock, no el analizador.

OJO: el onmessage del dash trata CUALQUIER mensaje sin `.dampers` como un frame de
estado completo. Por eso el mock nunca manda acks ni respuestas sueltas: solo frames.

Uso:
    .venv\\Scripts\\python.exe tools\\tyre_mock.py            # cicla escenarios cada 8 s
    .venv\\Scripts\\python.exe tools\\tyre_mock.py --scenario HOT   # arranca fijo en uno
Despues abrir http://localhost:8081 y entrar a la pagina de gomas.

Comandos por WS (para el QA, sin esperar el ciclo):
    {"cmd":"scenario","name":"DESVIADO"}   fija un escenario (congela el ciclo)
    {"cmd":"scenario","name":"auto"}       vuelve a ciclar
    {"cmd":"set_tyre_target","bar":1.90}   objetivo de presion (prueba el modal)
"""
import argparse
import asyncio
import http.server
import inspect
import json
import math
import os
import socket
import sys
import threading
import time

import websockets

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ams2_tyres

sys.stdout.reconfigure(line_buffering=True)   # el log sirve aunque la salida vaya a un pipe

WS_PORT = 8766
HTTP_PORT = 8081
EMIT_HZ = 15          # el watchdog del dash reconecta si pasan >3 s sin frame
SCENARIO_SECS = 8.0   # cuanto dura cada escenario en modo ciclo
ANIM_SECS = 8.0       # periodo de la rampa de temperatura del out-lap

# Objetivo de presion en caliente (bar). Lo pisa el comando set_tyre_target.
TARGET_BAR = ams2_tyres.DEFAULT_TARGET_BAR

# Escenario activo: None = ciclar automatico.
PINNED = None
_pin_t0 = 0.0


# ---------------- escenarios ----------------
# Cada uno devuelve las lecturas CRUDAS por esquina (orden FL, FR, RL, RR); el payload
# real (delta, psi, spread, rel, tdev, trend, camber, axle) lo deriva
# TyreAnalyzer.payload(). press en bar, temps en C, wear en fraccion 0..1, brake en C.
#
# EL REGIMEN TERMICO ES EL DE PISTA, NO EL DE BOX. La primera version de estos
# escenarios derivaba bulk = carcasa - 9, el offset de una sonda tomada con pista
# fria (carcasa media 57.9 C = out-lap). En pista de verdad (58 sesiones grabadas,
# tools/tyre_replay.py) la carcasa corre +12..+57 C sobre el bulk en autos vivos, las
# medianas de carcasa GT3/GT4 van de 95 a 143, y "SUP azul / CARC rojo" es el estado
# PERMANENTE de cualquier escala absoluta -- por eso la v3 salio saturada y por eso
# el QA no lo vio: valido 33 combinaciones dentro de un regimen que no ocurre
# manejando. Aca los escenarios anclan en `carcass` con niveles reales de GT3 y
# derivan bulk con _below(carc, ~28) y layer con _below(bulk, 3), que son los
# offsets medianos medidos en sesiones vivas. Si tocas estos numeros, corre el
# replay contra telemetry/ antes de creerle a la pantalla.
#
# Claves OPCIONALES por escenario (estado v3.1 que payload() ya no deriva de las
# lecturas sino de su propio estado interno):
#   carc_slow  norma lenta absoluta (default: la carcasa actual -> trend "stable";
#              ponla bajo la actual para simular warm-up -> trend "heat")
#   rel_slow   norma lenta de la ESTRUCTURA (default: la estructura actual ->
#              tdev None; correla para simular un evento -> tdev hot/cold)
#   runtime    segundos rodados (default 600; <120 apaga tdev, <45 apaga trend)
#   warm       bool (default True; el bridge real lo decide contra el plateau propio)
#   surf_alive bool (default True; False = modelo de banda muerto del juego ->
#              payload() emite SUP/BULK/EXT/INT/camber en null)
#   dir_izq /  segundos rodados cargando cada lado (default 120/120 = pista neutra con
#   dir_der    curva de sobra -> las 4 ruedas opinan de camber). Descompensalos para
#              simular un circuito direccional: el lado descargado pasa a "sin carga",
#              rombo hueco y sin banda verde, porque su spread NO mide camber.
#   cam_prev   {"F": C, "R": C} spread del eje cargado con que cerro la tanda ANTERIOR
#              de este auto (default {}: sin referencia -> ventana de respaldo +2..+13).
#              Con referencia el veredicto pasa a ser el delta contra ella.
#
# Ojo con la escala de los canales LATERALES (t_in / t_out): viven en la escala del
# BULK, no en la de la carcasa, porque mTyreTempCenter es byte-identico a mTyreTemp.
# Los SPREADS (t_in - t_out) mandan los veredictos de camber.
#
# `t_mid` ya no es una lectura propia del escenario: el "centro" no es un sensor
# independiente (mide lo mismo que el bulk), asi que se alimenta con el bulk.


def _below(vals, d):
    """Deriva una capa mas fria restando `d` C. Es la forma barata de respetar el
    gradiente medido sin recalcular cuatro numeros a mano en cada escenario."""
    return [None if v is None else v - d for v in vals]


def _sin_sesion(t):
    return dict(car="", compound="", live=False,
                press=[None] * 4, t_in=[None] * 4, t_out=[None] * 4,
                layer=[None] * 4, bulk=[None] * 4,
                carcass=[None] * 4, brake=[None] * 4, wear=[None] * 4,
                eol=[None] * 4, stint=0, warm=False, runtime=0)


def _frio_outlap(t):
    """Carcasa subiendo 55 -> 95 C en ANIM_SECS. warm=False (lejos del plateau) ->
    banner GOMA FRIA y deltas en "--"; carc_slow bajo la actual -> trend "heat" ->
    el spine dice CALENTANDO; runtime < 120 -> tdev apagado (aun no hay norma).
    El fill de CARC casi neutro: las 4 suben JUNTAS, la estructura no dice nada raro."""
    k = (t % ANIM_SECS) / ANIM_SECS
    c = 55.0 + 40.0 * k
    carc = [c, c - 2.0, c + 3.0, c + 1.0]
    bulk = _below(carc, 14.0)               # el offset real crece con la temperatura
    p = 1.58 + 0.14 * k                     # la presion sube con la temperatura
    return dict(car="Mercedes-AMG GT4", compound="Slick", live=True,
                press=[round(p + i * 0.01, 3) for i in range(4)],
                t_in=[b + 4.0 for b in bulk], t_out=[b - 2.0 for b in bulk],
                layer=_below(bulk, 3.0), bulk=bulk,
                carcass=carc,
                brake=[110 + 90 * k, 108 + 90 * k, 90 + 70 * k, 88 + 70 * k],
                wear=[0.015, 0.014, 0.012, 0.013],
                eol=[None] * 4,             # out-lap: el rate todavia no existe
                stint=1, warm=False, runtime=90,
                carc_slow=[v - 9.0 for v in carc])   # subiendo -> trend heat


def _caliente_ok(t):
    """Regimen de crucero GT3 REAL: carcasa 108-117 (si, sobre 100: eso es normal,
    no una alarma), bulk ~28 abajo, presion en objetivo. Estructura suave (rel -4..+4)
    -> las 4 bandas CARC casi neutras, tdev apagado, trend estable, EN TEMP."""
    carc = [112, 108, 117, 114]
    bulk = _below(carc, 28)
    return dict(car="Porsche 911 GT3 R", compound="Slick Soft", live=True,
                press=[TARGET_BAR] * 4,
                t_in=[87, 83, 92, 89], t_out=[81, 77, 86, 83],   # spread 6 -> camber ok
                layer=_below(bulk, 3), bulk=bulk,
                carcass=carc,
                brake=[420, 415, 330, 325],
                wear=[0.21, 0.23, 0.18, 0.19],
                eol=[27.0, 25.5, 33.0, 32.0],
                stint=8)


def _desviado(t):
    """Deltas +0.10 (falta presion) y -0.06 (sobra), spread +14 en FL (mucho camber
    negativo -> warn) y -2 en FR (negativo -> falta camber). Estructura direccional
    tipo Interlagos: el lado derecho carga (+8/-8) -> FR/RR tibias en el fill, y eso
    es INFORMACION de pista, no alarma (tdev apagado: la norma ya lo sabe)."""
    lo = TARGET_BAR - 0.10       # delta = target - press = +0.10  -> pstat "low"
    hi = TARGET_BAR + 0.06       # delta = -0.06                   -> pstat "high"
    carc = [106, 120, 104, 118]  # derecha cargada: rel ~ -6/+8/-8/+6
    bulk = _below(carc, 28)
    return dict(car="BMW M4 GT3", compound="Slick Medium", live=True,
                press=[lo, hi, lo, hi],
                t_in=[98, 96, 79, 93], t_out=[84, 98, 73, 87],   # spread 14 / -2 / 6 / 6
                layer=_below(bulk, 3), bulk=bulk,
                carcass=carc,
                brake=[455, 448, 340, 336],
                wear=[0.52, 0.31, 0.28, 0.27],
                eol=[11.5, 19.0, 21.0, 22.0],
                stint=14)


def _direccional(t):
    """Goiania: 100% curvas a izquierda, o sea CARGAN LAS DERECHAS. Reproduce los
    numeros medidos con el Audi R8 GT3 (FL +2 / FR +9 / RL +1 / RR +8) que eran el
    falso positivo: las izquierdas no trabajan, sus bordes se igualan, y el umbral
    absoluto por rueda las acusaba de "poco camber neg." sin arreglo posible.
    Lo que hay que ver en pantalla: FL/RL con rombo HUECO, sin banda verde y el texto
    "sin carga"; FR/RR con rombo lleno, banda y veredicto. Ademas lleva cam_prev, asi
    que las cargadas comparan contra su tanda anterior (+9 -> "0.0 vs previa")."""
    carc = [99, 81, 108, 91]         # medido en la carrera de Goiania
    bulk = _below(carc, 28)
    return dict(car="Audi R8 LMS GT3", compound="Slick Medium", live=True,
                press=[TARGET_BAR] * 4,
                # marco ABSOLUTO ya resuelto a interior/exterior por el analizador:
                # spread +2 / +9 / +1 / +8
                t_in=[72, 71, 81, 70], t_out=[70, 62, 80, 62],
                layer=_below(bulk, 3), bulk=bulk,
                carcass=carc,
                brake=[430, 425, 335, 330],
                wear=[0.19, 0.22, 0.16, 0.18],
                eol=[29.0, 26.0, 35.0, 33.0],
                stint=4,
                dir_izq=4.0, dir_der=210.0,      # indice -0.96 -> carga la derecha
                cam_prev={"F": 9.0, "R": 8.0})


def _evento_rl(t):
    """EL VEREDICTO NUEVO (v3.1): un trompo cocino la RL. Su carcasa esta +20 sobre
    la media (fill rojo profundo) y la ESTRUCTURA se movio contra la norma lenta
    (rel_slow) mas de DEV_REL -> tdev "hot" SOLO en RL: acento rojo de borde. Las
    otras tres quedan sin acento aunque esten a 108-116 C: calor absoluto no es
    evento. Esto reemplaza al viejo escenario HOT (4 carcasas >=100 = tstat hot),
    que contra pista real disparaba en conduccion normal en 8 de cada 10 autos."""
    carc = [110, 108, 138, 116]              # media 118 -> rel -8 / -10 / +20 / -2
    bulk = _below(carc, 28)
    return dict(car="Ferrari 488 GT3", compound="Slick Soft", live=True,
                press=[TARGET_BAR + 0.12] * 4,
                t_in=[86, 84, 112, 92], t_out=[77, 76, 103, 84],
                layer=_below(bulk, 3), bulk=bulk,
                carcass=carc,
                brake=[610, 605, 470, 465],
                wear=[0.71, 0.69, 0.55, 0.56],
                eol=[6.5, 7.2, 14.0, 13.5],
                stint=19,
                # la norma dice que RL solia correr +8 sobre la media -> dev +12 = hot
                rel_slow=[-5.0, -7.0, 8.0, 4.0])


def _canal_muerto(t):
    """El bug real de AMS2 (~10% de las sesiones grabadas): el modelo termico de
    banda NO corre -- bulk/layer/bordes pegados al ambiente -- pero la carcasa esta
    viva y alta. Numeros calcados de Kansai GT4 race 20260712. El payload debe emitir
    SUP/BULK/EXT/INT/spread/camber en null (em-dash en pantalla), el banner debe
    avisar SIN TERMICOS DE BANDA, y el fill de CARC sigue informando (rears cargadas).
    Si esta pantalla muestra un 25 pintado con paleta de estado, v3 volvio."""
    carc = [107, 104, 125, 127]
    return dict(car="Audi R8 LMS GT4", compound="Slick", live=True,
                press=[TARGET_BAR - 0.02] * 4,
                t_in=[25, 27, 31, 38], t_out=[27, 25, 38, 32],   # basura real del bug
                layer=[22, 22, 30, 30], bulk=[25, 25, 33, 34],
                carcass=carc,
                brake=[480, 470, 380, 375],
                wear=[0.30, 0.28, 0.36, 0.35],
                eol=[19.0, 21.0, 14.0, 14.5],
                stint=11, surf_alive=False)


def _detenido(t):
    """live=false: el auto esta parado (box/grilla). Las lecturas siguen siendo validas
    -- reflejan el enfriamiento (carc_slow sobre la actual -> trend "cool") -- pero
    el dash avisa DETENIDO. warm sigue true: recien salio de pista."""
    carc = [96, 95, 92, 93]
    bulk = _below(carc, 22)
    return dict(car="Porsche 911 GT3 R", compound="Slick Soft", live=False,
                press=[TARGET_BAR - 0.04] * 4,
                t_in=[76, 75, 73, 74], t_out=[70, 69, 67, 68],   # spread 6
                layer=_below(bulk, 3), bulk=bulk,
                carcass=carc,
                brake=[180, 176, 140, 138],
                wear=[0.34, 0.35, 0.30, 0.31],
                eol=[18.0, 17.5, 21.0, 20.5],
                stint=12, carc_slow=[v + 7.0 for v in carc])


def _parcial(t):
    """Campos null sueltos para probar los em-dashes. Cubre v2 y v3 de una:
    FL sin presion y SIN CAPA SUPERFICIAL, FR sin bulk y SIN EOL, RL sin carcasa
    (rompe el promedio del eje trasero) y sin t_in, RR sin t_out, sin desgaste y sin
    freno. Ninguna esquina queda entera, y ninguna queda sin nada."""
    return dict(car="Chevrolet Camaro GT4.R", compound="Slick", live=True,
                press=[None, TARGET_BAR + 0.02, TARGET_BAR - 0.01, TARGET_BAR],
                t_in=[84, 81, None, 82], t_out=[78, 77, 73, None],
                layer=[None, 75, 69, 73],          # FL sin t_surf  <- caso v3
                bulk=[80, None, 74, 78],           # FR sin bulk
                carcass=[109, 108, None, 107],     # RL sin carcasa: sin rel ni fill
                brake=[430, 425, 335, None],
                wear=[0.22, None, 0.19, None],
                eol=[12.0, None, 9.5, 20.0],       # FR sin eol     <- caso v3
                stint=6)


def _nombre_largo(t):
    """Overflow: nombre de auto y compuesto absurdamente largos en la barra de info."""
    carc = [110, 109, 112, 111]
    bulk = _below(carc, 28)
    return dict(car="Mercedes-AMG GT4 Evo Endurance Special Edition Nurburgring Test Car 2024",
                compound="Hipercompuesto Blando Experimental de Lluvia Extrema Ultra Slick",
                live=True,
                press=[TARGET_BAR + 0.01] * 4,
                t_in=[83, 82, 85, 84], t_out=[77, 76, 79, 78],   # spread 6
                layer=_below(bulk, 5), bulk=bulk,
                carcass=carc,
                brake=[410, 405, 320, 318],
                wear=[0.25, 0.26, 0.20, 0.21],
                eol=[24.0, 23.0, 29.5, 28.0],
                stint=3)


def _box_frio(t):
    """Parado Y frio: goma nueva antes de salir. Es el PRIMER estado de cada sesion y
    el que faltaba -- _detenido tiene la carcasa a ~70 (warm=True), asi que solo ejercia
    la rama parado-y-caliente. Aca el banner NO debe decir 'se esta enfriando': esta
    goma nunca estuvo caliente, y lo que el piloto necesita saber es por que no hay delta.

    El gradiente existe pero casi plano (23/26/28): la goma esta a temperatura ambiente,
    no hay energia entrando. Las 3 bandas deben salir azules y practicamente iguales."""
    return dict(car="Porsche 911 GT3 R", compound="Slick Soft", live=False,
                press=[TARGET_BAR - 0.22] * 4,
                # spread 2, pero con runtime=0 no hay derecho a opinar de camber:
                # el rombo sale hueco y sin banda. Correcto -- esta goma no ha girado.
                t_in=[26, 26, 25, 25], t_out=[24, 24, 23, 23],
                layer=[24, 24, 23, 23], bulk=[26, 26, 25, 25],
                carcass=[28, 28, 27, 27],
                brake=[40, 40, 38, 38],
                wear=[0.0, 0.0, 0.0, 0.0],
                eol=[None] * 4,             # goma nueva sin vueltas: no hay rate
                stint=0, warm=False, runtime=0)


def _gradiente_invertido(t):
    """SUP mas caliente que CARC: subviraje cronico, el tren delantero raspando la
    piel en cada curva. La capa superficial se dispara (118 / 116 C) mientras la
    carcasa sigue normal -- energia que entra por friccion de deslizamiento y no
    alcanza a difundir. En v3.1 esta firma se lee en los NUMEROS del corte (SUP > CARC
    invertido vs el orden normal), no en un fill: contra pista real la inversion
    ocurre 0-20% del tiempo y solo la piel del eje que desliza.

    Los traseros van sanos a proposito: en el mismo frame conviven el caso y su
    control, asi el QA compara sin cambiar de escenario. Nada mas debe gritar:
    presion en objetivo, camber ok, tdev apagado (la estructura no se movio)."""
    return dict(car="McLaren 720S GT3", compound="Slick Medium", live=True,
                press=[TARGET_BAR] * 4,
                t_in=[102, 100, 88, 89], t_out=[92, 91, 82, 83],  # spread 10/9/6/6 -> ok
                layer=[118, 116, 78, 79],      # DEL: SUP muy por encima de CARC
                bulk=[96, 95, 83, 84],
                carcass=[108, 106, 110, 111],
                brake=[520, 515, 380, 375],
                wear=[0.44, 0.46, 0.31, 0.32],
                eol=[13.0, 12.0, 24.0, 23.5],
                stint=11)


def _eol_corto(t):
    """Desgaste alto y EOL de pocas vueltas: el marcador '~N v' del .thead es el
    protagonista. FL ya cruzo el umbral operativo (82% > WEAR_THRESHOLD 80) -> eol 0.0
    y clase red del wear; FR queda a vuelta y media. Los traseros aguantan ~6.

    Goma gastada corriendo caliente y estable, presion en objetivo: lo unico que
    manda cambiar de goma es el par wear + eol."""
    carc = [113, 111, 109, 108]
    bulk = _below(carc, 28)
    return dict(car="BMW M4 GT3", compound="Slick Hard", live=True,
                press=[TARGET_BAR] * 4,
                t_in=[90, 89, 88, 87], t_out=[84, 83, 82, 81],   # spread 6
                layer=_below(bulk, 3), bulk=bulk,
                carcass=carc,
                brake=[480, 475, 360, 355],
                wear=[0.82, 0.79, 0.71, 0.72],
                eol=[0.0, 1.4, 6.2, 5.8],
                stint=34)


SCENARIOS = [
    ("SIN_SESION", _sin_sesion),
    ("BOX_FRIO", _box_frio),
    ("FRIO_OUTLAP", _frio_outlap),
    ("CALIENTE_OK", _caliente_ok),
    ("DESVIADO", _desviado),
    ("DIRECCIONAL", _direccional),
    ("GRADIENTE_INVERTIDO", _gradiente_invertido),
    ("EVENTO_RL", _evento_rl),
    ("CANAL_MUERTO", _canal_muerto),
    ("EOL_CORTO", _eol_corto),
    ("DETENIDO", _detenido),
    ("PARCIAL", _parcial),
    ("NOMBRE_LARGO", _nombre_largo),
]
BY_NAME = dict(SCENARIOS)


# ---------------- construccion del payload de gomas ----------------
# base_dir inexistente a proposito: TyreAnalyzer no encuentra tyre_targets.json, arranca
# con el default y NUNCA escribe en el repo (el bridge real usa ese archivo).
_an = ams2_tyres.TyreAnalyzer(base_dir=os.path.join(HERE, "tools", "_mock_no_state"))

# Que kwargs acepta payload() HOY. El contrato v3 dice que bridge_shm le pasa el vector
# de eol (y el stint); mientras el backend no aterrice, la firma es la vieja y estos
# datos los inyecta el mock mas abajo. Se mira la firma en vez de probar con try/except
# TypeError: ese except se tragaria un TypeError nacido DENTRO de payload().
_PAY_PARAMS = set(inspect.signature(_an.payload).parameters)

# Atributos por canal. `_t_surf`/`_t_bulk` son los del backend v3 ya aterrizado;
# `_t_mid` es el nombre v2 del bulk (el "centro", que medía lo mismo) y queda como
# fallback por si el mock corre contra un ams2_tyres viejo. Se escribe en todos los que
# EXISTAN y no se crea ninguno: inventar un atributo que payload() no lee seria
# pintarle al QA un dato que el bridge real nunca va a emitir.
_CHAN_ATTRS = {
    "press":   ("_press",),
    "t_in":    ("_t_in",),
    "t_out":   ("_t_out",),
    "layer":   ("_t_surf",),
    "bulk":    ("_t_bulk", "_t_mid"),
    "carcass": ("_carcass",),
    "brake":   ("_brake",),
    "wear":    ("_wear",),
}


def _f(vals):
    """A float: en el bridge real todo pasa por el EMA y sale float. Escribir un int
    aca haria que payload() emitiera 14 donde el bridge emite 14.0 -- misma pantalla,
    pero el mock dejaria de ser byte-comparable con el real."""
    return [None if v is None else float(v) for v in vals]


def _r(v):
    """Redondeo igual al de payload() para las temps (int o None)."""
    return None if v is None else round(v)


def _write(chan, vals):
    """Escribe un canal en el/los atributos que el analizador REALMENTE tenga."""
    vals = _f(vals)
    for name in _CHAN_ATTRS[chan]:
        if isinstance(getattr(_an, name, None), list):
            setattr(_an, name, list(vals))


_warned = set()


def _fill(obj, key, val, label):
    """Completa una clave del contrato v3 que el backend todavia no emite.

    Nunca pisa un valor ya calculado: solo escribe si la clave falta o vino en None
    teniendo el escenario un dato (sintoma de que payload() leyo otro atributo del que
    escribimos). Si el escenario tampoco tiene dato, igual se asegura que la clave
    EXISTA en null -- una clave ausente y una en null se ven igual en el frontend, pero
    no en un test que compare esquemas."""
    if val is None:
        obj.setdefault(key, None)
        return
    if obj.get(key) is None:
        if label not in _warned:
            _warned.add(label)
            print(f"[mock] backend sin {label}: lo inyecta el mock (contrato v3)")
        obj[key] = val


def tyres_payload(scn, t):
    d = scn(t)
    _an._car = d["car"]
    _an._compound = d["compound"]
    _an._live = d["live"]
    for chan in ("press", "t_in", "t_out", "layer", "bulk", "carcass", "brake", "wear"):
        _write(chan, d[chan])
    _an._targets = {d["car"]: TARGET_BAR}      # objetivo vigente, tambien con car=""

    # --- estado v3.1: payload() ya no deriva warm/tdev/trend de las lecturas sino de
    # su estado interno (normas lentas, tiempo rodado, liveness del modelo de banda).
    # Defaults = regimen estacionario sano: norma == actual (tdev off, trend stable),
    # 10 min rodados, warm, modelo de banda vivo. hasattr y no setattr a ciegas: si
    # esto corre contra un ams2_tyres viejo, no inventamos atributos que no lee.
    carc = _f(d["carcass"])
    if hasattr(_an, "_slow"):
        _an._slow = _f(d.get("carc_slow", d["carcass"]))
    if hasattr(_an, "_rel_slow"):
        avail = [v for v in carc if v is not None]
        if "rel_slow" in d:
            _an._rel_slow = _f(d["rel_slow"])
        elif len(avail) >= 2:
            mean = sum(avail) / len(avail)
            _an._rel_slow = [None if v is None else v - mean for v in carc]
        else:
            _an._rel_slow = [None] * 4
    if hasattr(_an, "_runtime"):
        _an._runtime = float(d.get("runtime", 600.0))
    if hasattr(_an, "_warm"):
        _an._warm = bool(d.get("warm", True))
    if hasattr(_an, "_surf_alive"):
        _an._surf_alive = bool(d.get("surf_alive", True))
    # Direccionalidad de la pista y referencia de camber: por default pista neutra con
    # curva cargada de sobra, que es el caso donde las 4 ruedas tienen veredicto.
    if hasattr(_an, "_t_izq"):
        _an._t_izq = float(d.get("dir_izq", 120.0))
        _an._t_der = float(d.get("dir_der", 120.0))
    if hasattr(_an, "_cam_prev"):
        _an._cam_prev = dict(d.get("cam_prev", {}))

    eol = d.get("eol", [None] * 4)
    stint = d.get("stint", 0)
    kw = {}
    if "eol" in _PAY_PARAMS:
        kw["eol"] = eol
    if "stint" in _PAY_PARAMS:
        kw["stint"] = stint
    p = _an.payload(**kw)

    for i, c in enumerate(p.get("corners", [])):
        # con el modelo de banda muerto (surf_dead) los null de t_surf/t_bulk son
        # VEREDICTO del backend, no una clave que falte: no rellenar sobre ellos
        if not p.get("surf_dead"):
            _fill(c, "t_surf", _r(d["layer"][i]), "corners[].t_surf")
            _fill(c, "t_bulk", _r(d["bulk"][i]), "corners[].t_bulk")
        _fill(c, "eol", None if eol[i] is None else round(float(eol[i]), 1),
              "corners[].eol")
    _fill(p, "stint", stint, "stint")
    return p


def base_frame(t):
    """Frame completo: TODOS los campos que lee applyStatic() en index.html. Si falta
    uno el dash muestra basura y el QA valida cualquier cosa."""
    rpm = int(6100 + 2100 * math.sin(t * 1.8))
    thr = 92 if rpm > 6000 else 35
    brk = 0 if rpm > 6000 else 60
    return {
        "connected": True,
        "speed_kmh": round(150 + 70 * math.sin(t * 1.8)),
        "rpm": rpm,
        "max_rpm": 8300,
        "gear": 4,
        "throttle": thr,
        "brake": brk,
        "fuel_liters": round(max(4.0, 48.0 - (t % 600) * 0.03), 1),
        "fuel_capacity": 65,
        "split_ahead": 0.214,
        "split_behind": 1.103,
        "event_remaining": 1325.0,
        "position": 3,
        "num_participants": 20,
        "current_lap": 5,
        "current_time": round(t % 95, 3),
        "last_lap": 95.610,
        "best_lap": 94.880,
        "water_temp": 91,
        "oil_temp": 104,
        "pit_limiter": False,
        "abs_active": brk > 50,
        "tc_active": thr > 80,
        "engine_warning": False,
        "fuel_per_lap": 2.85,
        "fuel_laps_left": 16,
        "leaderboard": [],
        # Minimo coherente: renderStrategy corta temprano con live=false -> "Sin sesion".
        "strategy": {"calibrating": True, "live": False, "mode": "none", "session": ""},
    }


def build_frame():
    now = time.monotonic()
    if PINNED:
        name, scn = PINNED, BY_NAME[PINNED]
        t_scn = now - _pin_t0
    else:
        i = int(now / SCENARIO_SECS) % len(SCENARIOS)
        name, scn = SCENARIOS[i]
        t_scn = now % SCENARIO_SECS
    f = base_frame(now)
    f["tyres"] = tyres_payload(scn, t_scn)
    return name, json.dumps(f)


# ---------------- WebSocket ----------------
CLIENTS = set()


async def ws_handler(ws):
    global PINNED, _pin_t0, TARGET_BAR
    CLIENTS.add(ws)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            cmd = msg.get("cmd")
            if cmd == "scenario":
                name = str(msg.get("name", "")).upper()
                if name in ("AUTO", ""):
                    PINNED = None
                    print("[mock] escenario: AUTO (ciclando)")
                elif name in BY_NAME:
                    PINNED = name
                    _pin_t0 = time.monotonic()
                    print(f"[mock] escenario fijado: {name}")
                else:
                    print(f"[mock] escenario desconocido: {name}")
            elif cmd == "set_tyre_target":
                try:
                    v = float(msg.get("bar"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(v) and 0.5 <= v <= 4.0:
                    TARGET_BAR = round(v, 2)
                    print(f"[mock] objetivo -> {TARGET_BAR} bar")
            # Cualquier otro comando (set_race, set_telemetry, ...) se ignora en silencio:
            # NO responder nada, el dash tomaria la respuesta como frame de estado.
    finally:
        CLIENTS.discard(ws)


async def pump():
    period = 1.0 / EMIT_HZ
    last_name = None
    while True:
        await asyncio.sleep(period)
        name, msg = build_frame()
        if name != last_name:
            print(f"[mock] escenario: {name}")
            last_name = name
        for ws in list(CLIENTS):
            try:
                await ws.send(msg)
            except websockets.ConnectionClosed:
                CLIENTS.discard(ws)


# ---------------- HTTP ----------------
class _MockHandler(http.server.SimpleHTTPRequestHandler):
    """Sin cache (mismo motivo que el bridge: el celular sirve versiones viejas) y con
    el puerto del WebSocket reescrito 8765 -> 8766 al vuelo, para que el dash servido
    aca hable con el mock y no con el bridge real."""

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
                    html = f.read()
            except OSError:
                self.send_error(500, "no se pudo leer index.html")
                return
            html = html.replace("location.hostname}:8765", f"location.hostname}}:{WS_PORT}")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


def serve_http():
    handler = lambda *a, **kw: _MockHandler(*a, directory=HERE, **kw)
    http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), handler).serve_forever()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def main():
    threading.Thread(target=serve_http, daemon=True).start()
    ip = lan_ip()
    print(f"[mock] MOCK de gomas (no toca AMS2 ni el bridge real de :8765/:8080)")
    print(f"[mock] WS   : ws://{ip}:{WS_PORT}")
    print(f"[mock] Dash : http://localhost:{HTTP_PORT}   (o http://{ip}:{HTTP_PORT})")
    print(f"[mock] Escenarios: {', '.join(n for n, _ in SCENARIOS)}")
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        await pump()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", help="arrancar fijo en un escenario (si no, cicla)")
    args = ap.parse_args()
    if args.scenario:
        n = args.scenario.upper()
        if n not in BY_NAME:
            sys.exit(f"escenario desconocido: {n}\nvalidos: {', '.join(BY_NAME)}")
        PINNED = n
        _pin_t0 = time.monotonic()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[mock] detenido")
