#!/usr/bin/env python3
"""Analizador de dampers de AMS2 a partir de mSuspensionVelocity (shared memory).

Muestrea la velocidad de amortiguador de las 4 esquinas a alta tasa (~166 Hz en
sesion), arma histogramas por esquina (vuelta en curso + acumulado multi-vuelta)
y calcula metricas + recomendaciones heuristicas para ajuste de dampers.

Datos verificados en pista (damper_probe.py):
- mSuspensionVelocity viene en m/s  -> se convierte a mm/s (x1000).
- Signo: + = compresion (bump), - = extension (rebound). (mSuspensionTravel es
  positivo y la velocidad es su derivada -> velocidad positiva = comprimiendo.)

Corre su propio hilo de muestreo con su propio Reader (independiente del bridge).
Solo Windows. Las recomendaciones son HEURISTICAS (punto de partida, no receta).
"""
import threading
import time

import ams2_shm

CORNERS = ("FL", "FR", "RL", "RR")

# Histograma: bins de 25 mm/s de -400 a +400 (fuera de rango cae en los extremos).
BIN_W = 25                      # mm/s por bin
BIN_MAX = 400                   # mm/s (borde)
N_BINS = (2 * BIN_MAX) // BIN_W  # 32
LOW_HIGH = 50                   # mm/s: umbral low-speed / high-speed
BOTTOM_GUARD_PCT = 5            # % de travel en tope para gatillar el guardrail anti-soften de bump.
                                # Mas bajo que el flag de bottoming de _spring_recommend (8%) a proposito:
                                # no sugerir un click cuesta menos que recomendar algo peligroso (C3).
CENTERS = [(-BIN_MAX + BIN_W * (i + 0.5)) for i in range(N_BINS)]

SAMPLE_SLEEP = 0.002            # espera entre sondeos con mismo seq
MAX_LAPS = 12                   # cuantas vueltas completas recordamos
MIN_LAP_SAMPLES = 300           # menos que esto = vuelta demasiado corta (out-lap)

_LIVE = (ams2_shm.GAME_INGAME_PLAYING, ams2_shm.GAME_INGAME_INMENU_TIME_TICKING)


def _bin_index(v_mmps):
    idx = int((v_mmps + BIN_MAX) // BIN_W)
    return 0 if idx < 0 else (N_BINS - 1 if idx >= N_BINS else idx)


def _zeros():
    return [[0] * N_BINS for _ in range(4)]


# Histograma de recorrido (mSuspensionTravel): bins de 5 mm de 0 a 120 mm.
TBIN_W = 5                      # mm por bin
TBIN_MAX = 120                  # mm (tope del histograma)
TN_BINS = TBIN_MAX // TBIN_W    # 24


def _travel_bin(t_mm):
    idx = int(t_mm // TBIN_W)
    return 0 if idx < 0 else (TN_BINS - 1 if idx >= TN_BINS else idx)


def _tzeros():
    return [[0] * TN_BINS for _ in range(4)]


class DamperAnalyzer:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = False
        self._thread = None
        self._cur = _zeros()          # histograma de velocidad, vuelta en curso
        self._acc = _zeros()          # acumulado de velocidad (vueltas validas)
        self._cur_t = _tzeros()       # histograma de recorrido (travel), vuelta en curso
        self._acc_t = _tzeros()       # acumulado de travel
        self._cur_n = 0
        self._acc_n = 0
        self._laps = []               # [{lap, time, valid, samples}]
        self._last_lap = -1
        self._last_track = None
        self._cur_invalid = False
        self._rate = 0.0

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop = True

    def reset(self):
        with self._lock:
            self._cur = _zeros()
            self._acc = _zeros()
            self._cur_t = _tzeros()
            self._acc_t = _tzeros()
            self._cur_n = 0
            self._acc_n = 0
            self._laps = []
            self._cur_invalid = False

    # ---------------- hilo de muestreo ----------------
    def _run(self):
        reader = None
        last_seq = -1
        frames = 0
        t0 = time.perf_counter()
        while not self._stop:
            if reader is None:
                try:
                    reader = ams2_shm.Reader().open()
                except ams2_shm.SharedMemoryUnavailable:
                    time.sleep(1.0)
                    continue
            d = reader.snapshot()
            seq = d.mSequenceNumber
            if seq == last_seq:
                time.sleep(SAMPLE_SLEEP)
            else:
                last_seq = seq
                frames += 1
                self._ingest(d)
            now = time.perf_counter()
            if now - t0 >= 1.0:
                with self._lock:
                    self._rate = frames / (now - t0)
                frames = 0
                t0 = now

    def _ingest(self, d):
        if d.mGameState not in _LIVE:
            return
        v = d.mViewedParticipantIndex
        if not (0 <= v < ams2_shm.STORED_PARTICIPANTS_MAX):
            return
        track = d.mTrackLocation            # bytes (recortado en NUL)
        lap = d.mParticipantInfo[v].mCurrentLap
        speed = d.mSpeed * 3.6
        invalid = bool(d.mLapInvalidated)

        with self._lock:
            if track != self._last_track:   # cambio de pista/sesion -> reset
                self._last_track = track
                self._cur = _zeros()
                self._acc = _zeros()
                self._cur_t = _tzeros()
                self._acc_t = _tzeros()
                self._cur_n = 0
                self._acc_n = 0
                self._laps = []
                self._cur_invalid = False
                self._last_lap = lap

            if self._last_lap >= 0 and lap > self._last_lap:     # cruzo meta
                valid = (not self._cur_invalid) and self._cur_n >= MIN_LAP_SAMPLES
                self._laps.append({
                    "lap": int(self._last_lap),
                    "time": d.mLastLapTime if d.mLastLapTime > 0 else None,
                    "valid": valid,
                    "samples": self._cur_n,
                })
                del self._laps[:-MAX_LAPS]
                if valid:
                    for c in range(4):
                        acc_c, cur_c = self._acc[c], self._cur[c]
                        for b in range(N_BINS):
                            acc_c[b] += cur_c[b]
                        acc_t, cur_t = self._acc_t[c], self._cur_t[c]
                        for b in range(TN_BINS):
                            acc_t[b] += cur_t[b]
                    self._acc_n += self._cur_n
                self._cur = _zeros()
                self._cur_t = _tzeros()
                self._cur_n = 0
                self._cur_invalid = False
            elif lap < self._last_lap:       # volvio a boxes / reinicio de vueltas
                self._cur = _zeros()
                self._cur_t = _tzeros()
                self._cur_n = 0
                self._cur_invalid = False
            self._last_lap = lap

            if speed < 5:                    # parado: no acumular
                return
            if invalid:
                self._cur_invalid = True
            for c in range(4):
                self._cur[c][_bin_index(d.mSuspensionVelocity[c] * 1000.0)] += 1
                self._cur_t[c][_travel_bin(d.mSuspensionTravel[c] * 1000.0)] += 1
            self._cur_n += 1

    # ---------------- salida para el frontend ----------------
    def payload(self):
        with self._lock:
            use_cur = self._acc_n == 0
            src = self._cur if use_cur else self._acc
            src_t = self._cur_t if use_cur else self._acc_t
            hist = [list(src[c]) for c in range(4)]
            thist = [list(src_t[c]) for c in range(4)]
            laps = list(self._laps)
            cur_n, acc_n, rate = self._cur_n, self._acc_n, self._rate
        corners = [self._corner_metrics(CORNERS[c], hist[c]) for c in range(4)]
        for c in range(4):
            corners[c].update(self._travel_metrics(thist[c]))
        return {
            "binW": BIN_W,
            "binMax": BIN_MAX,
            "lowHigh": LOW_HIGH,
            "centers": CENTERS,
            "corners": corners,
            "validLaps": sum(1 for l in laps if l["valid"]),
            "showingCurrent": use_cur,
            "curSamples": cur_n,
            "accSamples": acc_n,
            "rateHz": round(rate),
            "recommendations": self._recommend(corners),
            "springRecs": self._spring_recommend(corners),
        }

    @staticmethod
    def _corner_metrics(name, hist):
        total = sum(hist)
        if total == 0:
            return {"name": name, "hist": hist, "pctLow": 0, "pctHigh": 0,
                    "pctBump": 0, "pctRebound": 0, "medAbs": 0, "samples": 0,
                    "pctSB": 0, "pctFB": 0, "pctSR": 0, "pctFR": 0}
        # 4 cuadrantes: slow/fast x bump/rebound (umbral LOW_HIGH = 50 mm/s)
        sb = fb = sr = fr = 0
        for h, cc in zip(hist, CENTERS):
            if cc > 0:
                if cc <= LOW_HIGH: sb += h
                else: fb += h
            elif cc < 0:
                if cc >= -LOW_HIGH: sr += h
                else: fr += h
        bump, rebound, low = sb + fb, sr + fr, sb + sr
        # mediana de |velocidad| (ponderada por el histograma)
        order = sorted(zip((abs(cc) for cc in CENTERS), hist))
        cum, med = 0, 0
        for av, h in order:
            cum += h
            if cum >= total / 2:
                med = av
                break
        return {
            "name": name,
            "hist": hist,
            "pctLow": round(100 * low / total),
            "pctHigh": round(100 * (fb + fr) / total),
            "pctBump": round(100 * bump / total),
            "pctRebound": round(100 * rebound / total),
            "pctSB": round(100 * sb / total),
            "pctFB": round(100 * fb / total),
            "pctSR": round(100 * sr / total),
            "pctFR": round(100 * fr / total),
            "medAbs": round(med),
            "samples": total,
        }

    @staticmethod
    def _recommend(corners):
        """Deltas direccionales de clicks por eje (4-way: slow/fast x bump/rebound).

        Heuristica: se basa en SIMETRIA bump/rebound por banda de velocidad y en el
        contenido de alta velocidad. '-' = ablandar, '+' = endurecer. Son orientativos
        (iterar 1-2 clicks y re-medir), no el click exacto.
        """
        def clicks(dev):                 # % de desviacion -> clicks (1-3); deadband 8%
            return 0 if dev < 8 else max(1, min(3, round(dev / 8)))

        recs = ["Clicks orientativos (- = ablandar, + = endurecer). Iterar 1-2 por vez y "
                "re-medir; el histograma guia balance/simetria, no da el click exacto."]

        def axle(a, b, label):
            c1, c2 = corners[a], corners[b]
            if min(c1["samples"], c2["samples"]) == 0:
                return f"{label}: sin datos todavia."
            SB = (c1["pctSB"] + c2["pctSB"]) / 2
            FB = (c1["pctFB"] + c2["pctFB"]) / 2
            SR = (c1["pctSR"] + c2["pctSR"]) / 2
            FR = (c1["pctFR"] + c2["pctFR"]) / 2
            high = FB + FR
            bottom = max(c1.get("tBottom", 0), c2.get("tBottom", 0))   # peor neumatico del eje (seguridad)
            adj = {"slow bump": 0, "fast bump": 0, "slow reb": 0, "fast reb": 0}
            aS = SB - SR                      # balance bump/rebound de baja velocidad
            if clicks(abs(aS)):
                adj["slow bump" if aS > 0 else "slow reb"] -= clicks(abs(aS))
            aF = FB - FR                      # balance bump/rebound de alta velocidad
            if clicks(abs(aF)):
                adj["fast bump" if aF > 0 else "fast reb"] -= clicks(abs(aF))
            if high >= 28 and bottom < BOTTOM_GUARD_PCT:   # mucha alta velocidad (salvo bottoming)
                adj["fast bump"] -= max(1, min(3, round((high - 22) / 6)))
            # GUARDRAIL FISICO (rubrica C3): con bottoming NUNCA ablandar bump (agravaria el toque de
            # fondo). NO forzamos un endurecimiento numerico: el histograma de velocidad no separa el
            # bombeo de curva (donde endurecer fast bump es defensa correcta) de los impactos de piano
            # (donde endurecer es contraproducente) -> la correccion va como texto.
            note = ""
            if bottom >= BOTTOM_GUARD_PCT:
                adj["slow bump"] = max(0, adj["slow bump"])
                adj["fast bump"] = max(0, adj["fast bump"])
                note = (f" | BOTTOMING {bottom:.0f}%: NO ablandar bump -> subir rate/altura/packers "
                        "o endurecer fast bump; revisar tambien rebound (pack-down)")
            tips = [f"{k} {'+' if dv > 0 else ''}{dv}"
                    for k, dv in ((k, max(-3, min(3, v))) for k, v in adj.items()) if dv]
            body = " · ".join(tips) if tips else "balanceado, sin cambios"
            return f"{label} [SB{SB:.0f}/FB{FB:.0f}/SR{SR:.0f}/FR{FR:.0f}%]: {body}{note}"

        recs.append(axle(0, 1, "DELANTERO"))
        recs.append(axle(2, 3, "TRASERO"))
        for a, b, lbl in ((0, 1, "Delantero"), (2, 3, "Trasero")):
            ca, cb = corners[a], corners[b]
            if ca["samples"] and cb["samples"] and abs(ca["medAbs"] - cb["medAbs"]) > 12:
                hi, lo = (CORNERS[a], CORNERS[b]) if ca["medAbs"] > cb["medAbs"] else (CORNERS[b], CORNERS[a])
                recs.append(f"{lbl}: {hi} trabaja mas que {lo} (asimetria izq/der: peso/alturas/presiones)")
        return recs

    @staticmethod
    def _travel_metrics(th):
        """Metricas de recorrido de suspension (mm) a partir del histograma de travel."""
        total = sum(th)
        if total == 0:
            return {"tMin": 0, "tMax": 0, "tMed": 0, "tBottom": 0, "tTop": 0}
        occ = [i for i, v in enumerate(th) if v]
        lo, hi = occ[0], occ[-1]
        cum, med = 0, lo
        for i, v in enumerate(th):
            cum += v
            if cum >= total / 2:
                med = i
                break
        return {
            "tMin": lo * TBIN_W,
            "tMax": (hi + 1) * TBIN_W,
            "tMed": med * TBIN_W + TBIN_W // 2,
            "tBottom": round(100 * th[hi] / total),   # % en el bin superior ocupado (compresion)
            "tTop": round(100 * th[lo] / total),       # % en el bin inferior ocupado (extension)
        }

    @staticmethod
    def _spring_recommend(corners):
        """Guardrail de resortes: bottoming/topping y balance de recorrido (objetivo)."""
        recs = ["Travel = guardrail objetivo (no tocar fondo ni dejar la suspension muerta). "
                "El rate fino del resorte se afina por sensacion + tiempo + temps de goma."]

        def axle(a, b, label):
            c1, c2 = corners[a], corners[b]
            if min(c1["samples"], c2["samples"]) == 0:
                return f"{label}: sin datos todavia."
            bottom = (c1["tBottom"] + c2["tBottom"]) / 2
            top = (c1["tTop"] + c2["tTop"]) / 2
            tmin = min(c1["tMin"], c2["tMin"])
            tmax = max(c1["tMax"], c2["tMax"])
            tips = []
            if bottom >= 8:
                tips.append(f"posible bottoming ({bottom:.0f}% en tope de compresion) -> "
                            "subir rate o altura, o packers")
            if top >= 8:
                tips.append(f"rueda se descarga ({top:.0f}% en extension max) -> quiza muy duro / revisar altura")
            if not tips:
                tips.append("recorrido sano, sin bottoming/topping marcado")
            return f"{label} [travel {tmin:.0f}-{tmax:.0f}mm]: " + " · ".join(tips)

        recs.append(axle(0, 1, "DELANTERO"))
        recs.append(axle(2, 3, "TRASERO"))
        if all(corners[i]["samples"] for i in range(4)):
            fr = ((corners[0]["tMax"] - corners[0]["tMin"]) + (corners[1]["tMax"] - corners[1]["tMin"])) / 2
            re = ((corners[2]["tMax"] - corners[2]["tMin"]) + (corners[3]["tMax"] - corners[3]["tMin"])) / 2
            if abs(fr - re) >= 15:
                more = "delantero" if fr > re else "trasero"
                recs.append(f"Recorrido {more} mucho mayor (dif {abs(fr - re):.0f}mm): ese eje mas blando "
                            "-> afecta balance mecanico")
        return recs
