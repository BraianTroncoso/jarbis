"""
Publicación del estado hacia la cara.

Copia el patrón que ya funciona en `claude-spirit-box`: un solo JSON en disco que
el visor lee por polling. Es deliberadamente tonto — sin socket, sin suscripción,
sin orden de arranque. El visor puede abrirse, cerrarse y recargarse sin que el
cuerpo se entere, y si el cuerpo no está corriendo el visor muestra el último
estado conocido en vez de romperse.

Escritura atómica: se escribe a un `.tmp` al lado y se hace `os.replace()`, que
es atómico en NTFS. Sin eso, el visor eventualmente lee un JSON cortado a mitad
(pasa seguro, no es teórico: el polling es cada 300 ms).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from jarbis_body.config import settings
from jarbis_body.state.states import COLORS, DESCRIPTIONS, JarbisState


class StateWriter:
    """
    Mantiene `jarbis-state.json` al día.

    Un solo escritor por proceso. El lock serializa las escrituras porque el
    estado lo tocan el thread del wake word, el del audio y el del puente.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or settings.state_file
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = {
            "state": JarbisState.DORMIDO.value,
            "description": DESCRIPTIONS[JarbisState.DORMIDO],
            "color": COLORS[JarbisState.DORMIDO],
            "updated_at": None,
            "heard": None,
            "said": None,
            "listening": False,
            "speaking": False,
            "present": None,
            "wakeword_backend": settings.wakeword_backend,
            "vad_backend": settings.vad_backend,
            "voice": settings.tts_voice,
            "ptt": settings.push_to_talk,
            "follow_up": settings.follow_up,
            "greeting": settings.greeting_enabled,
            "tts": settings.tts_enabled,
            "turns": 0,
            "last_error": None,
            "latency_ms": None,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.flush()

    # ------------------------------------------------------------------ lectura

    @property
    def path(self) -> Path:
        return self._path

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    # ----------------------------------------------------------------- escritura

    def update(self, **fields: Any) -> None:
        """Mezcla campos en el snapshot y lo baja a disco."""
        with self._lock:
            self._snapshot.update(fields)
            self._snapshot["updated_at"] = datetime.now().isoformat()
            payload = dict(self._snapshot)
        self._write(payload)

    def set_state(self, state: JarbisState, error_message: str | None = None) -> None:
        """Refleja un cambio de estado de la máquina."""
        self.update(
            state=state.value,
            description=DESCRIPTIONS[state],
            color=COLORS[state],
            listening=state is JarbisState.ESCUCHANDO,
            speaking=state is JarbisState.HABLANDO,
            last_error=error_message if state is JarbisState.ERROR else None,
        )

    def set_heard(self, text: str, latency_ms: float | None = None) -> None:
        """Registra lo último que transcribió."""
        with self._lock:
            turns = self._snapshot.get("turns", 0) + 1
        self.update(heard=text, turns=turns, latency_ms=latency_ms)

    def set_said(self, text: str) -> None:
        """Registra lo último que contestó el cerebro."""
        self.update(said=text)

    def flush(self) -> None:
        """Fuerza una escritura del snapshot actual."""
        self._write(self.snapshot())

    def heartbeat(self) -> None:
        """
        Reescribe el estado sin cambiarlo, solo para refrescar `updated_at`.

        Hace falta porque la cara decide si el cuerpo está vivo mirando la
        antigüedad de ese campo. Sin latido, un cuerpo quieto en "dormido" no
        reescribe nada y a los pocos segundos la cara lo declara apagado —
        que es exactamente lo que pasó en el primer smoke test del botón: el
        cuerpo andaba y el botón decía "cuerpo apagado".

        La alternativa sería que la cara chequee el puerto del puente, pero eso
        la acopla al puente. Un latido en el archivo mantiene a la cara como lo
        que es: un lector de un archivo y nada más.
        """
        self.update()

    def _write(self, payload: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._path)
        except Exception as exc:
            logger.error(f"No se pudo escribir el estado en {self._path}: {exc}")


class SessionLog:
    """
    Historial de turnos en JSONL.

    Una línea por evento, append-only. Es lo que te deja reconstruir mañana qué
    entendió y qué contestó sin tener que mirar el log de loguru.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or settings.session_log
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, kind: str, **fields: Any) -> None:
        record = {"at": datetime.now().isoformat(), "kind": kind, **fields}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except Exception as exc:
                logger.error(f"No se pudo escribir el log de sesión: {exc}")
