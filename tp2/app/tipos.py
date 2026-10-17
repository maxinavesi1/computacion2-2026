"""Los tipos de tarea que sabe ejecutar la plataforma.

Cada tipo es una corrutina con la misma firma:

    async def tipo(params: dict, recursos: Recursos, progreso: Callable[[float], None]) -> dict

- `params` ya viene validado por la API (ver app/modelos.py).
- `recursos` son los objetos compartidos entre tareas (cliente HTTP, pools).
- `progreso(fraccion)` es opcional de usar: actualiza el % que muestra el GET.

Lo que devuelven queda como `resultado` de la tarea. Si lanzan una excepción,
la tarea pasa a `fallida`. Si se las cancela, el CancelledError tiene que
poder atravesarlas: ninguna lo captura.
"""

import asyncio
import multiprocessing
import signal
import time
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx

from app.hashing import hash_iterado

Progreso = Callable[[float], None]

# Cuántas iteraciones de SHA-256 se mandan al executor por vez (~50 ms en una
# CPU actual). Es el compromiso entre la latencia de cancelación (una tarea
# cancelada se entera al instante, pero el bloque que está en vuelo termina
# igual en su hilo/proceso) y el costo fijo de cada viaje al executor.
ITERACIONES_POR_BLOQUE = 100_000


def _ignorar_sigint():
    """Initializer de los procesos del pool.

    Ctrl+C en una terminal manda SIGINT a TODO el grupo de procesos, hijos
    incluidos. Si los hijos lo reciben, mueren a mitad de un bloque y el pool
    queda roto (BrokenProcessPool) justo cuando el padre intenta apagarse
    ordenadamente. Los hijos lo ignoran y es el padre quien decide cuándo
    terminan (pool.shutdown()).
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _calentar():
    return None


@dataclass
class Recursos:
    """Objetos que comparten todas las tareas. Se crean una vez al arrancar."""

    cliente_http: httpx.AsyncClient
    pool_procesos: Executor
    pool_hilos: Executor

    @classmethod
    def crear(cls, procesos: int) -> "Recursos":
        return cls(
            # Un solo cliente para todas las descargas: reutiliza conexiones
            # (keep-alive) y limita cuántas hay abiertas a la vez.
            cliente_http=httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                follow_redirects=True,
            ),
            # spawn explícito en todas las plataformas: el hijo arranca un
            # intérprete limpio en vez de heredar por fork() una copia del
            # padre con el event loop, el socket que escucha en :8000 y las
            # conexiones abiertas. Es más lento de arrancar (por eso el
            # precalentado), pero se comporta igual en Linux, macOS y Windows.
            pool_procesos=ProcessPoolExecutor(
                max_workers=procesos,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_ignorar_sigint,
            ),
            # Pool de hilos propio para el modo "hilos" de hashear. No usamos
            # el executor por defecto del loop porque asyncio lo usa para
            # resolver DNS (getaddrinfo): si lo llenáramos de hashes, las
            # descargas se quedarían esperando para resolver nombres.
            pool_hilos=ThreadPoolExecutor(max_workers=procesos, thread_name_prefix="hashear"),
        )

    async def precalentar(self, procesos: int) -> None:
        """Fuerza el arranque de los procesos del pool antes de la primera tarea.

        ProcessPoolExecutor crea los procesos a demanda, y con spawn cada uno
        tarda en importar todo. Sin esto, el primer `hashear` mediría también
        el arranque de los procesos.
        """
        loop = asyncio.get_running_loop()
        await asyncio.gather(*(loop.run_in_executor(self.pool_procesos, _calentar)
                               for _ in range(procesos)))

    async def cerrar(self) -> None:
        await self.cliente_http.aclose()
        # shutdown(wait=True) bloquea hasta que terminen los hijos: se hace
        # en un hilo para no congelar el loop mientras tanto.
        for pool in (self.pool_procesos, self.pool_hilos):
            await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)


# --- esperar: I/O simulado ------------------------------------------------

async def esperar(params: dict[str, Any], recursos: Recursos, progreso: Progreso) -> dict:
    """Duerme N segundos sin ocupar el loop.

    asyncio.sleep() no bloquea: suspende la corrutina y le pide al loop que
    la despierte en N segundos. Mientras tanto el loop atiende otras cosas.
    Si se cancela la tarea, el CancelledError aparece justo en este await.
    """
    await asyncio.sleep(params["segundos"])
    return {"dormido_s": params["segundos"]}


# --- hashear: CPU-bound ---------------------------------------------------

async def hashear(params: dict[str, Any], recursos: Recursos, progreso: Progreso) -> dict:
    """Aplica SHA-256 N veces al texto.

    Según params["ejecutor"]:
    - "procesos" (default): en el ProcessPoolExecutor. Paralelismo real.
    - "hilos": en un ThreadPoolExecutor. No bloquea el loop, pero compite
      con él por el GIL.
    - "ninguno": directamente en la corrutina. BLOQUEA el event loop durante
      todo el cálculo. Existe sólo para la medición 2 del README.
    """
    datos = params["texto"].encode()
    total = params["iteraciones"]
    modo = params.get("ejecutor", "procesos")
    inicio = time.perf_counter()

    if modo == "ninguno":
        h = hash_iterado(datos, total)  # <- acá se congela el loop, a propósito
        progreso(1.0)
    else:
        executor = recursos.pool_procesos if modo == "procesos" else recursos.pool_hilos
        loop = asyncio.get_running_loop()
        h, hechas = datos, 0
        while hechas < total:
            bloque = min(ITERACIONES_POR_BLOQUE, total - hechas)
            # Entre bloque y bloque la corrutina vuelve a pasar por un await:
            # ahí es donde una cancelación o un timeout la pueden alcanzar.
            h = await loop.run_in_executor(executor, hash_iterado, h, bloque)
            hechas += bloque
            progreso(hechas / total)

    return {
        "hash": h.hex(),
        "iteraciones": total,
        "ejecutor": modo,
        "segundos": round(time.perf_counter() - inicio, 4),
    }


# --- descargar: I/O-bound -------------------------------------------------

async def _descargar_una(cliente: httpx.AsyncClient, url: str) -> dict:
    """Descarga una URL midiendo código, tamaño y tiempo.

    Los errores de red/HTTP se devuelven como dato (una URL caída no hace
    fallar a las demás). Cualquier otra excepción se propaga.
    """
    inicio = time.perf_counter()
    try:
        # stream() en vez de get(): el cuerpo se recorre de a pedazos sin
        # tenerlo entero en memoria. El `async with` garantiza que la
        # respuesta se cierre (y la conexión vuelva al pool o se descarte)
        # pase lo que pase, incluida una cancelación a mitad de camino.
        async with cliente.stream("GET", url) as respuesta:
            tamano = 0
            async for pedazo in respuesta.aiter_bytes():
                tamano += len(pedazo)
        return {"url": url, "codigo": respuesta.status_code, "bytes": tamano,
                "segundos": round(time.perf_counter() - inicio, 4)}
    except httpx.HTTPError as e:
        return {"url": url, "error": f"{type(e).__name__}: {e}",
                "segundos": round(time.perf_counter() - inicio, 4)}


async def descargar(params: dict[str, Any], recursos: Recursos, progreso: Progreso) -> dict:
    """Descarga una lista de URLs, concurrentemente (default) o una por una.

    concurrente=True usa un TaskGroup: todas las descargas arrancan juntas y
    el total tarda lo que la más lenta. Además, si la tarea se cancela o una
    descarga lanza un error inesperado, el TaskGroup cancela a las demás y
    espera a que terminen antes de propagar: no quedan corrutinas huérfanas.

    concurrente=False hace un `await` por vez: el total es la suma. Existe
    para la medición 1 del README.
    """
    urls = params["urls"]
    terminadas = 0

    async def una(url: str) -> dict:
        nonlocal terminadas  # sin Lock: entre dos awaits nadie más corre
        resultado = await _descargar_una(recursos.cliente_http, url)
        terminadas += 1
        progreso(terminadas / len(urls))
        return resultado

    concurrente = params.get("concurrente", True)
    inicio = time.perf_counter()
    if concurrente:
        async with asyncio.TaskGroup() as grupo:
            tareas = [grupo.create_task(una(url)) for url in urls]
        descargas = [t.result() for t in tareas]
    else:
        descargas = [await una(url) for url in urls]

    errores = sum(1 for d in descargas if "error" in d)
    return {
        "concurrente": concurrente,
        "segundos_total": round(time.perf_counter() - inicio, 4),
        "ok": len(descargas) - errores,
        "errores": errores,
        "descargas": descargas,
    }


# Registro de tipos: agregar un tipo nuevo es escribir la corrutina, sumarla
# acá y declarar sus parámetros en app/modelos.py.
TIPOS: dict[str, Callable[..., Any]] = {
    "descargar": descargar,
    "hashear": hashear,
    "esperar": esperar,
}
