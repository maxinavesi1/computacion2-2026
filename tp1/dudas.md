# Dudas y decisiones abiertas

Las dos decisiones de arquitectura del TP sobre las que sigo sin tener certeza.
No son cosas que no entendí, son bifurcaciones donde elegí un camino y el otro
también me parece defendible.

Otras dudas que traía —la numeración de los campos de `/proc/<pid>/stat`, los
bits 32/33 de las máscaras de señales, y el `Nice` que la consigna dice que
está en `status`— las pude cerrar verificándolas contra `/proc` real mientras
escribía el informe, así que las respuestas quedaron donde corresponde: en el
README y en los comentarios del código que las usan.

---

## 1. ¿El diseño "sin agregador propio" es el correcto?

Ver README §3.5. La consigna lista un **Agregador** como componente mínimo
obligatorio. En mi diseño ese proceso existe (es el servidor que arranca
`multiprocessing.Manager()`, y se lo puede ver en la propia TUI), pero no lo
escribí yo: cada analizador escribe directo su clave del snapshot y la
"agregación" de las 7 dimensiones queda resuelta por construcción, porque
ninguno pisa la clave de otro.

Estoy convencido de que el argumento es correcto y de que es un diseño más
simple. Lo que **no** tengo claro es si es el que pide la consigna, o si la
intención era que hubiera un proceso mío en el medio, consumiendo de una
`Queue` lo que producen los 7 analizadores y siendo el único escritor del
`Manager.dict`.

A favor de mi versión: menos procesos, menos copias de los datos, y ninguna
`Queue` intermedia que pueda crecer sin control si el agregador se atrasa
respecto de los productores. A favor de la otra: habría un punto único donde
validar el snapshot antes de publicarlo (hoy no puedo, por ejemplo, descartar
un snapshot incompleto), y la discusión de race conditions sería más rica
porque habría un verdadero problema productor/consumidor que resolver en vez
de evitarlo por diseño.

Me quedé con la mía y la documenté, pero es la decisión del TP sobre la que
tengo menos certeza.

## 2. El recolector no "distribuye trabajo"

La consigna describe al recolector como "lista procesos vía `/proc`,
**distribuye trabajo** a los analizadores". El mío hace la primera mitad:
publica la lista de PIDs en un `Manager.list()` y cada analizador la lee y hace
el 100% de su propio trabajo sobre todos los PIDs.

La consecuencia medible es que hay lectura duplicada. Contando las llamadas por
proceso vivo, por vuelta de cada analizador:

| Archivo | Lo abren |
|---|---|
| `/proc/<pid>/status` | **5**: resumen, memoria, senales, scheduling y sistema |
| `/proc/<pid>/stat` | **4**: resumen, memoria, scheduling y sistema |

O sea que con N procesos vivos el sistema hace 5N lecturas de `status` y 4N de
`stat` por ciclo completo, en vez de N y N. No es tan grave como parece porque
cada analizador corre a su propio ritmo (las de 10 s contribuyen poco), pero es
trabajo duplicado real. Y el analizador de FDs es peor todavía, por otro
motivo: hace un `readlink()` por cada FD de cada proceso, que no es duplicación
sino volumen.

La alternativa sería que el recolector reparta los PIDs por una `Queue` entre
varios workers, o que lea `status` una vez y publique el resultado crudo para
que los demás lo consuman. La descarté porque acoplaría los analizadores entre
sí y rompería la propiedad de que cada uno tiene su propio intervalo
independiente: ¿a qué ritmo leería `status` el recolector, si sus cinco
consumidores refrescan a 2 s, 3 s, 10 s, 10 s y 2 s? Tendría que ir al ritmo del
más rápido y desperdiciar trabajo para los lentos, o al del más lento y
entregarle datos viejos al más rápido. Pero no estoy seguro de que el desacople
valga el costo, y es la pregunta que más espero que me hagan sobre
escalabilidad.

---

## Notas de proceso

- **La TUI la probé a mano**, corriendo el contenedor y apretando las teclas.
  Para que quedara algo repetible pasé la lógica de teclado a funciones puras
  sobre `EstadoUI` (`_manejar_tecla`, `_manejar_entrada_texto`,
  `_obtener_lista_procesos`), separadas del dibujado, y las cubrí en
  `tests/test_ui.py` inyectando keycodes. Lo que queda sin test automático es el
  dibujado en sí, que sigue siendo verificación visual.
- **Las señales las probé** con `kill -USR1/-USR2/-HUP/-TERM` contra el PID de
  `main` desde otra terminal (`docker compose exec`), verificando el archivo
  `dump_*.json`, el cambio visible en la vista FDs y las líneas en
  `monitor.log`.
