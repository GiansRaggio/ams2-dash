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

- **La atribución del sector sucio NO es de fiar, ni antes ni (todavía) después del
  arreglo.** Medido en el corpus: **134 de 134 vueltas invalidadas marcan el S3**, y 128
  marcan SÓLO el S3. Eso no puede ser cierto — no hay circuito donde todos los cortes
  ocurran en el último sector. El código re-evaluaba a qué sector culpar en CADA frame
  con `mLapInvalidated` en alto, así que un flag sostenido terminaba marcando el último
  sector poblado. **Arreglado el 2026-08-20** atribuyendo sólo en el flanco de subida
  (`ams2_telemetry.py:284`), con test de regresión que falla sin el fix.
  ⚠️ **Pero el arreglo NO está verificado en pista, y puede no ser suficiente.** Las tres
  hipótesis sobre el flag siguen abiertas y el dato grabado no las separa:
  el flag persiste hasta meta · el flag sube y baja · **AMS2 lo levanta tarde**, ya en el
  S3, cuando confirma la infracción. Si es la tercera, el flanco también cae en el S3 y el
  fix no corrige la atribución (sólo evita ensuciar sectores posteriores).
  **Verificación pendiente, y es barata**: salirse de pista A PROPÓSITO en el S1 de una
  vuelta y mirar qué marca `sectors.jsonl`. Un intento en cada sector cierra el tema.
  ⚠️ **No rescatar sectores "limpios" de vueltas sucias** para rankings ni para la vuelta
  ideal, ni con el corpus viejo ni con el nuevo, hasta que esa prueba se haga.
  Nota de método: el terreno de la traza NO sirve para ubicar el corte — detecta la primera
  vez que dos ruedas pisan algo que no es asfalto, y eso incluye pianos legítimos (da S1 en
  37 de 40 vueltas, que es el sesgo del detector, no el dato).

- **El punto de frenada por curva se mide bien, pero tiene tres trampas** (todas con
  test desde el 2026-08-20, `--frenadas`):
  1. **Modular el freno parte la frenada en dos.** Cortar en el primer hueco daba dos
     poblaciones en la misma curva (Snetterton: 5 vueltas en ~3.220 m y 12 en ~3.410) y
     una σ falsa de 89 m. Se tolera hasta 15 m con el freno suelto antes de cortar.
  2. **Las trazas truncadas envenenan el cálculo y hay que decirlo.** Sesiones enteras
     con ~300 muestras (6 s a 50 Hz) por inanición de CPU: se descartan bajo 1.000
     muestras y se reporta cuántas, porque callarlo parece "esta sesión no tiene frenadas".
  3. **En esas mismas trazas `lap_dist` NO es monótona**, así que las restas dejan de
     acotar y el punto se va al otro lado de la vuelta (rango de 1.117 m con un tope de
     búsqueda de 400). Guard explícito sobre el resultado.
  La dispersión va con el **filtro MAD de `_split_anomalas`**, no con σ cruda: 3 vueltas
  de 17 que frenaban 190 m más tarde llevaban la σ de 8 a 72 m.
- **La dispersión del punto de frenada es MENOR en práctica que en carrera** (1.9-6.7 m
  contra 3.6-11.6 m, mismo piloto). Es lo que el tráfico predice, y sirve como validación
  cruzada de que la métrica mide algo real y no ruido del detector.
  ⚠️ El corte de 10 m para llamar "inconsistente" a una curva **es de trabajo, no medido**.

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

## Intercambio con otros pilotos: el formato `.srt` (2026-08-19)

`ams2_srt.py` lee y escribe el binario propietario de **Sim Racing Telemetry**, la app
con que graba otro piloto. Sirve para importar sus vueltas a nuestro visor y para
mandarle las nuestras. Su app **no importa CSV**: por eso rechazaba nuestros archivos,
no por cómo estaban formateados.

**El formato** (ingeniería inversa verificada byte a byte): contenedor tipo RIFF,
`<FourCC><u32 tamaño><carga>`, strings con prefijo de largo. `F___<SRT >` con
hdr/src/sess/trck/veh, un índice de vueltas, y un `Z___` que es zlib (15,7 → 40,2 MB).
Dentro: 71 canales descritos por chunks `dsdp`, y por vuelta `laph`/`laps`/`lapd`.
Cada muestra son **624 bytes = `<f32 distancia>` + 155 f32** en el orden de los canales
(instancias×componentes cada uno: 1 escalares, 3 vectores, 4 por rueda).

**Las trampas, todas medidas — cuatro de las cinco son de UNIDAD O DE EJE:**

- **`world_position` viene como `[x, z, y]`**: el vertical es el componente **2**, al
  revés de la shared memory de AMS2. Mapearlo a ciegas dibuja un eje horizontal contra
  el perfil de altura: el trazado sale como garabato y el detector encontraba **32
  curvas donde hay 10**. Se detecta integrando el largo del trazado: con (0,2) da
  1952 m y con (0,1) da 4310, contra 4305 de `lap_distance`.
- **Los vectores de CUERPO van `[longitudinal, lateral, vertical]`** y los nuestros
  `[lateral, vertical, longitudinal]`. Afecta a `gforce`, `velocity` y `angular_vel`.
  Mapear por índice idéntico deja **la G de frenado en el canal lateral**, y como
  `analyze_telemetry` decide con `accel_x` qué rueda carga cada curva, el veredicto
  sale invertido en toda sesión importada — sin que nada se vea raro. Medido:
  `gforce[0]` corr −0,669 con (acelerador−freno), `gforce[1]` corr −0,873 con el
  volante, `angular_vel[2]` corr −0,963 con el volante.
- **`local_vz` tiene el signo opuesto**: el nuestro es negativo hacia adelante
  (−63,6..−17,8 m/s avanzando), el suyo positivo. Sin el −1 el export sale en reversa.
- **`ride_h` está en CENTÍMETROS en el nuestro** (6,7..12,2 en un GT4) y en metros en
  el suyo (0,04..0,16). Factor 100.
- `gforce` en **g** (nosotros m/s²) y `tyre_press` en **pascal** (nosotros Bar×100).
  `tyre_rps` sí comparte signo; `susp_pos`, `susp_vel` y `tyre_slip` comparten unidad.

**El orden de ruedas es FL, FR, RL, RR**: se confirmó porque presiones y temperatura de
carcasa salen más altas atrás, la firma conocida de ese auto.

**⚠️ La prueba byte a byte NO cubre el mapeo de canales.** Reconstruir el archivo del
otro y obtener el bloque de datos idéntico (40.241.222 b) valida el contenedor y la
compresión, pero alimenta floats **crudos** del original y **se saltea el constructor
entero**. Y el round-trip de export se valida con nuestro propio lector, que comparte
cualquier convención equivocada del escritor. Dos tests verdes dieron confianza falsa
mientras tres canales estaban permutados. Lo único que lo delata es la **huella física**:
`accel_z` debe seguir al pedal y `accel_x` no; `ang_vel_y` debe seguir al volante;
`local_vz` debe ser del orden de la velocidad y `local_vx` no (5 m/s de lateral, no 65).

**El `.srt` es entrada no confiable** (llega de un tercero). Tenía bomba de
descompresión —300 KB en disco → 631 MB de RAM, porque se descomprimía todo antes de
validar— más zlib corrupto y largos de string absurdos escapando como excepciones
crudas. Ahora se descomprime acotado a lo que el archivo declara, con tope de 512 MB,
y todo sale como `SrtError`.

**Las sesiones importadas van a `telemetry_importado/`, NO a `telemetry/`**: ese
directorio es el corpus con el que `tyre_replay` valida los analizadores, y una sesión
ajena trae canales que no tenemos (van en 0.0, declarados en `canales_ausentes` de
`session.json`) que lo envenenarían en silencio. Del lado **export** los ceros que se
leerían como medición se evitan: `tyre_isOnGround` va en 1.0 (cero diría "las 4 ruedas
en el aire toda la vuelta") y las temperaturas salen del resumen de la vuelta.

De paso, la **curvatura ahora se mide sobre una base física de 12 m** en vez de entre
puntos adyacentes: su app graba a ~20 Hz contra nuestros ~50, y al llevar sus puntos a
la grilla de 2 m se interpola y queda una poligonal con codos. Nuestras sesiones no se
movieron (Watkins Glen sigue 11/11).

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

8. **`.srt`: confirmar la permutación de ejes contra un SEGUNDO archivo.** El orden
   `[longitudinal, lateral, vertical]` se dedujo de un solo archivo (Alpine A110 GT4 en
   Spielberg). La evidencia es contundente —correlaciones de 0,87 y 0,96— pero conviene
   verificarlo con otro auto y otra pista antes de darlo por universal. Entre
   `velocity[1]` y `[2]` la evidencia es la más débil: se eligió `[1]` como lateral
   porque es el único que cambia de signo, y una velocidad lateral tiene que cambiarlo.
9. **`.srt`: canales que exportamos en 0.0.** `world_forward`, `world_right`,
   `oil_press`, `water_press`, `fuel_press`, `boost_*`, los de daño y `wing_setup` no
   los grabamos. La mayoría los tiene la shared memory de AMS2 y el recorder
   simplemente no los guarda: si alguna vez importa, se agregan al recorder primero.

## Corpus

~190 sesiones en `telemetry/`. `tools/tyre_replay.py` es el guardián: replay del analizador
REAL contra todas, falla si el camber deja de opinar, si no hay pistas direccionales o si la
referencia no llega al disco. Correr después de CUALQUIER cambio de umbral o de lógica.

## Bordes aprendidos de la pista (2026-09-10)

AMS2 no entrega la geometria de la pista (ni la shared memory ni los archivos, que van cifrados). Lo que si entrega es `mTerrain` por rueda, y el visor lo usa para APRENDER los bordes (`ams2_analysis.bordes`, `/api/bordes`): suma todas las vueltas grabadas en esa pista y variante (limpias y anuladas, de cualquier sesion), pone cada rueda en el mundo (media via 0.85 m, medio eje 1.35 m, rumbo desde las posiciones del auto) y anota por metro de la linea de referencia hasta donde hubo asfalto y donde hubo piano. Codigos del enum TerrainMaterials de PCARS2 verificados contra el corpus: 0 asfalto en toda la recta, 10 y 41 solo en pianos, 46 escapes (Mosport), 7 pasto, 49 linea blanca ilegal (Cordoba). Medido: el corredor es lo que cubrieron las ruedas -- 4 vueltas de Cordoba = 2.7 m de mediana, 83 de Mosport = 7.9 m, 154 de Road Atlanta = 9.8 m. Cache en `telemetry/_cache/bordes__<pista>.json` con firma (n trazas, mtime); Road Atlanta tarda 6.5 s la primera vez. Las importaciones .srt NO entran: traen el terreno en 0.0 y dirian que todo es asfalto.
