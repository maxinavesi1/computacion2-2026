"""Tests del modelo interno Tarea y de los estados."""

import json

import pytest

from app import tarea as tarea_modulo
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


class RelojFalso:
    """Imita al módulo time: un reloj de pared que puede saltar y uno monotónico que no."""

    def __init__(self):
        self.pared = 1_800_000_000.0
        self.mono = 500.0

    def time(self):
        return self.pared

    def monotonic(self):
        return self.mono

    def pasan(self, segundos):
        self.pared += segundos
        self.mono += segundos


@pytest.fixture
def reloj(monkeypatch):
    reloj = RelojFalso()
    monkeypatch.setattr(tarea_modulo, "time", reloj)
    return reloj


def test_tiempos_de_espera_y_duracion(reloj):
    tarea = _tarea()
    assert tarea.a_dict()["espera_s"] is None
    assert tarea.a_dict()["duracion_s"] is None

    reloj.pasan(2.5)
    tarea.marcar_inicio()
    reloj.pasan(3.5)
    tarea.marcar_fin()
    datos = tarea.a_dict()

    assert datos["espera_s"] == 2.5 and tarea.espera == 2.5
    assert datos["duracion_s"] == 3.5 and tarea.duracion == 3.5
    # Timestamps en ISO 8601 y en UTC, para que no dependan del huso del host.
    assert datos["creada"] == "2027-01-15T08:00:00.000+00:00"
    assert datos["finalizada"] == "2027-01-15T08:00:06.000+00:00"


def test_las_duraciones_no_cambian_si_salta_el_reloj_de_pared(reloj):
    # Pasó de verdad midiendo en Docker Desktop: al resincronizar la hora de
    # la VM con la de Windows, time.time() salta ~1,8 s para atrás.
    tarea = _tarea()
    tarea.marcar_inicio()
    reloj.pasan(3.0)
    reloj.pared -= 1.8  # el salto
    tarea.marcar_fin()

    assert tarea.duracion == 3.0  # con time.time() daría 1.2
    assert tarea.a_dict()["duracion_s"] == 3.0


def test_a_dict_puede_omitir_el_resultado():
    tarea = _tarea()
    tarea.resultado = {"mucho": "texto"}

    assert tarea.a_dict()["resultado"] == {"mucho": "texto"}
    assert "resultado" not in tarea.a_dict(incluir_resultado=False)
