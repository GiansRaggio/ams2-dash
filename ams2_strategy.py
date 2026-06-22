#!/usr/bin/env python3
"""Director de estrategia de carrera para AMS2 (Shared Memory).

Se alimenta de un snapshot de la shared memory por tick (lo llama bridge_shm) y
mantiene el estado de combustible / neumaticos / vueltas para producir un
"payload" de estrategia que el dash muestra en su pagina ESTRATEGIA.

Diseno verificado contra el header CREST2-AMS2 v14 (ver ams2_shm.py) y contra la
metodologia de ingenieros de carrera de sim racing. Decisiones NO triviales que
estan baked-in aca (cada una corrige una trampa real):

  * UNIDADES: el header documenta mEventTimeRemaining en MILISEGUNDOS y
    mSessionDuration en MINUTOS (no segundos). Se normaliza TODO a segundos con
    un TIME_SCALE detectado empiricamente al inicio (ver _detect_time_scale).
  * mTyreGrip NO se usa: el header lo marca 'OBSOLETE' y en AMS2 no se puebla.
    El desgaste sale de mTyreWear (delta real por vuelta) + mTyreTemp + deriva
    del lap-time. La direccion de mTyreWear se autodetecta por el signo del delta.
  * mFuelCapacity tiene un comentario auto-contradictorio: se usa solo si > 1.0
    (litros plausibles); si no, cae a una capacidad por defecto.
  * mEnforcedPitStopLap: el header v14 quito el UNSET=-1, asi que solo se trata
    como pit obligatorio si es >= 1 (-1 y 0 = sin pit obligatorio).
  * Modo de carrera robusto: mLapsInEvent > 0 => por vueltas; si no, y
    mEventTimeRemaining >= 0 => por tiempo. Guard de corrupcion: mVersion == 14.
  * fuel_per_lap y lap_time = media de las ultimas vueltas VERDES (se excluyen
    out-laps, repostajes e vueltas anomalas tipo Safety Car).
  * El margen de combustible se razona en VUELTAS, nunca en litros sueltos.

MODO PLANIFICACION: la shared memory solo describe la sesion ACTIVA; durante
practica/clasificacion NO expone el formato de la carrera futura. Por eso el dash
permite cargar el formato a mano (set_race_plan); con eso + el consumo medido en
practica se proyecta el plan de carrera. En cuanto se entra a la carrera real
(mSessionState == RACE) los datos en vivo toman el control.
"""
import math

import ams2_shm

# --- estados de juego con telemetria viva ---
_LIVE = {
    ams2_shm.GAME_INGAME_PLAYING,
    ams2_shm.GAME_INGAME_PAUSED,
    ams2_shm.GAME_INGAME_INMENU_TIME_TICKING,
}

# --- mSessionState (enum pCARS2 Type#1) ---
SESSION_INVALID = 0
SESSION_PRACTICE = 1
SESSION_TEST = 2
SESSION_QUALIFY = 3
SESSION_FORMATION = 4
SESSION_RACE = 5
SESSION_TIME_ATTACK = 6
_SESSION_LABEL = {1: "práctica", 2: "práctica", 3: "clasificación",
                  4: "vuelta previa", 5: "carrera", 6: "hotlap"}

# --- parametros de estrategia (configurables) ---
SAFETY_LAPS_TIMED = 1.5      # colchon de combustible en carrera por tiempo (mas incierta)
SAFETY_LAPS_LAPS = 1.0       # colchon en carrera por vueltas
SAFETY_LAPS_FLOOR = 1.0      # regla dura: nunca operar bajo 1 vuelta de margen
OUTLAP_FACTOR = 1.15         # el out-lap consume ~15% mas (motor frio, vuelta a fondo desde pit)
MARGIN_PCT = 0.02            # margen alternativo como % del consumo total

GREEN_KEEP = 5               # cuantas vueltas verdes promediar (fuel y lap-time)
ANOMALY_FACTOR = 1.30        # lap_time > 1.30 * mediana verde => anomala (SC/trafico/lluvia)
TYRE_WARMUP_LAPS = 3         # no juzgar desgaste en las primeras vueltas del stint (calentando)
WEAR_THRESHOLD = 0.80        # umbral operativo: planear cambio cuando el peor neumatico llega aca
WEAR_AMBER = 0.50            # neumatico en amarillo
WEAR_FLAT_LAPS = 4           # si el desgaste no se mueve en N vueltas verdes => dato plano
TEMP_COLD = 70.0             # ventana termica GT3 generica (C)
TEMP_HOT = 100.0
PIT_WINDOW = 3               # +/- vueltas alrededor del objetivo de parada

# --- detector de crossover lluvia->lisos (pista que seca) ---
# AMS2 NO expone el nivel de mojado de pista por shared memory (verificado vs
# ams2_shm.py / header CREST2 v14): el unico canal de lluvia es mRainDensity =
# precipitacion CAYENDO, no agua en superficie. Por eso el secado se INFIERE de
# proxies: lluvia baja y cayendo + pista calentando + gomas de lluvia recalentando
# (la wet se refrigera con el agua; sin agua sobrecalienta -> senal mas confiable).
RAIN_DRY_THR = 0.13          # densidad de lluvia bajo la cual cuenta como "dejo de llover"
TRACK_WARM_SLOPE = 0.15      # +C/vuelta sostenido => pista calentando/secando
WET_OVERHEAT = 72.0          # temp (C) de gomas de lluvia que delata pista sin agua

DEFAULT_TANK_L = 100.0       # fallback de capacidad si mFuelCapacity no es fiable
EPS = 1e-6
INF = float("inf")

# enum mPitMode v14: NONE=0, DRIVING_INTO=1, IN_PIT=2, DRIVING_OUT=3, IN_GARAGE=4, OUT_GARAGE=5
_PIT_NONE = 0
_PIT_INSIDE = (1, 2, 4)      # entrando / parado / en garage -> cuenta como "en boxes"


def _argmax(xs):
    bi, bv = 0, xs[0]
    for i, v in enumerate(xs):
        if v > bv:
            bi, bv = i, v
    return bi


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


class StrategyEngine:
    """Acumula estado por vuelta y proyecta el plan de combustible/gomas."""

    def __init__(self):
        self._race_override = None      # formato de carrera manual {mode,value,additional}
        self._use_all_laps = True       # contar vueltas anomalas/invalidas (default: si)
        self._reset_session()

    # ---------------- ciclo de sesion ----------------
    def _reset_session(self):
        # OJO: _race_override NO se toca aca (persiste entre sesiones del mismo evento
        # y mientras el front no lo borre). Se inicializa en __init__.
        self._sig = None
        self._compound = None
        self._time_scale = None        # factor -> segundos (1.0 o 0.001), lockeado al detectar
        self._cap = DEFAULT_TANK_L     # capacidad efectiva (litros), aprendida de mFuelCapacity>1
        self._laps_done = -1           # mLapsCompleted del jugador en el ultimo tick
        self._fuel_per_lap = []        # ultimas vueltas verdes (L/vuelta)
        self._fuel_at_cross = None     # nivel (L) al ultimo cruce de meta
        self._lap_times = []           # ultimas vueltas verdes (s)
        self._wear_invert = None       # None=desconocido; True si mTyreWear decrece con uso
        self._wear_seed = []           # primeros deltas para autodetectar direccion
        self._wear_at_cross = None     # [4] desgaste efectivo al ultimo cruce
        self._wear_rate = [0.0, 0.0, 0.0, 0.0]   # EMA por rueda
        self._wear_flat = 0            # vueltas verdes seguidas sin movimiento de desgaste
        self._stint_lap = 0            # vueltas dentro del stint actual
        self._pit_prev = False         # estaba en boxes el tick anterior
        self._skip_next = False        # la proxima vuelta es out-lap (post-pit) -> excluir
        self._has_pitted = False       # cumplio al menos una parada (para pit obligatorio)
        self._stint_lap_times = []     # lap-times del stint actual (para deriva)
        # --- detector de crossover lluvia->lisos ---
        self._rain_hist = []           # densidad de lluvia por vuelta (tendencia de secado)
        self._track_hist = []          # temp de pista por vuelta
        self._wet_temp_hist = []       # peor temp de neumatico por vuelta (wets recalentando)
        self._cross_state = "green"    # estado del semaforo (histeresis asimetrica)
        self._last = {"calibrating": True, "live": False, "mode": "none"}

    # ---------------- API publica (comandos del dash) ----------------
    def set_race_plan(self, mode, value, additional=0):
        """Carga el formato de carrera a mano (para planificar desde practica)."""
        if mode not in ("timed", "laps") or value is None or value <= 0:
            return
        self._race_override = {"mode": mode, "value": float(value),
                               "additional": max(0, int(additional))}

    def clear_race_plan(self):
        self._race_override = None

    def set_use_all_laps(self, on):
        """Si True (default), NO descarta vueltas anomalas/lentas del calculo.

        La validez por limites de pista (cortar) nunca afecta el calculo: una
        vuelta invalidada consume y rueda igual. Este flag solo controla el
        filtro de vueltas muy lentas (Safety Car / trompo / trafico).
        """
        self._use_all_laps = bool(on)

    # ---------------- ingestion ----------------
    def update(self, d):
        """Procesa un snapshot. El resultado se lee con payload()."""
        # Guard de corrupcion (AMS2 comparte el nombre de mapeo con PCARS2).
        if d.mVersion != ams2_shm.SHARED_MEMORY_VERSION or d.mNumParticipants <= 0:
            return
        v = d.mViewedParticipantIndex
        if not (0 <= v < ams2_shm.STORED_PARTICIPANTS_MAX):
            return
        p = d.mParticipantInfo[v]

        # Cambio de sesion (auto/pista) -> reset de datos medidos (no del plan manual).
        sig = (bytes(d.mTrackLocation), bytes(d.mCarName))
        if sig != self._sig:
            self._reset_session()
            self._sig = sig

        # Capacidad efectiva: confiar en mFuelCapacity solo si parece litros.
        if d.mFuelCapacity > 1.0:
            self._cap = d.mFuelCapacity

        # Cambio de compuesto (ej. slick->wet) -> resetear baseline de gomas y ritmo.
        comp = bytes(d.mTyreCompound[0])
        if self._compound is not None and comp != self._compound:
            self._wear_at_cross = None
            self._wear_rate = [0.0, 0.0, 0.0, 0.0]
            self._wear_invert = None
            self._wear_seed = []
            self._wear_flat = 0
            self._lap_times = []
            self._wet_temp_hist = []       # temps del compuesto anterior no aplican
            self._cross_state = "green"    # el cruce se reinicia con el compuesto nuevo
        self._compound = comp

        if self._time_scale is None:
            self._detect_time_scale(d)

        live = d.mGameState in _LIVE
        in_pit = d.mPitMode in _PIT_INSIDE
        if in_pit and not self._pit_prev:
            self._has_pitted = True
            self._stint_lap = 0
            self._stint_lap_times = []
            self._skip_next = True        # la vuelta que sigue al pit es out-lap
        self._pit_prev = in_pit

        laps_done = p.mLapsCompleted
        if self._laps_done < 0:
            self._laps_done = laps_done
            self._fuel_at_cross = d.mFuelLevel * self._cap
            self._wear_at_cross = self._wear_vec(d)
        elif laps_done > self._laps_done and live:
            self._on_lap_cross(d, laps_done)
            self._laps_done = laps_done
        elif laps_done < self._laps_done:
            self._laps_done = laps_done   # reinicio de sesion/vueltas

        self._last = self._project(d, p, live)

    def _detect_time_scale(self, d):
        """Fija TIME_SCALE para llevar mEventTimeRemaining a SEGUNDOS.

        El header v14 dice ms para mEventTimeRemaining y minutos para
        mSessionDuration, pero hay ambiguedad historica entre versiones. Como el
        tiempo restante nunca puede exceder la duracion (en la misma unidad), un
        valor de 'restante' mucho mayor que la duracion-en-segundos delata ms.
        """
        rem = d.mEventTimeRemaining
        if rem is None or rem < 0:
            return
        dur_s = d.mSessionDuration * 60.0   # header: minutos -> segundos
        if dur_s > EPS:
            self._time_scale = 0.001 if rem > dur_s * 1.5 else 1.0
        elif rem > 14400:                   # sin duracion: >4h en "segundos" es absurdo -> ms
            self._time_scale = 0.001
        else:
            self._time_scale = 1.0

    def _wear_vec(self, d):
        """Desgaste efectivo por rueda (0=nuevo .. 1=gastado), con direccion resuelta."""
        raw = [d.mTyreWear[i] for i in range(4)]
        if self._wear_invert:
            return [1.0 - x for x in raw]
        return list(raw)

    def _on_lap_cross(self, d, laps_done):
        self._stint_lap += 1
        fuel_now = d.mFuelLevel * self._cap
        last_lap = d.mLastLapTime

        out_lap = self._skip_next
        self._skip_next = False

        # Una vuelta cuenta salvo que sea out-lap (post-pit) o, con el filtro
        # activo, ANOMALA (mucho mas lenta que la mediana: SC / trompo / trafico).
        # La VALIDEZ por limites de pista (cortar) NO importa: una vuelta
        # invalidada consume y rueda igual -> cuenta. Con _use_all_laps=True (def)
        # tampoco se descartan las lentas.
        med = _median(self._lap_times)
        slow = (med is not None) and bool(last_lap) and (last_lap > med * ANOMALY_FACTOR)
        count_lap = self._use_all_laps or not slow

        # --- combustible: delta entre cruces de meta ---
        if self._fuel_at_cross is not None:
            used = self._fuel_at_cross - fuel_now
            refuel = used < -0.05          # subio el nivel => repostaje, nunca es "consumo"
            if not out_lap and not refuel and used > 0.05 and count_lap:
                self._fuel_per_lap.append(used)
                del self._fuel_per_lap[:-GREEN_KEEP]
        self._fuel_at_cross = fuel_now

        # --- lap-time (para vueltas-por-tiempo y deriva de ritmo) ---
        if not out_lap and last_lap and last_lap > 0 and count_lap:
            self._lap_times.append(last_lap)
            del self._lap_times[:-GREEN_KEEP]
            self._stint_lap_times.append(last_lap)
            del self._stint_lap_times[:-GREEN_KEEP]

        # --- condiciones de pista por vuelta (para el detector de crossover) ---
        # Se muestrea SIEMPRE al cruzar meta (lluvia/pista son del entorno, no del
        # compuesto). El peor neumatico delata wets recalentando en pista seca.
        self._rain_hist.append(d.mRainDensity)
        del self._rain_hist[:-GREEN_KEEP]
        self._track_hist.append(d.mTrackTemperature)
        del self._track_hist[:-GREEN_KEEP]
        self._wet_temp_hist.append(max(d.mTyreTemp[i] for i in range(4)))
        del self._wet_temp_hist[:-GREEN_KEEP]

        # --- neumaticos: delta de desgaste por rueda al cruzar meta ---
        wear_now = self._wear_vec(d)
        if self._wear_at_cross is not None and not out_lap:
            deltas = [wear_now[i] - self._wear_at_cross[i] for i in range(4)]
            # Autodeteccion de direccion: si en las primeras vueltas el desgaste
            # "baja", el campo es grip-remanente -> invertir y rehacer baseline.
            if self._wear_invert is None:
                self._wear_seed.append(max(deltas, key=abs))
                if len(self._wear_seed) >= 2:
                    self._wear_invert = (sum(self._wear_seed) < 0)
                    if self._wear_invert:
                        self._wear_at_cross = self._wear_vec(d)
                        return
            else:
                moved = False
                for i in range(4):
                    if deltas[i] > EPS and self._stint_lap > TYRE_WARMUP_LAPS:
                        self._wear_rate[i] += 0.4 * (deltas[i] - self._wear_rate[i])
                        moved = True
                self._wear_flat = 0 if moved else self._wear_flat + 1
        self._wear_at_cross = wear_now

    # ---------------- proyeccion ----------------
    def _tyres(self, d):
        """Bloque de neumaticos (wear% + temp + estado) + horizonte de desgaste."""
        wear_eff = self._wear_vec(d)
        worst = _argmax(wear_eff)
        wear_max = wear_eff[worst]
        rate_max = max(self._wear_rate)
        flat = self._wear_flat >= WEAR_FLAT_LAPS or rate_max <= EPS
        horizon = INF if flat else max(0.0, (WEAR_THRESHOLD - wear_max) / rate_max)
        temps = [d.mTyreTemp[i] for i in range(4)]
        tyres = []
        for i in range(4):
            w = wear_eff[i]
            st = "red" if w >= WEAR_THRESHOLD else "amber" if w >= WEAR_AMBER else "green"
            t = temps[i]
            tstat = "hot" if t >= TEMP_HOT else "cold" if (0 < t < TEMP_COLD) else "ok"
            tyres.append({"w": round(w * 100), "t": round(t), "st": st,
                          "tstat": tstat, "warm": self._stint_lap <= TYRE_WARMUP_LAPS})
        return {"tyres": tyres, "worst": worst, "horizon": horizon, "flat": flat}

    # ---------------- detector de crossover lluvia -> lisos ----------------
    def _is_wet_compound(self):
        """Heuristica multilenguaje: el compuesto montado es de agua?"""
        c = (self._compound or b"").lower()
        return any(k in c for k in (b"wet", b"rain", b"lluv", b"inter",
                                    b"mojad", b"weather", b"pluie", b"regen"))

    @staticmethod
    def _slope(xs):
        """Pendiente por muestra (minimos cuadrados). None si <2 puntos."""
        n = len(xs)
        if n < 2:
            return None
        mx = (n - 1) / 2.0
        mean = sum(xs) / n
        num = sum((i - mx) * (xs[i] - mean) for i in range(n))
        den = sum((i - mx) ** 2 for i in range(n))
        return num / den if den > EPS else 0.0

    def _crossover(self, d, in_race):
        """Cruce lluvia->lisos en pista que seca. Solo carrera + compuesto de agua.

        AMS2 no expone wetness de pista -> se infiere de 3 proxies: (A) lluvia baja
        y cayendo, (B) pista calentando, (C) gomas de lluvia recalentando (la mas
        confiable: la wet se enfria con agua). Semaforo con histeresis ASIMETRICA
        (subir libre, bajar solo si el secado realmente cede) porque el costo de
        cambiar tarde (~seg/vuelta) supera al de adelantarse (1 out-lap frio).
        Devuelve None cuando no aplica -> el dash oculta el panel.
        """
        if not in_race or not self._is_wet_compound():
            return None
        rain = d.mRainDensity
        track_t = d.mTrackTemperature
        wet_t = max(d.mTyreTemp[i] for i in range(4))
        base = {"rain": round(rain, 3), "track_t": round(track_t, 1), "wet_temp": round(wet_t)}
        if len(self._rain_hist) < 3:            # aun sin tendencia confiable
            base.update(state="calibrando", n_signals=0, signals=[])
            return base

        rain_sl = self._slope(self._rain_hist)
        track_sl = self._slope(self._track_hist)
        wet_sl = self._slope(self._wet_temp_hist)
        A = rain < RAIN_DRY_THR and rain_sl is not None and rain_sl < 0
        B = track_sl is not None and track_sl >= TRACK_WARM_SLOPE
        C = wet_t >= WET_OVERHEAT and wet_sl is not None and wet_sl > 0
        n = int(A) + int(B) + int(C)
        # lap-stall: ya no bajas tiempos pese a pista mejor -> refuerza el ROJO
        lt_sl = self._slope(self._lap_times) if len(self._lap_times) >= 3 else None
        D = lt_sl is not None and lt_sl >= -0.05

        # objetivo crudo desde las senales
        if C and rain < RAIN_DRY_THR * 0.6 and D:
            tgt = "red"
        elif n >= 2:
            tgt = "amber"
        elif n == 1:
            tgt = "amber" if C else "green"     # 1 sola senal: solo C (wets) pesa
        else:
            tgt = "green"
        # histeresis: subir libre; bajar solo si las senales realmente cedieron
        order = {"green": 0, "amber": 1, "red": 2}
        prev = self._cross_state
        state = tgt if order[tgt] >= order[prev] else (tgt if (n == 0 and not C) else prev)
        self._cross_state = state

        active = []
        if A: active.append("lluvia baja y cayendo")
        if B: active.append("pista calentando")
        if C: active.append("gomas de lluvia recalentando")
        if D: active.append("tiempos planos")
        base.update(state=state, n_signals=n, signals=active,
                    rain_slope=round(rain_sl, 4),
                    track_slope=round(track_sl, 3) if track_sl is not None else None,
                    wet_slope=round(wet_sl, 2) if wet_sl is not None else None)
        return base

    def _project(self, d, p, live):
        out = {"live": bool(live), "mode": "none", "calibrating": True}
        out["session"] = _SESSION_LABEL.get(d.mSessionState, "sesión")

        cap = self._cap
        cur_fuel = d.mFuelLevel * cap
        fpl = _mean(self._fuel_per_lap)
        avg_lap = _median(self._lap_times)
        cur_lap = p.mCurrentLap
        laps_done = p.mLapsCompleted
        n_green = len(self._fuel_per_lap)
        additional = max(0, d.mSessionAdditionalLaps)

        out["cap_l"] = round(cap, 1)
        out["fuel_cur_l"] = round(cur_fuel, 1)
        out["green_laps"] = n_green
        out["has_plan"] = self._race_override is not None
        out["all_laps"] = self._use_all_laps

        # Calibrando: faltan datos MEDIDOS (consumo / lap-time).
        if fpl is None or avg_lap is None or n_green < 2:
            return out
        out["calibrating"] = False
        out["fuel_per_lap"] = round(fpl, 2)
        out["avg_lap_s"] = round(avg_lap, 2)

        # Bloque de neumaticos (referencia comun a plan y a vivo).
        ty = self._tyres(d)
        out["tyres"] = ty["tyres"]
        out["worst"] = ty["worst"]
        out["tyre_flat"] = bool(ty["flat"])
        out["tyre_horizon"] = None if ty["horizon"] == INF else round(ty["horizon"], 1)
        out["compound"] = self._compound.split(b"\x00")[0].decode("utf-8", "replace")

        # --- fuente del plan: en vivo (carrera) vs estimado (formato manual) ---
        in_race = (d.mSessionState == SESSION_RACE)
        use_live = in_race and (d.mLapsInEvent > 0 or
                                (d.mEventTimeRemaining is not None and d.mEventTimeRemaining >= 0))
        if self._race_override and not use_live:
            return self._plan_payload(out, fpl, avg_lap, cap, ty["horizon"])

        # --- modo de carrera EN VIVO (robusto) ---
        if d.mLapsInEvent > 0:
            mode = "laps"
            laps_total = d.mLapsInEvent
            laps_rem = max(0, d.mLapsInEvent - laps_done)
        elif d.mEventTimeRemaining is not None and d.mEventTimeRemaining >= 0:
            mode = "timed"
            laps_total = None
            scale = self._time_scale if self._time_scale else 1.0
            time_s = d.mEventTimeRemaining * scale
            t_cur = d.mCurrentTime if (0 <= d.mCurrentTime < avg_lap) else 0.0
            to_finish_cur = max(0.0, avg_lap - t_cur)
            rem_after = time_s - to_finish_cur
            laps_rem = (1 + additional) if rem_after <= 0 \
                else (1 + math.ceil(rem_after / avg_lap) + additional)
            laps_rem = max(1, laps_rem)
            out["time_left_s"] = round(time_s)
        else:
            mode = "none"
            laps_total = None
            laps_rem = None
        out["mode"] = mode
        out["laps_total"] = laps_total
        out["laps_remaining"] = laps_rem
        out["cur_lap"] = cur_lap

        # Practica sin formato configurado -> mostramos consumo medido, sin proyeccion.
        if laps_rem is None:
            out["no_race"] = True
            out["alerts"] = []
            return out

        # --- combustible al final + semaforo por margen en VUELTAS ---
        timed = (mode == "timed")
        safety_laps = SAFETY_LAPS_TIMED if timed else SAFETY_LAPS_LAPS
        fuel_to_finish = laps_rem * fpl
        fuel_margin = max(safety_laps * fpl, MARGIN_PCT * fuel_to_finish)
        fuel_needed = fuel_to_finish + fuel_margin
        fuel_at_end = cur_fuel - fuel_to_finish
        margin_laps = fuel_at_end / fpl if fpl > EPS else 0.0
        out["fuel_at_end_l"] = round(fuel_at_end, 1)
        out["margin_laps"] = round(margin_laps, 1)
        out["fuel_status"] = ("green" if margin_laps >= 2.0
                              else "amber" if margin_laps >= SAFETY_LAPS_FLOOR else "red")
        # Vueltas de combustible FISICO en el estanque AHORA (sin margen): es lo que
        # define una alarma real de "te quedas sin nafta", NO el margen al final
        # (que es negativo a proposito cuando vas a parar a repostar).
        out["tank_laps"] = round(cur_fuel / fpl, 1) if fpl > EPS else None

        # --- fuel-to-save (cuando hay deficit) ---
        deficit = fuel_needed - cur_fuel
        if deficit > 0 and laps_rem > 0:
            save = deficit / laps_rem
            out["save_per_lap"] = round(save, 2)
            out["save_level"] = ("easy" if save <= 0.15
                                 else "hard" if save <= 0.40 else "impossible")
        else:
            out["save_per_lap"] = None
            out["save_level"] = None

        # --- horizontes de stint: combustible vs neumatico ---
        fuel_laps_now = max(0.0, (cur_fuel - safety_laps * fpl) / fpl) if fpl > EPS else 0.0
        stint_max_fuel = max(1, math.floor(cap / fpl) - 1)
        tyre_horizon = ty["horizon"]

        if tyre_horizon < fuel_laps_now:
            out["limiter"] = "tyre"
            out["limiter_laps"] = round(tyre_horizon, 1)
        else:
            out["limiter"] = "fuel"
            out["limiter_laps"] = round(fuel_laps_now, 1)

        # --- numero de paradas (incremental: primer stint con lo que hay) ---
        stint_first = min(fuel_laps_now, tyre_horizon)
        stint_next = min(stint_max_fuel, tyre_horizon)
        if laps_rem <= stint_first + EPS:
            stops = 0
        else:
            per = stint_next if stint_next != INF else stint_max_fuel
            stops = math.ceil((laps_rem - stint_first) / per) if per > EPS else 1
        enforced = d.mEnforcedPitStopLap
        enforced_lap = enforced if enforced is not None and enforced >= 1 else None
        if enforced_lap is not None and not self._has_pitted:
            stops = max(stops, 1)
        out["stops_min"] = int(stops)
        out["enforced_lap"] = enforced_lap
        out["has_pitted"] = bool(self._has_pitted)

        # --- ventana de la proxima parada ---
        if stops >= 1:
            mn = min(fuel_laps_now, tyre_horizon)
            latest_off = math.floor(mn if mn != INF else fuel_laps_now)
            latest = cur_lap + max(0, latest_off)
            if enforced_lap is not None and not self._has_pitted:
                latest = min(latest, enforced_lap)
            out["pit_from"] = int(max(cur_lap + 1, latest - PIT_WINDOW))
            out["pit_to"] = int(latest)
        else:
            out["pit_from"] = None
            out["pit_to"] = None

        # --- litros a cargar en la proxima parada ---
        if stops >= 1 and out["pit_to"]:
            laps_until_pit = max(0, out["pit_to"] - cur_lap)
            laps_after = max(0, laps_rem - laps_until_pit)
            after_need = (max(0, laps_after - 1) * fpl + fpl * OUTLAP_FACTOR
                          + safety_laps * fpl) if laps_after > 0 else 0.0
            fuel_at_entry = max(0.0, cur_fuel - laps_until_pit * fpl)
            to_add = max(0.0, after_need - fuel_at_entry)
            headroom = cap - fuel_at_entry
            out["need_two"] = to_add > headroom + EPS
            out["add_l"] = round(min(to_add, headroom), 0)
        else:
            out["add_l"] = None
            out["need_two"] = False

        # deriva de lap-time dentro del stint (descontando aligeramiento por menos peso)
        if len(self._stint_lap_times) >= 3:
            xs = self._stint_lap_times
            n = len(xs)
            mx = (n - 1) / 2.0
            mean = _mean(xs)
            num = sum((i - mx) * (xs[i] - mean) for i in range(n))
            den = sum((i - mx) ** 2 for i in range(n))
            out["pace_drift"] = round((num / den if den > EPS else 0.0) + 0.04, 2)
        else:
            out["pace_drift"] = None

        # --- detector de crossover lluvia->lisos (pista que seca) ---
        out["crossover"] = self._crossover(d, in_race)

        # --- alertas TTS: solo en la carrera en vivo (no en practica) ---
        out["alerts"] = self._alerts(out, cur_lap) if in_race else []
        return out

    def _plan_payload(self, out, fpl, avg_lap, cap, tyre_horizon):
        """Plan ESTIMADO de carrera (practica/quali) desde el formato manual."""
        ov = self._race_override
        add = int(ov.get("additional", 0))
        if ov["mode"] == "laps":
            race_total = int(ov["value"]) + add
            out["mode"] = "laps"
            safety = SAFETY_LAPS_LAPS
        else:
            race_total = math.ceil(ov["value"] * 60.0 / avg_lap) + add
            out["mode"] = "timed"
            out["race_minutes"] = ov["value"]
            safety = SAFETY_LAPS_TIMED
        out["planning"] = True
        out["race_total_laps"] = int(race_total)
        margin = max(safety * fpl, MARGIN_PCT * race_total * fpl)
        total_fuel = race_total * fpl + margin
        out["race_fuel_total_l"] = round(total_fuel, 1)
        out["race_start_fuel_l"] = round(min(cap, total_fuel), 1)
        stint_full = max(1, math.floor(cap / fpl) - 1)
        if tyre_horizon != INF:
            stint_full = min(stint_full, max(1, math.floor(tyre_horizon)))
        out["stint_full_laps"] = int(stint_full)
        out["race_stops"] = int(max(0, math.ceil(race_total / stint_full) - 1))
        out["alerts"] = []
        return out

    def _alerts(self, o, cur_lap):
        """Alertas para voz. El front las dispara por FLANCO (una vez al aparecer).

        Clave: no confundir 'no llego al final sin parar' (normal en carrera con
        paradas -> NO es alarma) con 'me quedo sin nafta en el estanque AHORA'.
        """
        a = []
        stops = o.get("stops_min", 0)

        # Ahorro para LLEGAR sin parar: solo tiene sentido si NO hay parada
        # planificada y el deficit es alcanzable (si vas a parar igual, no aplica).
        if stops == 0 and o.get("save_per_lap") and o.get("save_level") in ("easy", "hard"):
            lvl = 2 if o.get("save_level") == "hard" else 1
            a.append({"key": "save", "level": lvl,
                      "say": f"Ahorrá {o['save_per_lap']:.2f} litros por vuelta para llegar"})

        # Combustible FISICO bajo en el estanque (menos de ~1 vuelta): urgente y real.
        tl = o.get("tank_laps")
        if tl is not None and tl < 1.0:
            a.append({"key": "fumes", "level": 3,
                      "say": "Menos de una vuelta de combustible, entrá a boxes"})

        # Pit obligatorio acercandose.
        en = o.get("enforced_lap")
        if en and not o.get("has_pitted") and 0 <= (en - cur_lap) <= 3:
            a.append({"key": "enforced", "level": 3,
                      "say": f"Pit obligatorio en {en - cur_lap} vueltas"})

        # Ventana de pit abierta (recordatorio de cuanto cargar).
        pf, pt = o.get("pit_from"), o.get("pit_to")
        if pf and pt and pf <= cur_lap <= pt:
            add = o.get("add_l")
            txt = f"Ventana de pit abierta, cargá {int(add)} litros" if add else "Ventana de pit abierta"
            a.append({"key": "window", "level": 2, "say": txt})

        # Cruce lluvia->lisos en pista que seca: el error caro es quedarse tarde.
        cx = o.get("crossover")
        if cx and cx.get("state") == "red":
            a.append({"key": "cross_red", "level": 3,
                      "say": "Pista secándose, los lisos ya van más rápido, entrá a boxes"})
        elif cx and cx.get("state") == "amber":
            a.append({"key": "cross_amber", "level": 2,
                      "say": "Ventana de lisos abierta, preparate para cambiar a boxes"})
        return a

    def payload(self):
        return self._last
