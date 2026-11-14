"""
Test de integración del puente, sin micrófono ni cerebro real.

Verifica las dos cosas que tienen que ser verdad para que la arquitectura
funcione:

1. el cuerpo puede **empujar** un turno y el cerebro lo recibe sin preguntar
2. el cerebro puede mandar un comando de vuelta y el cuerpo lo ejecuta

Y la que tiene que ser verdad para que sea seguro: sin el token, la conexión se
rechaza. Eso no es paranoia de más — el CVE-2026-25253 de OpenClaw era
exactamente esto, un WebSocket en loopback sin autenticar, y estar en 127.0.0.1
no los salvó porque la conexión la hace el navegador de la víctima.

    pytest tests/test_bridge.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body.bridge.protocol import Turn, decode, encode  # noqa: E402
from jarbis_body.bridge.server import BridgeServer  # noqa: E402

# Puerto alto propio para no chocar con una instancia real corriendo en 8765.
TEST_PORT = 8799
TEST_TOKEN = "token-de-prueba-no-es-secreto"


@pytest.fixture
def bridge():
    """Un puente levantado en un puerto de prueba."""
    server = BridgeServer(host="127.0.0.1", port=TEST_PORT, token=TEST_TOKEN)
    assert server.start(timeout=10.0), "el puente no llegó a escuchar"
    yield server
    server.stop()


async def _connect(token: str = TEST_TOKEN):
    """Abre una conexión y hace el handshake. Devuelve el websocket."""
    import websockets

    ws = await websockets.connect(f"ws://127.0.0.1:{TEST_PORT}")
    await ws.send(json.dumps({"type": "hello", "token": token}))
    welcome = decode(await asyncio.wait_for(ws.recv(), timeout=3.0))
    assert welcome.get("type") == "welcome", f"handshake inesperado: {welcome}"
    return ws


def test_token_invalido_se_rechaza(bridge):
    """Sin el token correcto, la conexión se cierra."""

    async def scenario() -> None:
        import websockets

        ws = await websockets.connect(f"ws://127.0.0.1:{TEST_PORT}")
        await ws.send(json.dumps({"type": "hello", "token": "equivocado"}))
        with pytest.raises(Exception):
            # El servidor cierra; cualquier recv posterior falla.
            await asyncio.wait_for(ws.recv(), timeout=3.0)
            await asyncio.wait_for(ws.recv(), timeout=3.0)

    asyncio.run(scenario())
    assert bridge.client_count == 0


def test_handshake_ausente_corta(bridge):
    """Si no manda `hello`, el servidor corta por timeout."""

    async def scenario() -> None:
        import websockets

        ws = await websockets.connect(f"ws://127.0.0.1:{TEST_PORT}")
        # No se manda nada. El servidor tiene que cerrar solo.
        with pytest.raises(Exception):
            await asyncio.wait_for(ws.recv(), timeout=5.0)
            await asyncio.wait_for(ws.recv(), timeout=5.0)

    asyncio.run(scenario())


def test_el_cuerpo_empuja_y_el_cerebro_recibe(bridge):
    """
    El test central: `emit()` desde un thread cualquiera llega al cerebro.

    Esto es lo que el diseño anterior no podía hacer. Con MCP el cerebro tenía
    que llamar una tool para preguntar si había algo.
    """

    async def scenario() -> None:
        ws = await _connect()
        assert bridge.client_count == 1

        # Se emite desde OTRO thread, como pasa de verdad: el callback de audio
        # y el VAD corren fuera del loop del puente.
        emitted = threading.Event()

        def emit_from_thread() -> None:
            assert bridge.emit(Turn(text="guardá esto en MyJarbis", confidence=0.91))
            emitted.set()

        threading.Thread(target=emit_from_thread).start()
        assert emitted.wait(2.0)

        message = decode(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert message["type"] == "turn"
        assert message["text"] == "guardá esto en MyJarbis"
        assert message["confidence"] == pytest.approx(0.91)
        assert message["final"] is True
        assert "at" in message and "v" in message

        await ws.close()

    asyncio.run(scenario())


def test_el_cerebro_manda_comandos_de_vuelta(bridge):
    """Un `say` del cerebro llega a los handlers del cuerpo."""
    received: list[dict] = []
    done = threading.Event()

    def handler(message: dict) -> None:
        received.append(message)
        done.set()

    bridge.on_command(handler)

    async def scenario() -> None:
        ws = await _connect()
        await ws.send(
            json.dumps({"type": "say", "text": "lo guardé", "then_listen": True})
        )
        await asyncio.sleep(0.3)
        await ws.close()

    asyncio.run(scenario())

    assert done.wait(3.0), "el comando no llegó al handler"
    assert received[0]["type"] == "say"
    assert received[0]["text"] == "lo guardé"
    assert received[0]["then_listen"] is True


def test_comando_desconocido_se_ignora(bridge):
    """Un tipo que no está en BRAIN_COMMANDS no llega a los handlers."""
    received: list[dict] = []
    bridge.on_command(received.append)

    async def scenario() -> None:
        ws = await _connect()
        await ws.send(json.dumps({"type": "formatear_el_disco"}))
        await asyncio.sleep(0.3)
        await ws.close()

    asyncio.run(scenario())
    assert received == []


def test_emit_sin_cerebro_no_explota(bridge):
    """
    Sin nadie conectado, `emit()` devuelve False y sigue.

    Importa: el cuerpo tiene que arrancar aunque el cerebro esté apagado, en
    cualquier orden, sin bloquearse esperándolo.
    """
    assert bridge.client_count == 0
    assert bridge.emit(Turn(text="al vacío")) is False


def test_protocolo_serializa_ida_y_vuelta():
    """`encode` agrega metadatos y `decode` los devuelve. Sin servidor."""
    raw = encode(Turn(text="hola", confidence=0.5, latency_ms=120.0))
    message = decode(raw)
    assert message["type"] == "turn"
    assert message["text"] == "hola"
    assert message["v"] == 1
    assert message["latency_ms"] == 120.0


def test_decode_tolera_basura():
    """Un mensaje roto devuelve {} en vez de tirar excepción."""
    assert decode("no soy json") == {}
    assert decode("[1,2,3]") == {}
    assert decode(b'{"type":"ping"}') == {"type": "ping"}


# ============================================================================
# Modos: push-to-talk y una-sola-pregunta
# ============================================================================


def test_los_comandos_de_modo_estan_permitidos(bridge):
    """
    `close` y `mode` tienen que llegar a los handlers. `close` es la mitad de
    arriba del push-to-talk (soltar el botón) y `mode` lo manda la cara.
    """
    recibidos: list[dict] = []
    done = threading.Event()

    def handler(message: dict) -> None:
        recibidos.append(message)
        if len(recibidos) >= 2:
            done.set()

    bridge.on_command(handler)

    async def scenario() -> None:
        ws = await _connect()
        await ws.send(json.dumps({"type": "close"}))
        await ws.send(json.dumps({"type": "mode", "ptt": True, "follow_up": False}))
        await asyncio.sleep(0.3)
        await ws.close()

    asyncio.run(scenario())

    assert done.wait(3.0), f"llegaron {len(recibidos)} de 2"
    tipos = [r["type"] for r in recibidos]
    assert tipos == ["close", "mode"]
    assert recibidos[1]["ptt"] is True
    assert recibidos[1]["follow_up"] is False
