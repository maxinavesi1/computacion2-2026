#!/usr/bin/env python3
"""
main.py — orquestador del monitor.

Acá se crea TODA la memoria compartida (Manager dict/list, Value, Array,
Queue, Pipe) ANTES de lanzar ningún proceso hijo, se lanzan el recolector,
los 7 analizadores y el display, y el proceso principal queda a cargo de:

  1. Manejar las señales que recibe el monitor (self-pipe, ver senales.py).
  2. Drenar la cola de logs de errores de los analizadores a un archivo.
  3. Coordinar el shutdown limpio (por señal o porque el usuario apretó 'q'
     en el display) uniendo (join) a todos los procesos hijos.

Este módulo es intencionalmente "tonto": no sabe nada de /proc ni de curses,
sólo arma el grafo de procesos y IPC y reacciona a señales/errores.
"""

import json
import multiprocessing as mp
import os
import queue
import sys
import time

import display
import recolector
import senales
import vistas
from analizadores import fds, memoria, resumen, scheduling
from analizadores import senales as an_senales
from analizadores import sistema, threads

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("MONITOR_CONFIG", os.path.join(BASE_DIR, "config.json"))

# Los nombres y los intervalos (default y mínimo) de las 7 vistas salen de
# vistas.py, la misma tabla que usa el Display. Ver el docstring de ese módulo.
NOMBRES_VISTAS = vistas.NOMBRES

_MODULOS_ANALIZADORES = {
    "resumen": resumen,
    "memoria": memoria,
    "fds": fds,
    "threads": threads,
    "senales": an_senales,
    "scheduling": scheduling,
    "sistema": sistema,
}


def cargar_config(path):
    try:
        with open(path, "r") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[main] no pude leer {path} ({e!r}); uso valores por defecto", file=sys.stderr)
        cfg = {}
    cfg.setdefault("intervalos", {})
    for nombre, default in vistas.DEFAULTS.items():
        cfg["intervalos"].setdefault(nombre, default)
    cfg["intervalos"].setdefault("recolector", vistas.DEFAULT_RECOLECTOR)
    cfg.setdefault("fds", {}).setdefault("max_mostrados", 200)
    cfg.setdefault("filtros_default", {"comando": "", "usuario": ""})
    cfg.setdefault("dump_dir", ".")
    cfg.setdefault("log_file", "monitor.log")
    cfg.setdefault("modo_daemon", False)
    cfg.setdefault("daemon_intervalo_log", 5.0)
    return cfg


class Orquestador:
    """Agrupa toda la memoria compartida y los Process/handles para que main() quede legible."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.manager = mp.Manager()

        # snapshot global: única fuente de verdad que lee el Display
        self.snapshot = self.manager.dict({v: {"data": {}, "ts": 0.0} for v in NOMBRES_VISTAS})

        # dict chico de control/orquestación (reload por SIGHUP, contador de secuencia)
        self.control = self.manager.dict({
            "reload_seq": 0,
            "filtros_default": dict(cfg["filtros_default"]),
        })

        # lista de pids vivos, publicada por el recolector, consumida por los 7 analizadores
        self.pids = self.manager.list()

        # un Value de intervalo por vista + uno para el recolector, ajustables en
        # caliente. Se pasan por clamp_intervalo desde el arranque: un
        # config.json con "senales": 0.1 no debería poder saturar la CPU con
        # algo que la TUI después se niega a bajar tanto con la tecla '-'.
        self.interval_values = {
            v: mp.Value("d", vistas.clamp_intervalo(v, cfg["intervalos"][v])) for v in NOMBRES_VISTAS
        }
        self.recolector_interval = mp.Value(
            "d", max(vistas.MINIMO_RECOLECTOR, float(cfg["intervalos"]["recolector"])))

        self.verbose = mp.Value("b", False)
        self.stop_event = mp.Event()
        self.log_queue = mp.Queue()

        # arrays de tamaño fijo para el top3 de CPU/memoria (ver analizadores/sistema.py)
        self.top3_cpu_arr = mp.Array("i", 3, lock=True)
        self.top3_mem_arr = mp.Array("i", 3, lock=True)
        for i in range(3):
            self.top3_cpu_arr[i] = -1
            self.top3_mem_arr[i] = -1

        # un Pipe (duplex) por analizador, usado para el broadcast de config en SIGHUP
        self._pipes = {v: mp.Pipe() for v in NOMBRES_VISTAS}  # {nombre: (parent_conn, child_conn)}

        self.procesos = {}  # nombre -> multiprocessing.Process

    # -- lanzado de procesos ------------------------------------------------

    def lanzar_todo(self):
        p = mp.Process(target=recolector.run, name="recolector",
                        args=(self.pids, self.stop_event, self.recolector_interval, self.log_queue))
        p.start()
        self.procesos["recolector"] = p

        for nombre in NOMBRES_VISTAS:
            modulo = _MODULOS_ANALIZADORES[nombre]
            _, child_conn = self._pipes[nombre]
            # cfg_inicial: antes los analizadores arrancaban con cfg={} y sólo
            # veían el archivo después del primer SIGHUP, así que valores como
            # fds.max_mostrados se ignoraban en el arranque.
            # verbose_value: el Value que togglea SIGUSR2, para que el modo
            # verbose tenga efecto real sobre los datos y no sólo sobre el
            # cartelito del encabezado de la TUI.
            kwargs = dict(reload_conn=child_conn, verbose_value=self.verbose,
                          cfg_inicial=self.cfg)
            args = [self.pids, self.snapshot, self.interval_values[nombre], self.stop_event, self.log_queue]
            if nombre == "sistema":
                p = mp.Process(target=modulo.run, name=f"analizador-{nombre}",
                                args=(*args, self.top3_cpu_arr, self.top3_mem_arr), kwargs=kwargs)
            else:
                p = mp.Process(target=modulo.run, name=f"analizador-{nombre}", args=tuple(args), kwargs=kwargs)
            p.start()
            self.procesos[nombre] = p

        if not self.cfg["modo_daemon"]:
            p = mp.Process(
                target=display.run, name="display",
                args=(self.snapshot, self.pids, self.interval_values, self.stop_event,
                      self.verbose, self.top3_cpu_arr, self.top3_mem_arr, self.log_queue,
                      self.cfg["dump_dir"]),
                kwargs={"control": self.control},
            )
            p.start()
            self.procesos["display"] = p

    # -- acciones disparadas por señales -------------------------------------

    def reload_config(self):
        nuevo_cfg = cargar_config(CONFIG_PATH)
        self.cfg.update(nuevo_cfg)
        # Mismo clamp que aplican las teclas +/- del Display (vistas.py): antes
        # esta ruta usaba un piso fijo de 0.1s, así que un SIGHUP podía dejar
        # una vista por debajo del mínimo que la propia TUI respetaba.
        for nombre in NOMBRES_VISTAS:
            self.interval_values[nombre].value = vistas.clamp_intervalo(
                nombre, nuevo_cfg["intervalos"][nombre])
        self.recolector_interval.value = max(
            vistas.MINIMO_RECOLECTOR, float(nuevo_cfg["intervalos"]["recolector"]))

        # broadcast de la config completa a cada analizador vía su Pipe
        for nombre in NOMBRES_VISTAS:
            parent_conn, _ = self._pipes[nombre]
            try:
                parent_conn.send(nuevo_cfg)
            except Exception:
                pass

        self.control["filtros_default"] = dict(nuevo_cfg["filtros_default"])
        self.control["reload_seq"] = self.control["reload_seq"] + 1
        return nuevo_cfg

    def dump_snapshot(self):
        ts = int(time.time())
        ruta = os.path.join(self.cfg["dump_dir"], f"dump_{ts}.json")
        plano = {clave: valor for clave, valor in self.snapshot.items()}
        try:
            with open(ruta, "w") as f:
                json.dump(plano, f, indent=2, default=str)
        except OSError as e:
            self._log(f"[main] no pude escribir el dump en {ruta}: {e!r}")
        else:
            self._log(f"[main] dump escrito en {ruta}")

    def toggle_verbose(self):
        with self.verbose.get_lock():
            self.verbose.value = not self.verbose.value
        self._log(f"[main] modo verbose ahora: {'ON' if self.verbose.value else 'off'}")

    def _log(self, mensaje):
        try:
            self.log_queue.put_nowait(mensaje)
        except Exception:
            pass

    # -- shutdown -------------------------------------------------------------

    def apagar(self):
        self.stop_event.set()
        for nombre, p in self.procesos.items():
            p.join(timeout=3)
            if p.is_alive():
                self._log(f"[main] {nombre} no terminó a tiempo, forzando terminate()")
                p.terminate()
                p.join(timeout=1)
        self.manager.shutdown()


def loop_daemon(orq, log_fh, manejador):
    """Bonus: modo daemon sin TUI, sólo loggeando snapshots periódicos a archivo (ver README, extensiones)."""
    intervalo = orq.cfg.get("daemon_intervalo_log", 5.0)
    ultimo_log = 0.0
    while not orq.stop_event.is_set():
        # Igual que el loop interactivo: procesamos señales pendientes y
        # drenamos logs cada vuelta, aunque acá no haya Display. Antes este
        # loop sólo dormía sobre stop_event y nunca miraba el self-pipe, lo
        # cual dejaba SIGINT/SIGTERM/SIGHUP/etc. sin efecto en modo daemon.
        for signum in manejador.senales_pendientes(timeout=min(0.3, intervalo)):
            _procesar_senal(signum, orq, log_fh)
        _drenar_logs(orq, log_fh)

        ahora = time.time()
        if ahora - ultimo_log >= intervalo:
            ultimo_log = ahora
            sistema_data = orq.snapshot.get("sistema", {}).get("data")
            resumen_data = orq.snapshot.get("resumen", {}).get("data", {})
            if sistema_data:
                linea = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} procesos={sistema_data['procesos_totales']} "
                          f"zombies={sistema_data['zombies']} cpu_user={sistema_data['cpu_pct']['user']}% "
                          f"threads_totales={sistema_data['threads_totales']} pids_monitoreados={len(resumen_data)}")
                log_fh.write(linea + "\n")
                log_fh.flush()


def main():
    cfg = cargar_config(CONFIG_PATH)
    if "--daemon" in sys.argv:
        cfg["modo_daemon"] = True

    # Los handlers se instalan ANTES de crear el Manager y de forkear a los
    # hijos, y todo lo que viene después queda dentro del try/finally. Al
    # revés (como estaba antes) hay una ventana de varios cientos de
    # milisegundos —arrancar el Manager + 9 fork()— en la que un Ctrl+C mata a
    # main con el traceback por defecto y deja los hijos y el proceso del
    # Manager huérfanos, sin nadie que llame a stop_event.set().
    #
    # El precio de instalarlos antes es que cada hijo hereda la disposición del
    # padre vía fork(); por eso todos llaman a senales.reset_en_hijo() como
    # primera cosa (ver senales.py).
    manejador = senales.ManejadorSenales()
    orq = None
    log_fh = None

    try:
        orq = Orquestador(cfg)
        orq.lanzar_todo()

        log_path = os.path.join(cfg["dump_dir"], cfg["log_file"])
        log_fh = open(log_path, "a", buffering=1)
        log_fh.write(f"\n=== monitor iniciado {time.strftime('%Y-%m-%d %H:%M:%S')} (pid {os.getpid()}) ===\n")

        if cfg["modo_daemon"]:
            loop_daemon(orq, log_fh, manejador)
        else:
            while not orq.stop_event.is_set():
                for signum in manejador.senales_pendientes(timeout=0.3):
                    _procesar_senal(signum, orq, log_fh)
                _drenar_logs(orq, log_fh)
                # si el display murió solo (ej: terminal cerrada), no seguir en un loop zombie
                disp = orq.procesos.get("display")
                if disp is not None and not disp.is_alive():
                    orq.stop_event.set()
    except KeyboardInterrupt:
        # Con el self-pipe instalado esto no debería dispararse (SIGINT se
        # procesa como un byte en el loop, no como excepción), pero lo dejamos
        # como red de seguridad: si llega, es un SIGINT y se trata igual.
        pass
    finally:
        # Cada paso va condicionado a que la etapa correspondiente del arranque
        # haya llegado a completarse: si falló a mitad de camino (ej: sin
        # permiso para escribir el log) igual queremos apagar lo que sí se
        # levantó, en vez de dejar 9 hijos dando vueltas.
        #
        # El orden importa: primero apagamos a los hijos (que durante el
        # shutdown todavía pueden escribir a log_queue), después drenamos la
        # cola completa, y sólo al final cerramos el archivo.
        if orq is not None:
            orq.apagar()
            if log_fh is not None:
                _drenar_logs(orq, log_fh)
        if log_fh is not None:
            log_fh.write(f"=== monitor apagado {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            log_fh.close()
        manejador.cerrar()


def _procesar_senal(signum, orq, log_fh):
    import signal as sm
    if signum in (sm.SIGINT, sm.SIGTERM):
        orq._log(f"[main] señal {sm.Signals(signum).name} recibida: iniciando shutdown limpio")
        orq.stop_event.set()
    elif signum == sm.SIGHUP:
        orq.reload_config()
        orq._log("[main] SIGHUP recibido: config.json recargado")
    elif signum == sm.SIGUSR1:
        orq.dump_snapshot()
    elif signum == sm.SIGUSR2:
        orq.toggle_verbose()
    elif signum == sm.SIGWINCH:
        # el display recalcula el tamaño de la terminal en cada frame
        # (curses stdscr.getmaxyx()), así que no hace falta reenviar nada:
        # sólo lo dejamos loggeado para que quede visible que se recibió.
        orq._log("[main] SIGWINCH recibido (resize de terminal)")


def _drenar_logs(orq, log_fh):
    while True:
        try:
            msg = orq.log_queue.get_nowait()
        except queue.Empty:
            break
        log_fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    log_fh.flush()


if __name__ == "__main__":
    main()
