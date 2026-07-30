"""
tests/test_ui.py — tests del estado de la TUI y de la tabla de vistas.

Durante el desarrollo probé la TUI a mano, apretando las teclas, pero eso no es
repetible ni sirve como regresión. Estos tests atacan el problema por el otro
lado, aprovechando que el manejo de teclado de display.py
está separado del dibujado, así que se puede inyectar un keycode directamente
en `_manejar_tecla` y verificar cómo queda `EstadoUI`, sin abrir curses ni
necesitar una terminal. Es determinista y corre en cualquier lado.

Lo que NO cubren: el dibujado en sí (`_dibujar*`), que sigue siendo verificación
visual. Esa parte está aislada a propósito detrás de `_addstr_seguro`, que
recorta con `addnstr` en vez de crashear si la terminal es más chica que el
contenido.
"""

import contextlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import display  # noqa: E402
import vistas  # noqa: E402
from analizadores import fds as an_fds  # noqa: E402


class FakeValue:
    """Reemplazo de multiprocessing.Value: mismo .value y mismo get_lock()."""

    def __init__(self, valor):
        self.value = valor

    def get_lock(self):
        return contextlib.nullcontext()


def _intervalos():
    return {nombre: FakeValue(vistas.DEFAULTS[nombre]) for nombre in vistas.NOMBRES}


def _apretar(estado, tecla, interval_values=None):
    """Inyecta una tecla como si viniera de stdscr.getch(). Devuelve True si pidió salir."""
    return display._manejar_tecla(
        ord(tecla) if isinstance(tecla, str) else tecla,
        estado,
        interval_values if interval_values is not None else _intervalos(),
        stop_event=None, snapshot={}, dump_dir=".",
    )


class TestTablaDeVistas(unittest.TestCase):
    """La tabla de vistas.py tiene que coincidir con la de la consigna."""

    def test_intervalos_de_la_consigna(self):
        esperado = {
            "resumen": (2.0, 0.5), "memoria": (3.0, 1.0), "fds": (5.0, 2.0),
            "threads": (2.0, 0.5), "senales": (10.0, 5.0),
            "scheduling": (10.0, 5.0), "sistema": (2.0, 1.0),
        }
        for nombre, (default, minimo) in esperado.items():
            with self.subTest(vista=nombre):
                self.assertEqual(vistas.DEFAULTS[nombre], default)
                self.assertEqual(vistas.MINIMOS[nombre], minimo)

    def test_las_14_teclas_de_vista_estan_mapeadas(self):
        # 7 numéricas (1..7) + 7 alfabéticas (r m f t s p g)
        self.assertEqual(len(vistas.TECLA_A_VISTA), 14)
        for tecla in "1234567rmftspg":
            with self.subTest(tecla=tecla):
                self.assertIn(tecla, vistas.TECLA_A_VISTA)

    def test_clamp_respeta_el_minimo_de_cada_vista(self):
        self.assertEqual(vistas.clamp_intervalo("senales", 0.1), 5.0)
        self.assertEqual(vistas.clamp_intervalo("resumen", 0.1), 0.5)
        self.assertEqual(vistas.clamp_intervalo("resumen", 4.0), 4.0)


class TestCambioDeVista(unittest.TestCase):
    def test_teclas_numericas_y_alfabeticas_llevan_a_la_misma_vista(self):
        for numerica, alfabetica, nombre in [("1", "r", "resumen"), ("3", "f", "fds"),
                                             ("5", "s", "senales"), ("7", "g", "sistema")]:
            with self.subTest(vista=nombre):
                e1, e2 = display.EstadoUI(), display.EstadoUI()
                _apretar(e1, numerica)
                _apretar(e2, alfabetica)
                self.assertEqual(e1.vista_actual, nombre)
                self.assertEqual(e2.vista_actual, e1.vista_actual)

    def test_q_pide_salir(self):
        self.assertTrue(_apretar(display.EstadoUI(), "q"))

    def test_una_tecla_cualquiera_no_pide_salir(self):
        self.assertFalse(_apretar(display.EstadoUI(), "z"))


class TestIntervalos(unittest.TestCase):
    def test_mas_y_menos_afectan_solo_a_la_vista_activa(self):
        ivs = _intervalos()
        estado = display.EstadoUI()
        estado.vista_actual = "memoria"
        _apretar(estado, "+", ivs)
        self.assertEqual(ivs["memoria"].value, 3.5)
        self.assertEqual(ivs["resumen"].value, 2.0)  # la otra vista no se movió

    def test_no_se_puede_bajar_por_debajo_del_minimo(self):
        # Es la regresión del bug que tenía el reload por SIGHUP: el piso lo
        # aplica vistas.clamp_intervalo, compartido por el teclado y el reload.
        ivs = _intervalos()
        estado = display.EstadoUI()
        estado.vista_actual = "senales"
        for _ in range(30):
            _apretar(estado, "-", ivs)
        self.assertEqual(ivs["senales"].value, vistas.MINIMOS["senales"])

    def test_el_igual_funciona_como_el_mas(self):
        # '+' sin shift en muchos teclados llega como '='
        ivs = _intervalos()
        estado = display.EstadoUI()
        _apretar(estado, "=", ivs)
        self.assertEqual(ivs["resumen"].value, 2.5)


class TestOrdenYFiltros(unittest.TestCase):
    def test_c_cicla_el_orden_y_vuelve_al_principio(self):
        estado = display.EstadoUI()
        self.assertEqual(estado.orden, "cpu")
        for esperado in ["rss", "pid", "cpu"]:
            _apretar(estado, "c")
            self.assertEqual(estado.orden, esperado)

    def test_barra_abre_el_modo_entrada_de_comando(self):
        estado = display.EstadoUI()
        _apretar(estado, "/")
        self.assertEqual(estado.modo_entrada, "comando")

    def test_u_abre_el_modo_entrada_de_usuario(self):
        estado = display.EstadoUI()
        _apretar(estado, "u")
        self.assertEqual(estado.modo_entrada, "usuario")

    def test_escribir_un_filtro_y_confirmar_con_enter(self):
        estado = display.EstadoUI()
        _apretar(estado, "/")
        for ch in "python":
            display._manejar_entrada_texto(estado, ord(ch))
        display._manejar_entrada_texto(estado, 10)  # Enter
        self.assertEqual(estado.filtro_comando, "python")
        self.assertIsNone(estado.modo_entrada)

    def test_escape_cancela_sin_aplicar_el_filtro(self):
        estado = display.EstadoUI()
        estado.filtro_comando = "anterior"
        _apretar(estado, "/")
        for ch in "nuevo":
            display._manejar_entrada_texto(estado, ord(ch))
        display._manejar_entrada_texto(estado, 27)  # ESC
        self.assertEqual(estado.filtro_comando, "anterior")
        self.assertIsNone(estado.modo_entrada)

    def test_backspace_borra_del_buffer(self):
        estado = display.EstadoUI()
        _apretar(estado, "/")
        for ch in "abc":
            display._manejar_entrada_texto(estado, ord(ch))
        display._manejar_entrada_texto(estado, 127)
        self.assertEqual(estado.buffer_entrada, "ab")


class TestListaYPin(unittest.TestCase):
    def _snapshot(self, filas):
        return {"resumen": {"data": {f["pid"]: f for f in filas}, "ts": 1.0}}

    def _fila(self, pid, cpu=0.0, rss=0, usuario="root", comando="x"):
        return {"pid": pid, "cpu_pct": cpu, "rss_kb": rss, "usuario": usuario,
                "comando": comando, "ppid": 1, "estado": "S", "threads": 1}

    def test_orden_por_cpu_rss_y_pid(self):
        snap = self._snapshot([
            self._fila(10, cpu=1.0, rss=300),
            self._fila(20, cpu=9.0, rss=100),
            self._fila(30, cpu=5.0, rss=200),
        ])
        estado = display.EstadoUI()
        for orden, esperado in [("cpu", [20, 30, 10]), ("rss", [10, 30, 20]), ("pid", [10, 20, 30])]:
            estado.orden = orden
            filas, _ = display._obtener_lista_procesos(snap, estado)
            self.assertEqual([f["pid"] for f in filas], esperado, f"orden={orden}")

    def test_el_pin_deja_el_proceso_primero_pese_al_orden(self):
        # Es el requisito literal de la consigna: "no cambia aunque cambie el orden".
        snap = self._snapshot([
            self._fila(10, cpu=1.0), self._fila(20, cpu=9.0), self._fila(30, cpu=5.0),
        ])
        estado = display.EstadoUI()
        estado.pid_pineado = 10  # el de MENOS cpu
        filas, _ = display._obtener_lista_procesos(snap, estado)
        self.assertEqual(filas[0]["pid"], 10)
        estado.orden = "pid"
        filas, _ = display._obtener_lista_procesos(snap, estado)
        self.assertEqual(filas[0]["pid"], 10)

    def test_el_pin_se_suelta_si_el_proceso_muere(self):
        estado = display.EstadoUI()
        estado.pid_pineado = 999  # no está en el snapshot
        display._obtener_lista_procesos(self._snapshot([self._fila(10)]), estado)
        self.assertIsNone(estado.pid_pineado)

    def test_filtros_de_comando_y_usuario(self):
        snap = self._snapshot([
            self._fila(10, usuario="root", comando="python3 main.py"),
            self._fila(20, usuario="juana", comando="bash"),
        ])
        estado = display.EstadoUI()
        estado.filtro_comando = "PYTHON"  # el filtro es case-insensitive
        filas, _ = display._obtener_lista_procesos(snap, estado)
        self.assertEqual([f["pid"] for f in filas], [10])

        estado = display.EstadoUI()
        estado.filtro_usuario = "juana"
        filas, _ = display._obtener_lista_procesos(snap, estado)
        self.assertEqual([f["pid"] for f in filas], [20])

    def test_snapshot_vacio_no_explota(self):
        filas, ts = display._obtener_lista_procesos({}, display.EstadoUI())
        self.assertEqual(filas, [])
        self.assertEqual(ts, 0)

    def test_enter_pinea_y_despinea_el_seleccionado(self):
        estado = display.EstadoUI()
        estado._ultimo_orden_pids = [11, 22, 33]
        estado.seleccion = 1
        _apretar(estado, 10)                      # Enter
        self.assertEqual(estado.pid_pineado, 22)
        _apretar(estado, 10)                      # Enter otra vez: toggle
        self.assertIsNone(estado.pid_pineado)

    def test_navegacion_no_baja_de_cero(self):
        estado = display.EstadoUI()
        _apretar(estado, display.curses.KEY_UP)
        self.assertEqual(estado.seleccion, 0)


class TestAyuda(unittest.TestCase):
    def test_h_y_pregunta_abren_la_ayuda(self):
        for tecla in ("h", "?"):
            with self.subTest(tecla=tecla):
                estado = display.EstadoUI()
                _apretar(estado, tecla)
                self.assertTrue(estado.mostrar_ayuda)

    def test_cualquier_tecla_cierra_la_ayuda(self):
        # El propio panel dice "cualquier tecla cierra esta ayuda"; antes sólo
        # la cerraba h/?, así que apretar '2' cambiaba la vista de atrás
        # mientras el overlay seguía tapando la pantalla.
        for tecla in ("h", "?", "2", "c", "+", "-", "/", "u", "z"):
            with self.subTest(tecla=tecla):
                estado = display.EstadoUI()
                estado.mostrar_ayuda = True
                _apretar(estado, tecla)
                self.assertFalse(estado.mostrar_ayuda)

    def test_la_tecla_que_cierra_la_ayuda_no_hace_otra_cosa(self):
        estado = display.EstadoUI()
        estado.mostrar_ayuda = True
        _apretar(estado, "2")
        self.assertEqual(estado.vista_actual, "resumen")  # no cambió de vista

    def test_q_sale_incluso_con_la_ayuda_abierta(self):
        estado = display.EstadoUI()
        estado.mostrar_ayuda = True
        self.assertTrue(_apretar(estado, "q"))

    def test_la_ayuda_entra_en_una_terminal_de_24_filas(self):
        import inspect
        fuente = inspect.getsource(display._dibujar_ayuda)
        lineas = fuente.count('\n        "')
        self.assertLessEqual(lineas + 2, 24, f"la ayuda tiene {lineas} lineas + marco")


class TestModoVerbose(unittest.TestCase):
    """SIGUSR2 tiene que cambiar los DATOS, no sólo el cartel del encabezado."""

    def test_verbose_amplia_el_tope_de_fds_por_proceso(self):
        self.assertLess(an_fds._MAX_NORMAL, an_fds._MAX_VERBOSE)

    def test_inodo_de_pipes_y_sockets(self):
        self.assertEqual(an_fds._inodo("pipe:[2249]"), "2249")
        self.assertEqual(an_fds._inodo("socket:[31337]"), "31337")

    def test_inodo_es_none_para_lo_que_no_es_pipe_ni_socket(self):
        # anon_inode también tiene la forma ':[...]' pero adentro trae el tipo,
        # no un inodo: rotularlo como inodo sería mentir.
        for destino in ("/dev/pts/0", "/home/x/a.txt", "anon_inode:[eventpoll]",
                        "anon_inode:[eventfd]", None, ""):
            with self.subTest(destino=destino):
                self.assertIsNone(an_fds._inodo(destino))


if __name__ == "__main__":
    unittest.main()
