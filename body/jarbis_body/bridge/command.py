"""
Cliente corto para mandarle un comando al cuerpo.

Lo usan la cara web y el flork: los dos son procesos aparte que leen el archivo
de estado y, cuando apretás algo, necesitan decirle algo al cuerpo.

Una conexión por comando, y se cierra. Suena derrochador y no lo es: un apretón
de botón cada tantos segundos no justifica mantener un socket vivo con su
reconexión, su heartbeat y su estado. Y tiene una propiedad que importa más que
el ahorro: si el cuerpo no está corriendo, el comando falla solo y lo dice, en
vez de quedar encolado para aparecer diez minutos después.

Se usa el cliente **síncrono** de websockets (>=14) porque quien llama acá no
tiene event loop: `http.server` maneja cada request en su thread, y tkinter
corre su propio loop de UI.
"""

from __future__ import annotations

import json
from typing import Any

from jarbis_body.bridge.protocol import load_or_create_token
from jarbis_body.config import settings


class BridgeCommand:
    """
    Mandá comandos al cuerpo.

        cmd = BridgeCommand()
        ok, detalle = cmd.send("listen")
        ok, detalle = cmd.send("mode", {"ptt": True})
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int | None = None,
        kind: str = "face",
    ) -> None:
        self._url = f"ws://{host}:{port or settings.bridge_port}"
        self._token: str | None = None
        # Para que el log del cuerpo distinga un comando del panel de una
        # conexión del cerebro.
        self._kind = kind

    @property
    def url(self) -> str:
        return self._url

    def _get_token(self) -> str:
        # Se cachea: leer el archivo en cada click es innecesario, y si el token
        # rota hace falta reiniciar igual.
        if self._token is None:
            self._token = load_or_create_token(settings.bridge_token_file)
        return self._token

    def send(
        self, command: str, payload: dict[str, Any] | None = None, timeout: float = 3.0
    ) -> tuple[bool, str]:
        """
        Manda un comando. Devuelve (salió bien, detalle).

        Nunca lanza: quien llama es una UI, y una UI que explota porque el
        backend está apagado es peor que una que lo muestra.
        """
        try:
            from websockets.sync.client import connect
        except ImportError:
            return False, "falta websockets >= 14 (cliente síncrono)"

        try:
            with connect(self._url, open_timeout=timeout) as ws:
                ws.send(json.dumps({"type": "hello", "token": self._get_token(),
                                    "as": self._kind}))
                welcome = json.loads(ws.recv(timeout=timeout))
                if welcome.get("type") != "welcome":
                    return False, "el puente rechazó el handshake"

                message: dict[str, Any] = {"type": command}
                if payload:
                    message.update(payload)
                ws.send(json.dumps(message))
            return True, "enviado"
        except Exception as exc:
            # Lo más común con diferencia: el cuerpo no está corriendo.
            return False, f"no pude hablar con el cuerpo: {type(exc).__name__}"

    # Atajos, para que quien llama no escriba strings sueltos.

    def listen(self) -> tuple[bool, str]:
        """Abre el micrófono."""
        return self.send("listen")

    def close(self) -> tuple[bool, str]:
        """Cierra el turno y transcribe. Es soltar el botón en push-to-talk."""
        return self.send("close")

    def stop(self) -> tuple[bool, str]:
        """Corta la voz actual."""
        return self.send("stop")

    def sleep(self) -> tuple[bool, str]:
        """Vuelve a dormido."""
        return self.send("sleep")

    def mode(self, **flags: bool) -> tuple[bool, str]:
        """Cambia el modo: `ptt=`, `follow_up=`."""
        return self.send("mode", {k: bool(v) for k, v in flags.items()})
