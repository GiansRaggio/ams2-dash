# Dash para Le Mans Ultimate — evaluación de viabilidad y plan

> Estado: **evaluado, no empezado.** Documento para retomar.
> Fecha de la evaluación: 2026-07-27. Investigación hecha con Fable + verificación local.

## Veredicto

**Viable, y el resultado sería un dash MEJOR que el de AMS2**, no un puerto. LMU expone
como *dato* varias cosas que en AMS2 hay que inferir con meses de corpus, y algunas que
directamente no existen.

Lo verificado **contra el header oficial que el propio juego instala** en esta máquina
(`<LMU>\Support\SharedMemoryInterface\`, fechado abril 2026), no contra documentación de
terceros:

| Hecho | Estado |
|---|---|
| LMU instalado en `K:\SteamLibrary\steamapps\common\Le Mans Ultimate` | ✅ verificado |
| Shared memory **nativa** de S397 (`LMU_Data`), sin DLL de terceros | ✅ header presente |
| Setup completo legible (`.svm` en disco + REST en vivo) | ✅ verificado |
| Canales por rueda con carga, camber y toe vivos | ✅ en el header |
| SHM viva en runtime (EasyAntiCheat de por medio) | ⏳ **sonda** |
| `mOptimalTemp` poblado | ⏳ **sonda** |
| Las 3 temps del parche son sensores independientes | ⏳ **sonda** |

## Lo que cambia el juego: el setup ES un dato

Dos vías, las dos verificadas.

**En disco** — `UserData\player\...\*.svm`, texto plano, secciones por rueda con los
valores ya resueltos a unidades humanas:

```
[FRONTLEFT]   CamberSetting=11//-1.5 deg   PressureSetting=0//136 kPa   RideHeightSetting=11//4.6 cm
[REARLEFT]    CamberSetting=6//-0.9 deg
FrontAntiSwaySetting=1//D22 S-H            BrakePressureSetting=46//86 kgf (72%)
```

`TempModFile.svm` es el setup **cargado ahora** → el bridge lo lee al arrancar la tanda y
la ata al setup.

**En vivo** — `GET localhost:6397/rest/garage/getPlayerGarageData` devuelve el setup
completo: camber y presión en frío por esquina, los 4 clicks de damper por esquina,
resortes, ARBs, toe, diferencial, brake bias/migration, alas, relaciones de caja.
(Verificado leyendo el código de TinyPedal que lo consume, no un blog.)

**Consecuencia de diseño**: todo el aparato que construimos en AMS2 para *adivinar* el
camber —índice direccional acumulado, 60 s de curva, histéresis, ventana auto-referencial
contra la tanda previa en la misma pista— **deja de ser necesario**. En LMU `mCamber` y
`mTireLoad` son datos. Lo que **sí** sobrevive es la física: que el rolido se come el
camber de la goma de afuera y le calienta el hombro exterior sigue siendo el corazón del
diagnóstico. Muere el andamiaje, no el conocimiento.

## Canales por rueda (del header oficial, `InternalsPlugin.hpp`)

```
mCamber                        radianes, VIVO   (positivo = hacia afuera según el lado)
mToe                           radianes, VIVO
mTireLoad                      Newtons          ← qué rueda carga, AHORA
mLateralForce / mLongitudinalForce   Newtons
mGripFract                     fracción del parche deslizando
mTemperature[3]                Kelvin, izq/centro/der del AUTO (no inside/center/outside)
mTireInnerLayerTemperature[3]  Kelvin, 3 zonas
mTireCarcassTemperature        Kelvin
mOptimalTemp                   float            ← temperatura óptima del compuesto
mPressure                      kPa
mWear                          0..1 ("not necessarily proportional with grip loss")
mBrakeTemp                     Celsius
mSuspensionDeflection / mRideHeight   metros
mFlat / mDetached              bool             ← flat-spot modelado
mCompoundIndex / mCompoundType
```

**Trampa de unidades nueva**: gomas en **Kelvin**, frenos y óptimo en **Celsius**, presión
en **kPa**. Mismo tipo de error de 273° esperando.

**Trampa de marco de referencia**: el header advierte textual que `mTemperature[3]` es
izquierda/centro/derecha **del auto**, *"not to be confused with inside/center/outside!"*
— exactamente lo de `mTyreTempLeft/Right` en AMS2, sólo que acá viene documentado. Ojo que
`mCamber` en cambio SÍ es relativo a la rueda: dos convenciones distintas en el mismo
struct.

Por auto: `mFuel` en litros directos, `mRearBrakeBias`, downforce F/R, híbrido completo
(carga de batería, regen), **`mVirtualEnergy`** (el recurso que de verdad manda en LMU),
`mFrontAntiSway`/`mRearAntiSway`, `mDeltaBest`, gaps nativos. Scoring: `playerVehicleIdx`
directo (el hack de `player.txt` muere), `mTrackGripLevel`, clima con wetness por sector.

## Arquitectura de la SHM (resuelve un riesgo que Fable dejó abierto)

```
mapping:  LMU_Data          evento: LMU_Data_Event
lock:     LMU_SharedMemoryLockData + LMU_SharedMemoryLockEvent  (spinlock + waiters)
layout:   SharedMemoryObjectOut { generic, paths, scoring, telemetry }
          telemetry: activeVehicles, playerVehicleIdx, playerHasVehicle, TelemInfoV01[104]
          scoring:   ScoringInfoV01 + VehicleScoringInfoV01[104]
```

Es **event-driven con mutex**, no polling con contador de secuencia. O sea el problema de
"frames rotos" está cubierto por un lock explícito — mejor que lo de AMS2. Hay que
replicar el spinlock en Python (`InterlockedCompareExchange` → `ctypes`).

⚠️ Tu instalación tiene `rFactor2SharedMemoryMapPlugin64.dll` instalado y **habilitado**,
pero ése es el camino **legacy** y hay reportes de que dejó de funcionar en LMU 1.3.
**No construir sobre él.** La vía es la nativa.

## Qué se porta y qué muere del código actual

| Módulo | Destino |
|---|---|
| `index.html` (SPA, 5 páginas) | **Se porta casi gratis** — consume JSON del WS, agnóstico |
| `bridge_shm.py` (WS + HTTP + pump + reconexión) | **Cirugía menor** — cambia `update_state()` y el guard de consistencia |
| `ams2_shm.py` | **Se reescribe**, mecánico, con el header oficial a la vista |
| `player.txt` / `player_index()` | **Muere** — `playerVehicleIdx` nativo |
| `ams2_tyres.py` | **Esqueleto sí, calibración no** — EMAs, detección de canal muerto y persistencia por (pista, auto) se portan; ventanas, spreads y atribución de carga los reemplaza la medición directa |
| `ams2_strategy.py` | **Media reescritura + feature nueva obligatoria**: sin `mVirtualEnergy` un director de estrategia LMU está cojo |
| `ams2_dampers.py` | **Degrada** — no hay velocidad de suspensión nativa, hay que derivarla de la deflexión a ~50-60 Hz contra los ~166 Hz de hoy. Única página que sale perdiendo |
| `ams2_telemetry.py` | **Esqueleto sí**, mapeo de columnas nuevo |
| `tools/analyze_telemetry.py` | **Se porta vía columnas** — y mejora con `mGripFract` + `mTireLoad` |
| `tyre_replay.py` / `tyre_mock.py` / tests | **Arnés sí, corpus y fixtures no** |

### Decisión: repo hermano, no capa de abstracción

`lmu-dash` separado, copiando `index.html` y el transporte tal cual, analizadores propios.

El argumento (y es el que aprendimos a golpes): lo valioso de `ams2-dash` no es el
plumbing —eso se copia en una tarde— sino la **calibración por-sim**, y ésa no se
abstrae. Un "snapshot neutro" que le pase un `temp_center` al analizador sin decirle si es
sensor real o byte-copy es exactamente la clase de mentira estructural que costó dos
rediseños. Además tocar `ams2-dash` (252 tests, ~160 sesiones de corpus, estable) para
acomodar otro sim es riesgo de regresión puro.

Abstraer recién cuando el segundo dash exista y se VEA qué resultó común de verdad
(frontend y esquema del recorder son los candidatos; los analizadores casi seguro no).

## ⚠️ El corpus de AMS2 no valida NADA de LMU

Ni las ventanas térmicas, ni el `[0,10]` de spread, ni el ±3 °C inter-tanda, ni los 60 s
de curva. Todo eso es física del Madness Engine; LMU usa el linaje rF2. **El grabador se
enciende el día 1** porque el corpus es el activo lento.

## Plan por etapas

0. ~~Instalar LMU~~ — ya está instalado.
1. **SONDA `lmu_probe.py`** (medio día + una tanda). Abre `LMU_Data`, structs del header
   oficial, y durante 20-30 min de práctica (seco + ojalá 10 min de lluvia) loguea a CSV:
   `gameVersion`, delta/s de `SME_UPDATE_TELEMETRY` (→ tasa real), doble-lectura-y-compare
   (→ frames rotos), y por rueda `mTemperature[3]` con **byte-compare centro vs vecinos**,
   `mOptimalTemp`, `mPressure`, `mTireLoad`, `mCamber`, `mToe`, `mGripFract`, carcasa,
   capa interna, `mWear`, `mBrakeTemp`. En paralelo `curl` cada 5 s a
   `/rest/garage/getPlayerGarageData` guardando los JSON crudos.
2. **Bridge mínimo + página DASH** (1-2 días): `lmu_shm.py` + `bridge_lmu.py` emitiendo el
   mismo JSON → `index.html` casi intacto.
3. **GOMAS v1 sólo-medición** (3 zonas, presión, wear, frenos, carga) sin veredictos
   calibrados, **y el grabador encendido**.
4. **ESTRATEGIA con virtual energy** — el diferenciador real en LMU.
5. **Veredictos calibrados** cuando el corpus dé, con el loop sonda→replay→umbral.

Hasta un dash útil en pista: **1-2 semanas con tandas de por medio.** Los veredictos
finos: meses de corpus, como fue acá.

### Qué decide la sonda

- ¿SHM viva con EAC? → si no, el proyecto cambia de forma (REST como columna vertebral).
- ¿Centro real o byte-copy? → si es real, **revive el diagnóstico centro-vs-hombros**, que
  en AMS2 está muerto. Capacidad nueva.
- ¿`mOptimalTemp` poblado? → si sí, los veredictos térmicos pueden ser **absolutos contra
  el óptimo** en vez de auto-referenciales, con el andamiaje actual como fallback.
- ¿El JSON del setup completo en MI versión? → habilita el diagnóstico directo de camber.

Con esos cuatro en verde, esto no es portar el dash: es hacer uno mejor.

## Riesgos

1. **Interfaz nativa joven** (desde LMU v1.2, dic-2025) y S397 sigue agregando campos →
   validar `gameVersion`/`sizeof` al abrir y **fallar ruidoso**, no leer basura.
2. **REST con churn documentado** entre updates → usarla como enriquecimiento (setup,
   virtual energy), nunca como columna vertebral del dash en vivo.
3. **Canales muertos por descubrir** — cero documentación ≠ cero canales muertos (el
   `surf_dead` de AMS2 tampoco estaba documentado). **Sondear lluvia primero.**
4. **Requiere `Enable Plugins = ON`** en Settings → Gameplay. Es el "paso firewall" de
   LMU: documentarlo o el dash "no conecta" sin error visible.
5. **DAMPERS degrada** — sin velocidad nativa.

## Fuentes

Header oficial local: `K:\SteamLibrary\steamapps\common\Le Mans Ultimate\Support\SharedMemoryInterface\`
· [pyLMUSharedMemory](https://github.com/TinyPedal/pyLMUSharedMemory) (MIT, structs en ctypes)
· [TinyPedal `garage.py`](https://github.com/TinyPedal/TinyPedal/blob/master/tinypedal/process/garage.py) (mapeo del setup REST→SVM)
· [TinyPedal `lmu_restapi.py`](https://github.com/TinyPedal/TinyPedal/blob/master/tinypedal/adapter/lmu_restapi.py)
· [Changelog LMU v1.2](https://www.bsimracing.com/le-mans-ultimate-update-v1-2-full-changelog/)
· [SimHub #2264](https://github.com/SHWotever/SimHub/issues/2264) (DLL legacy rota en 1.3)
· [lmu-pitwall](https://github.com/Swizzjack/lmu-pitwall) (prior art)
