"""
Máquina de estados del cuerpo, segura entre threads.

Heredada de GesturePilot (state/machine.py) con el bug 2 del plan arreglado.

El original agendaba los timeouts con `asyncio.get_running_loop().call_later()`
y, cuando no había loop corriendo, hacía:

    logger.debug("No event loop, timeout not set")

...y seguía como si nada. Como todo el orquestador maneja threads planos
(`threading.Thread`), nunca hubo un loop, así que **ningún timeout disparó jamás**
en la app real. Si el VAD no cerraba el turno, quedaba escuchando para siempre.

Acá los timeouts son `threading.Timer`, que no necesita loop. Y el estado está
protegido por un `RLock` porque lo tocan tres threads distintos: el callback de
audio, el del wake word, y el del puente.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from loguru import logger

from jarbis_body.state.states import (
    STATE_TIMEOUTS_S,
    VALID_TRANSITIONS,
    JarbisState,
    StateContext,
)

StateChangeCallback = Callable[[JarbisState, JarbisState, StateContext], None]


class StateMachine:
    """
    Dueña del estado del cuerpo.

    Push, no pull: te suscribís con `on_change()` y te avisa. Nadie pregunta
    "¿en qué estado estás?" en un loop — ese era el pecado del diseño viejo.
    """

    def __init__(self, timeouts: dict[JarbisState, float | None] | None = None) -> None:
        self._lock = threading.RLock()
        self._timeouts = timeouts or dict(STATE_TIMEOUTS_S)
        self._context = StateContext(state=JarbisState.DORMIDO)
        self._callbacks: list[StateChangeCallback] = []
        self._timer: threading.Timer | None = None
        self._history: list[tuple[datetime, JarbisState, JarbisState]] = []

    # ------------------------------------------------------------ suscripción

    def on_change(self, callback: StateChangeCallback) -> None:
        """Registra un observador de cambios de estado."""
        with self._lock:
            self._callbacks.append(callback)

    # ----------------------------------------------------------------- lectura

    @property
    def state(self) -> JarbisState:
        with self._lock:
            return self._context.state

    @property
    def context(self) -> StateContext:
        with self._lock:
            return self._context

    def can_go_to(self, target: JarbisState) -> bool:
        with self._lock:
            return target in VALID_TRANSITIONS.get(self._context.state, set())

    # ---------------------------------------------------------------- escritura

    def transition_to(
        self,
        target: JarbisState,
        *,
        error_message: str | None = None,
        data: dict[str, Any] | None = None,
        force: bool = False,
    ) -> bool:
        """
        Mueve el estado. Devuelve False y loguea si la transición no es legal.

        `force=True` es solo para el apagado y para ERROR, que tienen que poder
        salir desde cualquier parte.
        """
        with self._lock:
            current = self._context.state

            if target == current:
                # Re-entrar al mismo estado reinicia el timeout, que es lo que
                # queremos en modo conversación (ESCUCHANDO → ESCUCHANDO).
                self._arm_timeout(target)
                return True

            if not force and target not in VALID_TRANSITIONS.get(current, set()):
                logger.warning(f"Transición ilegal: {current.value} → {target.value}")
                return False

            new_context = StateContext(
                state=target,
                previous_state=current,
                error_message=error_message,
                data=data or {},
            )
            self._context = new_context
            self._history.append((new_context.entered_at, current, target))
            self._arm_timeout(target)
            callbacks = list(self._callbacks)

        logger.info(f"Estado: {current.value} → {target.value}")

        # Los callbacks corren FUERA del lock: si uno se cuelga escribiendo a
        # disco o al socket, no congela la máquina entera.
        for callback in callbacks:
            try:
                callback(current, target, new_context)
            except Exception as exc:
                logger.error(f"Callback de estado falló: {exc}")

        return True

    def set_error(self, message: str) -> bool:
        """Lleva a ERROR, que auto-vuelve a DORMIDO por timeout."""
        return self.transition_to(JarbisState.ERROR, error_message=message, force=True)

    def reset(self) -> bool:
        """Vuelve a DORMIDO desde donde sea."""
        return self.transition_to(JarbisState.DORMIDO, force=True)

    # ----------------------------------------------------------------- timeouts

    def _arm_timeout(self, state: JarbisState) -> None:
        """
        Agenda la vuelta automática a DORMIDO. Se llama siempre con el lock
        tomado. bug 2: threading.Timer, no asyncio.
        """
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

        seconds = self._timeouts.get(state)
        if seconds is None:
            return

        timer = threading.Timer(seconds, self._on_timeout, args=(state,))
        timer.name = f"JarbisTimeout-{state.value}"
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _on_timeout(self, expected: JarbisState) -> None:
        """Dispara cuando un estado se quedó demasiado tiempo."""
        with self._lock:
            # Si ya nos movimos, este timer es viejo: ignorar.
            if self._context.state != expected:
                return
        logger.warning(f"Timeout en {expected.value} — volviendo a dormido")
        self.transition_to(JarbisState.DORMIDO, force=True)

    def shutdown(self) -> None:
        """Cancela cualquier timer pendiente para que el proceso pueda cerrar."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    # ------------------------------------------------------------ introspección

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            recent = self._history[-limit:]
        return [
            {"at": at.isoformat(), "from": src.value, "to": dst.value}
            for at, src, dst in recent
        ]

    def to_dict(self) -> dict[str, Any]:
        return self.context.to_dict()
