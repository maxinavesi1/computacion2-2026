"""Las tres mediciones obligatorias del TP2.

Uso, desde tp2/:

    docker compose run --rm --build mediciones            # las tres (~5 min)
    docker compose run --rm --build mediciones --solo 2   # una sola

Cada experimento levanta su PROPIO servidor (`python -m app.main`) como
subproceso, con la configuración que necesita (cantidad de workers, modo
bloqueante habilitado, etc.), y lo apaga con SIGTERM al terminar. Las
descargas van contra el servicio `lento` del compose, así los números no
dependen de la red del momento.

Los resultados se imprimen y quedan en mediciones/resultados.md (tablas) y
mediciones/resultados.json (datos crudos).
"""

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent.parent
SALIDA = Path(__file__).resolve().parent
URL_LENTO = os.environ.get("URL_LENTO", "http://localhost:8001")
PUERTO = 8100
API = f"http://127.0.0.1:{PUERTO}"
TERMINALES = ("completada", "fallida", "cancelada")

# 20 sitios que responden 200 con contenido real. Quedaron afuera www.gnu.org
# (desde nuestra red no conecta: ConnectTimeout a los 10 s) y Wikipedia
# (responde 403 a clientes que no se identifican con un User-Agent propio).
URLS_INTERNET = [
    "https://www.python.org/", "https://docs.python.org/3/", "https://pypi.org/",
    "https://peps.python.org/pep-0492/", "https://fastapi.tiangolo.com/",
    "https://www.python-httpx.org/", "https://docs.aiohttp.org/", "https://www.ietf.org/",
    "https://www.apache.org/", "https://github.com/",
    "https://www.debian.org/", "https://www.kernel.org/", "https://www.postgresql.org/",
    "https://www.mozilla.org/", "https://www.um.edu.ar/", "https://www.example.com/",
    "https://www.iana.org/", "https://www.w3.org/", "https://www.rust-lang.org/", "https://go.dev/",
]


# --- infraestructura ------------------------------------------------------------

def _r(x, d=3):
    return None if x is None else round(x, d)


def percentil(valores, p):
    orden = sorted(valores)
    return orden[min(len(orden) - 1, round(p / 100 * (len(orden) - 1)))]


def rss_mb(pid):
    """Memoria residente del proceso, leída de /proc (sólo Linux)."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for linea in f:
                if linea.startswith("VmRSS:"):
                    return int(linea.split()[1]) / 1024
    except OSError:
        pass
    return None


@asynccontextmanager
async def servidor(**config):
    """Levanta la plataforma con la config dada y la apaga con SIGTERM al salir."""
    with tempfile.TemporaryDirectory() as datos:
        env = {**os.environ, "HOST": "127.0.0.1", "PUERTO": str(PUERTO), "DIR_DATOS": datos,
               "WORKERS": "8", "PROCESOS_CPU": "4", "MAX_COLA": "100000", "TIMEOUT_APAGADO": "5"}
        env.update({nombre.upper(): str(valor) for nombre, valor in config.items()})
        ruta_log = Path(datos) / "servidor.log"
        with open(ruta_log, "wb") as log:
            proceso = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.main", cwd=RAIZ, env=env, stdout=log, stderr=log)
            try:
                await _esperar_salud(proceso, ruta_log)
                yield proceso
            except BaseException:
                print(f"\n!! falló con config {config}; final del log del servidor:\n"
                      + ruta_log.read_text(errors="replace")[-3000:], flush=True)
                raise
            finally:
                proceso.terminate()  # SIGTERM: apagado ordenado
                try:
                    await asyncio.wait_for(proceso.wait(), 60)
                except TimeoutError:
                    proceso.kill()
                    await proceso.wait()


async def _esperar_salud(proceso, ruta_log):
    async with httpx.AsyncClient(base_url=API, timeout=2) as c:
        for _ in range(300):
            if proceso.returncode is not None:
                raise RuntimeError("el servidor no arrancó:\n" + ruta_log.read_text()[-2000:])
            try:
                if (await c.get("/salud")).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError("el servidor no respondió /salud en 30 s")


def cliente():
    """Cliente httpx, para lo que no es masivo: crear tareas sueltas y consultar estado."""
    return httpx.AsyncClient(base_url=API, timeout=300,
                             limits=httpx.Limits(max_connections=100, max_keepalive_connections=100))


class ConexionCruda:
    """Una conexión HTTP/1.1 keep-alive escrita a mano sobre asyncio streams.

    Existe porque httpx resultó ser el cuello de botella al medir: es cómodo
    pero pesado, y con decenas de requests simultáneas su propio pool de
    conexiones gasta más CPU que el servidor. Lo medimos: una app FastAPI
    vacía "atendía" 586 req/s con 1 cliente httpx y 270 req/s con 50; con
    esta conexión cruda, la misma app atiende ~2400 req/s y no cae con la
    concurrencia. Las ráfagas de POST y los GET de la medición 2 van por acá
    para medir al servidor y no al cliente.
    """

    def __init__(self, lector, escritor):
        self._lector, self._escritor = lector, escritor

    @classmethod
    async def abrir(cls):
        return cls(*await asyncio.open_connection("127.0.0.1", PUERTO))

    async def pedir(self, metodo, ruta, cuerpo=None):
        """Manda una request y devuelve (código, cuerpo de la respuesta)."""
        datos = json.dumps(cuerpo).encode() if cuerpo is not None else b""
        cabecera = (f"{metodo} {ruta} HTTP/1.1\r\nHost: tp2\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(datos)}\r\n\r\n")
        self._escritor.write(cabecera.encode() + datos)
        linea_estado, *cabeceras = (await self._lector.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
        largo = next(int(c.split(":", 1)[1]) for c in cabeceras if c.lower().startswith("content-length:"))
        return int(linea_estado.split()[1]), await self._lector.readexactly(largo)

    async def cerrar(self):
        self._escritor.close()
        await self._escritor.wait_closed()


async def crear(c, tipo, params, timeout=600):
    r = await c.post("/tareas", json={"tipo": tipo, "params": params, "timeout": timeout})
    r.raise_for_status()
    return r.json()["id"]


async def esperar_tarea(c, tarea_id, cada=0.02):
    while True:
        tarea = (await c.get(f"/tareas/{tarea_id}")).json()
        if tarea["estado"] in TERMINALES:
            return tarea
        await asyncio.sleep(cada)


async def terminadas(c):
    tareas = (await c.get("/estadisticas")).json()["tareas"]
    return sum(tareas[e] for e in TERMINALES)


EMISORES = 50  # conexiones del cliente mandando POST a la vez


async def enviar(k, tipo, params):
    """Manda k POST /tareas por EMISORES conexiones crudas a la vez.

    Cada emisor toma el siguiente número de un iterador compartido hasta
    agotarlo: el patrón productor-consumidor del pool de workers, del lado
    del cliente. (La primera versión lanzaba los k de golpe con gather() y
    httpx: el cliente tardaba 75 s en mandar 5000 tareas de 1 s.)
    """
    restantes = iter(range(k))
    cuerpo = {"tipo": tipo, "params": params, "timeout": 600}
    ids = []

    async def emisor():
        conexion = await ConexionCruda.abrir()
        try:
            for _ in restantes:
                codigo, respuesta = await conexion.pedir("POST", "/tareas", cuerpo)
                if codigo != 202:
                    raise RuntimeError(f"POST /tareas respondió {codigo}: {respuesta[:200]!r}")
                ids.append(json.loads(respuesta)["id"])
        finally:
            await conexion.cerrar()

    # TaskGroup y no gather(): si un emisor falla, los demás se cancelan.
    async with asyncio.TaskGroup() as grupo:
        for _ in range(min(EMISORES, k)):
            grupo.create_task(emisor())
    return ids


async def lote(c, k, tipo, params, detalle=True, cada=0.02):
    """Manda k tareas iguales a la vez y mide cuánto tarda el lote completo."""
    previas = await terminadas(c)
    inicio = time.perf_counter()
    ids = set(await enviar(k, tipo, params))
    envio = time.perf_counter() - inicio
    while await terminadas(c) < previas + k:
        await asyncio.sleep(cada)
    fila = {"k": k, "envio_s": _r(envio), "total_s": _r(time.perf_counter() - inicio)}
    if detalle:  # una sola consulta: el listado ya trae espera_s y duracion_s
        listado = (await c.get("/tareas", params={"limite": 1000})).json()
        tareas = [t for t in listado if t["id"] in ids]
        assert len(tareas) == k, "el detalle sólo alcanza para k <= 1000"
        fila["espera_prom_s"] = _r(statistics.mean(t["espera_s"] for t in tareas))
        fila["duracion_prom_s"] = _r(statistics.mean(t["duracion_s"] for t in tareas))
        fila["no_completadas"] = sum(t["estado"] != "completada" for t in tareas)
    return fila


async def iteraciones_por_segundo(c):
    """Calibra la velocidad del hash en esta máquina."""
    tarea = await esperar_tarea(c, await crear(c, "hashear", {"texto": "calibrar", "iteraciones": 1_000_000}))
    return 1_000_000 / tarea["resultado"]["segundos"]


def tabla(encabezados, filas):
    lineas = ["| " + " | ".join(encabezados) + " |", "|" + "---|" * len(encabezados)]
    lineas += ["| " + " | ".join("—" if v is None else str(v) for v in fila) + " |" for fila in filas]
    return "\n".join(lineas)


# --- Medición 1: concurrencia I/O -------------------------------------------------

async def _una_corrida(urls, concurrente):
    """Una tarea `descargar` en un servidor recién levantado.

    Servidor nuevo en cada corrida para que todas arranquen en frío: si no,
    cada corrida aprovecha las conexiones keep-alive que dejó abiertas la
    anterior, y contra internet los tiempos bajaban corrida a corrida
    (7,8 s → 5,8 s → 3,5 s) sin que cambiara nada de lo que se mide.
    """
    async with servidor(), cliente() as c:
        tarea = await esperar_tarea(c, await crear(
            c, "descargar", {"urls": urls, "concurrente": concurrente}))
    resultado = tarea["resultado"]
    return {"total": resultado["segundos_total"], "ok": resultado["ok"], "errores": resultado["errores"],
            "individuales": [d["segundos"] for d in resultado["descargas"]]}


async def _comparar_descargas(urls, repeticiones):
    """Alterna secuencial/concurrente y resume cada cosa con la mediana de sus corridas."""
    corridas = {False: [], True: []}
    for _ in range(repeticiones):
        for concurrente in (False, True):
            corridas[concurrente].append(await _una_corrida(urls, concurrente))
    secuencial = statistics.median(c["total"] for c in corridas[False])
    concurrente = statistics.median(c["total"] for c in corridas[True])
    todas = corridas[False] + corridas[True]
    return {
        "secuencial_s": _r(secuencial), "concurrente_s": _r(concurrente),
        "speedup": _r(secuencial / concurrente, 1),
        # Lo que la teoría predice para cada modo, medido en las mismas corridas:
        "suma_individuales_s": _r(statistics.median(sum(c["individuales"]) for c in corridas[False])),
        "mas_lenta_s": _r(statistics.median(max(c["individuales"]) for c in corridas[True])),
        "ok": sum(c["ok"] for c in todas), "errores": sum(c["errores"] for c in todas),
        "corridas": {"secuencial": [c["total"] for c in corridas[False]],
                     "concurrente": [c["total"] for c in corridas[True]]},
    }


async def _hay_internet():
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.head("https://www.python.org/")
        return True
    except httpx.HTTPError:
        return False


async def medicion_1(repeticiones):
    urls_lento = [f"{URL_LENTO}/lento?segundos={0.2 + 0.04 * i:.2f}&kb=64" for i in range(20)]
    datos = {"servidor lento (local)": await _comparar_descargas(urls_lento, repeticiones)}
    if await _hay_internet():
        datos["internet (20 sitios reales)"] = await _comparar_descargas(URLS_INTERNET, repeticiones)

    filas = [[destino, d["secuencial_s"], d["concurrente_s"], f"**{d['speedup']}x**",
              d["suma_individuales_s"], d["mas_lenta_s"], f"{d['ok']}/{d['ok'] + d['errores']}"]
             for destino, d in datos.items()]
    md = tabla(["Destino", "Secuencial (s)", "Concurrente (s)", "Speedup",
                "Suma de los tiempos individuales (s)", "La más lenta (s)", "Descargas OK"], filas)
    return datos, md + (f"\n\nMediana de {repeticiones} corridas por modo, alternadas, cada una en un "
                        "servidor recién levantado (sin conexiones abiertas de la corrida anterior).")


# --- Medición 2: el event loop bloqueado ------------------------------------------

def _resumen_latencias(latencias):
    return {"p50_ms": _r(percentil(latencias, 50) * 1000, 1),
            "p95_ms": _r(percentil(latencias, 95) * 1000, 1),
            "max_ms": _r(max(latencias) * 1000, 1)}


async def _get_tareas(conexion):
    """GET /tareas?limite=10 cronometrado. Devuelve (segundos, tareas)."""
    inicio = time.perf_counter()
    codigo, cuerpo = await conexion.pedir("GET", "/tareas?limite=10")
    assert codigo == 200, codigo
    return time.perf_counter() - inicio, json.loads(cuerpo)


async def _latencias_durante_hash(c, conexion, iteraciones, ejecutor, intervalo=0.05):
    """GET /tareas cada `intervalo` s mientras corre un hash; mide cuánto tarda cada respuesta."""
    params = {"texto": "medicion", "iteraciones": iteraciones, "ejecutor": ejecutor}
    tarea_id = await crear(c, "hashear", params)
    latencias, mientras_corria = [], 0
    while True:
        latencia, tareas = await _get_tareas(conexion)
        latencias.append(latencia)
        if next(t["estado"] for t in tareas if t["id"] == tarea_id) in TERMINALES:
            break
        mientras_corria += 1
        await asyncio.sleep(intervalo)
    tarea = (await c.get(f"/tareas/{tarea_id}")).json()
    return {"duracion_hash_s": _r(tarea["duracion_s"]), "respuestas_mientras_corria": mientras_corria,
            **_resumen_latencias(latencias)}


async def medicion_2(segundos_objetivo=3.0):
    datos = {}
    async with servidor(permitir_bloqueante="si"), cliente() as c:
        iteraciones = int(await iteraciones_por_segundo(c) * segundos_objetivo)
        conexion = await ConexionCruda.abrir()
        try:
            latencias = []
            for _ in range(60):
                latencias.append((await _get_tareas(conexion))[0])
                await asyncio.sleep(0.05)
            datos["sin carga (referencia)"] = {"duracion_hash_s": None, "respuestas_mientras_corria": None,
                                               **_resumen_latencias(latencias)}
            for ejecutor in ("ninguno", "hilos", "procesos"):
                datos[f"hash con ejecutor={ejecutor}"] = await _latencias_durante_hash(
                    c, conexion, iteraciones, ejecutor)
        finally:
            await conexion.cerrar()

    filas = [[caso, d["duracion_hash_s"], d["respuestas_mientras_corria"],
              d["p50_ms"], d["p95_ms"], f"**{d['max_ms']}**"] for caso, d in datos.items()]
    md = tabla(["Caso", "Duración del hash (s)", "GET respondidos mientras corría",
                "Latencia p50 (ms)", "p95 (ms)", "Máxima (ms)"], filas)
    nota = (f"\n\nHash de {iteraciones:,} iteraciones de SHA-256; un GET /tareas?limite=10 "
            "cada 50 ms (cada GET espera la respuesta del anterior).")
    return {"iteraciones": iteraciones, **datos}, md + nota


# --- Medición 3: techo del pool -------------------------------------------------------

async def _serie(config, ks, tipo, params, detalle=True, cada=0.02):
    """Una serie de lotes de tamaño creciente sobre un mismo servidor.

    `params` puede ser un dict o una corrutina que lo calcula con el cliente
    (para calibrar el hash en el mismo servidor que se va a medir).
    """
    filas = []
    async with servidor(**config) as proceso, cliente() as c:
        if callable(params):
            params = await params(c)
        for k in ks:
            print(f"   {tipo} con {config}: k={k} ...", end=" ", flush=True)
            fila = await lote(c, k, tipo, params, detalle=detalle, cada=cada)
            fila["rss_mb"] = _r(rss_mb(proceso.pid), 1)
            print(f"{fila['total_s']} s", flush=True)
            filas.append(fila)
    return {"config": config, "params": params, "filas": filas}


async def _hash_de_medio_segundo(c):
    return {"texto": "techo", "iteraciones": int(await iteraciones_por_segundo(c) * 0.5)}


async def _serie_d():
    # Un servidor nuevo por cada k, con tantos workers como tareas: nada
    # espera en la cola. Se consulta /estadisticas cada 0,25 s porque recorre
    # todas las tareas (es O(n)) y consultarlo muy seguido cargaría al mismo
    # loop que se está midiendo.
    filas = []
    for k in (1000, 5000, 10000, 20000):
        serie = await _serie({"workers": k}, [k], "esperar", {"segundos": 1}, detalle=False, cada=0.25)
        filas += serie["filas"]
    return {"filas": filas}


SERIES_3 = ("A", "B", "B2", "C", "D")


async def medicion_3(series=SERIES_3):
    cpus = os.cpu_count() or 2
    planes = {
        "A": lambda: _serie({"workers": 8}, [1, 4, 8, 9, 16, 32, 64], "esperar", {"segundos": 1}),
        "B": lambda: _serie({"workers": 64, "procesos_cpu": 4}, [1, 2, 4, 5, 8, 16],
                            "hashear", _hash_de_medio_segundo),
        "B2": lambda: _serie({"workers": 128, "procesos_cpu": cpus},
                             sorted({1, 2, 4, cpus // 4, cpus // 2, cpus, 2 * cpus}),
                             "hashear", _hash_de_medio_segundo),
        "C": lambda: _serie({"workers": 2000}, [50, 100, 101, 200, 400, 800],
                            "descargar", {"urls": [f"{URL_LENTO}/lento?segundos=1&kb=1"]}),
        "D": _serie_d,
    }
    titulos = {
        "A": "**A. `esperar 1 s` con WORKERS=8** — el límite es la cantidad de workers.",
        "B": "**B. `hashear` (~0,5 s c/u) con PROCESOS_CPU=4 y WORKERS=64** — "
             "el límite es el pool de procesos.",
        "B2": f"**B2. `hashear` (~0,5 s c/u) con PROCESOS_CPU={cpus} (todas las CPUs)** — "
              "el límite es el hardware.",
        "C": "**C. `descargar` 1 URL que tarda 1 s, con WORKERS=2000** — el límite son las "
             "MAX_DESCARGAS=100 descargas simultáneas.",
        "D": "**D. `esperar 1 s` con WORKERS = k (nada espera en cola)** — el costo de muchas "
             "corrutinas y de recibir los pedidos.",
    }
    datos = {nombre: await planes[nombre]() for nombre in series}

    md = []
    for nombre, serie in datos.items():
        if nombre == "D":
            encabezados = ["Tareas (k)", "Envío de los POST (s)", "Tiempo total (s)",
                           "POST/s que aceptó la API", "Memoria del servidor (MB)"]
            filas = [[f["k"], f["envio_s"], f["total_s"], _r(f["k"] / f["envio_s"], 0), f["rss_mb"]]
                     for f in serie["filas"]]
        else:
            encabezados = ["Tareas a la vez (k)", "Tiempo total (s)", "Espera en cola prom. (s)",
                           "Duración prom. (s)"]
            filas = [[f["k"], f["total_s"], f["espera_prom_s"], f["duracion_prom_s"]]
                     for f in serie["filas"]]
        md += [titulos[nombre] + "\n", tabla(encabezados, filas) + "\n"]
    return datos, "\n".join(md)


# --- main ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--solo", type=int, choices=[1, 2, 3], help="correr una sola medición")
    parser.add_argument("--repeticiones", type=int, default=3, help="corridas por modo en la medición 1")
    parser.add_argument("--series", default=",".join(SERIES_3),
                        help="series de la medición 3, separadas por coma (default: todas)")
    args = parser.parse_args()
    series = [s.strip().upper() for s in args.series.split(",")]
    if not set(series) <= set(SERIES_3):
        parser.error(f"--series admite {', '.join(SERIES_3)}")

    entorno = {"fecha": datetime.now().isoformat(timespec="seconds"), "cpus": os.cpu_count(),
               "python": platform.python_version(), "plataforma": platform.platform()}
    titulos = {1: "Medición 1 — Concurrencia I/O: 20 descargas",
               2: "Medición 2 — El event loop bloqueado",
               3: "Medición 3 — Techo del pool"}
    mediciones = {1: lambda: medicion_1(args.repeticiones), 2: medicion_2,
                  3: lambda: medicion_3(series)}

    datos, secciones = {"entorno": entorno}, []
    for numero in ([args.solo] if args.solo else [1, 2, 3]):
        print(f"\n== {titulos[numero]} ...", flush=True)
        inicio = time.perf_counter()
        crudos, md = await mediciones[numero]()
        print(md, flush=True)
        print(f"({time.perf_counter() - inicio:.0f} s)", flush=True)
        datos[f"medicion_{numero}"] = crudos
        secciones.append(f"## {titulos[numero]}\n\n{md}\n")

    sufijo = f"_{args.solo}" if args.solo else ""
    encabezado = (f"# Resultados de las mediciones\n\nCorrida: {entorno['fecha']} · {entorno['cpus']} CPUs · "
                  f"Python {entorno['python']} · {entorno['plataforma']}\n\n")
    (SALIDA / f"resultados{sufijo}.md").write_text(encabezado + "\n".join(secciones), encoding="utf-8")
    (SALIDA / f"resultados{sufijo}.json").write_text(
        json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nGuardado en mediciones/resultados{sufijo}.md y .json")


if __name__ == "__main__":
    asyncio.run(main())
