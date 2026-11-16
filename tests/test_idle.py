"""
Tests de la vida en reposo.

Todo esto es animación, que es justo lo que nadie prueba y lo que más fácil se
rompe en silencio: una mirada que se pega a un borde, un parpadeo que queda
trabado, un despertar que no se duerme nunca. Nada de eso falla — se ve mal, y
sólo si lo estás mirando en ese momento.

Por eso `idle.py` no importa tkinter: así se puede correr acá.

    pytest tests/test_idle.py -v
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body.robot import idle  # noqa: E402

CENTRO = (1000.0, 500.0)


def vida(**kw) -> idle.IdleLife:
    """Una vida con azar fijo: las miraditas y los parpadeos son repetibles."""
    kw.setdefault("rng", random.Random(7))
    return idle.IdleLife(**kw)


def correr(v: idle.IdleLife, desde: float, hasta: float, cursor, paso: float = 0.05):
    """Simula cuadros entre dos momentos y devuelve el último."""
    t = desde
    ultimo = None
    while t <= hasta:
        ultimo = v.update(t, cursor, CENTRO)
        t += paso
    return ultimo


def despierta(desde: float = 0.0) -> idle.IdleLife:
    """Una vida ya despierta, para probar la mirada sin pelear con el sueño.

    Hace falta porque despertarse y mirar tienen radios distintos a propósito:
    se despierta si te le acercás, pero una vez despierto te sigue por toda la
    pantalla. Si compartieran radio, seguir al mouse sólo pasaría con el mouse
    encima y no se vería nunca.
    """
    v = vida()
    v.saw_you(desde)
    return v


# ============================================================================
# Despertarse y dormirse
# ============================================================================


def test_arranca_dormido():
    assert vida().update(0.0, None, CENTRO).awake is False


def test_el_mouse_cerca_lo_despierta():
    v = vida()
    estado = v.update(0.0, (CENTRO[0] + 40, CENTRO[1]), CENTRO)
    assert estado.awake is True


def test_el_mouse_lejos_no_lo_despierta():
    v = vida()
    lejos = (CENTRO[0] + idle.NEAR_PX + 50, CENTRO[1])
    assert v.update(0.0, lejos, CENTRO).awake is False


def test_se_duerme_al_rato_de_que_te_vas():
    v = vida(sleep_after_s=30.0)
    v.update(0.0, (CENTRO[0] + 10, CENTRO[1]), CENTRO)

    assert v.update(29.0, None, CENTRO).awake is True
    assert v.update(31.0, None, CENTRO).awake is False


def test_quedarse_cerca_lo_mantiene_despierto():
    """El temporizador se renueva mientras estés ahí, no se agota igual."""
    v = vida(sleep_after_s=30.0)
    cerca = (CENTRO[0] + 10, CENTRO[1])
    assert correr(v, 0.0, 120.0, cerca, paso=1.0).awake is True


def test_un_click_lo_despierta_aunque_el_mouse_este_lejos():
    """
    Se puede agarrar del borde y arrastrarlo sin pasar cerca del centro. Si eso
    no contara como interacción, se dormiría en la mano.
    """
    v = vida()
    v.saw_you(0.0)
    assert v.awake_at(1.0) is True


def test_dormido_no_mira_ni_parpadea():
    """Si durmiendo hiciera lo mismo que despierto, el estado no serviría."""
    estado = vida().update(0.0, None, CENTRO)
    assert (estado.gaze_x, estado.gaze_y) == (0.0, 0.0)
    assert estado.blinking is False
    assert estado.breath == 0.0


# ============================================================================
# La mirada
# ============================================================================


def test_mira_hacia_donde_esta_el_mouse():
    v = despierta()
    derecha = (CENTRO[0] + 300, CENTRO[1])
    estado = correr(v, 0.0, 2.0, derecha)
    assert estado.gaze_x > 0.3, "no siguió al mouse hacia la derecha"

    izquierda = (CENTRO[0] - 300, CENTRO[1])
    estado = correr(v, 2.0, 4.0, izquierda)
    assert estado.gaze_x < -0.3


def test_la_mirada_tambien_sube_y_baja():
    v = despierta()
    abajo = (CENTRO[0], CENTRO[1] + 300)
    assert correr(v, 0.0, 2.0, abajo).gaze_y > 0.3


def test_despierto_sigue_al_mouse_aunque_este_lejos():
    """
    Despertarse y mirar tienen radios distintos. Si el de mirar fuera el de
    despertarse, el robot sólo seguiría un cursor que ya está encima suyo —
    o sea, nunca se vería seguir nada.
    """
    v = despierta()
    lejos = (CENTRO[0] + idle.NEAR_PX * 3, CENTRO[1])
    assert correr(v, 0.0, 2.0, lejos).gaze_x > 0.3


@pytest.mark.parametrize("lejos", [2_000, 10_000, 100_000])
def test_la_mirada_no_se_sale_de_rango(lejos):
    """Sin tope, un monitor ancho mandaría el ojo fuera de la pantallita."""
    v = despierta()
    estado = correr(v, 0.0, 3.0, (CENTRO[0] + lejos, CENTRO[1] + lejos))
    assert -1.0 <= estado.gaze_x <= 1.0
    assert -1.0 <= estado.gaze_y <= 1.0


def test_la_mirada_no_salta():
    """Un ojo que se teletransporta se lee como un error de dibujo."""
    v = despierta()
    v.update(0.0, (CENTRO[0] + 5, CENTRO[1]), CENTRO)
    primero = v.update(0.05, (CENTRO[0] + 400, CENTRO[1]), CENTRO)
    assert primero.gaze_x < 0.5, "llegó al destino en un solo cuadro"
    assert primero.gaze_x > 0.0, "no se movió nada"


def test_la_mirada_llega_igual_a_cualquier_cuadro_por_segundo():
    """
    Despierto anima a 20 fps y dormido a 4. Si el suavizado fuera por cuadro y
    no por tiempo, la mirada tardaría cinco veces más en un caso que en el otro.
    """
    objetivo = (CENTRO[0] + 400, CENTRO[1])
    rapido = correr(despierta(), 0.0, 1.0, objetivo, paso=0.05)
    lento = correr(despierta(), 0.0, 1.0, objetivo, paso=0.25)
    assert rapido.gaze_x == pytest.approx(lento.gaze_x, abs=0.05)


# ============================================================================
# El parpadeo
# ============================================================================


def test_parpadea_mientras_esta_despierto():
    v = vida()
    cerca = (CENTRO[0] + 10, CENTRO[1])
    parpadeos = 0
    t = 0.0
    anterior = False
    while t <= 60.0:
        ahora = v.update(t, cerca, CENTRO).blinking
        if ahora and not anterior:
            parpadeos += 1
        anterior = ahora
        t += 0.05

    assert parpadeos > 5, f"sólo {parpadeos} parpadeos en un minuto"


def test_el_parpadeo_es_corto():
    """Un parpadeo largo no se lee como parpadeo, se lee como colgado."""
    v = vida()
    cerca = (CENTRO[0] + 10, CENTRO[1])
    t, duracion_max, actual = 0.0, 0.0, 0.0
    while t <= 60.0:
        if v.update(t, cerca, CENTRO).blinking:
            actual += 0.05
            duracion_max = max(duracion_max, actual)
        else:
            actual = 0.0
        t += 0.05
    assert duracion_max < 0.6


def test_el_parpadeo_no_es_un_metronomo():
    """Intervalos iguales se leen como máquina; de eso se trata el estado."""
    v = vida()
    cerca = (CENTRO[0] + 10, CENTRO[1])
    momentos, t, anterior = [], 0.0, False
    while t <= 180.0:
        ahora = v.update(t, cerca, CENTRO).blinking
        if ahora and not anterior:
            momentos.append(t)
        anterior = ahora
        t += 0.05

    intervalos = [round(b - a, 2) for a, b in zip(momentos, momentos[1:])]
    assert len(set(intervalos)) > 3, f"intervalos repetidos: {set(intervalos)}"


# ============================================================================
# La miradita de reposo
# ============================================================================


def test_mira_para_otro_lado_cada_tanto():
    """Sin cursor que seguir, la vida la pone la miradita."""
    v = vida()
    v.saw_you(0.0)

    maximo, t = 0.0, 0.0
    while t <= 120.0:
        if not v.awake_at(t):
            v.saw_you(t)
        maximo = max(maximo, abs(v.update(t, None, CENTRO).gaze_x))
        t += 0.1

    assert maximo > 0.3, "nunca miró para ningún lado"


def test_la_miradita_vuelve_al_centro():
    v = vida()
    v.saw_you(0.0)

    t, vueltas = 0.0, 0
    while t <= 120.0:
        if not v.awake_at(t):
            v.saw_you(t)
        if abs(v.update(t, None, CENTRO).gaze_x) < 0.05:
            vueltas += 1
        t += 0.1

    assert vueltas > 100, "se quedó mirando para el costado"


# ============================================================================
# La respiración
# ============================================================================


def test_la_respiracion_se_queda_en_rango():
    v = vida()
    cerca = (CENTRO[0] + 10, CENTRO[1])
    for paso in range(200):
        assert 0.0 <= v.update(paso * 0.1, cerca, CENTRO).breath <= 1.0


def test_la_respiracion_sube_y_baja():
    v = vida()
    cerca = (CENTRO[0] + 10, CENTRO[1])
    valores = [v.update(t * 0.2, cerca, CENTRO).breath for t in range(60)]
    assert max(valores) > 0.8 and min(valores) < 0.2


def test_la_respiracion_es_continua_al_dar_la_vuelta():
    """El ciclo vuelve a empezar todo el tiempo; no puede haber un salto ahí."""
    v = vida()
    cerca = (CENTRO[0] + 10, CENTRO[1])
    antes = v.update(idle.BREATH_S - 0.02, cerca, CENTRO).breath
    despues = v.update(idle.BREATH_S + 0.02, cerca, CENTRO).breath
    assert abs(antes - despues) < 0.05
