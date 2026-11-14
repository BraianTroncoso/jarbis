"""
Síntesis de voz con Piper.

Heredado de GesturePilot v0.2.0 (integration/tts.py) con tres arreglos del plan:

- bug 3: `stop()` abría `sd.stop()`, que corta TODA la reproducción de sounddevice
  en el proceso, no solo la voz. Ahora la reproducción va por un `OutputStream`
  propio y se corta chequeando el flag entre chunks.
- bug 4: `MODELS_DIR` se resolvía con un walk de cuatro `.parent` encadenados, que
  se rompe en cuanto el paquete se instala fuera del árbol del repo. Ahora sale de
  `settings`.
- bug 6: `_is_speaking` y `_stop_requested` se mutaban entre threads sin lock (el
  `_lock` solo cubría el init). Ahora hay un lock de estado aparte.

Además: la voz cae a la de respaldo si la principal no está en disco, en vez de
fallar en silencio.
"""

from __future__ import annotations

import io
import threading
import wave
from pathlib import Path

from loguru import logger

from jarbis_body.config import settings

# Cuántos frames se escriben por vuelta al stream. Más chico = corta más rápido
# cuando le hablás encima, más grande = menos riesgo de underrun. 1024 a 22 kHz
# son ~46 ms, que es el techo de latencia de interrupción.
_CHUNK_FRAMES = 1024


class JarbisTTS:
    """
    Motor de síntesis con Piper.

    Uso:
        tts = JarbisTTS()
        tts.speak("listo", block=False)
        tts.stop()  # corta solo esta voz
    """

    def __init__(self, voice: str | None = None) -> None:
        self._voice_name = voice or settings.tts_voice
        self._model_path, self._config_path = self._resolve_voice(self._voice_name)

        self._voice = None
        self._init_lock = threading.Lock()
        self._initialized = False

        # bug 6: estado compartido entre el thread de habla y quien llame stop()
        self._state_lock = threading.Lock()
        self._is_speaking = False
        self._stop_requested = False

        logger.info(f"JarbisTTS listo con voz: {self._voice_name}")

    # ------------------------------------------------------------------ rutas

    def _resolve_voice(self, voice: str) -> tuple[Path, Path]:
        """
        Devuelve (modelo, config) de la voz pedida, cayendo a la de respaldo si
        la principal no está en disco. bug 4: todo sale de settings.
        """
        model = settings.piper_model_path(voice)
        config = settings.piper_config_path(voice)

        if model.exists():
            return model, config

        fallback = settings.tts_voice_fallback
        if fallback and fallback != voice:
            fb_model = settings.piper_model_path(fallback)
            if fb_model.exists():
                logger.warning(
                    f"Voz '{voice}' no está en disco; usando respaldo '{fallback}'"
                )
                self._voice_name = fallback
                return fb_model, settings.piper_config_path(fallback)

        logger.error(f"No hay ninguna voz Piper en {settings.piper_dir}")
        return model, config

    # ------------------------------------------------------------------- init

    def _ensure_initialized(self) -> bool:
        """Carga la voz Piper una sola vez."""
        if self._initialized:
            return True

        with self._init_lock:
            if self._initialized:
                return True
            try:
                from piper import PiperVoice

                if not self._model_path.exists():
                    logger.error(f"Modelo no encontrado: {self._model_path}")
                    return False

                logger.info(f"Cargando voz Piper desde {self._model_path}...")
                self._voice = PiperVoice.load(
                    str(self._model_path),
                    str(self._config_path) if self._config_path.exists() else None,
                )
                self._initialized = True
                logger.info("Voz Piper cargada")
                return True
            except Exception as exc:
                logger.error(f"No se pudo inicializar Piper: {exc}")
                return False

    def preload(self) -> bool:
        """Carga la voz por adelantado para que el primer turno no pague el costo."""
        return self._ensure_initialized()

    # -------------------------------------------------------------- síntesis

    def synthesize(self, text: str) -> bytes | None:
        """Sintetiza texto a un WAV en memoria."""
        if not self._ensure_initialized():
            return None

        try:
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav_file:
                # Los parámetros van ANTES de sintetizar.
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self._voice.config.sample_rate)
                # Es synthesize_wav, no synthesize.
                self._voice.synthesize_wav(text, wav_file)
            buffer.seek(0)
            return buffer.read()
        except Exception as exc:
            logger.error(f"Falló la síntesis: {exc}")
            return None

    def synthesize_to_file(self, text: str, path: Path) -> bool:
        """
        Sintetiza a un archivo. Lo usa el doctor para verificar la cadena de voz
        sin necesidad de parlantes.
        """
        data = self.synthesize(text)
        if not data:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return True

    # ----------------------------------------------------------- estado/corte

    @property
    def is_speaking(self) -> bool:
        with self._state_lock:
            return self._is_speaking

    def stop(self) -> None:
        """
        Pide corte de la voz actual.

        bug 3: NO llama sd.stop(). Solo levanta el flag; el loop de escritura lo
        ve entre chunks y cierra su propio stream. Cualquier otro audio del
        proceso sigue sonando.
        """
        with self._state_lock:
            if not self._is_speaking:
                return
            self._stop_requested = True
        logger.info("Voz interrumpida")

    # ------------------------------------------------------------------ habla

    def speak(self, text: str, block: bool = True) -> bool:
        """
        Dice el texto en voz alta.

        Args:
            text: qué decir
            block: si True espera a que termine; si False vuelve enseguida
        """
        if not text or not text.strip():
            return False

        if not settings.tts_enabled:
            logger.debug("TTS deshabilitado por configuración")
            return False

        # Si ya está hablando, lo cortamos antes de encimar.
        if self.is_speaking:
            self.stop()

        if block:
            return self._speak_blocking(text)

        threading.Thread(
            target=self._speak_blocking,
            args=(text,),
            name="JarbisTTS-speak",
            daemon=True,
        ).start()
        return True

    def _speak_blocking(self, text: str) -> bool:
        with self._state_lock:
            self._is_speaking = True
            self._stop_requested = False

        try:
            import numpy as np
            import sounddevice as sd

            wav_data = self.synthesize(text)
            if not wav_data:
                logger.error("La síntesis no devolvió audio")
                return False

            with wave.open(io.BytesIO(wav_data), "rb") as wav_file:
                sample_rate = wav_file.getframerate()
                frames = wav_file.readframes(wav_file.getnframes())

            audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            logger.info(f"Diciendo: '{text[:60]}'")

            # Stream propio: cerrarlo solo afecta a esta voz.
            with sd.OutputStream(
                samplerate=sample_rate, channels=1, dtype="float32"
            ) as stream:
                for start in range(0, len(audio), _CHUNK_FRAMES):
                    with self._state_lock:
                        if self._stop_requested:
                            break
                    stream.write(audio[start : start + _CHUNK_FRAMES])

            with self._state_lock:
                interrupted = self._stop_requested
            if not interrupted:
                logger.debug("Voz terminada")
            return True

        except Exception as exc:
            logger.error(f"Falló la reproducción: {exc}")
            return False
        finally:
            with self._state_lock:
                self._is_speaking = False
                self._stop_requested = False

    # ------------------------------------------------------- frases enlatadas

    def acknowledge(self) -> bool:
        """Acuse corto, para cuando se recibió el turno y se está pensando."""
        return self.speak("Dale", block=False)

    def error(self) -> bool:
        return self.speak("Se me rompió algo", block=False)

    def cancelled(self) -> bool:
        return self.speak("Cancelado", block=False)


# Instancia global: una sola voz cargada para todo el proceso.
_instance: JarbisTTS | None = None
_instance_lock = threading.Lock()


def get_tts() -> JarbisTTS:
    """Devuelve la instancia global de TTS."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = JarbisTTS()
    return _instance


def speak(text: str, block: bool = True) -> bool:
    """Atajo para decir algo con la instancia global."""
    return get_tts().speak(text, block)
