"""
procfs.py — helpers de lectura y parseo de /proc.

Este es el único módulo que toca el filesystem /proc directamente. El resto
del sistema (analizadores, display, recolector) llama a estas funciones y
recibe estructuras de datos ya parseadas (dicts, tuplas, listas), nunca
strings crudos de /proc.

Referencia usada para el parseo de /proc/<pid>/stat: proc(5) man page
(https://man7.org/linux/man-pages/man5/proc.5.html), sección "/proc/pid/stat".

Nota sobre una discrepancia con la consigna: la consigna ubica la sesión (SID)
y el grupo de procesos (PGID) en los "campos 6-7" de stat. Según proc(5) son el
6 y el 5 respectivamente:

    (4) ppid   (5) pgrp   (6) session   (7) tty_nr

o sea que `session` sí es el 6 y coincide, pero `pgrp` es el 5, no el 7 (el 7
es tty_nr). Mi lectura es que la consigna quiso decir "campos 5-6" y además los
nombró en el orden inverso al que aparecen en el archivo. Lo verifiqué
comparando /proc crudo contra ps:

    awk '{print "pgrp="$5, "session="$6}' /proc/$$/stat
    ps -o pgid=,sid= -p $$        # -> los mismos dos números

El resto de los campos que usa este módulo coinciden exactamente entre la
consigna y el man page (minflt/majflt 10-13, utime/stime 14-15, priority 18,
nice 19, rt_priority 40, policy 41), así que es un error puntual de esa fila y
no un criterio de numeración distinto. Me quedo con proc(5) por ser la fuente
primaria.

Convención de errores: casi todas las funciones que leen un archivo de
/proc/<pid>/... pueden fallar porque el proceso terminó entre que lo listamos
y que lo leímos (TOCTOU / race condition clásica de /proc). En vez de dejar
propagar la excepción, cada función atrapa (FileNotFoundError, ProcessLookupError,
PermissionError, NotADirectoryError) y devuelve None (o un dict/lista vacía),
para que el analizador que la llama simplemente descarte ese PID en esa vuelta.
"""

import os
import re
import pwd
import grp
import signal

PROC = "/proc"

# Sentinel devuelto por read_fds() cuando el directorio existe pero no tenemos
# permiso para listarlo. Hace falta distinguirlo de None (= el proceso ya no
# existe): son dos situaciones distintas y la TUI las tiene que mostrar
# distinto (antes las dos caían en "sin permiso", así que procesos que habían
# muerto aparecían como si fueran de otro usuario).
SIN_PERMISO = object()

# --- constantes del sistema, calculadas una sola vez ---
CLK_TCK = os.sysconf("SC_CLK_TCK")          # jiffies (ticks) por segundo, usado para pasar utime/stime a segundos
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")      # tamaño de página en bytes (por si se necesita convertir desde páginas)

_ERRORS_ESPERADOS = (FileNotFoundError, ProcessLookupError, PermissionError, NotADirectoryError, ValueError, IsADirectoryError)


def _read(path):
    """Lee un archivo de /proc como texto completo. None si falla (proceso murió, sin permisos, etc.)."""
    try:
        with open(path, "r") as f:
            return f.read()
    except _ERRORS_ESPERADOS:
        return None
    except OSError:
        # /proc a veces tira OSError genérico (ESRCH envuelto) para procesos que ya no existen
        return None


def _read_link(path):
    try:
        return os.readlink(path)
    except _ERRORS_ESPERADOS:
        return None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Listado de procesos y threads
# ---------------------------------------------------------------------------

def list_pids():
    """Devuelve la lista de PIDs actualmente vivos, leyendo los nombres de carpeta de /proc."""
    pids = []
    try:
        for entry in os.listdir(PROC):
            if entry.isdigit():
                pids.append(int(entry))
    except OSError:
        pass
    return pids


def list_tids(pid):
    """Devuelve la lista de TIDs (threads / LWPs) de un proceso, vía /proc/<pid>/task."""
    try:
        return [int(t) for t in os.listdir(f"{PROC}/{pid}/task") if t.isdigit()]
    except _ERRORS_ESPERADOS:
        return []
    except OSError:
        return []


# ---------------------------------------------------------------------------
# /proc/<pid>/stat  (y /proc/<pid>/task/<tid>/stat, mismo formato)
# ---------------------------------------------------------------------------

# Nombres de los campos de stat, indexados de forma que _STAT_FIELDS[i] es el
# nombre del campo (i+3) del proc(5) (porque state es el campo 3: pid y comm
# se parsean aparte al tener el comm un formato especial "(nombre con espacios)").
_STAT_FIELDS = [
    "state", "ppid", "pgrp", "session", "tty_nr", "tpgid", "flags",
    "minflt", "cminflt", "majflt", "cmajflt", "utime", "stime",
    "cutime", "cstime", "priority", "nice", "num_threads", "itrealvalue",
    "starttime", "vsize", "rss", "rsslim", "startcode", "endcode",
    "startstack", "kstkesp", "kstkeip", "signal", "blocked", "sigignore",
    "sigcatch", "wchan", "nswap", "cnswap", "exit_signal", "processor",
    "rt_priority", "policy", "delayacct_blkio_ticks", "guest_time",
    "cguest_time",
]


def parse_stat(raw):
    """
    Parsea el contenido de /proc/<pid>/stat (o task/<tid>/stat).

    El campo comm (nombre del comando) viene entre paréntesis y puede
    contener espacios, paréntesis, etc. (ej: "(some (weird) name)"), por eso
    NO se puede hacer un simple split() de todo el archivo: buscamos el
    primer '(' y el ÚLTIMO ')' para extraer comm de forma robusta, tal como
    recomienda el propio proc(5).
    """
    if raw is None:
        return None
    try:
        first_paren = raw.index("(")
        last_paren = raw.rindex(")")
    except ValueError:
        return None

    pid_str = raw[:first_paren].strip()
    comm = raw[first_paren + 1:last_paren]
    resto = raw[last_paren + 1:].split()

    data = {"pid": int(pid_str), "comm": comm}
    for name, value in zip(_STAT_FIELDS, resto):
        try:
            data[name] = int(value)
        except ValueError:
            data[name] = value  # por si algún campo no fuera numérico en el futuro
    return data


def read_stat(pid, tid=None):
    if tid is None:
        path = f"{PROC}/{pid}/stat"
    else:
        path = f"{PROC}/{pid}/task/{tid}/stat"
    return parse_stat(_read(path))


# ---------------------------------------------------------------------------
# /proc/<pid>/status
# ---------------------------------------------------------------------------

_KB_FIELDS = {
    "VmPeak", "VmSize", "VmLck", "VmPin", "VmHWM", "VmRSS", "RssAnon",
    "RssFile", "RssShmem", "VmData", "VmStk", "VmExe", "VmLib", "VmPTE",
    "VmSwap", "HugetlbPages",
}
_INT_FIELDS = {
    "Tgid", "Ngid", "Pid", "PPid", "TracerPid", "FDSize", "Threads",
    "voluntary_ctxt_switches", "nonvoluntary_ctxt_switches", "NoNewPrivs",
    "Seccomp",
}


def parse_status(raw):
    """Parsea /proc/<pid>/status a un dict {Campo: valor}. Los campos VmXXX quedan en kB (int)."""
    if raw is None:
        return None
    data = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key in _KB_FIELDS:
            # formato "1234 kB" -> 1234 (int)
            m = re.match(r"(\d+)", value)
            data[key] = int(m.group(1)) if m else 0
        elif key in _INT_FIELDS:
            try:
                data[key] = int(value)
            except ValueError:
                data[key] = value
        elif key == "Uid":
            partes = value.split()
            data["Uid"] = tuple(int(x) for x in partes[:4])
        elif key == "Gid":
            partes = value.split()
            data["Gid"] = tuple(int(x) for x in partes[:4])
        else:
            data[key] = value
    return data


def read_status(pid, tid=None):
    if tid is None:
        path = f"{PROC}/{pid}/status"
    else:
        path = f"{PROC}/{pid}/task/{tid}/status"
    return parse_status(_read(path))


def read_cmdline(pid):
    """/proc/<pid>/cmdline viene separado por bytes \\0. Si está vacío (procesos kernel/zombies), devuelve ''."""
    raw = _read(f"{PROC}/{pid}/cmdline")
    if raw is None:
        return None
    return " ".join(p for p in raw.split("\0") if p)


def read_comm(pid, tid=None):
    """Nombre corto del thread/proceso (15 chars max), separado porque a veces alcanza y es más barato que status."""
    if tid is None:
        path = f"{PROC}/{pid}/comm"
    else:
        path = f"{PROC}/{pid}/task/{tid}/comm"
    raw = _read(path)
    return raw.strip() if raw is not None else None


# caches de uid/gid -> nombre, para no golpear pwd/grp en cada refresco de cada
# proceso (son lookups a /etc/passwd y /etc/group, no a /proc, y los nombres no
# cambian mientras el monitor está corriendo)
_uid_cache = {}
_gid_cache = {}


def username_for_uid(uid):
    if uid in _uid_cache:
        return _uid_cache[uid]
    try:
        name = pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        name = str(uid)
    _uid_cache[uid] = name
    return name


def groupname_for_gid(gid):
    """Igual que username_for_uid pero contra /etc/group, para el GID que pide la consigna."""
    if gid in _gid_cache:
        return _gid_cache[gid]
    try:
        name = grp.getgrgid(gid).gr_name
    except (KeyError, OverflowError):
        name = str(gid)
    _gid_cache[gid] = name
    return name


# ---------------------------------------------------------------------------
# Señales: decodificación de máscaras hexadecimales de 64 bits
# ---------------------------------------------------------------------------

# Nombres estándar de señales POSIX en Linux/x86, numeradas 1..31 (evitamos
# depender pura y exclusivamente de signal.Signals porque no todas las
# versiones de Python exponen los mismos alias, ej: SIGIOT/SIGPOLL).
_SIGNAL_NAMES = {
    1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 5: "SIGTRAP",
    6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE", 9: "SIGKILL", 10: "SIGUSR1",
    11: "SIGSEGV", 12: "SIGUSR2", 13: "SIGPIPE", 14: "SIGALRM", 15: "SIGTERM",
    16: "SIGSTKFLT", 17: "SIGCHLD", 18: "SIGCONT", 19: "SIGSTOP", 20: "SIGTSTP",
    21: "SIGTTIN", 22: "SIGTTOU", 23: "SIGURG", 24: "SIGXCPU", 25: "SIGXFSZ",
    26: "SIGVTALRM", 27: "SIGPROF", 28: "SIGWINCH", 29: "SIGIO", 30: "SIGPWR",
    31: "SIGSYS",
}

# Señales 32 y 33: NO son POSIX ni RT signals, son internas de la
# implementación de threads de glibc (NPTL). Son la razón exacta de por qué
# SIGRTMIN vale 34 y no 32: el kernel expone __SIGRTMIN = 32, pero glibc se
# reserva las dos primeras para uso propio y corre el rango "público" dos
# lugares para arriba. Por eso aparecen seteadas en SigBlk/SigCgt de
# cualquier proceso multithread linkeado contra glibc (incluido este monitor).
#   32 = SIGCANCEL  -> cancelación de threads (pthread_cancel)
#   33 = SIGSETXID  -> propagar setuid/setgid a todos los threads del proceso
# No están expuestas como constantes en <signal.h> para las aplicaciones, así
# que las nombramos a mano y aclaramos en la TUI que son de glibc, no POSIX.
_SIGNAL_NAMES_GLIBC = {
    32: "SIGCANCEL",
    33: "SIGSETXID",
}

# Lo tomamos de la stdlib en vez de hardcodear 34, así el nombre "SIGRTMIN+N"
# sale correcto incluso si se corriera sobre una libc con otro corrimiento.
_SIGRTMIN = int(getattr(signal, "SIGRTMIN", 34))
_SIGRTMAX = int(getattr(signal, "SIGRTMAX", 64))


def _nombre_senal(n):
    if n in _SIGNAL_NAMES:
        return _SIGNAL_NAMES[n]
    if n in _SIGNAL_NAMES_GLIBC:
        return _SIGNAL_NAMES_GLIBC[n]
    if _SIGRTMIN <= n <= _SIGRTMAX:
        return f"SIGRTMIN+{n - _SIGRTMIN}" if n > _SIGRTMIN else "SIGRTMIN"
    return f"SIG{n}"


def decode_sigmask(hex_mask):
    """
    Decodifica una máscara de señales de /proc/<pid>/status (SigBlk, SigIgn,
    SigCgt, SigPnd, ShdPnd) de hexadecimal a lista de nombres legibles.

    El formato es un entero de 64 bits en hex donde el bit (n-1) representa
    la señal número n (las señales POSIX arrancan en 1, no en 0).
    """
    if not hex_mask:
        return []
    try:
        mask = int(hex_mask, 16)
    except ValueError:
        return []
    nombres = []
    n = 1
    while mask:
        if mask & 1:
            nombres.append(_nombre_senal(n))
        mask >>= 1
        n += 1
    return nombres


# ---------------------------------------------------------------------------
# Scheduling: política numérica -> nombre
# ---------------------------------------------------------------------------

_POLICY_NAMES = {
    0: "SCHED_OTHER", 1: "SCHED_FIFO", 2: "SCHED_RR", 3: "SCHED_BATCH",
    5: "SCHED_IDLE", 6: "SCHED_DEADLINE",
}


def policy_name(n):
    return _POLICY_NAMES.get(n, f"POLICY_{n}")


_STATE_NAMES = {
    "R": "Running", "S": "Sleeping", "D": "Disk sleep (uninterruptible)",
    "Z": "Zombie", "T": "Stopped", "t": "Tracing stop", "X": "Dead",
    "I": "Idle",
}


def state_name(letter):
    return _STATE_NAMES.get(letter, letter)


# ---------------------------------------------------------------------------
# /proc/<pid>/maps — segmentos de memoria
# ---------------------------------------------------------------------------

_MAPS_RE = re.compile(
    r"^(?P<start>[0-9a-f]+)-(?P<end>[0-9a-f]+)\s+"
    r"(?P<perms>[r\-][w\-][x\-][ps])\s+"
    r"(?P<offset>[0-9a-f]+)\s+"
    r"(?P<dev>\S+)\s+"
    r"(?P<inode>\d+)\s*"
    r"(?P<path>.*)$"
)


def parse_maps(raw):
    """Parsea /proc/<pid>/maps en una lista de dicts por segmento."""
    if raw is None:
        return None
    segmentos = []
    for line in raw.splitlines():
        m = _MAPS_RE.match(line)
        if not m:
            continue
        start = int(m.group("start"), 16)
        end = int(m.group("end"), 16)
        segmentos.append({
            "start": start,
            "end": end,
            "size_kb": (end - start) // 1024,
            "perms": m.group("perms"),
            "path": m.group("path").strip(),
        })
    return segmentos


def agrupar_segmentos(segmentos):
    """
    Agrupa segmentos de /proc/<pid>/maps en categorías: heap, stack, shared,
    text, data. Ver README, sección "Decisiones de diseño" para la
    justificación del criterio de clasificación.
    """
    grupos = {"heap": 0, "stack": 0, "shared": 0, "text": 0, "data": 0}
    conteo = {"heap": 0, "stack": 0, "shared": 0, "text": 0, "data": 0}
    if not segmentos:
        return grupos, conteo
    for seg in segmentos:
        path = seg["path"]
        perms = seg["perms"]
        if path == "[heap]":
            cat = "heap"
        elif path.startswith("[stack"):
            cat = "stack"
        elif len(perms) >= 4 and perms[3] == "s":
            cat = "shared"
        elif "x" in perms:
            cat = "text"
        else:
            cat = "data"
        grupos[cat] += seg["size_kb"]
        conteo[cat] += 1
    return grupos, conteo


def read_maps_agrupado(pid):
    raw = _read(f"{PROC}/{pid}/maps")
    segmentos = parse_maps(raw)
    if segmentos is None:
        return None
    grupos, conteo = agrupar_segmentos(segmentos)
    return {"grupos_kb": grupos, "conteo": conteo, "total_segmentos": len(segmentos)}


# ---------------------------------------------------------------------------
# File descriptors
# ---------------------------------------------------------------------------

def _tipo_fd(destino):
    if destino is None:
        return "?"
    if destino.startswith("socket:["):
        return "socket"
    if destino.startswith("pipe:["):
        return "pipe"
    if destino.startswith("anon_inode:"):
        return "anon_inode"
    if destino.startswith("/memfd:") or destino.startswith("memfd:"):
        return "memfd"
    if destino.startswith("/dev/pts/") or destino in ("/dev/tty", "/dev/console"):
        return "tty"
    if destino.startswith("/"):
        return "file"
    return "otro"


def read_fds(pid):
    """
    Devuelve una lista de dicts {fd, destino, tipo} para cada file descriptor
    abierto por el proceso.

    Tres retornos posibles, y la diferencia importa:
      - lista        -> pudimos leer los FDs.
      - SIN_PERMISO  -> el proceso existe pero es de otro usuario y no tenemos
                        CAP_SYS_PTRACE (/proc/<pid>/fd es 0500 del dueño).
      - None         -> el proceso ya no existe (murió entre que el recolector
                        lo listó y que llegamos a leerlo: el TOCTOU de /proc).
    """
    fd_dir = f"{PROC}/{pid}/fd"
    try:
        entradas = os.listdir(fd_dir)
    except PermissionError:
        return SIN_PERMISO
    except _ERRORS_ESPERADOS:
        return None
    except OSError:
        return None

    resultado = []
    for fd in entradas:
        destino = _read_link(f"{fd_dir}/{fd}")
        resultado.append({
            "fd": int(fd) if fd.isdigit() else fd,
            "destino": destino if destino is not None else "?",
            "tipo": _tipo_fd(destino),
        })
    resultado.sort(key=lambda x: x["fd"] if isinstance(x["fd"], int) else 0)
    return resultado


# ---------------------------------------------------------------------------
# Stats globales del sistema
# ---------------------------------------------------------------------------

def read_loadavg():
    raw = _read(f"{PROC}/loadavg")
    if raw is None:
        return None
    partes = raw.split()
    corriendo, total = partes[3].split("/")
    return {
        "load1": float(partes[0]),
        "load5": float(partes[1]),
        "load15": float(partes[2]),
        "procesos_corriendo": int(corriendo),
        "procesos_totales": int(total),
        "ultimo_pid": int(partes[4]),
    }


_MEMINFO_KEYS = (
    "MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached",
    "SwapTotal", "SwapFree", "SwapCached", "Shmem", "Slab",
)


def read_meminfo():
    raw = _read(f"{PROC}/meminfo")
    if raw is None:
        return None
    data = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in _MEMINFO_KEYS:
            m = re.match(r"\s*(\d+)", value)
            data[key] = int(m.group(1)) if m else 0
    return data


def read_global_cpu():
    """Lee la línea 'cpu  ...' de /proc/stat: (user, nice, system, idle, iowait, irq, softirq, steal)."""
    raw = _read(f"{PROC}/stat")
    if raw is None:
        return None
    resultado = {"btime": None, "cpu": None, "ctxt": None, "processes": None,
                 "procs_running": None, "procs_blocked": None}
    for line in raw.splitlines():
        if line.startswith("cpu "):
            campos = [int(x) for x in line.split()[1:]]
            campos += [0] * (8 - len(campos))
            resultado["cpu"] = tuple(campos[:8])
        elif line.startswith("btime"):
            resultado["btime"] = int(line.split()[1])
        elif line.startswith("ctxt"):
            resultado["ctxt"] = int(line.split()[1])
        elif line.startswith("processes"):
            resultado["processes"] = int(line.split()[1])
        elif line.startswith("procs_running"):
            resultado["procs_running"] = int(line.split()[1])
        elif line.startswith("procs_blocked"):
            resultado["procs_blocked"] = int(line.split()[1])
    return resultado


def read_uptime():
    raw = _read(f"{PROC}/uptime")
    if raw is None:
        return None
    partes = raw.split()
    return {"uptime_seg": float(partes[0]), "idle_seg": float(partes[1])}


# ---------------------------------------------------------------------------
# Helpers de cálculo (CPU%, etc.) — puros, sin tocar /proc
# ---------------------------------------------------------------------------

def calcular_cpu_pct(jiffies_prev, jiffies_curr, wall_prev, wall_curr):
    """
    % de CPU = delta de jiffies de trabajo del proceso / delta de tiempo real
    * 100. Deliberadamente NO se divide por la cantidad de cores: un proceso
    con 2 threads a full marca ~200% en la vista "por proceso", que es el
    comportamiento estándar de top/htop (el modo "Irix" que traen por
    defecto). Dividir por N cores sería el modo "Solaris", que oculta
    justamente lo que este TP quiere mostrar: que un proceso puede usar más de
    un núcleo a la vez porque tiene varios LWPs corriendo en paralelo.
    """
    delta_jiffies = jiffies_curr - jiffies_prev
    delta_wall = wall_curr - wall_prev
    if delta_wall <= 0:
        return 0.0
    return max(0.0, (delta_jiffies / CLK_TCK) / delta_wall * 100.0)
