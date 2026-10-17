"""Tests del punto de entrada: qué pasa cuando llega SIGTERM/SIGINT."""

import signal

from app.config import Config
from app.main import crear_servidor


class PlataformaFalsa:
    aceptando = True

    def dejar_de_aceptar(self):
        self.aceptando = False


def test_una_senal_corta_el_ingreso_de_tareas_y_pide_el_apagado(tmp_path):
    servidor = crear_servidor(Config(dir_datos=tmp_path))
    plataforma = PlataformaFalsa()
    servidor.config.app.state.plataforma = plataforma

    # Es lo que hace el intérprete cuando llega la señal: llamar al handler
    # que uvicorn registró con signal.signal().
    servidor.handle_exit(signal.SIGTERM, None)

    assert plataforma.aceptando is False   # POST /tareas ya responde 503
    assert servidor.should_exit            # y uvicorn arranca el apagado


def test_una_senal_antes_de_terminar_de_arrancar_no_rompe_nada(tmp_path):
    servidor = crear_servidor(Config(dir_datos=tmp_path))  # sin plataforma todavía

    servidor.handle_exit(signal.SIGINT, None)

    assert servidor.should_exit


def test_el_servidor_usa_host_y_puerto_de_la_config(tmp_path):
    servidor = crear_servidor(Config(dir_datos=tmp_path, host="127.0.0.1", puerto=9999))

    assert (servidor.config.host, servidor.config.port) == ("127.0.0.1", 9999)
