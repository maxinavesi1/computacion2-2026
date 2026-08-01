"""
Analizador "resumen": la vista por defecto. Un renglón por proceso con los
datos básicos que se ven en la lista superior de la TUI, sin importar qué
vista de detalle esté activa.
"""

import time
import procfs
from analizadores._base import por_pid, run_loop


def _extraer(historial, cfg, verbose, log, pids_proxy):
    pids = list(pids_proxy)  # copia local: evita que la lista cambie bajo nuestros pies a mitad de la vuelta
    ahora = time.time()
    claves_vistas = set()

    def uno(pid):
        stat = procfs.read_stat(pid)
        if stat is None:
            return None  # murió entre que lo listamos y lo leímos (TOCTOU): lo salteamos esta vuelta
        status = procfs.read_status(pid)
        cmdline = procfs.read_cmdline(pid)

        # La clave es (pid, starttime), no el pid solo: ver procfs.clave_proceso.
        clave = procfs.clave_proceso(pid, stat)
        claves_vistas.add(clave)
        jiffies_actuales = stat["utime"] + stat["stime"]
        prev = historial.get(clave)
        if prev is not None:
            jiffies_prev, wall_prev = prev
            cpu_pct = procfs.calcular_cpu_pct(jiffies_prev, jiffies_actuales, wall_prev, ahora)
        else:
            cpu_pct = 0.0  # primera vez que vemos este proceso: no hay delta todavía
        historial[clave] = (jiffies_actuales, ahora)

        # Uid/Gid vienen como 4 valores (real, effective, saved, filesystem);
        # nos queda el real, que es el que interesa para "de quién es este
        # proceso". La consigna pide UID *y* GID además del nombre de usuario.
        uid = status["Uid"][0] if status and "Uid" in status else None
        gid = status["Gid"][0] if status and "Gid" in status else None
        return pid, {
            "pid": pid,
            "ppid": stat["ppid"],
            "uid": uid,
            "gid": gid,
            "usuario": procfs.username_for_uid(uid) if uid is not None else "?",
            "grupo": procfs.groupname_for_gid(gid) if gid is not None else "?",
            "estado": stat["state"],
            "estado_nombre": procfs.state_name(stat["state"]),
            "comando": cmdline if cmdline else f"[{stat['comm']}]",
            "comm": stat["comm"],
            "cpu_pct": round(cpu_pct, 1),
            "threads": status.get("Threads", 1) if status else 1,
            "rss_kb": status.get("VmRSS", 0) if status else 0,
        }

    resultado = por_pid(pids, uno, log)

    # limpiar historial de procesos que ya no existen, para no acumular memoria
    # indefinidamente en una corrida larga
    for clave_vieja in list(historial.keys()):
        if clave_vieja not in claves_vistas:
            del historial[clave_vieja]

    return resultado


def run(pids_proxy, snapshot, interval_value, stop_event, log_queue, **kwargs):
    run_loop("resumen", stop_event, interval_value, snapshot, log_queue,
              _extraer, pids_proxy=pids_proxy, **kwargs)
