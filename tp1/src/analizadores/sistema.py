"""
Analizador "sistema": stats globales de la máquina (no de un proceso en
particular): CPU agregada, load average, memoria, y un recorrido liviano de
TODOS los pids para contar por estado (¿cuántos zombies hay ahora mismo?) y
armar el top 3 por CPU% y por RSS.

Diseño: este analizador mantiene su PROPIO historial de jiffies por pid para
calcular el top 3 por CPU, en vez de leer el resultado ya calculado por el
analizador "resumen". Es un poco de trabajo duplicado, pero mantiene a los
analizadores totalmente desacoplados entre sí (ninguno depende de que otro
haya corrido antes, o de su intervalo de refresco) — ver README, sección
"Decisiones de diseño".

El top 3 además se publica en dos multiprocessing.Array de enteros
(top3_cpu_pids, top3_mem_pids) para que el Display pueda leerlo con overhead
mínimo sin tener que deserializar todo snapshot['sistema'] sólo para pintar
el encabezado. Es un ejemplo deliberado de "Array" para datos de tamaño fijo,
en contraste con el Manager.dict que se usa para las estructuras variables.
"""

import time
import procfs
from analizadores._base import run_loop

_ESTADOS_RELEVANTES = ("R", "S", "D", "T", "Z", "I")


def _extraer(historial, cfg, verbose, pids_proxy, top3_cpu_arr, top3_mem_arr):
    pids = list(pids_proxy)
    ahora = time.time()

    # --- CPU global (delta contra la lectura anterior) ---
    cpu_actual = procfs.read_global_cpu()
    cpu_pct = {"user": 0.0, "system": 0.0, "idle": 0.0, "iowait": 0.0}
    if cpu_actual and cpu_actual["cpu"]:
        prev = historial.get("_cpu_global")
        if prev is not None:
            u0, n0, s0, i0, io0, irq0, sirq0, st0 = prev
            u1, n1, s1, i1, io1, irq1, sirq1, st1 = cpu_actual["cpu"]
            total_prev = sum(prev)
            total_actual = sum(cpu_actual["cpu"])
            delta_total = max(1, total_actual - total_prev)
            cpu_pct = {
                "user": round((u1 - u0 + n1 - n0) / delta_total * 100, 1),
                "system": round((s1 - s0) / delta_total * 100, 1),
                "idle": round((i1 - i0) / delta_total * 100, 1),
                "iowait": round((io1 - io0) / delta_total * 100, 1),
            }
        historial["_cpu_global"] = cpu_actual["cpu"]

    loadavg = procfs.read_loadavg()
    meminfo = procfs.read_meminfo()
    uptime = procfs.read_uptime()

    # --- recorrido liviano de todos los procesos: estados, threads totales, top3 ---
    conteo_estados = {e: 0 for e in _ESTADOS_RELEVANTES}
    threads_totales = 0
    procesos_totales = 0
    candidatos_cpu = []  # (cpu_pct, pid, comm)
    candidatos_mem = []  # (rss_kb, pid, comm)
    hist_cpu = historial.setdefault("_cpu_por_pid", {})
    vistos = set()

    for pid in pids:
        stat = procfs.read_stat(pid)
        if stat is None:
            continue
        procesos_totales += 1
        estado = stat["state"]
        conteo_estados[estado] = conteo_estados.get(estado, 0) + 1
        threads_totales += stat.get("num_threads", 1)

        jiffies_actuales = stat["utime"] + stat["stime"]
        vistos.add(pid)
        prev = hist_cpu.get(pid)
        cpu_pid_pct = 0.0
        if prev is not None:
            jiffies_prev, wall_prev = prev
            cpu_pid_pct = procfs.calcular_cpu_pct(jiffies_prev, jiffies_actuales, wall_prev, ahora)
        hist_cpu[pid] = (jiffies_actuales, ahora)
        candidatos_cpu.append((cpu_pid_pct, pid, stat["comm"]))

        status = procfs.read_status(pid)
        rss = status.get("VmRSS", 0) if status else 0
        candidatos_mem.append((rss, pid, stat["comm"]))

    for pid_viejo in list(hist_cpu.keys()):
        if pid_viejo not in vistos:
            del hist_cpu[pid_viejo]

    candidatos_cpu.sort(reverse=True)
    candidatos_mem.sort(reverse=True)
    top3_cpu = [{"pid": p, "comm": c, "cpu_pct": round(v, 1)} for v, p, c in candidatos_cpu[:3]]
    top3_mem = [{"pid": p, "comm": c, "rss_kb": v} for v, p, c in candidatos_mem[:3]]

    # publicar en los Array de tamaño fijo (con su lock por defecto, para escritura consistente)
    with top3_cpu_arr.get_lock():
        for i in range(3):
            top3_cpu_arr[i] = top3_cpu[i]["pid"] if i < len(top3_cpu) else -1
    with top3_mem_arr.get_lock():
        for i in range(3):
            top3_mem_arr[i] = top3_mem[i]["pid"] if i < len(top3_mem) else -1

    return {
        "cpu_pct": cpu_pct,
        "loadavg": loadavg,
        "meminfo": meminfo,
        "uptime": uptime,
        "procesos_totales": procesos_totales,
        "threads_totales": threads_totales,
        "conteo_estados": conteo_estados,
        "zombies": conteo_estados.get("Z", 0),
        "top3_cpu": top3_cpu,
        "top3_mem": top3_mem,
        "btime": cpu_actual["btime"] if cpu_actual else None,
    }


def run(pids_proxy, snapshot, interval_value, stop_event, log_queue,
        top3_cpu_arr, top3_mem_arr, **kwargs):
    run_loop("sistema", stop_event, interval_value, snapshot, log_queue,
              _extraer, pids_proxy=pids_proxy,
              top3_cpu_arr=top3_cpu_arr, top3_mem_arr=top3_mem_arr, **kwargs)
