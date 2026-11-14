"""
Tests del VAD, que es lo que decide cuándo cerraste el turno.

El bug que estos tests existen para que no vuelva: el reloj del silencio
arrancaba al abrir el micrófono (`_last_sound_time = time.time()` en `start()`).
Con `silence_timeout=0.7`, eso cerraba el turno a los 0,4 segundos si tardabas
en empezar a hablar — lo detectó el primer smoke test real, apretando el botón
y no llegando a decir nada.

La semántica correcta: `silence_timeout` cuenta el silencio **después** de que
hablaste. Antes de la primera palabra no se cierra por silencio, se cierra por
`vad_max_wait_s`.

Se alimenta `_audio_callback` directamente con chunks sintéticos, sin micrófono.

    pytest tests/test_vad.py -v
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body.audio.voicemode import AudioRecorder  # noqa: E402

CHUNK = 1024


def silencio() -> np.ndarray:
    """Un chunk por debajo del umbral."""
    return np.full((CHUNK, 1), 0.001, dtype=np.float32)


def voz() -> np.ndarray:
    """Un chunk bien por encima del umbral."""
    return np.full((CHUNK, 1), 0.2, dtype=np.float32)


class Grabador:
    """
    Envuelve AudioRecorder para alimentarlo sin micrófono.

    No se llama `start()` porque abriría un InputStream real; se simula el
    estado que deja.

    Fuerza el detector por RMS. Estos tests son sobre **cuándo se cierra el
    turno**, no sobre qué detector se usa, y las señales sintéticas de acá son
    arrays de amplitud constante: el RMS las lee como fuertes, pero Silero
    —con razón— dice que un tono plano no es voz. Mezclar las dos cosas hacía
    que tests de timing fallaran por el detector. Silero se verifica aparte,
    con voz sintetizada de verdad.
    """

    def __init__(self, **kwargs):
        self.rec = AudioRecorder(**kwargs)
        self.rec._vad = None
        self.cierres: list[str] = []
        self.rec.on_silence = lambda: self.cierres.append("cerrado")

    def abrir(self) -> None:
        self.rec._audio_chunks = []
        self.rec._recording = True
        self.rec._start_time = time.time()
        self.rec._last_sound_time = None
        self.rec._silence_triggered = False
        self.rec._heard_speech = False

    def alimentar(self, chunk: np.ndarray, veces: int = 1) -> None:
        for _ in range(veces):
            self.rec._audio_callback(chunk, CHUNK, None, None)

    @property
    def cerro(self) -> bool:
        # El callback lanza un thread; se le da un instante.
        time.sleep(0.05)
        return bool(self.cierres)


# ============================================================================
# El bug
# ============================================================================


def test_no_cierra_antes_de_que_hables():
    """
    EL TEST QUE IMPORTA.

    Abrís el micrófono, pasa más del silence_timeout sin que digas nada, y el
    turno NO se cierra. Antes se cerraba y perdías la frase.
    """
    g = Grabador(silence_timeout=0.3)
    g.abrir()

    # Más del doble del timeout en silencio puro.
    time.sleep(0.7)
    g.alimentar(silencio(), veces=5)

    assert not g.cerro, "cerró el turno antes de que llegaras a hablar"


def test_cierra_despues_de_que_hablaste_y_te_callaste():
    """El caso normal: hablás, te callás, cierra."""
    g = Grabador(silence_timeout=0.3)
    g.abrir()

    g.alimentar(voz(), veces=3)
    assert not g.cerro, "cerró mientras todavía hablabas"

    time.sleep(0.4)
    g.alimentar(silencio())

    assert g.cerro, "no cerró después del silencio"


def test_el_silencio_se_cuenta_desde_la_ultima_palabra():
    """
    Una pausa corta en medio de la frase no corta.

    Es lo que pasa cuando dudás a mitad de oración.
    """
    g = Grabador(silence_timeout=0.3)
    g.abrir()

    g.alimentar(voz(), veces=2)
    time.sleep(0.15)          # pausa más corta que el timeout
    g.alimentar(silencio())
    assert not g.cerro, "cortó en una pausa corta"

    g.alimentar(voz(), veces=2)   # seguís hablando
    time.sleep(0.15)
    g.alimentar(silencio())
    assert not g.cerro, "el reloj no se reinició con la segunda frase"


def test_se_rinde_si_nunca_hablas():
    """
    Si el micrófono está muteado o no decís nada, cierra por max_wait y no se
    queda grabando para siempre.
    """
    g = Grabador(silence_timeout=5.0)
    g.rec.max_wait = 0.3
    g.abrir()

    time.sleep(0.4)
    g.alimentar(silencio(), veces=3)

    assert g.cerro, "nunca se rindió"


def test_cierra_una_sola_vez():
    """Muchos chunks de silencio no disparan el callback muchas veces."""
    g = Grabador(silence_timeout=0.2)
    g.abrir()

    g.alimentar(voz(), veces=2)
    time.sleep(0.3)
    g.alimentar(silencio(), veces=20)

    time.sleep(0.1)
    assert len(g.cierres) == 1, f"disparó {len(g.cierres)} veces"


def test_el_audio_se_acumula_igual():
    """Lo que no se puede perder: el audio se sigue guardando mientras espera."""
    g = Grabador(silence_timeout=0.3)
    g.abrir()

    g.alimentar(silencio(), veces=4)   # antes de hablar
    g.alimentar(voz(), veces=6)

    with g.rec._lock:
        total = sum(len(c) for c in g.rec._audio_chunks)
    assert total == CHUNK * 10, f"se perdieron chunks: {total}"


def test_el_reset_limpia_el_estado():
    """Un turno no hereda el `_heard_speech` del anterior."""
    g = Grabador(silence_timeout=0.3)

    g.abrir()
    g.alimentar(voz(), veces=2)
    assert g.rec._heard_speech

    g.abrir()   # turno nuevo
    assert not g.rec._heard_speech
    assert g.rec._last_sound_time is None
    assert not g.rec._silence_triggered


# ============================================================================
# Umbral
# ============================================================================


@pytest.mark.parametrize(
    "nivel,espera_voz",
    [(0.001, False), (0.015, False), (0.025, True), (0.2, True)],
)
def test_el_umbral_separa_voz_de_silencio(nivel, espera_voz):
    g = Grabador(silence_timeout=10.0, silence_threshold=0.02)
    g.abrir()
    g.alimentar(np.full((CHUNK, 1), nivel, dtype=np.float32), veces=2)
    assert g.rec._heard_speech is espera_voz


# ============================================================================
# Silero
# ============================================================================


def test_silero_necesita_los_64_de_contexto():
    """
    El bug que costó encontrar: la entrada son 576 samples (64 de contexto + 512
    nuevos), no 512. Con 512 el modelo acepta la entrada sin error y devuelve
    probabilidades bajas para TODO, así que parece andar y nunca detecta voz.

    Se verifica sobre la constante, no sobre el modelo, para que el test corra
    en WSL donde no hay onnxruntime.
    """
    from jarbis_body.audio.vad import CONTEXT_SAMPLES, WINDOW_SAMPLES

    assert CONTEXT_SAMPLES == 64
    assert WINDOW_SAMPLES == 512
    assert CONTEXT_SAMPLES + WINDOW_SAMPLES == 576


def test_silero_ausente_cae_al_rms():
    """
    Si el modelo no está en disco, el grabador tiene que seguir funcionando con
    el VAD viejo en vez de romperse.
    """
    from jarbis_body.audio.vad import SileroVad

    vad = SileroVad(model_path=Path("/no/existe/silero.onnx"))
    assert vad.load() is False
    assert vad.available is False
    # Y no explota si alguien lo usa igual.
    assert vad.is_speech(voz()) is False


# ============================================================================
# Push-to-talk
# ============================================================================


def test_en_ptt_el_vad_no_cierra_el_turno():
    """
    Con `auto_close=False` el turno dura lo que tengas el botón abajo: hablás,
    te callás, y NO cierra. Cierra `close_turn()` desde el orquestador.
    """
    g = Grabador(silence_timeout=0.2)
    g.rec.auto_close = False
    g.abrir()

    g.alimentar(voz(), veces=3)
    time.sleep(0.4)                 # mucho más que el silence_timeout
    g.alimentar(silencio(), veces=5)

    assert not g.cerro, "el VAD cerró el turno en modo push-to-talk"


def test_en_ptt_el_tope_duro_igual_protege():
    """Un botón trabado no graba para siempre."""
    g = Grabador(silence_timeout=0.2)
    g.rec.auto_close = False
    g.rec.hard_limit = 0.3
    g.abrir()

    g.alimentar(voz(), veces=2)
    time.sleep(0.4)
    g.alimentar(silencio(), veces=3)

    assert g.cerro, "el tope duro no disparó"


def test_en_ptt_sigue_detectando_voz():
    """
    El VAD no decide, pero sigue marcando si hubo voz. Hace falta para que el
    orquestador sepa si transcribir o descartar un turno vacío.
    """
    g = Grabador(silence_timeout=0.2)
    g.rec.auto_close = False
    g.abrir()

    g.alimentar(voz(), veces=2)
    assert g.rec._heard_speech
