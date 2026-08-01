# Computación II - 2026

## Datos del estudiante

- **Nombre:** Maximo Navesi
- **Legajo:** 64242
- **Email:** m.navesi@alumno.um.edu.ar
- **GitHub:** [@maxinavesi1](https://github.com/maxinavesi1)

## Estructura

- `bloque_0/` - Ejercicios del bloque autónomo
- `tp1/` - Trabajo Práctico 1
- `tp2/` - Trabajo Práctico 2

## Estado

| Componente | Estado |
|------------|--------|
| Bloque 0   | Completo |
| TP1        | Completo — [monitor de procesos y threads](tp1/) |
| TP2        | Pendiente |

### TP1 — Monitor de Procesos y Threads

Monitor de sistema en tiempo real estilo `htop`, que lee `/proc` directamente
(sin `psutil`). Arquitectura multiproceso: un recolector, 7 analizadores
independientes con su propio intervalo de refresco, y una TUI en `curses` con 7
vistas alternables. Maneja SIGINT/SIGTERM/SIGHUP/SIGUSR1/SIGUSR2/SIGWINCH con
el patrón self-pipe.

```bash
cd tp1
docker compose up -d --build     # levanta los 11 procesos
docker attach tp1-monitor        # engancha el teclado a la TUI
```

Ver [tp1/README.md](tp1/README.md) para el informe completo (arquitectura,
decisiones de diseño, conceptos del curso aplicados y limitaciones) y
[tp1/dudas.md](tp1/dudas.md) para las dudas que quedaron abiertas.