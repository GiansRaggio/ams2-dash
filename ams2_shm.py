#!/usr/bin/env python3
"""Lectura de la Shared Memory de Automobilista 2 (formato Project CARS 2, v14).

AMS2 publica un mapeo de memoria nombrado '$pcars2$' con la estructura
`SharedMemory` de Project CARS 2. Este modulo la mapea con ctypes (solo lectura)
y entrega snapshots consistentes.

Por que shared memory en vez de UDP: el UDP obliga al juego a serializar y emitir
un paquete por frame (causa stuttering). La shared memory el juego ya la escribe
siempre; nosotros solo la LEEMOS -> costo ~cero para el juego, sin stutter.

Layout verificado contra el header oficial (SHARED_MEMORY_VERSION 14) del proyecto
CREST2-AMS2. Todos los campos son <= 4 bytes => alineacion natural estandar (sin
#pragma pack), que es justo lo que reproduce ctypes.Structure por defecto.

Solo Windows (usa kernel32 OpenFileMapping / MapViewOfFile).
"""
import ctypes
from ctypes import wintypes

MAP_NAME = "$pcars2$"
SHARED_MEMORY_VERSION = 14
STORED_PARTICIPANTS_MAX = 64

# --- mGameState ---
GAME_EXITED = 0
GAME_FRONT_END = 1
GAME_INGAME_PLAYING = 2
GAME_INGAME_PAUSED = 3
GAME_INGAME_INMENU_TIME_TICKING = 4
GAME_INGAME_RESTARTING = 5
GAME_INGAME_REPLAY = 6
GAME_FRONT_END_REPLAY = 7

# --- bits de mCarFlags (mismo layout que el sCarFlags del protocolo UDP) ---
CAR_ENGINE_WARNING = 0x04
CAR_SPEED_LIMITER = 0x08
CAR_ABS = 0x10
CAR_TC = 0x40


class ParticipantInfo(ctypes.Structure):
    _fields_ = [
        ("mIsActive", ctypes.c_bool),
        ("mName", ctypes.c_char * 64),
        ("mWorldPosition", ctypes.c_float * 3),
        ("mCurrentLapDistance", ctypes.c_float),
        ("mRacePosition", ctypes.c_uint),
        ("mLapsCompleted", ctypes.c_uint),
        ("mCurrentLap", ctypes.c_uint),
        ("mCurrentSector", ctypes.c_int),
    ]


class SharedMemory(ctypes.Structure):
    _fields_ = [
        ("mVersion", ctypes.c_uint),
        ("mBuildVersionNumber", ctypes.c_uint),
        ("mGameState", ctypes.c_uint),
        ("mSessionState", ctypes.c_uint),
        ("mRaceState", ctypes.c_uint),
        ("mViewedParticipantIndex", ctypes.c_int),
        ("mNumParticipants", ctypes.c_int),
        ("mParticipantInfo", ParticipantInfo * 64),
        ("mUnfilteredThrottle", ctypes.c_float),
        ("mUnfilteredBrake", ctypes.c_float),
        ("mUnfilteredSteering", ctypes.c_float),
        ("mUnfilteredClutch", ctypes.c_float),
        ("mCarName", ctypes.c_char * 64),
        ("mCarClassName", ctypes.c_char * 64),
        ("mLapsInEvent", ctypes.c_uint),
        ("mTrackLocation", ctypes.c_char * 64),
        ("mTrackVariation", ctypes.c_char * 64),
        ("mTrackLength", ctypes.c_float),
        ("mNumSectors", ctypes.c_int),
        ("mLapInvalidated", ctypes.c_bool),
        ("mBestLapTime", ctypes.c_float),
        ("mLastLapTime", ctypes.c_float),
        ("mCurrentTime", ctypes.c_float),
        ("mSplitTimeAhead", ctypes.c_float),
        ("mSplitTimeBehind", ctypes.c_float),
        ("mSplitTime", ctypes.c_float),
        ("mEventTimeRemaining", ctypes.c_float),
        ("mPersonalFastestLapTime", ctypes.c_float),
        ("mWorldFastestLapTime", ctypes.c_float),
        ("mCurrentSector1Time", ctypes.c_float),
        ("mCurrentSector2Time", ctypes.c_float),
        ("mCurrentSector3Time", ctypes.c_float),
        ("mFastestSector1Time", ctypes.c_float),
        ("mFastestSector2Time", ctypes.c_float),
        ("mFastestSector3Time", ctypes.c_float),
        ("mPersonalFastestSector1Time", ctypes.c_float),
        ("mPersonalFastestSector2Time", ctypes.c_float),
        ("mPersonalFastestSector3Time", ctypes.c_float),
        ("mWorldFastestSector1Time", ctypes.c_float),
        ("mWorldFastestSector2Time", ctypes.c_float),
        ("mWorldFastestSector3Time", ctypes.c_float),
        ("mHighestFlagColour", ctypes.c_uint),
        ("mHighestFlagReason", ctypes.c_uint),
        ("mPitMode", ctypes.c_uint),
        ("mPitSchedule", ctypes.c_uint),
        ("mCarFlags", ctypes.c_uint),
        ("mOilTempCelsius", ctypes.c_float),
        ("mOilPressureKPa", ctypes.c_float),
        ("mWaterTempCelsius", ctypes.c_float),
        ("mWaterPressureKPa", ctypes.c_float),
        ("mFuelPressureKPa", ctypes.c_float),
        ("mFuelLevel", ctypes.c_float),
        ("mFuelCapacity", ctypes.c_float),
        ("mSpeed", ctypes.c_float),
        ("mRpm", ctypes.c_float),
        ("mMaxRPM", ctypes.c_float),
        ("mBrake", ctypes.c_float),
        ("mThrottle", ctypes.c_float),
        ("mClutch", ctypes.c_float),
        ("mSteering", ctypes.c_float),
        ("mGear", ctypes.c_int),
        ("mNumGears", ctypes.c_int),
        ("mOdometerKM", ctypes.c_float),
        ("mAntiLockActive", ctypes.c_bool),
        ("mLastOpponentCollisionIndex", ctypes.c_int),
        ("mLastOpponentCollisionMagnitude", ctypes.c_float),
        ("mBoostActive", ctypes.c_bool),
        ("mBoostAmount", ctypes.c_float),
        ("mOrientation", ctypes.c_float * 3),
        ("mLocalVelocity", ctypes.c_float * 3),
        ("mWorldVelocity", ctypes.c_float * 3),
        ("mAngularVelocity", ctypes.c_float * 3),
        ("mLocalAcceleration", ctypes.c_float * 3),
        ("mWorldAcceleration", ctypes.c_float * 3),
        ("mExtentsCentre", ctypes.c_float * 3),
        ("mTyreFlags", ctypes.c_uint * 4),
        ("mTerrain", ctypes.c_uint * 4),
        ("mTyreY", ctypes.c_float * 4),
        ("mTyreRPS", ctypes.c_float * 4),
        ("mTyreSlipSpeed", ctypes.c_float * 4),
        ("mTyreTemp", ctypes.c_float * 4),
        ("mTyreGrip", ctypes.c_float * 4),
        ("mTyreHeightAboveGround", ctypes.c_float * 4),
        ("mTyreLateralStiffness", ctypes.c_float * 4),
        ("mTyreWear", ctypes.c_float * 4),
        ("mBrakeDamage", ctypes.c_float * 4),
        ("mSuspensionDamage", ctypes.c_float * 4),
        ("mBrakeTempCelsius", ctypes.c_float * 4),
        ("mTyreTreadTemp", ctypes.c_float * 4),
        ("mTyreLayerTemp", ctypes.c_float * 4),
        ("mTyreCarcassTemp", ctypes.c_float * 4),
        ("mTyreRimTemp", ctypes.c_float * 4),
        ("mTyreInternalAirTemp", ctypes.c_float * 4),
        ("mCrashState", ctypes.c_uint),
        ("mAeroDamage", ctypes.c_float),
        ("mEngineDamage", ctypes.c_float),
        ("mAmbientTemperature", ctypes.c_float),
        ("mTrackTemperature", ctypes.c_float),
        ("mRainDensity", ctypes.c_float),
        ("mWindSpeed", ctypes.c_float),
        ("mWindDirectionX", ctypes.c_float),
        ("mWindDirectionY", ctypes.c_float),
        ("mCloudBrightness", ctypes.c_float),
        ("mSequenceNumber", ctypes.c_uint),
        ("mWheelLocalPositionY", ctypes.c_float * 4),
        ("mSuspensionTravel", ctypes.c_float * 4),
        ("mSuspensionVelocity", ctypes.c_float * 4),
        ("mAirPressure", ctypes.c_float * 4),
        ("mEngineSpeed", ctypes.c_float),
        ("mEngineTorque", ctypes.c_float),
        ("mWings", ctypes.c_float * 2),
        ("mHandBrake", ctypes.c_float),
        ("mCurrentSector1Times", ctypes.c_float * 64),
        ("mCurrentSector2Times", ctypes.c_float * 64),
        ("mCurrentSector3Times", ctypes.c_float * 64),
        ("mFastestSector1Times", ctypes.c_float * 64),
        ("mFastestSector2Times", ctypes.c_float * 64),
        ("mFastestSector3Times", ctypes.c_float * 64),
        ("mFastestLapTimes", ctypes.c_float * 64),
        ("mLastLapTimes", ctypes.c_float * 64),
        ("mLapsInvalidated", ctypes.c_bool * 64),
        ("mRaceStates", ctypes.c_uint * 64),
        ("mPitModes", ctypes.c_uint * 64),
        ("mOrientations", (ctypes.c_float * 3) * 64),
        ("mSpeeds", ctypes.c_float * 64),
        ("mCarNames", (ctypes.c_char * 64) * 64),
        ("mCarClassNames", (ctypes.c_char * 64) * 64),
        ("mEnforcedPitStopLap", ctypes.c_int),
        ("mTranslatedTrackLocation", ctypes.c_char * 64),
        ("mTranslatedTrackVariation", ctypes.c_char * 64),
        ("mBrakeBias", ctypes.c_float),
        ("mTurboBoostPressure", ctypes.c_float),
        ("mTyreCompound", (ctypes.c_char * 40) * 4),
        ("mPitSchedules", ctypes.c_uint * 64),
        ("mHighestFlagColours", ctypes.c_uint * 64),
        ("mHighestFlagReasons", ctypes.c_uint * 64),
        ("mNationalities", ctypes.c_uint * 64),
        ("mSnowDensity", ctypes.c_float),
        ("mSessionDuration", ctypes.c_float),
        ("mSessionAdditionalLaps", ctypes.c_int),
        ("mTyreTempLeft", ctypes.c_float * 4),
        ("mTyreTempCenter", ctypes.c_float * 4),
        ("mTyreTempRight", ctypes.c_float * 4),
        ("mDrsState", ctypes.c_uint),
        ("mRideHeight", ctypes.c_float * 4),
        ("mJoyPad0", ctypes.c_uint),
        ("mDPad", ctypes.c_uint),
        ("mAntiLockSetting", ctypes.c_int),
        ("mTractionControlSetting", ctypes.c_int),
        ("mErsDeploymentMode", ctypes.c_int),
        ("mErsAutoModeEnabled", ctypes.c_bool),
        ("mClutchTemp", ctypes.c_float),
        ("mClutchWear", ctypes.c_float),
        ("mClutchOverheated", ctypes.c_bool),
        ("mClutchSlipping", ctypes.c_bool),
        ("mYellowFlagState", ctypes.c_int),
        ("mSessionIsPrivate", ctypes.c_bool),
        ("mLaunchStage", ctypes.c_int),
    ]


_FILE_MAP_READ = 0x0004


class SharedMemoryUnavailable(Exception):
    """No se pudo abrir '$pcars2$' (AMS2 cerrado o Shared Memory desactivada)."""


class Reader:
    """Abre '$pcars2$' una vez y entrega snapshots consistentes y baratos."""

    def __init__(self):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenFileMappingW.restype = ctypes.c_void_p
        k.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.MapViewOfFile.restype = ctypes.c_void_p
        k.MapViewOfFile.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
        k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        self._k = k
        self._handle = None
        self._addr = None
        self._view = None
        self._buf = SharedMemory()
        self._size = ctypes.sizeof(SharedMemory)

    def open(self):
        h = self._k.OpenFileMappingW(_FILE_MAP_READ, False, MAP_NAME)
        if not h:
            raise SharedMemoryUnavailable(
                f"No existe el mapeo '{MAP_NAME}'. Abri AMS2 con "
                "Options -> System -> Shared Memory = Project CARS 2."
            )
        addr = self._k.MapViewOfFile(h, _FILE_MAP_READ, 0, 0, 0)
        if not addr:
            err = ctypes.get_last_error()
            self._k.CloseHandle(h)
            raise SharedMemoryUnavailable(f"MapViewOfFile fallo (err={err})")
        self._handle = h
        self._addr = addr
        self._view = ctypes.cast(addr, ctypes.POINTER(SharedMemory))
        return self

    def snapshot(self):
        """Copia consistente del bloque, protegida por mSequenceNumber.

        El juego incrementa mSequenceNumber antes y despues de cada escritura;
        si es impar, hay una escritura en curso. Reintenta unas pocas veces para
        no entregar un frame "roto" (mezcla de dos updates).
        """
        view = self._view
        buf = self._buf
        for _ in range(8):
            seq1 = view.contents.mSequenceNumber
            if seq1 & 1:                      # escritura en curso
                continue
            ctypes.memmove(ctypes.byref(buf), self._addr, self._size)
            if buf.mSequenceNumber == seq1:   # no cambio durante la copia
                return buf
        return buf  # mejor esfuerzo

    def close(self):
        if self._addr:
            self._k.UnmapViewOfFile(ctypes.c_void_p(self._addr))
            self._addr = None
        if self._handle:
            self._k.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None


def _s(b):
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else b


if __name__ == "__main__":
    # Sonda de validacion: vuelca campos clave en vivo para verificar offsets.
    import time
    print(f"sizeof(SharedMemory) = {ctypes.sizeof(SharedMemory)} bytes "
          f"(esperado mVersion={SHARED_MEMORY_VERSION})")
    r = Reader().open()
    try:
        for _ in range(4):
            d = r.snapshot()
            v = d.mViewedParticipantIndex
            p = d.mParticipantInfo[v] if 0 <= v < 64 else None
            print("-" * 64)
            print(f"mVersion={d.mVersion}  build={d.mBuildVersionNumber}  seq={d.mSequenceNumber}")
            print(f"gameState={d.mGameState} sessionState={d.mSessionState} raceState={d.mRaceState}")
            print(f"car='{_s(d.mCarName)}'  class='{_s(d.mCarClassName)}'  "
                  f"track='{_s(d.mTrackLocation)}' / '{_s(d.mTrackVariation)}'")
            print(f"speed={d.mSpeed * 3.6:6.1f} km/h   rpm={d.mRpm:6.0f}/{d.mMaxRPM:.0f}   "
                  f"gear={d.mGear}/{d.mNumGears}")
            print(f"thr={d.mUnfilteredThrottle * 100:3.0f}%  brk={d.mUnfilteredBrake * 100:3.0f}%   "
                  f"fuel={d.mFuelLevel * d.mFuelCapacity:5.1f} L  cap={d.mFuelCapacity:.1f} L")
            print(f"best={d.mBestLapTime:.3f}  last={d.mLastLapTime:.3f}  cur={d.mCurrentTime:.3f}  "
                  f"split a/b={d.mSplitTimeAhead:.3f}/{d.mSplitTimeBehind:.3f}  "
                  f"eventRemain={d.mEventTimeRemaining:.1f}")
            print(f"carFlags={d.mCarFlags:#06x}  oil={d.mOilTempCelsius:.0f}C  "
                  f"water={d.mWaterTempCelsius:.0f}C  num={d.mNumParticipants}  viewed={v}")
            if p:
                print(f"viewed[{v}] pos={p.mRacePosition} lap={p.mCurrentLap} "
                      f"name='{_s(p.mName)}'")
            time.sleep(0.5)
    finally:
        r.close()
