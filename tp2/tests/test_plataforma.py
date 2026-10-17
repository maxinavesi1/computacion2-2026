"""Tests de la Plataforma: cola, pool de workers, cancelación, timeouts y apagado.

Se usan tipos de tarea de mentira ("dormir", "explotar") para controlar
exactamente cuánto tarda o cómo falla cada tarea, sin red ni CPU.
"""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime

import pytest

from app.config import Config
from app.persistencia import volcar_estado
from app.plataforma import (ColaLlena, Plataforma, PlataformaCerrada,
                            TareaNoEncontrada, TareaTerminada)
from app.tarea import Estado, Tarea


async def _dormir(params, recursos, progreso):
    await asyncio.sleep(params["s"])
    progreso(0.5)
    return {"dormido": params["s"]}


async def _explotar(params, recursos, progreso):
    await asyncio.sleep(0)
    raise ValueError("kaboom")


async def _explotar_en_grupo(params, recursos, progreso):
    async with asyncio.TaskGroup() as grupo:
        grupo.create_task(_explotar(params, recursos, progreso))


TIPOS_PRUEBA = {"dormir": _dormir, "explotar": _explotar, "explotar_en_grupo": _explotar_en_grupo}


class RecursosFalsos:
    cerrados = False

    async def cerrar(self):
        self.cerrados = True


def _config(tmp_path, **cambios):
    valores = dict(workers=2, max_cola=5, timeout_defecto=5, timeout_apagado=0.3,
                   dir_datos=tmp_path, precalentar_procesos=False)
    valores.update(cambios)
    return Config(**valores)


@pytest.fixture
async def plataforma(tmp_path):
    p = Plataforma(_config(tmp_path), tipos=TIPOS_PRUEBA, recursos=RecursosFalsos())
    await p.iniciar()
    yield p
    if not p.apagada:
        await p.apagar()


async def _hasta_que_termine(tarea, limite=2.0):
    await asyncio.wait_for(tarea.fin.wait(), limite)


async def _hasta_que_arranque(tarea, limite=2.0):
    async with asyncio.timeout(limite):
        while tarea.estado is Estado.PENDIENTE:
            await asyncio.sleep(0.005)


# --- encolar y ejecutar -----------------------------------------------------

async def test_encolar_devuelve_enseguida_una_tarea_pendiente(plataforma):
    inicio = time.perf_counter()
    tarea = plataforma.encolar("dormir", {"s": 10})

    assert time.perf_counter() - inicio < 0.01
    assert tarea.estado is Estado.PENDIENTE
    assert plataforma.obtener(tarea.id) is tarea


async def test_un_worker_ejecuta_la_tarea_hasta_completarla(plataforma):
    tarea = plataforma.encolar("dormir", {"s": 0.05})
    await _hasta_que_termine(tarea)

    assert tarea.estado is Estado.COMPLETADA
    assert tarea.resultado == {"dormido": 0.05}
    assert tarea.progreso == 1.0
    assert tarea.error is None
    assert tarea.creada <= tarea.iniciada <= tarea.finalizada


async def test_el_progreso_reportado_se_ve_mientras_corre(plataforma):
    tarea = plataforma.encolar("dormir", {"s": 0.05})
    await _hasta_que_arranque(tarea)
    assert tarea.estado is Estado.EJECUTANDO

    await _hasta_que_termine(tarea)
    assert tarea.progreso == 1.0


async def test_se_usa_el_timeout_por_defecto_si_no_se_indica(plataforma):
    assert plataforma.encolar("dormir", {"s": 0}).timeout == 5
    assert plataforma.encolar("dormir", {"s": 0}, timeout=1.5).timeout == 1.5


async def test_tipo_desconocido_se_rechaza(plataforma):
    with pytest.raises(ValueError, match="minar-bitcoins"):
        plataforma.encolar("minar-bitcoins", {})


async def test_los_workers_corren_en_paralelo_hasta_su_cantidad(plataforma):
    # 2 workers: dos tareas de 0.2 s tardan ~0.2 s en total, no 0.4 s...
    inicio = time.perf_counter()
    dos = [plataforma.encolar("dormir", {"s": 0.2}) for _ in range(2)]
    for t in dos:
        await _hasta_que_termine(t)
    assert time.perf_counter() - inicio < 0.35

    # ...pero una tercera tiene que esperar a que se libere un worker.
    inicio = time.perf_counter()
    tres = [plataforma.encolar("dormir", {"s": 0.2}) for _ in range(3)]
    for t in tres:
        await _hasta_que_termine(t)
    assert time.perf_counter() - inicio >= 0.36
    assert tres[2].a_dict()["espera_s"] >= 0.17


async def test_listar_filtra_por_estado(plataforma):
    rapida = plataforma.encolar("dormir", {"s": 0})
    lentas = [plataforma.encolar("dormir", {"s": 10}) for _ in range(2)]
    en_cola = plataforma.encolar("dormir", {"s": 10})
    await _hasta_que_termine(rapida)
    for t in lentas:
        await _hasta_que_arranque(t)

    assert plataforma.listar(Estado.COMPLETADA) == [rapida]
    assert set(plataforma.listar(Estado.EJECUTANDO)) == set(lentas)
    assert plataforma.listar(Estado.PENDIENTE) == [en_cola]
    assert len(plataforma.listar()) == 4


async def test_obtener_una_tarea_inexistente_da_none(plataforma):
    assert plataforma.obtener("no-existe") is None


# --- fallas -----------------------------------------------------------------

async def test_una_excepcion_deja_la_tarea_fallida_y_el_worker_sigue(plataforma):
    rota = plataforma.encolar("explotar", {})
    await _hasta_que_termine(rota)

    assert rota.estado is Estado.FALLIDA
    assert rota.error == "ValueError: kaboom"

    # Los mismos 2 workers siguen atendiendo.
    sanas = [plataforma.encolar("dormir", {"s": 0}) for _ in range(2)]
    for t in sanas:
        await _hasta_que_termine(t)
    assert all(t.estado is Estado.COMPLETADA for t in sanas)


async def test_el_error_de_un_grupo_muestra_la_causa_real(plataforma):
    tarea = plataforma.encolar("explotar_en_grupo", {})
    await _hasta_que_termine(tarea)

    assert tarea.estado is Estado.FALLIDA
    assert "ValueError: kaboom" in tarea.error


async def test_al_vencer_el_timeout_queda_fallida_con_el_motivo(plataforma):
    tarea = plataforma.encolar("dormir", {"s": 10}, timeout=0.1)
    await _hasta_que_termine(tarea)

    assert tarea.estado is Estado.FALLIDA
    assert tarea.error == "timeout: superó los 0.1 s"
    assert tarea.a_dict()["duracion_s"] < 0.3


async def test_un_worker_que_muere_es_reemplazado(plataforma, monkeypatch, caplog):
    correr_original = plataforma._correr
    llamadas = []

    async def correr_con_bug(tarea):
        llamadas.append(tarea)
        if len(llamadas) == 1:
            raise RuntimeError("bug en el worker")
        await correr_original(tarea)

    monkeypatch.setattr(plataforma, "_correr", correr_con_bug)
    with caplog.at_level(logging.ERROR):
        plataforma.encolar("dormir", {"s": 0})  # mata a un worker
        await asyncio.sleep(0.05)

    assert "bug en el worker" in caplog.text
    # Siguen habiendo 2 workers: dos tareas largas corren a la vez.
    largas = [plataforma.encolar("dormir", {"s": 0.2}) for _ in range(2)]
    for t in largas:
        await _hasta_que_arranque(t, limite=0.1)
    assert plataforma.estadisticas()["workers"] == {"total": 2, "ocupados": 2}


# --- cancelación ------------------------------------------------------------

async def test_cancelar_una_tarea_en_cola(plataforma):
    ocupando = [plataforma.encolar("dormir", {"s": 0.2}) for _ in range(2)]
    en_cola = plataforma.encolar("dormir", {"s": 0})

    cancelada = await plataforma.cancelar(en_cola.id)

    assert cancelada is en_cola
    assert en_cola.estado is Estado.CANCELADA
    assert "en cola" in en_cola.error
    # Cuando un worker la saca de la cola, la saltea: nunca se ejecuta.
    for t in ocupando:
        await _hasta_que_termine(t)
    await asyncio.sleep(0.05)
    assert en_cola.iniciada is None and en_cola.resultado is None


async def test_cancelar_una_tarea_en_ejecucion(plataforma):
    tarea = plataforma.encolar("dormir", {"s": 30})
    await _hasta_que_arranque(tarea)

    inicio = time.perf_counter()
    await plataforma.cancelar(tarea.id)

    assert time.perf_counter() - inicio < 0.1
    assert tarea.estado is Estado.CANCELADA
    assert tarea.error == "cancelada por el usuario"
    assert tarea.ejecucion is None
    # El worker quedó libre: las siguientes 2 tareas corren a la vez.
    siguientes = [plataforma.encolar("dormir", {"s": 0}) for _ in range(2)]
    for t in siguientes:
        await _hasta_que_termine(t)
    assert plataforma.estadisticas()["workers"]["ocupados"] == 0


async def test_cancelar_una_tarea_terminada_es_un_error(plataforma):
    tarea = plataforma.encolar("dormir", {"s": 0})
    await _hasta_que_termine(tarea)

    with pytest.raises(TareaTerminada):
        await plataforma.cancelar(tarea.id)
    assert tarea.estado is Estado.COMPLETADA


async def test_cancelar_una_tarea_inexistente_es_un_error(plataforma):
    with pytest.raises(TareaNoEncontrada):
        await plataforma.cancelar("no-existe")


# --- límites ----------------------------------------------------------------

async def test_con_la_cola_llena_se_rechazan_tareas(plataforma):
    for _ in range(2):  # ocupan a los 2 workers
        await _hasta_que_arranque(plataforma.encolar("dormir", {"s": 10}))
    for _ in range(5):  # llenan la cola (max_cola=5)
        plataforma.encolar("dormir", {"s": 10})

    with pytest.raises(ColaLlena):
        plataforma.encolar("dormir", {"s": 10})
    assert len(plataforma.listar()) == 7  # la rechazada no quedó registrada


async def test_despues_de_dejar_de_aceptar_se_rechazan_tareas(plataforma):
    plataforma.dejar_de_aceptar()

    with pytest.raises(PlataformaCerrada):
        plataforma.encolar("dormir", {"s": 0})


# --- estadísticas -----------------------------------------------------------

async def test_estadisticas(plataforma):
    hecha = plataforma.encolar("dormir", {"s": 0.1})
    rota = plataforma.encolar("explotar", {})
    await _hasta_que_termine(hecha)
    await _hasta_que_termine(rota)
    larga = plataforma.encolar("dormir", {"s": 10})
    await _hasta_que_arranque(larga)
    plataforma.encolar("dormir", {"s": 10})

    stats = plataforma.estadisticas()

    assert stats["aceptando"] is True
    assert stats["tareas"] == {"total": 4, "pendiente": 1, "ejecutando": 1,
                               "completada": 1, "fallida": 1, "cancelada": 0}
    assert stats["workers"] == {"total": 2, "ocupados": 1}
    assert stats["cola"] == {"en_cola": 1, "capacidad": 5}
    assert 0.08 <= stats["tiempos_promedio_s"]["ejecucion"] < 0.2
    assert stats["tiempos_promedio_s"]["espera"] >= 0
    assert stats["por_tipo"]["dormir"]["completadas"] == 1
    json.dumps(stats)


# --- arranque y apagado -----------------------------------------------------

async def test_al_arrancar_reporta_volcados_previos(tmp_path, caplog):
    vieja = Tarea(tipo="dormir", params={"s": 1}, timeout=5)
    volcar_estado([vieja], tmp_path)
    p = Plataforma(_config(tmp_path), tipos=TIPOS_PRUEBA, recursos=RecursosFalsos())

    with caplog.at_level(logging.INFO):
        await p.iniciar()
    await p.apagar()

    assert "estado_" in caplog.text
    assert "1 tareas" in caplog.text and "pendiente: 1" in caplog.text


async def test_con_varios_volcados_detalla_el_ultimo_y_cuenta_los_demas(tmp_path, caplog):
    for hora in (10, 11):
        volcar_estado([], tmp_path, ahora=datetime(2026, 10, 17, hora, tzinfo=UTC))
    vieja = Tarea(tipo="dormir", params={"s": 1}, timeout=5)
    volcar_estado([vieja], tmp_path, ahora=datetime(2026, 10, 17, 12, tzinfo=UTC))
    p = Plataforma(_config(tmp_path), tipos=TIPOS_PRUEBA, recursos=RecursosFalsos())

    with caplog.at_level(logging.INFO):
        await p.iniciar()
    await p.apagar()

    assert "estado_20261017-120000.json — 1 tareas (pendiente: 1)" in caplog.text
    assert "2 volcados anteriores" in caplog.text
    assert "estado_20261017-100000.json" not in caplog.text


async def test_un_volcado_vacio_se_reporta_sin_detalle(tmp_path, caplog):
    volcar_estado([], tmp_path, ahora=datetime(2026, 10, 17, 10, tzinfo=UTC))
    p = Plataforma(_config(tmp_path), tipos=TIPOS_PRUEBA, recursos=RecursosFalsos())

    with caplog.at_level(logging.INFO):
        await p.iniciar()
    await p.apagar()

    assert "estado_20261017-100000.json — 0 tareas" in caplog.text
    assert "()" not in caplog.text


async def test_apagado_ordenado(tmp_path):
    recursos = RecursosFalsos()
    p = Plataforma(_config(tmp_path, timeout_apagado=0.3), tipos=TIPOS_PRUEBA, recursos=recursos)
    await p.iniciar()
    corta = p.encolar("dormir", {"s": 0.1})     # termina dentro del límite
    larga = p.encolar("dormir", {"s": 30})      # no llega: se cancela
    en_cola = p.encolar("dormir", {"s": 0})     # nunca arranca
    await _hasta_que_arranque(corta)
    await _hasta_que_arranque(larga)

    inicio = time.perf_counter()
    ruta = await p.apagar()

    assert time.perf_counter() - inicio < 0.6     # esperó con límite
    assert corta.estado is Estado.COMPLETADA
    assert larga.estado is Estado.CANCELADA
    assert "apagado" in larga.error
    assert en_cola.estado is Estado.PENDIENTE
    assert recursos.cerrados
    assert p.apagada
    # Nada quedó corriendo: ni workers ni tareas.
    assert asyncio.all_tasks() == {asyncio.current_task()}
    # El volcado tiene las tres, con su estado final.
    volcado = json.loads(ruta.read_text(encoding="utf-8"))
    assert ruta.parent == tmp_path and ruta.name.startswith("estado_")
    assert {t["id"]: t["estado"] for t in volcado["tareas"]} == {
        corta.id: "completada", larga.id: "cancelada", en_cola.id: "pendiente"}
    with pytest.raises(PlataformaCerrada):
        p.encolar("dormir", {"s": 0})
