"""
Tests del supervisor.

Lo que se prueba acá es una sola distinción, y es la que hace que Jarbis se
pueda apagar: **cerrar también es morirse**, visto desde afuera. Un supervisor
que repone todo lo que se muere repone lo que acabás de cerrar, y entonces el
botón de cerrar no cierra nada.

No se levantan procesos de verdad: se le ponen procesos falsos con un código de
salida elegido, que es exactamente la única información con la que el
supervisor toma la decisión.

    pytest tests/test_supervisor.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body import EXIT_USER_QUIT  # noqa: E402
from jarbis_body.supervisor import Component, Supervisor  # noqa: E402


class ProcesoMuerto:
    """Un proceso que ya terminó, con el código que se le pida."""

    def __init__(self, code: int) -> None:
        self._code = code
        self.pid = 1234

    def poll(self) -> int:
        return self._code


def supervisor_con(code: int, name: str = "robot") -> tuple[Supervisor, Component, list]:
    """Un supervisor con un componente ya muerto y `start` espiado."""
    component = Component(name, ["no-se-ejecuta"])
    component.proc = ProcesoMuerto(code)

    sup = Supervisor([component])
    arrancados: list[Component] = []
    sup.start = lambda c: (arrancados.append(c), True)[1]  # type: ignore[assignment]
    return sup, component, arrancados


# ============================================================================
# Cerrar a mano
# ============================================================================


def test_cerrar_a_mano_no_lo_repone():
    """El bug que esto blindea: lo cerrabas y volvía a aparecer."""
    sup, component, arrancados = supervisor_con(EXIT_USER_QUIT)

    sup._check()

    assert arrancados == [], "repuso un componente que cerraste vos"
    assert component.dead is True


def test_cerrar_a_mano_baja_todo():
    """
    El ítem dice "Cerrar Jarbis", no "cerrar el robot". Si bajara sólo la cara
    visible, lo caro —Whisper en memoria, la sesión del cerebro— seguiría ahí.
    """
    sup, _component, _ = supervisor_con(EXIT_USER_QUIT)

    sup._check()

    assert sup._stopping is True
    assert sup._closed_by_user is True


def test_cerrar_a_mano_no_cuenta_como_falla():
    """Si contara, cerrarlo cinco veces lo marcaría como roto."""
    sup, component, _ = supervisor_con(EXIT_USER_QUIT)

    sup._check()

    assert component.fails == 0


def test_cerrar_el_cuerpo_tambien_baja_todo():
    """El código reservado vale para cualquier pieza, no sólo para el robot."""
    sup, _component, arrancados = supervisor_con(EXIT_USER_QUIT, name="cuerpo")

    sup._check()

    assert sup._stopping is True
    assert arrancados == []


# ============================================================================
# Morirse de verdad, que es lo que el supervisor existe para arreglar
# ============================================================================


def test_morirse_sigue_reponiendo():
    sup, component, arrancados = supervisor_con(1)

    sup._check()          # lo ve caído y agenda el reintento
    assert component.fails == 1
    assert sup._stopping is False

    component.next_try = 0.0
    sup._check()          # ya pasó el backoff
    assert arrancados == [component]


def test_morirse_no_baja_el_resto():
    sup, component, _ = supervisor_con(1)

    sup._check()

    assert sup._stopping is False
    assert sup._closed_by_user is False
    assert component.dead is False


def test_se_rinde_despues_de_varias_fallas():
    """Reintentar para siempre es peor que estar caído: hace ruido y gasta."""
    sup, component, arrancados = supervisor_con(1)

    # Dos pasadas por ciclo, como en la realidad: en la que descubre la muerte
    # se agenda el reintento, y recién en una posterior se intenta.
    for _ in range(10):
        component.proc = ProcesoMuerto(1)
        sup._check()
        component.next_try = 0.0
        sup._check()

    assert component.dead is True
    assert len(arrancados) < 10


def test_el_backoff_crece_y_se_corta():
    component = Component("x", ["x"])
    anteriores = []
    for fails in range(10):
        component.fails = fails
        anteriores.append(component.backoff())

    assert anteriores[0] < anteriores[1] < anteriores[2]
    assert max(anteriores) == 30.0


# ============================================================================
# El código en sí
# ============================================================================


def test_el_codigo_reservado_no_choca_con_los_comunes():
    """
    0 es salir bien, 1 es error, 2 es error de argumentos, y Windows usa los
    altos. Si el reservado fuera uno de ésos, un proceso que se rompe de verdad
    apagaría Jarbis en silencio.
    """
    assert EXIT_USER_QUIT not in (0, 1, 2, 130, 143)
    assert 0 < EXIT_USER_QUIT < 126


@pytest.mark.parametrize("code", [0, 1, 2, -9, None])
def test_cualquier_otro_codigo_es_una_caida(code):
    sup, component, _ = supervisor_con(code if code is not None else 0)

    sup._check()

    assert sup._stopping is False
    assert component.fails == 1
