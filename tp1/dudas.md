# Dudas y decisiones abiertas

Cosas que me quedaron dando vueltas durante el TP. Las primeras tres las pude
cerrar mientras escribía el informe (las dejo con la respuesta porque el camino
hasta ahí me sirvió para entender el tema); las últimas dos siguen abiertas.

---

## 1. La consigna y el man page no coinciden en los campos de `stat` (resuelto)

La consigna dice que Sesión (SID) y grupo de procesos (PGID) están en los
**campos 6-7** de `/proc/<pid>/stat`. Al implementar `procfs.parse_stat` seguí
el [man page de proc(5)](https://man7.org/linux/man-pages/man5/proc.5.html),
que numera:

```
(4) ppid
(5) pgrp     <- grupo de procesos (PGID)
(6) session  <- sesión (SID)
(7) tty_nr
```

Al principio creí que había un corrimiento general de +1 entre las dos fuentes,
pero comparándolas fila por fila resultó ser más puntual: **`session` sí es el
campo 6**, así que ese coincide; el que no coincide es `pgrp`, que es el 5 y no
el 7. Mi lectura es que la consigna quiso decir "campos 5-6" y además los listó
en el orden inverso al que aparecen en el archivo (nombra SID primero, pero en
`stat` viene PGID primero).

Lo verifiqué así, comparando `/proc` crudo contra `ps`:

```bash
$ awk '{print "pgrp="$5, "session="$6}' /proc/$$/stat
$ ps -o pid,pgid,sid -p $$
```

Los otros campos de la tabla de la consigna los chequeé uno por uno y coinciden
exactamente con el man page: utime/stime en 14-15, minflt/majflt en 10-13,
priority en 18, nice en 19, rt_priority en 40, policy en 41. Así que es un error
puntual de esa fila, no un criterio de numeración distinto. Me quedé con el man
page por ser la fuente primaria.

## 2. Los bits 32 y 33 de las máscaras de señales (resuelto)

Al decodificar `SigCgt` me encontré con procesos que tenían seteados bits fuera
del rango 1-31 de las señales POSIX, y por debajo de 34, que es donde
`signal.SIGRTMIN` dice que arrancan las RT. Por ejemplo, en un `polkitd` de mi
sistema:

```
SigCgt: 0000000100000002   ->   ['SIGINT', 'SIGSETXID']
```

Son señales internas de NPTL, la implementación de threads de glibc:

- **32 = `SIGCANCEL`**, que usa `pthread_cancel()` para cancelar threads.
- **33 = `SIGSETXID`**, que usa glibc para propagar un `setuid()`/`setgid()` a
  todos los threads del proceso (el kernel sólo cambia las credenciales del
  thread que hace la syscall, así que glibc le manda esta señal al resto).

Lo que me cerró el círculo es que **son la explicación de por qué `SIGRTMIN`
vale 34 y no 32**: el kernel expone `__SIGRTMIN = 32`, pero glibc se reserva
esas dos para uso propio y corre el rango "público" dos lugares para arriba.
Durante el desarrollo yo tenía un `_SIGRTMIN = 34` hardcodeado en `procfs.py`
sin entender de dónde salía el 34; ahora sale de `signal.SIGRTMIN` y el
comentario explica el motivo.

Al final decidí **sí** nombrarlas (`SIGCANCEL`/`SIGSETXID`) en vez de mostrar
`SIG32`/`SIG33`. Al principio me parecía más honesto no inventarles nombre,
pero un bit sin explicación en la vista Señales es más confuso que informativo,
y el comentario del código aclara que son de glibc y no POSIX.

**Lo que medí, para no exagerar el hallazgo.** Mi primera versión de esta nota
decía que "los bits 32 y 33 aparecen en casi cualquier proceso". Lo conté y no
es así. Sobre 30 procesos de mi sistema (glibc 2.39):

| | procesos |
|---|---|
| con `SIGSETXID` en alguna máscara | 5 de 30 |
| con `SIGCANCEL` en alguna máscara | **0 de 30** |
| multithread (`Threads` > 1) | 6, y 4 de esos tienen `SIGSETXID` |

O sea: `SIGSETXID` aparece, pero sólo en procesos que usan NPTL de verdad (no en
todos), y `SIGCANCEL` **no aparece en ninguno** en glibc 2.39 — la cancelación
de threads dejó de implementarse con un handler que quede registrado en
`SigCgt`. Dejo el nombre mapeado igual, porque el bit sigue estando definido y
puede aparecer en otra versión de libc, pero no puedo afirmar que se vea en la
práctica acá.

**Queda abierto** si `SIGCANCEL` se ve en glibc más viejas (< 2.34, donde
`pthread_cancel` sí usaba un handler) o en musl, que no tengo a mano para
probar.

## 3. `Nice` no está en `/proc/<pid>/status` (resuelto)

La consigna ofrece dos fuentes para Nice: "`/proc/<pid>/stat` campo 19 **o**
`status: Nice`". Al implementar el analizador de scheduling no encontré esa
línea en `status` y supuse que era una cuestión de versión de kernel.

No lo es: `/proc/<pid>/status` **no tiene ni tuvo nunca** un campo `Nice:`. Se
verifica en cualquier kernel con:

```bash
$ grep -i nice /proc/self/status      # no devuelve nada
```

Los campos de `status` relacionados con scheduling son `Cpus_allowed_list` y
los dos contadores de context switches, que sí uso. El único lugar donde vive
nice es el campo 19 de `stat` (y, indirectamente, `/proc/<pid>/autogroup`).
O sea que la alternativa que ofrece la consigna simplemente no existe, y el
código no tiene una ruta sin ejercitar: tiene la única ruta posible.

---

## 4. ¿El diseño "sin agregador propio" es el correcto? (abierto)

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

## 5. El recolector no "distribuye trabajo" (abierto)

La consigna describe al recolector como "lista procesos vía `/proc`,
**distribuye trabajo** a los analizadores". El mío hace la primera mitad:
publica la lista de PIDs en un `Manager.list()` y cada analizador la lee y hace
el 100% de su propio trabajo sobre todos los PIDs.

La consecuencia medible es que hay lectura duplicada: cuatro analizadores
(`resumen`, `memoria`, `senales`, `scheduling`) abren y parsean
`/proc/<pid>/status` de todos los procesos, cada uno a su propio ritmo. Con
`pid: host` y varios cientos de procesos eso se nota, y el analizador de FDs es
peor todavía porque hace un `readlink()` por FD de cada proceso.

La alternativa sería que el recolector reparta los PIDs por una `Queue` entre
varios workers, o que lea `status` una vez y publique el resultado crudo para
que los demás lo consuman. La descarté porque acoplaría los analizadores entre
sí y rompería la propiedad de que cada uno tiene su propio intervalo
independiente (¿a qué ritmo leería `status` el recolector, si sus cuatro
consumidores refrescan a 2 s, 3 s, 10 s y 10 s?). Pero no estoy seguro de que el
desacople valga el costo, y es la pregunta que más espero que me hagan sobre
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
