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
from analizadores import _base  # noqa: E402
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
        _apretar(estado, display.curses.KEY_DOWN)   # baja a la fila 1 -> pid 22
        self.assertEqual(estado.pid_seleccionado, 22)
        _apretar(estado, 10)                        # Enter
        self.assertEqual(estado.pid_pineado, 22)
        _apretar(estado, 10)                        # Enter otra vez: toggle
        self.assertIsNone(estado.pid_pineado)

    def test_pinear_no_desplaza_la_seleccion_a_otro_proceso(self):
        """
        Regresión del bug del pin. Con la selección anclada al índice, pinear
        movía la fila al tope pero el índice se quedaba quieto: el detalle
        pasaba a mostrar otro proceso y el segundo Enter pineaba a ese otro en
        vez de despinear.
        """
        snap = self._snapshot([
            self._fila(10, cpu=9.0), self._fila(20, cpu=5.0), self._fila(30, cpu=3.0),
        ])
        estado = display.EstadoUI()

        # frame 1: la lista sale ordenada por cpu y el usuario baja hasta el 30
        filas, _ = display._obtener_lista_procesos(snap, estado)
        estado._ultimo_orden_pids = [f["pid"] for f in filas]
        display._reanclar_seleccion(estado, estado._ultimo_orden_pids)
        _apretar(estado, display.curses.KEY_DOWN)
        _apretar(estado, display.curses.KEY_DOWN)
        self.assertEqual(estado.pid_seleccionado, 30)

        # pinea el 30
        _apretar(estado, 10)
        self.assertEqual(estado.pid_pineado, 30)

        # frame 2: el 30 salta al tope y la selección tiene que seguirlo
        filas, _ = display._obtener_lista_procesos(snap, estado)
        estado._ultimo_orden_pids = [f["pid"] for f in filas]
        display._reanclar_seleccion(estado, estado._ultimo_orden_pids)
        self.assertEqual(estado._ultimo_orden_pids[0], 30, "el pineado va primero")
        self.assertEqual(estado.seleccion, 0, "la barra siguió al proceso pineado")
        self.assertEqual(estado.pid_seleccionado, 30, "el detalle muestra el pineado")

        # y ahora el segundo Enter SÍ despinea
        _apretar(estado, 10)
        self.assertIsNone(estado.pid_pineado)

    def test_la_seleccion_sigue_al_proceso_aunque_cambie_el_orden(self):
        """
        Con orden por CPU% la lista se reordena sola en cada refresco. La barra
        tiene que quedarse sobre el mismo proceso, no sobre la misma fila.
        """
        estado = display.EstadoUI()
        snap = self._snapshot([self._fila(10, cpu=9.0), self._fila(20, cpu=1.0)])
        filas, _ = display._obtener_lista_procesos(snap, estado)
        estado._ultimo_orden_pids = [f["pid"] for f in filas]
        display._reanclar_seleccion(estado, estado._ultimo_orden_pids)
        _apretar(estado, display.curses.KEY_DOWN)
        self.assertEqual(estado.pid_seleccionado, 20)

        # el 20 pega un pico y pasa a estar primero
        snap = self._snapshot([self._fila(10, cpu=1.0), self._fila(20, cpu=9.0)])
        filas, _ = display._obtener_lista_procesos(snap, estado)
        display._reanclar_seleccion(estado, [f["pid"] for f in filas])
        self.assertEqual(estado.pid_seleccionado, 20, "no saltó al otro proceso")
        self.assertEqual(estado.seleccion, 0, "el índice se recalculó solo")

    def test_cambiar_de_vista_mantiene_el_proceso_seleccionado(self):
        estado = display.EstadoUI()
        estado._ultimo_orden_pids = [11, 22, 33]
        _apretar(estado, display.curses.KEY_DOWN)
        _apretar(estado, "2")   # a la vista Memoria
        self.assertEqual(estado.vista_actual, "memoria")
        self.assertEqual(estado.pid_seleccionado, 22)

    def test_si_muere_el_proceso_seleccionado_la_seleccion_no_explota(self):
        estado = display.EstadoUI()
        estado.pid_seleccionado = 999
        estado.seleccion = 5
        display._reanclar_seleccion(estado, [11, 22])
        self.assertEqual(estado.seleccion, 1)
        self.assertEqual(estado.pid_seleccionado, 22)

    def test_lista_vacia_no_explota(self):
        estado = display.EstadoUI()
        estado.pid_seleccionado = 42
        display._reanclar_seleccion(estado, [])
        self.assertEqual(estado.seleccion, 0)
        self.assertIsNone(estado.pid_seleccionado)

    def test_navegacion_no_baja_de_cero(self):
        estado = display.EstadoUI()
        estado._ultimo_orden_pids = [11, 22]
        _apretar(estado, display.curses.KEY_UP)
        self.assertEqual(estado.seleccion, 0)

    def test_navegacion_no_pasa_del_final(self):
        estado = display.EstadoUI()
        estado._ultimo_orden_pids = [11, 22]
        for _ in range(10):
            _apretar(estado, display.curses.KEY_DOWN)
        self.assertEqual(estado.seleccion, 1)
        self.assertEqual(estado.pid_seleccionado, 22)


class TestLayout(unittest.TestCase):
    """
    Regresión del panel de detalle recortado. Con un reparto 45/55 fijo, en una
    terminal estándar de 24 filas al detalle le quedaban 10 filas útiles y la
    vista Señales necesita 11: la última máscara (ShdPnd) terminaba debajo del
    pie de página, invisible.
    """

    # Lo que ocupa cada panel de detalle, contado sobre el código de display.py
    NECESITA = {"senales": 11, "sistema": 11, "resumen": 5, "scheduling": 6}

    def _filas_utiles_de_detalle(self, alto):
        alto_lista = display._alto_de_lista(alto)
        inicio_detalle = 3 + alto_lista + 1
        # _dibujar_detalle gasta su primera fila en la línea de "=", y la última
        # fila de la pantalla es el pie
        return alto - inicio_detalle - 1 - 1

    def test_en_24_filas_entran_todos_los_paneles(self):
        utiles = self._filas_utiles_de_detalle(24)
        for vista, necesita in self.NECESITA.items():
            with self.subTest(vista=vista):
                self.assertGreaterEqual(
                    utiles, necesita,
                    f"la vista {vista} necesita {necesita} filas y hay {utiles}")

    def test_la_lista_nunca_queda_sin_espacio(self):
        for alto in range(10, 60):
            with self.subTest(alto=alto):
                self.assertGreaterEqual(display._alto_de_lista(alto), display._LISTA_MIN)

    def test_en_terminales_grandes_vuelve_al_reparto_nominal(self):
        # con espacio de sobra la lista se lleva ~45%, no el mínimo
        self.assertEqual(display._alto_de_lista(50), int((50 - 5) * 0.45))

    def test_el_detalle_nunca_invade_el_pie(self):
        for alto in range(20, 60):
            with self.subTest(alto=alto):
                alto_lista = display._alto_de_lista(alto)
                inicio_detalle = 3 + alto_lista + 1
                self.assertLess(inicio_detalle, alto - 1)


class TestRecortar(unittest.TestCase):
    """
    El recorte silencioso de addnstr puede mentir: una lista de señales cortada
    en el borde termina en "SIGCHLD," y se lee como si esa fuera la última.
    """

    def test_no_toca_lo_que_entra(self):
        self.assertEqual(display._recortar("hola", 10), "hola")
        self.assertEqual(display._recortar("hola", 4), "hola")

    def test_marca_el_corte_con_elipsis(self):
        self.assertEqual(display._recortar("abcdefgh", 4), "abc…")

    def test_el_resultado_nunca_excede_el_ancho(self):
        for n in range(1, 30):
            with self.subTest(n=n):
                self.assertLessEqual(len(display._recortar("x" * 50, n)), n)

    def test_ancho_cero_o_negativo_devuelve_vacio(self):
        self.assertEqual(display._recortar("hola", 0), "")
        self.assertEqual(display._recortar("hola", -3), "")

    def test_una_lista_de_senales_cortada_se_nota(self):
        nombres = ", ".join(["SIGHUP", "SIGINT", "SIGQUIT", "SIGILL", "SIGTRAP"])
        recortado = display._recortar(nombres, 20)
        self.assertTrue(recortado.endswith("…"))
        self.assertFalse(recortado.endswith(", "))


class TestTerminalChica(unittest.TestCase):
    """
    Por debajo del mínimo se muestra un cartel en vez de dibujar la interfaz.
    Sin el guard, curses posiciona el cursor en filas que no existen, la
    terminal scrollea para acomodarlas y la pantalla queda repetida varias veces
    hacia abajo: parece que el programa está roto cuando sólo falta agrandar.
    """

    def test_el_minimo_alcanza_para_el_layout_basico(self):
        # encabezado (3) + lista mínima + separador + al menos 1 de detalle + pie
        minimo_necesario = 3 + display._LISTA_MIN + 1 + 1 + 1
        self.assertGreaterEqual(display._MIN_FILAS, minimo_necesario)

    def test_el_minimo_entra_en_el_pie_mas_corto(self):
        self.assertGreaterEqual(display._MIN_COLS, len(display._PIES[-1]) + 1)

    def test_24x80_esta_por_encima_del_minimo(self):
        # el tamaño estándar tiene que dibujar la interfaz completa, no el cartel
        self.assertLess(display._MIN_FILAS, 24)
        self.assertLess(display._MIN_COLS, 80)

    def test_alto_de_lista_sigue_siendo_valido_en_el_minimo(self):
        al = display._alto_de_lista(display._MIN_FILAS)
        self.assertGreaterEqual(al, display._LISTA_MIN)
        self.assertLess(3 + al + 1, display._MIN_FILAS - 1)


class TestPie(unittest.TestCase):
    """
    Regresión: el pie medía 103 caracteres y en una terminal de 80 columnas se
    cortaba en "+/- interva", escondiendo `h ayuda` y `q salir`.
    """

    def _elegido(self, ancho):
        presupuesto = max(0, ancho - 1)
        return next((p for p in display._PIES if len(p) <= presupuesto), display._PIES[-1])

    def test_en_80_columnas_se_ven_ayuda_y_salir(self):
        pie = self._elegido(80)
        self.assertLessEqual(len(pie), 79)
        self.assertIn("h ayuda", pie)
        self.assertIn("q salir", pie)

    def test_todos_los_anchos_razonables_conservan_como_salir(self):
        for ancho in range(20, 200):
            with self.subTest(ancho=ancho):
                self.assertIn("q salir", self._elegido(ancho))

    def test_en_terminal_ancha_usa_la_version_completa(self):
        self.assertEqual(self._elegido(200), display._PIES[0])

    def test_las_variantes_estan_ordenadas_de_mas_larga_a_mas_corta(self):
        largos = [len(p) for p in display._PIES]
        self.assertEqual(largos, sorted(largos, reverse=True))


class TestFiltrosDefault(unittest.TestCase):
    def test_se_aplican_en_el_primer_frame(self):
        # Regresión: había un `if seq != 0` que salteaba justo el primer frame,
        # así que filtros_default de config.json se ignoraba hasta un SIGHUP.
        estado = display.EstadoUI()
        control = {"reload_seq": 0,
                   "filtros_default": {"comando": "python", "usuario": "root"}}
        display._revisar_reload(estado, control)
        self.assertEqual(estado.filtro_comando, "python")
        self.assertEqual(estado.filtro_usuario, "root")

    def test_no_se_reaplican_en_cada_frame(self):
        # si el usuario cambia el filtro a mano, los frames siguientes no lo pisan
        estado = display.EstadoUI()
        control = {"reload_seq": 0, "filtros_default": {"comando": "python", "usuario": ""}}
        display._revisar_reload(estado, control)
        estado.filtro_comando = "otra-cosa"
        display._revisar_reload(estado, control)
        self.assertEqual(estado.filtro_comando, "otra-cosa")

    def test_un_sighup_vuelve_a_aplicarlos(self):
        estado = display.EstadoUI()
        control = {"reload_seq": 0, "filtros_default": {"comando": "python", "usuario": ""}}
        display._revisar_reload(estado, control)
        estado.filtro_comando = "otra-cosa"
        control["reload_seq"] = 1
        control["filtros_default"] = {"comando": "bash", "usuario": ""}
        display._revisar_reload(estado, control)
        self.assertEqual(estado.filtro_comando, "bash")


class TestAislamientoPorPid(unittest.TestCase):
    """
    `por_pid` existe para que un PID problemático no cueste la vuelta entera:
    sin él, la excepción sube a run_loop, que descarta el dict completo y deja
    la vista congelada con el snapshot viejo.
    """

    def test_un_pid_que_falla_no_tira_el_resto(self):
        def fn(pid):
            if pid == 2:
                raise RuntimeError("boom")
            return pid, {"pid": pid}

        resultado = _base.por_pid([1, 2, 3], fn)
        self.assertEqual(sorted(resultado), [1, 3])

    def test_none_saltea_sin_contarlo_como_error(self):
        errores = []
        resultado = _base.por_pid([1, 2], lambda p: None, errores.append)
        self.assertEqual(resultado, {})
        self.assertEqual(errores, [])

    def test_los_errores_se_loguean_pero_con_tope(self):
        def fn(pid):
            raise RuntimeError("boom")

        mensajes = []
        _base.por_pid(list(range(20)), fn, mensajes.append)
        # 3 errores concretos + 1 línea de resumen
        self.assertEqual(len(mensajes), _base._MAX_ERRORES_LOGUEADOS + 1)
        self.assertIn("17 pids más", mensajes[-1])

    def test_sin_logger_no_explota(self):
        def fn(pid):
            raise RuntimeError("boom")

        self.assertEqual(_base.por_pid([1, 2], fn), {})


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
