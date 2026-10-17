"""Tests de la configuración por variables de entorno."""

from pathlib import Path

import pytest

from app.config import Config


def test_valores_por_defecto_razonables():
    config = Config()

    assert config.workers >= 1
    assert config.procesos_cpu >= 1
    assert config.max_cola >= 1
    assert config.timeout_defecto > 0
    assert config.timeout_apagado > 0


def test_lee_y_convierte_variables_de_entorno():
    config = Config.desde_entorno({
        "WORKERS": "3",
        "PROCESOS_CPU": "2",
        "MAX_COLA": "50",
        "TIMEOUT_DEFECTO": "2.5",
        "TIMEOUT_APAGADO": "4",
        "DIR_DATOS": "/tmp/volcados",
        "PRECALENTAR_PROCESOS": "no",
        "PUERTO": "9000",
    })

    assert config.workers == 3
    assert config.procesos_cpu == 2
    assert config.max_cola == 50
    assert config.timeout_defecto == 2.5
    assert config.timeout_apagado == 4.0
    assert config.dir_datos == Path("/tmp/volcados")
    assert config.precalentar_procesos is False
    assert config.puerto == 9000


def test_variables_ausentes_toman_el_default():
    assert Config.desde_entorno({}) == Config()


@pytest.mark.parametrize("variable", ["WORKERS", "MAX_COLA", "PROCESOS_CPU"])
def test_rechaza_cantidades_no_positivas(variable):
    with pytest.raises(ValueError, match=variable.lower()):
        Config.desde_entorno({variable: "0"})


def test_rechaza_timeouts_no_positivos():
    with pytest.raises(ValueError, match="timeout_defecto"):
        Config(timeout_defecto=0)
