# Resultados de las mediciones

Corrida: 2026-10-17T17:24:36 · 20 CPUs · Python 3.12.15 · Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.41

## Medición 1 — Concurrencia I/O: 20 descargas

| Destino | Secuencial (s) | Concurrente (s) | Speedup | Suma de los tiempos individuales (s) | La más lenta (s) | Descargas OK |
|---|---|---|---|---|---|---|
| servidor lento (local) | 11.769 | 1.005 | **11.7x** | 11.768 | 0.981 | 120/120 |
| internet (20 sitios reales) | 7.199 | 1.625 | **4.4x** | 7.199 | 1.601 | 120/120 |

Mediana de 3 corridas por modo, alternadas, cada una en un servidor recién levantado (sin conexiones abiertas de la corrida anterior).

## Medición 2 — El event loop bloqueado

| Caso | Duración del hash (s) | GET respondidos mientras corría | Latencia p50 (ms) | p95 (ms) | Máxima (ms) |
|---|---|---|---|---|---|
| sin carga (referencia) | — | — | 2.9 | 3.7 | **3.9** |
| hash con ejecutor=ninguno | 2.838 | 0 | 2837.2 | 2837.2 | **2837.2** |
| hash con ejecutor=hilos | 2.901 | 47 | 11.8 | 17.5 | **18.5** |
| hash con ejecutor=procesos | 3.012 | 59 | 0.9 | 1.1 | **1.2** |

Hash de 8,635,578 iteraciones de SHA-256; un GET /tareas?limite=10 cada 50 ms (cada GET espera la respuesta del anterior).

## Medición 3 — Techo del pool

**A. `esperar 1 s` con WORKERS=8** — el límite es la cantidad de workers.

| Tareas a la vez (k) | Tiempo total (s) | Espera en cola prom. (s) | Duración prom. (s) |
|---|---|---|---|
| 1 | 1.025 | 0.0 | 1.001 |
| 4 | 1.018 | 0.001 | 1.003 |
| 8 | 1.025 | 0.001 | 1.001 |
| 9 | 2.046 | 0.112 | 1.001 |
| 16 | 2.048 | 0.503 | 1.003 |
| 32 | 4.026 | 1.504 | 1.003 |
| 64 | 8.063 | 3.507 | 1.002 |

**B. `hashear` (~0,5 s c/u) con PROCESOS_CPU=4 y WORKERS=64** — el límite es el pool de procesos.

| Tareas a la vez (k) | Tiempo total (s) | Espera en cola prom. (s) | Duración prom. (s) |
|---|---|---|---|
| 1 | 0.51 | 0.0 | 0.495 |
| 2 | 0.532 | 0.0 | 0.504 |
| 4 | 0.512 | 0.0 | 0.493 |
| 5 | 0.684 | 0.001 | 0.649 |
| 8 | 1.068 | 0.001 | 1.025 |
| 16 | 2.037 | 0.002 | 1.975 |

**B2. `hashear` (~0,5 s c/u) con PROCESOS_CPU=20 (todas las CPUs)** — el límite es el hardware.

| Tareas a la vez (k) | Tiempo total (s) | Espera en cola prom. (s) | Duración prom. (s) |
|---|---|---|---|
| 1 | 0.416 | 0.0 | 0.404 |
| 2 | 0.419 | 0.0 | 0.412 |
| 4 | 0.446 | 0.001 | 0.411 |
| 5 | 0.444 | 0.001 | 0.417 |
| 10 | 0.733 | 0.001 | 0.662 |
| 20 | 1.003 | 0.005 | 0.94 |
| 40 | 1.956 | 0.004 | 1.888 |

**C. `descargar` 1 URL que tarda 1 s, con WORKERS=2000** — el límite son las MAX_DESCARGAS=100 descargas simultáneas.

| Tareas a la vez (k) | Tiempo total (s) | Espera en cola prom. (s) | Duración prom. (s) |
|---|---|---|---|
| 50 | 1.147 | 0.009 | 1.069 |
| 100 | 1.214 | 0.01 | 1.133 |
| 101 | 2.094 | 0.01 | 1.149 |
| 200 | 2.397 | 0.01 | 1.727 |
| 400 | 4.607 | 0.009 | 2.819 |
| 800 | 9.225 | 0.011 | 5.044 |

**D. `esperar 1 s` con WORKERS = k (nada espera en cola)** — el costo de muchas corrutinas y de recibir los pedidos.

| Tareas (k) | Envío de los POST (s) | Tiempo total (s) | POST/s que aceptó la API | Memoria del servidor (MB) |
|---|---|---|---|---|
| 1000 | 0.35 | 1.419 | 2857.0 | 59.1 |
| 5000 | 1.727 | 2.832 | 2895.0 | 76.5 |
| 10000 | 3.545 | 4.692 | 2821.0 | 93.1 |
| 20000 | 7.364 | 8.579 | 2716.0 | 127.8 |

