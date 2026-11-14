"""
Detector de Wake Word para GesturePilot.

Permite activar la grabación de voz diciendo "Hey Jarvis" o similar.

Usa OpenWakeWord si está disponible (recomendado), o un fallback
basado en Whisper que escucha continuamente.

OpenWakeWord:
- Modelo pre-entrenado para "Hey Jarvis"
- Muy eficiente (puede correr en Raspberry Pi)
- Baja latencia

Fallback Whisper:
- Usa faster-whisper en modo tiny
- Escucha en chunks de 2 segundos
- Busca "jarvis" en la transcripción
"""

import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Optional

import numpy as np
from loguru import logger

# Intentar importar OpenWakeWord
try:
    import openwakeword
    from openwakeword.model import Model as OwwModel
    HAS_OPENWAKEWORD = True
    # Algunas versiones no tienen __version__
    oww_version = getattr(openwakeword, "__version__", "unknown")
    logger.info(f"OpenWakeWord available (version {oww_version})")
except ImportError:
    HAS_OPENWAKEWORD = False
    logger.info("OpenWakeWord not available, will use Whisper fallback")

# Intentar importar sounddevice para captura de audio
try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False
    logger.warning("sounddevice not available, wake word detection disabled")


class WakeWordStatus(Enum):
    """Estado del detector de wake word."""
    STOPPED = auto()
    LISTENING = auto()
    DETECTED = auto()
    ERROR = auto()


@dataclass
class WakeWordResult:
    """Resultado de una detección de wake word."""
    detected: bool
    word: str
    confidence: float
    timestamp: float


# Callback type
WakeWordCallback = Callable[[WakeWordResult], None]


class OpenWakeWordDetector:
    """
    Detector de wake word usando OpenWakeWord.

    Modelos pre-entrenados disponibles:
    - "hey_jarvis": Para activar con "Hey Jarvis"
    - "alexa": Para activar con "Alexa"
    - "hey_mycroft": Para activar con "Hey Mycroft"
    - Muchos más en https://github.com/dscripka/openWakeWord
    """

    def __init__(
        self,
        model_name: str = "hey_jarvis",
        threshold: float = 0.5,
        sample_rate: int = 16000,
    ):
        """
        Inicializa el detector.

        Args:
            model_name: Nombre del modelo a usar
            threshold: Umbral de confianza (0.0 a 1.0)
            sample_rate: Tasa de muestreo del audio
        """
        self.model_name = model_name
        self.threshold = threshold
        self.sample_rate = sample_rate

        # Cargar modelo
        logger.info(f"Loading OpenWakeWord model: {model_name}")
        self._model = OwwModel(
            wakeword_models=[model_name],
            inference_framework="onnx",
        )
        logger.info(f"OpenWakeWord model loaded: {model_name}")

        # Estado
        self._status = WakeWordStatus.STOPPED
        self._callbacks: list[WakeWordCallback] = []

        # Audio
        self._audio_queue: queue.Queue = queue.Queue()
        self._stream: Optional[sd.InputStream] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def on_wake_word(self, callback: WakeWordCallback) -> None:
        """Registra un callback para cuando se detecta el wake word."""
        self._callbacks.append(callback)

    def start(self) -> bool:
        """Inicia la escucha de wake word."""
        if not HAS_SOUNDDEVICE:
            logger.error("Cannot start wake word: sounddevice not available")
            return False

        if self._status == WakeWordStatus.LISTENING:
            return True

        logger.info(f"Starting wake word detection: {self.model_name}")

        self._stop_event.clear()

        # Callback para el stream de audio
        def audio_callback(indata, frames, time_info, status):
            if status:
                logger.warning(f"Audio status: {status}")
            self._audio_queue.put(indata.copy())

        # Iniciar stream
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype=np.int16,
                blocksize=1280,  # 80ms a 16kHz (recomendado por OpenWakeWord)
                callback=audio_callback,
            )
            self._stream.start()
        except Exception as e:
            logger.error(f"Failed to start audio stream: {e}")
            self._status = WakeWordStatus.ERROR
            return False

        # Thread de procesamiento
        self._listen_thread = threading.Thread(
            target=self._listen_loop,
            name="WakeWord-Listener",
            daemon=True,
        )
        self._listen_thread.start()

        self._status = WakeWordStatus.LISTENING
        logger.info("Wake word detection started")
        return True

    def stop(self) -> None:
        """Detiene la escucha de wake word."""
        if self._status == WakeWordStatus.STOPPED:
            return

        logger.info("Stopping wake word detection")

        self._stop_event.set()

        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if self._listen_thread:
            self._listen_thread.join(timeout=2.0)
            self._listen_thread = None

        self._status = WakeWordStatus.STOPPED
        logger.info("Wake word detection stopped")

    def _listen_loop(self) -> None:
        """Loop de escucha que procesa audio y detecta wake word."""
        logger.debug("Wake word listen loop started")

        while not self._stop_event.is_set():
            try:
                # Obtener audio del queue
                audio = self._audio_queue.get(timeout=0.1)

                # Convertir a formato esperado por OpenWakeWord
                # OpenWakeWord espera audio como int16
                audio_flat = audio.flatten()

                # Predecir
                prediction = self._model.predict(audio_flat)

                # Verificar si se detectó wake word
                for model_name, scores in prediction.items():
                    # scores puede ser un array, tomar el último valor
                    score = scores[-1] if hasattr(scores, '__len__') else scores

                    if score >= self.threshold:
                        logger.info(f"Wake word detected: {model_name} (score={score:.3f})")
                        self._status = WakeWordStatus.DETECTED

                        result = WakeWordResult(
                            detected=True,
                            word=model_name,
                            confidence=float(score),
                            timestamp=time.time(),
                        )

                        # Notificar callbacks
                        for cb in self._callbacks:
                            try:
                                cb(result)
                            except Exception as e:
                                logger.error(f"Wake word callback error: {e}")

                        # Reset el modelo para evitar detecciones repetidas
                        self._model.reset()
                        self._status = WakeWordStatus.LISTENING

            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in wake word loop: {e}")

        logger.debug("Wake word listen loop ended")

    @property
    def status(self) -> WakeWordStatus:
        """Estado actual del detector."""
        return self._status

    @property
    def is_listening(self) -> bool:
        """True si está escuchando."""
        return self._status == WakeWordStatus.LISTENING


class WhisperWakeWordDetector:
    """
    Detector de wake word usando Whisper como fallback.

    Escucha continuamente en chunks de 2 segundos y busca
    "jarvis" en la transcripción.

    Más pesado que OpenWakeWord pero funciona sin dependencias adicionales.
    """

    def __init__(
        self,
        wake_words: list[str] | None = None,
        model_size: str = "tiny",
        sample_rate: int = 16000,
        chunk_duration: float = 1.5,  # Segundos por chunk (reducido para menor latencia)
    ):
        """
        Inicializa el detector.

        Args:
            wake_words: Lista de palabras a detectar (default: ["jarvis"])
            model_size: Tamaño del modelo Whisper
            sample_rate: Tasa de muestreo
            chunk_duration: Duración de cada chunk de audio
        """
        # Variantes comunes de como Whisper transcribe "Jarvis" en español
        self.wake_words = wake_words or [
            "jarvis", "yarvis", "jarvi", "jarriz", "jarris",
            "carvis", "carves", "charvis", "jarbis", "yarbis",
        ]
        self.model_size = model_size
        self.sample_rate = sample_rate
        self.chunk_duration = chunk_duration

        # Transcriber (lazy loading)
        self._transcriber = None

        # Estado
        self._status = WakeWordStatus.STOPPED
        self._callbacks: list[WakeWordCallback] = []

        # Audio
        self._audio_buffer: list[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._buffer_lock = threading.Lock()

    def _get_transcriber(self):
        """Obtiene o crea el transcriber (lazy loading)."""
        if self._transcriber is None:
            logger.info(f"Loading Whisper model: {self.model_size}")
            from faster_whisper import WhisperModel
            self._transcriber = WhisperModel(
                self.model_size,
                device="cpu",
                compute_type="int8",
            )
            logger.info("Whisper model loaded for wake word detection")
        return self._transcriber

    def on_wake_word(self, callback: WakeWordCallback) -> None:
        """Registra un callback para cuando se detecta el wake word."""
        self._callbacks.append(callback)

    def start(self) -> bool:
        """Inicia la escucha de wake word."""
        if not HAS_SOUNDDEVICE:
            logger.error("Cannot start wake word: sounddevice not available")
            return False

        if self._status == WakeWordStatus.LISTENING:
            return True

        logger.info(f"Starting Whisper wake word detection: {self.wake_words}")

        self._stop_event.clear()

        # Callback para el stream de audio
        def audio_callback(indata, frames, time_info, status):
            if status:
                logger.warning(f"Audio status: {status}")
            with self._buffer_lock:
                self._audio_buffer.append(indata.copy())

        # Iniciar stream
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype=np.float32,
                blocksize=int(self.sample_rate * 0.1),  # 100ms blocks
                callback=audio_callback,
            )
            self._stream.start()
        except Exception as e:
            logger.error(f"Failed to start audio stream: {e}")
            self._status = WakeWordStatus.ERROR
            return False

        # Thread de procesamiento
        self._listen_thread = threading.Thread(
            target=self._listen_loop,
            name="WakeWord-Whisper",
            daemon=True,
        )
        self._listen_thread.start()

        self._status = WakeWordStatus.LISTENING
        logger.info("Whisper wake word detection started")
        return True

    def stop(self) -> None:
        """Detiene la escucha de wake word."""
        if self._status == WakeWordStatus.STOPPED:
            return

        logger.info("Stopping Whisper wake word detection")

        self._stop_event.set()

        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if self._listen_thread:
            self._listen_thread.join(timeout=2.0)
            self._listen_thread = None

        self._status = WakeWordStatus.STOPPED
        logger.info("Whisper wake word detection stopped")

    def _listen_loop(self) -> None:
        """Loop de escucha que procesa chunks de audio."""
        logger.debug("Whisper wake word loop started")

        samples_per_chunk = int(self.sample_rate * self.chunk_duration)

        while not self._stop_event.is_set():
            time.sleep(0.1)  # Small delay

            # Verificar si tenemos suficiente audio
            with self._buffer_lock:
                if not self._audio_buffer:
                    continue

                # Concatenar buffers
                audio = np.concatenate(self._audio_buffer)

                if len(audio) < samples_per_chunk:
                    continue

                # Tomar chunk y limpiar buffer
                chunk = audio[:samples_per_chunk]
                remaining = audio[samples_per_chunk:]
                self._audio_buffer = [remaining] if len(remaining) > 0 else []

            # Transcribir chunk
            try:
                transcriber = self._get_transcriber()
                segments, _ = transcriber.transcribe(
                    chunk.flatten(),
                    language="es",
                    beam_size=1,
                    vad_filter=False,
                )

                text = " ".join(s.text for s in segments).lower().strip()

                if text:
                    logger.info(f"🎧 Wake word check: '{text}'")

                    # Buscar wake words
                    for word in self.wake_words:
                        if word.lower() in text:
                            logger.info(f"Wake word detected via Whisper: '{word}' in '{text}'")
                            self._status = WakeWordStatus.DETECTED

                            result = WakeWordResult(
                                detected=True,
                                word=word,
                                confidence=0.8,  # Whisper no da confianza por palabra
                                timestamp=time.time(),
                            )

                            for cb in self._callbacks:
                                try:
                                    cb(result)
                                except Exception as e:
                                    logger.error(f"Wake word callback error: {e}")

                            self._status = WakeWordStatus.LISTENING
                            break

            except Exception as e:
                logger.error(f"Error transcribing for wake word: {e}")

        logger.debug("Whisper wake word loop ended")

    @property
    def status(self) -> WakeWordStatus:
        """Estado actual del detector."""
        return self._status

    @property
    def is_listening(self) -> bool:
        """True si está escuchando."""
        return self._status == WakeWordStatus.LISTENING


# =============================================================================
# Detector unificado
# =============================================================================

class WakeWordDetector:
    """
    Detector de wake word que elige automáticamente el backend.

    Prioridad:
    1. OpenWakeWord (si está instalado)
    2. Whisper fallback

    Uso:
    ----
    ```python
    detector = WakeWordDetector()

    def on_wake(result: WakeWordResult):
        print(f"Wake word detected: {result.word}")
        # Activar grabación...

    detector.on_wake_word(on_wake)
    detector.start()

    # ... escucha en background ...

    detector.stop()
    ```
    """

    def __init__(
        self,
        prefer_openwakeword: bool = True,
        model_name: str = "hey_jarvis",
        threshold: float = 0.5,
        whisper_wake_words: list[str] | None = None,
    ):
        """
        Inicializa el detector.

        Args:
            prefer_openwakeword: Usar OpenWakeWord si está disponible
            model_name: Modelo de OpenWakeWord a usar
            threshold: Umbral de confianza
            whisper_wake_words: Palabras a detectar con Whisper fallback
        """
        self._impl: OpenWakeWordDetector | WhisperWakeWordDetector

        if prefer_openwakeword and HAS_OPENWAKEWORD:
            logger.info("Using OpenWakeWord for wake word detection")
            self._impl = OpenWakeWordDetector(
                model_name=model_name,
                threshold=threshold,
            )
        else:
            logger.info("Using Whisper for wake word detection")
            self._impl = WhisperWakeWordDetector(
                wake_words=whisper_wake_words,
            )

    def on_wake_word(self, callback: WakeWordCallback) -> None:
        """Registra un callback para cuando se detecta el wake word."""
        self._impl.on_wake_word(callback)

    def start(self) -> bool:
        """Inicia la escucha."""
        return self._impl.start()

    def stop(self) -> None:
        """Detiene la escucha."""
        self._impl.stop()

    @property
    def status(self) -> WakeWordStatus:
        """Estado actual."""
        return self._impl.status

    @property
    def is_listening(self) -> bool:
        """True si está escuchando."""
        return self._impl.is_listening
