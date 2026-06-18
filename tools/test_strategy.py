#!/usr/bin/env python3
"""Tests del StrategyEngine con snapshots sinteticos (sin AMS2 en vivo).

Valida la matematica verificada por el workflow: laps_to_go (timed/laps), fuel
projection, semaforo, fuel-to-save, stops, deteccion de unidades ms/min, guard
de capacidad. Correr: python tools/test_strategy.py
"""
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
        self.mTyreCompound = [b"Medium", b"Medium", b"Medium", b"Medium"]
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


if __name__ == "__main__":
    for t in (test_lap_race, test_timed_race_seconds, test_timed_race_millis,
              test_fuel_deficit, test_capacity_guard, test_tyre_wear,
              test_planning_practice, test_live_overrides_plan,
              test_all_laps_toggle, test_no_false_fumes, test_calibrating):
        t()
        print()
    print("done.")
