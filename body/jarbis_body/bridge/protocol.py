"""
Protocolo del puente entre el cuerpo y el cerebro.

JSON por mensaje sobre WebSocket. Deliberadamente chico: cinco tipos de mensaje
del cuerpo hacia el cerebro y cuatro de vuelta. Si hace falta un sexto, conviene
preguntarse si el cuerpo está intentando pensar.

## La inversión

Esto es lo único importante del rediseño. GesturePilot exponía diez tools MCP y
el cerebro tenía que llamar `submit_voice_prompt` para preguntar "¿dijo algo?".
MCP es pull: el cerebro habla primero, siempre. Con eso un twin proactivo es
imposible por construcción, no por falta de código.

Acá el cuerpo abre la conexión y **empuja**. El cerebro se queda escuchando. Eso
permite las dos cosas que antes no se podían: que Jarbis conteste sin que le
preguntes, y que avise sin que le hables.

## Autenticación

Loopback no es autenticación. El CVE-2026-25253 de OpenClaw (CVSS 8.8, RCE de un
click) funcionaba contra instancias atadas a 127.0.0.1: alcanza con que una
página web que estés visitando haga la conexión desde tu propio navegador. Por
eso el primer mensaje tiene que ser un `hello` con el token del archivo local, y
el servidor corta la conexión si no llega en dos segundos.
"""

from __future__ import annotations

import json
import secrets
import stat
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

PROTOCOL_VERSION = 1

# Quién se conecta. El cerebro mantiene la conexión; la cara y el flork la abren
# para un comando y la cierran. Sin esto el log decía "Cerebro conectado" cada
# vez que apretabas un botón, que es ruido y además mentira.
CLIENT_KINDS = {"brain": "cerebro", "face": "la cara", "robot": "el robot",
                "unknown": "un cliente"}

# Cuánto espera el servidor el `hello` antes de cortar.
HANDSHAKE_TIMEOUT_S = 2.0


# ============================================================================
# Mensajes del cuerpo hacia el cerebro
# ============================================================================


@dataclass
class Turn:
    """
    Dijiste algo y ya está transcripto. Es el mensaje central.

    `final=False` sería una hipótesis parcial en streaming; hoy el cuerpo manda
    solo finales, pero el campo está para no romper el protocolo después.
    """

    text: str
    confidence: float = 0.0
    duration_s: float = 0.0
    latency_ms: float = 0.0
    final: bool = True
    type: str = field(default="turn", init=False)


@dataclass
class StateChanged:
    """El cuerpo cambió de estado. El cerebro lo usa para saber si puede hablar."""

    state: str
    previous: str | None = None
    type: str = field(default="state", init=False)


@dataclass
class WakeDetected:
    """Se escuchó el nombre."""

    word: str
    score: float
    type: str = field(default="wake", init=False)


@dataclass
class Interrupted:
    """Le hablaste encima mientras hablaba: el cerebro debería abortar el turno."""

    reason: str = "speech"
    type: str = field(default="interrupted", init=False)


@dataclass
class Proactive:
    """
    El cuerpo habla primero.

    Es el único mensaje que NO viene de que le hayas hablado. Lo emite el
    reflejo cuando una señal pasó todos sus filtros, y es la razón de ser de
    toda la inversión del puente: con MCP esto no se podía expresar.
    """

    message: str
    key: str
    urgency: str = "normal"
    type: str = field(default="proactive", init=False)


@dataclass
class BodyError:
    """Algo se rompió en el cuerpo."""

    message: str
    where: str = ""
    type: str = field(default="error", init=False)


# ============================================================================
# Mensajes del cerebro hacia el cuerpo
# ============================================================================

# {"type": "say",      "text": "...", "then_listen": true}
# {"type": "stop"}                       cortar la voz actual
# {"type": "listen"}                     abrir el micrófono sin wake word
# {"type": "close"}                      cerrar el turno YA y transcribir
# {"type": "sleep"}                      volver a dormido
# {"type": "mode", "ptt": bool, "follow_up": bool, "greeting": bool}
#
# `close` existe para push-to-talk: con el botón apretado el VAD no cierra nada,
# cierra soltarlo. `mode` lo manda la cara cuando cambiás los interruptores.

BRAIN_COMMANDS = {"say", "stop", "listen", "close", "sleep", "mode", "ping"}


def encode(message: Any) -> str:
    """Serializa un dataclass de mensaje a JSON con metadatos."""
    payload = asdict(message) if hasattr(message, "__dataclass_fields__") else dict(message)
    payload.setdefault("type", "unknown")
    payload["v"] = PROTOCOL_VERSION
    payload["at"] = datetime.now().isoformat()
    return json.dumps(payload, ensure_ascii=False)


def decode(raw: str | bytes) -> dict[str, Any]:
    """Parsea un mensaje entrante. Devuelve {} si no es JSON válido."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Mensaje del puente no es JSON válido")
        return {}
    if not isinstance(data, dict):
        logger.warning("Mensaje del puente no es un objeto")
        return {}
    return data


# ============================================================================
# Token compartido
# ============================================================================


def load_or_create_token(path: Path) -> str:
    """
    Lee el token del puente, creándolo en el primer arranque.

    Se guarda con permisos restringidos. En NTFS el bit de modo de POSIX es
    cosmético, pero el archivo vive bajo el perfil del usuario, que ya está
    protegido por ACL; y si alguna vez esto corre en Linux, el chmod sirve.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
        logger.warning(f"Token vacío en {path}; se regenera")

    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    logger.info(f"Token del puente generado en {path}")
    return token
