"""
Hoja de contacto del robot: los cinco estados, uno al lado del otro.

    .venv-win\\Scripts\\python.exe scripts\\preview_robot.py --shot salida.png
    .venv-win\\Scripts\\python.exe scripts\\preview_robot.py --en --menu --shot hero.png

Existe porque el robot es un artefacto visual y revisarlo en el escritorio, de a
un estado por vez esperando a que el cuerpo cambie, es inviable.

Llama al MISMO render que el widget real: una hoja que pudiera divergir de lo
que ves en pantalla no serviría para revisar nada. Por lo mismo `--menu` abre el
menú de verdad y lo fotografía, en vez de redibujar una imitación.
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body.robot import render, shape  # noqa: E402
from jarbis_body.robot.menu import Item, RobotMenu  # noqa: E402

ESTADOS = ["dormido", "escuchando", "pensando", "hablando", "error"]

# Los estados se llaman en español adentro. Las imágenes del README, que está en
# inglés, necesitan la traducción — pero sólo del rótulo: el render sigue
# recibiendo el nombre real, así la hoja no puede divergir del widget.
EN = {
    "dormido": "idle",
    "escuchando": "listening",
    "pensando": "thinking",
    "hablando": "speaking",
    "error": "error",
}


def capturar(x: int, y: int, w: int, h: int, destino: str) -> None:
    """Fotografía un rectángulo de la pantalla real."""
    import subprocess

    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         "Add-Type -AssemblyName System.Drawing;"
         f"$b=New-Object System.Drawing.Bitmap({w},{h});"
         "$g=[System.Drawing.Graphics]::FromImage($b);"
         f"$g.CopyFromScreen({x},{y},0,0,$b.Size);"
         f"$b.Save('{destino}',[System.Drawing.Imaging.ImageFormat]::Png);"
         "$g.Dispose();$b.Dispose()"],
        capture_output=True, timeout=30,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=150)
    parser.add_argument("--shot", default=None, help="guardar PNG y salir")
    parser.add_argument("--light", action="store_true",
                        help="fondo claro, para ver cómo se apoya")
    parser.add_argument("--en", action="store_true",
                        help="rótulos en inglés, para las imágenes del README")
    parser.add_argument("--menu", action="store_true",
                        help="un solo robot con el menú abierto al lado")
    parser.add_argument("--phase", type=float, default=0.3)
    args = parser.parse_args()

    rotulo = (lambda e: EN[e]) if args.en else (lambda e: e)
    estados = ["escuchando"] if args.menu else ESTADOS

    scale = shape.scale_for(args.width)
    ancho = shape.SCREEN_W * scale
    alto = shape.SCREEN_H * scale
    gap, pad = 30, 30
    w = int(pad * 2 + len(estados) * ancho + (len(estados) - 1) * gap)
    h = int(pad * 2 + alto + 34)
    if args.menu:
        # Hay que dejarle lugar al menú, que es una ventana aparte apoyada a la
        # derecha del robot.
        w += 208 + gap
        h = max(h, int(pad * 2 + 5 * 36 + 2 * 13 + 20))

    fondo = "#d8d4cc" if args.light else "#1a1a22"
    root = tk.Tk()
    root.title(f"robot · {args.width}px")
    root.configure(bg=fondo)
    canvas = tk.Canvas(root, width=w, height=h, bg=fondo, highlightthickness=0)
    canvas.pack()

    for i, estado in enumerate(estados):
        dx = pad + i * (ancho + gap)
        render.draw_robot(canvas, estado, dx, pad, scale,
                          render.Anim(phase=args.phase, level=0.8))
        if not args.menu:
            canvas.create_text(dx + ancho / 2, pad + alto + 18, text=rotulo(estado),
                               fill="#8a8a9c" if not args.light else "#6b6860",
                               font=("Segoe UI", 9))

    root.update()
    menu = None
    if args.menu:
        menu = RobotMenu(root)
        menu.show(
            root.winfo_rootx() + int(pad + ancho + gap),
            root.winfo_rooty() + pad,
            [
                Item("Hablar", lambda: None, accent=True),
                Item(),
                Item("Abrir el panel", lambda: None),
                Item("Silenciar", lambda: None, toggle=False),
                Item(),
                Item("Cerrar Jarbis", lambda: None),
            ],
        )
        root.update()
        # El menú entra con un fundido de ~100 ms; sacarle la foto antes lo
        # dejaría semitransparente.
        root.after(400, root.quit)
        root.mainloop()

    if args.shot:
        root.update()
        capturar(root.winfo_rootx(), root.winfo_rooty(), w, h, args.shot)
        if menu is not None:
            menu.close()
        root.destroy()
        print(args.shot)
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
