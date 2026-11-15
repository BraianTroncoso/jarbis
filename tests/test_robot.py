"""
Tests del robot.

Se prueba la geometría y la expresión, no los píxeles: verificar un render sería
frágil y lento, y para mirarlo está `scripts/preview_robot.py`, que dibuja los
cinco estados a un PNG sobre fondo oscuro y claro.

Lo que sí hay que blindar es lo que rompe en silencio:

- que el chroma key no aparezca en la paleta. Cada píxel de ese color desaparece,
  así que un LED con ese valor sería un agujero en la cara.
- que los cinco estados se distingan entre sí. Toda la expresión son dos barras;
  si dos estados terminan con la misma forma y el mismo color, el robot deja de
  comunicar sin que nada falle.
- que las formas entren en la pantalla. Un ojo más alto que el marco se dibuja
  igual, cortado.

    pytest tests/test_robot.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body.robot import shape  # noqa: E402

ESTADOS = ["dormido", "escuchando", "pensando", "hablando", "error"]


# ============================================================================
# El chroma key
# ============================================================================


def test_el_color_transparente_no_esta_en_la_paleta():
    """
    Cada píxel del chroma desaparece y deja pasar el click. Si fuera el color de
    un LED, el robot tendría un agujero con forma de ojo.
    """
    usados = {shape.SCREEN_BG.lower(), shape.SCREEN_EDGE.lower(), shape.DMETER.lower()}
    for pal in shape.PALETTES.values():
        usados.update({pal.led.lower(), pal.edge.lower(), pal.bubble.lower()})
    assert shape.CHROMA.lower() not in usados


def test_el_chroma_es_improbable():
    """Tiene que ser un color que nadie elegiría para dibujar."""
    r, g, b = (int(shape.CHROMA[i : i + 2], 16) for i in (1, 3, 5))
    # Magenta saturado al máximo: rojo y azul full, verde casi nulo.
    assert r > 250 and b > 250 and g < 10


# ============================================================================
# Que los estados se distingan
# ============================================================================


@pytest.mark.parametrize("estado", ESTADOS)
def test_cada_estado_tiene_paleta_y_forma(estado):
    pal = shape.palette(estado)
    assert pal.led.startswith("#")
    assert pal.edge.startswith("#")
    assert shape.eyes(estado).height > 0


def test_los_cinco_estados_son_distinguibles():
    """
    Toda la expresión son dos barras. Si dos estados coinciden en forma Y color,
    el robot deja de comunicar y nada falla: hay que detectarlo acá.
    """
    firmas = {}
    for estado in ESTADOS:
        forma = shape.eyes(estado)
        pal = shape.palette(estado)
        firma = (round(forma.height, 1), round(forma.offset_y, 1), forma.cross, pal.led)
        assert firma not in firmas, f"{estado} se ve igual que {firmas[firma]}"
        firmas[firma] = estado


def test_dormido_es_el_unico_sin_latido():
    """El reposo tiene que verse quieto; si late, no es reposo."""
    assert shape.palette("dormido").glow is False
    assert shape.palette("error").glow is False  # el error tampoco late
    for estado in ("escuchando", "pensando", "hablando"):
        assert shape.palette(estado).glow is True


def test_dormido_tiene_los_ojos_casi_cerrados():
    assert shape.eyes("dormido").height < 8


def test_escuchando_tiene_los_ojos_bien_abiertos():
    assert shape.eyes("escuchando").height > 28


def test_pensando_mira_para_abajo():
    """El desplazamiento hacia abajo es lo que lo hace leer como 'pensando'."""
    assert shape.eyes("pensando").offset_y > 0


def test_solo_el_error_usa_aspas():
    for estado in ESTADOS:
        assert shape.eyes(estado).cross is (estado == "error")


def test_el_error_usa_el_magenta_de_dmeter():
    assert shape.palette("error").led == "#f73d8f"


def test_un_estado_desconocido_cae_a_dormido():
    """El cuerpo podría publicar un estado nuevo; el robot no puede romperse."""
    assert shape.palette("invento") == shape.palette("dormido")
    assert shape.eyes("invento") == shape.eyes("dormido")


# ============================================================================
# Que todo entre en la pantalla
# ============================================================================


@pytest.mark.parametrize("estado", ESTADOS)
def test_los_ojos_entran_en_la_pantalla(estado):
    forma = shape.eyes(estado)
    for centro in (shape.EYE_LEFT_X, shape.EYE_RIGHT_X):
        x0, y0, x1, y1 = shape.eye_box(centro, forma, 1.0, 0.0, 0.0)
        assert x0 >= 4, f"{estado}: el ojo se sale por la izquierda"
        assert x1 <= shape.SCREEN_W - 4, f"{estado}: se sale por la derecha"
        assert y0 >= 4, f"{estado}: se sale por arriba"
        assert y1 <= shape.SCREEN_H - 4, f"{estado}: se sale por abajo"


def test_los_ojos_no_se_tocan():
    """Dos barras pegadas se leen como una sola."""
    separacion = (shape.EYE_RIGHT_X - shape.EYE_W / 2) - (shape.EYE_LEFT_X + shape.EYE_W / 2)
    assert separacion >= 10, f"solo {separacion} px entre los ojos"


def test_la_onda_entra_y_no_pisa_los_ojos():
    forma = shape.eyes("hablando")
    _, _, _, ojo_abajo = shape.eye_box(shape.EYE_LEFT_X, forma, 1.0, 0.0, 0.0)
    onda_arriba = shape.WAVE_Y - shape.WAVE_MAX_H / 2
    assert onda_arriba > ojo_abajo, "la onda se superpone con los ojos"
    assert shape.WAVE_Y + shape.WAVE_MAX_H / 2 <= shape.SCREEN_H - 2


def test_los_puntitos_no_pisan_los_ojos():
    forma = shape.eyes("pensando")
    _, ojo_arriba, _, _ = shape.eye_box(shape.EYE_LEFT_X, forma, 1.0, 0.0, 0.0)
    assert shape.THINK_DOTS_Y + shape.THINK_DOT_R < ojo_arriba


# ============================================================================
# El rectángulo redondeado
# ============================================================================


def test_el_rectangulo_redondeado_cierra():
    puntos = shape.rounded_rect(0, 0, 100, 60, 12)
    assert len(puntos) % 2 == 0
    assert len(puntos) >= 8


def test_el_radio_se_acota_al_lado_corto():
    """
    Pedir un radio mayor que la mitad del lado corto daría los arcos cruzados.
    Se acota en vez de dibujar una figura rota.
    """
    puntos = shape.rounded_rect(0, 0, 20, 10, 999)
    xs, ys = puntos[0::2], puntos[1::2]
    assert min(xs) >= -0.01 and max(xs) <= 20.01
    assert min(ys) >= -0.01 and max(ys) <= 10.01


def test_radio_cero_da_un_rectangulo():
    assert shape.rounded_rect(0, 0, 10, 10, 0) == [0, 0, 10, 0, 10, 10, 0, 10]


def test_el_rectangulo_queda_dentro_de_su_caja():
    puntos = shape.rounded_rect(5, 7, 105, 67, 20)
    xs, ys = puntos[0::2], puntos[1::2]
    assert min(xs) >= 4.99 and max(xs) <= 105.01
    assert min(ys) >= 6.99 and max(ys) <= 67.01


# ============================================================================
# Animación
# ============================================================================


def test_la_onda_nunca_desaparece():
    """Una barra en cero se lee como un hueco, no como silencio."""
    for paso in range(20):
        for altura in shape.wave_heights(paso / 20):
            assert altura >= 0.25

def test_la_onda_tiene_barras_de_distinto_alto():
    """Si todas suben y bajan juntas parece un acordeón, no una voz."""
    alturas = shape.wave_heights(0.3)
    assert len(set(round(a, 2) for a in alturas)) > 3


def test_el_latido_es_continuo_al_dar_la_vuelta():
    """La fase vuelve a 0 todo el tiempo; no puede haber un salto ahí."""
    assert shape.glow_alpha(0.0) == pytest.approx(shape.glow_alpha(1.0), abs=0.01)


def test_el_latido_se_queda_en_rango():
    for paso in range(40):
        assert 0.0 <= shape.glow_alpha(paso / 40) <= 1.0


# ============================================================================
# Mezcla de colores
# ============================================================================


def test_mezclar_en_los_extremos_devuelve_los_originales():
    assert shape.mix("#000000", "#ffffff", 0.0) == "#000000"
    assert shape.mix("#000000", "#ffffff", 1.0) == "#ffffff"


def test_mezclar_a_la_mitad():
    assert shape.mix("#000000", "#ffffff", 0.5) == "#808080"


@pytest.mark.parametrize("valor", [-3.0, 1.5, 99.0])
def test_mezclar_acota_valores_locos(valor):
    resultado = shape.mix("#102030", "#405060", valor)
    assert resultado in ("#102030", "#405060")


def test_la_escala_da_el_ancho_pedido():
    for ancho in (96, 128, 160, 220):
        assert shape.SCREEN_W * shape.scale_for(ancho) == pytest.approx(ancho)
