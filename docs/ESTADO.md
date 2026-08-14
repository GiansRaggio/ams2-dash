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

## Stuttering y frame times (investigación del 2026-08-12)

Herramienta: `tools/grabar-presentmon.bat` (PresentMon de Intel, binario en
`sim/lmu-dash/tools/bin/`). Salida a `frametime/` (gitignoreado, ~150 MB por noche).
Análisis con `sim/lmu-dash/tools/lmu_presentmon.py`, que sirve para cualquier juego.

**Cadena de mejoras, cada eslabón medido** (Ryzen 5 7600X, RTX 4070 SUPER,
Samsung LC34G55T 3440×1440 @165 Hz con G-SYNC, AMS2 en Road America):

| paso | mediana | eventos/min | ms perdidos por evento |
|---|---|---|---|
| sin nada | 5,61 ms (178 fps) | 28,5 | 37 |
| **cap a 160** | **6,25 ms (160 fps)** | 26,9 | 39 |
| **+ apps de fondo cerradas** | 6,25 ms | 25,0 | **20** |
| **+ Game Bar apagado** | 6,25 ms | 24,0 | 21 |

El cap arregló el **ritmo**: de 68% de frames fuera de la ventana VRR a 0,3%.
Cerrar aplicaciones arregló la **severidad**: partió a la mitad los ms perdidos por
evento y eliminó los atascos catastróficos (el peor pasó de 2064 ms a 59 ms).
Ninguno de los dos cambió la FRECUENCIA de los eventos, que se quedó en ~25/min.
En todos los casos los frames malos son CPU-bound: CPU ~30 ms contra GPU ~5 ms.

**Trampas, todas verificadas contra capturas reales:**

- **AMS2 no tiene limitador de fotogramas.** `graphicsconfigdx11.xml` declara todas
  sus propiedades y no hay ninguna: solo `Vsync` y `FrameLatency`. El cap va por driver.
- **El cap de la NVIDIA App NO se escribe en el perfil del driver.** Se configuró a
  160, la App lo mostraba aplicado, y la medición dio 178 fps de mediana con 70% de
  frames sobre 166 — idéntico a no tener cap. En el **Panel de Control clásico** la
  lista de programas aparecía VACÍA, sin rastro de AMS2. Agregándolo ahí a mano
  (`Agregar` → `Examinar` → el .exe) y pulsando **Aplicar**, funcionó: mediana 6,25 ms
  exactos. Usar siempre el panel clásico y verificar midiendo.
- **El proceso real es `AMS2AVX.exe`, no `AMS2.exe`.** El lanzador elige según el
  soporte AVX de la CPU. Apuntar al otro captura CERO frames sin dar ningún error.
- **PresentMon debe arrancar ANTES que el juego.** Enganchado a un AMS2 ya corriendo
  clasifica todo como `Composed: Flip` en vez de `Hardware Composed: Independent Flip`
  —pierde la creación de la swap chain— y en modo compuesto el DWM regulariza los
  intervalos, **escondiendo los atascos**. Misma máquina, mismo día: p99 = 6,34 ms
  enganchando tarde contra 13,05 ms arrancando antes. La captura sale preciosa y no
  significa nada. Verificar siempre la columna `PresentMode`.
- **`--terminate_on_proc_exit` produce un abrazo mortal.** PresentMon abre un handle al
  proceso del juego; ese handle mantiene vivo el objeto del proceso aunque el juego ya
  esté muerto (0 hilos, 0 handles), así que espera para siempre a que desaparezca algo
  que él mismo sostiene, y deja el CSV bloqueado en exclusiva. Se usa `--timed N
  --terminate_after_timed`.
- **El CSV queda bloqueado mientras PresentMon vive**: ni leerlo ni copiarlo. Y no se
  puede matar sin elevar, porque `--restart_as_admin` deja el proceso de trabajo
  elevado. De ahí que la captura se corte sola por tiempo.
- **El MOZA Pit House muestra el juego como "Ejecutando" después de cerrarlo.** Es
  estado obsoleto de su interfaz, no un proceso vivo: verificar con `tasklist`.

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
6. **Stuttering: falta la medición limpia del undervolt.** Tras aplicar Curve Optimizer
   −12 el piloto reporta mejora sustancial y el gráfico del juego ya no muestra caídas,
   pero la única captura posterior se enganchó tarde (`Composed: Flip`) y por eso no
   sirve para cuantificarla. Repetir con PresentMon arrancado ANTES del juego. Vigilar
   además errores WHEA: al momento de escribir esto no había ninguno, pero llevaba 18
   minutos de encendido y un undervolt inestable puede tardar días en dar la cara.
7. **Ruido tipo estática mientras se maneja, no en boxes ni en el escritorio.** Es
   anterior al undervolt. La hipótesis de que fuera el mismo evento que los atascos de
   CPU quedó debilitada cuando el piloto precisó que lo escucha **al girar**: apunta a
   la muestra de sonido de fricción de goma del auto. Test barato: cambiar de auto, y
   comparar el ritmo del ruido contra el de las curvas (en Road America, 12 curvas en
   133 s = una cada 11 s).

## Corpus

~190 sesiones en `telemetry/`. `tools/tyre_replay.py` es el guardián: replay del analizador
REAL contra todas, falla si el camber deja de opinar, si no hay pistas direccionales o si la
referencia no llega al disco. Correr después de CUALQUIER cambio de umbral o de lógica.
