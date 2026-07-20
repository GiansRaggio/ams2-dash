#!/usr/bin/env python3
"""Analizador de gomas para AMS2 (sincronico, alimentado del bridge).

Cubre lo que el bloque de gomas del director de estrategia NO cubre: temperatura por
ZONAS (interior/medio/exterior), presion en caliente y el diferencial contra la presion
objetivo. El desgaste NO se recalcula aca: se recibe ya resuelto desde StrategyEngine
(que detecta si mTyreWear crece o decrece con el uso) para no tener dos verdades.

CANALES: todo lo de aca se midio en vivo con tools/tyre_probe.py (Mercedes-AMG GT4 en
Snetterton, lisos, 2807 frames @187Hz). Lo que se aprendio y por que importa:

1. mAirPressure viene en Bar x100  -> /100 = bar. (165.2 crudo = 1.65 bar = 24.0 psi)

2. mTyreCarcassTemp viene en KELVIN -> -273.15 = C. Y es EL canal termico bueno:
   leyo 75-93 C, justo en la ventana operativa 70-100. En cambio mTyreTemp (bulk) leyo
   39-47 C en el mismo momento, o sea SIEMPRE daria "frio" contra esa ventana. Por eso
   el veredicto termico de aca usa CARCASA, no bulk.

3. mTyreTempLeft/Center/Right SI se pueblan y son usables, pero con dos ojos:

   a) L y R estan en marco ABSOLUTO del auto (izquierda/derecha de la pista), no
      relativo a la rueda. Se dedujo del dato: el borde interior salio mas caliente en
      las 4 esquinas de forma ESPEJADA (FL caliente a la derecha, FR caliente a la
      izquierda, idem atras) -- firma clasica de camber negativo, y solo cuadra si L/R
      son absolutos. De ahi el mapeo INNER_IS_RIGHT de abajo.

   b) mTyreTempCenter es BYTE-IDENTICO a mTyreTemp (bulk) en las 4 esquinas. O sea el
      "centro" NO es una tercera medicion independiente del piso de la goma. Por eso
      aca NO se deriva presion del perfil termico centro-vs-hombros (el clasico
      "centro caliente = sobreinflado"): ese diagnostico necesita un centro real.
      Ademas el offset centro-vs-hombros salio sospechosamente uniforme (-2.6/-2.7/
      -2.8/-2.75 C en las 4 esquinas), que huele a artefacto y no a fisica.
      -> El diferencial de presion sale del OBJETIVO configurable, que es honesto.
      -> Las 3 zonas se muestran igual (el piloto las lee), y el spread interior-exterior
         SI se usa: ese si es una medicion real de distribucion lateral -> camber.

Referencia del caveat: SimHub #632 (inner/outer cruzados / center = surface temp).

No corre hilo propio: el bridge llama update(d, wear) por frame y payload() al emitir.
"""
import json
import math
import os

CORNERS = ("FL", "FR", "RL", "RR")
BAR_TO_PSI = 14.5038
KELVIN = 273.15

# FL(0) y RL(2) son ruedas IZQUIERDAS -> su borde interior es el canal "Right".
# FR(1) y RR(3) son derechas -> su borde interior es el canal "Left". Ver nota 3a.
INNER_IS_RIGHT = (True, False, True, False)

MIN_SPEED = 15.0         # km/h: bajo esto no es lectura "en marcha"
EMA = 0.06               # suavizado de presion/temps (la presion oscila con la carga)

CARCASS_COLD = 70.0      # ventana operativa medida en carcasa (C)
CARCASS_HOT = 100.0
WARM_MIN = 60.0          # bajo esto la lectura de presion no sirve para decidir setup

CAMBER_OK_LO = 3.0       # spread interior-exterior (C) sano para lisos
CAMBER_OK_HI = 12.0

DEFAULT_TARGET_BAR = 1.75   # punto de partida GT3/GT4 lisos; el piloto lo ajusta
PRESS_TOL = 0.03            # +-bar que se considera "en objetivo"
TARGETS_FILE = "tyre_targets.json"


def _s(buf):
    return bytes(buf).split(b"\x00")[0].decode("utf-8", "replace")


class TyreAnalyzer:
    """Estado de gomas por esquina + diferencial contra la presion objetivo."""

    def __init__(self, base_dir=None):
        self._dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self._targets = self._load_targets()
        self._car = ""
        self._compound = ""
        self._live = False
        self._press = [None] * 4      # bar (EMA)
        self._t_in = [None] * 4       # C, borde interior (EMA)
        self._t_mid = [None] * 4      # C, "centro" (= bulk, ver nota 3b)
        self._t_out = [None] * 4      # C, borde exterior (EMA)
        self._carcass = [None] * 4    # C (EMA)
        self._brake = [None] * 4      # C (EMA)
        self._wear = [None] * 4       # 0..1, resuelto por StrategyEngine

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

    def reset(self):
        """Olvida las lecturas suavizadas (no toca los objetivos guardados)."""
        for a in (self._press, self._t_in, self._t_mid, self._t_out,
                  self._carcass, self._brake, self._wear):
            for i in range(4):
                a[i] = None

    # ---------------- ingesta ----------------
    def update(self, d, wear=None):
        """Un snapshot de SHM. `wear` = vector 0..1 ya resuelto por StrategyEngine."""
        if d is None:
            self._live = False
            return
        car = _s(d.mCarName)
        if car and car != self._car:        # cambio de auto -> las lecturas viejas no aplican
            self._car = car
            self.reset()
        self._compound = _s(d.mTyreCompound[0])
        self._live = (d.mSpeed * 3.6) >= MIN_SPEED

        for c in range(4):
            # Lecturas absolutas: validas tambien parado (reflejan enfriamiento). Cada
            # canal por separado: un NaN puntual en uno no descarta los otros.
            self._ema(self._press, c, d.mAirPressure[c] / 100.0)
            self._ema(self._carcass, c, d.mTyreCarcassTemp[c] - KELVIN)
            self._ema(self._brake, c, d.mBrakeTempCelsius[c])
            self._ema(self._t_mid, c, d.mTyreTempCenter[c])
            left, right = d.mTyreTempLeft[c], d.mTyreTempRight[c]
            inner, outer = (right, left) if INNER_IS_RIGHT[c] else (left, right)
            self._ema(self._t_in, c, inner)
            self._ema(self._t_out, c, outer)
            if wear is not None:
                try:
                    w = float(wear[c])
                    if math.isfinite(w):
                        self._wear[c] = min(max(w, 0.0), 1.0)
                except (TypeError, ValueError, IndexError):
                    pass

    @staticmethod
    def _ema(arr, c, v):
        if not math.isfinite(v):
            return
        arr[c] = v if arr[c] is None else arr[c] + EMA * (v - arr[c])

    # ---------------- salida ----------------
    def payload(self):
        target = self.target()
        corners = []
        for c in range(4):
            p, car = self._press[c], self._carcass[c]
            ti, to = self._t_in[c], self._t_out[c]
            spread = (ti - to) if (ti is not None and to is not None) else None
            corners.append({
                "name": CORNERS[c],
                "press": round(p, 2) if p is not None else None,
                "psi": round(p * BAR_TO_PSI, 1) if p is not None else None,
                "delta": round(target - p, 2) if p is not None else None,
                "pstat": self._pstat(p, target),
                "t_in": round(ti) if ti is not None else None,
                "t_mid": round(self._t_mid[c]) if self._t_mid[c] is not None else None,
                "t_out": round(to) if to is not None else None,
                "spread": round(spread, 1) if spread is not None else None,
                "camber": self._camber_hint(spread),
                "carcass": round(car) if car is not None else None,
                "tstat": self._tstat(car),
                "wear": round(self._wear[c] * 100, 1) if self._wear[c] is not None else None,
                "brake": round(self._brake[c]) if self._brake[c] is not None else None,
            })
        warm = [t for t in self._carcass if t is not None]
        return {
            "live": self._live,
            "car": self._car,
            "compound": self._compound,
            "target": round(target, 2),
            # La presion solo sirve para decidir setup con la goma en temperatura.
            "warm": bool(warm) and (sum(warm) / len(warm)) >= WARM_MIN,
            "corners": corners,
            "axle": self._axle(),
        }

    @staticmethod
    def _pstat(p, target):
        if p is None:
            return None
        d = p - target
        if abs(d) <= PRESS_TOL:
            return "ok"
        return "high" if d > 0 else "low"

    @staticmethod
    def _tstat(carcass):
        if carcass is None:
            return None
        if carcass >= CARCASS_HOT:
            return "hot"
        return "cold" if carcass < CARCASS_COLD else "ok"

    @staticmethod
    def _camber_hint(spread):
        """Distribucion lateral interior-vs-exterior. Medicion real (a diferencia del
        centro), asi que aca si se opina: mucho spread = exceso de camber negativo."""
        if spread is None:
            return None
        if spread > CAMBER_OK_HI:
            return "mucho camber neg."
        if spread < 0:
            return "falta camber neg."
        if spread < CAMBER_OK_LO:
            return "poco camber neg."
        return "ok"

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
