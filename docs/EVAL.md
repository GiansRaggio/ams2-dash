# Loop de evaluación y mejora — AMS2 Dash

Documento vivo del proceso iterativo para mejorar **fiabilidad**, **interpretación**
y **consejos** del dashboard. Se actualiza en cada iteración.

## El loop (atado a las tandas de pista)

1. **EVALUAR**
   - *Objetivo:* `python tools/eval_strategy.py` sobre la última sesión grabada →
     nota 0-100 por eje (combustible/lap-time/desgaste) contra la telemetría real.
   - *Cualitativo:* la **rúbrica** de abajo (revisión a ojo + casos de prueba).
2. **PRIORIZAR** — elegir 1-3 ítems del **backlog** por valor/esfuerzo.
3. **IMPLEMENTAR + VALIDAR** — código + test (la suite no debe bajar de verde).
4. **PROBAR EN PISTA** — el piloto corre una tanda con REC on → nuevo eval →
   comparar notas → siguiente iteración.

Cadencia: por sesión. Insumos: telemetría grabada (`telemetry/*/`) + feedback de pista.
Herramientas: `eval_strategy.py` (fiabilidad), `analyze_telemetry.py` (interpretación).

## Rúbrica (el estándar — derivada de SimHub/iRacing/MoTeC/Crew Chief)

**Fiabilidad**
- F1. Ningún número vivo se muestra con frame inválido/congelado → STALE / "sin datos", nunca el último valor disfrazado de vivo.
- F2. Unidades y rangos correctos en todo canal (ver tabla de unidades); fuera de rango = STALE.
- F3. Consumo/estrategia nunca usa vueltas sucias (formación, in/out-lap, amarilla, outlier). Error de "litros a cargar" ≤ 1 vuelta.
- F4. La incertidumbre se comunica (sin data / parcial / confiable + nº de vueltas de muestra), no se oculta.

**Interpretación**
- I1. Cada pantalla glanceable se entiende en ≤2 s por color/forma, sin leer crudos.
- I2. Los crudos se traducen a diagnóstico (gomas spread L/C/R, damper banda+asimetría, peor sector), no solo se muestran.
- I3. La procedencia se etiqueta: estimado/aprox/live-beta. 0 datos derivados presentados como medidos.

**Consejos**
- C1. Accionable: verbo + magnitud + unidad ("cargá 38 L", "bajá bump lento 1 click").
- C2. Declara el limitante y la decisión (una ventana de pit), no dos números sueltos.
- C3. Guardrails físicos: bottoming → no ablandar dampers; lift&coast → avisar posible sub-carga. 0 consejos peligrosos.

**UX**
- U1. TTS solo eventos accionables: 0 repeticiones <60 s, 0 datos STALE, no críticos caen en recta.
- U2. Una densidad de datos por vista; navegación 1 tap/swipe; estados de error visibles <3 s.

## Tabla de unidades canónicas (shared memory AMS2 / PCARS2)

| Campo | Unidad | Nota |
|-------|--------|------|
| `mFuelLevel` | fracción 0–1 | litros = × `mFuelCapacity` |
| `mTyreTemp`, `mTyreTempLeft/Center/Right` | **°C** | L/C/R poblados, center entre bordes 100% (verificado 2026-06-20, Audi GT4) |
| `mTyreCarcassTemp`, `mTyreTreadTemp`, `*LayerTemp` | **Kelvin** | restar 273.15 — **NO** usar sin convertir |
| `mEventTimeRemaining` | **ms** | normalizado a s (TIME_SCALE) |
| `mSessionDuration` | **min** | × 60 a s |
| `mSuspensionTravel/Velocity` | m, m/s | en dampers ×1000 → mm |
| `mAirPressure` | **Bar×100** (verificado) | crudo ~188 → /100 ×14.5038 ≈ 27 psi (2026-06-20). NO es PSI directo |
| `mEnforcedPitStopLap` | nº vuelta | v14 quitó UNSET=-1 → válido sólo si ≥1 |

## Backlog priorizado (valor/esfuerzo)

**Quick wins de fiabilidad (foundation):**
- [it.1] Validar cada frame (mVersion, carname propio, rango fuel) → reusar último bueno. *(hecho)*
- STALE con badge gris + TTS callado en datos muertos.
- Settle: ignorar formación/out-lap, "calculando…" hasta ≥1 vuelta limpia.
- Banda de confianza en el semáforo (sin data / parcial / confiable + nº vueltas).

**Consejos (alto valor):**
- Limitante explícito fuel-vs-goma → UNA ventana de pit ("manda FL, pitea v15-18").
- Vida de goma en VUELTAS por pendiente de desgaste (regresión, no % crudo).
- Consumo robusto: media de N verdes + descarte in/out-lap + margen 5%.
- Detección de lift-and-coast para no sub-cargar.
- Vueltas restantes en carrera a tiempo vía pace propio + última del líder (estimado).
- Objetivo de ahorro: L/vuelta + costo s/vuelta + veredicto factible/justo/imposible.
- Guardrail de bottoming en dampers antes de recomendar ablandar.

**Interpretación:**
- Gomas: estado color por ventana (azul/verde/rojo) + diagnóstico spread L/C/R.
- Damper: bandas LSR/LSC/HSR/HSC + objetivo cono -30..+30 + % por banda.
- Tiempos por sector con peor sector resaltado; "pit en vuelta N" concreto.
- Black box de combustible de 5 números (Restante/Última/Promedio/Vueltas-est/A-cargar).

**UX / análisis (logger):**
- TTS: cola por severidad + cooldown + no hablar en curva.
- Delta por distancia vs mejor vuelta propia (modo análisis post-stint).
- Mapa de pista, overlay de trazas, ghost lap, consistencia, vmin por curva, coasting.

**Descartados por restricciones:** presión en vivo como número confiable, lógica de
Safety Car limpia, relative tipo F3 de iRacing, coaching contra pro externo. (Detalle
y razón en la investigación.)

## Bitácora de iteraciones

### it.0 — baseline (estado al iniciar el loop)
Página ESTRATEGIA (fuel-at-end semáforo, ahorro, ventana de pit, 4 ruedas, limitante,
planificación manual), DAMPERS (histograma + clicks + travel), TIEMPOS, botón detener,
toggle todas-las-vueltas, TTS edge-trigger, **fix de falsa alarma de combustible**
(basada en estanque real, no proyección al final). Logger de telemetría + analizador +
eval harness. Suite: 39 asserts estrategia + 13 telemetría.

### it.2 — análisis post-stint (interpretación I1/I2 + consejos C1)
`analyze_telemetry.py` gana modo de manejo: detección de curvas (apex por mínimos
prominentes de velocidad), **delta por distancia vs tu mejor vuelta** (`--vs A [B]`)
con "dónde perdés/ganás" mapeado a curva, **vmin de apex** A-vs-ref, **coasting** por
tramo (gas y freno sueltos), y **consistencia por sector** (sector más disperso) en el
resumen. Probado: identifica correctamente la curva donde se pierde el tiempo y el déficit
de vmin. Suite: +8 asserts (`test_analysis.py`). *Próximo eval: correr y comparar vueltas.*

### it.1 — "no mentir": guard de frame (F1, F2)
Validación de frame en la capa de lectura (`mVersion`, carname propio no vacío,
`mFuelLevel` en rango) → si el frame es basura (corrupción de MMF compartida con
PCARS2), se conserva el último bueno y se marca SIN SEÑAL en vez de volcar números
corruptos. Tabla de unidades canónicas documentada (evita el bug Kelvin/Celsius).
*Próximo eval: correr una tanda y comparar la nota de fiabilidad.*

### it.3 — niveles de grabación (perf / PCs de menor rendimiento)
Confirmado que **AMS2 no guarda telemetría a disco de forma nativa** (las apps —
AMS2SD/MoTeC, SimHub, sim-to-motec — leen la misma shared memory que nosotros).
Leer la memoria es **costo cero para el juego** (por eso dejamos el UDP); el único
costo es CPU propia. Nuestro store (carpeta/sesión, `summary.jsonl` + trazas
`.csv.gz`, append-only) ya es la "mini base de datos". Agregado: **3 niveles** de
grabación seleccionables (⚙) y persistidos —
- **off**: hilo dormido, no lee.
- **summary**: solo resumen por vuelta a ~10 Hz, sin trazas (casi gratis, PCs flojos).
- **full**: resumen + traza de 71 canales a 50 Hz (default).

`set_mode()` en el logger, comando WS con `mode`, selector 3-vías en el dash,
indicador REC con el modo. Suite: +4 asserts (modo resumen guarda línea sin traza).

### it.4 — telemetría completa + sectores rescatables + canales verificados
Logger ampliado **71→87 canales** (yaw/vel angular, vel local, `terrain` por rueda, carcass temp
K→C, max_rpm/torque, abs_active, y tc/abs_setting+drs en summary). **Verificado en pista** (Audi R8
GT4 @ Buenos Aires): spread de goma L/C/R **poblado con center entre bordes 100%** → el diagnóstico
de camber/presión SÍ es construible (contra el temor de SimHub #632); presión = **Bar×100** (≈27
psi); yaw/terrain/abs/carcass poblados; `abs_setting` viaja (el README decía que no). **Bug de
sectores arreglado**: el logger leía `mCurrentSector1Time` ya reseteado al cruzar meta (S1≈0 →
vuelta teórica falsa 1:36.9); ahora S1/S2 se capturan en vivo y S3 = total−S1−S2, con recuperación
en el analizador para sesiones viejas. **Rescate de sectores**: `sectors.jsonl` guarda splits +
validez por sector de TODA vuelta de pista (incluidas las invalidadas) → la "vuelta ideal" toma el
mejor sector **limpio** de cada vuelta, rescatando sectores buenos de vueltas con error. Telemetría
**Moza diferida** (no aporta para tiempos: la fuerza load-cell en kg no está expuesta; `brake-output`
duplica `mUnfilteredBrake`). Suite: +12 asserts. Pendiente: camber/presión en la UI (beta), balance
por curva (yaw+slip).

### it.5 — guardrail C3 de dampers (bottoming → no ablandar bump)
`_recommend` no cruzaba el recorrido de suspensión: la regla `high>=28` podía recomendar ablandar
fast bump con la suspensión tocando fondo (viola C3). Ahora cruza `tBottom` (peor neumático del eje,
umbral `BOTTOM_GUARD_PCT`=5% — más bajo que el flag de resortes a 8% a propósito) y, con bottoming,
suprime todo ablandamiento de bump y avisa la corrección física (rate/altura/packers; revisar también
rebound/pack-down). Refinado por **revisión adversarial** (2 lentes: física + regresión): se quitó el
endurecimiento numérico forzado de fast bump — el histograma de velocidad no separa el bombeo de curva
de los impactos de piano, así que la sugerencia de endurecer va como texto. `tools/test_dampers.py`
(nuevo): 11 asserts. Suite total: 88.

### it.6 — motor de insights v1 (CLI-first)
`analyze_telemetry.py --insights`: convierte el análisis en 2-4 consejos accionables priorizados por
tiempo recuperable (C1: veredicto+acción+magnitud), referencia = tu propia mejor vuelta limpia.
Diseñado con workflow (2 lentes: coaching + confiabilidad). Reglas v1: **R2** peor sector vs tu ideal
(medido), **R1** déficit de vmin por curva (vmin medido / tiempo estimado), **R3** coasting en la
entrada (metros). R4/R5/R6 (frenada/gas/yaw-slip) diferidas: no confiables con N chico. **Guards
anti-falso-positivo** (la lección del peor-sector distorsionado por warmup): mínimo 3 vueltas limpias o
calla; descarta warmup/invalidadas; piso de ruido (<0.30s sector, <3 km/h, <25m); repetición en ≥2
vueltas; anti-empate; procedencia explícita; dedup por curva. Inicia el refactor a estructuras
(`*_struct` devuelven dicts → habilitan UI futura). `tools/test_insights.py` (nuevo): 13 asserts. Suite
total: 101. Validado en pista: con 2 vueltas limpias el motor dice "N insuficiente" (correcto) — necesita
una tanda de 3-4 vueltas limpias seguidas para hablar.
