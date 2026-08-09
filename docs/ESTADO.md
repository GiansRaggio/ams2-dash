# Estado y pendientes

> Punto de retome. Lo que está acá es lo que **costó descubrir** y no se deduce leyendo el
> código: hechos medidos, trampas conocidas y trabajo abierto con su evidencia.
> Última actualización: 2026-07-30.

## El patrón de bug que se repite (leer antes de tocar cualquier veredicto por rueda)

**Un canal por rueda interpretado sin saber cuál rueda está trabajando.** Apareció cinco
veces. Cada vez pareció un problema distinto y era el mismo:

| Dónde | Qué pasaba | Estado |
|---|---|---|
| Camber (`ams2_tyres`) | Veredicto en las 4 ruedas; la ventana absoluta estaba centrada en la distribución de la rueda DESCARGADA → acusaba al 27% de los ejes cargados | ✅ arreglado |
| Saturación (`--saturacion`) | `tyre_grip` es margen SIN USAR: la goma descargada satura 81% del tiempo contra 26% la cargada → el veredicto caía en una rueda ociosa en **66 de 67 curvas** | ✅ arreglado |
| Balance (`--balance`) | Promediaba las dos ruedas del eje; la descargada patina más (96% de los ejes traseros) y el ratio R/F se corría al sobreviraje → **25% de las curvas con veredicto equivocado** | ✅ arreglado |
| `dEdge` (`--tyres`) | Ignoraba `INNER_IS_RIGHT` → el spread de las dos ruedas izquierdas salía con el signo invertido | ✅ arreglado |
| "Momento de inestabilidad" (`--balance`) | Sigue usando el promedio RL/RR: puede estar reportando la trasera interior patinando en el diferencial, no el trasero cargado | ❌ **abierto** |

Al tocar algo nuevo por rueda: preguntarse **cuál rueda mide** antes de escribir el umbral.

## Hechos medidos (no re-derivar; falsar sólo con evidencia nueva)

- **`accel_x > 0` ⇒ cargan las ruedas DERECHAS.** Fijado por TEMPERATURA: corr **−0.895**
  entre el índice direccional de `accel_x` y el calor izquierda-derecha, 69 sesiones.
  ⚠️ **NO deducirlo de `mSuspensionTravel`**: más travel es rueda EXTENDIDA (descargada),
  corr −0.872 con el calor. Deducirlo al revés invierte el veredicto entero — ya pasó.
- **`mSuspensionTravel` crece con la EXTENSIÓN.** Dos jueces independientes sobre 79
  sesiones: `corr(freno, travel delantero) = −0.405` y `corr(altura al piso, travel) = +0.910`.
- **La goma CARGADA tiene el spread de bordes MÁS BAJO** (mediana +5.0 contra +8.2 la
  descargada). El rolido se come el camber estático de la rueda de afuera y le calienta el
  hombro exterior — ésa es la medición de camber que sirve.
- **Presión en caliente GT3/GT4**: 180 esquinas, mediana **1.89 bar** (27.4 psi), p10-p90
  1.80-2.02. Reiza (staff) dice 1.8-1.9 para GT3. Spec real Pirelli GT4: 2.0 hot, mínimo de
  inflado 1.4 (y ese 1.4 es el mínimo que AMS2 deja poner).
- **`dcen` (bulk − media de bordes) correlaciona +0.98 con la presión** (40 de 42 sesiones),
  pero **NO separa vueltas rápidas de lentas** (mediana +0.23 en la más rápida contra +0.00
  en la más lenta). Por eso se emite como MEDICIÓN y no como veredicto.
- **Las reglas de simetría de dampers estaban clavadas cerca de cero por identidad física**:
  el recorrido en compresión y en extensión de una vuelta son el mismo número. `|SB−SR|`
  p90 = 3.5, `|FB−FR|` máximo = 4.0 en todo el corpus.
- **Histograma de dampers**: bins de 25 mm/s, ±400. Lento = |v| ≤ 50 → las **4 barras
  centrales**. Rápido = las **colas** desde ±62.5. El `low%/alta%` de la cabecera ES esa
  partición.

## Hechos del visor de telemetría (`ams2_analysis.py` + `analisis.html`)

- **La traza NO llega a meta.** AMS2 congela la shared memory al cruzar y el recorder
  guarda los últimos frames repetidos. Medido en 138 vueltas de 30 sesiones: mediana
  **0 ms** de tiempo faltante, p90 11 ms. Pero **todos** los outliers (hasta 228 ms)
  salen de UNA sesión, `Road_Atlanta__..._race__20260802_214607`, que además tiene
  **44% de muestras con distancia repetida** → ahí el hilo grabador se quedó sin CPU.
  Esos 12.5 m sin grabar a 178 km/h aparecían como un delta falso de 227 ms.
  El visor completa el tramo con el **cronómetro** (que se conoce exacto) y publica
  `cierre_s` para que una traza truncada se anuncie sola.
- **Numerar curvas por mínimos de velocidad pierde las rápidas.** En Spa se comía Eau
  Rouge y Blanchimont (14 de ~19). Se numera por **curvatura del trazado** + filtro de
  giro total ≥10°: la geometría no depende de cómo manejaste ese día. Contra oficial:
  Watkins Glen 11/11, Jerez 12/13, Road Atlanta 11/12, Spa 15/19 (el catálogo separa
  lo que la geometría une). **El número no es verdad de catálogo** — en el mapa se ve
  cada T dibujada, así que el error se detecta mirando.
- **El contador de vueltas se REINICIA al volver al garage.** Una práctica de Road
  Atlanta tiene seis tandas y tres "vuelta 1". La identidad es el `uid` del recorder,
  que ahora va también en `timeline.jsonl`.
- **Las vueltas de boxes son el BORDE de una tanda, no su inicio.** Tratar cada cruce
  con `pit`+`out` como inicio daba **once** tandas donde hay **seis**.
- **Trazas `X###.csv.gz` = vuelta invalidada.** Se graban para poder comparar la tanda
  completa, pero NO entran a `summary.jsonl`: todas las herramientas de análisis llegan
  a las trazas por el resumen, así que su corpus sigue siendo solo vueltas limpias.
  Verificado con `tyre_replay` (83 sesiones, VERIFICA OK).
- La API `/api/*` queda **expuesta en la LAN**: el nombre de sesión se valida como
  frontera de confianza (un `..` colado serviría cualquier archivo del disco).

## Trampas del entorno

- **`netstat` NO sirve para saber si el bridge está vivo.** Se puede colgar el event loop
  quedando el proceso vivo y reteniendo 8765/8080: desde afuera se ve idéntico a
  funcionando y el dash del teléfono queda congelado. **Verificar abriendo el WebSocket.**
  (El watchdog ahora lo detecta, vuelca stacks y reinicia con `execv`.)
- **La página del teléfono es una SPA: no se recarga sola.** Tras cambiar `index.html` hay
  que recargar a mano o se ve la versión vieja con backend nuevo.
- **El bridge no arranca solo** — decisión del piloto: lo lanza él cuando entra a AMS2,
  porque a veces necesita otros monitores. `start-dash.bat`, o `Start-Process` para que no
  cuelgue de la shell del agente.
- **El replay sólo ve vueltas limpias concatenadas**: nunca el garaje, el out-lap, los pits
  ni los resets. Tres bugs reales vivieron ahí (warm pegado, camber sin ejercitar,
  persistencia sin cobertura). Una observación en pista le gana al corpus cuando el corpus
  no contiene el fenómeno.
- **Correr los tests con `.venv\Scripts\python.exe`**, no con el python del PATH (no tiene
  `websockets`). Verificar exit code **Y** conteo de PASS/FAIL: hay suites cuyo `__main__`
  ignora el booleano.

## Abierto

1. **"Momento de inestabilidad"** usa el promedio RL/RR (ver tabla de arriba).
2. **Traslado presión FRÍO → CALIENTE: sin resolver, y ojo con volver a intentarlo mal.**
   El README y la UI afirman "el cambio en frío se traslada ~1:1". Puede ser falso, pero
   **no se puede medir con este corpus: no contiene ninguna lectura en frío** (la más fría
   está 19 °C sobre el ambiente; la mediana de arranque de stint, 76 °C). Cualquier
   pendiente que se "mida" acá reconstruye el lado frío con el mismo modelo que quiere
   validar → circular. Ya se cayó en eso una vez. Para resolverlo hace falta grabar el
   arranque en frío (el recorder hoy sólo guarda vueltas limpias).
3. **`report_tyres` (`--tyres`, beta)**: juzga con ventanas absolutas que el proyecto ya
   descartó en el resto, y no tiene gate de modelo de superficie muerto.
4. **Consejos de damper inaplicables**: el dash recomienda `fast bump/reb` sin saber si el
   auto los tiene (AMS2 no expone el setup). El piloto decidió filtrarlo él; no se
   construyó el registro de ajustes por auto.
5. **Dash para Le Mans Ultimate**: evaluado y viable, plan en [LMU-DASH.md](LMU-DASH.md).
   Sin empezar. Lo próximo es la sonda de medio día.

## Corpus

~190 sesiones en `telemetry/`. `tools/tyre_replay.py` es el guardián: replay del analizador
REAL contra todas, falla si el camber deja de opinar, si no hay pistas direccionales o si la
referencia no llega al disco. Correr después de CUALQUIER cambio de umbral o de lógica.
