"""
Tests del saludo de la mañana.

Lo que hay que blindar acá es que **no moleste**. Un saludo que aparece dos
veces, o a las cuatro de la tarde, o cada vez que reiniciás la máquina, se
apaga el primer día y nunca más se vuelve a prender.

    pytest tests/test_greeting.py -v
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body.config import settings  # noqa: E402
from jarbis_body.greeting import GreetingTrigger  # noqa: E402

MANANA = datetime(2026, 11, 17, 9, 30)     # dentro de 08:00–11:00
TARDE = datetime(2026, 11, 17, 15, 0)      # fuera
MADRUGADA = datetime(2026, 11, 17, 3, 0)   # fuera


@pytest.fixture
def trigger(tmp_path, monkeypatch):
    """Un disparador con el saludo prendido y su estado en un tmp."""
    monkeypatch.setattr(settings, "greeting_enabled", True)
    monkeypatch.setattr(settings, "greeting_from", "08:00")
    monkeypatch.setattr(settings, "greeting_until", "11:00")

    disparos = []
    t = GreetingTrigger(on_fire=disparos.append, state_path=tmp_path / "greeting.json")
    t.disparos = disparos
    return t


# ============================================================================
# Cuándo saluda
# ============================================================================


def test_saluda_en_la_ventana(trigger):
    assert trigger._check(MANANA) is True
    assert len(trigger.disparos) == 1


def test_no_saluda_a_la_tarde(trigger):
    assert trigger._check(TARDE) is False
    assert trigger.disparos == []


def test_no_saluda_de_madrugada(trigger):
    assert trigger._check(MADRUGADA) is False
    assert trigger.disparos == []


def test_no_saluda_si_esta_apagado(tmp_path, monkeypatch):
    """Arranca apagado: que te hable sin que le hables se prende a mano."""
    monkeypatch.setattr(settings, "greeting_enabled", False)
    disparos = []
    t = GreetingTrigger(on_fire=disparos.append, state_path=tmp_path / "g.json")
    assert t._check(MANANA) is False
    assert disparos == []
    assert t.start() is False


# ============================================================================
# Una sola vez — lo que evita que sea un despertador
# ============================================================================


def test_no_saluda_dos_veces_el_mismo_dia(trigger):
    assert trigger._check(MANANA) is True
    assert trigger._check(datetime(2026, 11, 17, 10, 0)) is False
    assert len(trigger.disparos) == 1


def test_sobrevive_un_reinicio(tmp_path, monkeypatch):
    """
    EL TEST QUE IMPORTA.

    El supervisor reinicia procesos y la máquina se prende más de una vez por
    mañana. Si "ya saludé" viviera en memoria, cada reinicio sería un saludo.
    """
    monkeypatch.setattr(settings, "greeting_enabled", True)
    monkeypatch.setattr(settings, "greeting_from", "08:00")
    monkeypatch.setattr(settings, "greeting_until", "11:00")
    ruta = tmp_path / "greeting.json"

    primero = GreetingTrigger(on_fire=lambda s: None, state_path=ruta)
    assert primero._check(MANANA) is True

    # Proceso nuevo, objeto nuevo, mismo archivo.
    disparos = []
    segundo = GreetingTrigger(on_fire=disparos.append, state_path=ruta)
    assert segundo._check(datetime(2026, 11, 17, 9, 45)) is False
    assert disparos == []


def test_al_dia_siguiente_vuelve_a_saludar(trigger):
    assert trigger._check(MANANA) is True
    assert trigger._check(datetime(2026, 11, 18, 9, 0)) is True
    assert len(trigger.disparos) == 2


def test_reset_lo_deja_saludar_de_nuevo(trigger):
    assert trigger._check(MANANA) is True
    trigger.reset()
    assert trigger._check(MANANA) is True


# ============================================================================
# La señal
# ============================================================================


def test_la_senal_le_pregunta_al_cerebro(trigger):
    """
    El saludo no es un texto fijo: es una pregunta para el cerebro, que lo arma
    con el contexto real de MyJarbis. Sin `ask_brain` el cuerpo diría el prompt
    en voz alta, que sería ridículo.
    """
    trigger._check(MANANA)
    signal = trigger.disparos[0]
    assert signal.data.get("ask_brain") is True
    assert signal.key == "saludo-matutino"
    assert signal.message == settings.greeting_prompt


def test_el_prompt_pide_una_respuesta_corta(trigger):
    """Lo va a leer un sintetizador: un párrafo largo se escucha eterno."""
    prompt = settings.greeting_prompt.lower()
    assert "una sola oración" in prompt or "corto" in prompt


# ============================================================================
# La ventana horaria
# ============================================================================


@pytest.mark.parametrize(
    "hora,dentro",
    [("07:59", False), ("08:00", True), ("09:30", True), ("11:00", True), ("11:01", False)],
)
def test_los_bordes_de_la_ventana(trigger, hora, dentro):
    h, m = (int(x) for x in hora.split(":"))
    from datetime import time as t

    assert trigger._in_window(t(h, m)) is dentro


def test_la_ventana_puede_cruzar_medianoche(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "greeting_from", "22:00")
    monkeypatch.setattr(settings, "greeting_until", "06:00")
    from datetime import time as t

    tr = GreetingTrigger(on_fire=lambda s: None, state_path=tmp_path / "g.json")
    assert tr._in_window(t(23, 0)) is True
    assert tr._in_window(t(2, 0)) is True
    assert tr._in_window(t(14, 0)) is False


def test_un_horario_invalido_no_rompe(tmp_path, monkeypatch):
    """Si alguien escribe cualquier cosa en el .env, cae a un default."""
    monkeypatch.setattr(settings, "greeting_from", "no soy una hora")
    tr = GreetingTrigger(on_fire=lambda s: None, state_path=tmp_path / "g.json")
    from datetime import time as t

    assert tr._parse("no soy una hora") == t(9, 0)
    assert isinstance(tr._in_window(t(10, 0)), bool)


def test_si_no_puede_anotar_no_saluda_en_loop(tmp_path, monkeypatch):
    """
    Si el archivo de estado no se puede escribir, es preferible no saludar a
    saludar cada minuto. Se verifica que `_mark` no explote.
    """
    monkeypatch.setattr(settings, "greeting_enabled", True)
    imposible = tmp_path / "no" / "existe" / "g.json"
    tr = GreetingTrigger(on_fire=lambda s: None, state_path=imposible)
    # No tiene que lanzar.
    tr._mark(date(2026, 11, 17))
