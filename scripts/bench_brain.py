"""
Cuánto tarda en pensar, por motor.

    python3 scripts/bench_brain.py

Mide los dos motores con las mismas frases y la misma sesión. Existe porque el
cambio al Agent SDK se justificó con un número —un `claude -p` trivial tardaba
6,9 segundos antes de pensar nada— y una optimización sin medición después es
una opinión.

OJO: gasta tokens de verdad. Las frases son cortas y el modelo es haiku, pero
no es gratis.

Deja la tabla en `docs/BENCH-BRAIN.md`.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "brain"))

from engine import SdkEngine, SubprocessEngine  # noqa: E402

TWIN = Path.home() / "dev-own" / "twin"

# Cortas y realistas. Todas caen del lado de haiku según las reglas de escalado,
# así que se mide el transporte, no el modelo.
FRASES = [
    "Decí solamente: ok",
    "En una oración, qué es MyJarbis",
    "Y cuál fue mi pregunta anterior",
]


async def medir(engine, etiqueta: str) -> dict:
    print(f"\n--- {etiqueta} ---")
    t0 = time.perf_counter()
    arrancado = await engine.start()
    arranque = time.perf_counter() - t0
    if not arrancado:
        print("  no arrancó")
        return {"etiqueta": etiqueta, "arranque": None, "turnos": []}
    print(f"  arranque: {arranque:.1f}s")

    turnos = []
    for frase in FRASES:
        t = time.perf_counter()
        respuesta = await engine.think(frase)
        dt = time.perf_counter() - t
        turnos.append(dt)
        print(f"  {dt:6.1f}s  {frase[:36]:<36} → {respuesta[:52]!r}")

    await engine.stop()
    return {"etiqueta": etiqueta, "arranque": arranque, "turnos": turnos}


async def main() -> int:
    print("Midiendo los dos motores. Gasta tokens (haiku, frases cortas).")

    resultados = []
    # El SDK primero: si el subprocess corriera antes, su sesión quedaría en
    # caché y le regalaría ventaja al segundo.
    resultados.append(await medir(
        SdkEngine(TWIN, model="haiku", timeout=180.0), "Agent SDK (sesión viva)"))
    resultados.append(await medir(
        SubprocessEngine(TWIN, model="haiku", timeout=180.0), "subprocess (claude -p)"))

    lineas = [
        "# Latencia del cerebro, por motor",
        "",
        "Hardware: i7-1255U, sin GPU. Modelo haiku, frases cortas que no escalan.",
        "El arranque del SDK se paga UNA vez; el del subprocess, en cada turno.",
        "",
        "| motor | arranque | turno 1 | turno 2 | turno 3 | promedio por turno |",
        "|---|---|---|---|---|---|",
    ]
    for r in resultados:
        if r["arranque"] is None:
            lineas.append(f"| {r['etiqueta']} | no arrancó | — | — | — | — |")
            continue
        t = r["turnos"]
        prom = sum(t) / len(t) if t else 0
        celdas = " | ".join(f"{x:.1f}s" for x in t)
        lineas.append(
            f"| {r['etiqueta']} | {r['arranque']:.1f}s | {celdas} | **{prom:.1f}s** |"
        )

    validos = [r for r in resultados if r["arranque"] is not None and r["turnos"]]
    if len(validos) == 2:
        a = sum(validos[0]["turnos"]) / len(validos[0]["turnos"])
        b = sum(validos[1]["turnos"]) / len(validos[1]["turnos"])
        lineas += ["", f"Diferencia por turno: **{b - a:.1f}s** a favor del SDK "
                       f"({b / a:.1f}x más rápido)."]

    out = REPO / "docs" / "BENCH-BRAIN.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    print()
    print("\n".join(lineas[5:]))
    print(f"\nReporte en {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
