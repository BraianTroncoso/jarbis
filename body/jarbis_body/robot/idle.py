"""
La vida en reposo: lo que hace el robot cuando no hace nada.

Hasta acá el robot estaba dormido o escuchando, y nada en el medio. Por eso la
única forma de que pareciera vivo era ponerlo a escuchar, que es exactamente lo
que no querés de algo que vive en la pantalla todo el día.

**Despierto** es el estado que faltaba: ojos abiertos, micrófono cerrado. La
diferencia entre encendido y atento.

## La lección de Clippy

Lo que hacía simpático al asistente de Office era que tenía vida cuando no
pasaba nada. Lo que lo hizo odiado era que **interrumpía**. Acá hay lo primero
y nada de lo segundo: el robot mira, parpadea y respira, y jamás arranca una
conversación por su cuenta. Eso sigue siendo del reflejo, que no tiene sensores
a propósito.

## Por qué esto no toca al cuerpo

Despertarse es una forma de dibujarse, no de ponerse a trabajar. El cuerpo
sigue en reposo de verdad: no hay estado nuevo en su máquina de estados, ni
campo nuevo en el archivo de estado, ni una mentira que mantener sincronizada.
El panel sigue mostrando lo que el cuerpo hace — y el cuerpo no está haciendo
nada.

## Por qué es un módulo aparte y sin tkinter

Para poder probarlo. tkinter no está en el WSL donde corre la suite, así que
todo lo que viva en `widget.py` es código que nadie verifica. Acá adentro no
hay nada de Tk: entra el reloj y dónde está el mouse, sale hacia dónde miran
los ojos y si está parpadeando.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# Qué tan cerca tiene que pasar el mouse para que abra los ojos. Es un radio
# desde el centro de la carita, generoso a propósito: si hay que apuntarle, ya
# no es "se despertó porque estabas ahí", es un botón escondido.
NEAR_PX = 170.0

# Cuánto aguanta despierto sin verte.
SLEEP_AFTER_S = 30.0

# A qué distancia el ojo ya llegó al tope. Más allá de esto mirar "más" no se
# nota, y seguir escalando haría que la mirada se pegue a los bordes.
GAZE_FULL_PX = 420.0

# Cuánto tarda la mirada en llegar adonde apunta, más o menos. Sin esto el ojo
# salta de un píxel al otro y se ve como un error de dibujo, no como una
# mirada.
GAZE_EASE_S = 0.22

# Parpadeo: cuánto dura y cada cuánto. El rango ancho es el punto — un
# parpadeo regular se lee como un metrónomo, no como algo vivo.
BLINK_S = 0.13
BLINK_EVERY = (2.4, 6.5)
DOUBLE_BLINK_CHANCE = 0.22
DOUBLE_BLINK_GAP_S = 0.18

# La miradita de reposo: cada tanto mira para otro lado y vuelve.
GLANCE_EVERY = (20.0, 40.0)
GLANCE_HOLD_S = 1.2

# La respiración: un ciclo lento del halo. Seis segundos es más o menos el
# ritmo de alguien tranquilo; más rápido parece nervioso.
BREATH_S = 6.0


@dataclass(frozen=True)
class Life:
    """Lo que hay que dibujar este cuadro."""

    awake: bool
    gaze_x: float        # -1..1, izquierda a derecha
    gaze_y: float        # -1..1, arriba a abajo
    blinking: bool
    breath: float        # 0..1, el ciclo de la respiración


class IdleLife:
    """
    Decide si está despierto y hacia dónde mira.

        vida = IdleLife()
        vida.saw_you(ahora)                       # hubo interacción
        estado = vida.update(ahora, cursor, centro)

    `cursor` y `centro` van en coordenadas de pantalla. `cursor` puede ser None
    si no se pudo leer; en ese caso la mirada sigue su cuenta y no se despierta
    por proximidad.
    """

    def __init__(
        self,
        *,
        sleep_after_s: float = SLEEP_AFTER_S,
        near_px: float = NEAR_PX,
        rng: random.Random | None = None,
    ) -> None:
        self._sleep_after = sleep_after_s
        self._near = near_px
        self._rng = rng or random.Random()

        self._last_seen = float("-inf")
        self._gaze = (0.0, 0.0)
        self._last_update: float | None = None

        self._blink_until = 0.0
        self._next_blink = 0.0
        self._pending_blink = 0.0     # el segundo de un parpadeo doble

        self._next_glance = 0.0
        self._glance_until = 0.0
        self._glance_target = (0.0, 0.0)

    # ------------------------------------------------------------------ afuera

    def saw_you(self, now: float) -> None:
        """Hubo interacción directa: un click, un arrastre, el menú."""
        self._last_seen = now

    def awake_at(self, now: float) -> bool:
        return now - self._last_seen < self._sleep_after

    # ------------------------------------------------------------------ cuadro

    def update(
        self,
        now: float,
        cursor: tuple[float, float] | None,
        center: tuple[float, float],
    ) -> Life:
        dt = 0.0 if self._last_update is None else max(0.0, now - self._last_update)
        self._last_update = now

        if cursor is not None and _distance(cursor, center) <= self._near:
            self._last_seen = now

        awake = self.awake_at(now)

        if not awake:
            # Dormido no mira ni parpadea: si lo hiciera, no se distinguiría de
            # despierto y el estado nuevo no serviría de nada.
            self._gaze = (0.0, 0.0)
            return Life(False, 0.0, 0.0, False, 0.0)

        target = self._target(now, cursor, center)
        self._gaze = _ease(self._gaze, target, dt)

        return Life(
            awake=True,
            gaze_x=self._gaze[0],
            gaze_y=self._gaze[1],
            blinking=self._blinking(now),
            breath=0.5 - 0.5 * math.cos(2 * math.pi * (now % BREATH_S) / BREATH_S),
        )

    # ------------------------------------------------------------------ mirada

    def _target(
        self,
        now: float,
        cursor: tuple[float, float] | None,
        center: tuple[float, float],
    ) -> tuple[float, float]:
        """Adónde querría mirar: al cursor si lo tiene, si no a su miradita."""
        if cursor is not None:
            dx = (cursor[0] - center[0]) / GAZE_FULL_PX
            dy = (cursor[1] - center[1]) / GAZE_FULL_PX
            return (_clamp(dx), _clamp(dy))

        # Sin cursor, la vida la pone la miradita de reposo.
        if now >= self._glance_until and now >= self._next_glance:
            self._next_glance = now + self._rng.uniform(*GLANCE_EVERY)
            self._glance_until = now + GLANCE_HOLD_S
            lado = self._rng.choice((-1.0, 1.0))
            self._glance_target = (lado * self._rng.uniform(0.5, 0.9),
                                   self._rng.uniform(-0.25, 0.25))

        if now < self._glance_until:
            return self._glance_target
        return (0.0, 0.0)

    # ---------------------------------------------------------------- parpadeo

    def _blinking(self, now: float) -> bool:
        if now < self._blink_until:
            return True

        if self._pending_blink and now >= self._pending_blink:
            self._blink_until = now + BLINK_S
            self._pending_blink = 0.0
            return True

        if now >= self._next_blink:
            self._blink_until = now + BLINK_S
            self._next_blink = now + self._rng.uniform(*BLINK_EVERY)
            if self._rng.random() < DOUBLE_BLINK_CHANCE:
                self._pending_blink = self._blink_until + DOUBLE_BLINK_GAP_S
                self._next_blink = max(self._next_blink,
                                       self._pending_blink + BLINK_S + 0.3)
            return True

        return False


# ============================================================================
# Cuentas
# ============================================================================


def _clamp(value: float, limit: float = 1.0) -> float:
    return max(-limit, min(limit, value))


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _ease(
    current: tuple[float, float], target: tuple[float, float], dt: float
) -> tuple[float, float]:
    """
    Acerca la mirada a donde apunta, por fracción del camino que falta.

    Exponencial y no lineal para que no importe a cuántos cuadros por segundo
    corra el widget: con dt grande se acerca más de una vez, con dt chico menos,
    y el tiempo total hasta llegar es el mismo. Despierto anima a 20 fps y
    dormido a 4, así que esto no es teórico.
    """
    if dt <= 0.0:
        return current
    k = 1.0 - math.exp(-dt / GAZE_EASE_S)
    return (
        current[0] + (target[0] - current[0]) * k,
        current[1] + (target[1] - current[1]) * k,
    )
