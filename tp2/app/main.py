"""Punto de entrada: `python -m app.main`.

Señales y apagado ordenado
--------------------------
uvicorn registra, con signal.signal(), un handler para SIGINT y SIGTERM (y
SIGBREAK en Windows): su método `handle_exit`, que sólo levanta el flag
`should_exit`. El loop principal de uvicorn lo consulta cada 0,1 s; cuando
lo ve, deja de escuchar en el puerto, espera a las peticiones en curso y
ejecuta el shutdown del lifespan (app/api.py), que es donde la Plataforma
espera a las tareas, vuelca el estado y libera recursos.

Extendemos `handle_exit` para cortar además el ingreso de tareas en el
mismo instante en que llega la señal, sin esperar a ese ciclo de 0,1 s ni a
que se cierre el socket.

Por qué signal.signal + flag y no loop.add_signal_handler(): el segundo sólo
existe en Unix. Así funciona igual en Linux, macOS y Windows, que es lo que
sugiere la consigna.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable

import uvicorn
from fastapi import FastAPI

from app.api import crear_app
from app.config import Config

log = logging.getLogger(__name__)


class Servidor(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, al_recibir_senal: Callable[[int], None]):
        super().__init__(config)
        self._al_recibir_senal = al_recibir_senal

    def handle_exit(self, sig: int, frame) -> None:
        # Esto corre como handler de señal: en el hilo principal, entre dos
        # bytecodes de lo que sea que se estuviera ejecutando (puede ser en
        # medio de una corrutina). Por eso sólo se tocan flags.
        self._al_recibir_senal(sig)
        super().handle_exit(sig, frame)


def _cortar_ingreso(app: FastAPI, sig: int) -> None:
    plataforma = getattr(app.state, "plataforma", None)
    if plataforma is None:  # la señal llegó antes de terminar de arrancar
        return
    plataforma.dejar_de_aceptar()
    # Ni siquiera logueamos acá: si la señal interrumpió al hilo principal
    # mientras escribía en stderr, escribir de nuevo desde el handler puede
    # fallar ("reentrant call"). call_soon_threadsafe() deja el log agendado
    # para que lo ejecute el loop en su próxima vuelta: es el mismo truco
    # del self-pipe que usa asyncio por dentro para las señales.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.call_soon_threadsafe(log.warning, "Recibí %s: no acepto más tareas y empiezo el apagado",
                              signal.Signals(sig).name)


def crear_servidor(config: Config) -> Servidor:
    app = crear_app(config)
    uv_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.puerto,
        log_config=None,             # que uvicorn use el logging configurado acá
        timeout_graceful_shutdown=5,  # máximo para cerrar las conexiones abiertas
    )
    return Servidor(uv_config, al_recibir_senal=lambda sig: _cortar_ingreso(app, sig))


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # httpx loguea cada request en INFO: con descargas de 20 URLs, es ruido.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    servidor = crear_servidor(Config.desde_entorno())
    # Tras un apagado por SIGINT, uvicorn vuelve a lanzar la señal con el
    # handler original restaurado (KeyboardInterrupt); acá ya está todo cerrado.
    with contextlib.suppress(KeyboardInterrupt):
        servidor.run()


# La guarda es obligatoria por el ProcessPoolExecutor con "spawn": cada
# proceso hijo vuelve a importar este módulo (como "__mp_main__"). Sin el if,
# cada hijo levantaría su propio servidor, que crearía su propio pool, que...
if __name__ == "__main__":
    main()
