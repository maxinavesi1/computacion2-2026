"""
display.py — la interfaz de texto (TUI) del monitor, implementada con curses
(stdlib, no requiere dependencias externas).

Corre en su PROPIO proceso (multiprocessing.Process), separado de los 7
analizadores. Sólo LEE de memoria compartida (snapshot, pids_proxy, arrays de
top3) y ESCRIBE en un único lugar: los multiprocessing.Value de intervalo,
cuando el usuario aprieta +/-, tal como pide la consigna.

Decisión de diseño: no usamos un thread aparte para leer el teclado. curses
no es thread-safe en general (mezclar getch() en un thread con refresh() en
otro es una fuente clásica de bugs), así que en cambio usamos
`stdscr.timeout(150)` (getch no bloqueante con timeout de 150ms): en cada
vuelta del loop principal leemos como mucho una tecla y repintamos. Esto
cumple el requisito funcional (la UI responde a las teclas) sin la
complejidad extra de sincronizar un thread con la librería de curses.
"""

import curses
import signal
import time

import senales
import vistas

# La tabla de vistas (nombre, teclas, título, intervalo default y mínimo) vive
# en vistas.py, compartida con main.py: antes estaba duplicada y los mínimos
# que respetaba el teclado no eran los mismos que respetaba el reload por
# SIGHUP. Ver el docstring de vistas.py.
VISTAS = vistas.VISTAS

_ORDENES = ["cpu", "rss", "pid"]


def run(snapshot, pids_proxy, interval_values, stop_event, verbose_value,
        top3_cpu_arr, top3_mem_arr, log_queue, dump_dir, control=None):
    # SIGINT/SIGTERM a SIG_IGN antes de inicializar curses: Ctrl+C en la
    # terminal le llega a todo el grupo de procesos en foreground, así que sin
    # esto el Display salía por KeyboardInterrupt desde adentro de
    # curses.wrapper y multiprocessing imprimía un traceback encima de la
    # terminal que el wrapper acababa de restaurar. Ahora el Ctrl+C lo procesa
    # sólo main (por self-pipe) y nosotros salimos ordenadamente por stop_event.
    #
    # SIGWINCH se deja explícitamente en SIG_DFL: ncurses instala su propio
    # handler de resize sólo si encuentra la disposición en SIG_DFL, y de él
    # depende que getmaxyx() devuelva el tamaño nuevo al redimensionar.
    senales.reset_en_hijo(dejar_en_default=(signal.SIGWINCH,))
    curses.wrapper(_main, snapshot, pids_proxy, interval_values, stop_event,
                    verbose_value, top3_cpu_arr, top3_mem_arr, log_queue, dump_dir, control)


class EstadoUI:
    def __init__(self):
        self.vista_actual = "resumen"
        self.seleccion = 0          # índice dentro de la lista YA renderizada
        self.pid_pineado = None
        self.filtro_comando = ""
        self.filtro_usuario = ""
        self.orden = "cpu"          # cpu | rss | pid
        self.mostrar_ayuda = False
        self.modo_entrada = None    # None | "comando" | "usuario"
        self.buffer_entrada = ""
        self.mensaje_estado = ""
        self.mensaje_ts = 0
        self._ultimo_reload_seq = -1


def _main(stdscr, snapshot, pids_proxy, interval_values, stop_event,
          verbose_value, top3_cpu_arr, top3_mem_arr, log_queue, dump_dir, control=None):
    curses.curs_set(0)
    stdscr.timeout(150)
    stdscr.keypad(True)
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
    except curses.error:
        pass  # terminales sin soporte de color: seguimos sin estilos

    estado = EstadoUI()

    while not stop_event.is_set():
        if control is not None:
            _revisar_reload(estado, control)
        try:
            _dibujar(stdscr, snapshot, estado, interval_values, verbose_value,
                     top3_cpu_arr, top3_mem_arr)
        except curses.error:
            pass  # terminal muy chica en este frame puntual: no es fatal, seguimos

        tecla = stdscr.getch()
        if tecla == -1:
            continue

        if estado.modo_entrada is not None:
            _manejar_entrada_texto(estado, tecla)
            continue

        salir = _manejar_tecla(tecla, estado, interval_values, stop_event, snapshot, dump_dir)
        if salir:
            break

    stop_event.set()  # por si salimos por 'q': que main y los analizadores también corten


def _revisar_reload(estado, control):
    """
    SIGHUP en el proceso principal actualiza control['reload_seq'] y
    control['filtros_default']. Acá detectamos el cambio de secuencia y
    reaplicamos los filtros por defecto — es la única forma en que main.py
    puede "tocar" el estado local del proceso Display, que de otra manera
    vive enteramente en memoria propia (no compartida).
    """
    try:
        seq = control.get("reload_seq", 0)
    except Exception:
        return
    if seq != estado._ultimo_reload_seq:
        estado._ultimo_reload_seq = seq
        if estado._ultimo_reload_seq != 0:  # no pisar los filtros en el primer frame
            filtros = control.get("filtros_default", {})
            estado.filtro_comando = filtros.get("comando", "")
            estado.filtro_usuario = filtros.get("usuario", "")


# ---------------------------------------------------------------------------
# Manejo de teclado
# ---------------------------------------------------------------------------

def _manejar_tecla(tecla, estado, interval_values, stop_event, snapshot, dump_dir):
    ch = chr(tecla) if 0 <= tecla < 256 else ""

    if ch == "q":
        return True

    # La ayuda es un overlay que tapa la pantalla, y su propio texto dice
    # "cualquier tecla cierra esta ayuda". Antes sólo la cerraba 'h'/'?': con la
    # ayuda abierta, apretar '2' cambiaba la vista de atrás sin que se viera
    # nada, porque el panel seguía encima. Ahora la primera tecla la cierra y se
    # consume ahí, que es lo que hace cualquier overlay de ayuda.
    if estado.mostrar_ayuda:
        estado.mostrar_ayuda = False
        return False

    if ch in vistas.TECLA_A_VISTA:
        estado.vista_actual = vistas.TECLA_A_VISTA[ch]
        estado.seleccion = 0
        return False

    if tecla in (curses.KEY_UP,):
        estado.seleccion = max(0, estado.seleccion - 1)
    elif tecla in (curses.KEY_DOWN,):
        estado.seleccion += 1
    elif tecla in (10, 13, curses.KEY_ENTER):
        pids_visibles = getattr(estado, "_ultimo_orden_pids", [])
        if 0 <= estado.seleccion < len(pids_visibles):
            pid_click = pids_visibles[estado.seleccion]
            estado.pid_pineado = None if estado.pid_pineado == pid_click else pid_click
    elif ch == "/":
        estado.modo_entrada = "comando"
        estado.buffer_entrada = estado.filtro_comando
    elif ch == "u":
        estado.modo_entrada = "usuario"
        estado.buffer_entrada = estado.filtro_usuario
    elif ch == "c":
        idx = _ORDENES.index(estado.orden)
        estado.orden = _ORDENES[(idx + 1) % len(_ORDENES)]
    elif ch in ("+", "="):
        _ajustar_intervalo(estado, interval_values, +1)
    elif ch == "-":
        _ajustar_intervalo(estado, interval_values, -1)
    elif ch in ("h", "?"):
        estado.mostrar_ayuda = not estado.mostrar_ayuda

    return False


def _manejar_entrada_texto(estado, tecla):
    if tecla in (10, 13, curses.KEY_ENTER):
        if estado.modo_entrada == "comando":
            estado.filtro_comando = estado.buffer_entrada
        else:
            estado.filtro_usuario = estado.buffer_entrada
        estado.modo_entrada = None
    elif tecla == 27:  # ESC: cancela sin aplicar
        estado.modo_entrada = None
    elif tecla in (curses.KEY_BACKSPACE, 127, 8):
        estado.buffer_entrada = estado.buffer_entrada[:-1]
    elif 32 <= tecla < 256:
        estado.buffer_entrada += chr(tecla)


def _ajustar_intervalo(estado, interval_values, direccion):
    """
    Teclas +/-: ajustan el intervalo de la vista activa escribiendo en el
    multiprocessing.Value que lee el analizador correspondiente (es la vía que
    pide la consigna para el refresh diferenciado). El piso lo aplica
    vistas.clamp_intervalo, la misma función que usa el reload por SIGHUP.
    """
    val = interval_values[estado.vista_actual]
    paso = 0.5
    with val.get_lock():
        val.value = vistas.clamp_intervalo(estado.vista_actual, val.value + direccion * paso)


# ---------------------------------------------------------------------------
# Dibujado
# ---------------------------------------------------------------------------

def _obtener_lista_procesos(snapshot, estado):
    resumen = snapshot.get("resumen")
    if not resumen:
        return [], 0
    data = resumen["data"]
    filas = list(data.values())

    if estado.filtro_comando:
        filas = [f for f in filas if estado.filtro_comando.lower() in f["comando"].lower()]
    if estado.filtro_usuario:
        filas = [f for f in filas if estado.filtro_usuario.lower() in f["usuario"].lower()]

    if estado.orden == "cpu":
        filas.sort(key=lambda f: f["cpu_pct"], reverse=True)
    elif estado.orden == "rss":
        filas.sort(key=lambda f: f["rss_kb"], reverse=True)
    else:
        filas.sort(key=lambda f: f["pid"])

    # el proceso pineado, si sigue existiendo, va siempre primero y no se
    # reordena con el resto (cumple "no cambia aunque cambie el orden")
    if estado.pid_pineado is not None:
        pineado = [f for f in filas if f["pid"] == estado.pid_pineado]
        resto = [f for f in filas if f["pid"] != estado.pid_pineado]
        filas = pineado + resto
        if not pineado:
            estado.pid_pineado = None  # el proceso pineado ya no existe

    return filas, resumen["ts"]


def _dibujar(stdscr, snapshot, estado, interval_values, verbose_value, top3_cpu_arr, top3_mem_arr):
    stdscr.erase()
    alto, ancho = stdscr.getmaxyx()

    filas, ts_resumen = _obtener_lista_procesos(snapshot, estado)
    estado._ultimo_orden_pids = [f["pid"] for f in filas]
    estado.seleccion = min(estado.seleccion, max(0, len(filas) - 1))

    _dibujar_encabezado(stdscr, ancho, estado, interval_values, verbose_value,
                        top3_cpu_arr, top3_mem_arr, snapshot)

    # tamaño de la mitad superior (lista) vs inferior (detalle): 45/55 aprox
    alto_lista = max(4, int((alto - 5) * 0.45))
    fila_inicio_lista = 3
    _dibujar_lista(stdscr, filas, estado, fila_inicio_lista, alto_lista, ancho)

    fila_inicio_detalle = fila_inicio_lista + alto_lista + 1
    pid_seleccionado = filas[estado.seleccion]["pid"] if filas and estado.seleccion < len(filas) else None
    _dibujar_detalle(stdscr, snapshot, estado, pid_seleccionado, fila_inicio_detalle,
                      alto - fila_inicio_detalle - 1, ancho)

    _dibujar_pie(stdscr, alto - 1, ancho, estado)

    if estado.mostrar_ayuda:
        _dibujar_ayuda(stdscr, alto, ancho)

    if estado.modo_entrada is not None:
        _dibujar_prompt_entrada(stdscr, alto, ancho, estado)

    stdscr.refresh()


def _addstr_seguro(stdscr, y, x, texto, attr=0):
    alto, ancho = stdscr.getmaxyx()
    if y < 0 or y >= alto or x >= ancho:
        return
    try:
        stdscr.addnstr(y, x, texto, max(0, ancho - x - 1), attr)
    except curses.error:
        pass


def _dibujar_encabezado(stdscr, ancho, estado, interval_values, verbose_value,
                        top3_cpu_arr, top3_mem_arr, snapshot):
    vista = estado.vista_actual
    # Mostramos el intervalo vigente y su piso al lado del título, para que el
    # efecto de las teclas +/- sea visible y para que se vea cuándo tocaste el
    # mínimo de esa vista en vez de que la tecla parezca "no hacer nada".
    intervalo = interval_values[vista].value
    minimo = vistas.MINIMOS[vista]
    tope = " (mín)" if intervalo <= minimo + 1e-9 else ""
    titulo = f" Monitor /proc — vista: {vistas.TITULOS[vista]} — refresco: {intervalo:.1f}s{tope} "
    _addstr_seguro(stdscr, 0, 0, titulo.ljust(ancho), curses.A_REVERSE)

    verbose_txt = "ON" if (verbose_value and verbose_value.value) else "off"
    top_cpu_pid = top3_cpu_arr[0] if top3_cpu_arr else -1
    top_mem_pid = top3_mem_arr[0] if top3_mem_arr else -1
    resumen = (snapshot.get("resumen") or {}).get("data", {})
    top_cpu_info = resumen.get(top_cpu_pid)
    top_mem_info = resumen.get(top_mem_pid)
    partes = [f"verbose:{verbose_txt}"]
    if top_cpu_info:
        partes.append(f"top-cpu:{top_cpu_pid}({top_cpu_info['cpu_pct']}%)")
    if top_mem_info:
        partes.append(f"top-mem:{top_mem_pid}({top_mem_info['rss_kb']//1024}MB)")
    if estado.filtro_comando:
        partes.append(f"filtro-cmd:'{estado.filtro_comando}'")
    if estado.filtro_usuario:
        partes.append(f"filtro-user:'{estado.filtro_usuario}'")
    partes.append(f"orden:{estado.orden}")
    _addstr_seguro(stdscr, 1, 0, " | ".join(partes))

    _addstr_seguro(stdscr, 2, 0, "-" * ancho)


def _dibujar_lista(stdscr, filas, estado, y0, alto, ancho):
    encabezado = f"{'':1}{'PID':>7} {'USER':<10} {'PPID':>7} {'S':1} {'CPU%':>6} {'RSS(MB)':>8} {'THR':>4}  COMANDO"
    _addstr_seguro(stdscr, y0, 0, encabezado, curses.A_BOLD)

    primero_visible = max(0, estado.seleccion - alto + 2)
    for i in range(alto - 1):
        idx = primero_visible + i
        if idx >= len(filas):
            break
        f = filas[idx]
        marcador = "*" if estado.pid_pineado == f["pid"] else " "
        linea = (f"{marcador}{f['pid']:>7} {f['usuario'][:10]:<10} {f['ppid']:>7} "
                 f"{f['estado']:1} {f['cpu_pct']:>6.1f} {f['rss_kb']/1024:>8.1f} "
                 f"{f['threads']:>4}  {f['comando'][:ancho]}")
        attr = curses.A_REVERSE if idx == estado.seleccion else 0
        _addstr_seguro(stdscr, y0 + 1 + i, 0, linea, attr)

    if not filas:
        _addstr_seguro(stdscr, y0 + 1, 0, "(sin datos todavía — esperando al recolector/analizador de resumen...)")


def _dibujar_pie(stdscr, y, ancho, estado):
    ayuda = "1-7/rmftspg vista | ↑↓ nav | Enter pin | / cmd | u user | c orden | +/- intervalo | h ayuda | q salir"
    _addstr_seguro(stdscr, y, 0, ayuda.ljust(ancho), curses.A_DIM)


def _dibujar_prompt_entrada(stdscr, alto, ancho, estado):
    etiqueta = "Filtrar por comando: " if estado.modo_entrada == "comando" else "Filtrar por usuario: "
    y = alto - 2
    _addstr_seguro(stdscr, y, 0, (etiqueta + estado.buffer_entrada + "_").ljust(ancho), curses.A_REVERSE)


def _dibujar_ayuda(stdscr, alto, ancho):
    # 17 líneas + el marco = 19: entra en una terminal estándar de 24 filas.
    # La versión larga anterior tenía 26 y `h = min(alto - 2, ...)` recortaba
    # justo el final, así que en 24 filas se perdía hasta la línea que explica
    # cómo cerrar el panel.
    lineas = [
        "AYUDA — Monitor de Procesos y Threads",
        "",
        "1/r Resumen    2/m Memoria     3/f File descriptors  4/t Threads",
        "5/s Señales    6/p Scheduling  7/g Sistema global",
        "",
        "↑ ↓     Navegar la lista       c    Orden: CPU% / RSS / PID",
        "Enter   Pin del proceso        / u  Filtrar comando / usuario",
        "+ / -   Intervalo de la vista  q    Salir limpiamente",
        "",
        "Señales al proceso main (kill -SIGXXX <pid>):",
        "  INT/TERM  shutdown limpio      USR1   dump del snapshot a JSON",
        "  HUP       recarga config.json  USR2   toggle modo verbose",
        "  WINCH     repintar (resize de terminal)",
        "",
        "Verbose (USR2) agrega: más FDs por proceso + el inodo de pipes y",
        "sockets, el desglose de RSS (anon/file/shmem) y, por thread, el",
        "core donde corrió y sus jiffies crudos de utime/stime.",
        "",
        "(cualquier tecla cierra esta ayuda)",
    ]
    h = min(alto - 2, len(lineas) + 2)
    w = min(ancho - 4, max(len(l) for l in lineas) + 4)
    y0 = (alto - h) // 2
    x0 = (ancho - w) // 2
    win = curses.newwin(h, w, max(0, y0), max(0, x0))
    win.box()
    for i, linea in enumerate(lineas[:h - 2]):
        try:
            win.addnstr(i + 1, 2, linea, w - 4)
        except curses.error:
            pass
    win.refresh()


# ---------------------------------------------------------------------------
# Paneles de detalle por vista (todas reciben snapshot + pid seleccionado)
# ---------------------------------------------------------------------------

def _dibujar_detalle(stdscr, snapshot, estado, pid, y0, alto, ancho):
    _addstr_seguro(stdscr, y0, 0, "=" * ancho)
    y0 += 1
    if estado.vista_actual == "sistema":
        _detalle_sistema(stdscr, snapshot, y0, alto, ancho)
        return

    if pid is None:
        _addstr_seguro(stdscr, y0, 0, "(seleccioná un proceso arriba para ver el detalle)")
        return

    funcs = {
        "resumen": _detalle_resumen,
        "memoria": _detalle_memoria,
        "fds": _detalle_fds,
        "threads": _detalle_threads,
        "senales": _detalle_senales,
        "scheduling": _detalle_scheduling,
    }
    funcs[estado.vista_actual](stdscr, snapshot, pid, y0, alto, ancho)


def _detalle_resumen(stdscr, snapshot, pid, y0, alto, ancho):
    data = (snapshot.get("resumen") or {}).get("data", {}).get(pid)
    if not data:
        return
    _addstr_seguro(stdscr, y0, 0, f"PID {pid} — detalle Resumen", curses.A_BOLD)
    _addstr_seguro(stdscr, y0 + 1, 0,
                    f"UID: {data['uid']} ({data['usuario']})   GID: {data['gid']} ({data['grupo']})   "
                    f"PPID: {data['ppid']}")
    _addstr_seguro(stdscr, y0 + 2, 0, f"Estado: {data['estado']} ({data['estado_nombre']})   Threads: {data['threads']}")
    _addstr_seguro(stdscr, y0 + 3, 0, f"CPU%: {data['cpu_pct']}   RSS: {data['rss_kb']/1024:.1f} MB")
    _addstr_seguro(stdscr, y0 + 4, 0, f"Comando completo: {data['comando']}")


def _detalle_memoria(stdscr, snapshot, pid, y0, alto, ancho):
    data = (snapshot.get("memoria") or {}).get("data", {}).get(pid)
    if not data:
        _addstr_seguro(stdscr, y0, 0, "(sin datos de memoria todavía para este pid)")
        return
    _addstr_seguro(stdscr, y0, 0, f"PID {pid} — detalle Memoria", curses.A_BOLD)
    _addstr_seguro(stdscr, y0 + 1, 0,
                    f"VmSize: {data['vm_size_kb']:>9} kB   VmRSS: {data['vm_rss_kb']:>9} kB   VmHWM: {data['vm_hwm_kb']:>9} kB")
    _addstr_seguro(stdscr, y0 + 2, 0,
                    f"VmData: {data['vm_data_kb']:>9} kB   VmStk: {data['vm_stk_kb']:>9} kB   VmExe: {data['vm_exe_kb']:>9} kB")
    _addstr_seguro(stdscr, y0 + 3, 0, f"VmLib:  {data['vm_lib_kb']:>9} kB   VmSwap:{data['vm_swap_kb']:>9} kB")
    _addstr_seguro(stdscr, y0 + 4, 0,
                    f"Page faults — minor: {data['minflt']}  major: {data['majflt']}  "
                    f"(hijos: {data['cminflt']}/{data['cmajflt']})")
    fila = y0 + 5
    if "rss_anon_kb" in data:  # modo verbose (SIGUSR2)
        # Va antes del chequeo de segmentos: el desglose de RSS sale de status,
        # que sí podemos leer incluso cuando maps nos devuelve EACCES.
        _addstr_seguro(stdscr, fila, 0,
                        f"VmPeak:{data['vm_peak_kb']:>9} kB   RSS anon: {data['rss_anon_kb']} kB"
                        f"   file: {data['rss_file_kb']} kB   shmem: {data['rss_shmem_kb']} kB")
        fila += 1

    segs = data.get("segmentos")
    if not segs:
        _addstr_seguro(stdscr, fila + 1, 0,
                        "Segmentos: sin permiso para leer /proc/<pid>/maps (proceso de otro usuario)")
        return

    _addstr_seguro(stdscr, fila + 1, 0, "Segmentos de memoria (agrupados):", curses.A_UNDERLINE)
    fila += 2
    for cat, kb in segs["grupos_kb"].items():
        cnt = segs["conteo"][cat]
        _addstr_seguro(stdscr, fila, 2, f"{cat:<8} {kb:>8} kB   ({cnt} segmentos)")
        fila += 1


def _detalle_fds(stdscr, snapshot, pid, y0, alto, ancho):
    data = (snapshot.get("fds") or {}).get("data", {}).get(pid)
    if not data:
        _addstr_seguro(stdscr, y0, 0, "(sin datos de FDs todavía para este pid)")
        return
    if data.get("error"):
        _addstr_seguro(stdscr, y0, 0, f"PID {pid} — {data['error']}")
        return
    resumen_tipos = ", ".join(f"{t}:{c}" for t, c in data["por_tipo"].items())
    _addstr_seguro(stdscr, y0, 0, f"PID {pid} — {data['total']} FDs abiertos ({resumen_tipos})", curses.A_BOLD)
    if data.get("truncado"):
        _addstr_seguro(stdscr, y0 + 1, 0,
                        f"(mostrando los primeros {len(data['fds'])} de {data['total']} — "
                        f"SIGUSR2 activa el modo verbose para ver más)", curses.A_DIM)

    fila = y0 + 2
    mostrados = 0
    for entrada in data["fds"]:
        if fila >= y0 + alto - 1:
            restantes = len(data["fds"]) - mostrados
            _addstr_seguro(stdscr, fila, 0, f"... ({restantes} más en el snapshot, no caben en la terminal)")
            break
        # El inodo sólo viene en modo verbose: dos FDs con el mismo pipe:[N] o
        # socket:[N] son los dos extremos del MISMO objeto del kernel.
        extra = f"  inodo={entrada['inodo']}" if entrada.get("inodo") else ""
        _addstr_seguro(stdscr, fila, 0,
                        f"  fd {entrada['fd']:>4}  [{entrada['tipo']:<10}]  {entrada['destino']}{extra}")
        fila += 1
        mostrados += 1


def _detalle_threads(stdscr, snapshot, pid, y0, alto, ancho):
    data = (snapshot.get("threads") or {}).get("data", {}).get(pid)
    if not data:
        _addstr_seguro(stdscr, y0, 0, "(sin datos de threads todavía para este pid)")
        return
    verbose = bool(data and "cpu_ultimo_core" in data[0])
    _addstr_seguro(stdscr, y0, 0,
                    f"PID {pid} — {len(data)} threads (LWPs). El TID igual al PID es el thread principal.",
                    curses.A_BOLD)
    cab = f"{'TID':>7} {'NOMBRE':<16} {'S':1} {'CPU%':>6} {'VOL':>7} {'INVOL':>7}"
    if verbose:
        cab += f" {'CORE':>5} {'UTIME':>8} {'STIME':>8}"
    _addstr_seguro(stdscr, y0 + 1, 0, cab)
    fila = y0 + 2
    for th in sorted(data, key=lambda t: -t["cpu_pct"]):
        if fila >= y0 + alto - 1:
            break
        linea = (f"{th['tid']:>7} {th['nombre'][:16]:<16} {th['estado']:1} {th['cpu_pct']:>6.1f} "
                 f"{th['voluntary_ctxt_switches']:>7} {th['nonvoluntary_ctxt_switches']:>7}")
        if verbose:
            linea += f" {th.get('cpu_ultimo_core', -1):>5} {th.get('utime', 0):>8} {th.get('stime', 0):>8}"
        _addstr_seguro(stdscr, fila, 0, linea)
        fila += 1


def _detalle_senales(stdscr, snapshot, pid, y0, alto, ancho):
    data = (snapshot.get("senales") or {}).get("data", {}).get(pid)
    if not data:
        _addstr_seguro(stdscr, y0, 0, "(sin datos de señales todavía para este pid)")
        return
    _addstr_seguro(stdscr, y0, 0, f"PID {pid} — máscaras de señales", curses.A_BOLD)
    etiquetas = [("sigblk", "Bloqueadas (SigBlk)"), ("sigign", "Ignoradas (SigIgn)"),
                 ("sigcgt", "Con handler propio (SigCgt)"), ("sigpnd", "Pendientes del proceso (SigPnd)"),
                 ("shdpnd", "Pendientes del grupo (ShdPnd)")]
    fila = y0 + 1
    for clave, etiqueta in etiquetas:
        info = data.get(clave, {"hex": "?", "nombres": []})
        nombres = ", ".join(info["nombres"]) if info["nombres"] else "(ninguna)"
        _addstr_seguro(stdscr, fila, 0, f"{etiqueta} [{info['hex']}]:")
        fila += 1
        _addstr_seguro(stdscr, fila, 2, nombres)
        fila += 2


def _detalle_scheduling(stdscr, snapshot, pid, y0, alto, ancho):
    data = (snapshot.get("scheduling") or {}).get("data", {}).get(pid)
    if not data:
        _addstr_seguro(stdscr, y0, 0, "(sin datos de scheduling todavía para este pid)")
        return
    _addstr_seguro(stdscr, y0, 0, f"PID {pid} — scheduling", curses.A_BOLD)
    _addstr_seguro(stdscr, y0 + 1, 0, f"Policy: {data['policy']} ({data['policy_num']})   Nice: {data['nice']}   Priority: {data['priority']}")
    _addstr_seguro(stdscr, y0 + 2, 0, f"RT priority: {data['rt_priority']}   CPU affinity: {data['cpus_allowed_list']}")
    _addstr_seguro(stdscr, y0 + 3, 0, f"Ctx switches — voluntarios: {data['voluntary_ctxt_switches']}   involuntarios: {data['nonvoluntary_ctxt_switches']}")
    _addstr_seguro(stdscr, y0 + 4, 0, f"utime: {data['utime']} ticks   stime: {data['stime']} ticks")
    _addstr_seguro(stdscr, y0 + 5, 0, f"SID: {data['sid']}   PGID: {data['pgid']}")


def _detalle_sistema(stdscr, snapshot, y0, alto, ancho):
    data = (snapshot.get("sistema") or {}).get("data")
    if not data:
        _addstr_seguro(stdscr, y0, 0, "(esperando al analizador de sistema...)")
        return
    cpu = data["cpu_pct"]
    _addstr_seguro(stdscr, y0, 0,
                    f"CPU global — user: {cpu['user']}%  system: {cpu['system']}%  "
                    f"idle: {cpu['idle']}%  iowait: {cpu['iowait']}%", curses.A_BOLD)
    la = data["loadavg"] or {}
    _addstr_seguro(stdscr, y0 + 1, 0,
                    f"Load average: {la.get('load1')} {la.get('load5')} {la.get('load15')}   "
                    f"(corriendo {la.get('procesos_corriendo')}/{la.get('procesos_totales')})")
    mem = data["meminfo"] or {}
    _addstr_seguro(stdscr, y0 + 2, 0,
                    f"Memoria — total: {mem.get('MemTotal', 0)//1024} MB  libre: {mem.get('MemFree', 0)//1024} MB  "
                    f"buffers: {mem.get('Buffers', 0)//1024} MB  cache: {mem.get('Cached', 0)//1024} MB")
    _addstr_seguro(stdscr, y0 + 3, 0,
                    f"Swap — total: {mem.get('SwapTotal', 0)//1024} MB  libre: {mem.get('SwapFree', 0)//1024} MB")
    up = data["uptime"] or {}
    # Boot time: /proc/stat línea btime, en segundos desde el epoch. Lo mostramos
    # como fecha legible además del uptime, que es el mismo dato visto "al
    # revés" (uptime = ahora - btime).
    btime = data.get("btime")
    boot_txt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(btime)) if btime else "?"
    _addstr_seguro(stdscr, y0 + 4, 0,
                    f"Boot time: {boot_txt}   Uptime: {up.get('uptime_seg', 0)/3600:.1f} h   "
                    f"(idle acumulado: {up.get('idle_seg', 0)/3600:.1f} h)")
    _addstr_seguro(stdscr, y0 + 5, 0,
                    f"Procesos: {data['procesos_totales']}   Threads totales: {data['threads_totales']}   "
                    f"Zombies: {data['zombies']}")

    ce = data["conteo_estados"]
    _addstr_seguro(stdscr, y0 + 6, 0,
                    f"Por estado — R:{ce.get('R', 0)} S:{ce.get('S', 0)} D:{ce.get('D', 0)} "
                    f"T:{ce.get('T', 0)} Z:{ce.get('Z', 0)} I:{ce.get('I', 0)}")

    _addstr_seguro(stdscr, y0 + 8, 0, "Top 3 por CPU:", curses.A_UNDERLINE)
    for i, p in enumerate(data["top3_cpu"]):
        _addstr_seguro(stdscr, y0 + 9 + i, 2, f"{p['pid']:>7}  {p['comm']:<16} {p['cpu_pct']:>6.1f}%")

    _addstr_seguro(stdscr, y0 + 8, ancho // 2, "Top 3 por Memoria:", curses.A_UNDERLINE)
    for i, p in enumerate(data["top3_mem"]):
        _addstr_seguro(stdscr, y0 + 9 + i, ancho // 2 + 2, f"{p['pid']:>7}  {p['comm']:<16} {p['rss_kb']/1024:>7.1f} MB")
