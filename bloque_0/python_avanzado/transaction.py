"""Context manager que permite revertir cambios en un objeto si ocurre una excepción."""


class Transaction:
    """Guarda el estado de un objeto y lo restaura si el bloque falla.

    Uso:
        with Transaction(objeto):
            objeto.atributo = nuevo_valor
            # Si hay excepción, se revierte a los valores anteriores
    """

    def __init__(self, objeto) -> None:
        """Recibe el objeto cuyo estado se va a proteger."""
        self._objeto = objeto
        self._estado_inicial: dict = {}

    def __enter__(self) -> "Transaction":
        """Guarda una copia del estado actual del objeto."""
        self._estado_inicial = vars(self._objeto).copy()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Restaura el estado anterior si hubo una excepción."""
        if exc_type is not None:
            vars(self._objeto).update(self._estado_inicial)
        return False


if __name__ == "__main__":

    class Cuenta:
        """Cuenta bancaria simple para demostrar transacciones."""

        def __init__(self, saldo: float, nombre: str = "Sin nombre") -> None:
            self.saldo = saldo
            self.nombre = nombre

        def __repr__(self) -> str:
            return f"Cuenta(nombre={self.nombre!r}, saldo={self.saldo})"

    cuenta = Cuenta(1000)
    print(f"Estado inicial: {cuenta}")

    print("\n=== Transacción exitosa ===")
    with Transaction(cuenta):
        cuenta.saldo -= 100
        cuenta.saldo -= 200
    print(f"Después de la transacción: {cuenta}")

    print("\n=== Transacción que falla (debe revertirse) ===")
    try:
        with Transaction(cuenta):
            cuenta.saldo -= 100
            cuenta.nombre = "Modificado"
            raise ValueError("Error simulado en mitad de la transacción")
    except ValueError as e:
        print(f"Excepción capturada: {e}")
    print(f"Estado después del rollback: {cuenta}")

    print("\n=== Verificación: saldo y nombre no cambiaron ===")
    assert cuenta.saldo == 700, "El saldo debería ser 700"
    assert cuenta.nombre == "Sin nombre", "El nombre debería ser 'Sin nombre'"
    print("OK — el estado fue revertido correctamente.")
