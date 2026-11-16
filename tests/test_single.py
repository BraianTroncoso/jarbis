"""
Tests del guard de instancia única.

Vienen de un bug visible desde el escritorio: apretar el acceso dos veces
levantaba dos Jarbis. Dos robots en pantalla, dos cerebros, y dos cuerpos
peleando por el mismo puerto — el segundo moría con código 1 y el supervisor lo
reponía para siempre.

Lo que se prueba acá es el camino de archivo, que es el respaldo fuera de
Windows. En Windows manda el mutex con nombre, que no tiene ninguno de estos
problemas porque lo suelta el sistema; justamente por eso el que hay que
blindar es el otro.

El caso que importa no es "el segundo no entra" sino **"el lock viejo no deja
afuera al dueño legítimo"**: un guard que se traba es peor que no tener guard,
porque el síntoma es que Jarbis no abre nunca más y no hay nada en pantalla que
lo explique.

    pytest tests/test_single.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body.single import SingleInstance  # noqa: E402


def guard(tmp_path: Path, name: str = "sin-mutex") -> SingleInstance:
    """Un guard forzado al camino de archivo: el mutex dice "no estoy"."""
    g = SingleInstance(lock_file=tmp_path / "supervisor.lock", name=name)
    g._acquire_mutex = lambda: None         # type: ignore[method-assign]
    return g


# ============================================================================
# Uno solo
# ============================================================================


def test_el_primero_entra(tmp_path):
    assert guard(tmp_path).acquire() is True


def test_el_segundo_no_entra(tmp_path):
    primero = guard(tmp_path)
    assert primero.acquire() is True
    assert guard(tmp_path).acquire() is False, "levantó un segundo Jarbis"


def test_despues_de_soltar_entra_otro(tmp_path):
    primero = guard(tmp_path)
    primero.acquire()
    primero.release()

    assert guard(tmp_path).acquire() is True


def test_pedirlo_dos_veces_desde_el_mismo_no_se_bloquea(tmp_path):
    g = guard(tmp_path)
    assert g.acquire() is True
    assert g.acquire() is True


# ============================================================================
# Que no se trabe, que es el modo de falla caro
# ============================================================================


def test_un_lock_de_un_proceso_muerto_se_reclama(tmp_path):
    """
    Si el lock sobrevive al proceso, Jarbis no vuelve a abrir nunca y nada en
    pantalla lo explica. Peor que no tener guard.
    """
    lock = tmp_path / "supervisor.lock"
    lock.write_text(str(_pid_muerto()), encoding="utf-8")

    assert guard(tmp_path).acquire() is True


def test_un_lock_ilegible_se_reclama(tmp_path):
    (tmp_path / "supervisor.lock").write_text("", encoding="utf-8")
    assert guard(tmp_path).acquire() is True


def test_un_lock_con_basura_se_reclama(tmp_path):
    (tmp_path / "supervisor.lock").write_text("no soy un pid", encoding="utf-8")
    assert guard(tmp_path).acquire() is True


def test_el_lock_guarda_el_pid_de_quien_lo_tiene(tmp_path):
    g = guard(tmp_path)
    g.acquire()

    assert (tmp_path / "supervisor.lock").read_text(encoding="utf-8") == str(os.getpid())


def test_un_lock_de_un_proceso_vivo_no_se_reclama(tmp_path):
    """El complemento del anterior: reclamar de más es volver al bug."""
    (tmp_path / "supervisor.lock").write_text(str(os.getpid()), encoding="utf-8")

    assert guard(tmp_path).acquire() is False


# ============================================================================
# Sin ningún mecanismo disponible
# ============================================================================


def test_sin_mutex_y_sin_archivo_no_deja_pasar():
    """
    Ante la duda se niega. Negar como mucho no abre y se ve enseguida; dejar
    pasar levanta el segundo Jarbis, que es el bug que esto existe para evitar.
    """
    g = SingleInstance(lock_file=None, name="sin-mutex")
    g._acquire_mutex = lambda: None         # type: ignore[method-assign]

    assert g.acquire() is False


# ============================================================================
# Las tres respuestas del mutex
# ============================================================================


def test_si_el_mutex_dice_ocupado_no_se_cae_al_archivo(tmp_path):
    """
    El bug tal como pasó: el primer Jarbis tomó el mutex y nunca escribió
    archivo, así que el segundo veía el mutex ocupado, se caía al respaldo,
    encontraba el archivo libre y arrancaba igual. Dos robots en pantalla.

    "Ocupado" y "no puedo preguntar" son respuestas distintas y sólo la
    segunda habilita el respaldo.
    """
    g = SingleInstance(lock_file=tmp_path / "supervisor.lock", name="x")
    g._acquire_mutex = lambda: False        # type: ignore[method-assign]

    assert g.acquire() is False
    assert not (tmp_path / "supervisor.lock").exists(), "escribió el lock igual"


def test_si_el_mutex_lo_toma_no_hace_falta_el_archivo(tmp_path):
    g = SingleInstance(lock_file=tmp_path / "supervisor.lock", name="x")
    g._acquire_mutex = lambda: True         # type: ignore[method-assign]

    assert g.acquire() is True
    assert not (tmp_path / "supervisor.lock").exists()


def test_solo_un_mutex_ausente_habilita_el_respaldo(tmp_path):
    g = SingleInstance(lock_file=tmp_path / "supervisor.lock", name="x")
    g._acquire_mutex = lambda: None         # type: ignore[method-assign]

    assert g.acquire() is True
    assert (tmp_path / "supervisor.lock").exists()


# ============================================================================
# Como context manager
# ============================================================================


def test_el_with_suelta_al_salir(tmp_path):
    with guard(tmp_path) as tomado:
        assert tomado is True

    assert guard(tmp_path).acquire() is True


def _pid_muerto() -> int:
    """Un PID que seguro no existe."""
    pid = 999_999
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except OSError:
            pass
        pid -= 1
