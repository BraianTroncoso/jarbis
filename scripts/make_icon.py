#!/usr/bin/env python3
"""
El ícono de Jarbis: la misma cara, en un .ico.

    python3 scripts/make_icon.py

Hace falta porque cerrar Jarbis ahora lo cierra de verdad, y la forma de
volver es un acceso en el escritorio. Un acceso directo con el ícono genérico
de Windows, en un escritorio lleno, no se encuentra — y entonces la puerta de
vuelta existe pero no se usa.

Las medidas salen de `jarbis_body.robot.shape`, el mismo módulo que dibuja el
robot en pantalla. Un ícono con sus propias constantes se vería parecido hoy y
distinto en tres cambios.

Un .ico moderno es un índice y una lista de PNG embebidos; no hace falta
Pillow, alcanza con cairosvg para rasterizar y 22 bytes de encabezado.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "body"))

try:
    import cairosvg
except ImportError:
    cairosvg = None

from jarbis_body.robot import shape  # noqa: E402

OUT = REPO / "assets" / "jarbis.ico"

# Windows pide estos tamaños. El 256 es el que se ve en el escritorio con
# íconos grandes; el 16 es el de la barra de tareas.
SIZES = (16, 24, 32, 48, 64, 128, 256)

# El estado que se dibuja. "escuchando" es el único que se lee de un vistazo:
# ojos bien abiertos y el verde que el proyecto usa de acento.
STATE = "escuchando"

# Margen alrededor de la pantalla, en proporción al lado del ícono. Sin él los
# bordes redondeados quedan pegados al recorte y la figura se lee cuadrada.
MARGIN = 0.06


def face_svg(side: int) -> str:
    """La cara, centrada en un cuadrado de `side` píxeles."""
    pal = shape.palette(STATE)
    forma = shape.eyes(STATE)

    usable = side * (1 - MARGIN * 2)
    scale = usable / shape.SCREEN_W
    w = shape.SCREEN_W * scale
    h = shape.SCREEN_H * scale
    dx = (side - w) / 2
    dy = (side - h) / 2

    # Un trazo que no escala con el ícono desaparece en 16 px y es un marco
    # grueso en 256.
    stroke = max(1.0, side * 0.012)

    pantalla = " ".join(
        f"{x:.2f},{y:.2f}"
        for x, y in zip(
            *[iter(shape.rounded_rect(
                dx, dy, dx + w, dy + h, shape.SCREEN_RADIUS * scale, steps=10,
            ))] * 2
        )
    )

    partes = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{side}" '
        f'height="{side}" viewBox="0 0 {side} {side}">',
        f'<polygon points="{pantalla}" fill="{shape.SCREEN_BG}" '
        f'stroke="{pal.edge}" stroke-width="{stroke:.2f}" '
        f'stroke-linejoin="round"/>',
    ]

    for centro in (shape.EYE_LEFT_X, shape.EYE_RIGHT_X):
        x0, y0, x1, y1 = shape.eye_box(centro, forma, scale, dx, dy)
        partes.append(
            f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" '
            f'height="{y1 - y0:.2f}" rx="{shape.EYE_RADIUS * scale:.2f}" '
            f'fill="{pal.led}"/>'
        )

    partes.append("</svg>")
    return "".join(partes)


def ico(images: list[tuple[int, bytes]]) -> bytes:
    """Empaqueta PNGs en un .ico.

    El formato es un índice de 6 bytes, una entrada de 16 por imagen y después
    los datos. El 0 en el campo de tamaño NO es un error: un lado de 256 no
    entra en un byte y se codifica justamente así.
    """
    cabecera = struct.pack("<HHH", 0, 1, len(images))
    offset = len(cabecera) + 16 * len(images)

    entradas, datos = [], []
    for side, png in images:
        entradas.append(struct.pack(
            "<BBBBHHII",
            side if side < 256 else 0,
            side if side < 256 else 0,
            0,              # paleta: ninguna
            0,              # reservado
            1,              # planos
            32,             # bits por píxel
            len(png),
            offset,
        ))
        datos.append(png)
        offset += len(png)

    return cabecera + b"".join(entradas) + b"".join(datos)


def main() -> int:
    if cairosvg is None:
        print("falta cairosvg:  pip install cairosvg", file=sys.stderr)
        return 1

    imagenes = []
    for side in SIZES:
        png = cairosvg.svg2png(
            bytestring=face_svg(side).encode("utf-8"),
            output_width=side, output_height=side,
        )
        imagenes.append((side, png))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(ico(imagenes))
    print(f"✓ {OUT.relative_to(REPO)}  ({len(SIZES)} tamaños, "
          f"{OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
