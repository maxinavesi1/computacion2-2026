"""
tests/test_procfs.py — tests unitarios de procfs.py contra fixtures estáticos.

Se corren con:  python3 -m unittest discover -s tests -v
(desde la raíz del repo, con src/ en el PYTHONPATH — ver tests/__init__.py
y el pequeño sys.path.insert de más abajo).

No requieren Docker ni permisos de root: son puramente sobre texto de
ejemplo, para poder testear el parseo (la parte más propensa a errores
sutiles de todo el TP) de forma rápida y repetible.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import procfs  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _leer_fixture(nombre):
    with open(os.path.join(FIXTURES, nombre)) as f:
        return f.read()


class TestParseStat(unittest.TestCase):
    def setUp(self):
        self.raw = _leer_fixture("stat_comm_raro.txt")
        self.data = procfs.parse_stat(self.raw)

    def test_comm_con_parentesis_y_espacios(self):
        # El caso límite que justifica NO hacer un simple .split() de todo
        # el archivo: comm contiene sus propios paréntesis.
        self.assertEqual(self.data["comm"], "my (weird) proc")
        self.assertEqual(self.data["pid"], 1234)

    def test_campos_basicos(self):
        self.assertEqual(self.data["state"], "S")
        self.assertEqual(self.data["ppid"], 1)
        self.assertEqual(self.data["pgrp"], 1234)
        self.assertEqual(self.data["session"], 5678)

    def test_campos_de_scheduling(self):
        self.assertEqual(self.data["priority"], 20)
        self.assertEqual(self.data["nice"], 0)
        self.assertEqual(self.data["rt_priority"], 0)
        self.assertEqual(self.data["policy"], 0)
        self.assertEqual(procfs.policy_name(self.data["policy"]), "SCHED_OTHER")

    def test_campos_de_cpu_y_faults(self):
        self.assertEqual(self.data["utime"], 250)
        self.assertEqual(self.data["stime"], 50)
        self.assertEqual(self.data["minflt"], 100)
        self.assertEqual(self.data["majflt"], 5)

    def test_stat_invalido_no_explota(self):
        self.assertIsNone(procfs.parse_stat(None))
        self.assertIsNone(procfs.parse_stat("esto no tiene parentesis"))


class TestParseStatus(unittest.TestCase):
    def setUp(self):
        self.raw = _leer_fixture("status_ejemplo.txt")
        self.data = procfs.parse_status(self.raw)

    def test_campos_kb_se_convierten_a_int(self):
        self.assertEqual(self.data["VmRSS"], 44000)
        self.assertEqual(self.data["VmSize"], 123000)
        self.assertIsInstance(self.data["VmRSS"], int)

    def test_uid_gid_como_tupla(self):
        self.assertEqual(self.data["Uid"], (1000, 1000, 1000, 1000))
        self.assertEqual(self.data["Gid"], (1000, 1000, 1000, 1000))

    def test_threads_y_ctxt_switches(self):
        self.assertEqual(self.data["Threads"], 4)
        self.assertEqual(self.data["voluntary_ctxt_switches"], 42)
        self.assertEqual(self.data["nonvoluntary_ctxt_switches"], 7)

    def test_mascaras_de_senales_quedan_como_string_hex(self):
        self.assertEqual(self.data["SigBlk"], "0000000000010000")
        self.assertIsInstance(self.data["SigIgn"], str)


class TestDecodeSigmask(unittest.TestCase):
    def test_mascara_vacia(self):
        self.assertEqual(procfs.decode_sigmask("0000000000000000"), [])

    def test_una_sola_senal_conocida(self):
        # bit 1 (0-indexado) -> señal 2 -> SIGINT
        self.assertEqual(procfs.decode_sigmask("2"), ["SIGINT"])

    def test_varias_senales_combinadas(self):
        # SIGHUP (bit0) + SIGTERM (bit14) -> 0x1 | 0x4000 = 0x4001
        nombres = procfs.decode_sigmask("4001")
        self.assertIn("SIGHUP", nombres)
        self.assertIn("SIGTERM", nombres)
        self.assertEqual(len(nombres), 2)

    def test_senal_realtime_sin_nombre_posix_estandar(self):
        # bit 33 -> señal 34 -> SIGRTMIN
        mask = hex(1 << 33)[2:]
        self.assertIn("SIGRTMIN", procfs.decode_sigmask(mask))

    def test_senales_internas_de_glibc_32_y_33(self):
        # 32 y 33 no son POSIX ni RT: son internas de NPTL (la implementación
        # de threads de glibc). Aparecen en el SigBlk de cualquier proceso
        # multithread, así que hay que nombrarlas o la vista Señales muestra
        # dos bits sin explicación.
        mask = hex((1 << 31) | (1 << 32))[2:]
        nombres = procfs.decode_sigmask(mask)
        self.assertEqual(set(nombres), {"SIGCANCEL", "SIGSETXID"})

    def test_sigrtmin_se_toma_de_la_stdlib_no_hardcodeado(self):
        # glibc se reserva 32 y 33, así que corre el rango RT dos lugares:
        # SIGRTMIN es 34 y no 32 (que es el __SIGRTMIN del kernel). Verificamos
        # que el rango arranque justo después de las dos señales de glibc.
        self.assertEqual(procfs._SIGRTMIN, 34)
        self.assertNotIn(procfs._SIGRTMIN, procfs._SIGNAL_NAMES_GLIBC)
        self.assertEqual(procfs._nombre_senal(procfs._SIGRTMIN), "SIGRTMIN")
        self.assertEqual(procfs._nombre_senal(procfs._SIGRTMIN + 3), "SIGRTMIN+3")

    def test_una_mascara_real_de_proceso_multithread(self):
        # SigCgt real de un proceso Python multithread: SIGINT + las dos de
        # glibc. Es el caso que motivó el mapeo, no un caso sintético.
        nombres = procfs.decode_sigmask("0000000180000002")
        self.assertEqual(nombres, ["SIGINT", "SIGCANCEL", "SIGSETXID"])


class TestMaps(unittest.TestCase):
    def setUp(self):
        self.segmentos = procfs.parse_maps(_leer_fixture("maps_ejemplo.txt"))
        self.grupos, self.conteo = procfs.agrupar_segmentos(self.segmentos)

    def test_cantidad_de_segmentos(self):
        self.assertEqual(len(self.segmentos), 6)

    def test_agrupamiento_por_categoria(self):
        self.assertEqual(self.grupos["heap"], 132)
        self.assertEqual(self.grupos["stack"], 132)
        self.assertEqual(self.grupos["shared"], 64)
        self.assertEqual(self.grupos["text"], 20)
        self.assertEqual(self.grupos["data"], 60)

    def test_conteo_de_segmentos_por_categoria(self):
        self.assertEqual(self.conteo["data"], 2)  # el rodata sin permiso 'x' + el mmap anónimo
        self.assertEqual(self.conteo["text"], 1)
        self.assertEqual(self.conteo["heap"], 1)
        self.assertEqual(self.conteo["stack"], 1)
        self.assertEqual(self.conteo["shared"], 1)


class TestTipoFd(unittest.TestCase):
    def test_tipos_conocidos(self):
        casos = {
            "socket:[12345]": "socket",
            "pipe:[999]": "pipe",
            "anon_inode:[eventfd]": "anon_inode",
            "/dev/pts/3": "tty",
            "/home/user/archivo.txt": "file",
            None: "?",
        }
        for destino, esperado in casos.items():
            with self.subTest(destino=destino):
                self.assertEqual(procfs._tipo_fd(destino), esperado)


class TestCalcularCpuPct(unittest.TestCase):
    def test_delta_cero_da_cero_por_ciento(self):
        self.assertEqual(procfs.calcular_cpu_pct(100, 100, 0.0, 1.0), 0.0)

    def test_medio_nucleo_ocupado_durante_un_segundo(self):
        # CLK_TCK jiffies por segundo (típicamente 100). Medio segundo de
        # CPU (CLK_TCK/2 jiffies) en 1 segundo real = 50%.
        jiffies_medio_segundo = procfs.CLK_TCK // 2
        pct = procfs.calcular_cpu_pct(0, jiffies_medio_segundo, 0.0, 1.0)
        self.assertAlmostEqual(pct, 50.0, delta=1.0)

    def test_wall_time_no_positivo_no_explota(self):
        self.assertEqual(procfs.calcular_cpu_pct(0, 100, 5.0, 5.0), 0.0)


if __name__ == "__main__":
    unittest.main()
