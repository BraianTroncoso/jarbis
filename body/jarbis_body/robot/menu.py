"""
El menú del robot, dibujado a mano.

El menú nativo de tkinter (`tk.Menu`) hereda el del sistema: esquinas
cuadradas, tipografía impuesta por Windows, sin animación, y lo único que se
puede estilar son los colores. Al lado de una pantallita redondeada con ojos de
LED, desentona.

Este es un `Toplevel` sin bordes con un Canvas adentro: mismo oscuro que la
cara, esquinas redondeadas, el ítem bajo el mouse se ilumina, y aparece con un
fundido corto.

## Lo que hay que cuidar en un menú propio

Un menú del sistema se cierra solo cuando hacés click afuera o apretás Escape,
y nadie lo programa. Acá hay que hacerlo a mano, y si se olvida queda una
ventana flotando que no se va — peor que el menú feo. Están cubiertos los tres
casos: click afuera, Escape, y perder el foco.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass

from jarbis_body.robot import shape

# Medidas. Pensadas para que el menú se sienta de la misma familia que la
# pantallita: mismo radio proporcional, mismo fondo.
WIDTH = 208
ITEM_H = 36
SEPARATOR_H = 13
PAD_Y = 10
PAD_X = 18
RADIUS = 14

BG = "#15151d"
EDGE = "#2b2b36"
TEXT = "#e8e8f0"
TEXT_DIM = "#8a8a9c"
HOVER = "#22222c"
ACCENT = "#2de2a6"

FADE_STEPS = 6
FADE_MS = 14


@dataclass
class Item:
    """Una línea del menú. Sin `action` es un separador."""

    label: str = ""
    action: Callable[[], None] | None = None
    toggle: bool | None = None       # si no es None, dibuja un interruptor
    accent: bool = False             # resaltado, para la acción principal

    @property
    def is_separator(self) -> bool:
        return self.action is None and self.toggle is None and not self.label


class RobotMenu:
    """
    El menú contextual.

        menu = RobotMenu(parent)
        menu.show(x, y, [Item("Hablar", callback), Item(), Item("Salir", quit)])
    """

    def __init__(self, parent: tk.Misc) -> None:
        self._parent = parent
        self._win: tk.Toplevel | None = None
        self._canvas: tk.Canvas | None = None
        self._items: list[Item] = []
        self._rows: list[tuple[float, float, Item]] = []
        self._hover: int | None = None
        self._height = 0

    # ------------------------------------------------------------------ abrir

    def show(self, x: int, y: int, items: list[Item]) -> None:
        """Abre el menú con su esquina superior izquierda cerca de `(x, y)`."""
        self.close()
        self._items = items

        alto = PAD_Y * 2 + sum(
            SEPARATOR_H if it.is_separator else ITEM_H for it in items
        )

        win = tk.Toplevel(self._parent)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            # Para que las esquinas redondeadas no tengan un cuadrado detrás.
            win.attributes("-transparentcolor", shape.CHROMA)
        except tk.TclError:
            pass
        win.attributes("-alpha", 0.0)

        # Que no se salga de la pantalla.
        pantalla_w = win.winfo_screenwidth()
        pantalla_h = win.winfo_screenheight()
        x = min(max(4, x), pantalla_w - WIDTH - 4)
        y = min(max(4, y), pantalla_h - alto - 4)
        win.geometry(f"{WIDTH}x{alto}+{x}+{y}")

        canvas = tk.Canvas(
            win, width=WIDTH, height=alto, bg=shape.CHROMA,
            highlightthickness=0, bd=0,
        )
        canvas.pack()

        self._win = win
        self._canvas = canvas
        self._height = alto
        self._layout(alto)
        self._draw()

        canvas.bind("<Motion>", self._on_motion)
        canvas.bind("<Button-1>", self._on_click)
        canvas.bind("<Leave>", lambda _e: self._set_hover(None))
        # Los tres caminos de salida de un menú. Si falta alguno, queda una
        # ventana flotando que no se va.
        win.bind("<Escape>", lambda _e: self.close())
        win.bind("<Button-3>", lambda _e: self.close())

        win.focus_force()
        # El cierre por perder el foco se engancha DESPUÉS de tomarlo: atado
        # antes, el propio `focus_force()` dispara el evento y el menú se
        # cerraba solo antes de que llegaras a verlo.
        win.after(220, lambda: win.bind("<FocusOut>", lambda _e: self.close()))
        self._fade_in(1)

    def _layout(self, alto: int) -> None:
        """Calcula el rango vertical de cada ítem, una sola vez."""
        self._rows = []
        y = PAD_Y
        for item in self._items:
            h = SEPARATOR_H if item.is_separator else ITEM_H
            self._rows.append((y, y + h, item))
            y += h

    def _fade_in(self, paso: int) -> None:
        if self._win is None:
            return
        self._win.attributes("-alpha", paso / FADE_STEPS)
        if paso < FADE_STEPS:
            self._win.after(FADE_MS, self._fade_in, paso + 1)

    # ----------------------------------------------------------------- dibujar

    def _draw(self) -> None:
        canvas = self._canvas
        if canvas is None or self._win is None:
            return
        canvas.delete("all")

        # El alto se guarda, no se le pregunta a la ventana.
        #
        # `geometry()` devuelve "1x1+0+0" hasta que el gestor de ventanas aplica
        # el tamaño pedido, y acá se dibuja antes de eso: el panel salía de un
        # píxel de alto y el menú se veía roto. La primera vez a veces zafaba por
        # una cuestión de tiempos, que es lo peor que puede pasar con un bug así.
        alto = self._height
        canvas.create_polygon(
            shape.rounded_rect(0.5, 0.5, WIDTH - 0.5, alto - 0.5, RADIUS),
            fill=BG, outline=EDGE, width=1,
        )

        for indice, (y0, y1, item) in enumerate(self._rows):
            if item.is_separator:
                medio = (y0 + y1) / 2
                canvas.create_line(
                    PAD_X, medio, WIDTH - PAD_X, medio, fill=EDGE, width=1
                )
                continue

            if indice == self._hover:
                canvas.create_polygon(
                    shape.rounded_rect(5, y0 + 1, WIDTH - 5, y1 - 1, 9),
                    fill=HOVER, outline="",
                )

            color = TEXT
            if item.accent:
                color = ACCENT
            elif item.action is None and item.toggle is None:
                color = TEXT_DIM

            canvas.create_text(
                PAD_X, (y0 + y1) / 2, text=item.label, anchor="w",
                fill=color, font=("Segoe UI", 10),
            )

            if item.toggle is not None:
                self._draw_toggle(canvas, WIDTH - PAD_X - 26, (y0 + y1) / 2, item.toggle)

    def _draw_toggle(self, canvas: tk.Canvas, x: float, y: float, on: bool) -> None:
        """Un interruptor chico, del mismo lenguaje que los del panel web."""
        w, h = 26.0, 15.0
        canvas.create_polygon(
            shape.rounded_rect(x, y - h / 2, x + w, y + h / 2, h / 2),
            fill=ACCENT if on else "#2b2b36", outline="",
        )
        r = h / 2 - 3
        cx = x + w - h / 2 if on else x + h / 2
        canvas.create_oval(
            cx - r, y - r, cx + r, y + r,
            fill=BG if on else "#55555f", outline="",
        )

    # ------------------------------------------------------------- interacción

    def _row_at(self, y: float) -> int | None:
        for indice, (y0, y1, item) in enumerate(self._rows):
            if y0 <= y < y1:
                return None if item.is_separator or item.action is None else indice
        return None

    def _set_hover(self, indice: int | None) -> None:
        if indice == self._hover:
            return
        self._hover = indice
        self._draw()

    def _on_motion(self, event: tk.Event) -> None:
        self._set_hover(self._row_at(event.y))

    def _on_click(self, event: tk.Event) -> None:
        indice = self._row_at(event.y)
        if indice is None:
            return
        accion = self._rows[indice][2].action
        # Se cierra ANTES de ejecutar: si la acción abre un navegador o tarda,
        # el menú no puede quedar colgado en pantalla mientras tanto.
        self.close()
        if accion is not None:
            accion()

    # ------------------------------------------------------------------ cerrar

    def close(self) -> None:
        self._hover = None
        if self._win is None:
            return
        try:
            self._win.destroy()
        except tk.TclError:
            pass
        self._win = None
        self._canvas = None

    @property
    def is_open(self) -> bool:
        return self._win is not None
