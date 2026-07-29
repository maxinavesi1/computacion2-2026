"""
senales.py — manejo de las señales que recibe EL MONITOR (proceso principal),
no confundir con analizadores/senales.py (que lee qué señales tiene
configuradas cada proceso *del sistema*).

Patrón usado: self-pipe (visto en clase 6). Un signal handler en Python (o en
C) sólo puede llamar de forma segura a un conjunto muy chico de funciones
async-signal-safe; llamar a print(), hacer I/O de archivos, tocar estructuras
de datos complejas o (peor) tocar un multiprocessing.Manager desde dentro de
un handler es Undefined Behavior en el caso general. La solución estándar es
que el handler haga lo mínimo posible —escribir un byte a un pipe con
os.write(), que sí es async-signal-safe— y que el trabajo real (releer
config.json, volcar el snapshot a JSON, etc.) lo haga el loop principal,
fuera del contexto de señal, cuando detecta que llegó ese byte.

Alternativa que también cumple el mismo objetivo y que Python expone
directamente: signal.set_wakeup_fd(fd), que hace que el intérprete escriba
automáticamente a un fd cada vez que llega una señal (sin que nosotros
escribamos el handler a mano). Acá implementamos el self-pipe "a mano" con
signal.signal() porque necesitamos saber CUÁL señal llegó (set_wakeup_fd
escribe el número de señal como un byte igual, así que en la práctica ambos
enfoques terminan siendo equivalentes; dejamos el enfoque explícito porque
es más claro para el informe y las preguntas de la corrección).
"""

import os
import select
import signal

SENALES_MANEJADAS = (
    signal.SIGINT, signal.SIGTERM, signal.SIGHUP,
    signal.SIGUSR1, signal.SIGUSR2, signal.SIGWINCH,
)


def reset_en_hijo(dejar_en_default=()):
    """
    Se llama al arrancar CADA proceso hijo, como primera cosa.

    El proceso principal instala sus handlers ANTES de forkear (para no tener
    una ventana de arranque en la que un Ctrl+C mate al monitor dejando hijos
    huérfanos), y fork() copia la disposición de señales del padre. Eso
    significa que sin este reset cada hijo heredaría el handler self-pipe del
    padre: escribiría al pipe de señales del padre —un fd que el hijo también
    heredó— y nadie leería nunca esos bytes del lado del hijo.

    Así que en el hijo:
      - SIGINT/SIGTERM van a SIG_IGN: el shutdown lo coordina el padre vía
        stop_event, no queremos que el Ctrl+C de la terminal (que en Linux le
        llega a TODO el grupo de procesos en foreground) mate a un analizador a
        mitad de una escritura al Manager.
      - el resto (HUP/USR1/USR2/WINCH) va a SIG_IGN también: son señales de
        control dirigidas al monitor como un todo, y el padre ya las traduce a
        acciones concretas (broadcast de config por Pipe, dump, etc.).
      - salvo las que se pidan en `dejar_en_default`, que vuelven a SIG_DFL.
        El Display usa esto para SIGWINCH: ncurses instala su propio handler de
        resize sólo si encuentra la disposición en SIG_DFL, así que si la
        dejáramos en SIG_IGN romperíamos el redimensionado de la terminal.
    """
    for sig in SENALES_MANEJADAS:
        try:
            signal.signal(sig, signal.SIG_DFL if sig in dejar_en_default else signal.SIG_IGN)
        except (OSError, ValueError):
            pass


class ManejadorSenales:
    def __init__(self):
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)
        os.set_blocking(self._w, False)
        for sig in SENALES_MANEJADAS:
            signal.signal(sig, self._handler)

    def _handler(self, signum, frame):
        # ÚNICA operación permitida acá adentro: escribir un byte a un pipe.
        # os.write sobre un fd non-blocking puede levantar BlockingIOError si
        # el pipe está lleno (buffer de 64KB en Linux); en ese caso no pasa
        # nada grave, el próximo poll() del loop principal igual va a
        # despertar por los bytes que sí entraron.
        try:
            os.write(self._w, bytes([signum]))
        except (BlockingIOError, OSError):
            pass

    def senales_pendientes(self, timeout=0.0):
        """
        No bloqueante (con timeout opcional): devuelve la lista de números de
        señal recibidos desde la última llamada, en el orden en que llegaron.
        """
        listos, _, _ = select.select([self._r], [], [], timeout)
        if not listos:
            return []
        try:
            datos = os.read(self._r, 4096)
        except BlockingIOError:
            return []
        return list(datos)

    def fileno(self):
        """Para poder usar este objeto directamente en un select() externo si hiciera falta."""
        return self._r

    def cerrar(self):
        for sig in SENALES_MANEJADAS:
            try:
                signal.signal(sig, signal.SIG_DFL)
            except Exception:
                pass
        os.close(self._r)
        os.close(self._w)
