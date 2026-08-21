#!/usr/bin/env python3
"""Tests del StrategyEngine con snapshots sinteticos (sin AMS2 en vivo).

Valida la matematica verificada por el workflow: laps_to_go (timed/laps), fuel
projection, semaforo, fuel-to-save, stops, deteccion de unidades ms/min, guard
de capacidad. Correr: python tools/test_strategy.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ams2_strategy as S


class P:
    def __init__(self, laps_completed=0, current_lap=1, pos=1):
        self.mLapsCompleted = laps_completed
        self.mCurrentLap = current_lap
        self.mRacePosition = pos
        self.mIsActive = True
        self.mName = b"YO"


class Snap:
    """Mimica los campos de ams2_shm.SharedMemory que usa el engine."""
    def __init__(self, **kw):
        self.mVersion = 14
        self.mNumParticipants = 10
        self.mViewedParticipantIndex = 0
        self.mTrackLocation = b"Interlagos"
        self.mCarName = b"GT3 Test"
        self.mGameState = 2                       # PLAYING
        self.mSessionState = 5                     # RACE (los tests en vivo asumen carrera)
        self.mFuelLevel = 0.5                     # fraccion
        self.mFuelCapacity = 100.0                # litros
        self.mEventTimeRemaining = -1.0
        self.mSessionDuration = 0.0
        self.mSessionAdditionalLaps = 0
        self.mLapsInEvent = 0
        self.mEnforcedPitStopLap = -1
        self.mPitMode = 0
        self.mCurrentTime = 10.0
        self.mLastLapTime = 90.0
        self.mBestLapTime = 89.0
        self.mTyreWear = [0.0, 0.0, 0.0, 0.0]
        self.mTyreTemp = [85.0, 85.0, 85.0, 85.0]
        # _tyres() paso a leer la CARCASA (el bulk se muere en algunas sesiones). En KELVIN,
        # como la entrega la shared memory. Sin esto el archivo entero revienta en la primera
        # funcion con AttributeError: 0 PASS, 0 FAIL, ni un test llega a correr.
        self.mTyreCarcassTemp = [t + 273.15 for t in self.mTyreTemp]
        self.mTyreCompound = [b"Medium", b"Medium", b"Medium", b"Medium"]
        self.mRainDensity = 0.0                    # seco por defecto
        self.mTrackTemperature = 25.0
        self._p = P()
        self.__dict__.update(kw)

    @property
    def mParticipantInfo(self):
        return [self._p] * 64


def _ok(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")
    return cond


def feed_laps(e, snap_fn, n, fuel_start, fuel_per_lap, cap=100.0, lap_time=90.0,
              wear_per_lap=0.0):
    """Simula n cruces de meta consumiendo fuel_per_lap y desgastando gomas."""
    fuel = fuel_start
    for lap in range(n + 1):
        wear = min(0.99, wear_per_lap * lap)
        s = snap_fn(
            mFuelLevel=fuel / cap, mFuelCapacity=cap,
            mLastLapTime=lap_time, mCurrentTime=5.0,
            mTyreWear=[wear, wear, wear, wear],
        )
        s._p = P(laps_completed=lap, current_lap=lap + 1)
        e.update(s)
        fuel -= fuel_per_lap
    return fuel


def test_lap_race():
    print("test_lap_race (20 vueltas, 2.5 L/v):")
    e = S.StrategyEngine()
    # carrera por vueltas: 20 totales, vamos consumiendo
    fuel = 90.0
    for lap in range(6):
        s = Snap(mLapsInEvent=20, mLapsCompleted=lap, mFuelLevel=fuel / 100.0,
                 mLastLapTime=90.0, mCurrentTime=5.0)
        s._p = P(laps_completed=lap, current_lap=lap + 1)
        e.update(s)
        fuel -= 2.5
    o = e.payload()
    _ok("modo laps", o["mode"] == "laps", o["mode"])
    _ok("laps_remaining = 20-5 = 15", o["laps_remaining"] == 15, o["laps_remaining"])
    _ok("no calibrando", not o["calibrating"])
    _ok("fuel_per_lap ~2.5", abs(o["fuel_per_lap"] - 2.5) < 0.1, o["fuel_per_lap"])
    # quedan 15 v * 2.5 = 37.5 L; tenemos ~77.5 -> sobra
    _ok("fuel_at_end positivo", o["fuel_at_end_l"] > 0, o["fuel_at_end_l"])
    print(f"    -> status={o['fuel_status']} margin={o['margin_laps']}v stops={o['stops_min']}")


def test_timed_race_seconds():
    print("test_timed_race (reloj en SEGUNDOS, 600s, lap 90s):")
    e = S.StrategyEngine()
    fuel = 90.0
    for lap in range(4):
        s = Snap(mEventTimeRemaining=600.0 - lap * 90, mSessionDuration=10.0,  # 10 min
                 mFuelLevel=fuel / 100.0, mLastLapTime=90.0, mCurrentTime=0.0)
        s._p = P(laps_completed=lap, current_lap=lap + 1)
        e.update(s)
        fuel -= 3.0
    o = e.payload()
    _ok("modo timed", o["mode"] == "timed", o["mode"])
    _ok("time_scale = 1.0 (segundos)", e._time_scale == 1.0, e._time_scale)
    # restan ~330s tras 3 vueltas -> ceil(330/90)=4 vueltas
    _ok("laps_remaining ~ 4", o["laps_remaining"] in (3, 4, 5), o["laps_remaining"])
    print(f"    -> laps_rem={o['laps_remaining']} status={o['fuel_status']}")


def test_timed_race_millis():
    print("test_timed_race (reloj en MILISEGUNDOS, header v14):")
    e = S.StrategyEngine()
    fuel = 90.0
    for lap in range(4):
        # mEventTimeRemaining en ms: 600000 ms = 600 s; duracion 10 min
        s = Snap(mEventTimeRemaining=600000.0 - lap * 90000, mSessionDuration=10.0,
                 mFuelLevel=fuel / 100.0, mLastLapTime=90.0, mCurrentTime=0.0)
        s._p = P(laps_completed=lap, current_lap=lap + 1)
        e.update(s)
        fuel -= 3.0
    o = e.payload()
    _ok("time_scale = 0.001 (ms detectado)", e._time_scale == 0.001, e._time_scale)
    _ok("laps_remaining sano (~4, NO ~6000)", 2 <= o["laps_remaining"] <= 6, o["laps_remaining"])
    print(f"    -> laps_rem={o['laps_remaining']} (sin normalizar daria miles)")


def test_fuel_deficit():
    print("test_fuel_deficit (poco combustible -> fuel-to-save):")
    e = S.StrategyEngine()
    # 20 vueltas, consumo 3 L/v, pero arrancamos con poco
    fuel = 30.0
    for lap in range(4):
        s = Snap(mLapsInEvent=20, mLapsCompleted=lap, mFuelLevel=fuel / 100.0,
                 mLastLapTime=90.0, mCurrentTime=5.0)
        s._p = P(laps_completed=lap, current_lap=lap + 1)
        e.update(s)
        fuel -= 3.0
    o = e.payload()
    # quedan 16 v * 3 = 48 L; tenemos 18 -> deficit grande
    _ok("status red", o["fuel_status"] == "red", o["fuel_status"])
    _ok("fuel_at_end negativo", o["fuel_at_end_l"] < 0, o["fuel_at_end_l"])
    _ok("save_per_lap presente", o["save_per_lap"] is not None, o["save_per_lap"])
    _ok("save_level impossible", o["save_level"] == "impossible", o["save_level"])
    _ok("stops_min >= 1", o["stops_min"] >= 1, o["stops_min"])
    print(f"    -> save={o['save_per_lap']}L/v level={o['save_level']} stops={o['stops_min']} add={o.get('add_l')}")


def test_capacity_guard():
    print("test_capacity_guard (mFuelCapacity = 1.0 no fiable -> fallback):")
    e = S.StrategyEngine()
    fuel_frac = 0.5
    for lap in range(4):
        s = Snap(mLapsInEvent=20, mLapsCompleted=lap, mFuelCapacity=1.0,
                 mFuelLevel=fuel_frac, mLastLapTime=90.0, mCurrentTime=5.0)
        s._p = P(laps_completed=lap, current_lap=lap + 1)
        e.update(s)
        fuel_frac -= 0.02
    o = e.payload()
    _ok("cap cae a DEFAULT (100)", abs(o["cap_l"] - S.DEFAULT_TANK_L) < 0.1, o["cap_l"])


def test_tyre_wear():
    print("test_tyre_wear (desgaste creciente -> horizonte de gomas):")
    e = S.StrategyEngine()
    feed_laps(e, Snap, 8, 90.0, 1.0, lap_time=90.0, wear_per_lap=0.10)
    # forzar modo laps re-alimentando con mLapsInEvent
    fuel = 82.0
    for lap in range(8, 12):
        wear = min(0.99, 0.10 * lap)
        s = Snap(mLapsInEvent=30, mLapsCompleted=lap, mFuelLevel=fuel / 100.0,
                 mLastLapTime=90.0, mCurrentTime=5.0, mTyreWear=[wear]*4)
        s._p = P(laps_completed=lap, current_lap=lap + 1)
        e.update(s)
        fuel -= 1.0
    o = e.payload()
    _ok("direccion de wear detectada (no invertida)", e._wear_invert is False, e._wear_invert)
    _ok("tyre_horizon presente", o.get("tyre_horizon") is not None, o.get("tyre_horizon"))
    _ok("4 ruedas en payload", len(o.get("tyres", [])) == 4)
    print(f"    -> wear peor={o['tyres'][o['worst']]['w']}% horizonte={o.get('tyre_horizon')}v limiter={o.get('limiter')}")

    st0 = e.stint_laps()
    s2 = Snap(mLapsInEvent=30, mLapsCompleted=13, mFuelLevel=0.7,
              mLastLapTime=90.0, mCurrentTime=5.0, mTyreWear=[0.99] * 4)
    s2._p = P(laps_completed=13, current_lap=14)
    e.update(s2)
    _ok("stint_laps suma una vuelta por cruce", e.stint_laps() == st0 + 1,
        (st0, e.stint_laps()))


def test_eol_por_rueda():
    print("test_eol_por_rueda (vueltas que le quedan a CADA goma -> pagina de gomas):")
    e = S.StrategyEngine()
    # 10 vueltas al 5%/v: wear 0.50, rate ~0.05 -> (0.80-0.50)/0.05 = ~6 vueltas.
    feed_laps(e, Snap, 10, 90.0, 1.0, wear_per_lap=0.05)
    s = Snap(mLapsInEvent=30, mTyreWear=[0.50, 0.50, 0.50, 0.50])
    s._p = P(laps_completed=10, current_lap=11)
    e.update(s)
    eol = e.eol_vec(s)
    _ok("eol_vec 4 valores", len(eol) == 4, eol)
    _ok("todos finitos (ni None ni inf)",
        all(isinstance(v, float) and math.isfinite(v) for v in eol), eol)
    _ok("eol ~6 vueltas", all(4.5 < v < 8.0 for v in eol), [round(v, 2) for v in eol])
    _ok("eol == horizonte con 4 ruedas parejas",
        abs(eol[0] - e.payload()["tyre_horizon"]) < 0.6,
        (round(eol[0], 2), e.payload()["tyre_horizon"]))
    # Rueda pasada de umbral: se satura en 0, nunca negativo.
    s2 = Snap(mLapsInEvent=30, mTyreWear=[0.95, 0.50, 0.50, 0.50])
    s2._p = P(laps_completed=10, current_lap=11)
    _ok("rueda pasada de umbral -> 0.0, no negativo", e.eol_vec(s2)[0] == 0.0, e.eol_vec(s2))


def test_eol_sin_datos():
    print("test_eol_sin_datos (desgaste plano / sin muestras -> None, nunca inf):")
    e = S.StrategyEngine()
    s = Snap()
    s._p = P()
    e.update(s)
    _ok("recien arrancado -> 4 None", e.eol_vec(s) == [None] * 4, e.eol_vec(s))
    _ok("stint 0 al arrancar", e.stint_laps() == 0, e.stint_laps())
    # Sesion con desgaste apagado: vueltas verdes sin que se mueva mTyreWear -> plano.
    feed_laps(e, Snap, 10, 90.0, 1.0, wear_per_lap=0.0)
    _ok("desgaste plano -> 4 None (no inf)", e.eol_vec(s) == [None] * 4, e.eol_vec(s))


def test_planning_practice():
    print("test_planning (PRÁCTICA + formato manual -> plan de carrera):")
    e = S.StrategyEngine()
    e.set_race_plan("timed", 30, additional=0)     # carrera de 30 min cargada a mano
    # sesión de PRÁCTICA (mSessionState=1), sin datos de carrera en la SM
    fuel = 50.0
    for lap in range(5):
        s = Snap(mSessionState=S.SESSION_PRACTICE, mLapsInEvent=0, mEventTimeRemaining=-1.0,
                 mFuelLevel=fuel / 100.0, mLastLapTime=90.0, mCurrentTime=5.0)
        s._p = P(laps_completed=lap, current_lap=lap + 1)
        e.update(s)
        fuel -= 2.4
    o = e.payload()
    _ok("planning activo", o.get("planning") is True, o.get("planning"))
    _ok("session = práctica", o.get("session") == "práctica", o.get("session"))
    # 30 min / 90 s = 20 vueltas
    _ok("race_total_laps = 20", o.get("race_total_laps") == 20, o.get("race_total_laps"))
    _ok("combustible total > 0", o.get("race_fuel_total_l", 0) > 40, o.get("race_fuel_total_l"))
    _ok("largá con <= tanque", o.get("race_start_fuel_l") <= o["cap_l"], o.get("race_start_fuel_l"))
    _ok("race_stops presente", "race_stops" in o, o.get("race_stops"))
    _ok("sin voz en práctica", o.get("alerts") == [], o.get("alerts"))
    print(f"    -> {o['race_total_laps']}v · total {o['race_fuel_total_l']}L · largá {o['race_start_fuel_l']}L · {o['race_stops']} parada(s) · stint ~{o['stint_full_laps']}v")


def test_live_overrides_plan():
    print("test_live_overrides_plan (en CARRERA, los datos en vivo mandan sobre el plan):")
    e = S.StrategyEngine()
    e.set_race_plan("laps", 99)                     # plan absurdo a propósito
    fuel = 90.0
    for lap in range(5):
        s = Snap(mSessionState=S.SESSION_RACE, mLapsInEvent=20, mLapsCompleted=lap,
                 mFuelLevel=fuel / 100.0, mLastLapTime=90.0, mCurrentTime=5.0)
        s._p = P(laps_completed=lap, current_lap=lap + 1)
        e.update(s)
        fuel -= 2.5
    o = e.payload()
    _ok("NO planning (vivo manda)", not o.get("planning"), o.get("planning"))
    _ok("laps_remaining en vivo = 16", o.get("laps_remaining") == 16, o.get("laps_remaining"))
    _ok("session = carrera", o.get("session") == "carrera", o.get("session"))
    _ok("voz activa en carrera", isinstance(o.get("alerts"), list))


def test_all_laps_toggle():
    print("test_all_laps_toggle (vuelta lenta cuenta segun el toggle):")
    times = [100, 90, 90, 300]   # cruces en lap1,2,3 -> lap-times 90,90,300 (la ultima lenta)

    def run(all_laps):
        e = S.StrategyEngine()
        e.set_use_all_laps(all_laps)
        fuel = 90.0
        for lap, lt in enumerate(times):
            s = Snap(mLapsInEvent=30, mLapsCompleted=lap, mFuelLevel=fuel / 100.0,
                     mLastLapTime=lt, mCurrentTime=5.0)
            s._p = P(laps_completed=lap, current_lap=lap + 1)
            e.update(s)
            fuel -= 2.5
        return e.payload()

    o_all = run(True)
    o_filt = run(False)
    _ok("default refleja all_laps=True", o_all["all_laps"] is True)
    _ok("con TODAS: la vuelta lenta cuenta (3 muestras)", o_all["green_laps"] == 3, o_all["green_laps"])
    _ok("con FILTRO: la vuelta lenta se descarta (2 muestras)", o_filt["green_laps"] == 2, o_filt["green_laps"])
    print(f"    -> todas={o_all['green_laps']} muestras · filtrado={o_filt['green_laps']} muestras")


def test_no_false_fumes():
    print("test_no_false_fumes (carrera larga c/parada: NO alarma con tanque lleno):")
    # carrera por tiempo larga (mucho más que el tanque) -> fuel_at_end negativo a propósito,
    # pero el estanque tiene combustible de sobra AHORA: no debe gritar "sin combustible".
    e = S.StrategyEngine()
    fuel = 95.0
    for lap in range(5):
        s = Snap(mEventTimeRemaining=5400.0 - lap * 100, mSessionDuration=90.0,  # 90 min
                 mFuelLevel=fuel / 100.0, mLastLapTime=100.0, mCurrentTime=5.0)
        s._p = P(laps_completed=lap, current_lap=lap + 1)
        e.update(s)
        fuel -= 3.0
    o = e.payload()
    keys = [al["key"] for al in o.get("alerts", [])]
    _ok("stops_min >= 1 (carrera larga)", o.get("stops_min", 0) >= 1, o.get("stops_min"))
    _ok("tank_laps alto (>5)", o.get("tank_laps", 0) > 5, o.get("tank_laps"))
    _ok("NO alarma 'fumes' con tanque lleno", "fumes" not in keys, keys)
    _ok("NO alarma 'save' (vas a parar igual)", "save" not in keys, keys)

    # Ahora SÍ: tanque casi vacío (menos de 1 vuelta) -> fumes debe sonar.
    e2 = S.StrategyEngine()
    fuel = 95.0
    for lap in range(5):
        s = Snap(mEventTimeRemaining=5400.0 - lap * 100, mSessionDuration=90.0,
                 mFuelLevel=fuel / 100.0, mLastLapTime=100.0, mCurrentTime=5.0)
        s._p = P(laps_completed=lap, current_lap=lap + 1)
        e2.update(s)
        fuel -= 3.0
    # forzar estanque casi vacío en el último snapshot
    s = Snap(mEventTimeRemaining=4800.0, mSessionDuration=90.0, mFuelLevel=2.5 / 100.0,
             mLastLapTime=100.0, mCurrentTime=5.0)
    s._p = P(laps_completed=6, current_lap=7)
    e2.update(s)
    keys2 = [al["key"] for al in e2.payload().get("alerts", [])]
    _ok("SÍ alarma 'fumes' con <1 vuelta en tanque", "fumes" in keys2, keys2)
    print(f"    -> tanque lleno: {keys} · tanque vacío: {keys2}")


def test_calibrating():
    print("test_calibrating (1 sola vuelta -> calibrando):")
    e = S.StrategyEngine()
    s = Snap(mLapsInEvent=20, mLapsCompleted=0)
    e.update(s)
    s2 = Snap(mLapsInEvent=20, mLapsCompleted=1, mFuelLevel=0.47)
    s2._p = P(laps_completed=1, current_lap=2)
    e.update(s2)
    o = e.payload()
    _ok("calibrando con <2 vueltas verdes", o["calibrating"], o.get("green_laps"))


def feed_wet(e, rains, tracks, wet_temps, lap_times=None, laps_in_event=30, cap=100.0,
             surf_alive=True):
    """Carrera con goma de AGUA y condiciones que evolucionan (listas paralelas,
    una entrada por update; la 1ra inicializa, el resto cruzan meta).

    surf_alive=False simula el bug de AMS2 que deja mTyreTemp pegado al ambiente: es
    el estado REAL de 6 de las 10 sesiones de lluvia grabadas (ver crossover_replay)."""
    n = len(rains)
    lap_times = lap_times or [120.0] * n
    fuel = 90.0
    for i in range(n):
        s = Snap(mLapsInEvent=laps_in_event, mFuelLevel=fuel / cap, mFuelCapacity=cap,
                 mLastLapTime=lap_times[i], mCurrentTime=5.0,
                 mTyreCompound=[b"Lluvia"] * 4,
                 mRainDensity=rains[i], mTrackTemperature=tracks[i],
                 mTyreTemp=[wet_temps[i]] * 4)
        s._p = P(laps_completed=i, current_lap=i + 1)
        e.update(s, surf_alive=surf_alive)
        fuel -= 2.8
    return e.payload()


def test_crossover_dry_none():
    print("test_crossover_dry_none (seco/slick -> sin panel de cruce):")
    e = S.StrategyEngine()
    feed_laps(e, lambda **k: Snap(mLapsInEvent=30, **k), 5, 90.0, 2.8)
    o = e.payload()
    _ok("crossover None en seco", o.get("crossover") is None, o.get("crossover"))


def test_crossover_green_raining():
    print("test_crossover_green_raining (lluvia firme -> aguantá):")
    e = S.StrategyEngine()
    o = feed_wet(e, rains=[0.5, 0.5, 0.5, 0.5, 0.5],
                 tracks=[20.0, 20.0, 20.0, 20.0, 20.0],
                 wet_temps=[55, 55, 56, 55, 56])
    cx = o.get("crossover") or {}
    _ok("hay panel de cruce con goma de agua", cx.get("state") is not None, cx)
    _ok("estado green con lluvia firme", cx.get("state") == "green", cx.get("state"))
    keys = [a["key"] for a in o.get("alerts", [])]
    _ok("sin alerta de cruce", "cross_amber" not in keys and "cross_red" not in keys, keys)


def test_crossover_drying_alerts():
    print("test_crossover_drying_alerts (pista secándose -> amber/red + voz):")
    e = S.StrategyEngine()
    o = feed_wet(e, rains=[0.30, 0.25, 0.18, 0.12, 0.09, 0.07],
                 tracks=[22.0, 22.4, 22.8, 23.2, 23.6, 24.0],
                 wet_temps=[60, 64, 68, 72, 75, 77],
                 lap_times=[120.0, 120.0, 120.3, 120.5, 121.0, 121.2])
    cx = o.get("crossover") or {}
    _ok("estado amber/red al secar", cx.get("state") in ("amber", "red"), cx.get("state"))
    _ok("detecta wets recalentando",
        "gomas de lluvia recalentando" in (cx.get("signals") or []), cx.get("signals"))
    keys = [a["key"] for a in o.get("alerts", [])]
    _ok("dispara alerta de voz", "cross_red" in keys or "cross_amber" in keys, keys)


def test_crossover_surf_dead():
    print("test_crossover_surf_dead (bulk muerto -> ni señal C ni temperatura inventada):")
    # MISMO secado que test_crossover_drying_alerts, pero con el modelo de banda muerto:
    # AMS2 deja mTyreTemp en ~33 C con la carcasa en 110-130. Sin el gate, esos 33 se
    # leian como "wets frias" y ademas se pintaban en el dash como dato real.
    e = S.StrategyEngine()
    o = feed_wet(e, rains=[0.30, 0.25, 0.18, 0.12, 0.09, 0.07],
                 tracks=[22.0, 22.4, 22.8, 23.2, 23.6, 24.0],
                 wet_temps=[33, 34, 32, 35, 33, 34],
                 lap_times=[120.0, 120.0, 120.3, 120.5, 121.0, 121.2],
                 surf_alive=False)
    cx = o.get("crossover") or {}
    _ok("marca el canal muerto", cx.get("wet_dead") is True, cx.get("wet_dead"))
    _ok("NO emite temperatura inventada", cx.get("wet_temp") is None, cx.get("wet_temp"))
    _ok("sin señal de wets recalentando",
        "gomas de lluvia recalentando" not in (cx.get("signals") or []), cx.get("signals"))
    _ok("el semáforo sigue vivo con las otras señales",
        cx.get("state") in ("green", "amber", "red"), cx.get("state"))
    print(f"    -> estado={cx.get('state')} señales={cx.get('signals')}")

    # Contraparte: el MISMO secado con el canal vivo si tiene que ver las wets.
    e2 = S.StrategyEngine()
    o2 = feed_wet(e2, rains=[0.30, 0.25, 0.18, 0.12, 0.09, 0.07],
                  tracks=[22.0, 22.4, 22.8, 23.2, 23.6, 24.0],
                  wet_temps=[60, 64, 68, 72, 75, 77],
                  lap_times=[120.0, 120.0, 120.3, 120.5, 121.0, 121.2])
    cx2 = o2.get("crossover") or {}
    _ok("con el canal vivo SÍ ve las wets",
        "gomas de lluvia recalentando" in (cx2.get("signals") or []), cx2.get("signals"))
    _ok("y emite la temperatura", cx2.get("wet_temp") is not None, cx2.get("wet_temp"))


def test_crossover_warmup_not_drying():
    print("test_crossover_warmup_not_drying (wets calentando con lluvia firme != secado):")
    # Firma medida en Buenos Aires 21/06 vueltas 2-7: la lluvia CLAVADA en 0.200 y la
    # wet subiendo de 67 a 77 porque recien entra en temperatura. Antes esto encendia
    # amber en plena lluvia; el crossover no puede confundir warm-up con pista seca.
    e = S.StrategyEngine()
    o = feed_wet(e, rains=[0.200] * 6, tracks=[24.9, 24.9, 25.0, 25.0, 25.1, 25.1],
                 wet_temps=[67, 70, 73, 73, 77, 77],
                 lap_times=[132.6, 132.5, 132.8, 132.0, 132.4, 132.3])
    cx = o.get("crossover") or {}
    _ok("green con la lluvia firme pese a wets calientes", cx.get("state") == "green",
        cx.get("state"))
    _ok("no acusa wets recalentando",
        "gomas de lluvia recalentando" not in (cx.get("signals") or []), cx.get("signals"))
    keys = [a["key"] for a in o.get("alerts", [])]
    _ok("sin alerta de cruce", "cross_amber" not in keys and "cross_red" not in keys, keys)


def test_crossover_calibrating():
    print("test_crossover_calibrating (<3 vueltas de historia):")
    e = S.StrategyEngine()
    o = feed_wet(e, rains=[0.3, 0.25, 0.2], tracks=[22, 22.3, 22.6], wet_temps=[60, 64, 68])
    cx = o.get("crossover") or {}
    _ok("calibrando con poca historia", cx.get("state") == "calibrando", cx.get("state"))


def test_player_index():
    print("test_player_index (anclaje al JUGADOR, no a la camara — bug MP):")
    import ams2_shm as SHM

    class _P:
        def __init__(self, name):
            self.mName = name.encode("utf-8")
            self.mIsActive = True

    class _D:
        def __init__(self, names, viewed):
            self._ps = [_P(n) for n in names]
            self.mNumParticipants = len(names)
            self.mViewedParticipantIndex = viewed

        @property
        def mParticipantInfo(self):
            return self._ps

    # Nombres genericos a proposito: el repo es publico y estos eran pilotos reales.
    # El test verifica el MATCHEO, no quienes son.
    d = _D(["Piloto Uno", "Piloto Dos", "YoMismo", "piloto_cuatro"], viewed=1)
    _ok("matchea por nombre (ignora el visto=1)", SHM.player_index(d, "yomismo") == 2, SHM.player_index(d, "yomismo"))
    _ok("sin nombre -> cae al visto", SHM.player_index(d, "") == 1, SHM.player_index(d, ""))
    _ok("nombre inexistente -> cae al visto", SHM.player_index(d, "zzz") == 1, SHM.player_index(d, "zzz"))
    d2 = _D(["A", "B"], viewed=9)
    _ok("visto fuera de rango -> 0", SHM.player_index(d2, "") == 0, SHM.player_index(d2, ""))


def test_speech_server():
    print("test_speech_server (voz edge-trigger, sin hablar de verdad):")
    import bridge_shm
    ss = bridge_shm.SpeechServer(enabled=True)
    spoken = []
    ss._speak = lambda t, lvl=0: spoken.append(t)
    a2 = [{"key": "window", "level": 2, "say": "ventana"},
          {"key": "enforced", "level": 3, "say": "pit obligatorio"}]
    ss.handle({"alerts": a2})
    _ok("habla la mas urgente primero", spoken == ["pit obligatorio"], spoken)
    ss.handle({"alerts": a2})
    _ok("no repite la dicha; dice la nueva", spoken == ["pit obligatorio", "ventana"], spoken)
    ss.handle({"alerts": [{"key": "window", "level": 2, "say": "ventana"}]})   # desaparece enforced
    ss.handle({"alerts": [{"key": "enforced", "level": 3, "say": "pit obligatorio"}]})  # reaparece
    _ok("re-suena tras desaparecer/reaparecer", spoken[-1] == "pit obligatorio", spoken)
    ss2 = bridge_shm.SpeechServer(enabled=False)
    said = []
    ss2._speak = lambda t, lvl=0: said.append(t)
    ss2.handle({"alerts": [{"key": "x", "level": 3, "say": "no"}]})
    _ok("disabled no habla", said == [], said)


if __name__ == "__main__":
    for t in (test_lap_race, test_timed_race_seconds, test_timed_race_millis,
              test_fuel_deficit, test_capacity_guard, test_tyre_wear,
              test_eol_por_rueda, test_eol_sin_datos,
              test_planning_practice, test_live_overrides_plan,
              test_all_laps_toggle, test_no_false_fumes, test_calibrating,
              test_crossover_dry_none, test_crossover_green_raining,
              test_crossover_drying_alerts, test_crossover_surf_dead,
              test_crossover_warmup_not_drying, test_crossover_calibrating,
              test_player_index, test_speech_server):
        t()
        print()
    print("done.")
