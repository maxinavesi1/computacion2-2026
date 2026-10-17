"""Tests de los tres tipos de tarea: esperar, hashear y descargar.

Las descargas se testean contra httpx.MockTransport: sin red, con demoras
controladas, para que los tiempos sean predecibles.
"""

import asyncio
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import httpx
import pytest

from app import tipos
from app.hashing import hash_iterado
from app.tipos import Recursos, descargar, esperar, hashear


def _sin_progreso(_):
    pass


@pytest.fixture(scope="module")
def pool_procesos():
    # spawn, igual que en producción (ver app/tipos.py).
    pool = ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn"))
    yield pool
    pool.shutdown(cancel_futures=True)


@pytest.fixture
def pool_hilos():
    pool = ThreadPoolExecutor(max_workers=2)
    yield pool
    pool.shutdown(cancel_futures=True)


@pytest.fixture
def recursos(pool_procesos, pool_hilos):
    """Recursos con un cliente HTTP que no sale a la red."""
    async def sin_red(request):
        return httpx.Response(200, content=b"")

    return Recursos(
        cliente_http=httpx.AsyncClient(transport=httpx.MockTransport(sin_red)),
        pool_procesos=pool_procesos,
        pool_hilos=pool_hilos,
    )


def _con_transporte(recursos, handler):
    recursos.cliente_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return recursos


async def _peor_hueco_del_loop(corrutina):
    """Corre `corrutina` mientras un "latido" intenta despertarse cada 5 ms.

    Devuelve (resultado, hueco más largo entre latidos). Si el event loop
    está libre, el hueco ronda los 5 ms; si algo lo bloquea, el hueco mide
    cuánto duró el bloqueo.
    """
    huecos = []

    async def latido():
        anterior = time.perf_counter()
        while True:
            await asyncio.sleep(0.005)
            ahora = time.perf_counter()
            huecos.append(ahora - anterior)
            anterior = ahora

    tarea_latido = asyncio.create_task(latido())
    await asyncio.sleep(0.02)
    try:
        resultado = await corrutina
        await asyncio.sleep(0.02)  # que el latido registre el último hueco
    finally:
        tarea_latido.cancel()
    return resultado, max(huecos)


# --- esperar --------------------------------------------------------------

async def test_esperar_duerme_lo_pedido(recursos):
    inicio = time.perf_counter()
    resultado = await esperar({"segundos": 0.2}, recursos, _sin_progreso)

    # Margen inferior: el loop puede despertar un timer hasta una resolución
    # de reloj antes (~15.6 ms en Windows).
    assert 0.18 <= time.perf_counter() - inicio < 0.4
    assert resultado == {"dormido_s": 0.2}


async def test_esperar_se_cancela_enseguida(recursos):
    tarea = asyncio.create_task(esperar({"segundos": 30}, recursos, _sin_progreso))
    await asyncio.sleep(0.05)
    inicio = time.perf_counter()
    tarea.cancel()

    with pytest.raises(asyncio.CancelledError):
        await tarea
    assert time.perf_counter() - inicio < 0.1


# --- hashear --------------------------------------------------------------

@pytest.mark.parametrize("ejecutor", ["procesos", "hilos", "ninguno"])
async def test_hashear_da_el_hash_iterado_con_cualquier_ejecutor(recursos, ejecutor):
    params = {"texto": "hola", "iteraciones": 250_000, "ejecutor": ejecutor}

    resultado = await hashear(params, recursos, _sin_progreso)

    assert resultado["hash"] == hash_iterado(b"hola", 250_000).hex()
    assert resultado["iteraciones"] == 250_000
    assert resultado["ejecutor"] == ejecutor
    assert resultado["segundos"] > 0


async def test_hashear_reporta_progreso_por_bloque(recursos, monkeypatch):
    monkeypatch.setattr(tipos, "ITERACIONES_POR_BLOQUE", 1000)
    avances = []

    await hashear({"texto": "x", "iteraciones": 3500, "ejecutor": "hilos"}, recursos, avances.append)

    # 3 bloques de 1000 y uno de 500.
    assert avances == [1000 / 3500, 2000 / 3500, 3000 / 3500, 1.0]


@pytest.mark.parametrize("ejecutor", ["procesos", "hilos"])
async def test_hashear_con_executor_no_bloquea_el_event_loop(recursos, ejecutor):
    params = {"texto": "x", "iteraciones": 600_000, "ejecutor": ejecutor}

    _, peor_hueco = await _peor_hueco_del_loop(hashear(params, recursos, _sin_progreso))

    # Con el trabajo afuera del loop, el latido sigue andando. Con hilos el
    # hueco es algo mayor por la competencia por el GIL (README §3.2).
    assert peor_hueco < 0.15


async def test_hashear_sin_executor_congela_el_event_loop(recursos):
    params = {"texto": "x", "iteraciones": 600_000, "ejecutor": "ninguno"}

    resultado, peor_hueco = await _peor_hueco_del_loop(hashear(params, recursos, _sin_progreso))

    # El latido no pudo correr durante TODO el cálculo: el loop estuvo tomado.
    assert peor_hueco >= resultado["segundos"] * 0.9


async def test_hashear_sin_executor_ni_siquiera_respeta_un_timeout(recursos):
    # El timeout de asyncio es cooperativo: cancela la tarea en su próximo
    # `await`. Un cálculo que nunca cede el control no tiene próximo await,
    # así que termina "bien" aunque haya tardado mucho más que el límite.
    params = {"texto": "x", "iteraciones": 400_000, "ejecutor": "ninguno"}

    async with asyncio.timeout(0.01):
        resultado = await hashear(params, recursos, _sin_progreso)

    assert resultado["segundos"] > 0.01


@pytest.mark.parametrize("ejecutor", ["procesos", "hilos"])
async def test_hashear_se_cancela_entre_bloques(recursos, ejecutor):
    params = {"texto": "x", "iteraciones": 50_000_000, "ejecutor": ejecutor}  # ~25 s
    tarea = asyncio.create_task(hashear(params, recursos, _sin_progreso))
    await asyncio.sleep(0.3)
    inicio = time.perf_counter()
    tarea.cancel()

    with pytest.raises(asyncio.CancelledError):
        await tarea
    # La corrutina se entera al instante; el bloque en curso termina solo
    # en el hilo/proceso (a lo sumo ~50 ms más) y nadie lo vuelve a usar.
    assert time.perf_counter() - inicio < 0.1


# --- descargar ------------------------------------------------------------

def _handler_con_demora(demora_s, tamano=1000):
    async def handler(request):
        await asyncio.sleep(demora_s)
        return httpx.Response(200, content=b"x" * tamano)
    return handler


URLS = [f"http://prueba.local/{i}" for i in range(5)]


async def test_descargar_concurrente_tarda_lo_que_la_mas_lenta(recursos):
    _con_transporte(recursos, _handler_con_demora(0.2))

    inicio = time.perf_counter()
    resultado = await descargar({"urls": URLS, "concurrente": True}, recursos, _sin_progreso)

    assert time.perf_counter() - inicio < 0.4  # no 5 x 0.2 = 1 s
    assert resultado["concurrente"] is True
    assert [d["url"] for d in resultado["descargas"]] == URLS  # mismo orden
    for d in resultado["descargas"]:
        assert d["codigo"] == 200
        assert d["bytes"] == 1000
        assert d["segundos"] >= 0.18


async def test_descargar_secuencial_tarda_la_suma(recursos):
    _con_transporte(recursos, _handler_con_demora(0.1))

    inicio = time.perf_counter()
    resultado = await descargar({"urls": URLS, "concurrente": False}, recursos, _sin_progreso)

    assert time.perf_counter() - inicio >= 0.45
    assert resultado["segundos_total"] >= 0.45
    assert resultado["concurrente"] is False


async def test_descargar_reporta_errores_por_url_sin_fallar_toda_la_tarea(recursos):
    async def handler(request):
        if request.url.path == "/caida":
            raise httpx.ConnectError("no hay nadie escuchando", request=request)
        if request.url.path == "/no-existe":
            return httpx.Response(404, content=b"nope")
        return httpx.Response(200, content=b"ok")

    _con_transporte(recursos, handler)
    urls = ["http://prueba.local/ok", "http://prueba.local/caida", "http://prueba.local/no-existe"]

    resultado = await descargar({"urls": urls, "concurrente": True}, recursos, _sin_progreso)

    ok, caida, no_existe = resultado["descargas"]
    assert ok["codigo"] == 200 and ok["bytes"] == 2
    assert "ConnectError" in caida["error"] and "codigo" not in caida
    assert no_existe["codigo"] == 404  # un 404 es una respuesta, no un error de red
    assert resultado["ok"] == 2 and resultado["errores"] == 1


async def test_descargar_reporta_progreso(recursos):
    _con_transporte(recursos, _handler_con_demora(0))
    avances = []

    await descargar({"urls": URLS[:4], "concurrente": False}, recursos, avances.append)

    assert avances == [0.25, 0.5, 0.75, 1.0]


class StreamLento(httpx.AsyncByteStream):
    """Cuerpo de respuesta que gotea 1 KB cada 50 ms y registra si lo cierran."""

    def __init__(self, cerrados, url):
        self.cerrados = cerrados
        self.url = url

    async def __aiter__(self):
        for _ in range(200):
            await asyncio.sleep(0.05)
            yield b"x" * 1024

    async def aclose(self):
        self.cerrados.append(self.url)


async def test_cancelar_a_mitad_de_descarga_cierra_todo_y_no_deja_huerfanas(recursos):
    cerrados = []

    async def handler(request):
        return httpx.Response(200, stream=StreamLento(cerrados, str(request.url)))

    _con_transporte(recursos, handler)
    tarea = asyncio.create_task(descargar({"urls": URLS, "concurrente": True}, recursos, _sin_progreso))
    await asyncio.sleep(0.3)  # las 5 descargas están a mitad de camino

    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    assert sorted(cerrados) == sorted(URLS)  # las 5 respuestas se cerraron
    assert asyncio.all_tasks() == {asyncio.current_task()}  # ninguna corrutina huérfana


async def test_si_una_descarga_explota_las_demas_se_cancelan(recursos):
    # Un error que NO es de red (un bug) no se reporta por URL: tiene que
    # hacer fallar la tarea. Con TaskGroup, las hermanas se cancelan; con
    # gather() seguirían corriendo solas (README §3.3).
    cerrados = []

    async def handler(request):
        if request.url.path == "/bug":
            await asyncio.sleep(0.1)
            raise RuntimeError("bug inesperado")
        return httpx.Response(200, stream=StreamLento(cerrados, str(request.url)))

    _con_transporte(recursos, handler)
    urls = URLS[:3] + ["http://prueba.local/bug"]

    with pytest.raises(Exception) as info:
        await descargar({"urls": urls, "concurrente": True}, recursos, _sin_progreso)

    # Lo importante: cuando el error llega, ya no queda nada corriendo.
    assert sorted(cerrados) == sorted(URLS[:3])
    assert asyncio.all_tasks() == {asyncio.current_task()}
    # Y el error original llega envuelto en el ExceptionGroup del TaskGroup.
    assert info.group_contains(RuntimeError, match="bug inesperado")
