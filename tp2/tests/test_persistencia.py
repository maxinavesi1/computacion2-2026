"""Tests del volcado de estado a JSON y de la detección de volcados previos."""

import json
from datetime import UTC, datetime

import pytest

from app.persistencia import buscar_volcados, resumir_volcado, volcar_estado
from app.tarea import Estado, Tarea

AHORA = datetime(2026, 10, 17, 15, 4, 5, tzinfo=UTC)


def _tareas():
    hecha = Tarea(tipo="esperar", params={"segundos": 1}, timeout=5)
    hecha.estado = Estado.COMPLETADA
    hecha.resultado = {"dormido_s": 1}
    pendiente = Tarea(tipo="hashear", params={"texto": "x", "iteraciones": 10}, timeout=5)
    return [hecha, pendiente]


def test_vuelca_a_un_archivo_con_timestamp_en_el_nombre(tmp_path):
    ruta = volcar_estado(_tareas(), tmp_path / "datos", ahora=AHORA)

    assert ruta == tmp_path / "datos" / "estado_20261017-150405.json"
    contenido = json.loads(ruta.read_text(encoding="utf-8"))
    assert contenido["cantidad"] == 2
    assert [t["estado"] for t in contenido["tareas"]] == ["completada", "pendiente"]
    assert contenido["tareas"][0]["resultado"] == {"dormido_s": 1}


def test_no_deja_archivos_temporales(tmp_path):
    volcar_estado(_tareas(), tmp_path, ahora=AHORA)

    assert [p.name for p in tmp_path.iterdir()] == ["estado_20261017-150405.json"]


def test_sin_directorio_no_hay_volcados(tmp_path):
    assert buscar_volcados(tmp_path / "no-existe") == []


def test_busca_solo_volcados_y_del_mas_viejo_al_mas_nuevo(tmp_path):
    (tmp_path / "otra_cosa.json").write_text("{}")
    (tmp_path / "estado_20261017-120000.json").write_text("{}")
    (tmp_path / "estado_20261016-090000.json").write_text("{}")

    nombres = [p.name for p in buscar_volcados(tmp_path)]

    assert nombres == ["estado_20261016-090000.json", "estado_20261017-120000.json"]


def test_resume_un_volcado_por_estado(tmp_path):
    ruta = volcar_estado(_tareas(), tmp_path, ahora=AHORA)

    assert resumir_volcado(ruta) == {"total": 2, "completada": 1, "pendiente": 1}


def test_un_volcado_corrupto_se_reporta_como_valueerror(tmp_path):
    ruta = tmp_path / "estado_20261017-120000.json"
    ruta.write_text("{esto no es json")

    with pytest.raises(ValueError, match="estado_20261017-120000.json"):
        resumir_volcado(ruta)
