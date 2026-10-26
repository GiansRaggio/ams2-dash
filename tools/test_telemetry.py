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
        # Los TRADUCIDOS son otra cosa que los crudos, y a proposito distintos en el
        # mock: es justamente la confusion que dejaba nuestras exportaciones sin
        # pareja en la app del otro piloto.
        self.mTranslatedTrackLocation = b"Nordschleife"
        self.mTranslatedTrackVariation = b"24h Layout"
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


def feed_tramo(log, lap_completed, f0, f1, n=120, pit=0, invalid=False, estado=2):
    """n muestras SIN mover el contador, con la distancia yendo de f0 a f1 (fracciones
    del largo de la pista del mock). Para armar garage, pit lane y out-lap."""
    largo = Snap().mTrackLength
    for k in range(n):
        s = Snap(laps_completed=lap_completed, invalid=invalid, pit=pit,
                 dist=(f0 + (f1 - f0) * k / max(1, n - 1)) * largo)
        s.mGameState = estado
        log._ingest(s)


def cruce_contado(log, lap_completed, lap_time):
    """Cruce de meta que AMS2 SI cuenta: sube el contador y publica el tiempo."""
    s = Snap(laps_completed=lap_completed, dist=0.0)
    s.mLastLapTime = lap_time
    log._ingest(s)


def _leer_jsonl(ruta):
    if not os.path.exists(ruta):
        return []
    return [json.loads(x) for x in open(ruta, encoding="utf-8").read().splitlines() if x.strip()]


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
        todas = [f for f in os.listdir(sd) if f.endswith(".csv.gz")] if sd else []
        traces = [f for f in todas if not f.startswith("P")]
        _ok("1 traza .csv.gz de pista", len(traces) == 1, traces)
        _ok("la out-lap cronometrada guarda su traza aparte, como P###",
            [f for f in todas if f.startswith("P")] == ["P001_472.500s.csv.gz"], todas)
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
        tr2 = [f for f in os.listdir(sd2) if f.endswith(".csv.gz") and not f.startswith("P")] if sd2 else []
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

        # Las apps de telemetria de terceros deciden que sesiones son comparables por
        # el NOMBRE, y usan el traducido -- no el crudo. Exportabamos "Spielberg"
        # donde el resto tiene "Spielberg (Spielberg)", asi que nuestras sesiones no
        # hacian pareja con ninguna.
        print("test_nombres_traducidos (para que el .srt matchee con otras apps):")
        log9 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel9_"))
        feed_lap(log9, 0); cross_to(log9, 1)
        sd9 = session_dir(log9._base)
        mj = json.load(open(os.path.join(sd9, "session.json"), encoding="utf-8")) if sd9 else {}
        _ok("guarda el nombre de pista traducido", mj.get("track_tr") == "Nordschleife",
            mj.get("track_tr"))
        _ok("guarda la variante traducida, distinta de la cruda",
            mj.get("track_variation_tr") == "24h Layout" and mj.get("track_variation") == "24h",
            (mj.get("track_variation_tr"), mj.get("track_variation")))
        shutil.rmtree(log9._base, ignore_errors=True)

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
        test_cierre_en_menu_principal()
        test_juego_cerrado_de_golpe()
        test_procesos_del_juego()
        test_apagar_cierra()
        test_carpeta_diferida()
        test_traza_atomica()
        test_corridas_por_boxes()
        test_garage_outlap_sin_contar()
        test_outlap_contada_desde_garage()
        test_vuelta1_de_carrera()

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
        # Las dos vueltas de boxes guardan traza P###, pero la anuncian en `trace_boxes`:
        # en `trace` los lectores de siempre (visor, informe) siguen viendo None.
        en_disco = sorted(f for f in os.listdir(sd) if f.startswith("P")) if sd else []
        _ok("vuelta de la parada y out-lap: traza P### en disco",
            [f[:4] for f in en_disco] == ["P001", "P003", "P004"], en_disco)
        _ok("anunciadas en trace_boxes y NO en trace",
            all(r.get("trace") is None and r.get("trace_boxes") in en_disco
                for r in (pit_lap or {}, out_lap or {})),
            [(r.get("trace"), r.get("trace_boxes")) for r in (pit_lap or {}, out_lap or {})])

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


def _menu(log, t, estado=1, reloj=None):
    """Un frame fuera de pista en el instante `t` del reloj falso. En el menu
    principal el juego no publica participantes: se manda asi a proposito, que es
    justo el caso que el return temprano de _ingest se comia."""
    reloj[0] = t
    s = Snap(laps_completed=0)
    s.mGameState = estado
    s.mNumParticipants = 0 if estado in (0, 1) else 5
    log._ingest(s)


def test_cierre_en_menu_principal():
    """Al terminar la clase el alumno sale al MENU PRINCIPAL: la carpeta tiene que
    cerrarse ahi (y quedar lista para entregar), no recien al entrar a otra sesion."""
    print("test_cierre_en_menu_principal (menu principal sostenido -> carpeta cerrada):")
    import time as _time
    base = tempfile.mkdtemp(prefix="ams2tel8_")
    try:
        reloj = [0.0]
        log = T.TelemetryLogger(base_dir=base)
        log._reloj = lambda: reloj[0]
        cerradas = []
        log.set_on_close(cerradas.append)
        feed_lap(log, 0); cross_to(log, 1)
        feed_lap(log, 1); cross_to(log, 2)
        a = log.sess_dir()
        _ok("girando: hay carpeta en curso", a is not None and os.path.isdir(a), a)

        _menu(log, 100.0, reloj=reloj)
        _menu(log, 104.9, reloj=reloj)
        log._drenar_cerrada()
        _ok("4.9 s en el menu principal: todavia NO se cierra (debounce)",
            log.sess_dir() == a and not cerradas, log.sess_dir())
        _menu(log, 105.1, reloj=reloj)
        log._drenar_cerrada()
        _ok("5 s seguidos en el menu principal: la carpeta se cierra", log.sess_dir() is None)
        _ok("y la bandeja la recibe (via _cerrada, como al rotar)", cerradas == [a], cerradas)
        _ok("la firma queda en blanco (la proxima entrada abre carpeta nueva)", log._sig is None)
        _ok("los rivales de la sesion quedan escritos", os.path.exists(os.path.join(a, "rivals.json")))
        _menu(log, 200.0, reloj=reloj)
        log._drenar_cerrada()
        _ok("seguir en el menu no la vuelve a anunciar", cerradas == [a], cerradas)

        # volver a entrar a la MISMA pista con el mismo auto: es otra sesion del juego
        _time.sleep(1.1)                  # el nombre de carpeta lleva la hora al segundo
        feed_lap(log, 0); cross_to(log, 1)
        b = log.sess_dir()
        _ok("al volver a pista se abre una carpeta NUEVA (no se pega a la cerrada)",
            b is not None and b != a, (a, b))
        tl = os.path.join(a, "timeline.jsonl")
        n_antes = len(open(tl, encoding="utf-8").read().splitlines())
        feed_lap(log, 1); cross_to(log, 2)
        _ok("y a la cerrada no le entra nada mas",
            len(open(tl, encoding="utf-8").read().splitlines()) == n_antes)

        # Lo que NO cierra: pausa, menu dentro de la sesion, reinicio, repeticion.
        for estado, nombre in ((3, "pausa"), (4, "menu con el reloj corriendo"),
                               (5, "reinicio de sesion"), (6, "repeticion")):
            for t in range(0, 61, 5):
                _menu(log, 1000.0 + t, estado=estado, reloj=reloj)
            log._drenar_cerrada()
            _ok(f"60 s en {nombre}: la carpeta sigue abierta", log.sess_dir() == b, log.sess_dir())
            reloj[0] = 0.0

        # un parpadeo NO acumula: el debounce exige 5 s SEGUIDOS en el menu principal
        _menu(log, 2000.0, reloj=reloj)
        _menu(log, 2003.0, reloj=reloj)
        _menu(log, 2003.5, estado=2, reloj=reloj)   # vuelve a pista un instante
        _menu(log, 2004.0, reloj=reloj)
        _menu(log, 2008.5, reloj=reloj)             # 4.5 s desde que volvio al menu
        log._drenar_cerrada()
        _ok("menu-pista-menu: el contador se reinicia, no se cierra", log.sess_dir() == b)
        _menu(log, 2009.1, reloj=reloj)
        log._drenar_cerrada()
        _ok("con 5 s seguidos desde el ultimo regreso, si", log.sess_dir() is None and cerradas[-1] == b)

        # el juego cerrado (GAME_EXITED) tambien es fin de sesion
        log3 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel8b_"))
        r3 = [0.0]
        log3._reloj = lambda: r3[0]
        feed_lap(log3, 0); cross_to(log3, 1); feed_lap(log3, 1); cross_to(log3, 2)
        c3 = log3.sess_dir()
        _menu(log3, 10.0, estado=0, reloj=r3)
        _menu(log3, 15.5, estado=0, reloj=r3)
        _ok("juego cerrado (GAME_EXITED) 5 s: tambien se cierra",
            log3.sess_dir() is None and log3._cerrada == c3)
        shutil.rmtree(log3._base, ignore_errors=True)

        # repeticion vista DESDE el menu principal (7): la sesion ya termino. Visto en
        # pista: carrera -> 4 -> 1 -> 7. (La repeticion DENTRO de la sesion, 6, no.)
        log4 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel8c_"))
        r4 = [0.0]
        log4._reloj = lambda: r4[0]
        feed_lap(log4, 0); cross_to(log4, 1); feed_lap(log4, 1); cross_to(log4, 2)
        c4 = log4.sess_dir()
        _menu(log4, 10.0, estado=7, reloj=r4)
        _menu(log4, 15.5, estado=7, reloj=r4)
        _ok("repeticion desde el menu principal (FRONT_END_REPLAY) 5 s: se cierra",
            log4.sess_dir() is None and log4._cerrada == c4)
        shutil.rmtree(log4._base, ignore_errors=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_juego_cerrado_de_golpe():
    """El alumno cierra AMS2 directo (o se cae), sin pasar por el menu principal. La
    shared memory NO desaparece (el bridge tiene handles abiertos): queda el ultimo
    frame congelado. La carpeta se cierra cuando el frame no cambia 5 s y AMS2 ya no
    esta en la lista de procesos; si el frame vuelve a moverse antes, nada."""
    print("test_juego_cerrado_de_golpe (memoria congelada y sin proceso -> carpeta cerrada):")
    base = tempfile.mkdtemp(prefix="ams2tel10_")
    try:
        reloj = [0.0]
        vivo = [True]
        consultas = [0]

        def juego_vivo():
            consultas[0] += 1
            return vivo[0]
        log = T.TelemetryLogger(base_dir=base)
        log._reloj = lambda: reloj[0]
        log._juego_vivo = juego_vivo
        feed_lap(log, 0); cross_to(log, 1)
        a = log.sess_dir()

        def frame(t, seq):
            """lo que hace _run con cada frame: vigilar y, si corresponde, ingerir"""
            reloj[0] = t
            if log._vigilar_juego(seq):
                log._ingest(Snap(laps_completed=1, dist=500.0))
        for k in range(10):
            frame(100.0 + k * 0.02, 1000 + k)            # el juego escribe: avanza
        frame(101.0, 1009)                               # y se congela
        frame(105.9, 1009)
        _ok("4.9 s congelada: sigue abierta", log.sess_dir() == a and consultas[0] == 0)
        frame(106.1, 1009)
        _ok("5 s congelada pero AMS2 sigue corriendo (pausa, minimizado): NO se cierra",
            log.sess_dir() == a and consultas[0] == 1, consultas[0])
        frame(106.5, 1009)
        _ok("la lista de procesos se mira a lo mas cada 2 s", consultas[0] == 1, consultas[0])
        vivo[0] = None
        frame(108.2, 1009)
        _ok("si no se puede saber si el juego esta, tampoco se cierra", log.sess_dir() == a)
        vivo[0] = False
        frame(110.3, 1009)
        log._drenar_cerrada()
        _ok("congelada y AMS2 ya no esta: la carpeta se cierra", log.sess_dir() is None
            and log._cerrada is None, log.sess_dir())
        carpetas = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
        for k in range(50):
            frame(111.0 + k, 1009)                       # el frame congelado dice "en pista"
        carpetas2 = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
        _ok("los frames congelados ya no se ingieren (no abren una carpeta vacia)",
            carpetas2 == carpetas and log.sess_dir() is None, carpetas2)
        frame(200.0, 5000)                               # el juego volvio
        _ok("cuando el frame vuelve a moverse, graba de nuevo (carpeta nueva)",
            log.sess_dir() is not None and log.sess_dir() != a, log.sess_dir())

        # sin memoria legible (open/snapshot fallan): mismo debounce, y se cancela
        # si vuelve antes
        log2 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel10b_"))
        r2 = [0.0]
        vivo2 = [True]
        log2._reloj = lambda: r2[0]
        log2._juego_vivo = lambda: vivo2[0]
        feed_lap(log2, 0); cross_to(log2, 1)
        feed_lap(log2, 1); cross_to(log2, 2)
        b = log2.sess_dir()
        for t, seq in ((10.0, 1), (11.0, None), (14.0, None), (15.5, 2)):
            r2[0] = t
            log2._vigilar_juego(seq)
        _ok("memoria perdida 4.5 s y vuelve: se cancela", log2.sess_dir() == b)
        for t in (20.0, 22.0, 25.1):
            r2[0] = t
            log2._vigilar_juego(None)
        _ok("memoria ilegible 5 s pero AMS2 vivo (cargando): NO se cierra", log2.sess_dir() == b)
        vivo2[0] = None
        r2[0] = 27.2
        log2._vigilar_juego(None)
        _ok("ilegible y no se sabe si el juego esta: tampoco", log2.sess_dir() == b)
        vivo2[0] = False
        r2[0] = 29.3
        log2._vigilar_juego(None)
        _ok("ilegible y AMS2 explicitamente cerrado: se cierra",
            log2.sess_dir() is None and log2._cerrada == b)
        shutil.rmtree(log2._base, ignore_errors=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_procesos_del_juego():
    """El juego se reconoce por nombre EXACTO de ejecutable, no por prefijo: un
    'ams2-dash.exe' o una herramienta 'ams2tools.exe' no pueden pasar por el juego
    (y dejar abierta para siempre la carpeta de una sesion que ya termino)."""
    print("test_procesos_del_juego (nombres exactos de AMS2):")
    import ams2_shm
    _ok("AMS2.exe y AMS2AVX.exe son el juego",
        {"ams2.exe", "ams2avx.exe"} <= ams2_shm.EXES_JUEGO)
    _ok("otros 'ams2*.exe' no (dash, herramientas, reportero de crashes)",
        not ({"ams2-dash.exe", "ams2tools.exe", "bugsplathd64.exe"} & ams2_shm.EXES_JUEGO))
    r = ams2_shm.juego_abierto()
    _ok("la consulta responde True/False/None sin reventar", r in (True, False, None), r)


def test_apagar_cierra():
    """Apagado, el hilo no lee la memoria ni drena: una carpeta abierta antes de
    apagar se quedaba "en curso" para siempre y la bandeja no la ofrecia."""
    print("test_apagar_cierra (grabador a off -> carpeta cerrada y entregable):")
    base = tempfile.mkdtemp(prefix="ams2tel11_")
    try:
        log = T.TelemetryLogger(base_dir=base)
        cerradas = []
        log.set_on_close(cerradas.append)
        feed_lap(log, 0); cross_to(log, 1)
        feed_lap(log, 1); cross_to(log, 2)
        a = log.sess_dir()
        log.set_mode("off")
        _ok("al apagar, la carpeta deja de ser 'la actual'", log.sess_dir() is None)
        log._drenar_cerrada()                    # lo que hace _run en modo off cada 0.5 s
        _ok("y la bandeja la recibe", cerradas == [a], cerradas)
        log.set_mode("full")
        feed_lap(log, 0); cross_to(log, 1)
        b = log.sess_dir()
        _ok("al prender en la misma sesion del juego (mismo segundo incluso) va a una "
            "carpeta NUEVA, nunca a la que ya se entrego",
            b is not None and b != a, (os.path.basename(a), os.path.basename(b or "")))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_carpeta_diferida():
    """Entrar a una sesion y salir sin girar no deja carpeta: se crea con el primer
    cruce de meta. Y la bandeja solo recibe carpetas con vueltas."""
    print("test_carpeta_diferida (sin cruzar meta no hay carpeta; sin vueltas no hay entrega):")
    base = tempfile.mkdtemp(prefix="ams2tel13_")
    try:
        reloj = [0.0]
        log = T.TelemetryLogger(base_dir=base)
        log._reloj = lambda: reloj[0]
        cerradas = []
        log.set_on_close(cerradas.append)
        # en el garage, con la goma ya poblada (evento 'start') y el reloj corriendo
        for k in range(100):
            s = Snap(laps_completed=0, dist=0.0, pit=4)
            s.mGameState = 4
            log._ingest(s)
        _ok("en el garage, sin cruzar meta: NO hay carpeta en disco",
            not [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))])
        _ok("aunque el grabador ya sabe como se va a llamar", log.sess_dir() is not None)
        _menu(log, 10.0, reloj=reloj)
        _menu(log, 15.5, reloj=reloj)
        log._drenar_cerrada()
        _ok("vuelve al menu: nada en disco y nada para la bandeja",
            not os.listdir(base) and cerradas == [], (os.listdir(base), cerradas))
        # ahora si gira: el primer cruce crea la carpeta, con los eventos del garage
        feed_lap(log, 0, pit=2); cross_to(log, 1)
        sd = log.sess_dir()
        _ok("el primer cruce de meta crea la carpeta", sd and os.path.isdir(sd))
        recs = [json.loads(x) for x in open(os.path.join(sd, "timeline.jsonl"), encoding="utf-8")]
        orden = [r.get("event") or r.get("kind") for r in recs]
        primera_vuelta = next(i for i, r in enumerate(recs) if r.get("type") == "lap")
        _ok("con los eventos del garage (goma de largada, boxes) volcados ANTES de la vuelta",
            "start" in orden[:primera_vuelta] and "pit_in" in orden[:primera_vuelta], orden)
        _ok("y su session.json con la hora de ENTRADA a la sesion",
            os.path.exists(os.path.join(sd, "session.json")))
        reloj[0] = 100.0
        _menu(log, 100.0, reloj=reloj)
        _menu(log, 105.5, reloj=reloj)
        log._drenar_cerrada()
        _ok("carpeta con solo la vuelta de salida (sin resumen): no se le ofrece a la bandeja",
            cerradas == [], cerradas)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_traza_atomica():
    """La linea de tiempo anuncia el nombre de la traza antes del volcado: bajo ese
    nombre tiene que existir solo la traza COMPLETA (se escribe a .tmp y se renombra)."""
    print("test_traza_atomica (gzip a .tmp + os.replace):")
    base = tempfile.mkdtemp(prefix="ams2tel12_")
    orig = T.os.replace
    vistos = []

    def replace(src, dst):
        vistos.append((os.path.basename(src), os.path.basename(dst), os.path.exists(dst)))
        return orig(src, dst)
    try:
        T.os.replace = replace
        log = T.TelemetryLogger(base_dir=base)
        anuncios = []
        escribir = log._write_timeline

        def write_timeline(rec):
            # cuando la linea de tiempo ANUNCIA una traza, esa traza ya tiene que existir
            for clave in ("trace", "trace_boxes"):
                if rec.get(clave):
                    anuncios.append(os.path.exists(os.path.join(log._sess_dir, rec[clave])))
            return escribir(rec)
        log._write_timeline = write_timeline
        feed_lap(log, 0); cross_to(log, 1)
        feed_lap(log, 1); cross_to(log, 2)
        sd = session_dir(base)
        _ok("la linea de tiempo anuncia la traza recien cuando la traza ya existe",
            anuncios == [True, True], anuncios)
        todas = [x for x in vistos if x[1].endswith(".csv.gz")]
        _ok("la traza de boxes (P###) tambien pasa por .tmp + rename",
            [x[1][:1] for x in todas] == ["P", "L"] and all(x[0] == x[1] + ".tmp" for x in todas), todas)
        gz = [x for x in todas if not x[1].startswith("P")]
        _ok("la traza se escribe a .tmp y se renombra a su nombre final",
            len(gz) == 1 and gz[0][0] == gz[0][1] + ".tmp", gz)
        _ok("antes del rename el nombre final NO existia (nadie pudo leerla a medias)",
            gz and gz[0][2] is False)
        _ok("no queda ningun .tmp", not [f for f in os.listdir(sd) if f.endswith(".tmp")])
        with gzip.open(os.path.join(sd, gz[0][1]), "rt", encoding="utf-8") as f:
            _ok("y la traza final se lee entera", f.readline().strip().split(",") == T.HEADER)
    finally:
        T.os.replace = orig
        shutil.rmtree(base, ignore_errors=True)


def test_corridas_por_boxes():
    """El frio y la tanda de despues son DOS corridas de la MISMA carpeta: las separa
    solo la pasada por boxes (mPitMode), sin cerrar la sesion."""
    print("test_corridas_por_boxes (frio -> boxes -> tanda: una carpeta, dos corridas):")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import analyze_telemetry as A
    base = tempfile.mkdtemp(prefix="ams2tel9_")
    try:
        log = T.TelemetryLogger(base_dir=base)
        feed_lap(log, 0, pit=2); cross_to(log, 1)          # sale de boxes: out-lap
        feed_lap(log, 1); cross_to(log, 2)                 # (out-lap)
        for n in range(2, 7):                              # el frio: 5 vueltas
            feed_lap(log, n); cross_to(log, n + 1)
        feed_lap(log, 7, pit=1); cross_to(log, 8)          # entra a boxes
        for n in range(8, 11):                             # vuelve a salir: out + 2
            feed_lap(log, n); cross_to(log, n + 1)
        carpetas = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
        _ok("una sola carpeta (la sesion no se cerro en boxes)", len(carpetas) == 1, carpetas)
        cs, partida = A.corridas_struct(os.path.join(base, carpetas[0]))
        _ok("dos corridas: el frio (5) y la de despues de boxes (2)",
            partida and [c["cronometradas"] for c in cs] == [5, 2],
            (partida, [c["cronometradas"] for c in cs]))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_garage_outlap_sin_contar():
    """AMS2 NO cuenta la out-lap que sale del garage cuando el pit lane no cruza la
    linea de meta (la mayoria de las pistas). Al cruzar meta arranca el cronometro,
    pero mLapsCompleted recien sube al cerrar la vuelta LANZADA. El grabador juntaba
    garage + out-lap + lanzada en una sola vuelta 'pit', sin traza, y marcaba 'out' a
    la siguiente. Caso real: Spa con el MC12, 2026-10-18 -- ninguna de sus 5 vueltas
    quedo grabada, incluidas 2:16.871 y 2:16.555, validas en el juego."""
    print("test_garage_outlap_sin_contar (garage -> out-lap no contada -> lanzada CON traza):")
    base = tempfile.mkdtemp(prefix="ams2tel14_")
    try:
        log = T.TelemetryLogger(base_dir=base)
        # entra a la sesion en el garage (despues de la linea, como en Spa)
        feed_tramo(log, 0, 0.01, 0.01, n=50, pit=4, estado=4)
        feed_tramo(log, 0, 0.01, 0.05, n=20, pit=5)             # pit lane de salida
        feed_tramo(log, 0, 0.05, 0.99, n=300, invalid=True)     # out-lap (AMS2 la invalida)
        feed_tramo(log, 0, 0.0, 0.99, n=260)                    # cruzo meta SIN contar: lanzada
        cruce_contado(log, 1, 136.871)                          # recien aca sube el contador
        feed_tramo(log, 1, 0.0, 0.99, n=260)                    # una mas, normal
        cruce_contado(log, 2, 137.487)
        # ESC al garage a mitad de la vuelta 3 y vuelta a salir (el contador NO se reinicia)
        feed_tramo(log, 2, 0.0, 0.40, n=100)
        feed_tramo(log, 2, 0.01, 0.01, n=50, pit=4, estado=4)
        feed_tramo(log, 2, 0.01, 0.05, n=20, pit=5)
        feed_tramo(log, 2, 0.05, 0.99, n=300, invalid=True)
        feed_tramo(log, 2, 0.0, 0.99, n=260)                    # lanzada tras el garage
        cruce_contado(log, 3, 136.555)

        sd = session_dir(base)
        trazas = sorted(f for f in os.listdir(sd) if f.endswith(".csv.gz")) if sd else []
        _ok("las tres lanzadas tienen traza L (la out-lap invalida no las ensucia)",
            trazas == ["L001_136.871s.csv.gz", "L002_137.487s.csv.gz", "L003_136.555s.csv.gz"],
            trazas)
        filas = []
        if "L003_136.555s.csv.gz" in trazas:
            with gzip.open(os.path.join(sd, "L003_136.555s.csv.gz"), "rt", encoding="utf-8") as f:
                f.readline()
                filas = [r.split(",") for r in f.read().strip().splitlines()]
        _ok("la traza de la lanzada es SOLO la lanzada (sin garage ni out-lap)",
            abs(len(filas) - 260) <= 2, len(filas))
        largo = Snap().mTrackLength
        _ok("y arranca en la linea de meta",
            bool(filas) and float(filas[0][T.HEADER.index("lap_dist")]) < 0.05 * largo,
            filas[0][T.HEADER.index("lap_dist")] if filas else None)

        res = _leer_jsonl(os.path.join(sd, "summary.jsonl"))
        _ok("summary: las tres lanzadas y ninguna out-lap",
            [(r["lap"], r["lap_time"]) for r in res] == [(1, 136.871), (2, 137.487), (3, 136.555)],
            [(r["lap"], r["lap_time"]) for r in res])
        _ok("summary: muestras de UNA vuelta, no de garage + out-lap + lanzada",
            bool(res) and all(abs(r["samples"] - 261) <= 2 for r in res), [r["samples"] for r in res])

        tl = [r for r in _leer_jsonl(os.path.join(sd, "timeline.jsonl")) if r.get("type") == "lap"]
        _ok("timeline: out-lap (pit, sin tiempo ni traza) antes de cada tanda",
            [(r["lap"], r["kind"], r["lap_time"], r["trace"]) for r in tl if r["kind"] == "pit"]
            == [(0, "pit", None, None), (2, "pit", None, None)],
            [(r["lap"], r["kind"], r["lap_time"]) for r in tl])
        _ok("la salida del garage SIN tiempo no guarda traza de boxes",
            not any(r.get("trace_boxes") for r in tl)
            and not [f for f in os.listdir(sd) if f.startswith("P")],
            [r.get("trace_boxes") for r in tl])
        _ok("timeline: la vuelta que sigue a la lanzada ya NO es 'out'",
            [r["kind"] for r in tl] == ["pit", "flying", "flying", "pit", "flying"],
            [r["kind"] for r in tl])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_outlap_contada_desde_garage():
    """En las pocas pistas con el garage ANTES de la linea (Imola GP, Jerez, Goiania
    Mixed, Virginia) el pit lane cruza meta, ahi arranca el cronometro y la out-lap SI
    se cuenta. Ese cruce dentro del pit lane no puede partir nada; y la vuelta que
    sigue a la out-lap es lanzada, no 'out' (antes se perdia igual)."""
    print("test_outlap_contada_desde_garage (pit lane que cruza meta -> out-lap contada):")
    base = tempfile.mkdtemp(prefix="ams2tel15_")
    try:
        log = T.TelemetryLogger(base_dir=base)
        feed_tramo(log, 0, 0.97, 0.97, n=50, pit=4, estado=4)   # garage antes de la linea
        feed_tramo(log, 0, 0.97, 0.995, n=20, pit=5)
        feed_tramo(log, 0, 0.0, 0.03, n=20, pit=5)              # cruza meta en el pit lane
        feed_tramo(log, 0, 0.03, 0.99, n=300)                   # out-lap
        cruce_contado(log, 1, 140.250)
        feed_tramo(log, 1, 0.0, 0.99, n=260)                    # lanzada
        cruce_contado(log, 2, 110.500)

        sd = session_dir(base)
        trazas = sorted(f for f in os.listdir(sd) if f.endswith(".csv.gz")) if sd else []
        _ok("de pista, solo la lanzada tiene traza",
            [f for f in trazas if not f.startswith("P")] == ["L002_110.500s.csv.gz"], trazas)
        _ok("la out-lap contada guarda su traza de boxes (P###)",
            [f for f in trazas if f.startswith("P")] == ["P001_140.250s.csv.gz"], trazas)
        tl = [r for r in _leer_jsonl(os.path.join(sd, "timeline.jsonl")) if r.get("type") == "lap"]
        _ok("timeline: out-lap 'pit' y lanzada 'flying' (el cruce en el pit lane no parte)",
            [(r["lap"], r["kind"]) for r in tl] == [(1, "pit"), (2, "flying")],
            [(r["lap"], r["kind"]) for r in tl])
        res = _leer_jsonl(os.path.join(sd, "summary.jsonl"))
        _ok("summary: solo la lanzada", [r["lap"] for r in res] == [2], [r["lap"] for r in res])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _carrera(log, lap_completed, f0, f1, n, estado, pit=0, lap_time=None):
    """Muestras de una sesion de CARRERA. `estado` = mRaceState del auto (1 sin largar,
    2 corriendo). Con lap_time, la ultima muestra es el cruce que cierra la vuelta."""
    largo = Snap().mTrackLength
    for k in range(n):
        s = Snap(laps_completed=lap_completed, pit=pit,
                 dist=(f0 + (f1 - f0) * k / max(1, n - 1)) * largo)
        s.mSessionState = 5
        s.mRaceStates = [estado] * 64
        log._ingest(s)
    if lap_time is not None:
        s = Snap(laps_completed=lap_completed + 1, dist=0.0)
        s.mSessionState = 5
        s.mRaceStates = [2] * 64
        s.mLastLapTime = lap_time
        log._ingest(s)


def test_vuelta1_de_carrera():
    """La vuelta 1 de una carrera nunca se grababa: el grabador daba por hecho que la
    primera vuelta de la sesion sale de boxes, y el buffer arrastraba la espera en la
    parrilla (y la formacion). Caso real: Spa con el Viper, 2026-10-18 -- vuelta 1 de
    151.351 s con 267 s de muestras y sin traza. Ahora se graba desde la largada con
    traza S### y kind 'start', FUERA del resumen: no es representativa (largada, trafico)."""
    print("test_vuelta1_de_carrera (parrilla -> largada -> vuelta 1 con traza, sin formacion):")
    base = tempfile.mkdtemp(prefix="ams2tel16_")
    try:
        # largada detrás de la linea, despues de una vuelta de formacion
        log = T.TelemetryLogger(base_dir=base)
        _carrera(log, 0, 0.30, 0.97, 300, estado=1)        # formacion (sin largar)
        _carrera(log, 0, 0.97, 0.97, 200, estado=1)        # parado en la parrilla
        _carrera(log, 0, 0.97, 0.999, 20, estado=2)        # luces: arranca, aun antes de la linea
        _carrera(log, 0, 0.0, 0.99, 260, estado=2, lap_time=151.351)   # vuelta 1
        _carrera(log, 1, 0.0, 0.99, 260, estado=2, lap_time=138.188)   # vuelta 2
        sd = session_dir(base)
        trazas = sorted(f for f in os.listdir(sd) if f.endswith(".csv.gz")) if sd else []
        _ok("la vuelta 1 tiene traza propia (S) y la 2 la normal (L)",
            trazas == ["L002_138.188s.csv.gz", "S001_151.351s.csv.gz"], trazas)
        filas = []
        if "S001_151.351s.csv.gz" in trazas:
            with gzip.open(os.path.join(sd, "S001_151.351s.csv.gz"), "rt", encoding="utf-8") as f:
                f.readline()
                filas = [r.split(",") for r in f.read().strip().splitlines()]
        largo = Snap().mTrackLength
        _ok("la traza de la vuelta 1 no trae formacion ni parrilla", abs(len(filas) - 260) <= 2,
            len(filas))
        _ok("y arranca en la linea (distancia creciente para el visor)",
            bool(filas) and float(filas[0][T.HEADER.index("lap_dist")]) < 0.05 * largo,
            filas[0][T.HEADER.index("lap_dist")] if filas else None)
        tl = [r for r in _leer_jsonl(os.path.join(sd, "timeline.jsonl")) if r.get("type") == "lap"]
        v1 = tl[0] if tl else {}
        _ok("timeline: vuelta 1 'start', sin marca de salida de boxes, con su traza",
            (v1.get("lap"), v1.get("kind"), v1.get("out"), v1.get("trace"))
            == (1, "start", False, "S001_151.351s.csv.gz"),
            (v1.get("lap"), v1.get("kind"), v1.get("out"), v1.get("trace")))
        _ok("timeline: muestras de la vuelta, no de la espera", abs(v1.get("samples", 0) - 261) <= 2,
            v1.get("samples"))
        res = _leer_jsonl(os.path.join(sd, "summary.jsonl"))
        _ok("summary: la vuelta 1 NO entra (no es representativa)",
            [r["lap"] for r in res] == [2], [r["lap"] for r in res])
        secs = _leer_jsonl(os.path.join(sd, "sectors.jsonl"))
        _ok("sectors: tampoco", [r["lap"] for r in secs] == [2], [r["lap"] for r in secs])
        shutil.rmtree(base, ignore_errors=True)

        # parrilla DESPUES de la linea: la vuelta arranca en las luces, con la partida
        base = tempfile.mkdtemp(prefix="ams2tel16b_")
        log = T.TelemetryLogger(base_dir=base)
        _carrera(log, 0, 0.01, 0.01, 200, estado=1)
        _carrera(log, 0, 0.01, 0.99, 280, estado=2, lap_time=120.5)
        sd = session_dir(base)
        trazas = sorted(f for f in os.listdir(sd) if f.endswith(".csv.gz")) if sd else []
        filas = []
        if trazas:
            with gzip.open(os.path.join(sd, trazas[0]), "rt", encoding="utf-8") as f:
                f.readline()
                filas = f.read().strip().splitlines()
        _ok("parrilla despues de la linea: la traza trae la partida desde las luces",
            trazas == ["S001_120.500s.csv.gz"] and abs(len(filas) - 280) <= 2,
            (trazas, len(filas)))
        shutil.rmtree(base, ignore_errors=True)

        # largada desde boxes: sigue siendo out-lap
        base = tempfile.mkdtemp(prefix="ams2tel16c_")
        log = T.TelemetryLogger(base_dir=base)
        _carrera(log, 0, 0.96, 0.96, 200, estado=1, pit=2)
        _carrera(log, 0, 0.96, 0.99, 30, estado=2, pit=3)
        _carrera(log, 0, 0.0, 0.99, 260, estado=2, lap_time=125.0)
        sd = session_dir(base)
        trazas = sorted(f for f in os.listdir(sd) if f.endswith(".csv.gz")) if sd else []
        tl = [r for r in _leer_jsonl(os.path.join(sd, "timeline.jsonl")) if r.get("type") == "lap"]
        _ok("largada desde boxes: out-lap sin traza de pista ni resumen",
            not [f for f in trazas if not f.startswith("P")]
            and [r["kind"] for r in tl] in (["pit"], ["out"])
            and not _leer_jsonl(os.path.join(sd, "summary.jsonl")),
            (trazas, [r["kind"] for r in tl]))
        _ok("largada desde boxes: la vuelta 1 queda como traza de boxes, anunciada aparte",
            trazas == ["P001_125.000s.csv.gz"] and tl[0].get("trace") is None
            and tl[0].get("trace_boxes") == "P001_125.000s.csv.gz", (trazas, tl[0] if tl else None))
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
