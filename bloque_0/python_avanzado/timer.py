"""Context manager para medir el tiempo de ejecución de un bloque de código."""
import time
from contextlib import contextmanager


class Timer:
    """Mide el tiempo transcurrido en un bloque with.

    Uso:
        with Timer("Mi operación"):
            ...

        with Timer() as t:
            ...
        print(t.elapsed)
    """

    def __init__(self, nombre: str = "") -> None:
        """Inicializa el timer con un nombre opcional."""
        self.nombre = nombre
        self._inicio: float | None = None
        self._elapsed: float = 0.0

    def __enter__(self) -> "Timer":
        """Inicia el cronómetro al entrar al bloque."""
        self._inicio = time.perf_counter()
        self._elapsed = 0.0
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Detiene el cronómetro e imprime el resultado si hay nombre."""
        if self._inicio is not None:
            self._elapsed = time.perf_counter() - self._inicio
        if self.nombre:
            print(f"[Timer] {self.nombre}: {self._elapsed:.3f}s")
        return False

    @property
    def elapsed(self) -> float:
        """Retorna el tiempo transcurrido desde el inicio o el tiempo final."""
        if self._inicio is None:
            return 0.0
        if self._elapsed > 0:
            return self._elapsed
        return time.perf_counter() - self._inicio


@contextmanager
def timer(nombre: str = ""):
    """Versión funcional del Timer usando @contextmanager.

    Uso:
        with timer("Mi operación"):
            ...
    """
    inicio = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - inicio
        if nombre:
            print(f"[Timer] {nombre}: {elapsed:.3f}s")


if __name__ == "__main__":
    print("=== Timer como clase ===")
    with Timer("Lista de cuadrados"):
        datos = [x ** 2 for x in range(1_000_000)]

    print("\n=== Timer sin nombre, acceso a elapsed ===")
    with Timer() as t:
        time.sleep(0.2)
    print(f"El bloque tardó {t.elapsed:.3f} segundos")

    print("\n=== Timer con pasos intermedios ===")
    with Timer() as t:
        time.sleep(0.1)
        print(f"  Después del paso 1: {t.elapsed:.3f}s")
        time.sleep(0.1)
        print(f"  Después del paso 2: {t.elapsed:.3f}s")

    print("\n=== Versión @contextmanager ===")
    with timer("Operación con contextmanager"):
        time.sleep(0.15)
