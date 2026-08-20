#!/usr/bin/env python3
"""Tests de ams2_tyres.TyreAnalyzer (el que alimenta la pagina GOMAS).

Existia con cobertura CERO: tools/test_tyres.py testea el analizador OFFLINE
(analyze_telemetry), no este modulo. Lo cazo una revision adversarial.

Cubre las propiedades que hacen HONESTO al instrumento -- las que, si se rompen,
vuelven a producir la clase de bug que costo dos redisenos:
  * rel es suma cero (no puede pintar las 4 ruedas del mismo lado)
  * los centinelas no generan veredicto
  * NaN/faltantes no envenenan el broadcast (un NaN en el JSON tumba el dash entero)
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ams2_tyres  # noqa: E402

NOWHERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sin_estado_")
_fails = []


def _ok(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        _fails.append(name)


class Snap:
    """Snapshot minimo con lo que lee TyreAnalyzer.update().

    `lat` = mLocalAcceleration[0]: >0 CARGA LAS DERECHAS (signo fijado por temperatura,
    corr -0.895 con el calor izq-der; NO por susp_travel, que va al reves).
    `tl`/`tr` = bordes por rueda en marco ABSOLUTO del auto (mTyreTempLeft/Right); por
    default ambos en `edges`, o sea spread 0."""

    def __init__(self, edges=62.0, carc=(83, 103, 56, 70), bulk=80.0, press=200.0,
                 speed=0.0, lat=0.0, tl=None, tr=None, track=b""):
        self.mCarName = b"Test Car"
        self.mTrackLocation = track
        self.mTyreCompound = [b"Liso"] * 4
        self.mSpeed = speed
        self.mLocalAcceleration = [lat, 0.0, 0.0]
        self.mAirPressure = [press] * 4
        self.mTyreCarcassTemp = [t + 273.15 for t in carc]     # KELVIN, como la SHM
        self.mTyreLayerTemp = [t + 273.15 for t in (49, 53, 43, 47)]
        self.mTyreTemp = [bulk] * 4                            # bulk ya viene en Celsius
        self.mBrakeTempCelsius = [189, 195, 77, 77]
        self.mTyreTempLeft = list(tl) if tl else [edges] * 4
        self.mTyreTempRight = list(tr) if tr else [edges] * 4


def _run(snap, n=40):
    a = ams2_tyres.TyreAnalyzer(base_dir=NOWHERE)
    for _ in range(n):
        a.update(snap)
    return a.payload()


# Caso Goiania medido con el Audi R8 GT3: FL +2 / FR +9 / RL +1 / RR +8.
# La pista CARGA LAS IZQUIERDAS (carcasa 106/114 C contra 87/96 en las derechas), asi que
# las de veredicto son la FL y la RL -- las de spread BAJO. El rolido se come el camber
# de la rueda de afuera y le calienta el hombro exterior; la de adentro conserva su
# camber y muestra un spread alto que no mide nada. Ver nota 6b de ams2_tyres.
# INNER_IS_RIGHT = (True, False, True, False) -> en FL/RL el interior es mTyreTempRight.
GOIANIA_TL = [60.0, 69.0, 60.0, 68.0]    # mTyreTempLeft por rueda
GOIANIA_TR = [62.0, 60.0, 61.0, 60.0]    # mTyreTempRight por rueda


def _tanda(a=None, secs=300.0, t0=0.0, base_dir=NOWHERE, **kw):
    """Rueda `secs` s a 50 km/h con reloj inyectado. Devuelve (analizador, reloj)."""
    a = a or ams2_tyres.TyreAnalyzer(base_dir=base_dir)
    t = t0
    while t < t0 + secs:
        t += 0.5
        a.update(Snap(speed=50.0, **kw), now=t)
    return a, t


def test_centinela_de_bordes():
    """Bordes en 0.0 EXACTO = el auto no esta en pista, no una medicion.

    Regresion real: con el auto en garage los bordes leen 0.0 y la pagina dictaba
    'poco camber neg.' a partir de un spread de 0.0. Un neumatico real nunca marca
    0 C en el borde ni frio (el ambiente anda en 15-30)."""
    c = _run(Snap(edges=0.0))["corners"][0]
    _ok("bordes en 0 -> sin t_in/t_out", c["t_in"] is None and c["t_out"] is None)
    _ok("bordes en 0 -> sin spread", c["spread"] is None)
    _ok("bordes en 0 -> SIN veredicto de camber", c["camber"] is None, repr(c["camber"]))
    # pero con bordes REALES y una tanda rodada de verdad si se opina
    a, _ = _tanda(lat=0.0, tl=GOIANIA_TL, tr=GOIANIA_TR, secs=300.0)
    # sin G lateral no hay curva cargada acumulada -> tampoco hay derecho a opinar
    p = a.payload()
    _ok("rodando en recta -> sin veredicto (nunca cargo)",
        all(c["cstat"] == "idle" and c["cband"] is None for c in p["corners"]),
        [c["cstat"] for c in p["corners"]])
    # ...pero DICIENDO por que: un rombo hueco mudo no se distingue de una falla
    _ok("el idle explica el motivo",
        all(c["camber"] == "sin curvas aun" for c in p["corners"]),
        [c["camber"] for c in p["corners"]])
    _ok("...y el spread SI se muestra", p["corners"][1]["spread"] == 9.0,
        repr(p["corners"][1]["spread"]))


def test_rel_es_suma_cero():
    """rel = carcasa - media(4). La propiedad que impide el modo de falla de la v3
    (todo rojo siempre): si suma cero, alguna esquina esta siempre bajo la media."""
    cs = _run(Snap(carc=(120, 118, 122, 119)))["corners"]
    rels = [c["rel"] for c in cs]
    _ok("rel presente en las 4", all(r is not None for r in rels), rels)
    if all(r is not None for r in rels):
        _ok("rel suma ~0", abs(sum(rels)) < 0.2, f"suma={sum(rels):.3f}")
        _ok("no todas del mismo lado", not (all(r > 0 for r in rels) or all(r < 0 for r in rels)))
    # carcasas ALTAS pero parejas: rel ~0 en todas -> el instrumento NO grita
    cs2 = _run(Snap(carc=(140, 140, 140, 140)))["corners"]
    _ok("carcasa alta pero pareja -> rel ~0 (no alarma por caliente)",
        all(abs(c["rel"]) < 0.5 for c in cs2 if c["rel"] is not None),
        [c["rel"] for c in cs2])


def test_nan_no_envenena():
    """Un NaN en el payload = json.dumps escribe 'NaN' = JSON invalido = el JSON.parse
    del navegador tira y el dash COMPLETO deja de actualizarse, no solo gomas."""
    import json
    s = Snap()
    s.mTyreCarcassTemp = [float("nan")] * 4
    s.mAirPressure = [float("inf")] * 4
    p = _run(s)
    try:
        json.dumps(p, allow_nan=False)
        _ok("payload con NaN/inf serializa a JSON estricto", True)
    except ValueError as e:
        _ok("payload con NaN/inf serializa a JSON estricto", False, str(e))


def test_campo_faltante():
    """Una version del juego (o un mock) sin un canal no debe tumbar el analizador:
    el bridge atrapa la excepcion pero se queda con el frame anterior en silencio."""
    s = Snap()
    del s.mTyreLayerTemp
    try:
        _run(s)
        _ok("canal faltante no revienta", True)
    except AttributeError:
        _ok("canal faltante no revienta", False, "AttributeError")





def test_no_opina_detenido():
    """Con el auto parado el patron termico deja de venir de la conduccion: se aplana
    solo y el rel se desploma contra su norma. Medido en vivo: 8 de 19 alarmas de una
    sesion de 40 min salieron bajo 15 km/h (boxes y grilla)."""
    # OJO con la semantica: tdev dispara con un CAMBIO contra la norma propia, no con
    # una asimetria estable (una constante la absorbe la EMA lenta y deja de ser noticia).
    # Asi que primero se establece la norma parejo, y RECIEN AHI se cocina una esquina.
    a = ams2_tyres.TyreAnalyzer(base_dir=NOWHERE)
    t = 0.0
    for _ in range(400):                       # norma: las 4 parejas
        t += 0.5
        a.update(Snap(carc=(120, 120, 120, 120), speed=50.0), now=t)
    for _ in range(60):                        # rodando, la FR se cocina
        t += 0.5
        a.update(Snap(carc=(120, 150, 118, 119), speed=50.0), now=t)
    parado = a.payload()
    for _ in range(60):                        # mismo desbalance, pero DETENIDO
        t += 0.5
        a.update(Snap(carc=(120, 150, 118, 119), speed=0.0), now=t)
    p = a.payload()
    _ok("detenido -> sin alarma", all(c["tdev"] is None for c in p["corners"]),
        [c["tdev"] for c in p["corners"]])
    _ok("pero rodando si opinaba", any(c["tdev"] for c in parado["corners"]),
        [c["tdev"] for c in parado["corners"]])


def test_salto_de_sesion_no_alarma():
    """Las 4 carcasas saltando juntas = cambio de sesion o de gomas. Medido en vivo:
    125 -> 58 C en un frame al rotar de sesion, y la alarma sonaba en las 4 esquinas."""
    a = ams2_tyres.TyreAnalyzer(base_dir=NOWHERE)
    t = 0.0
    for _ in range(400):
        t += 0.5
        a.update(Snap(carc=(125, 129, 105, 106), speed=50.0), now=t)
    for _ in range(10):                        # gomas nuevas: las 4 se desploman juntas
        t += 0.5
        a.update(Snap(carc=(58, 51, 46, 53), speed=50.0), now=t)
    p = a.payload()
    _ok("salto simultaneo -> sin alarma", all(c["tdev"] is None for c in p["corners"]),
        [c["tdev"] for c in p["corners"]])
    # pero UNA sola esquina moviendose sigue siendo un evento de verdad
    b = ams2_tyres.TyreAnalyzer(base_dir=NOWHERE)
    t = 0.0
    for _ in range(400):
        t += 0.5
        b.update(Snap(carc=(120, 120, 120, 120), speed=50.0), now=t)
    for _ in range(60):
        t += 0.5
        b.update(Snap(carc=(120, 120, 145, 120), speed=50.0), now=t)
    p2 = b.payload()
    _ok("una sola esquina -> SI alarma", p2["corners"][2]["tdev"] == "hot",
        [c["tdev"] for c in p2["corners"]])


def test_camber_solo_en_la_rueda_cargada():
    """LA regresion reportada: "siempre dice poco camber negativo, incluso con el maximo
    camber negativo posible". Dos defectos encadenados, los dos medidos contra el corpus:

    1. El veredicto se emitia en las CUATRO ruedas, pero solo la que la pista carga mide
       camber (la asimetria izq-der del spread correlaciona 0.85 con la direccionalidad).
    2. La ventana [3,12] estaba centrada en la distribucion de la rueda DESCARGADA
       (mediana +8.2) y no en la CARGADA (+5.2), asi que acusaba al 27% de los ejes
       cargados del corpus. Ahi salia el "poco camber" perpetuo.

    Caso Goiania: cargan las izquierdas, FL +2 y RL +1. Esos numeros son NORMALES para
    una rueda cargada; con la ventana vieja los dos gritaban."""
    # accel_x < 0 => cargan las IZQUIERDAS (signo fijado por temperatura, no por
    # suspension: mas susp_travel es rueda extendida = descargada)
    a, _ = _tanda(lat=-8.0, tl=GOIANIA_TL, tr=GOIANIA_TR, secs=300.0)
    p = a.payload()
    cs = p["corners"]
    _ok("pista direccional detectada (carga la izquierda)", p["dir"] == "I", repr(p["dir"]))
    _ok("FR descargada -> sin veredicto de camber",
        cs[1]["cstat"] == "idle" and cs[1]["camber"] == "sin carga",
        f'{cs[1]["cstat"]} / {cs[1]["camber"]}')
    _ok("RR descargada -> sin veredicto de camber",
        cs[3]["cstat"] == "idle", repr(cs[3]["cstat"]))
    _ok("FR descargada -> tampoco se pinta banda verde", cs[1]["cband"] is None)
    # el numero se sigue mostrando: se deja de OPINAR, no de medir
    _ok("FR igual muestra su spread", cs[1]["spread"] == 9.0, repr(cs[1]["spread"]))
    _ok("FL cargada (+2) -> SI opina y esta ok",
        cs[0]["cstat"] == "ok" and cs[0]["camber"] == "camber ok",
        f'{cs[0]["cstat"]} / {cs[0]["camber"]}')
    _ok("RL cargada (+1) -> SI opina y esta ok", cs[2]["cstat"] == "ok",
        f'{cs[2]["cstat"]} / {cs[2]["camber"]}')
    # el falso positivo historico: +2 y +1 caian bajo el LO=3 de la ventana vieja
    _ok("ninguna rueda dice 'poco camber neg.'",
        not any("poco" in (c["camber"] or "") for c in cs),
        [c["camber"] for c in cs])


def test_pista_neutra_opinan_las_cuatro():
    """Sin lado dominante (Spa, Kansai, Hungaroring miden |indice| 0.00-0.14) las
    cuatro ruedas trabajan parecido y las cuatro tienen derecho a veredicto."""
    a = ams2_tyres.TyreAnalyzer(base_dir=NOWHERE)
    t = 0.0
    for i in range(600):            # alterna izquierda/derecha -> indice ~0
        t += 0.5
        a.update(Snap(speed=50.0, lat=8.0 if i % 2 else -8.0,
                      tl=GOIANIA_TL, tr=GOIANIA_TR), now=t)
    p = a.payload()
    _ok("pista neutra", p["dir"] == "=", repr(p["dir"]))
    _ok("las 4 opinan", all(c["cstat"] in ("ok", "warn") for c in p["corners"]),
        [c["cstat"] for c in p["corners"]])
    _ok("las 4 con banda", all(c["cband"] is not None for c in p["corners"]))


def test_referencia_propia_gana_al_umbral_absoluto():
    """El nucleo auto-referencial: cerrada una tanda, la siguiente se juzga contra ELLA
    y no contra una ventana absoluta que el corpus no autoriza a afirmar. Ruido
    inter-tanda medido del mismo auto: p90 3.6 C -> CAMBER_DELTA=4 separa el cambio de
    setup del ruido de pista."""
    tmp = tempfile.mkdtemp(prefix="ams2cam_")
    try:
        # --- tanda 1: pista que carga las IZQUIERDAS (lat > 0), spread +9 en la FL.
        # INNER_IS_RIGHT[FL] = True -> el interior de la FL es mTyreTempRight.
        tl = [60.0, 69.0, 60.0, 68.0]
        tr = [69.0, 60.0, 68.0, 60.0]      # FL int 69 - ext 60 = +9 · RL = +8
        a, t = _tanda(lat=-8.0, tl=tl, tr=tr, secs=300.0, base_dir=tmp)
        _ok("tanda 1: carga la izquierda", a.dir_side() == "I", a.dir_side())
        _ok("tanda 1: sin referencia usa la ventana de respaldo",
            a.payload()["corners"][0]["cband"] == [0.0, 10.0],
            repr(a.payload()["corners"][0]["cband"]))
        # --- rotar de sesion: las 4 carcasas saltan juntas -> cierra la tanda
        for _ in range(6):
            t += 0.5
            a.update(Snap(speed=50.0, lat=-8.0, tl=tl, tr=tr, carc=(30, 30, 30, 30)), now=t)
        guardado = json.load(open(os.path.join(tmp, "tyre_targets.json"), encoding="utf-8"))
        base = guardado.get("_camber", {}).get("Test Car", {})
        _ok("la tanda quedo persistida", abs(base.get("F", 0) - 9.0) < 0.5, repr(base))
        # --- tanda 2 en un bridge NUEVO (reinicio): el camber se movio a +14
        tl2 = [60.0, 74.0, 60.0, 73.0]
        tr2 = [74.0, 60.0, 73.0, 60.0]     # spread +14
        b, _ = _tanda(lat=-8.0, tl=tl2, tr=tr2, secs=300.0, base_dir=tmp)
        c = b.payload()["corners"][0]
        _ok("tanda 2: juzga contra SU referencia, no contra la ventana",
            c["cband"] == [6.0, 12.0], repr(c["cband"]))
        _ok("tanda 2: reporta el cambio contra la previa",
            c["camber"] == "+5.0° vs previa", repr(c["camber"]))
        _ok("tanda 2: +5 supera el ruido (3) -> avisa", c["cstat"] == "warn",
            repr(c["cstat"]))
        # un cambio DENTRO del ruido no debe gritar
        tl3 = [60.0, 71.0, 60.0, 70.0]
        tr3 = [71.0, 60.0, 70.0, 60.0]     # spread +11 -> delta +2, ruido
        d, _ = _tanda(lat=-8.0, tl=tl3, tr=tr3, secs=300.0, base_dir=tmp)
        c3 = d.payload()["corners"][0]
        _ok("un delta de +2 sigue bajo el umbral -> no avisa", c3["cstat"] == "ok",
            f'{c3["cstat"]} / {c3["camber"]}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_guarda_sin_derecho_a_opinar():
    """Una referencia envenenada es peor que ninguna: si la tanda no acumulo curva
    cargada (out-lap, vuelta de formacion, entrada a boxes) no se guarda nada."""
    tmp = tempfile.mkdtemp(prefix="ams2cam_")
    try:
        a, t = _tanda(lat=0.0, tl=GOIANIA_TL, tr=GOIANIA_TR, secs=300.0, base_dir=tmp)
        for _ in range(6):                  # salto de sesion sin haber curveado nunca
            t += 0.5
            a.update(Snap(speed=50.0, tl=GOIANIA_TL, tr=GOIANIA_TR,
                          carc=(30, 30, 30, 30)), now=t)
        _ok("sin curva cargada no persiste referencia",
            not os.path.exists(os.path.join(tmp, "tyre_targets.json")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_referencia_por_pista_y_auto():
    """La PISTA es parte de la llave. El spread del eje cargado del mismo auto cambia
    mas entre circuitos (ruido p90 4.3 C) que dentro de uno (2.1 C), asi que con la
    llave solo por auto, cambiar de trazado se reporta como si hubieras movido el setup
    -- y este piloto cambia de pista cada sesion."""
    tmp = tempfile.mkdtemp(prefix="ams2cam_")
    try:
        tl = [60.0, 69.0, 60.0, 68.0]
        tr = [69.0, 60.0, 68.0, 60.0]        # spread +9 en la FL cargada
        a, t = _tanda(lat=-8.0, tl=tl, tr=tr, secs=300.0, base_dir=tmp,
                      track=b"Interlagos")
        a.close()                            # cierra la tanda de Interlagos
        base = json.load(open(os.path.join(tmp, "tyre_targets.json"),
                              encoding="utf-8"))["_camber"]
        _ok("la llave lleva pista y auto", "Interlagos|Test Car" in base, list(base))
        # misma pista -> compara contra la referencia
        b, _ = _tanda(lat=-8.0, tl=tl, tr=tr, secs=300.0, base_dir=tmp,
                      track=b"Interlagos")
        _ok("misma pista -> hay referencia",
            b.payload()["corners"][0]["camber"] == "+0.0° vs previa",
            repr(b.payload()["corners"][0]["camber"]))
        # OTRA pista, mismo auto, sin tocar el setup -> NO debe inventar un cambio
        tl2 = [60.0, 74.0, 60.0, 73.0]
        tr2 = [74.0, 60.0, 73.0, 60.0]       # spread +14: tipico de otro trazado
        c, _ = _tanda(lat=-8.0, tl=tl2, tr=tr2, secs=300.0, base_dir=tmp,
                      track=b"Hungaroring")
        cor = c.payload()["corners"][0]
        _ok("pista nueva -> cae a la ventana, no a un delta falso",
            cor["cband"] == [0.0, 10.0] and "previa" not in (cor["camber"] or ""),
            f'{cor["cband"]} / {cor["camber"]}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_histeresis_del_lado_cargado():
    """Sin histeresis el veredicto de dos ruedas parpadeaba decenas de veces por tanda
    en las pistas que viven cerca de DIR_NEUTRAL (33 y 37 cambios en 11 min en Termas;
    21 de 58 sesiones del corpus). Una vez declarado el lado, soltarlo cuesta mas."""
    a = ams2_tyres.TyreAnalyzer(base_dir=NOWHERE)
    t = 0.0
    # se establece un lado con holgura (indice ~ -1)
    for _ in range(200):
        t += 0.5
        a.update(Snap(speed=50.0, lat=-8.0, tl=GOIANIA_TL, tr=GOIANIA_TR), now=t)
    _ok("lado declarado", a.dir_side() == "I", a.dir_side())
    # ahora se lo lleva justo bajo DIR_NEUTRAL pero sobre DIR_EXIT: NO debe soltarlo
    cambios = 0
    prev = a.dir_side()
    objetivo = (1.0 - 0.12) / 2.0 * (a._t_izq + a._t_der)   # indice ~0.12
    while a._t_der < objetivo:
        t += 0.5
        a.update(Snap(speed=50.0, lat=8.0, tl=GOIANIA_TL, tr=GOIANIA_TR), now=t)
        s = a.dir_side()
        if s != prev:
            cambios += 1
            prev = s
    idx = (a._t_izq - a._t_der) / (a._t_izq + a._t_der)
    _ok("en la zona gris NO suelta el lado", a.dir_side() == "I",
        f"idx={idx:+.3f} lado={a.dir_side()}")
    _ok("y no parpadeo ni una vez", cambios == 0, f"cambios={cambios}")
    # empujando bien adentro de la zona neutra si lo suelta
    while abs((a._t_izq - a._t_der) / (a._t_izq + a._t_der)) > 0.05:
        t += 0.5
        a.update(Snap(speed=50.0, lat=8.0, tl=GOIANIA_TL, tr=GOIANIA_TR), now=t)
    _ok("bien adentro de la zona neutra si suelta", a.dir_side() == "=", a.dir_side())


def test_runtime_vuelve_a_cero_al_rotar_sesion():
    """_runtime solo se limpiaba en reset() (cambio de auto), asi que pasados los
    primeros 120 s de vida del bridge el gate quedaba satisfecho PARA SIEMPRE y un
    out-lap de 20 s alcanzaba para pisar la referencia buena."""
    a, t = _tanda(lat=-8.0, tl=GOIANIA_TL, tr=GOIANIA_TR, secs=300.0)
    _ok("tanda armada", a._camber_ok())
    for _ in range(6):                       # rotacion de sesion
        t += 0.5
        a.update(Snap(speed=50.0, lat=-8.0, tl=GOIANIA_TL, tr=GOIANIA_TR,
                      carc=(30, 30, 30, 30)), now=t)
    _ok("tras rotar, el gate vuelve a exigir goma en regimen", not a._camber_ok(),
        f"runtime={a._runtime:.0f} carga={a._t_izq + a._t_der:.0f}")


def test_warm_no_sobrevive_al_cierre_de_tanda():
    """El delta de presion SOLO se muestra con la goma en su temperatura de trabajo
    (el frontend hace `frio = !y.warm`). warm se reseteaba en reset() --cambio de auto o
    de pista-- pero NO al cerrar la tanda, y en ese camino las dos EMAs de carcasa se
    re-siembran juntas al valor frio: gap = 0, que nunca cae bajo WARM_EXIT, asi que
    warm quedaba pegado en True de la tanda anterior.

    Reportado en vivo: el dash mandaba a AGREGAR presion leyendo la presion FRIA cuando
    en caliente ya estaba SOBRE el objetivo. El piloto subio presiones y el auto empeoro.
    El replay no puede cazar esto: solo ve vueltas limpias concatenadas, nunca el garaje
    ni el out-lap."""
    a = ams2_tyres.TyreAnalyzer(base_dir=NOWHERE)
    t = 0.0
    for _ in range(700):                      # tanda caliente y estable -> warm
        t += 0.5
        a.update(Snap(speed=50.0, carc=(115, 115, 115, 115), press=200.0), now=t)
    _ok("tanda caliente -> warm", a.payload()["warm"])
    for _ in range(10):                       # gomas nuevas: las 4 carcasas se desploman
        t += 0.5
        a.update(Snap(speed=50.0, carc=(40, 40, 40, 40), press=165.0), now=t)
    p = a.payload()
    _ok("cierre de tanda con goma fria -> se PIERDE warm", not p["warm"],
        f'warm={p["warm"]} carcasa={p["corners"][0]["carcass"]}')
    # y lo que de verdad importa: sin warm el frontend no muestra el delta de presion
    _ok("...que es lo que apaga el consejo de presion en frio",
        not p["warm"] and p["corners"][0]["press"] is not None,
        f'press={p["corners"][0]["press"]}')


def test_referencia_corrupta_no_envenena():
    """El archivo es editable a mano y sobrevive a versiones viejas: un valor basura
    no debe mover la banda verde a un lugar absurdo ni meter NaN en el broadcast."""
    import json as _json
    tmp = tempfile.mkdtemp(prefix="ams2cam_")
    try:
        with open(os.path.join(tmp, "tyre_targets.json"), "w", encoding="utf-8") as f:
            _json.dump({"Test Car": 1.8,
                        "_camber": {"Test Car": {"F": "NaN", "R": 9999.0}}}, f)
        tl = [60.0, 69.0, 60.0, 68.0]
        tr = [69.0, 60.0, 68.0, 60.0]
        a, _ = _tanda(lat=-8.0, tl=tl, tr=tr, secs=300.0, base_dir=tmp)
        p = a.payload()
        _ok("objetivo de presion intacto pese a la clave reservada", p["target"] == 1.8,
            repr(p["target"]))
        _ok("referencia corrupta ignorada -> cae a la ventana de respaldo",
            p["corners"][0]["cband"] == [0.0, 10.0], repr(p["corners"][0]["cband"]))
        _json.dumps(p, allow_nan=False)     # revienta si se colo un NaN
        _ok("payload sigue siendo JSON estricto", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("== ams2_tyres.TyreAnalyzer ==")
    test_centinela_de_bordes()
    test_rel_es_suma_cero()
    test_nan_no_envenena()
    test_campo_faltante()
    test_no_opina_detenido()
    test_salto_de_sesion_no_alarma()
    test_camber_solo_en_la_rueda_cargada()
    test_pista_neutra_opinan_las_cuatro()
    test_referencia_propia_gana_al_umbral_absoluto()
    test_no_guarda_sin_derecho_a_opinar()
    test_referencia_por_pista_y_auto()
    test_histeresis_del_lado_cargado()
    test_runtime_vuelve_a_cero_al_rotar_sesion()
    test_warm_no_sobrevive_al_cierre_de_tanda()
    test_referencia_corrupta_no_envenena()
    print(f"\n{'todo verde' if not _fails else 'FALLAS: ' + ', '.join(_fails)}")
    sys.exit(1 if _fails else 0)
