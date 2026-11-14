"""
Servidor del puente: el cuerpo empuja, el cerebro escucha.

Corre un WebSocket en loopback dentro de su propio thread con su propio event
loop, porque todo el resto del cuerpo es threads planos (el callback de audio de
PortAudio, el del wake word, los timers de la máquina de estados). Mezclar eso
con asyncio en el thread principal es la receta del cuelgue silencioso.

Las dos direcciones:

    cuerpo → cerebro   turn, state, wake, interrupted, error      (push)
    cerebro → cuerpo   say, stop, listen, sleep, ping             (comandos)

`emit()` es seguro de llamar desde cualquier thread: encola en el loop con
`call_soon_threadsafe`. Si no hay nadie conectado, el evento se descarta y se
loguea — el cuerpo no se bloquea esperando que el cerebro aparezca, que es lo
que hace que puedas arrancar los dos en cualquier orden.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from loguru import logger

from jarbis_body.bridge.protocol import (
    BRAIN_COMMANDS,
    CLIENT_KINDS,
    HANDSHAKE_TIMEOUT_S,
    PROTOCOL_VERSION,
    decode,
    encode,
    load_or_create_token,
)
from jarbis_body.bridge.netif import bind_hosts
from jarbis_body.config import settings

CommandHandler = Callable[[dict[str, Any]], None]


class BridgeServer:
    """
    Servidor de push. Uno por proceso.

        bridge = BridgeServer()
        bridge.on_command(handler)
        bridge.start()
        bridge.emit(Turn(text="hola"))
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
    ) -> None:
        # Si no se pide un host explícito, se calcula la lista: loopback más la
        # IP que mira a WSL. Sin eso el cerebro en WSL no llega — el loopback
        # de la VM y el de Windows son distintos en modo NAT.
        if host is not None:
            self._host: str | list[str] = host
        else:
            self._host = bind_hosts(allow_wsl=settings.bridge_allow_wsl)
        self._port = port or settings.bridge_port
        self._token = token or load_or_create_token(settings.bridge_token_file)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server = None
        self._clients: set[Any] = set()
        self._handlers: list[CommandHandler] = []
        self._ready = threading.Event()
        self._stopping = threading.Event()
        # Se separa "ya terminé de intentar" de "lo logré". Sin esto, un puerto
        # ocupado hacía que start() devolviera True y el cuerpo reportara
        # "puente escuchando" cuando en realidad nadie podía conectarse.
        self._bind_error: Exception | None = None

    # ------------------------------------------------------------ suscripción

    def on_command(self, handler: CommandHandler) -> None:
        """Registra un receptor de comandos del cerebro."""
        self._handlers.append(handler)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def is_running(self) -> bool:
        return (
            self._ready.is_set()
            and self._bind_error is None
            and not self._stopping.is_set()
        )

    @property
    def token(self) -> str:
        return self._token

    def _host_list(self) -> list[str]:
        """Las direcciones a las que se ató, siempre como lista."""
        return [self._host] if isinstance(self._host, str) else list(self._host)

    # ------------------------------------------------------------------ control

    def start(self, timeout: float = 5.0) -> bool:
        """Levanta el servidor en su propio thread. Bloquea hasta que escuche."""
        if self.is_running:
            return True

        self._stopping.clear()
        self._ready.clear()
        self._bind_error = None
        self._thread = threading.Thread(
            target=self._run_loop, name="JarbisBridge", daemon=True
        )
        self._thread.start()

        if not self._ready.wait(timeout):
            logger.error("El puente no llegó a escuchar en el tiempo esperado")
            return False

        if self._bind_error is not None:
            logger.error(
                f"El puente no pudo tomar {self._host}:{self._port} "
                f"({self._bind_error}). ¿Ya hay otra instancia corriendo?"
            )
            return False

        for address in self._host_list():
            logger.info(f"Puente escuchando en ws://{address}:{self._port}")
        return True

    def stop(self) -> None:
        """
        Cierra el servidor y todas las conexiones.

        Cierra el socket de escucha explícitamente antes de parar el loop. Sin
        eso el puerto queda tomado un rato y el siguiente arranque falla con un
        bind error confuso — lo encontró el test corriendo varios casos seguidos
        sobre el mismo puerto.
        """
        if self._loop is None:
            return

        self._stopping.set()

        if not self._loop.is_closed():
            closed = threading.Event()

            async def shutdown() -> None:
                try:
                    for client in list(self._clients):
                        try:
                            await client.close()
                        except Exception:
                            pass
                    self._clients.clear()
                    if self._server is not None:
                        self._server.close()
                        await self._server.wait_closed()
                        self._server = None
                finally:
                    closed.set()
                    self._loop.stop()

            try:
                self._loop.call_soon_threadsafe(
                    lambda: self._loop.create_task(shutdown())
                )
                closed.wait(timeout=3.0)
            except RuntimeError:
                # El loop ya se murió; no queda nada que cerrar ordenadamente.
                pass

        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

        self._ready.clear()
        logger.info("Puente detenido")

    def _run_loop(self) -> None:
        """Punto de entrada del thread: su propio event loop, aislado del resto."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
            loop.run_forever()
        except Exception as exc:
            if not self._stopping.is_set():
                logger.error(f"El loop del puente murió: {exc}")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _serve(self) -> None:
        import websockets

        try:
            self._server = await websockets.serve(
                self._handle_client,
                self._host,
                self._port,
                # Ping cada 20 s: detecta un cerebro que se murió sin cerrar.
                ping_interval=20,
                ping_timeout=20,
                max_size=1_000_000,
            )
            self._ready.set()
        except Exception as exc:
            # Se guarda el error y se libera start(), que es quien lo reporta.
            # No se re-lanza: levantaría el except del thread y ensuciaría el log
            # con un traceback que no agrega nada sobre "el puerto está ocupado".
            self._bind_error = exc
            self._ready.set()

    # ------------------------------------------------------------------ clientes

    async def _handle_client(self, websocket) -> None:
        """Atiende una conexión: handshake, y después escucha comandos."""
        peer = getattr(websocket, "remote_address", ("?", 0))

        kind = await self._handshake(websocket, peer)
        if kind is None:
            return

        quien = CLIENT_KINDS.get(kind, CLIENT_KINDS["unknown"])
        self._clients.add(websocket)
        # Las conexiones de un comando suelto son un ida y vuelta de
        # milisegundos; loguearlas en INFO llena el archivo de nada.
        nivel = logger.info if kind == "brain" else logger.debug
        nivel(f"{quien}: conectado ({len(self._clients)} en total)")

        try:
            async for raw in websocket:
                self._dispatch(decode(raw))
        except Exception as exc:
            logger.debug(f"Conexión del cerebro cerrada: {exc}")
        finally:
            self._clients.discard(websocket)
            nivel = logger.info if kind == "brain" else logger.debug
            nivel(f"{quien}: desconectado ({len(self._clients)} quedan)")

    async def _handshake(self, websocket, peer) -> str | None:
        """
        Exige un `hello` con el token antes de aceptar nada.

        Sin esto cualquier página que estés visitando puede conectarse a tu
        loopback desde tu propio navegador y hablarle al cuerpo.
        """
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=HANDSHAKE_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(f"Handshake sin respuesta desde {peer[0]}; cerrando")
            await websocket.close(code=4001, reason="handshake timeout")
            return None
        except Exception:
            return None

        message = decode(raw)
        if message.get("type") != "hello" or message.get("token") != self._token:
            logger.warning(f"Handshake rechazado desde {peer[0]} (token inválido)")
            await websocket.close(code=4003, reason="unauthorized")
            return None

        await websocket.send(
            encode({"type": "welcome", "protocol": PROTOCOL_VERSION, "body": "jarbis"})
        )
        kind = str(message.get("as") or "unknown")
        return kind if kind in CLIENT_KINDS else "unknown"

    def _dispatch(self, message: dict[str, Any]) -> None:
        """Entrega un comando del cerebro a los handlers registrados."""
        kind = message.get("type")
        if kind not in BRAIN_COMMANDS:
            logger.warning(f"Comando desconocido del cerebro: {kind!r}")
            return

        for handler in self._handlers:
            try:
                handler(message)
            except Exception as exc:
                logger.error(f"Handler de comando falló ({kind}): {exc}")

    # -------------------------------------------------------------------- emisión

    def emit(self, message: Any) -> bool:
        """
        Manda un evento a todos los cerebros conectados.

        Seguro desde cualquier thread. Devuelve False si no había nadie
        escuchando, sin bloquear ni fallar: el cuerpo tiene que poder funcionar
        con el cerebro apagado.
        """
        if self._loop is None or not self._ready.is_set():
            logger.debug("Puente no listo; evento descartado")
            return False

        if not self._clients:
            logger.debug("Sin cerebro conectado; evento descartado")
            return False

        payload = encode(message)
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._broadcast(payload))
        )
        return True

    async def _broadcast(self, payload: str) -> None:
        dead = []
        for client in list(self._clients):
            try:
                await client.send(payload)
            except Exception as exc:
                logger.debug(f"Envío al cerebro falló: {exc}")
                dead.append(client)
        for client in dead:
            self._clients.discard(client)
