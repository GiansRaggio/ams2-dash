#!/usr/bin/env python3
"""Tests del guardrail fisico de dampers (rubrica C3): con bottoming, NUNCA ablandar bump.

Antes el consejo de clicks (_recommend) no miraba el recorrido de suspension, asi que podia
recomendar ablandar bump con la suspension tocando fondo -> consejo peligroso. Aca verificamos
que el guardrail lo impide y que SIN bottoming el comportamiento previo se conserva (zero-regression).
Correr: python tools/test_dampers.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ams2_dampers as D


def _ok(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")
    return cond


def corner(name, sb, fb, sr, fr, med=20, samples=1000, tbottom=0):
    return {"name": name, "samples": samples, "pctSB": sb, "pctFB": fb,
            "pctSR": sr, "pctFR": fr, "medAbs": med, "tBottom": tbottom, "tTop": 0,
            "hist": []}


def front_line(corners):
    recs = D.DamperAnalyzer._recommend(corners)
    return next((r for r in recs if r.startswith("DELANTERO")), "")


def main():
    nodata = [corner("RL", 0, 0, 0, 0, samples=0), corner("RR", 0, 0, 0, 0, samples=0)]
    print("test guardrail bottoming -> no ablandar bump (C3):")

    # SIN bottoming: aS=20 (ablanda slow bump) + high=30 (ablanda fast bump) = comportamiento previo
    line = front_line([corner("FL", 40, 15, 20, 15), corner("FR", 40, 15, 20, 15)] + nodata)
    _ok("sin bottoming: ablanda slow bump (regresion cero)", "slow bump -" in line, line)
    _ok("sin bottoming: ablanda fast bump por alta vel", "fast bump -" in line, line)
    _ok("sin bottoming: sin nota de bottoming", "BOTTOMING" not in line)

    # CON bottoming (tBottom=15): el guard suprime el ablandamiento de bump
    lineb = front_line([corner("FL", 40, 15, 20, 15, tbottom=15),
                        corner("FR", 40, 15, 20, 15, tbottom=15)] + nodata)
    _ok("con bottoming: NO ablanda slow bump", "slow bump -" not in lineb, lineb)
    _ok("con bottoming: NO ablanda fast bump", "fast bump -" not in lineb, lineb)
    _ok("con bottoming: avisa BOTTOMING", "BOTTOMING" in lineb)
    _ok("con bottoming: NO fuerza stiffen numerico (correccion va al texto)",
        "fast bump +" not in lineb and "fast bump -" not in lineb, lineb)

    # bottoming SIN balance que ablande bump: igual avisa, no inventa cambios peligrosos
    linec = front_line([corner("FL", 25, 10, 25, 10, tbottom=12),
                        corner("FR", 25, 10, 25, 10, tbottom=12)] + nodata)
    _ok("bottoming balanceado: avisa pero no ablanda bump",
        "BOTTOMING" in linec and "slow bump -" not in linec and "fast bump -" not in linec, linec)

    # bottoming de UNA sola rueda del eje: el guard usa el peor (max), no el promedio
    lined = front_line([corner("FL", 40, 15, 20, 15, tbottom=16),
                        corner("FR", 40, 15, 20, 15, tbottom=0)] + nodata)
    _ok("bottoming de 1 rueda dispara el guard (max, no avg)",
        "BOTTOMING" in lined and "slow bump -" not in lined, lined)

    # umbral del guard (5%) mas bajo que el flag de resortes (8%): bottoming leve igual bloquea
    linee = front_line([corner("FL", 40, 15, 20, 15, tbottom=6),
                        corner("FR", 40, 15, 20, 15, tbottom=6)] + nodata)
    _ok("bottoming leve 6% (>=5): bloquea soften de bump",
        "slow bump -" not in linee and "BOTTOMING" in linee, linee)
    linef = front_line([corner("FL", 40, 15, 20, 15, tbottom=4),
                        corner("FR", 40, 15, 20, 15, tbottom=4)] + nodata)
    _ok("bottoming 4% (<5): comportamiento normal, ablanda",
        "slow bump -" in linef and "BOTTOMING" not in linef, linef)

    print("test ancla al jugador en multiplayer (no a la camara):")

    class P:
        def __init__(self, name, lap):
            self.mName = name
            self.mCurrentLap = lap
            self.mIsActive = True

    class Snap:
        """Camara mirando al rival (viewed=0, vuelta 9); el jugador va en [2], vuelta 3."""
        mGameState = D._LIVE[0]
        mViewedParticipantIndex = 0
        mNumParticipants = 3
        mTrackLocation = b"Ibarra"
        mSpeed = 30.0
        mLastLapTime = 90.0
        mLapInvalidated = False
        mSuspensionVelocity = [0.01] * 4
        mSuspensionTravel = [0.05] * 4
        mParticipantInfo = ([P(b"Rival Uno", 9), P(b"Rival Dos", 7), P(b"Player", 3)]
                            + [P(b"", 0)] * 61)

    an = D.DamperAnalyzer()
    an._player_name = "player"                     # como si player.txt dijera "Player"
    an._ingest(Snap())
    _ok("cuenta vueltas del JUGADOR (3), no de la camara (9)", an._last_lap == 3, an._last_lap)
    _ok("acumula muestras del snapshot", an._cur_n == 1, an._cur_n)

    print("done.")


if __name__ == "__main__":
    main()
