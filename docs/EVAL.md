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
| `mTyreTemp`, `mTyreTempLeft/Center/Right` | **°C** | los que usamos (OK) |
| `mTyreCarcassTemp`, `mTyreTreadTemp`, `*LayerTemp` | **Kelvin** | restar 273.15 — **NO** usar sin convertir |
| `mEventTimeRemaining` | **ms** | normalizado a s (TIME_SCALE) |
| `mSessionDuration` | **min** | × 60 a s |
| `mSuspensionTravel/Velocity` | m, m/s | en dampers ×1000 → mm |
| `mAirPressure` | PSI (declarado) | libs derivadas confunden Bar×100 — tratar como referencia |
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
