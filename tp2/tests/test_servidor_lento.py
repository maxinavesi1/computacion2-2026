"""Tests del servidor auxiliar que simula sitios lentos para las mediciones."""

import time

import httpx
import pytest

from app.servidor_lento import app


@pytest.fixture
async def cliente():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lento") as c:
        yield c


async def test_lento_demora_y_devuelve_el_tamano_pedido(cliente):
    inicio = time.perf_counter()
    r = await cliente.get("/lento", params={"segundos": 0.2, "kb": 3})

    assert time.perf_counter() - inicio >= 0.18
    assert r.status_code == 200
    assert len(r.content) == 3 * 1024


async def test_goteo_manda_el_cuerpo_de_a_pedazos_durante_el_tiempo_pedido(cliente):
    llegadas = []
    inicio = time.perf_counter()
    async with cliente.stream("GET", "/goteo", params={"segundos": 0.3, "kb": 4}) as r:
        async for pedazo in r.aiter_bytes():
            llegadas.append((time.perf_counter() - inicio, len(pedazo)))

    assert sum(n for _, n in llegadas) == 4 * 1024
    assert llegadas[-1][0] >= 0.2   # el último pedazo llegó al final, no todo junto


async def test_parametros_fuera_de_rango_dan_422(cliente):
    assert (await cliente.get("/lento", params={"segundos": -1})).status_code == 422
    assert (await cliente.get("/goteo", params={"kb": 0})).status_code == 422
