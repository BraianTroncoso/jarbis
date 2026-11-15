"""
Test de extremo a extremo: cuerpo ↔ cerebro, sin micrófono.

Levanta el puente de verdad, arranca el cerebro de verdad como proceso aparte, y
empuja un turno. Verifica que el cerebro se conecte, lo reciba y mande la
respuesta de vuelta como `say`.

Lo único simulado es el audio: en vez de hablarle, se llama `emit(Turn(...))`
directo, que es exactamente lo que hace el orquestador cuando Whisper termina.
El cerebro corre con `--dry-run` para no gastar tokens ni depender de la red.

Esto es lo que cierra el lazo que el diseño viejo no podía cerrar.

    pytest tests/test_e2e.py -v -s
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "body"))

from jarbis_body.bridge.protocol import StateChanged, Turn  # noqa: E402
from jarbis_body.bridge.server import BridgeServer  # noqa: E402

E2E_PORT = 8798
E2E_TOKEN = "token-e2e"


@pytest.fixture
def cuerpo():
    """El puente, hablando por el cuerpo."""
    server = BridgeServer(host="127.0.0.1", port=E2E_PORT, token=E2E_TOKEN)
    assert server.start(timeout=10.0)
    yield server
    server.stop()


@pytest.fixture
def cerebro(cuerpo):
    """El cerebro real como subproceso, en dry-run."""
    proc = subprocess.Popen(
        [
            sys.executable,
            str(REPO / "brain" / "client.py"),
            "--url",
            f"ws://127.0.0.1:{E2E_PORT}",
            "--token",
            E2E_TOKEN,
            "--dry-run",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(REPO),
        # Grupo propio: así el teardown se lleva al cerebro y a cualquier hijo
        # suyo. Sin esto quedaban procesos sueltos reintentando contra un puerto
        # muerto, y eventualmente ensuciaban otro test de la suite.
        start_new_session=True,
    )

    # Esperar a que se conecte de verdad, no un sleep al azar.
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if cuerpo.client_count > 0:
            break
        if proc.poll() is not None:
            salida = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"el cerebro murió al arrancar:\n{salida}")
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("el cerebro no se conectó en 20s")

    yield proc

    import os
    import signal

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def test_el_cerebro_se_conecta_solo(cuerpo, cerebro):
    """El cerebro encuentra el puente y hace el handshake sin ayuda."""
    assert cuerpo.client_count == 1


def test_un_turno_va_y_vuelve(cuerpo, cerebro):
    """
    El lazo completo: el cuerpo empuja texto, el cerebro contesta `say`.

    Es el test que define si la arquitectura funciona. Con MCP esto era
    imposible: el cuerpo no tenía forma de iniciar nada.
    """
    recibidos: list[dict] = []
    llego = threading.Event()

    def handler(message: dict) -> None:
        recibidos.append(message)
        llego.set()

    cuerpo.on_command(handler)

    assert cuerpo.emit(
        Turn(text="guardá esto en MyJarbis", confidence=0.93, duration_s=1.4)
    )

    assert llego.wait(30.0), "el cerebro no contestó"

    respuesta = recibidos[0]
    assert respuesta["type"] == "say"
    assert respuesta["text"]
    # En dry-run repite lo que escuchó, así que se puede verificar el contenido.
    assert "guardá esto en MyJarbis" in respuesta["text"]
    # `then_listen` es lo que hace que sea conversación y no un comando suelto.
    assert respuesta["then_listen"] is True


def test_varios_turnos_seguidos(cuerpo, cerebro):
    """Tres turnos en fila, cada uno con su respuesta. Nada se pierde ni se mezcla."""
    recibidos: list[dict] = []
    lock = threading.Lock()

    def handler(message: dict) -> None:
        with lock:
            recibidos.append(message)

    cuerpo.on_command(handler)

    frases = ["uno", "dos", "tres"]
    for frase in frases:
        assert cuerpo.emit(Turn(text=frase))
        # Se espera la respuesta antes del siguiente: el cuerpo real no manda un
        # turno nuevo mientras está en PENSANDO.
        deadline = time.time() + 30.0
        esperados = frases.index(frase) + 1
        while time.time() < deadline:
            with lock:
                if len(recibidos) >= esperados:
                    break
            time.sleep(0.05)

    with lock:
        textos = [r["text"] for r in recibidos]
    assert len(textos) == 3, f"se esperaban 3 respuestas, llegaron {len(textos)}"
    for frase, texto in zip(frases, textos):
        assert frase in texto


def test_el_cerebro_tolera_eventos_que_no_son_turnos(cuerpo, cerebro):
    """Los cambios de estado y los wake no tienen que hacerlo contestar."""
    recibidos: list[dict] = []
    cuerpo.on_command(recibidos.append)

    cuerpo.emit(StateChanged(state="escuchando", previous="dormido"))
    cuerpo.emit(StateChanged(state="dormido", previous="escuchando"))
    time.sleep(1.5)

    assert recibidos == [], "contestó a un evento que no era un turno"


def test_el_cerebro_sobrevive_que_el_puente_se_caiga(cuerpo, cerebro):
    """
    Se corta el puente y se vuelve a levantar: el cerebro tiene que reconectar
    solo. Importa porque el cuerpo se va a reiniciar más veces que el cerebro.
    """
    assert cuerpo.client_count == 1
    cuerpo.stop()
    time.sleep(1.0)

    assert cuerpo.start(timeout=10.0)

    deadline = time.time() + 40.0
    while time.time() < deadline:
        if cuerpo.client_count > 0:
            break
        time.sleep(0.2)

    assert cuerpo.client_count == 1, "el cerebro no reconectó"
