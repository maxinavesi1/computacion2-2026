"""Tests de la API HTTP de punta a punta (sin red y sin levantar uvicorn).

El cliente httpx habla directo con la app ASGI en memoria, y el lifespan de
la app (que arranca y apaga la Plataforma) corre igual que en producción.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import httpx
import pytest

from app.api import crear_app
from app.config import Config
from app.hashing import hash_iterado
from app.tipos import Recursos


async def _web_falsa(request):
    await asyncio.sleep(0.05)
    return httpx.Response(200, content=b"hola mundo")


@asynccontextmanager
async def _app_con(tmp_path, **cambios):
    valores = dict(workers=2, max_cola=3, timeout_defecto=5, timeout_apagado=0.3,
                   dir_datos=tmp_path, precalentar_procesos=False)
    valores.update(cambios)
    # Hilos en lugar de procesos para que los tests no paguen el spawn.
    pool = ThreadPoolExecutor(max_workers=2)
    recursos = Recursos(cliente_http=httpx.AsyncClient(transport=httpx.MockTransport(_web_falsa)),
                        pool_procesos=pool, pool_hilos=pool)
    aplicacion = crear_app(Config(**valores), recursos=recursos)
    async with aplicacion.router.lifespan_context(aplicacion):
        yield aplicacion


@asynccontextmanager
async def _cliente_de(aplicacion):
    transporte = httpx.ASGITransport(app=aplicacion)
    async with httpx.AsyncClient(transport=transporte, base_url="http://tp2") as c:
        yield c


@pytest.fixture
async def app(tmp_path):
    async with _app_con(tmp_path) as aplicacion:
        yield aplicacion


@pytest.fixture
async def cliente(app):
    async with _cliente_de(app) as c:
        yield c


async def _crear(cliente, tipo, params, **extra):
    r = await cliente.post("/tareas", json={"tipo": tipo, "params": params, **extra})
    assert r.status_code == 202, r.text
    return r.json()


async def _esperar_estado(cliente, tarea_id, estados, limite=3.0):
    async with asyncio.timeout(limite):
        while True:
            tarea = (await cliente.get(f"/tareas/{tarea_id}")).json()
            if tarea["estado"] in estados:
                return tarea
            await asyncio.sleep(0.01)


async def _esperar_fin(cliente, tarea_id):
    return await _esperar_estado(cliente, tarea_id, {"completada", "fallida", "cancelada"})


# --- POST /tareas -----------------------------------------------------------

async def test_post_responde_al_instante_sin_esperar_a_la_tarea(cliente):
    inicio = time.perf_counter()
    r = await cliente.post("/tareas", json={"tipo": "esperar", "params": {"segundos": 30}})

    assert time.perf_counter() - inicio < 0.2
    assert r.status_code == 202
    cuerpo = r.json()
    assert cuerpo["estado"] == "pendiente"
    assert cuerpo["tipo"] == "esperar"
    assert r.headers["location"] == f"/tareas/{cuerpo['id']}"


async def test_tarea_esperar_completa(cliente):
    tarea = await _crear(cliente, "esperar", {"segundos": 0.05})

    final = await _esperar_fin(cliente, tarea["id"])

    assert final["estado"] == "completada"
    assert final["resultado"] == {"dormido_s": 0.05}
    # Margen de 20 ms: el loop puede despertar un timer hasta una resolución
    # de reloj antes (~15.6 ms en Windows).
    assert final["duracion_s"] >= 0.03


async def test_tarea_hashear_completa(cliente):
    tarea = await _crear(cliente, "hashear", {"texto": "abc", "iteraciones": 1000, "ejecutor": "hilos"})

    final = await _esperar_fin(cliente, tarea["id"])

    assert final["estado"] == "completada"
    assert final["resultado"]["hash"] == hash_iterado(b"abc", 1000).hex()


async def test_hashear_usa_procesos_por_defecto(cliente):
    tarea = await _crear(cliente, "hashear", {"texto": "abc", "iteraciones": 10})
    assert tarea["params"]["ejecutor"] == "procesos"


async def test_hashear_sin_executor_esta_prohibido_por_defecto(cliente):
    # Congelaría el event loop: un cliente cualquiera no puede pedirlo.
    pedido = {"tipo": "hashear", "params": {"texto": "x", "iteraciones": 10, "ejecutor": "ninguno"}}

    r = await cliente.post("/tareas", json=pedido)

    assert r.status_code == 403
    assert "PERMITIR_BLOQUEANTE" in r.json()["detail"]
    assert (await cliente.get("/tareas")).json() == []  # no se encoló nada


async def test_hashear_sin_executor_se_puede_habilitar_para_medir(tmp_path):
    pedido = {"tipo": "hashear", "params": {"texto": "x", "iteraciones": 10, "ejecutor": "ninguno"}}

    async with (_app_con(tmp_path, permitir_bloqueante=True) as aplicacion,
                _cliente_de(aplicacion) as cliente):
        r = await cliente.post("/tareas", json=pedido)
        final = await _esperar_fin(cliente, r.json()["id"])

    assert r.status_code == 202
    assert final["resultado"]["hash"] == hash_iterado(b"x", 10).hex()


async def test_tarea_descargar_completa(cliente):
    urls = ["http://a.local/1", "http://b.local/2"]
    tarea = await _crear(cliente, "descargar", {"urls": urls})

    final = await _esperar_fin(cliente, tarea["id"])

    assert final["estado"] == "completada"
    descargas = final["resultado"]["descargas"]
    assert [d["codigo"] for d in descargas] == [200, 200]
    assert [d["bytes"] for d in descargas] == [10, 10]


async def test_el_timeout_del_pedido_se_respeta(cliente):
    tarea = await _crear(cliente, "esperar", {"segundos": 10}, timeout=0.1)

    final = await _esperar_fin(cliente, tarea["id"])

    assert final["estado"] == "fallida"
    assert "timeout" in final["error"]


@pytest.mark.parametrize("pedido", [
    {"tipo": "minar", "params": {}},                                        # tipo inexistente
    {"tipo": "esperar", "params": {"segundos": -1}},                        # fuera de rango
    {"tipo": "esperar", "params": {}},                                      # falta un campo
    {"tipo": "esperar", "params": {"segundos": 1, "extra": 2}},             # campo de más
    {"tipo": "esperar", "params": {"segundos": 1}, "timeout": 0},           # timeout inválido
    {"tipo": "hashear", "params": {"texto": "x", "iteraciones": 0}},
    {"tipo": "hashear", "params": {"texto": "x", "iteraciones": 5, "ejecutor": "gpu"}},
    {"tipo": "descargar", "params": {"urls": []}},
    {"tipo": "descargar", "params": {"urls": ["ftp://no.es/http"]}},
])
async def test_pedidos_invalidos_dan_422(cliente, pedido):
    r = await cliente.post("/tareas", json=pedido)
    assert r.status_code == 422


async def test_con_la_cola_llena_da_503(cliente):
    # Primero se ocupan los 2 workers. Hay que esperarlos: el transporte en
    # memoria no hace I/O real, así que este test no le cede el loop a los
    # workers entre un POST y otro (multitarea cooperativa: sin un await que
    # suspenda, nadie más corre).
    for _ in range(2):
        tarea = await _crear(cliente, "esperar", {"segundos": 30})
        await _esperar_estado(cliente, tarea["id"], {"ejecutando"})
    for _ in range(3):  # y después se llena la cola de 3
        await _crear(cliente, "esperar", {"segundos": 30})

    r = await cliente.post("/tareas", json={"tipo": "esperar", "params": {"segundos": 1}})

    assert r.status_code == 503
    assert "retry-after" in r.headers


async def test_apagandose_da_503(app, cliente):
    app.state.plataforma.dejar_de_aceptar()

    r = await cliente.post("/tareas", json={"tipo": "esperar", "params": {"segundos": 1}})

    assert r.status_code == 503


# --- GET /tareas ------------------------------------------------------------

async def test_listar_filtra_por_estado_y_no_trae_resultados(cliente):
    hecha = await _crear(cliente, "esperar", {"segundos": 0})
    await _esperar_fin(cliente, hecha["id"])
    larga = await _crear(cliente, "esperar", {"segundos": 30})
    await _esperar_estado(cliente, larga["id"], {"ejecutando"})

    completadas = (await cliente.get("/tareas", params={"estado": "completada"})).json()
    ejecutando = (await cliente.get("/tareas", params={"estado": "ejecutando"})).json()
    todas = (await cliente.get("/tareas")).json()

    assert [t["id"] for t in completadas] == [hecha["id"]]
    assert [t["id"] for t in ejecutando] == [larga["id"]]
    assert [t["id"] for t in todas] == [larga["id"], hecha["id"]]  # la más nueva primero
    assert "resultado" not in todas[0]


async def test_listar_respeta_el_limite(cliente):
    for _ in range(3):
        await _crear(cliente, "esperar", {"segundos": 0})

    assert len((await cliente.get("/tareas", params={"limite": 2})).json()) == 2


async def test_listar_con_estado_invalido_da_422(cliente):
    assert (await cliente.get("/tareas", params={"estado": "dormida"})).status_code == 422


async def test_tarea_inexistente_da_404(cliente):
    assert (await cliente.get("/tareas/no-existe")).status_code == 404


# --- DELETE /tareas/{id} ----------------------------------------------------

async def test_delete_cancela_una_tarea_en_ejecucion(cliente):
    tarea = await _crear(cliente, "esperar", {"segundos": 30})
    await _esperar_estado(cliente, tarea["id"], {"ejecutando"})

    r = await cliente.delete(f"/tareas/{tarea['id']}")

    assert r.status_code == 200
    assert r.json()["estado"] == "cancelada"
    assert (await cliente.get(f"/tareas/{tarea['id']}")).json()["estado"] == "cancelada"


async def test_delete_cancela_una_tarea_en_cola(cliente):
    for _ in range(2):
        await _crear(cliente, "esperar", {"segundos": 30})
    en_cola = await _crear(cliente, "esperar", {"segundos": 30})

    r = await cliente.delete(f"/tareas/{en_cola['id']}")

    assert r.status_code == 200
    assert r.json()["estado"] == "cancelada"


async def test_delete_de_una_tarea_terminada_da_409(cliente):
    tarea = await _crear(cliente, "esperar", {"segundos": 0})
    await _esperar_fin(cliente, tarea["id"])

    assert (await cliente.delete(f"/tareas/{tarea['id']}")).status_code == 409


async def test_delete_de_una_tarea_inexistente_da_404(cliente):
    assert (await cliente.delete("/tareas/no-existe")).status_code == 404


# --- GET /estadisticas y /salud --------------------------------------------

async def test_estadisticas(cliente):
    hecha = await _crear(cliente, "esperar", {"segundos": 0})
    await _esperar_fin(cliente, hecha["id"])

    stats = (await cliente.get("/estadisticas")).json()

    assert stats["tareas"]["completada"] == 1
    assert stats["workers"] == {"total": 2, "ocupados": 0}
    assert stats["cola"]["capacidad"] == 3
    assert stats["por_tipo"]["esperar"]["completadas"] == 1


async def test_salud(cliente):
    r = await cliente.get("/salud")
    assert r.status_code == 200
    assert r.json() == {"estado": "ok", "aceptando": True}
