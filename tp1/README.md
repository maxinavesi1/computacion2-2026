# TP Nº1 — Monitor de Procesos y Threads

**Computación II — Universidad de Mendoza — 2026**

Monitor de sistema en tiempo real (estilo `htop`) que lee `/proc` directamente,
sin `psutil` ni ninguna librería que abstraiga el acceso al kernel. Muestra la
anatomía interna de cada proceso: memoria, file descriptors, threads, señales y
scheduling. La arquitectura es multiproceso: un recolector, 7 analizadores
independientes (cada uno con su propio intervalo de refresco) y una TUI con 7
vistas alternables.

---

## Índice

1. [Descripción general](#1-descripción-general)
2. [Diagrama de arquitectura](#2-diagrama-de-arquitectura)
3. [Decisiones de diseño](#3-decisiones-de-diseño)
4. [Conceptos del curso aplicados](#4-conceptos-del-curso-aplicados)
5. [Limitaciones conocidas](#5-limitaciones-conocidas)
6. [Cómo correr y testear](#6-cómo-correr-y-testear)
7. [Ejemplo de uso](#7-ejemplo-de-uso)
8. [Decisiones sobre la TUI](#8-decisiones-sobre-la-tui)
9. [Lo que aprendí](#9-lo-que-aprendí)
10. [Extensiones implementadas (bonus)](#10-extensiones-implementadas-bonus)

---

## 1. Descripción general

El monitor levanta **11 procesos**: el principal (orquestador), el proceso
servidor que crea `multiprocessing.Manager()`, el recolector, los 7
analizadores y el display. Son 10 en modo daemon, donde el display no se lanza.
Verificable con `pgrep -P <pid de main>`, que devuelve los 10 hijos directos.
Cada analizador lee **sólo** `/proc` —a través de
`procfs.py`, el único módulo del proyecto que toca el filesystem— y escribe su
resultado en una estructura de memoria compartida (`snapshot`) que la TUI lee
para pintar las 7 vistas.

**Uso básico:**

```bash
docker compose up --build
```

Eso abre la TUI directamente en la terminal (el contenedor es interactivo:
`tty: true`, `stdin_open: true`).

| Tecla | Acción |
|---|---|
| `1`..`7` o `r m f t s p g` | Cambiar de vista (Resumen/Memoria/FDs/Threads/Señales/Scheduling/Sistema) |
| `↑` `↓` | Navegar la lista de procesos |
| `Enter` | Pinnear/despinnear el proceso seleccionado |
| `/` | Filtrar por nombre de comando |
| `u` | Filtrar por usuario |
| `c` | Ciclar orden: CPU% → RSS → PID |
| `+` / `-` | Ajustar el intervalo de refresco de la vista activa |
| `h` / `?` | Ayuda |
| `q` | Salir limpiamente |

Señales que acepta el proceso `main` (`kill -SIGXXX <pid>`): `SIGINT`,
`SIGTERM`, `SIGHUP`, `SIGUSR1`, `SIGUSR2`, `SIGWINCH` — ver §3.4.

---

## 2. Diagrama de arquitectura

```
                         ┌─────────────────────────────────┐
                         │      MAIN (orquestador)         │
                         │  self-pipe: INT/TERM/HUP/       │
                         │  USR1/USR2/WINCH  (senales.py)  │
                         └────────────┬────────────────────┘
                    instala los handlers, crea toda la memoria
                    compartida y recién entonces forkea a los hijos
      ┌───────────┬───────────┬───────────┬───────────┬───────────┬───────────┬───────────┐
      │           │           │           │           │           │           │           │
 ┌────▼────┐ ┌────▼────┐ ┌────▼────┐ ┌────▼────┐ ┌────▼────┐ ┌────▼─────┐ ┌───▼──────┐ ┌──▼──────┐
 │Recolec- │ │Resumen  │ │Memoria  │ │FDs      │ │Threads  │ │Señales   │ │Scheduling│ │Sistema  │
 │tor  1s  │ │  2s     │ │  3s     │ │  5s     │ │  2s     │ │  10s     │ │  10s     │ │  2s     │
 └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └────┬─────┘ └────┬─────┘ └────┬────┘
      │escribe    │           │           │           │           │            │           │
      ▼           └───────────┴─────┬─────┴───────────┴───────────┴────────────┴───────────┘
 pids_proxy                         │ cada uno escribe SÓLO su propia clave
 (Manager.list)                     │ snapshot[nombre] = {"data":.., "ts":.., "verbose":..}
      │                             ▼
      │              ┌───────────────────────────────────────────┐
      └─────leen─────▶      SNAPSHOT GLOBAL (Manager().dict)     │
                     │ resumen·memoria·fds·threads·senales·      │
                     │ scheduling·sistema  (7 claves + ts c/u)   │
                     └───────────────────┬─────────────────────┬─┘
                                sólo lee │                     │ Array('i',3)
                                         ▼                     │ top3_cpu / top3_mem
                                ┌────────────────────┐         │ (lock incluido)
                                │   Display (TUI)    │◀────────┘
                                │   curses, 1 loop   │
                                └─────────┬──────────┘
                               escribe intervalos con +/-
                                          ▼
                         multiprocessing.Value('d', ...) — uno por vista
                         (el mismo Value que lee el analizador correspondiente)

  Por fuera del flujo de datos:
  · Queue (log_queue): cualquier hijo -> main, para errores/eventos (8 productores, 1 consumidor)
  · Pipe (uno por analizador): main -> analizador, broadcast de config.json en SIGHUP
  · Value('b') (verbose): main -> los 7 analizadores + display, togglea SIGUSR2
  · Event (stop_event): main -> todos, shutdown coordinado
```

---

## 3. Decisiones de diseño

### 3.1 ¿Por qué cada mecanismo de IPC y no otro?

| Mecanismo | Para qué lo uso | Por qué éste y no otro |
|---|---|---|
| `Manager().dict()` (`snapshot`) | Los 7 resultados de los analizadores | Son estructuras **anidadas y de tamaño variable** (dicts de dicts, listas de threads/FDs de largo variable). `Value`/`Array` sólo sirven para tipos C fijos; no pueden representar "la lista de FDs de un proceso" sin que yo serialice a mano. `Manager.dict` hace esa serialización (pickle) por mí a través de su proceso servidor. |
| `Manager().list()` (`pids_proxy`) | La lista de PIDs vivos | Mismo argumento: el largo cambia en cada vuelta. La reemplazo siempre entera con `pids_proxy[:] = nuevos` (una sola llamada RPC), nunca elemento a elemento, para que ningún analizador vea una lista a medio escribir. |
| `multiprocessing.Value` (intervalos, `verbose`) | Escalares simples (7 floats + 1 bool) | Es la opción **más liviana**: no arma un proceso servidor ni pickle, es memoria compartida real (mmap) con un lock chico. Para un número, pagar el overhead de un Manager sería injustificado. Y la consigna lo pide explícitamente para el ajuste de intervalos en vivo. |
| `multiprocessing.Array('i', 3)` (`top3_cpu`, `top3_mem`) | Los 3 PIDs con más CPU/memoria | Tamaño fijo y conocido de antemano: candidato perfecto para `Array`, evitando una llamada RPC al Manager sólo para leer 3 enteros que el encabezado consulta en **cada frame** (~7 fps). |
| `multiprocessing.Queue` (`log_queue`) | Errores/eventos de cualquier hijo → 1 consumidor (main) | Es el patrón exacto para el que está pensada `Queue`: **muchos productores, un consumidor**, sin importar el orden entre productores distintos. Un `Pipe` es 1-a-1; con 8 procesos escribiendo no alcanza. |
| `multiprocessing.Pipe` (uno por analizador) | `main` empuja la config recargada por SIGHUP a cada analizador | Acá sí es 1-a-1 (main ↔ ese analizador puntual), full-duplex, y no necesito que lo vea nadie más. Es más liviano que una `Queue` por analizador y no mezcla mensajes de **control** con `log_queue`, que es de errores. |
| `multiprocessing.Event` (`stop_event`) | Shutdown coordinado | Es el primitivo pensado para "una bandera que muchos procesos esperan", con `wait()` interrumpible — más expresivo que un `Value('b')` con polling manual. |

Los cinco primitivos que pide la consigna (`Queue`, `Pipe`, `Manager`, `Value`,
`Array`) están usados, y cada uno por un motivo distinto, no para cumplir la
lista.

### 3.2 Race conditions y cómo las evito

1. **Escritura vs. lectura del snapshot.** Cada analizador es el **único
   escritor** de su propia clave (`snapshot["memoria"] = {...}`). Nunca dos
   procesos escriben la misma clave, así que no hace falta lock explícito.
   Además la asignación reemplaza el diccionario **entero** de una sola vez
   (una sola llamada RPC), nunca lo muto campo por campo: eso evita que el
   Display lea un dict a medio construir. Si hiciera
   `snapshot["memoria"]["algún_pid"] = x` en vez de reasignar todo, sí habría
   una ventana de inconsistencia — el proxy del Manager haría dos RPCs (un
   `__getitem__` y un `__setitem__` sobre el dict interno) y entre las dos el
   Display podría leer.

2. **La lista de PIDs cambiando bajo los pies de un analizador.** El recolector
   la actualiza con `pids_proxy[:] = nuevos` (una sola RPC). Cada analizador,
   al arrancar su vuelta, hace `pids = list(pids_proxy)` **una sola vez** y
   trabaja sobre esa copia local durante toda la iteración, en vez de iterar
   directo sobre el proxy.

3. **TOCTOU clásico de `/proc`:** un proceso listado por el recolector puede
   morir antes de que el analizador llegue a leer su `/proc/<pid>/algo`. Todas
   las funciones de `procfs.py` atrapan `FileNotFoundError`/
   `ProcessLookupError`/`PermissionError` y devuelven `None`; el analizador
   descarta ese PID en esa vuelta. No es una excepción de mi código, es un
   estado esperable del sistema. `read_fds()` distingue además los dos casos
   con un sentinel (`SIN_PERMISO` vs. `None`), porque "es de otro usuario" y
   "ya no existe" son cosas distintas y la TUI las tiene que mostrar distinto.

4. **`top3_cpu_arr` / `top3_mem_arr`:** los escribe un único analizador
   (`sistema.py`), pero los protejo igual con `arr.get_lock()` al escribir.
   El Display los lee sin lock a propósito: el peor caso es ver el array a
   mitad de actualizar durante **un** frame de ~150 ms, que se corrige solo en
   el siguiente. Prefiero el lock del lado de la escritura, que es donde
   importaría si en algún momento hubiera más de un escritor.

5. **`interval_value.value` leído por el analizador y escrito por el Display:**
   `multiprocessing.Value` crea un lock interno por defecto (`lock=True`), así
   que la lectura/escritura del `double` ya es atómica. Igual uso
   `with val.get_lock()` en el ajuste con `+`/`-`, porque ahí hago
   read-modify-write (leer el valor, sumarle el paso, escribirlo) y eso **sí**
   necesita el lock tomado durante las tres operaciones, no una por una.

6. **Shutdown a mitad de una escritura:** en vez de `time.sleep(intervalo)`,
   todos los loops usan `stop_event.wait(intervalo)`, que retorna
   inmediatamente si alguien llama a `stop_event.set()`. Eso evita la race de
   "el analizador de Señales está dormido 10 segundos, el `join(timeout=3)` se
   cumple igual y un `terminate()` lo mata a mitad de una escritura al
   Manager", que sí podría dejar el snapshot inconsistente.

7. **Ventana de arranque sin handlers.** `main()` instala el self-pipe
   **antes** de crear el Manager y de forkear. Al revés hay una ventana de
   varios cientos de milisegundos en la que un Ctrl+C mata a main con el
   traceback por defecto y deja los 9 hijos y el Manager huérfanos, sin nadie
   que llame a `stop_event.set()`. El precio de instalarlos antes es que cada
   hijo hereda la disposición del padre vía `fork()`, así que todos llaman a
   `senales.reset_en_hijo()` como primera línea (ver §3.4).

### 3.3 ¿Por qué estos intervalos por defecto?

Van de más frecuente/liviano a menos frecuente/pesado, siguiendo el costo real
de cada lectura de `/proc`:

- **Resumen y Sistema (2s):** son las vistas "de un vistazo", y leer
  `stat`+`status`+`cmdline` por proceso es barato (3 `open()`/`read()` chicos).
  Vale la pena que se sientan vivas.
- **Threads (2s):** igual de barato por thread, pero multiplicado por la
  cantidad de threads del sistema. Lo dejo igual que Resumen porque en general
  no hay tantos como para que moleste.
- **Memoria (3s):** además de `status`, lee y parsea `/proc/<pid>/maps`
  completo (decenas o cientos de líneas por proceso) — más caro, así que menos
  frecuente.
- **FDs (5s):** implica un `os.listdir()` **más un `os.readlink()` por cada
  file descriptor abierto** de cada proceso. Es con diferencia el analizador
  más caro en syscalls si hay procesos con muchos FDs (un servidor con
  cientos de sockets), así que le doy el intervalo más relajado.
- **Señales y Scheduling (10s):** son datos que casi no cambian salvo que
  alguien llame explícitamente a `sigprocmask()`/`sched_setscheduler()`. No se
  pierde nada refrescando cada 10 s, y así les dejo más CPU a las vistas que sí
  cambian todo el tiempo.
- **Recolector (1s):** más rápido que la vista más exigente (0.5 s de
  Resumen/Threads), para que la lista de PIDs esté fresca. Si el recolector
  fuera más lento que un analizador, ese analizador estaría releyendo la misma
  lista vieja varias veces al hilo.

La tabla completa (nombre, teclas, default y **mínimo**) vive en un solo lugar,
`src/vistas.py`, y la importan tanto `display.py` (para las teclas `+`/`-`)
como `main.py` (para el reload por SIGHUP). Antes estaba duplicada y las dos
rutas que pueden cambiar un intervalo respetaban pisos distintos: el teclado no
bajaba de 5 s en la vista Señales pero un SIGHUP sí la podía dejar en 0.1 s.
Ahora las dos pasan por `vistas.clamp_intervalo()`.

### 3.4 Señales del monitor

Implementadas con el patrón **self-pipe** (`src/senales.py`): el handler de
cada señal hace lo mínimo posible y async-signal-safe (un `os.write()` de 1
byte a un pipe interno); el trabajo real —leer `config.json`, volcar el
snapshot— lo hace el loop principal cuando detecta ese byte, **fuera** del
contexto del handler.

- **SIGINT / SIGTERM** → `stop_event.set()`. `main` une (`join`) a los 9 hijos
  con `terminate()` de respaldo a los 3 s, drena la cola de logs y recién ahí
  apaga el `Manager`.
- **SIGHUP** → relee `config.json`, actualiza los `Value` de intervalo de las
  7 vistas (pasando por `vistas.clamp_intervalo`, el mismo piso que respeta el
  teclado) y hace un broadcast de la config completa a cada analizador por su
  `Pipe`. Los filtros por defecto de la TUI se propagan al Display mediante un
  `reload_seq` en un `Manager.dict` chico (`control`), porque el estado de
  filtros vive únicamente en la memoria local del proceso Display y no hay otra
  forma de tocarlo desde afuera.
- **SIGUSR1** → vuelca el snapshot completo a `dump_<timestamp>.json`.
- **SIGUSR2** → togglea un `Value('b')` que **los 7 analizadores leen en cada
  vuelta**, así que cambia los datos que se recolectan, no sólo el cartel del
  encabezado: la vista FDs pasa de 20 a `fds.max_mostrados` entradas por
  proceso y agrega el inodo de cada pipe/socket, Memoria suma el desglose de
  RSS (anon/file/shmem) y VmPeak, y Threads suma el último core en que corrió
  cada LWP más sus jiffies crudos de `utime`/`stime`. El `Value` se lee una
  sola vez por iteración para que toda la vuelta use un valor coherente.
- **SIGWINCH** → no hace falta reenviarlo: el Display recalcula
  `stdscr.getmaxyx()` en cada frame. Lo dejo con un handler que lo loguea para
  que quede visible que la señal llegó (la consigna la marca como opcional).

**Latencia de SIGHUP y SIGUSR2 (y por qué los analizadores duermen en tramos).**
Los dos mecanismos entregan su efecto en ≤ 0.5 s, y llegar a eso fue
consecuencia de probarlo. En la primera versión cada analizador hacía un único
`stop_event.wait(intervalo)` y miraba el Pipe de reload sólo al principio de la
vuelta. Midiéndolo con un SIGHUP seguido de un SIGTERM 3 segundos después, sólo
5 de los 7 analizadores habían registrado el reload: faltaban Señales y
Scheduling, los de intervalo 10 s, que estaban dormidos. Lo mismo pasaba con
SIGUSR2: un dump tomado 1 segundo después del toggle salía con `verbose: false`
en las 7 vistas.

Ahora el sueño se parte en tramos de 0.5 s (`_TRAMO_MAX` en
`analizadores/_base.py`) y entre tramos el analizador drena el Pipe y compara el
`Value` de verbose; si el usuario togleó, corta el sueño y fuerza una vuelta.
Sólo se interrumpe la cadencia normal ante una acción explícita del usuario, así
que la propiedad de "cada analizador tiene su propio ritmo" se mantiene. El
costo son 2 wakeups por segundo por analizador, cada uno un `sem_timedwait` que
vuelve por timeout: nada comparado con parsear cientos de archivos de `/proc`
por vuelta.

**Disposición de señales en los hijos.** `senales.reset_en_hijo()` es lo
primero que corre cada proceso hijo. Pone SIGINT/SIGTERM en `SIG_IGN` porque en
Linux el Ctrl+C de la terminal le llega a **todo el grupo de procesos en
foreground**, no sólo al padre: sin esto, un Ctrl+C mataría a cada analizador a
mitad de una escritura al Manager en vez de dejar que `main` coordine. También
pone HUP/USR1/USR2/WINCH en `SIG_IGN`, porque son señales dirigidas al monitor
como un todo y si quedaran con el handler heredado del padre cada hijo
escribiría a un pipe que nadie lee de su lado.

La única excepción es el Display, que pide dejar **SIGWINCH en `SIG_DFL`**:
ncurses instala su propio handler de resize sólo si encuentra la disposición en
`SIG_DFL`, y de él depende que `getmaxyx()` devuelva el tamaño nuevo al
redimensionar la terminal. Si lo dejara en `SIG_IGN` "por prolijidad" rompería
el redibujado.

### 3.5 El "Agregador": por qué no es un proceso mío

La consigna lista un componente **Agregador** que "mantiene el snapshot global
en memoria compartida". En este diseño ese componente **existe y es un proceso
real, pero no lo escribí yo**: es el proceso servidor que
`multiprocessing.Manager()` arranca internamente. El `snapshot` que ven los
analizadores es un *proxy*; cada `snapshot[clave] = valor` es una llamada RPC
por un socket Unix a ese proceso, que es el único que sostiene el diccionario
de verdad en su memoria. Se lo puede ver en la propia TUI: aparece en la lista
como un `python3` hijo de `main` que no es ninguno de los 9 que lanzo yo.

No escribí una capa de agregación propia porque, al diseñar cada analizador
para que toque **sólo su propia clave**, la fusión de las 7 dimensiones en un
snapshot coherente queda resuelta por construcción: no hay nada que combinar.
El precio de esa decisión es explícito: no hay un punto único donde validar o
enriquecer los datos antes de publicarlos, y si quisiera (por ejemplo)
descartar un snapshot incompleto tendría que agregar ese proceso.

El único analizador que podría leer el resultado de otro es `sistema.py`, para
el top 3 por CPU. **Elegí que no lo haga**: mantiene su propio historial de
jiffies por PID, duplicando trabajo, a cambio de que ningún analizador dependa
de que otro haya corrido antes ni de su intervalo de refresco. Si `sistema`
(2 s) leyera el CPU% de `resumen` (2 s pero ajustable a 0.5 s o a 10 s con
`+`/`-`), el top 3 mostraría datos calculados sobre una ventana temporal
distinta a la que dice su propio `ts`.

### 3.6 `pid: host`: por qué acepto romper el aislamiento

Sin `pid: host`, el contenedor tiene su propio PID namespace y el monitor ve
únicamente sus 11 procesos. Funciona perfecto, pero el "sistema" que muestra
es un contenedor casi vacío: todos los procesos del mismo usuario, ningún
zombie, ningún kernel thread, nada que filtrar con `u`.

Con `pid: host` ve el sistema real, que es lo que hace que las 7 vistas tengan
algo interesante que mostrar. El costo es concreto y no lo quiero esconder:
requiere `cap_add: SYS_PTRACE` para leer `maps`/`fd` de procesos ajenos, y es
lo opuesto al aislamiento que normalmente se busca en Docker.

Me quedé con `pid: host` en el servicio por defecto, pero dejé el mismo monitor
**sin** esos privilegios como segundo servicio (`monitor-aislado`, ver
`docker-compose.yml`), porque comparar los dos es en sí mismo la demostración
de qué es un PID namespace: la misma imagen, el mismo código leyendo el mismo
`/proc`, y dos realidades distintas según en qué namespace corra.

```bash
docker compose up --build                        # ve el host (~cientos de procesos)
docker compose run --rm monitor-aislado          # ve sólo los ~10 propios
```

---

## 4. Conceptos del curso aplicados

- **Clase 3 (procesos, `/proc`, memoria virtual):** la vista Memoria sale de
  `/proc/<pid>/status` (VmSize/VmRSS/VmData/VmStk/VmExe/VmLib/VmHWM/VmSwap) y
  de `/proc/<pid>/maps`, que agrupo en `procfs.agrupar_segmentos()`
  distinguiendo heap (`[heap]`), stack (`[stack]`), segmentos compartidos (bit
  `s` en los permisos) y código (`x` en los permisos) — la separación
  texto/datos/pila que vimos en la anatomía de un proceso en memoria virtual.

- **Clase 4 (fork/exec/wait, zombies, COW):** en la vista Sistema cuento
  zombies mirando el campo `State` de `/proc/<pid>/stat` (carácter `Z`). Un
  zombie es un proceso que ya llamó a `exit()` pero cuyo padre todavía no hizo
  `wait()`/`waitpid()`, así que el kernel mantiene su entrada en la tabla de
  procesos sólo para poder entregarle el código de salida al padre. La
  consecuencia se ve en mi propia vista Memoria: un zombie muestra VmRSS ≈ 0,
  porque su espacio de direcciones ya fue liberado — lo único que queda es el
  `task_struct`.

- **Clase 5 (pipes, FDs, IPC básico):** la vista de File Descriptors lista
  literalmente `/proc/<pid>/fd/*` y usa `os.readlink()` para ver a dónde apunta
  cada uno, clasificando por el formato del symlink (`pipe:[N]`, `socket:[N]`,
  `anon_inode:`, `/dev/pts/N`). En modo verbose muestro además el inodo `N`, y
  ahí se ve lo de la clase en un sistema vivo: dos procesos distintos con FDs
  distintos apuntando a `pipe:[N]` con **el mismo N** son los dos extremos del
  mismo pipe. Con `pid: host` se puede encontrar exactamente eso entre mi
  proceso `main` y sus analizadores, que están unidos por `Pipe()`.

- **Clase 6 (señales, handlers, máscaras, async-signal-safe, self-pipe):** es
  la clase que más código directo generó. `src/senales.py` implementa el
  self-pipe para las 6 señales, y la vista Señales decodifica las máscaras de
  64 bits de `SigBlk/SigIgn/SigCgt/SigPnd/ShdPnd` bit a bit. Decodificarlas me
  llevó a un detalle que no esperaba: aparecen bits por encima de 31 que no
  corresponden a ninguna señal POSIX y que están por debajo de `SIGRTMIN`. Son
  internas de NPTL (la implementación de threads de glibc): la 32 es
  `SIGCANCEL` (`pthread_cancel()`) y la 33 es `SIGSETXID` (propagar
  `setuid()`/`setgid()` a todos los threads, porque el kernel sólo cambia las
  credenciales del thread que hace la syscall). Y son la razón exacta de por
  qué `SIGRTMIN` vale 34 y no 32: el kernel expone `__SIGRTMIN = 32`, pero
  glibc se reserva esas dos y corre el rango público dos lugares para arriba.
  Antes de escribir esto creía que los dos bits aparecían en casi cualquier
  proceso, así que lo conté: sobre 30 procesos con glibc 2.39, `SIGSETXID`
  aparece en 5 (4 de los 6 multithread) y `SIGCANCEL` **en ninguno** — en glibc
  moderna la cancelación de threads dejó de dejar un handler registrado en
  `SigCgt`. Mapeo los dos nombres igual, porque el bit sigue definido y puede
  aparecer en otra libc, pero no puedo afirmar que se vea en la práctica acá.

- **Clase 7 (mmap y memoria compartida):** `Value` y `Array` son, por debajo,
  `mmap` anónimo compartido entre procesos con `fork()` — memoria compartida de
  verdad. `Manager`, en cambio, usa un socket Unix hacia un proceso servidor,
  porque necesita serializar estructuras arbitrarias de Python y no sólo tipos
  C. Elegir cuál usar para cada dato (§3.1) es aplicar directamente esa
  distinción: los 3 enteros del top 3 van a `Array` porque el encabezado los
  lee en cada frame; el snapshot anidado va a `Manager` porque no hay forma de
  meterlo en un mmap de tipos C sin escribir yo el serializador.

- **Clase 8 (`Process`, `Queue`, `Pipe`, daemons):** los 9 hijos son
  `multiprocessing.Process` directo, no `Pool`, porque cada analizador vive
  para siempre con su propio ritmo en vez de tomar una tarea de una cola y
  terminar. `log_queue` es una `Queue` multi-productor / un-consumidor.
  Deliberadamente **no** marco los procesos como `daemon=True`: un proceso
  daemon de multiprocessing se mata automáticamente cuando muere el padre, sin
  chance de hacer su propio cierre, y acá quiero coordinar el apagado yo con
  `stop_event`.

- **Clase 9 (`Pool`, `Manager`, `Value`, `Array`):** el snapshot usa
  `Manager().dict()`, los intervalos y verbose usan `Value`, el top 3 usa
  `Array`. Cada uno elegido por el motivo de §3.1, no por default.

- **Clase 10 (threading, GIL, threads como LWPs en `/proc/<pid>/task`):** la
  vista Threads lista `/proc/<pid>/task/<tid>/` — el mismo mecanismo con el que
  el kernel expone los threads como "procesos livianos" que comparten espacio
  de direcciones pero tienen su propio `stat`/`status`, y por lo tanto su
  propio `utime`/`stime`. Eso es lo que me permite calcular CPU% **por thread**
  y no sólo por proceso, y por eso el CPU% de un proceso puede pasar de 100%:
  no divido por la cantidad de cores (modo "Irix" de top, no "Solaris"),
  justamente para que se vea que varios LWPs corren en paralelo en cores
  distintos. El TID igual al PID es el thread principal.
  El GIL no afecta a los procesos que monitoreo (viven fuera de mi
  intérprete), pero **sí** es la razón por la que los 7 analizadores tienen que
  ser procesos y no threads: son CPU-bound (parsean cientos de archivos de
  texto por vuelta), y como threads se serializarían sobre un solo core.

---

## 5. Limitaciones conocidas

- **Permisos entre usuarios distintos:** `/proc/<pid>/maps` y `/proc/<pid>/fd/`
  sólo son legibles por el dueño del proceso o con `CAP_SYS_PTRACE`. El
  contenedor corre como root y con `cap_add: SYS_PTRACE`, pero si se corre sin
  esos privilegios esos dos analizadores muestran `"sin permiso"` para procesos
  ajenos en vez de romperse. Está manejado, pero **sí** es una limitación real
  de qué se puede ver.
- **`Nice` sale sólo de `stat`.** La consigna ofrece `/proc/<pid>/stat` campo
  19 **o** `/proc/<pid>/status: Nice`. La segunda opción no existe: `status` no
  tiene ni tuvo nunca una línea `Nice:` (`grep -i nice /proc/self/status` no
  devuelve nada, en ningún kernel — no es una cuestión de versión). Uso el
  campo 19 de `stat`, que es la única fuente real.
- **CPU% en la primera lectura de un PID nuevo:** siempre muestra `0.0`, porque
  no hay lectura anterior con la que calcular el delta de jiffies. Se corrige
  en la vuelta siguiente de ese analizador. Lo mismo pasa si un PID
  desaparece transitoriamente de una vuelta: se purga su historial y el
  siguiente cálculo arranca de cero.
- **Terminal chica:** con menos de ~20 líneas o ~80 columnas los paneles de
  detalle se recortan. Uso `addnstr` para no crashear, pero el contenido se
  corta y no implementé scroll horizontal ni vertical en el panel de detalle
  (sí en la lista de procesos).
- **Escalabilidad: lectura duplicada y volumen de FDs.** Como cada analizador
  es autónomo y lee lo que necesita, hay archivos que se abren varias veces por
  ciclo: `/proc/<pid>/status` lo leen **5** analizadores (resumen, memoria,
  senales, scheduling y sistema) y `/proc/<pid>/stat` lo leen **4**. Con N
  procesos vivos eso es 5N y 4N lecturas por ciclo completo en vez de N y N.
  Lo atenúa que cada uno corra a su propio ritmo (los de 10 s aportan poco),
  pero es trabajo duplicado real, y es el precio explícito del desacople entre
  analizadores (ver §3.5 y `dudas.md` §2).
  Aparte de eso, el analizador de FDs es el cuello de botella por volumen, no
  por duplicación: hace un `listdir` más un `readlink` **por cada FD de cada
  proceso** en cada vuelta. De ahí su intervalo de 5 s y el tope de 20 FDs por
  proceso en modo no verbose. El arreglo real —que el recolector reparta los
  PIDs entre varios workers por una `Queue`— no lo implementé.
- **Si un analizador muere, nadie lo relanza.** Si un analizador se cae por una
  excepción no capturada, su clave del snapshot deja de actualizarse: el `ts`
  queda viejo y los datos congelados. El Display **no** muestra hoy un aviso de
  "vista congelada" comparando ese `ts` contra el reloj, así que se nota sólo
  porque los números dejan de cambiar. `main.py` tampoco relanza analizadores
  caídos.
- **`config.json` sólo aplica en caliente los intervalos y `fds.max_mostrados`.**
  Campos nuevos requieren extender `Orquestador.reload_config` y el analizador
  correspondiente para leerlos del broadcast por Pipe.
- **Sin persistencia entre corridas:** los historiales de CPU% viven en la
  memoria de cada analizador y arrancan vacíos. No es un requisito del TP.
- **`pid: host` en Docker Desktop (Windows/macOS)** muestra los procesos de la
  VM de Linux, no los del sistema operativo del host. En Linux nativo sí son
  los procesos reales de la máquina.
- **La vista Sistema muestra dos conteos de procesos que pueden no coincidir**,
  y es a propósito: `Procesos: N` sale de recorrer `/proc/<pid>/` uno por uno,
  mientras que el `(corriendo X/Y)` del load average sale del campo 4 de
  `/proc/loadavg`, que el kernel calcula por su cuenta. En un kernel normal `Y`
  debería ser la cantidad de tareas (threads) existentes. Corriendo esto sobre
  WSL2 medí `1/309` en loadavg cuando `ps -eL` contaba 52 tareas y mi recorrido
  de `/proc` daba 34 procesos — o sea que el `Y` de WSL2 no es confiable. Mi
  conteo sí lo es: coincide exactamente con `ps -e`. Decidí mostrar los dos
  números en vez de "arreglar" el de loadavg, porque la discrepancia es
  información sobre el entorno, no un error de parseo.

---

## 6. Cómo correr y testear

### Levantar el monitor

```bash
git clone https://github.com/maxinavesi1/computacion2-2026.git
cd computacion2-2026/tp1
docker compose up --build
```

Un solo comando, como pide la consigna. Para salir: tecla `q`, o `Ctrl+C`.

### Ver la diferencia de PID namespace

```bash
docker compose run --rm monitor-aislado
```

El mismo monitor sin `pid: host` ni `SYS_PTRACE`: sólo ve sus propios ~10
procesos. Ver §3.6.

### Modo daemon (sin TUI, bonus — §10)

```bash
docker compose run --rm monitor python3 src/main.py --daemon
```

Loguea una línea de resumen a `monitor.log` cada `daemon_intervalo_log`
segundos, sin levantar curses.

### Probar las señales

Desde otra terminal, buscando el PID del proceso `main`:

```bash
docker compose exec monitor pgrep -f "src/main.py"     # el PID más chico es main
docker compose exec monitor kill -USR1 <pid>   # dump a dump_<timestamp>.json
docker compose exec monitor kill -USR2 <pid>   # toggle verbose (mirá la vista FDs)
docker compose exec monitor kill -HUP  <pid>   # recarga config.json
docker compose exec monitor kill -TERM <pid>   # shutdown limpio
```

Para ver SIGHUP haciendo algo visible: editá `"fds": {"max_mostrados": 5}` en
`config.json` (el bind mount hace que el contenedor lo vea al instante), mandá
SIGHUP, activá verbose con SIGUSR2 y mirá cómo se recorta la lista de FDs.

### Correr los tests

No requieren Docker ni privilegios (son sobre fixtures de texto y sobre el
estado de la TUI, nunca sobre `/proc` real), pero sí Linux, porque `procfs.py`
importa `pwd`/`grp`:

```bash
docker compose run --rm monitor python3 -m unittest discover -s tests -v
```

**53 tests** en dos archivos:

- `tests/test_procfs.py` (23): parseo de `stat` —incluyendo el caso límite de
  un `comm` con espacios y paréntesis propios—, de `status`, de `maps` y su
  agrupamiento por categoría, decodificación de máscaras de señales (con las
  internas de glibc y las RT) y cálculo de CPU%.
- `tests/test_ui.py` (30): la tabla de vistas contra los valores de la
  consigna, el mapeo de las 14 teclas de vista, el clamp de intervalos
  (incluida la regresión del piso que no respetaba SIGHUP), el ciclo de orden,
  los filtros con su cancelación por ESC, el pin manteniéndose primero pese a
  cambiar el criterio de ordenamiento, y el overlay de ayuda (que cierre con
  cualquier tecla y que no crezca más allá de una terminal de 24 filas).

### Qué se verificó y cómo

| Verificación | Resultado |
|---|---|
| 53 tests unitarios (Linux nativo y dentro del contenedor) | OK |
| `list_pids()` contra `ps -e` | 34 = 34, coinciden exactamente |
| Campos 5/6 de `stat` contra `ps -o pgid,sid` | coinciden (ver el docstring de `procfs.py`) |
| `grep -i nice /proc/self/status` | 0 coincidencias: el campo no existe |
| Sistema completo en modo daemon | 9 hijos, shutdown limpio, 0 huérfanos |
| SIGUSR1 | dump de ~143 KB, JSON válido, 7 claves |
| SIGHUP | los 7 analizadores lo registran en el mismo segundo |
| SIGUSR2 | las 7 vistas pasan a `verbose: true` en < 1 s |
| `read_fds` con permisos ajenos | 30 de 46 pids devuelven `SIN_PERMISO`, ninguno confundido con "murió" |
| `docker compose build` | OK |
| terminfo en el contenedor | 1844 entradas; `setupterm` OK en 12 TERM distintos |
| `pid: host` vs `monitor-aislado` | 24 procesos y 2 usuarios vs 12 procesos y 1 usuario |
| Arranque de la TUI y las 14 teclas de vista | las 7 vistas pintan barra y panel de detalle |
| `+`/`-`, `c`, `/`, `↑↓`, `Enter`, `h`, `q` | todas responden; `q` sale con código 0 y 0 huérfanos |

La TUI la verifiqué de dos maneras. Los 30 tests de `test_ui.py` cubren la
lógica de teclado sobre `EstadoUI` sin abrir curses. Además la corrí de verdad
bajo una pseudo-terminal de 24×100 (módulo `pty` de la stdlib), mandándole
teclas y leyendo la salida a través de un emulador VT100 para poder afirmar qué
quedaba efectivamente en pantalla en cada paso. Ese arnés vive fuera del repo
porque depende de una librería que no es del TP, pero fue lo que hizo aparecer
el bug del panel de ayuda que se describe en §8.

### Probar `procfs.py` contra el `/proc` real

```bash
docker compose run --rm monitor python3 -c \
  "import sys; sys.path.insert(0,'src'); import procfs, os; print(procfs.read_stat(os.getpid()))"
```

---

## 7. Ejemplo de uso

*Salida real de una corrida, transcripta como texto (todavía me debo un GIF).*

Vista File Descriptors, con el proceso `main` seleccionado:

```
 Monitor /proc — vista: File Descriptors — refresco: 5.0s
verbose:off | top-cpu:651(3.0%) | top-mem:502(34MB) | orden:cpu
---------------------------------------------------------------------------
   PID USER          PPID S   CPU%  RSS(MB)  THR  COMANDO
   651 root           631 S    3.0     15.4    1  python3 src/main.py
   645 root           631 S    1.5     15.5    1  python3 src/main.py
     1 root             0 S    1.0      4.6    7  /process_api --firecracke
   632 root           631 S    1.0     16.2   11  python3 src/main.py
=============================================================================
PID 651 — 35 FDs abiertos (tty:3, file:3, pipe:14, socket:15)
(mostrando los primeros 20 de 35 — SIGUSR2 activa el modo verbose para ver más)

  fd    0  [tty       ]  /dev/pts/0
  fd    1  [tty       ]  /dev/pts/0
  fd    2  [tty       ]  /dev/pts/0
  fd    3  [file      ]  /dev/shm/pym-631-mne10_hw (deleted)
  fd    5  [pipe      ]  pipe:[2249]
  fd    6  [pipe      ]  pipe:[2273]
1-7/rmftspg vista | ↑↓ nav | Enter pin | / cmd | u user | c orden | +/- intervalo | h ayuda | q salir
```

Los `pipe:[2249]` / `pipe:[2273]` son los `Pipe()` de reload de config: con
SIGUSR2 activado se ve el inodo al lado y se puede confirmar que el otro
extremo aparece en el proceso analizador correspondiente.

---

## 8. Decisiones sobre la TUI

- **`curses` en vez de `rich`:** no suma una dependencia externa (viene con
  Python en Linux) y da control directo sobre `getch()` no bloqueante
  (`stdscr.timeout(150)`), que es exactamente lo que necesitaba para "leer como
  mucho una tecla por frame y seguir refrescando".
- **Sin thread de teclado:** `curses` no es thread-safe; mezclar `getch()` en un
  thread con `refresh()`/`addstr()` en otro es una fuente clásica de corrupción
  de pantalla. Un único loop con `timeout` no bloqueante cumple el mismo
  requisito funcional con mucho menos riesgo. La consigna aclara que el thread
  de teclado no es obligatorio.
- **El manejo de teclado está separado del dibujado.** `_manejar_tecla()` sólo
  muta `EstadoUI` y `_dibujar()` sólo lee. Además de ser más fácil de razonar,
  es lo que hace testeable la TUI sin abrir curses ni una pty: `test_ui.py`
  inyecta keycodes directamente y verifica el estado resultante.
- **`noutrefresh()` + `doupdate()` en vez de `refresh()` por ventana.** El panel
  de ayuda es la única cosa que vive en una ventana propia (`curses.newwin`), y
  ahí aprendí por qué curses distingue las dos operaciones. Refrescar dos
  ventanas superpuestas por separado no funciona: `refresh()` vuelca a la
  terminal *y* actualiza el modelo interno que ncurses tiene de la pantalla
  física, así que el segundo `refresh()` ve el panel como contenido que sobra y
  lo repinta con la ventana de abajo. Con la ayuda dibujada antes del
  `stdscr.refresh()`, `h` no mostraba absolutamente nada; dibujándola después
  aparecía pero parpadeando en cada frame. `noutrefresh()` sólo toca el buffer
  virtual y `doupdate()` compone todo en una única escritura física, que es
  exactamente el mecanismo que curses ofrece para superponer ventanas sin
  parpadeo.
- **La ayuda entra en 24×80 y se cierra con cualquier tecla.** Está en dos
  columnas (21×68 con el marco) porque en una sola columna se pasaba de 24
  filas y el recorte se comía justo la línea que explica cómo cerrarla. Y la
  primera tecla que se aprieta la cierra y se consume ahí, en vez de cambiar
  de vista por detrás del overlay.
- **Layout 45/55 (lista/detalle):** la lista de procesos queda siempre visible,
  cualquiera sea la vista activa, como pide la consigna; el panel de detalle se
  lleva la mayoría del espacio vertical porque es donde vive la información
  específica de cada vista.
- **El proceso pineado va siempre primero, fuera del criterio de sort:** así
  cumplo literalmente "no cambia aunque cambie el orden". En vez de tratar de
  seguir el índice de una fila que se mueve, saco al pineado del sort y lo fijo
  en la fila 0. Si el proceso muere, el pin se suelta solo.
- **El intervalo vigente está en el título.** Muestro `refresco: 5.0s` y un
  `(mín)` cuando se toca el piso, para que `+`/`-` no parezcan no hacer nada
  cuando la vista ya está en su mínimo.
- **Top-CPU/top-mem siempre visibles en el encabezado,** leídos de los `Array`
  de tamaño fijo y no del snapshot, para que no dependan de la vista activa ni
  hagan una lectura al Manager en cada frame.

---

## 9. Lo que aprendí

Lo que más me cambió la forma de pensar el TP fue darme cuenta de que
"memoria compartida" son en realidad dos cosas muy distintas que
`multiprocessing` presenta con una API parecida. `Value` y `Array` son un `mmap`
anónimo: los procesos escriben y leen las mismas páginas físicas de RAM, y por
eso leer los 3 enteros del top 3 en cada frame no cuesta nada. `Manager().dict()`
parece lo mismo desde el código, pero cada `snapshot[clave] = valor` es una
llamada RPC por un socket Unix a otro proceso, con pickle de ida y de vuelta.
La diferencia no es un detalle de implementación: define qué primitiva
conviene para cada dato y, sobre todo, qué race conditions son posibles. Un
`Array` de tamaño fijo con lock es trivial de auditar; un dict compartido con
estructuras anidadas me obligó a pensar explícitamente, para cada escritura, si
alguien podía estar leyendo a mitad de camino — y de ahí salió la regla de
"reasignar el dict entero de una sola vez, nunca mutarlo campo por campo", que
es lo que evita el torn read.

Lo segundo fue entender por qué las señales necesitan el patrón self-pipe. Es
tentador escribir la lógica de "recargar config" o "volcar el snapshot"
directamente adentro del handler; recién cuando pensé en *dónde* puede
interrumpir una señal se me hizo evidente el problema. El handler puede
interrumpir **cualquier** línea, incluida una que ya estaba a mitad de una
llamada al Manager, con sus locks internos tomados. Si desde el handler llamo
otra vez al Manager, me estoy reentrando a mí mismo. El self-pipe resuelve eso
reduciendo el handler a la única operación que sí es async-signal-safe —un
`os.write()` de un byte— y moviendo todo el trabajo real al loop principal.
Por el mismo razonamiento terminé moviendo la instalación de los handlers a
antes del `fork()`, y agregando un reset explícito en cada hijo: la disposición
de señales es parte del estado que `fork()` copia, y eso se me pasó por alto en
la primera versión.

Lo tercero fue más chico pero es lo que más me gustó: `/proc` expone el estado
real del kernel, con todos sus detalles de implementación adentro, no una API
prolija pensada para consumo público. Los bits 32 y 33 de las máscaras de
señales aparecían seteados en casi todos los procesos y no correspondían a nada
en `signal.h`. Investigarlos me llevó a `SIGCANCEL`/`SIGSETXID`, internas de
glibc, y de ahí a entender por qué `SIGRTMIN` vale 34 y no 32 — algo que había
visto como un número mágico durante todo el desarrollo y que resultó ser
consecuencia directa de esos dos bits. Es exactamente el tipo de cosa que una
librería como `psutil` te habría "limpiado" sin que te enteres.

---

## 10. Extensiones implementadas (bonus)

- **Modo daemon** (`python3 src/main.py --daemon`): el monitor completo sin
  TUI, loggeando un resumen periódico del snapshot a `monitor.log`. Usa la
  misma arquitectura multiproceso; sólo cambia qué proceso *no* se lanza
  (`display`) y qué hace el loop principal con el tiempo que antes usaba para
  el teclado. El loop de daemon procesa igual el self-pipe, así que las 5
  señales siguen funcionando sin TUI.
- **Tests unitarios** (53, en `tests/`): sobre fixtures de texto estáticos y
  sobre el estado de la TUI, sin depender de `/proc` real ni de privilegios.
  Incluyen un test de regresión del piso de intervalos que SIGHUP no respetaba.
- **Comparación de PID namespaces** (`monitor-aislado` en `docker-compose.yml`):
  el mismo monitor con y sin `pid: host`, para ver en vivo qué cambia.
- **Volcado a JSON bajo demanda:** SIGUSR1 ya era obligatorio, pero además
  quedó disponible programáticamente (`Orquestador.dump_snapshot`) para poder
  extenderlo a un dump periódico. No lo dejé automático para no llenar el disco
  de archivos sin que el usuario lo pida.

No implementé el resto de las extensiones sugeridas (histórico con
mini-gráficos ASCII, detección de anomalías, vista de jerarquía tipo `pstree`,
comparativa cross-proceso).
