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
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body import EXIT_USER_QUIT  # noqa: E402
from jarbis_body.supervisor import (  # noqa: E402
    QUIT_MARK_TTL_S,
    Component,
    Supervisor,
)


class ProcesoMuerto:
    """Un proceso que ya terminó, con el código que se le pida."""

    def __init__(self, code: int) -> None:
        self._code = code
        self.pid = 1234

    def poll(self) -> int:
        return self._code


def supervisor_con(
    code: int, name: str = "robot", mark: Path | None = None
) -> tuple[Supervisor, Component, list]:
    """Un supervisor con un componente ya muerto y `start` espiado."""
    component = Component(name, ["no-se-ejecuta"])
    component.proc = ProcesoMuerto(code)

    sup = Supervisor([component], quit_mark=mark)
    arrancados: list[Component] = []
    sup.start = lambda c: (arrancados.append(c), True)[1]  # type: ignore[assignment]
    return sup, component, arrancados


def marca(tmp_path: Path, antiguedad_s: float = 0.0) -> Path:
    """La seña que deja el menú, con la antigüedad que se pida."""
    ruta = tmp_path / "quit.mark"
    ruta.write_text(str(time.time() - antiguedad_s), encoding="utf-8")
    return ruta


# ============================================================================
# Cerrar a mano
# ============================================================================


def test_cerrar_a_mano_no_lo_repone(tmp_path):
    """El bug que esto blindea: lo cerrabas y volvía a aparecer."""
    sup, component, arrancados = supervisor_con(EXIT_USER_QUIT, mark=marca(tmp_path))

    sup._check()

    assert arrancados == [], "repuso un componente que cerraste vos"
    assert component.dead is True


def test_cerrar_a_mano_baja_todo(tmp_path):
    """
    El ítem dice "Cerrar Jarbis", no "cerrar el robot". Si bajara sólo la cara
    visible, lo caro —Whisper en memoria, la sesión del cerebro— seguiría ahí.
    """
    sup, _component, _ = supervisor_con(EXIT_USER_QUIT, mark=marca(tmp_path))

    sup._check()

    assert sup._stopping is True
    assert sup._closed_by_user is True


def test_cerrar_a_mano_no_cuenta_como_falla(tmp_path):
    """Si contara, cerrarlo cinco veces lo marcaría como roto."""
    sup, component, _ = supervisor_con(EXIT_USER_QUIT, mark=marca(tmp_path))

    sup._check()

    assert component.fails == 0


def test_cerrar_el_cuerpo_tambien_baja_todo(tmp_path):
    """El código reservado vale para cualquier pieza, no sólo para el robot."""
    sup, _component, arrancados = supervisor_con(
        EXIT_USER_QUIT, name="cuerpo", mark=marca(tmp_path)
    )

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
def test_cualquier_otro_codigo_es_una_caida(code, tmp_path):
    sup, component, _ = supervisor_con(
        code if code is not None else 0, mark=marca(tmp_path)
    )

    sup._check()

    assert sup._stopping is False
    assert component.fails == 1


# ============================================================================
# La seña, que es lo que hace que el código de salida no alcance
# ============================================================================


def test_sin_la_sena_el_codigo_no_alcanza(tmp_path):
    """
    Un código de salida es un número: lo puede producir un kill, un proceso
    roto, o un lanzador que propaga el código de su hijo. Si el número solo
    bastara, algún día Jarbis se apaga entero y el log dice "lo cerraste vos"
    sin que hayas tocado nada.
    """
    sup, component, arrancados = supervisor_con(
        EXIT_USER_QUIT, mark=tmp_path / "no-existe.mark"
    )

    sup._check()

    assert sup._stopping is False, "se apagó sin que nadie cerrara nada"
    assert component.fails == 1


def test_con_la_sena_fresca_si_cierra(tmp_path):
    sup, _component, _ = supervisor_con(EXIT_USER_QUIT, mark=marca(tmp_path))

    sup._check()

    assert sup._stopping is True


def test_una_sena_vieja_no_sirve(tmp_path):
    """Si no caducara, el cierre de anoche apagaría Jarbis mañana."""
    sup, _c, _ = supervisor_con(
        EXIT_USER_QUIT, mark=marca(tmp_path, antiguedad_s=QUIT_MARK_TTL_S + 60)
    )

    sup._check()

    assert sup._stopping is False


def test_la_sena_se_consume(tmp_path):
    """Leerla la borra: no puede quedar en disco esperando a un código 42."""
    ruta = marca(tmp_path)
    sup, _c, _ = supervisor_con(EXIT_USER_QUIT, mark=ruta)

    sup._check()

    assert not ruta.exists()


def test_una_sena_ilegible_no_cierra(tmp_path):
    ruta = tmp_path / "quit.mark"
    ruta.write_text("cualquier cosa", encoding="utf-8")
    sup, _c, _ = supervisor_con(EXIT_USER_QUIT, mark=ruta)

    sup._check()

    assert sup._stopping is False


def test_una_sena_del_futuro_no_cierra(tmp_path):
    """Un reloj corrido no puede ser una orden de apagado."""
    sup, _c, _ = supervisor_con(
        EXIT_USER_QUIT, mark=marca(tmp_path, antiguedad_s=-3600)
    )

    sup._check()

    assert sup._stopping is False


def test_la_sena_no_apaga_si_el_codigo_no_es_el_reservado(tmp_path):
    """Las dos condiciones, no una u otra."""
    sup, _c, _ = supervisor_con(1, mark=marca(tmp_path))

    sup._check()

    assert sup._stopping is False
