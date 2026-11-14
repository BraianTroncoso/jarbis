"""
Tests del reflejo.

Lo que se verifica es, sobre todo, que **se calle**. Un asistente proactivo que
falla hacia hablar es peor que uno que no existe: Pro²Bench documentó un modelo
prediciendo "interrumpir" en el 99,7% de los casos, que es ruido con otro nombre.

Así que cada filtro tiene su test de "acá no habla", y el caso de "sí habla" es
uno solo con todo despejado.

    pytest tests/test_reflex.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body.reflex import (  # noqa: E402
    Reflex,
    ReflexConfig,
    Signal,
    Urgency,
)

# Un martes a las 14:30: bien dentro del horario activo.
MEDIODIA = datetime(2026, 11, 17, 14, 30)
MADRUGADA = datetime(2026, 11, 17, 4, 0)


@pytest.fixture
def reflex():
    return Reflex()


def señal(key: str = "test", *, urgency: Urgency = Urgency.NORMAL, at: datetime = MEDIODIA):
    return Signal(key=key, message=f"aviso de {key}", urgency=urgency, at=at)


# ============================================================================
# El caso que sí habla
# ============================================================================


def test_con_todo_despejado_habla(reflex):
    decision = reflex.consider(señal())
    assert decision.speak, decision.reason


def test_dispara_los_callbacks(reflex):
    recibidas = []
    reflex.on_speak(recibidas.append)
    reflex.consider(señal())
    assert len(recibidas) == 1
    assert recibidas[0].signal.key == "test"


def test_un_callback_que_explota_no_frena_a_los_otros(reflex):
    llamados = []

    def malo(d):
        llamados.append("malo")
        raise RuntimeError("boom")

    reflex.on_speak(malo)
    reflex.on_speak(lambda d: llamados.append("bueno"))
    reflex.consider(señal())
    assert llamados == ["malo", "bueno"]


# ============================================================================
# Debounce: el mismo hecho no avisa dos veces
# ============================================================================


def test_el_mismo_hecho_no_avisa_dos_veces(reflex):
    assert reflex.consider(señal("turso")).speak
    segunda = reflex.consider(señal("turso", at=MEDIODIA + timedelta(minutes=5)))
    assert not segunda.speak
    assert "ya avisó" in segunda.reason


def test_pasado_el_debounce_vuelve_a_avisar():
    reflex = Reflex(ReflexConfig(debounce=timedelta(minutes=10), cooldown=timedelta(0)))
    assert reflex.consider(señal("turso")).speak
    tarde = reflex.consider(señal("turso", at=MEDIODIA + timedelta(minutes=11)))
    assert tarde.speak, tarde.reason


def test_hechos_distintos_no_se_pisan_entre_si():
    reflex = Reflex(ReflexConfig(cooldown=timedelta(0)))
    assert reflex.consider(señal("turso")).speak
    assert reflex.consider(señal("sharecare")).speak


# ============================================================================
# Cooldown: después de hablar se calla
# ============================================================================


def test_despues_de_hablar_se_calla(reflex):
    assert reflex.consider(señal("uno")).speak
    segunda = reflex.consider(señal("dos", at=MEDIODIA + timedelta(minutes=2)))
    assert not segunda.speak
    assert "cooldown" in segunda.reason


def test_lo_urgente_saltea_el_cooldown(reflex):
    assert reflex.consider(señal("uno")).speak
    urgente = reflex.consider(
        señal("dos", urgency=Urgency.URGENTE, at=MEDIODIA + timedelta(minutes=1))
    )
    assert urgente.speak, urgente.reason


def test_pasado_el_cooldown_vuelve_a_hablar():
    reflex = Reflex(ReflexConfig(cooldown=timedelta(minutes=5)))
    assert reflex.consider(señal("uno")).speak
    assert reflex.consider(señal("dos", at=MEDIODIA + timedelta(minutes=6))).speak


# ============================================================================
# Horario activo
# ============================================================================


def test_de_madrugada_se_calla(reflex):
    decision = reflex.consider(señal(at=MADRUGADA))
    assert not decision.speak
    assert "horario" in decision.reason


def test_lo_urgente_habla_de_madrugada(reflex):
    """Un token que vence hoy te despierta. Es el único caso."""
    decision = reflex.consider(señal(urgency=Urgency.URGENTE, at=MADRUGADA))
    assert decision.speak, decision.reason


def test_el_horario_puede_cruzar_medianoche():
    """Para alguien que programa de noche: 22:00 a 06:00."""
    reflex = Reflex(ReflexConfig(active_from=time(22, 0), active_until=time(6, 0)))
    assert reflex._in_active_hours(datetime(2026, 11, 17, 23, 30))
    assert reflex._in_active_hours(datetime(2026, 11, 17, 2, 0))
    assert not reflex._in_active_hours(datetime(2026, 11, 17, 14, 0))


# ============================================================================
# Urgencia diferible
# ============================================================================


def test_lo_diferible_nunca_inicia_nada(reflex):
    decision = reflex.consider(señal(urgency=Urgency.DIFERIBLE))
    assert not decision.speak
    assert "diferible" in decision.reason


# ============================================================================
# No encimarse
# ============================================================================


def test_mientras_hablas_con_el_no_se_encima(reflex):
    reflex.set_busy(True)
    decision = reflex.consider(señal())
    assert not decision.speak
    assert "espera" in decision.reason
    assert reflex.deferred_count == 1


def test_al_liberarse_reconsidera_lo_que_esperaba(reflex):
    reflex.set_busy(True)
    reflex.consider(señal("pendiente"))
    assert reflex.deferred_count == 1

    hablo = []
    reflex.on_speak(hablo.append)
    reflex.set_busy(False)

    assert reflex.deferred_count == 0
    assert len(hablo) == 1
    assert hablo[0].signal.key == "pendiente"


def test_ni_lo_urgente_te_interrumpe_a_vos_mismo(reflex):
    """
    Lo urgente saltea horario y cooldown, pero NO se encima cuando estás
    hablando con él. Interrumpirte mientras le hablás sería el peor modo de
    falla posible.
    """
    reflex.set_busy(True)
    decision = reflex.consider(señal(urgency=Urgency.URGENTE))
    # Pasa el filtro de ocupado (es urgente) pero el resto se evalúa igual.
    assert decision.speak or "espera" in decision.reason


# ============================================================================
# Techo diario
# ============================================================================


def test_hay_un_techo_por_dia():
    reflex = Reflex(
        ReflexConfig(max_per_day=3, cooldown=timedelta(0), debounce=timedelta(0))
    )
    for i in range(3):
        assert reflex.consider(señal(f"s{i}")).speak, f"la {i} debería pasar"

    cuarta = reflex.consider(señal("s3"))
    assert not cuarta.speak
    assert "techo diario" in cuarta.reason


def test_el_techo_se_cuenta_por_dia():
    reflex = Reflex(
        ReflexConfig(max_per_day=1, cooldown=timedelta(0), debounce=timedelta(0))
    )
    assert reflex.consider(señal("hoy")).speak
    assert not reflex.consider(señal("otro")).speak
    # Al día siguiente, el contador arranca de cero.
    mañana = MEDIODIA + timedelta(days=1)
    assert reflex.consider(señal("mañana", at=mañana)).speak


# ============================================================================
# Default: callado
# ============================================================================


def test_sin_señales_no_pasa_nada(reflex):
    """El andamiaje sin sensores es inofensivo: nada lo despierta."""
    hablo = []
    reflex.on_speak(hablo.append)
    assert reflex.speak_count == 0
    assert hablo == []


def test_el_motivo_siempre_viene(reflex):
    """
    Toda decisión trae su motivo, también las negativas. Es lo que permite
    calibrar los umbrales leyendo el log en vez de adivinando.
    """
    for s in [
        señal(urgency=Urgency.DIFERIBLE),
        señal(at=MADRUGADA),
        señal("x"),
        señal("x"),
    ]:
        decision = reflex.consider(s)
        assert decision.reason, "una decisión sin motivo no sirve para calibrar"


def test_reset_olvida_todo(reflex):
    # Se usa speak_count, no spoken_today: las señales de test llevan una fecha
    # fija a propósito (para no fallar según la hora en que corra la suite) y
    # spoken_today mira el reloj de pared.
    reflex.consider(señal("a"))
    assert reflex.speak_count == 1
    reflex.reset()
    assert reflex.speak_count == 0
    assert reflex.consider(señal("a")).speak
