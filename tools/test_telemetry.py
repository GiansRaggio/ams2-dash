#!/usr/bin/env python3
"""Tests del TelemetryLogger con snapshots sinteticos (sin AMS2 en vivo).

Verifica: se guarda una vuelta valida (traza csv.gz + linea de resumen), se
descartan out-lap, vuelta invalida y grabacion deshabilitada. Escribe a un dir
temporal que se limpia al final. Correr: python tools/test_telemetry.py
"""
import gzip
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ams2_telemetry as T


class P:
    def __init__(self, laps_completed=0, dist=0.0):
        self.mLapsCompleted = laps_completed
        self.mCurrentLap = laps_completed + 1
        self.mCurrentLapDistance = dist
        self.mWorldPosition = [10.0, 1.0, 20.0]
        self.mRacePosition = 1
        self.mName = b"Player"        # no matchea player.txt -> player_index cae al visto (índice 0)
        self.mIsActive = True


class Snap:
    def __init__(self, laps_completed=0, invalid=False, pit=0, dist=0.0, compound=b"Soft"):
        # --- contexto de carrera. Valores de "solo en pista, sin novedad": sin nadie
        # cerca, sin contacto, sin dano, bandera verde. Los tests que ejercitan
        # racecraft los sobreescriben.
        self.mSplitTimeAhead = -1.0            # -1 = no hay nadie adelante
        self.mSplitTimeBehind = -1.0
        self.mLastOpponentCollisionIndex = -1  # -1 = ningun contacto todavia
        self.mLastOpponentCollisionMagnitude = 0.0
        self.mCrashState = 0
        self.mAeroDamage = 0.0
        self.mEngineDamage = 0.0
        self.mHighestFlagColour = 1            # verde
        self.mYellowFlagState = 0
        self.mLaunchStage = 0
        self.mClutchSlipping = False
        self.mVersion = 14
        self.mNumParticipants = 5
        self.mViewedParticipantIndex = 0
        self.mGameState = 2          # PLAYING
        self.mSessionState = 1       # PRACTICE
        self.mTrackLocation = b"Nordschleife"
        self.mTrackVariation = b"24h"
        self.mCarName = b"Porsche 992 GT3 R"
        self.mCarClassName = b"GT3"
        self.mTrackLength = 20832.0
        self.mTyreCompound = [compound, compound, compound, compound]
        self.mPitMode = pit
        self.mLapInvalidated = invalid
        self.mLastLapTime = 472.5
        self.mCurrentTime = 120.0
        self.mFuelCapacity = 90.0
        self.mFuelLevel = 0.5
        # canales escalares
        self.mSpeed = 55.0
        self.mRpm = 7200.0
        self.mGear = 4
        self.mUnfilteredThrottle = 0.8
        self.mUnfilteredBrake = 0.0
        self.mUnfilteredClutch = 0.0
        self.mUnfilteredSteering = 0.1
        self.mSteering = 0.1
        self.mBrakeBias = 0.56
        self.mLocalAcceleration = [2.0, 9.8, 1.0]
        self.mOrientation = [0.5, 0.01, 0.02]
        self.mLocalVelocity = [1.0, 0.0, 55.0]
        self.mAngularVelocity = [0.0, 0.2, 0.01]
        self.mMaxRPM = 7800.0
        self.mEngineTorque = 320.0
        self.mAntiLockActive = False
        self.mTractionControlSetting = 3
        self.mAntiLockSetting = 2
        self.mDrsState = 0
        self.mWaterTempCelsius = 90.0
        self.mOilTempCelsius = 105.0
        self.mAmbientTemperature = 22.0
        self.mTrackTemperature = 31.0
        self.mRainDensity = 0.0
        self.mCurrentSector1Time = 100.0
        self.mCurrentSector2Time = 180.0
        self.mCurrentSector3Time = 192.5
        # canales por esquina
        q = lambda v: [v, v, v, v]
        self.mTyreTemp = q(88.0)
        self.mTyreTempLeft = q(86.0)
        self.mTyreTempCenter = q(89.0)
        self.mTyreTempRight = q(90.0)
        self.mBrakeTempCelsius = q(350.0)
        self.mTyreWear = q(0.05)
        self.mSuspensionTravel = q(0.03)
        self.mSuspensionVelocity = q(0.1)
        self.mTyreSlipSpeed = q(0.5)
        self.mRideHeight = q(0.06)
        self.mAirPressure = q(165.0)
        self.mTyreRPS = q(120.0)
        self.mTerrain = [0, 0, 0, 0]
        self.mTyreCarcassTemp = q(360.0)    # ~87 C en Kelvin
        self.mTyreGrip = q(0.42)            # margen de agarre sin usar (0..1)
        self.mTyreLayerTemp = q(351.0)      # ~78 C en Kelvin (piel, mas fria que la carcasa)
        # arrays por participante (los usa _track_rivals para el registro de rivales)
        self.mFastestLapTimes = [95.5] * 64
        self.mFastestSector1Times = [30.1] * 64
        self.mFastestSector2Times = [35.2] * 64
        self.mFastestSector3Times = [30.2] * 64
        self.mLastLapTimes = [96.0] * 64
        self.mLapsInvalidated = [False] * 64
        self._p = P(laps_completed, dist)

    @property
    def mParticipantInfo(self):
        return [self._p] * 64


def _ok(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")
    return cond


def feed_lap(log, lap_completed, n=260, invalid=False, pit=0, fuel0=0.5, wear0=0.05,
             compound=b"Soft"):
    """Alimenta n muestras de una vuelta (mLapsCompleted=lap_completed)."""
    for k in range(n):
        s = Snap(laps_completed=lap_completed, invalid=invalid, pit=pit,
                 dist=k / n * 20000.0, compound=compound)
        s.mFuelLevel = fuel0 - k / n * 0.03      # va consumiendo
        s.mTyreWear = [wear0 + k / n * 0.02] * 4
        log._ingest(s)


def cross_to(log, lap_completed, fuel=0.5, wear=0.05, pit=0, compound=b"Soft"):
    """Una muestra con el nuevo contador de vueltas -> dispara commit + begin.

    El combustible/desgaste se leen EN EL CRUCE DE META (begin/commit usan estos
    snapshots), asi que para simular consumo hay que bajar fuel entre cruces.
    """
    s = Snap(laps_completed=lap_completed, dist=0.0, pit=pit, compound=compound)
    s.mFuelLevel = fuel
    s.mTyreWear = [wear] * 4
    log._ingest(s)


def session_dir(base):
    subs = [os.path.join(base, d) for d in os.listdir(base)
            if os.path.isdir(os.path.join(base, d))]
    return subs[0] if subs else None


def main():
    base = tempfile.mkdtemp(prefix="ams2tel_")
    try:
        log = T.TelemetryLogger(base_dir=base)

        print("test_valid_lap (lap1 limpia -> traza + resumen):")
        feed_lap(log, 0)                 # lap0 = out-lap inicial (se descarta)
        cross_to(log, 1, fuel=0.50, wear=0.05)   # begin lap1 (45 L, wear 0.05)
        feed_lap(log, 1)                 # lap1 limpia
        cross_to(log, 2, fuel=0.45, wear=0.10)   # commit lap1: uso 4.5 L, deg 0.05
        sd = session_dir(base)
        _ok("carpeta de sesion creada", sd is not None)
        traces = [f for f in os.listdir(sd) if f.endswith(".csv.gz")] if sd else []
        _ok("1 traza .csv.gz", len(traces) == 1, traces)
        sumf = os.path.join(sd, "summary.jsonl")
        lines = open(sumf, encoding="utf-8").read().strip().splitlines() if os.path.exists(sumf) else []
        _ok("1 linea de resumen", len(lines) == 1, len(lines))
        if lines:
            rec = json.loads(lines[0])
            _ok("resumen valido=True", rec["valid"] is True)
            _ok("fuel_used > 0", rec["fuel_used"] > 0, rec["fuel_used"])
            _ok("wear_delta presente", rec["wear_delta"] is not None, rec["wear_delta"])
            _ok("tyre_temp_avg presente", rec["tyre_temp_avg"] is not None)
        if traces:
            with gzip.open(os.path.join(sd, traces[0]), "rt", encoding="utf-8") as f:
                head = f.readline().strip().split(",")
                rows = f.read().strip().splitlines()
            _ok("header coincide con HEADER", head == T.HEADER, len(head))
            _ok("filas ~260", abs(len(rows) - 260) <= 2, len(rows))
            _ok("cada fila tiene todas las columnas",
                all(len(r.split(",")) == len(T.HEADER) for r in rows[:5]))
        secf = os.path.join(sd, "sectors.jsonl") if sd else ""
        slines = open(secf, encoding="utf-8").read().strip().splitlines() if os.path.exists(secf) else []
        _ok("sectors.jsonl: 1 registro (limpia)", len(slines) == 1, len(slines))
        if slines:
            srec = json.loads(slines[0])
            _ok("sectores: S1/S2 vivos + S3=total-S1-S2", srec["sectors"] == [100.0, 180.0, 192.5], srec["sectors"])
            _ok("limpia: sec_valid todos True", srec["sec_valid"] == [True, True, True], srec["sec_valid"])
            _ok("limpia: invalid=False", srec["invalid"] is False)

        # La invalidada guarda TRAZA (prefijo X, para que el visor pueda comparar la
        # tanda completa) pero NO resumen: el corpus de las herramientas de analisis
        # llega por summary.jsonl y tiene que seguir siendo solo vueltas limpias.
        print("test_invalid_lap (lap invalida -> traza X, pero fuera del resumen):")
        log2 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel2_"))
        feed_lap(log2, 0); cross_to(log2, 1)
        feed_lap(log2, 1, invalid=True)          # se invalida
        cross_to(log2, 2)
        sd2 = session_dir(log2._base)
        tr2 = [f for f in os.listdir(sd2) if f.endswith(".csv.gz")] if sd2 else []
        _ok("invalida: guarda traza con prefijo X", len(tr2) == 1 and tr2[0][0] == "X", tr2)
        _ok("invalida: NINGUNA traza con prefijo L (no se cuela al corpus)",
            not any(f[0] == "L" for f in tr2), tr2)
        sumf2 = os.path.join(sd2, "summary.jsonl") if sd2 else ""
        _ok("invalida: NO entra a summary.jsonl", not os.path.exists(sumf2)
            or not open(sumf2, encoding="utf-8").read().strip())
        tlf2 = os.path.join(sd2, "timeline.jsonl") if sd2 else ""
        tl2 = [json.loads(x) for x in open(tlf2, encoding="utf-8").read().splitlines()] if os.path.exists(tlf2) else []
        inv = [r for r in tl2 if r.get("type") == "lap" and r.get("kind") == "invalid"]
        _ok("invalida: la linea de tiempo apunta a su traza", bool(inv) and inv[0].get("trace") in tr2,
            inv[0].get("trace") if inv else None)
        secf2 = os.path.join(sd2, "sectors.jsonl") if sd2 else ""
        sl2 = open(secf2, encoding="utf-8").read().strip().splitlines() if os.path.exists(secf2) else []
        _ok("invalida: sectors.jsonl SI guarda el registro (rescate)", len(sl2) == 1, len(sl2))
        if sl2:
            r2 = json.loads(sl2[0])
            _ok("invalida: invalid=True", r2["invalid"] is True)
            _ok("invalida: sec_valid marca >=1 sector sucio", r2["sec_valid"].count(False) >= 1, r2["sec_valid"])
        shutil.rmtree(log2._base, ignore_errors=True)

        # mLapInvalidated NO es un pulso: AMS2 lo levanta al pisar el limite y lo
        # deja alto hasta meta. Atribuir el sector en cada frame con el flag alto
        # terminaba culpando SIEMPRE al ultimo sector poblado -> en el corpus real
        # 134 de 134 vueltas sucias marcaban el S3. El test viejo no lo pillaba
        # porque el mock trae los sector times ya poblados desde el frame 0.
        print("test_sector_invalido_en_el_flanco (corte en S1 -> se culpa a S1, no a S3):")
        log7 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel7_"))
        feed_lap(log7, 0)
        cruce = Snap(laps_completed=1, dist=0.0)   # AMS2 resetea los sectores al cruzar
        cruce.mCurrentSector1Time = 0.0
        cruce.mCurrentSector2Time = 0.0
        log7._ingest(cruce)
        for k in range(260):
            s = Snap(laps_completed=1, dist=k / 260 * 20000.0)
            s.mLapInvalidated = k >= 10          # se ensucia temprano y NO se baja
            if k < 60:                            # todavia en el S1: nada poblado
                s.mCurrentSector1Time = 0.0
                s.mCurrentSector2Time = 0.0
            elif k < 150:                         # ya cerro el S1, corriendo el S2
                s.mCurrentSector2Time = 0.0
            log7._ingest(s)
        cross_to(log7, 2)
        sd7 = session_dir(log7._base)
        sl7 = open(os.path.join(sd7, "sectors.jsonl"), encoding="utf-8").read().strip().splitlines() if sd7 else []
        r7 = json.loads(sl7[0]) if sl7 else {}
        _ok("corte en S1: se culpa al S1", r7.get("sec_valid") == [False, True, True], r7.get("sec_valid"))
        shutil.rmtree(log7._base, ignore_errors=True)

        # Los canales de carrera existen porque el grabador era ciego a todo lo que
        # pasaba con los otros autos, y eso dejaba el 20% de racecraft de la escuela
        # sin una sola medicion objetiva.
        print("test_canales_de_carrera (contacto, gaps, banderas quedan en la traza):")
        log8 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel8_"))
        feed_lap(log8, 0); cross_to(log8, 1)
        for k in range(260):
            s8 = Snap(laps_completed=1, dist=k / 260 * 20000.0)
            s8.mSplitTimeAhead = 0.85          # alguien justo adelante: estela
            s8.mSplitTimeBehind = 2.40
            if k >= 100:                        # a mitad de vuelta, un toque
                s8.mLastOpponentCollisionIndex = 3
                s8.mLastOpponentCollisionMagnitude = 12.5
                s8.mAeroDamage = 0.08
            if 150 <= k < 200:
                s8.mHighestFlagColour = 3       # bandera
                s8.mYellowFlagState = 1
            log8._ingest(s8)
        cross_to(log8, 2)
        sd8 = session_dir(log8._base)
        tr8 = [f for f in os.listdir(sd8) if f.endswith(".csv.gz")] if sd8 else []
        if tr8:
            with gzip.open(os.path.join(sd8, tr8[0]), "rt", encoding="utf-8") as f:
                head8 = f.readline().strip().split(",")
                filas8 = [r.split(",") for r in f.read().strip().splitlines()]
            col = {n: i for i, n in enumerate(head8)}
            _ok("la traza trae los canales de carrera",
                all(c in col for c in ("split_ahead", "coll_mag", "coll_idx", "flag",
                                       "yellow", "aero_dmg", "race_pos")), sorted(col)[:3])
            mags = [float(r[col["coll_mag"]]) for r in filas8]
            _ok("el contacto queda grabado con su magnitud", max(mags) == 12.5, max(mags))
            _ok("antes del toque la magnitud es 0", mags[0] == 0.0, mags[0])
            # La trampa: AMS2 deja el ULTIMO choque publicado, no lo baja. Contar frames
            # con magnitud > 0 NO cuenta choques -- hay que detectar el cambio de valor.
            frames_con_mag = sum(1 for m in mags if m > 0)
            saltos = sum(1 for i in range(1, len(mags)) if mags[i] != mags[i - 1])
            _ok("coll_mag es estado sostenido, no pulso (por eso se cuentan CAMBIOS)",
                frames_con_mag > 100 and saltos == 1, (frames_con_mag, saltos))
            # La PRIMERA fila es la muestra del cruce de meta, que trae los valores
            # por defecto: la vuelta nueva arranca en ese frame. Por eso se mira el
            # cuerpo de la vuelta, no la fila 0.
            gaps = [float(r[col["split_ahead"]]) for r in filas8[1:]]
            _ok("el gap al de adelante queda grabado", gaps and all(g == 0.85 for g in gaps),
                sorted(set(gaps))[:3])
            flags = [int(r[col["flag"]]) for r in filas8]
            _ok("la bandera queda grabada mientras ondea", 3 in flags and 1 in flags,
                sorted(set(flags)))
        shutil.rmtree(log8._base, ignore_errors=True)

        # El buffer de la vuelta SOLO se vacia en _begin_lap, que corre al cambiar el
        # contador de vueltas. En el lobby/garage con el reloj corriendo el contador
        # NO avanza, asi que si se acumulara ahi la lista creceria a 50 filas/s para
        # siempre: 104 MB/hora de strings. El GC de Python recorre contenedores, se
        # lleva el GIL en cada pasada y hunde los FPS del juego -- medido en pista,
        # de 160 a 60 fps tras varias carreras seguidas.
        print("test_buffer_en_menu (con el reloj corriendo en menu NO se acumula):")
        log6 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel6_"))
        for k in range(300):                      # 6 s a 50 Hz en el lobby
            s = Snap(laps_completed=3, dist=k * 2.0)
            s.mGameState = 4                      # INGAME_INMENU_TIME_TICKING
            log6._ingest(s)
        _ok("en menu con reloj: el buffer NO crece", len(log6._buf) == 0, len(log6._buf))
        for k in range(120):                      # y manejando SI graba
            log6._ingest(Snap(laps_completed=3, dist=1000 + k * 2.0))
        _ok("manejando: el buffer si acumula", len(log6._buf) == 120, len(log6._buf))
        _ok("techo duro definido por si el contador nunca avanza",
            T.MAX_LAP_SAMPLES >= 20000, T.MAX_LAP_SAMPLES)
        shutil.rmtree(log6._base, ignore_errors=True)

        print("test_disabled (modo off -> NO se guarda):")
        log3 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel3_"))
        log3.set_mode("off")
        feed_lap(log3, 0); cross_to(log3, 1)
        feed_lap(log3, 1); cross_to(log3, 2)
        sd3 = session_dir(log3._base)
        tr3 = ([f for f in os.listdir(sd3) if f.endswith(".csv.gz")]
               if sd3 and os.path.isdir(sd3) else [])
        _ok("off no guarda", len(tr3) == 0, tr3)
        # regresion critica: en OFF no debe crearse timeline.jsonl
        tlf3 = os.path.join(sd3, "timeline.jsonl") if sd3 else ""
        _ok("off: NO crea timeline.jsonl", not os.path.exists(tlf3), tlf3)
        st = log3.status()
        _ok("status refleja mode=off", st["mode"] == "off" and st["enabled"] is False)
        shutil.rmtree(log3._base, ignore_errors=True)

        print("test_summary (modo resumen -> linea de resumen SIN traza):")
        log4 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel4_"))
        log4.set_mode("summary")
        feed_lap(log4, 0); cross_to(log4, 1, fuel=0.50, wear=0.05)
        feed_lap(log4, 1); cross_to(log4, 2, fuel=0.45, wear=0.10)
        sd4 = session_dir(log4._base)
        tr4 = [f for f in os.listdir(sd4) if f.endswith(".csv.gz")] if sd4 else []
        sumf4 = os.path.join(sd4, "summary.jsonl") if sd4 else ""
        lines4 = (open(sumf4, encoding="utf-8").read().strip().splitlines()
                  if os.path.exists(sumf4) else [])
        _ok("resumen: 1 linea", len(lines4) == 1, len(lines4))
        _ok("resumen: SIN traza .csv.gz", len(tr4) == 0, tr4)
        if lines4:
            rec4 = json.loads(lines4[0])
            _ok("resumen: trace=null", rec4["trace"] is None)
            _ok("resumen: fuel_used > 0", rec4["fuel_used"] > 0, rec4["fuel_used"])
        shutil.rmtree(log4._base, ignore_errors=True)

        test_timeline()
        test_compound_empty_start()
        test_report_timeline_reader()

        print("done.")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_timeline():
    """La linea de tiempo (timeline.jsonl) captura TODA vuelta cruzada (no solo las limpias) +
    eventos de largada / pit / cambio de goma. Simula: largada en Lisos, unas vueltas, entrada a
    pit con cambio a Lluvia, y salida."""
    print("test_timeline (timeline.jsonl: toda vuelta + eventos largada/pit/goma):")
    base = tempfile.mkdtemp(prefix="ams2tel5_")
    try:
        log = T.TelemetryLogger(base_dir=base)
        LISOS, LLUVIA = b"Lisos", b"Lluvia"

        # largada en Lisos: lap1 = out-lap, lap2 = limpia
        feed_lap(log, 0, compound=LISOS)                 # emite evento start (Lisos)
        cross_to(log, 1, fuel=0.50, wear=0.05, compound=LISOS)   # commit lap1 (out)
        feed_lap(log, 1, compound=LISOS)                 # lap2 limpia
        cross_to(log, 2, fuel=0.47, wear=0.08, compound=LISOS)   # commit lap2 (flying)
        # vuelta de pit: entra a boxes (pit_in) y cambia de goma a Lluvia (tyre_change)
        feed_lap(log, 2, n=120, pit=1, compound=LISOS)   # pit_in en V2 (corta, en boxes)
        feed_lap(log, 2, n=120, pit=1, compound=LLUVIA)  # tyre_change Lisos -> Lluvia (parado)
        cross_to(log, 3, fuel=0.44, wear=0.02, pit=0, compound=LLUVIA)  # pit_out + commit vuelta de pit
        feed_lap(log, 3, compound=LLUVIA)                # sigue con Lluvia
        cross_to(log, 4, fuel=0.41, wear=0.05, compound=LLUVIA)

        sd = session_dir(base)
        tlf = os.path.join(sd, "timeline.jsonl") if sd else ""
        recs = ([json.loads(x) for x in open(tlf, encoding="utf-8").read().strip().splitlines()]
                if os.path.exists(tlf) else [])
        _ok("timeline.jsonl existe", len(recs) > 0, len(recs))

        laps = [r for r in recs if r.get("type") == "lap"]
        events = [r for r in recs if r.get("type") == "event"]
        lap_nums = sorted(r["lap"] for r in laps)
        # cada vuelta cruzada deja un registro lap: se cruzaron 4 metas (a laps 1,2,3,4)
        _ok(">=1 registro lap por CADA vuelta cruzada (4)", len(laps) == 4, lap_nums)

        # la vuelta de pit (la que se cruza al salir de boxes) es kind pit u out
        pit_kinds = [r["kind"] for r in laps if r["kind"] in ("pit", "out")]
        _ok("hay vuelta(s) marcada pit/out (no solo flying)", len(pit_kinds) >= 1, [r["kind"] for r in laps])
        # la vuelta que contuvo la parada (cruzada al salir, lap 3) es kind 'pit'
        pit_lap = next((r for r in laps if r["lap"] == 3), None)
        _ok("la vuelta de la parada es kind 'pit'", pit_lap is not None and pit_lap["kind"] == "pit",
            pit_lap["kind"] if pit_lap else None)
        # la vuelta cruzada DESPUES de la parada (lap 4) es la out-lap -> kind 'out'
        out_lap = next((r for r in laps if r["lap"] == 4), None)
        _ok("la vuelta post-pit es kind 'out'", out_lap is not None and out_lap["kind"] == "out",
            out_lap["kind"] if out_lap else None)

        start = [r for r in events if r["event"] == "start"]
        _ok("evento start presente", len(start) == 1, len(start))
        _ok("start: compuesto de largada = Lisos",
            bool(start) and start[0].get("compound") == ["Lisos"] * 4,
            start[0].get("compound") if start else None)

        tc = [r for r in events if r["event"] == "tyre_change"]
        _ok("evento tyre_change presente", len(tc) == 1, len(tc))
        _ok("tyre_change: from Lisos -> to Lluvia",
            bool(tc) and tc[0].get("from") == ["Lisos"] * 4 and tc[0].get("to") == ["Lluvia"] * 4,
            (tc[0].get("from"), tc[0].get("to")) if tc else None)
        # el cambio de goma se hizo PARADO en boxes en la misma vuelta (V2), sin cruzar meta:
        # debe emitirse UNA sola vez (ni 0 ni 2) y quedar atribuido a V2, no a la vuelta siguiente
        _ok("tyre_change parado sin cruzar meta: exactamente 1", len(tc) == 1, len(tc))
        _ok("tyre_change atribuido a V2 (parado en box)", bool(tc) and tc[0].get("lap") == 2,
            tc[0].get("lap") if tc else None)

        _ok("evento pit_in presente", any(r["event"] == "pit_in" for r in events),
            [r["event"] for r in events])
        _ok("evento pit_out presente", any(r["event"] == "pit_out" for r in events),
            [r["event"] for r in events])

        # zero-regression: las vueltas LIMPIAS (summary/traza) no cambian -> solo lap2 es flying+valida
        sumf = os.path.join(sd, "summary.jsonl") if sd else ""
        sumlines = (open(sumf, encoding="utf-8").read().strip().splitlines()
                    if os.path.exists(sumf) else [])
        _ok("summary sigue con solo las limpias (1: la lap2 flying)", len(sumlines) == 1, len(sumlines))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_compound_empty_start():
    """AMS2 puebla los strings de goma con lag: los primeros frames tras cargar la sesion
    suelen tener mTyreCompound vacio. 'start' NO debe capturar esa basura ('x'): tiene que
    esperar al primer compuesto REAL y NO debe dispararse un tyre_change fantasma cuando el
    compuesto real puebla. Ademas un torn frame (goma vacia a mitad de carrera) tampoco genera
    un cambio de goma inexistente."""
    print("test_compound_empty_start (largada con goma vacia -> sin basura ni cambio fantasma):")
    base = tempfile.mkdtemp(prefix="ams2tel6_")
    try:
        log = T.TelemetryLogger(base_dir=base)
        EMPTY, SOFT = b"", b"Soft"
        feed_lap(log, 0, n=30, compound=EMPTY)     # goma sin poblar: NO debe emitir start aun
        feed_lap(log, 0, n=230, compound=SOFT)     # llega la goma real -> start = Soft
        cross_to(log, 1, fuel=0.50, wear=0.05, compound=SOFT)
        feed_lap(log, 1, compound=SOFT)
        cross_to(log, 2, fuel=0.47, wear=0.08, compound=SOFT)
        sd = session_dir(base)
        tlf = os.path.join(sd, "timeline.jsonl") if sd else ""
        recs = ([json.loads(x) for x in open(tlf, encoding="utf-8").read().strip().splitlines()]
                if os.path.exists(tlf) else [])
        events = [r for r in recs if r.get("type") == "event"]
        starts = [r for r in events if r["event"] == "start"]
        tcs = [r for r in events if r["event"] == "tyre_change"]
        _ok("start unico pese a la goma vacia inicial", len(starts) == 1, len(starts))
        _ok("start NO captura basura ('x'): compuesto = Soft real",
            bool(starts) and starts[0].get("compound") == ["Soft"] * 4,
            starts[0].get("compound") if starts else None)
        _ok("sin tyre_change fantasma al poblar la goma", len(tcs) == 0, len(tcs))

        # torn frame a mitad de carrera (Soft -> vacio -> Soft): 0 cambios de goma
        log2 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel6b_"))
        feed_lap(log2, 0, n=260, compound=SOFT)
        cross_to(log2, 1, fuel=0.50, wear=0.05, compound=SOFT)
        feed_lap(log2, 1, n=100, compound=SOFT)
        log2._ingest(Snap(laps_completed=1, dist=8000.0, compound=EMPTY))   # torn frame
        feed_lap(log2, 1, n=160, compound=SOFT)
        cross_to(log2, 2, fuel=0.47, wear=0.08, compound=SOFT)
        sd2 = session_dir(log2._base)
        tlf2 = os.path.join(sd2, "timeline.jsonl") if sd2 else ""
        recs2 = ([json.loads(x) for x in open(tlf2, encoding="utf-8").read().strip().splitlines()]
                 if os.path.exists(tlf2) else [])
        tcs2 = [r for r in recs2 if r.get("type") == "event" and r["event"] == "tyre_change"]
        _ok("torn frame (goma vacia) no genera cambio de goma", len(tcs2) == 0, len(tcs2))
        shutil.rmtree(log2._base, ignore_errors=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_report_timeline_reader():
    """El lado LECTOR (analyze_telemetry.report_timeline / _load_timeline) debe: (1) no crashear
    cuando falta timeline.jsonl; (2) sobrevivir una ULTIMA linea truncada (taskkill a mitad del
    f.write) sin reventar con JSONDecodeError, saltandola y leyendo el resto."""
    print("test_report_timeline_reader (lector: timeline ausente + linea corrupta por taskkill):")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import analyze_telemetry as A
    base = tempfile.mkdtemp(prefix="ams2tel7_")
    try:
        # (1) carpeta sin timeline.jsonl -> _load_timeline devuelve [] y report_timeline no crashea
        _ok("timeline ausente: _load_timeline devuelve []", A._load_timeline(base) == [])
        try:
            A.report_timeline(base)
            _ok("timeline ausente: report_timeline no crashea", True)
        except Exception as e:
            _ok("timeline ausente: report_timeline no crashea", False, repr(e))

        # (2) timeline.jsonl con 2 registros validos + una ULTIMA linea truncada (JSON partido)
        good1 = json.dumps({"type": "event", "event": "start", "lap": 0, "compound": ["Soft"] * 4})
        good2 = json.dumps({"type": "lap", "lap": 1, "kind": "flying", "lap_time": 92.5,
                            "compound": ["Soft"] * 4, "rain": 0.0})
        tlf = os.path.join(base, "timeline.jsonl")
        with open(tlf, "w", encoding="utf-8") as f:
            f.write(good1 + "\n")
            f.write(good2 + "\n")
            f.write('{"type": "lap", "lap": 2, "kind": "fly')   # taskkill a mitad del write
        try:
            recs = A._load_timeline(base)
            ok = len(recs) == 2 and recs[0]["event"] == "start" and recs[1]["lap"] == 1
            _ok("linea corrupta final: se salta, se leen los 2 registros validos", ok, len(recs))
        except Exception as e:
            _ok("linea corrupta final: se salta (no JSONDecodeError)", False, repr(e))
        try:
            A.report_timeline(base)
            _ok("linea corrupta: report_timeline no crashea", True)
        except Exception as e:
            _ok("linea corrupta: report_timeline no crashea", False, repr(e))
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
