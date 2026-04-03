"""Decorador de reintentos para funciones que pueden fallar transitoriamente."""
import time
from functools import wraps
from typing import Tuple, Type


def retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
):
    """Decora una función para reintentar su ejecución si lanza una excepción.

    Args:
        max_attempts: Número máximo de intentos (default: 3).
        delay: Segundos de espera entre intentos (default: 1).
        exceptions: Tupla de tipos de excepción a capturar (default: Exception).

    Uso:
        @retry(max_attempts=3, delay=1, exceptions=(ConnectionError,))
        def conectar():
            ...
    """
    def decorador(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            ultimo_error = None
            for intento in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as error:
                    ultimo_error = error
                    if intento < max_attempts:
                        print(
                            f"Intento {intento}/{max_attempts} falló: {error}. "
                            f"Esperando {delay}s..."
                        )
                        time.sleep(delay)
                    else:
                        print(f"Intento {intento}/{max_attempts} falló: {error}.")
            raise ultimo_error
        return wrapper
    return decorador


if __name__ == "__main__":
    print("=== Caso que falla siempre ===")

    @retry(max_attempts=3, delay=0.1, exceptions=(ConnectionError,))
    def conectar_siempre_falla():
        """Simula un servidor que nunca responde."""
        raise ConnectionError("Servidor no disponible")

    try:
        conectar_siempre_falla()
    except ConnectionError as e:
        print(f"Error final capturado: {e}\n")

    print("=== Caso que eventualmente funciona ===")
    intentos = {"n": 0}

    @retry(max_attempts=5, delay=0.1)
    def conectar_a_veces():
        """Simula un servidor que falla las primeras 2 veces."""
        intentos["n"] += 1
        if intentos["n"] < 3:
            raise ConnectionError("Servidor ocupado")
        return "Conectado exitosamente"

    RESULTADO = conectar_a_veces()
    print(f"Resultado: {RESULTADO}\n")

    print("=== Solo captura excepciones indicadas ===")

    @retry(max_attempts=3, delay=0.1, exceptions=(ConnectionError,))
    def lanza_otro_error():
        """Lanza un ValueError que no debe reintentarse."""
        raise ValueError("Error de lógica, no reintentar")

    try:
        lanza_otro_error()
    except ValueError as e:
        print(f"ValueError propagado sin reintentos: {e}")
