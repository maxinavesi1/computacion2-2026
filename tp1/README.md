# TP Nº1 — Monitor de Procesos y Threads

**Computación II — Universidad de Mendoza — 2026**

Monitor de sistema en tiempo real (estilo `htop`) que lee `/proc` directamente,
sin `psutil`. Arquitectura multiproceso: un recolector, 7 analizadores
independientes (cada uno con su propio intervalo de refresco) y una TUI en
`curses` con 7 vistas.

---

## Índice

1. [Descripción general](#1-descripción-general)
2. [Arquitectura](#2-arquitectura)
3. [Decisiones de diseño](#3-decisiones-de-diseño)
4. [Conceptos del curso aplicados](#4-conceptos-del-curso-aplicados)
5. [Limitaciones conocidas](#5-limitaciones-conocidas)
6. [Cómo correr y testear](#6-cómo-correr-y-testear)
7. [Ejemplo de uso](#7-ejemplo-de-uso)
8. [Decisiones sobre la TUI](#8-decisiones-sobre-la-tui)
9. [Lo que aprendí](#9-lo-que-aprendí)
10. [Extensiones (bonus)](#10-extensiones-bonus)

---

## 1. Descripción general

El monitor levanta 11 procesos: `main` (orquestador), el proceso servidor del
`Manager`, un recolector, 7 analizadores (uno por vista) y el display. Cada
analizador lee **sólo** `/proc` —a través de `procfs.py`, el único módulo que
toca el filesystem— y escribe su resultado en una estructura de memoria
compartida (`snapshot`) que la TUI lee para pintar las 7 vistas.

### Cómo levantarlo

```bash
docker compose up -d --build
docker attach tp1-monitor
```

Son dos comandos porque `docker compose up` conecta la salida del contenedor a
la pantalla pero no el teclado. `docker attach` conecta lo que falta. Como
atajo de un solo comando: `docker compose run --rm monitor`.

### Teclas

| Tecla | Acción |
|---|---|
| `1`..`7` o `r m f t s p g` | Cambiar de vista |
| `↑` `↓` | Navegar la lista de procesos |
| `Enter` | Pinnear/despinnear el proceso seleccionado |
| `/` | Filtrar por nombre de comando |
| `u` | Filtrar por usuario |
| `c` | Ciclar orden: CPU% → RSS → PID |
| `+` / `-` | Ajustar el intervalo de refresco de la vista activa |
| `h` / `?` | Ayuda |
| `q` | Salir |

Señales que acepta `main` (`kill -SIGXXX <pid>`): `SIGINT`, `SIGTERM`,
`SIGHUP`, `SIGUSR1`, `SIGUSR2`, `SIGWINCH`.

---

## 2. Arquitectura

```
                    MAIN (orquestador)
         instala señales, crea memoria compartida,
              lanza recolector + analizadores
     ┌─────────┬─────────┬─────────┬─────────┬─────────┬──────────┬─────────┐
Recolector  Resumen   Memoria     FDs      Threads   Señales  Scheduling  Sistema
   1s          2s        3s        5s         2s        10s        10s       2s
     │           └────────┴─────────┴──────────┴──────────┴──────────┴─────────┘
     ▼                cada uno escribe SÓLO su propia clave del snapshot
 pids_proxy                                │
(Manager.list)                             ▼
     │                    SNAPSHOT GLOBAL (Manager().dict)
     └──── lo leen ────►   resumen · memoria · fds · threads ·
                           señales · scheduling · sistema
                                            │
                                     sólo lee │  Array('i',3): top3_cpu/mem
                                            ▼
                                     Display (TUI, curses)
                                            │
                                escribe intervalos con +/-
                                            ▼
                       Value('d') por vista (mismo que lee cada analizador)

Aparte del flujo de datos:
· Queue: cualquier hijo -> main, errores/eventos
· Pipe (uno por analizador): main -> analizador, config nueva en SIGHUP
· Value('b'): main -> analizadores + display, togglea SIGUSR2 (verbose)
· Event: main -> todos, shutdown coordinado
```

---

## 3. Decisiones de diseño

### 3.1 Por qué cada mecanismo de IPC

| Mecanismo | Para qué | Por qué |
|---|---|---|
| `Manager().dict()` (`snapshot`) | Los 7 resultados de los analizadores | Son estructuras anidadas de tamaño variable. `Value`/`Array` sólo sirven para tipos fijos; el `Manager` serializa (pickle) por mí. |
| `Manager().list()` (`pids_proxy`) | La lista de PIDs vivos | El largo cambia en cada vuelta. Se reemplaza entera (`pids_proxy[:] = nuevos`), nunca elemento a elemento. |
| `Value` (intervalos, `verbose`) | Escalares simples | Es memoria compartida real (mmap), mucho más liviana que un Manager para un solo número. La consigna la pide para el ajuste de intervalos en vivo. |
| `Array('i', 3)` (top 3 CPU/mem) | Tamaño fijo y conocido | Evita una consulta al Manager para leer 3 enteros que la TUI necesita en cada frame. |
| `Queue` (`log_queue`) | Errores de cualquier hijo → 1 consumidor | Es el patrón para el que está pensada: muchos productores, un consumidor. |
| `Pipe` (uno por analizador) | `main` reenvía config nueva en SIGHUP | Es 1 a 1 con ese analizador puntual; más liviano que una Queue por analizador. |
| `Event` (`stop_event`) | Shutdown coordinado | `wait()` interrumpible, mejor que un `Value` con polling manual. |

Los cinco mecanismos que pide la consigna están usados, cada uno por un motivo
distinto.

### 3.2 Race conditions

- **Cada analizador escribe sólo su propia clave** del snapshot, y siempre
  reemplaza el diccionario entero de una vez (`snapshot["memoria"] = {...}`),
  nunca campo por campo. Así el Display nunca lee algo a medio escribir.
- **La lista de PIDs** se lee una sola vez al principio de cada vuelta
  (`pids = list(pids_proxy)`) y se trabaja sobre esa copia, no sobre el proxy
  en vivo.
- **TOCTOU de `/proc`:** un proceso puede morir entre que el recolector lo
  lista y el analizador lo lee. `procfs.py` atrapa esos errores y devuelve
  `None`; el analizador descarta ese PID en esa vuelta.
- **`top3_cpu_arr`/`top3_mem_arr`** se escriben con `arr.get_lock()`. Se leen
  sin lock a propósito: el peor caso es ver el array a mitad de escribir
  durante un frame, que se corrige solo en el siguiente.
- **Shutdown:** todos los loops duermen con `stop_event.wait(...)` en vez de
  `time.sleep(...)`, así un `stop_event.set()` los despierta al instante en
  vez de esperar a que termine el sueño y forzar un `terminate()` a mitad de
  una escritura.
- Los handlers de señal se instalan **antes** de crear el Manager y forkear a
  los hijos, para no dejar una ventana donde un Ctrl+C temprano deja procesos
  huérfanos.

### 3.3 Intervalos por defecto

De más frecuente/liviano a menos frecuente/pesado, según el costo real de leer
cada archivo de `/proc`:

- **Resumen, Sistema, Threads (2s):** lecturas chicas (`stat`+`status`).
- **Memoria (3s):** además parsea `/proc/<pid>/maps`, que puede tener cientos
  de líneas.
- **FDs (5s):** hace un `readlink()` por cada file descriptor abierto de cada
  proceso — el más caro si hay procesos con muchos FDs.
- **Señales y Scheduling (10s):** casi no cambian salvo que alguien llame a
  `sigprocmask()` o `sched_setscheduler()` explícitamente.
- **Recolector (1s):** más rápido que la vista más exigente, para que la
  lista de PIDs esté fresca.

Los valores (default y mínimo) están en `src/vistas.py`, un solo lugar que
usan tanto el teclado (`+`/`-`) como el reload por SIGHUP.

### 3.4 Señales del monitor

Implementadas con el patrón **self-pipe** (`src/senales.py`): el handler de
cada señal sólo hace un `os.write()` de 1 byte a un pipe interno (lo único
async-signal-safe); el trabajo real lo hace el loop principal cuando ve ese
byte, fuera del handler.

- **SIGINT/SIGTERM** → shutdown: se apagan los 9 hijos y recién después el
  Manager.
- **SIGHUP** → relee `config.json` y actualiza los intervalos y la config de
  cada analizador (vía su `Pipe`).
- **SIGUSR1** → vuelca el snapshot a `dump_<timestamp>.json`.
- **SIGUSR2** → togglea un `Value('b')` (verbose) que leen los 7 analizadores:
  cambia los datos que recolectan (más FDs por proceso, más detalle de
  memoria y threads), no sólo el cartel de la TUI.
- **SIGWINCH** → no hace falta reenviarlo: `curses` recalcula el tamaño de la
  terminal en cada frame solo.

Los procesos hijos ignoran SIGINT/SIGTERM al arrancar (`senales.reset_en_hijo`),
porque en Linux un Ctrl+C en la terminal le llega a todo el grupo de procesos:
sin esto, cada analizador podría morir a mitad de una escritura al Manager en
vez de dejar que `main` coordine el apagado.

### 3.5 El "Agregador"

La consigna pide un componente que "mantenga el snapshot en memoria
compartida". Ese componente existe, pero no lo escribí yo: es el proceso
servidor que `multiprocessing.Manager()` arranca internamente (se ve en la
propia TUI como un `python3` extra). No hice una capa de agregación propia
porque cada analizador escribe sólo su clave, así que combinar las 7
dimensiones queda resuelto por construcción. Es una decisión documentada como
duda abierta en `dudas.md`.

### 3.6 `pid: host`

Sin `pid: host` el contenedor sólo ve sus propios 11 procesos. Con `pid: host`
ve los procesos reales de la máquina, que es lo que hace interesante al
monitor, a costa de romper el aislamiento normal de un contenedor (y de
necesitar `cap_add: SYS_PTRACE` para leer `maps`/`fd` de procesos ajenos). Dejé
un segundo servicio, `monitor-aislado`, sin esos privilegios, para comparar:

```bash
docker compose run --rm monitor              # ve el host
docker compose run --rm monitor-aislado      # ve sólo sus 11 procesos
```

---

## 4. Conceptos del curso aplicados

- **Clase 3 (procesos, `/proc`, memoria virtual):** la vista Memoria sale de
  `/proc/<pid>/status` y de `/proc/<pid>/maps`, agrupado en heap/stack/código/
  datos según los permisos de cada segmento.

- **Clase 4 (fork/exec/wait, zombies):** la vista Sistema cuenta zombies
  mirando el estado `Z` de `/proc/<pid>/stat`. Un zombie ya llamó a `exit()`
  pero su padre no hizo `wait()`, así que el kernel guarda su entrada sólo
  para poder entregarle el código de salida. Se ve en la vista Memoria: un
  zombie muestra RSS ≈ 0 porque ya no tiene espacio de direcciones.

- **Clase 5 (pipes, FDs):** la vista de File Descriptors lista
  `/proc/<pid>/fd/*` con `os.readlink()`. En modo verbose se ve el inodo de
  cada pipe/socket; dos FDs con el mismo `pipe:[N]` son los dos extremos del
  mismo pipe.

- **Clase 6 (señales, self-pipe):** `src/senales.py` implementa el self-pipe.
  La vista Señales decodifica las máscaras de 64 bits bit a bit. Al hacerlo
  aparecieron dos bits (32 y 33) que no son señales POSIX: son internas de
  glibc (`SIGCANCEL`/`SIGSETXID`), y explican por qué `SIGRTMIN` vale 34 y no
  32 (glibc se reserva esas dos).

- **Clase 7 (mmap, memoria compartida):** `Value`/`Array` son mmap real entre
  procesos con `fork()`; `Manager` es un proceso servidor que habla por
  socket. Esa diferencia es la que justifica usar uno u otro según el dato
  (§3.1).

- **Clase 8 (`Process`, `Queue`, `Pipe`):** los 9 hijos son
  `multiprocessing.Process` directo, no `Pool`, porque cada uno vive para
  siempre con su propio ritmo. No son `daemon=True` porque quiero coordinar yo
  el shutdown con `stop_event`.

- **Clase 9 (`Manager`, `Value`, `Array`):** las tres primitivas están usadas,
  cada una por el motivo de §3.1.

- **Clase 10 (threading, GIL, LWPs):** la vista Threads lista
  `/proc/<pid>/task/<tid>/`, el mecanismo con el que el kernel expone los
  threads. Por eso se puede calcular CPU% por thread, y por eso un proceso
  puede superar el 100% (no se divide por cantidad de cores). Los 7
  analizadores son procesos y no threads porque son CPU-bound y el GIL los
  serializaría en un solo core.

---

## 5. Limitaciones conocidas

- **Permisos entre usuarios:** `/proc/<pid>/maps` y `/proc/<pid>/fd/` sólo son
  legibles por el dueño o con `CAP_SYS_PTRACE`. Sin esos privilegios, esos dos
  analizadores muestran "sin permiso" para procesos ajenos.
- **`Nice`:** la consigna ofrece dos fuentes (`stat` o `status`), pero
  `/proc/<pid>/status` nunca tuvo una línea `Nice:`. Se usa siempre el campo
  19 de `stat`.
- **CPU% en `0.0` la primera vez que aparece un proceso**, porque no hay
  lectura anterior para calcular el delta. Se corrige en la vuelta siguiente.
- **Terminal chica:** por debajo de 24 filas el panel de detalle empieza a
  recortarse; por debajo de 14×60 se muestra un cartel en vez de dibujar algo
  ilegible.
- **Listas largas (como `SigBlk`) se truncan** con `…` si no entran en el
  ancho de la terminal; no hay scroll horizontal.
- **Lectura duplicada:** varios analizadores leen `/proc/<pid>/status` o
  `stat` del mismo proceso por separado (cada uno a su ritmo), en vez de
  compartir una sola lectura. Es el precio de que sean independientes entre
  sí.
- **Si un analizador muere, nadie lo relanza**; su vista queda con datos
  viejos sin aviso explícito.
- **`config.json` sólo aplica en caliente los intervalos y `fds.max_mostrados`.**
- **Sin persistencia entre corridas** (los históricos de CPU% viven en
  memoria y arrancan vacíos).
- **La vista Sistema puede mostrar dos conteos de procesos que no coinciden**
  entre sí (el propio y el que reporta `/proc/loadavg`) en algunos entornos
  como WSL2, donde ese campo del kernel no es confiable. El propio conteo del
  monitor sí coincide con `ps -e`.

---

## 6. Cómo correr y testear

```bash
git clone https://github.com/maxinavesi1/computacion2-2026.git
cd computacion2-2026/tp1
docker compose up -d --build
docker attach tp1-monitor
```

Para salir: `q`, o `Ctrl+C`. Si usaste `up -d`, después podés limpiar con
`docker compose down`.

Atajo de un solo comando: `docker compose run --rm monitor`.

> `docker attach` necesita una terminal real (un TTY). No funciona desde un
> panel que sólo muestra logs, como el de la extensión de Docker en VS Code.

### Ver el aislamiento de PIDs

```bash
docker compose run --rm monitor-aislado
```

### Modo daemon (sin TUI)

```bash
docker compose run --rm monitor python3 src/main.py --daemon
```

Loguea un resumen a `monitor.log` cada pocos segundos, sin curses.

### Probar las señales

```bash
docker compose exec monitor pgrep -f "src/main.py"     # el PID más chico es main
docker compose exec monitor kill -USR1 <pid>   # dump a dump_<timestamp>.json
docker compose exec monitor kill -USR2 <pid>   # toggle verbose
docker compose exec monitor kill -HUP  <pid>   # recarga config.json
docker compose exec monitor kill -TERM <pid>   # shutdown limpio
```

### Correr los tests

```bash
docker compose run --rm monitor python3 -m unittest discover -s tests -v
```

87 tests entre `test_procfs.py` (parseo de `stat`/`status`/`maps`, máscaras de
señales, cálculo de CPU%) y `test_ui.py` (teclado, tabla de vistas, layout,
filtros). No requieren `/proc` real ni privilegios, pero sí Linux (porque
`procfs.py` importa `pwd`/`grp`).

---

## 7. Ejemplo de uso

Vista Señales, con las 5 máscaras del PID 1 (`systemd`):

```
 Monitor /proc — vista: Señales — refresco: 10.0s
verbose:off | top-cpu:2343(2.5%) | top-mem:1656(25MB) | orden:cpu
-------------------------------------------------------------------------------
     PID USER          PPID S   CPU%  RSS(MB)  THR  COMANDO
    2355 maxi          2339 R    1.0     16.4    1  python3 src/main.py
    2369 maxi          2339 S    1.0     16.5    1  python3 src/main.py
    2360 maxi          2339 R    0.5     15.9    1  python3 src/main.py
       1 root             0 S    0.0     11.6    1  /sbin/init

===============================================================================
PID 1 — máscaras de señales
Bloqueadas (SigBlk) [7fefc1fe28014a03]:
  SIGHUP, SIGINT, SIGUSR1, SIGUSR2, SIGTERM, SIGCHLD, SIGWINCH, SIGPWR, SIGRTM…
Ignoradas (SigIgn) [0000000000001000]:
  SIGPIPE
Con handler propio (SigCgt) [00000000000004ec]:
  SIGQUIT, SIGILL, SIGABRT, SIGBUS, SIGFPE, SIGSEGV
1-7 vista ↑↓ nav Enter pin / cmd u user c orden +/- int h ayuda q salir
```

Vista Sistema global:

```
CPU global — user: 0.4%  system: 0.2%  idle: 99.4%  iowait: 0.0%
Load average: 0.31 0.08 0.03   (corriendo 3/566)
Memoria — total: 7761 MB  libre: 6583 MB  buffers: 3 MB  cache: 331 MB
Boot time: 2026-08-01 14:46:53   Uptime: 0.8 h
Procesos: 49   Threads totales: 84   Zombies: 0
Top 3 por CPU:                          Top 3 por Memoria:
     2343  python3             2.0%          1656  docker-desktop-     25.1 MB
     2369  python3             1.0%           278  unattended-upgr     22.2 MB
```

Vista FDs con verbose activado (`SIGUSR2`) — se ve el inodo de cada pipe:

```
PID 651 — 35 FDs abiertos (tty:3, file:3, pipe:14, socket:15)
  fd    0  [tty       ]  /dev/pts/0
  fd    5  [pipe      ]  pipe:[2249]  inodo=2249
  fd    6  [pipe      ]  pipe:[2273]  inodo=2273
```

---

## 8. Decisiones sobre la TUI

- **`curses` en vez de `rich`:** viene con Python en Linux, y da control
  directo sobre `getch()` no bloqueante (`stdscr.timeout(150)`), que es lo que
  hace falta para leer una tecla por frame sin trabarse.
- **Sin thread de teclado:** `curses` no es thread-safe, así que un único loop
  con timeout no bloqueante cumple lo mismo sin el riesgo de mezclar
  `getch()` y `refresh()` en threads distintos.
- **El manejo de teclado está separado del dibujado:** `_manejar_tecla()` sólo
  cambia el estado, `_dibujar()` sólo lee. Eso permitió testear la TUI sin
  abrir `curses`, inyectando teclas directamente en los tests.
- **Layout adaptativo:** el reparto lista/detalle es 45/55 por defecto, pero
  en terminales chicas se prioriza que el panel de detalle entre completo
  (hasta cierto mínimo).
- **La selección se ancla al PID, no a la fila.** Si se ancla al índice, al
  pinear un proceso este salta a la fila 0 pero el índice se queda quieto, y
  el detalle termina mostrando otro proceso.
- **El proceso pineado va siempre primero**, fuera del criterio de orden, así
  no cambia de posición aunque cambie el ordenamiento.
- **El intervalo vigente se muestra en el título** (`refresco: 5.0s`), para
  que `+`/`-` no parezcan no hacer nada cuando ya se llegó al mínimo.
- **Top-CPU/top-mem en el encabezado** se leen de los `Array`, no del
  snapshot completo, para no pagar una consulta al Manager en cada frame.

---

## 9. Lo que aprendí

Antes de este TP tenía mezclados en la cabeza "memoria compartida" y "un lugar
donde todos escriben y ya está". Haciendo esto me di cuenta de que `Value`/`Array`
y `Manager` son cosas bastante distintas aunque `multiprocessing` las trate
parecido: los primeros son memoria de verdad, la misma que ven todos los
procesos; el `Manager` en cambio es un proceso aparte con el que hablás por
socket cada vez que leés o escribís algo. Se nota en la velocidad (leer 3
números de un `Array` es gratis, pedirle al Manager el snapshot entero no) y
en cómo pensás las race conditions: con el `Array` es fácil ver qué puede
salir mal, con el `Manager.dict` tuve que pensar dos veces antes de escribir
`snapshot[algo][otra_cosa] = x` porque ahí sí se podía leer algo a medio
armar. Terminé con la regla de reemplazar el diccionario entero de una,
nunca ir tocando campo por campo.

Lo otro que me costó entender de entrada fue por qué no se puede simplemente
escribir el código que hace falta adentro del handler de una señal. Me parecía
lo más directo: llega SIGHUP, releo el config ahí mismo. El problema es que la
señal te puede interrumpir en CUALQUIER lado, incluso a mitad de que el
programa esté hablando con el Manager, y si el handler intenta hablar con el
Manager también, quedás enredado con vos mismo. Por eso el handler solo escribe
un byte a un pipe y todo el trabajo posta lo hace el loop principal después,
afuera de la señal. También me comí un bug por esto: había puesto los handlers
DESPUÉS de crear los procesos hijos, y como fork() copia todo tal cual estaba,
los hijos heredaban el handler del padre sin sentido. Hubo que ponerlo antes.

Lo que más me gustó fue una tontera: al leer las máscaras de señales me
aparecían dos bits prendidos (32 y 33) que no eran ninguna señal normal.
Buscando qué eran resultó que son cosas internas de glibc para manejar
threads (cancelación y cambios de uid), nada que uno vería usando psutil o
algo así. Fue el momento en que más sentí que estaba mirando el sistema
"de verdad" y no una versión ya masticada por una librería.

---

## 10. Extensiones (bonus)

- **Modo daemon:** el monitor completo sin TUI, loggeando a `monitor.log`.
  Usa la misma arquitectura; sólo no se lanza el proceso `display`.
- **87 tests** sobre fixtures de texto y sobre el estado de la TUI, sin
  depender de `/proc` real.
- **Comparación de PID namespaces** (`monitor-aislado`): el mismo monitor con
  y sin `pid: host`.
- **Dump a JSON bajo demanda**, disponible también fuera de la señal
  (`Orquestador.dump_snapshot`).

No implementé el resto de las extensiones sugeridas (histórico con
mini-gráficos, detección de anomalías, vista tipo `pstree`, comparativa
cross-proceso).
