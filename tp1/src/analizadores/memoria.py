"""Analizador "memoria": detalle de memoria virtual/residente y segmentos de /proc/<pid>/maps."""

import procfs
from analizadores._base import por_pid, run_loop


def _extraer(historial, cfg, verbose, log, pids_proxy):
    pids = list(pids_proxy)

    def uno(pid):
        status = procfs.read_status(pid)
        stat = procfs.read_stat(pid)
        if status is None or stat is None:
            return None
        maps = procfs.read_maps_agrupado(pid)

        fila = {
            "pid": pid,
            "vm_size_kb": status.get("VmSize", 0),
            "vm_rss_kb": status.get("VmRSS", 0),
            "vm_hwm_kb": status.get("VmHWM", 0),
            "vm_data_kb": status.get("VmData", 0),
            "vm_stk_kb": status.get("VmStk", 0),
            "vm_exe_kb": status.get("VmExe", 0),
            "vm_lib_kb": status.get("VmLib", 0),
            "vm_swap_kb": status.get("VmSwap", 0),
            "minflt": stat.get("minflt", 0),
            "cminflt": stat.get("cminflt", 0),
            "majflt": stat.get("majflt", 0),
            "cmajflt": stat.get("cmajflt", 0),
            "segmentos": maps,  # None si no tenemos permiso para leer /proc/<pid>/maps
        }
        if verbose:
            # Modo verbose (SIGUSR2): sumamos el desglose fino de RSS que trae
            # status, que en modo normal omitimos porque son 4 campos más por
            # proceso viajando por el Manager en cada refresco.
            fila.update({
                "vm_peak_kb": status.get("VmPeak", 0),
                "rss_anon_kb": status.get("RssAnon", 0),
                "rss_file_kb": status.get("RssFile", 0),
                "rss_shmem_kb": status.get("RssShmem", 0),
            })
        return pid, fila

    return por_pid(pids, uno, log)


def run(pids_proxy, snapshot, interval_value, stop_event, log_queue, **kwargs):
    run_loop("memoria", stop_event, interval_value, snapshot, log_queue,
              _extraer, pids_proxy=pids_proxy, **kwargs)
