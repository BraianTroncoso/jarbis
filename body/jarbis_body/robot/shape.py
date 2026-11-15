"""
La geometría del robot: una pantallita con dos ojos de LED.

Reemplaza a una versión anterior que usaba un personaje de marca redibujado en
vectores. Se descartó por pedido: demasiado personaje, poco legible sobre el
escritorio. La referencia ahora es el robot doméstico japonés —LOVOT, Romi— que
no tiene cara: tiene dos formas que cambian.

## La idea

Todo se dibuja con dos primitivas: rectángulos redondeados y círculos. No hay
curvas a mano, no hay trazos. Eso tiene una consecuencia buena: el robot se lee
igual a 80 píxeles que a 200, y cambiar una expresión es cambiar un número.

## El espacio

Todo se define sobre una pantalla de 128 × 88 y después se escala. Las
coordenadas de acá son esos píxeles base.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# La pantalla.
SCREEN_W = 128.0
SCREEN_H = 88.0
SCREEN_RADIUS = 24.0

# Los ojos. Mismo ancho siempre; lo que cambia es el alto y la posición, que es
# justamente lo que hace que dos barras parezcan una expresión.
EYE_W = 26.0
EYE_LEFT_X = 41.0
EYE_RIGHT_X = 87.0
EYE_Y = 43.0
EYE_RADIUS = 7.0

# La onda de voz, debajo de los ojos cuando habla.
WAVE_BARS = 7
WAVE_X0 = 36.0
WAVE_X1 = 92.0
WAVE_Y = 70.0
# A 15 de alto y 4 de ancho las barras se leían como puntos sueltos, no como
# una onda. El ancho de la pantalla da lugar de sobra.
WAVE_MAX_H = 22.0
WAVE_BAR_W = 5.5

# Los puntitos de pensar, arriba.
THINK_DOTS_X = (49.0, 64.0, 79.0)
THINK_DOTS_Y = 18.0
THINK_DOT_R = 3.2

# El color que la ventana vuelve transparente y click-through en Windows.
# Un magenta imposible: cada píxel de este color desaparece, así que no puede
# aparecer en ningún elemento del dibujo. Hay un test que lo verifica.
CHROMA = "#ff00fe"

# El fondo de la pantallita y su borde en reposo.
SCREEN_BG = "#0b0a0f"
SCREEN_EDGE = "#26262f"


@dataclass(frozen=True)
class EyeShape:
    """Cómo se ven los ojos en un estado."""

    height: float          # alto de cada barra
    offset_y: float = 0.0  # cuánto se corren respecto del centro
    cross: bool = False    # aspas, para el error


@dataclass(frozen=True)
class Palette:
    """Los colores de un estado."""

    led: str        # los ojos y la onda
    edge: str       # el borde de la pantalla
    glow: bool      # si el borde late
    bubble: str     # el borde del globo de diálogo


# Verde, naranja y celeste son los mismos que usa el panel web, para que las dos
# superficies digan lo mismo. El magenta es el de Dmeter (#f73d8f), el que más
# viene de la marca personal del proyecto.
DMETER = "#f73d8f"

PALETTES: dict[str, Palette] = {
    "dormido": Palette(led="#3d3d47", edge=SCREEN_EDGE, glow=False, bubble="#3b3842"),
    "escuchando": Palette(led="#2de2a6", edge="#2de2a6", glow=True, bubble="#2de2a6"),
    "pensando": Palette(led="#ffb02e", edge="#ffb02e", glow=True, bubble="#ffb02e"),
    "hablando": Palette(led="#4aa8ff", edge="#4aa8ff", glow=True, bubble="#4aa8ff"),
    "error": Palette(led=DMETER, edge=DMETER, glow=False, bubble=DMETER),
}

# El alto y la posición de los ojos por estado. Toda la expresión vive acá.
#
# Dormido: dos rayitas, como párpados cerrados.
# Escuchando: bien abiertos y atentos.
# Pensando: bajos y corridos hacia abajo — la mirada que se va para otro lado.
# Hablando: abiertos, con la onda de voz debajo.
EYES: dict[str, EyeShape] = {
    "dormido": EyeShape(height=5.0),
    "escuchando": EyeShape(height=34.0),
    "pensando": EyeShape(height=13.0, offset_y=7.0),
    "hablando": EyeShape(height=28.0, offset_y=-4.0),
    "error": EyeShape(height=26.0, cross=True),
}

# El parpadeo usa la misma forma que dormido, sea cual sea el estado.
BLINK = EyeShape(height=4.0)


def palette(state: str) -> Palette:
    """La paleta de un estado, cayendo a dormido si no se conoce."""
    return PALETTES.get(state, PALETTES["dormido"])


def eyes(state: str) -> EyeShape:
    """La forma de los ojos de un estado."""
    return EYES.get(state, EYES["dormido"])


# ============================================================================
# Primitivas
# ============================================================================


def rounded_rect(
    x0: float, y0: float, x1: float, y1: float, radius: float, steps: int = 6
) -> list[float]:
    """
    Los puntos de un rectángulo redondeado, listos para `create_polygon`.

    Tk no tiene rectángulo redondeado. La alternativa clásica es componerlo con
    arcos y rectángulos, pero eso deja costuras visibles donde se tocan las
    piezas. Un polígono con puntos calculados sobre las esquinas es una sola
    forma, sin juntas.

    `radius` se acota a la mitad del lado más corto: pedir más que eso daría una
    figura con los arcos cruzados.
    """
    radius = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    if radius == 0:
        return [x0, y0, x1, y0, x1, y1, x0, y1]

    puntos: list[float] = []
    # Las cuatro esquinas, en orden, con su centro y el ángulo donde arranca.
    esquinas = (
        (x1 - radius, y0 + radius, -90.0),   # arriba derecha
        (x1 - radius, y1 - radius, 0.0),     # abajo derecha
        (x0 + radius, y1 - radius, 90.0),    # abajo izquierda
        (x0 + radius, y0 + radius, 180.0),   # arriba izquierda
    )
    for cx, cy, inicio in esquinas:
        for i in range(steps + 1):
            angulo = math.radians(inicio + i * (90.0 / steps))
            puntos.extend([cx + radius * math.cos(angulo), cy + radius * math.sin(angulo)])
    return puntos


def eye_box(
    center_x: float, shape: EyeShape, scale: float, dx: float, dy: float
) -> tuple[float, float, float, float]:
    """La caja de un ojo, ya escalada y desplazada."""
    half_w = EYE_W / 2
    half_h = shape.height / 2
    cy = EYE_Y + shape.offset_y
    return (
        (center_x - half_w) * scale + dx,
        (cy - half_h) * scale + dy,
        (center_x + half_w) * scale + dx,
        (cy + half_h) * scale + dy,
    )


def scale_for(width_px: float) -> float:
    """Factor de escala para que la pantalla mida `width_px` de ancho."""
    return width_px / SCREEN_W


def wave_heights(phase: float, bars: int = WAVE_BARS) -> list[float]:
    """
    Los altos de la onda de voz, entre 0 y 1.

    Cada barra usa una frecuencia distinta para que la onda no se vea como un
    acordeón sincronizado. Las del medio son más altas, como en un medidor real.
    """
    alturas = []
    for i in range(bars):
        centro = 1.0 - abs(i - (bars - 1) / 2) / ((bars - 1) / 2) * 0.45
        onda = 0.5 + 0.5 * math.sin(phase * 2 * math.pi * (1.0 + i * 0.37) + i)
        alturas.append(max(0.30, centro * onda))
    return alturas


def glow_alpha(phase: float) -> float:
    """
    Cuánto brilla el borde, entre 0 y 1, latiendo.

    Curva suave en vez de lineal: un latido lineal se siente mecánico.
    """
    return 0.45 + 0.55 * (0.5 + 0.5 * math.sin(phase * 2 * math.pi))


def mix(color_a: str, color_b: str, amount: float) -> str:
    """
    Mezcla dos colores hex. `amount` 0 devuelve el primero, 1 el segundo.

    Tk no tiene transparencia por elemento, así que para que el borde "brille"
    se interpola contra el fondo en vez de bajarle el alfa.
    """
    amount = max(0.0, min(1.0, amount))

    def canales(color: str) -> tuple[int, int, int]:
        color = color.lstrip("#")
        return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]

    ra, ga, ba = canales(color_a)
    rb, gb, bb = canales(color_b)
    return "#{:02x}{:02x}{:02x}".format(
        round(ra + (rb - ra) * amount),
        round(ga + (gb - ga) * amount),
        round(ba + (bb - ba) * amount),
    )
