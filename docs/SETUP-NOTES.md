# Notas de Setup — aprendizajes desde la telemetría

Guía viva de setup destilada de **nuestra propia data** (varias carreras/prácticas analizadas con
`--insights` / `--balance` / `--tyres`) + investigación verificada. No es un setup único: son
**direcciones y ventanas afinables** por feel + telemetría. Español chileno.

> Cómo validar cualquier cambio: corre el stint, después `analyze_telemetry.py <carpeta> --tyres`
> (presión/temps), `--balance` (sobre/subviraje + momento de inestabilidad) y `--insights`. Aísla el
> setup actual del resto de la sesión con `--last N` (las N vueltas recientes, por `uid`).

---

## Principios (lo que más nos costó aprender)

### 1. Presión: apunta a la VENTANA EN CALIENTE, no a la más baja
La presión es la palanca #1 del **agarre y de la *catchability*** (qué tan avisado/progresivo es el límite).
- **Objetivo: presión EN CALIENTE en ventana** — GT3 ~**1.85 bar (~27 psi)**, GT4 ~**27-28 psi** hot.
  Se mide a **mitad de stint con la goma caliente** (nunca el cold), con `--tyres`.
- **Demasiado baja** (lo que nos pasó al bajar a 1.4 bar): el grip *pico* sube, pero la curva grip-vs-deslizamiento
  se estrecha → el neumático **cae del pico sin meseta** → *breakaway repentino, límite vago, "cuando se va, se
  va rápido y no lo cachas"*. Además **recalienta** (y entonces pierde grip igual). El cold queda al borde bajo.
- **Demasiado alta** (lo que nos pasó al inicio en Sebring, 31-32 psi hot): centro de banda recalentado, menos
  huella, el auto resbala y se calienta atrás.
- **Regla:** sube el cold hasta caer en ventana hot; no persigas la presión hacia abajo buscando "estabilidad de
  promedio" — abajo de ventana es donde el límite se vuelve vago. (GT3: cold ~**1.5-1.55 bar** suele caer en ~1.85 hot.)
- **Bandera roja en `--tyres`:** una goma fría (<70°C) o muy bajo la ventana → vas a grainear y el límite queda
  vago, **antes** que cualquier ARB/diff.

### 2. Balance: doma el trasero sin matar el aviso del límite
Nuestros autos (GT4 Audi y GT3 Lambo) salieron **sobreviraje-prone**, peor en **curvas lentas** (el trasero se
suelta en la entrada/al pisar). Para reducirlo **sin perder catchability**:
- **NO "todo más blando".** Ablandar ARB trasera + dampers + bajar presión a la vez quita el sobreviraje pero te
  deja **sin aviso del límite** (esa fue nuestra trampa).
- **La vía limpia:** **ARB delantera más firme** (le das mordida adelante sin tocar/desestabilizar el trasero) +
  **ARB trasera media** (no al mínimo) + **presión en ventana** + **brake bias un toque adelante** para las
  entradas lentas. Estabilizas sin difuminar la señal.
- **Diferencial (preload/clutches):** sube el lock para tracción/estabilidad de salida con **TC=0**. Ojo: si el
  trasero se va en la **entrada** (no al pisar), más lock puede empeorar — ahí prioriza presión/ARB.
- **Dampers:** rebound trasero muy blando = el tren tras reacciona lento → el desliz se desarrolla antes de que lo
  sientas. Afinar de a 1 click, no en bloque.

### 3. La asimetría de temperaturas es de la PISTA, no de presión
Que una rueda (ej. la izquierda/trasera) corra más caliente que otra es por la **dirección dominante de las
curvas** (pista que gira a derecha carga el lado izquierdo) + sesgo del auto — **no se arregla con presión**.
La presión balancea **cada goma consigo misma** (centro vs bordes), no el desbalance izq/der.

### 4. Dato AMS2 (GT3 Gen2): el diferencial solo edita PRELOAD y CLUTCHES
Los ramp angles power/coast **no son ajustables** en los GT3 Gen2 de AMS2. Olvida "diff coast" — son esos dos.

---

## Hoja de setup — Lamborghini Huracán GT3 EVO2 (AMS2)

Auto "pointy" (motor central): gira bien pero **snapea en lentas**; con **ala=0** (meta) toda la estabilidad
es **mecánica + presión**. Idea madre: *no está sobrevirado de fábrica — el límite vago viene de presión baja +
ablandar de más; la receta es presión en ventana + ARB delantera firme, no "todo blando".*

| Parámetro | Objetivo | Nota |
|-----------|----------|------|
| **Presión frío** ⭐ | **1.50-1.55 bar** (~22 psi) | apunta a ~1.85 hot; 1.4 queda corto |
| Presión caliente (lo que persigues) | **~1.85 bar / 26.5 psi** (1.8-1.9) | mídelo a mitad de stint |
| ARB delantera | media-firme (+1-2 sobre default) | la vía limpia anti-sobreviraje |
| ARB trasera | media-blanda (1-2 bajo default) | no al mínimo (mata catchability) |
| Diff preload | ~120 Nm (90-150; techo 150 si rota al pisar) | tracción/estabilidad con TC=0 |
| Diff clutches | +1-2 plates (4-8) | más lock mata el power-oversteer |
| Rebound trasero | hacia el medio | si lo ablandaste en bloque, fírmalo 1-2 |
| Brake bias | tu base, +1-2% adelante listo para lentas | calma la entrada |
| Rake | leve positivo (cola +1-2 más alta) | mordida de entrada |
| Ala | **0** (respetado) | sin aero atrás → todo mecánico |

**Catchability (orden de impacto):** (1) presión a ventana — recupera la mayor parte del aviso; (2) firma el
rebound trasero al medio; (3) bias +1-2% adelante + ARB delantera firme.

**Por pista:** estrecha/lenta (Hungaroring) → preload al techo (130-150), ARB del. firme, bias adelante, rake
leve, cold un toque más alta (cuesta meter calor). Rápida (Bathurst/Sebring) → menos preload, presión normal.

---

## Notas — Audi R8 GT4 (Buenos Aires)
- **Sobreviraje en lentas** (igual que el Lambo). Levers sin ala: ARB tras. más blanda / ARB del. más firme /
  más precarga de diff / brake bias adelante.
- **Brake bias 48F/52R lo soltó en la frenada** (R/F bajo frenada 0.86→1.16): para este auto suelto, bias adelante.
- **Subir precarga (90→150 Nm) dio estabilidad/tracción** (~break-even en tiempo, con ganancia de tracción medida).
- **Consumo: ~3.26 L/vuelta** (mediana). Lap-time best ~2:02.1.
- **S1 se domina con repetibilidad** (mismo punto de frenada/línea), no con setup.

---

## Estrategia de carrera (combustible en carrera por TIEMPO)
Para una carrera de `T` minutos:
- **Vueltas ≈ floor(T·60 / lap_time) + 1** (cruzas meta tras el reloj 0 → +1 vuelta de cierre).
- **Combustible para terminar = vueltas × consumo/vuelta.** Carga eso **+ margen** (~1.5 vueltas + vuelta de
  formación). No sobrecargues: combustible = peso.
- **Robustez:** una carrera por tiempo quema ~`consumo/vuelta ÷ lap_time` L/s ≈ constante por trazado → el
  **total** es parecido aunque cambie la pista; lo que cambia es el **número de vueltas**.
- En vivo, la **página ESTRATEGIA del dash** (`ams2_strategy.py`) lo calcula con tu consumo real; en práctica
  puedes cargar el formato a mano (`set_race_plan`) para ver la proyección.

**Ejemplo (GT4, 40 min @ Buenos Aires):** ~20 vueltas · ~65 L para terminar · **cargar ~70-72 L**.

### Modo combo — agrega TODAS tus sesiones de un auto+pista (offline)
Además del ⚙ en vivo (que usa solo la sesión actual), el analizador consolida el histórico por combinación:
- `analyze_telemetry.py --list` — sesiones **agrupadas por combinación** pista-auto.
- `analyze_telemetry.py --combo [filtro]` — mejor vuelta, **tendencia entre prácticas**, consumo medio e
  **insights RECURRENTES** (los que se repiten en ≥2 tandas = lo persistente, no un mal día). Sin filtro usa
  la última combinación; con filtro (`--combo GT4`) matchea substring de auto/pista.
- `analyze_telemetry.py --race-fuel <min>` o `--race-laps <N>` — **carga estimada** desde el consumo de las
  **prácticas** del combo (excluye carreras, que son atípicas: mojado/tráfico/ahorro). Ej: `--race-fuel 40 --combo GT4`.

---

## Cruce LLUVIA → LISOS (pista que seca)
La decisión más cara de una carrera mixta. **AMS2 no expone un % de mojado** (verificado vs
`ams2_shm.py`): el secado se **infiere** de proxies. Checklist en vivo:

1. **¿Paró de llover?** → `rain` (densidad de lluvia) **bajando** vuelta a vuelta y ya bajo **~0.13**.
   (`rain=0` NO es pista seca — la superficie sigue mojada un rato; es señal *adelantada*.)
2. **¿La pista calienta?** → `track_t` subiendo sostenido (secado y calor van juntos).
3. **¿Tus gomas de lluvia se recalientan (>72°C)?** → **la señal más confiable.** La wet se enfría con
   el agua; sin agua sobrecalienta. *(En la carrera GT4 del 21-06 las wets tocaron 75.7°C en la V9 — ya
   tocaba cambiar; se cambió ~V15 y los lisos fueron ~8s/vuelta más rápidos.)*

**≥2 de las 3, sostenidas 2 vueltas → entra a la PRÓXIMA ventana de pits. NO "una vuelta más".**

- **Confirmación visual (manda sobre la data):** charcos fuera de trazada desapareciendo + menos spray.
  La data SIEMPRE va un poco atrás de lo que ves en cabina.
- **Regla de oro (payoff asimétrico):** cambiar temprano = **1 out-lap fría** (~3s, pago único).
  Cambiar tarde = **~8s CADA vuelta** y crece. **Ante la duda, adelántate.**
- **Nunca repongas lluvia en pista que seca monótona** (apostar contra la tendencia).

### Detector de crossover (en el dash)
La página ESTRATEGIA muestra un **semáforo** (`ams2_strategy.py::_crossover`), solo en carrera con goma
de agua, con voz del ingeniero:
- 🟢 **VERDE** "aguantá lluvia" · 🟡 **AMARILLO** "ventana de lisos abierta, prepará boxes" (TTS) ·
  🔴 **ROJO** "vas tarde, lisos ya van más rápido, box" (TTS urgente).
- Umbrales afinables: `RAIN_DRY_THR=0.13`, `TRACK_WARM_SLOPE=0.15`, `WET_OVERHEAT=72`. Histéresis
  asimétrica (sube agresivo, baja conservador). Muestra las **señales crudas**, no un semáforo opaco.
- **Honesto:** infiere de proxies con retardo; la línea seca visual y los rivales NO están en la data →
  el ojo en cabina manda en el instante del cambio. Reduce el sesgo a quedarse tarde, no elimina el azar.
