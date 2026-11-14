"""
Estados del cuerpo de Jarbis.

Heredado de GesturePilot (state/states.py) pero recortado: los siete estados
viejos existían para el flujo de gestos (PAUSED, PREVIEW, EXECUTING con
confirmación por pulgar arriba). El flujo de voz tiene cinco y ninguno depende
de la cámara.

    DORMIDO ──wake word / hotkey──► ESCUCHANDO
       ▲                                 │
       │                            silencio
       │                                 ▼
       │                            PENSANDO ──respuesta──► HABLANDO
       │                                 │                      │
       └─────────── fin del turno ───────┴──────────────────────┘

ERROR cuelga de cualquiera y vuelve a DORMIDO solo.

Los nombres están en español a propósito: son lo que muestra la cara en pantalla
y lo que dice el archivo de estado que leen el visor y el widget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class JarbisState(str, Enum):
    """
    Estado del cuerpo. Hereda de str para que serialice directo a JSON sin
    conversión manual en el archivo de estado.
    """

    DORMIDO = "dormido"
    ESCUCHANDO = "escuchando"
    PENSANDO = "pensando"
    HABLANDO = "hablando"
    ERROR = "error"


# Qué transiciones son legales. Todo lo que no esté acá se rechaza y se loguea.
VALID_TRANSITIONS: dict[JarbisState, set[JarbisState]] = {
    JarbisState.DORMIDO: {JarbisState.ESCUCHANDO, JarbisState.ERROR},
    JarbisState.ESCUCHANDO: {JarbisState.PENSANDO, JarbisState.DORMIDO, JarbisState.ERROR},
    JarbisState.PENSANDO: {JarbisState.HABLANDO, JarbisState.DORMIDO, JarbisState.ERROR},
    # HABLANDO vuelve a ESCUCHANDO en modo conversación, sin pedir el nombre otra vez.
    JarbisState.HABLANDO: {
        JarbisState.ESCUCHANDO,
        JarbisState.DORMIDO,
        JarbisState.ERROR,
    },
    JarbisState.ERROR: {JarbisState.DORMIDO},
}

# Cuánto puede quedarse en cada estado antes de volver solo a DORMIDO.
# None = sin timeout. Esto es lo que evita que quede colgado escuchando para
# siempre si el VAD no cierra el turno.
STATE_TIMEOUTS_S: dict[JarbisState, float | None] = {
    JarbisState.DORMIDO: None,
    JarbisState.ESCUCHANDO: 30.0,
    JarbisState.PENSANDO: 180.0,  # el cerebro puede tardar en una tarea larga
    JarbisState.HABLANDO: 120.0,
    JarbisState.ERROR: 5.0,
}

DESCRIPTIONS: dict[JarbisState, str] = {
    JarbisState.DORMIDO: "Esperando que me llames",
    JarbisState.ESCUCHANDO: "Te escucho",
    JarbisState.PENSANDO: "Pensando",
    JarbisState.HABLANDO: "Hablando",
    JarbisState.ERROR: "Algo se rompió",
}

# Para la cara. Hex, no BGR: el consumidor ahora es un navegador, no OpenCV.
COLORS: dict[JarbisState, str] = {
    JarbisState.DORMIDO: "#4a4a55",
    JarbisState.ESCUCHANDO: "#2de2a6",
    JarbisState.PENSANDO: "#ffb02e",
    JarbisState.HABLANDO: "#4aa8ff",
    JarbisState.ERROR: "#ff2d75",
}


@dataclass
class StateContext:
    """Estado actual más lo que pasó alrededor."""

    state: JarbisState
    entered_at: datetime = field(default_factory=datetime.now)
    previous_state: JarbisState | None = None
    error_message: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def seconds_in_state(self) -> float:
        return (datetime.now() - self.entered_at).total_seconds()

    @property
    def description(self) -> str:
        return DESCRIPTIONS[self.state]

    @property
    def color(self) -> str:
        return COLORS[self.state]

    def to_dict(self) -> dict[str, Any]:
        """Forma serializable, la que termina en el archivo de estado."""
        return {
            "state": self.state.value,
            "description": self.description,
            "color": self.color,
            "entered_at": self.entered_at.isoformat(),
            "seconds_in_state": round(self.seconds_in_state, 2),
            "previous_state": self.previous_state.value if self.previous_state else None,
            "error_message": self.error_message,
            "data": self.data,
        }
