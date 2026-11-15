"""
Dibuja el robot sobre un Canvas de Tk.

Vive separado del widget para que la hoja de contacto
(`scripts/preview_robot.py`) dibuje con el mismo código. Una hoja que pudiera
divergir de lo que ves en pantalla no serviría para revisar nada — y revisar
mirando es lo único que funciona con algo visual.

Orden de dibujo, de atrás hacia adelante: el halo, la pantalla, el borde, los
ojos, y lo que cuelgue del estado (la onda o los puntitos).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jarbis_body.robot import shape


@dataclass
class Anim:
    """Lo que cambia cuadro a cuadro. Todo opcional: por default, en reposo."""

    phase: float = 0.0        # 0..1, para el latido, la onda y los puntitos
    blinking: bool = False    # parpadeo momentáneo
    level: float = 0.0        # 0..1, cuánta voz hay ahora


def draw_robot(
    canvas: Any,
    state: str,
    dx: float,
    dy: float,
    scale: float,
    anim: Anim | None = None,
) -> None:
    """
    Dibuja el robot con su esquina superior izquierda en `(dx, dy)`.

    No borra el canvas: eso es de quien llama, porque la hoja de contacto dibuja
    varios sobre el mismo.
    """
    anim = anim or Anim()
    pal = shape.palette(state)

    _draw_glow(canvas, pal, dx, dy, scale, anim)
    _draw_screen(canvas, pal, dx, dy, scale, anim)
    _draw_eyes(canvas, state, pal, dx, dy, scale, anim)

    if state == "hablando":
        _draw_wave(canvas, pal, dx, dy, scale, anim)
    elif state == "pensando":
        _draw_think_dots(canvas, pal, dx, dy, scale, anim)


# ============================================================================
# Las capas
# ============================================================================


def _draw_glow(canvas, pal, dx, dy, scale, anim) -> None:
    """
    El halo de afuera: la pantalla un poco más grande, en un color apagado.

    Tk no tiene opacidad por elemento, así que el brillo se consigue
    interpolando el color del estado contra el fondo en vez de bajarle el alfa.
    """
    if not pal.glow:
        return

    extra = 5.0 * scale
    intensidad = shape.glow_alpha(anim.phase)
    color = shape.mix(shape.SCREEN_BG, pal.edge, 0.22 * intensidad)

    canvas.create_polygon(
        shape.rounded_rect(
            dx - extra, dy - extra,
            dx + shape.SCREEN_W * scale + extra,
            dy + shape.SCREEN_H * scale + extra,
            (shape.SCREEN_RADIUS + 4) * scale,
        ),
        fill=color, outline="",
    )


def _draw_screen(canvas, pal, dx, dy, scale, anim) -> None:
    """La pantallita: fondo oscuro y borde del color del estado."""
    puntos = shape.rounded_rect(
        dx, dy,
        dx + shape.SCREEN_W * scale,
        dy + shape.SCREEN_H * scale,
        shape.SCREEN_RADIUS * scale,
    )

    borde = pal.edge
    if pal.glow:
        # El borde late junto con el halo, pero más sutil.
        borde = shape.mix(shape.SCREEN_EDGE, pal.edge, 0.55 + 0.45 * shape.glow_alpha(anim.phase))

    canvas.create_polygon(
        puntos, fill=shape.SCREEN_BG, outline=borde, width=max(1.0, 1.6 * scale)
    )


def _draw_eyes(canvas, state, pal, dx, dy, scale, anim) -> None:
    """Los dos LED. Toda la expresión está acá."""
    forma = shape.BLINK if anim.blinking else shape.eyes(state)

    if forma.cross:
        _draw_cross_eyes(canvas, pal, dx, dy, scale, forma)
        return

    radio = min(shape.EYE_RADIUS, forma.height / 2) * scale
    for centro in (shape.EYE_LEFT_X, shape.EYE_RIGHT_X):
        x0, y0, x1, y1 = shape.eye_box(centro, forma, scale, dx, dy)
        canvas.create_polygon(
            shape.rounded_rect(x0, y0, x1, y1, radio),
            fill=pal.led, outline="",
        )


def _draw_cross_eyes(canvas, pal, dx, dy, scale, forma) -> None:
    """Aspas, para el error. Dos barras cruzadas por ojo."""
    grosor = max(2.0, 4.0 * scale)
    for centro in (shape.EYE_LEFT_X, shape.EYE_RIGHT_X):
        x0, y0, x1, y1 = shape.eye_box(centro, forma, scale, dx, dy)
        canvas.create_line(x0, y0, x1, y1, fill=pal.led, width=grosor, capstyle="round")
        canvas.create_line(x0, y1, x1, y0, fill=pal.led, width=grosor, capstyle="round")


def _draw_wave(canvas, pal, dx, dy, scale, anim) -> None:
    """
    La onda de voz, debajo de los ojos.

    El nivel real del audio no llega hasta acá —sería acoplar el dibujo a la
    captura— así que la onda es sintética y `anim.level` solo la modula. Lo que
    comunica es "está hablando", no cuánto.
    """
    alturas = shape.wave_heights(anim.phase)
    paso = (shape.WAVE_X1 - shape.WAVE_X0) / (len(alturas) - 1)
    ancho = shape.WAVE_BAR_W / 2
    escala_nivel = 0.55 + 0.45 * max(0.0, min(1.0, anim.level or 1.0))

    for i, altura in enumerate(alturas):
        x = shape.WAVE_X0 + i * paso
        alto = shape.WAVE_MAX_H * altura * escala_nivel
        x0 = (x - ancho) * scale + dx
        x1 = (x + ancho) * scale + dx
        y0 = (shape.WAVE_Y - alto / 2) * scale + dy
        y1 = (shape.WAVE_Y + alto / 2) * scale + dy
        canvas.create_polygon(
            shape.rounded_rect(x0, y0, x1, y1, ancho * scale),
            fill=pal.led, outline="",
        )


def _draw_think_dots(canvas, pal, dx, dy, scale, anim) -> None:
    """Tres puntitos arriba, encendiéndose en secuencia."""
    import math

    for i, px in enumerate(shape.THINK_DOTS_X):
        brillo = 0.5 + 0.5 * math.sin(anim.phase * 2 * math.pi - i * 1.2)
        if brillo < 0.4:
            continue
        x = px * scale + dx
        y = shape.THINK_DOTS_Y * scale + dy
        r = shape.THINK_DOT_R * scale * (0.7 + 0.5 * brillo)
        canvas.create_oval(
            x - r, y - r, x + r, y + r,
            fill=shape.mix(shape.SCREEN_BG, pal.led, brillo), outline="",
        )
