"""Tests del modelo interno Tarea y de los estados."""

import json

from app.tarea import Estado, Tarea


def _tarea(**kwargs):
    return Tarea(tipo="esperar", params={"segundos": 1}, timeout=10, **kwargs)


def test_una_tarea_nueva_arranca_pendiente_y_con_id():
    tarea = _tarea()

    assert tarea.estado is Estado.PENDIENTE
    assert tarea.id
    assert tarea.creada > 0
    assert tarea.iniciada is None and tarea.finalizada is None


def test_los_ids_no_se_repiten():
    ids = {_tarea().id for _ in range(1000)}
    assert len(ids) == 1000


def test_solo_completada_fallida_y_cancelada_son_terminales():
    terminales = {e for e in Estado if e.terminal}
    assert terminales == {Estado.COMPLETADA, Estado.FALLIDA, Estado.CANCELADA}


def test_los_estados_se_serializan_como_texto():
    assert json.dumps(Estado.EJECUTANDO) == '"ejecutando"'


def test_a_dict_es_serializable_y_no_expone_lo_interno():
    tarea = _tarea()
    datos = tarea.a_dict()

    json.dumps(datos)  # no tiene que explotar
    assert datos["id"] == tarea.id
    assert datos["estado"] == "pendiente"
    assert datos["params"] == {"segundos": 1}
    for interno in ("ejecucion", "fin", "motivo_cancelacion"):
        assert interno not in datos


def test_tiempos_de_espera_y_duracion():
    tarea = _tarea(creada=1_800_000_000.0)
    assert tarea.a_dict()["espera_s"] is None
    assert tarea.a_dict()["duracion_s"] is None

    tarea.iniciada = 1_800_000_002.5
    tarea.finalizada = 1_800_000_006.0
    datos = tarea.a_dict()

    assert datos["espera_s"] == 2.5
    assert datos["duracion_s"] == 3.5
    # Timestamps en ISO 8601 y en UTC, para que no dependan del huso del host.
    assert datos["creada"] == "2027-01-15T08:00:00.000+00:00"


def test_a_dict_puede_omitir_el_resultado():
    tarea = _tarea()
    tarea.resultado = {"mucho": "texto"}

    assert tarea.a_dict()["resultado"] == {"mucho": "texto"}
    assert "resultado" not in tarea.a_dict(incluir_resultado=False)
