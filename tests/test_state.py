"""
Tests de la máquina de estados y del publicador.

El caso que importa es `test_el_timeout_dispara_de_verdad`. El bug 2 del plan era
que GesturePilot agendaba los timeouts con `asyncio.call_later()` desde threads
sin event loop, loguenado "No event loop, timeout not set" y siguiendo. Como todo
el orquestador usaba `threading.Thread`, **ningún timeout disparó nunca** en la
app real: si el VAD no cerraba el turno, quedaba escuchando para siempre.

Un test que verifica que el timeout dispara desde un thread plano es la única
forma de evitar que esa regresión vuelva sin que nadie se dé cuenta.

    pytest tests/test_state.py -v
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body.state.machine import StateMachine  # noqa: E402
from jarbis_body.state.states import JarbisState  # noqa: E402
from jarbis_body.state.writer import SessionLog, StateWriter  # noqa: E402


@pytest.fixture
def machine():
    m = StateMachine()
    yield m
    m.shutdown()


# ============================================================================
# Transiciones
# ============================================================================


def test_arranca_dormido(machine):
    assert machine.state is JarbisState.DORMIDO


def test_el_camino_feliz_completo(machine):
    assert machine.transition_to(JarbisState.ESCUCHANDO)
    assert machine.transition_to(JarbisState.PENSANDO)
    assert machine.transition_to(JarbisState.HABLANDO)
    assert machine.transition_to(JarbisState.DORMIDO)
    assert machine.state is JarbisState.DORMIDO


def test_modo_conversacion_vuelve_a_escuchar(machine):
    """HABLANDO → ESCUCHANDO sin pasar por DORMIDO: no hay que repetir el nombre."""
    machine.transition_to(JarbisState.ESCUCHANDO)
    machine.transition_to(JarbisState.PENSANDO)
    machine.transition_to(JarbisState.HABLANDO)
    assert machine.transition_to(JarbisState.ESCUCHANDO)
    assert machine.state is JarbisState.ESCUCHANDO


def test_transicion_ilegal_se_rechaza(machine):
    """DORMIDO → HABLANDO no existe: no puede contestar algo que no escuchó."""
    assert machine.transition_to(JarbisState.HABLANDO) is False
    assert machine.state is JarbisState.DORMIDO


def test_force_saltea_la_validacion(machine):
    """El apagado tiene que poder volver a dormido desde cualquier parte."""
    assert machine.transition_to(JarbisState.HABLANDO, force=True)
    assert machine.state is JarbisState.HABLANDO


def test_error_es_alcanzable_desde_cualquier_estado(machine):
    machine.transition_to(JarbisState.ESCUCHANDO)
    assert machine.set_error("micrófono ocupado")
    assert machine.state is JarbisState.ERROR
    assert machine.context.error_message == "micrófono ocupado"


# ============================================================================
# Observadores
# ============================================================================


def test_los_callbacks_reciben_el_cambio(machine):
    seen: list[tuple[JarbisState, JarbisState]] = []
    machine.on_change(lambda prev, cur, ctx: seen.append((prev, cur)))

    machine.transition_to(JarbisState.ESCUCHANDO)
    machine.transition_to(JarbisState.PENSANDO)

    assert seen == [
        (JarbisState.DORMIDO, JarbisState.ESCUCHANDO),
        (JarbisState.ESCUCHANDO, JarbisState.PENSANDO),
    ]


def test_un_callback_que_explota_no_frena_a_los_otros(machine):
    """
    Si el escritor de estado falla por disco lleno, el puente tiene que seguir
    recibiendo el cambio. Los callbacks corren fuera del lock justamente por esto.
    """
    llamados: list[str] = []

    def malo(prev, cur, ctx):
        llamados.append("malo")
        raise RuntimeError("disco lleno")

    machine.on_change(malo)
    machine.on_change(lambda prev, cur, ctx: llamados.append("bueno"))

    assert machine.transition_to(JarbisState.ESCUCHANDO)
    assert llamados == ["malo", "bueno"]


# ============================================================================
# Timeouts — el bug 2
# ============================================================================


def test_el_timeout_dispara_de_verdad():
    """
    El test que existe por el bug 2.

    Se llama desde un thread plano, sin ningún event loop corriendo, que es
    exactamente la condición en la que la implementación vieja se rendía en
    silencio. Con `threading.Timer` tiene que disparar igual.
    """
    machine = StateMachine(timeouts={JarbisState.ESCUCHANDO: 0.3})
    try:
        volvio = threading.Event()

        def on_change(prev, cur, ctx):
            if prev is JarbisState.ESCUCHANDO and cur is JarbisState.DORMIDO:
                volvio.set()

        machine.on_change(on_change)

        # Desde un thread, sin loop, como en la app real.
        def activar() -> None:
            machine.transition_to(JarbisState.ESCUCHANDO)

        thread = threading.Thread(target=activar)
        thread.start()
        thread.join()

        assert machine.state is JarbisState.ESCUCHANDO
        assert volvio.wait(2.0), "el timeout no disparó (regresión del bug 2)"
        assert machine.state is JarbisState.DORMIDO
    finally:
        machine.shutdown()


def test_reentrar_al_mismo_estado_reinicia_el_timeout():
    """
    En modo conversación se vuelve a ESCUCHANDO repetidamente; cada vuelta tiene
    que resetear el reloj, no acumular.
    """
    machine = StateMachine(timeouts={JarbisState.ESCUCHANDO: 0.5})
    try:
        machine.transition_to(JarbisState.ESCUCHANDO)
        time.sleep(0.3)
        machine.transition_to(JarbisState.ESCUCHANDO)  # reinicia
        time.sleep(0.3)
        # Si no se hubiera reiniciado, acá ya habría expirado (0.6 > 0.5).
        assert machine.state is JarbisState.ESCUCHANDO
    finally:
        machine.shutdown()


def test_un_timer_viejo_no_pisa_el_estado_nuevo():
    """
    Si se sale de ESCUCHANDO antes del timeout, el timer que queda en vuelo no
    tiene que mandar a dormir desde PENSANDO.
    """
    machine = StateMachine(
        timeouts={JarbisState.ESCUCHANDO: 0.3, JarbisState.PENSANDO: None}
    )
    try:
        machine.transition_to(JarbisState.ESCUCHANDO)
        machine.transition_to(JarbisState.PENSANDO)
        time.sleep(0.6)
        assert machine.state is JarbisState.PENSANDO
    finally:
        machine.shutdown()


def test_shutdown_cancela_los_timers():
    machine = StateMachine(timeouts={JarbisState.ESCUCHANDO: 0.3})
    machine.transition_to(JarbisState.ESCUCHANDO)
    machine.shutdown()
    time.sleep(0.5)
    assert machine.state is JarbisState.ESCUCHANDO


# ============================================================================
# Publicador de estado
# ============================================================================


def test_el_escritor_deja_json_valido(tmp_path):
    import json

    path = tmp_path / "estado.json"
    writer = StateWriter(path=path)

    writer.set_state(JarbisState.ESCUCHANDO)
    writer.set_heard("guardá esto", latency_ms=1234.5)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["state"] == "escuchando"
    assert data["listening"] is True
    assert data["speaking"] is False
    assert data["heard"] == "guardá esto"
    assert data["turns"] == 1
    assert data["latency_ms"] == 1234.5
    assert data["updated_at"] is not None


def test_el_escritor_no_deja_json_cortado(tmp_path):
    """
    Escritura atómica. El visor lee cada 300 ms; sin `os.replace()` tarde o
    temprano lee un archivo a medio escribir y rompe el parseo.
    """
    import json

    path = tmp_path / "estado.json"
    writer = StateWriter(path=path)

    errores: list[Exception] = []

    def escribir() -> None:
        for i in range(60):
            writer.set_heard(f"frase numero {i} con texto suficiente para pesar algo")

    def leer() -> None:
        for _ in range(200):
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # pragma: no cover
                errores.append(exc)
            time.sleep(0.002)

    hilos = [threading.Thread(target=escribir), threading.Thread(target=leer)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert not errores, f"se leyó JSON cortado: {errores[:3]}"
    assert not list(path.parent.glob("*.tmp")), "quedó un .tmp sin renombrar"


def test_el_log_de_sesion_es_jsonl(tmp_path):
    import json

    path = tmp_path / "sesion.jsonl"
    log = SessionLog(path=path)

    log.append("heard", text="hola", latency_ms=12.0)
    log.append("said", text="chau")

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["kind"] == "heard"
    assert first["text"] == "hola"
    assert "at" in first
