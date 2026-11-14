"""
VoiceMode - Grabación y transcripción de voz para GesturePilot.

Este módulo maneja:
1. AudioRecorder: Captura audio del micrófono
2. VoiceTranscriber: Transcribe audio a texto con Whisper
3. VoiceMode: Orquesta grabación + transcripción

Flujo:
------
1. Usuario hace gesto ✋ (LISTENING) → VoiceMode.start_recording()
2. Usuario habla: "Crea una función que sume dos números"
3. Usuario hace gesto ✊ (PROCESSING) → VoiceMode.stop_and_transcribe()
4. Whisper transcribe → retorna texto
5. Texto se envía a Claude Code

Dependencias:
- sounddevice: Captura de audio
- faster-whisper: Transcripción local (sin API key)
"""

import io
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Callable

import numpy as np
from loguru import logger

from jarbis_body.config import settings


class VoiceModeStatus(Enum):
    """Estado del módulo de voz."""
    IDLE = auto()           # Listo para grabar
    RECORDING = auto()      # Grabando audio
    TRANSCRIBING = auto()   # Transcribiendo con Whisper
    ERROR = auto()          # Error


@dataclass
class TranscriptionResult:
    """Resultado de una transcripción."""
    text: str                          # Texto transcrito
    language: str                      # Idioma detectado
    duration_s: float                  # Duración del audio
    transcription_time_s: float        # Tiempo que tomó transcribir
    confidence: float = 0.0            # Confianza promedio
    timestamp: datetime = field(default_factory=datetime.now)


# Callback type para cuando hay nueva transcripción
TranscriptionCallback = Callable[[TranscriptionResult], None]


class AudioRecorder:
    """
    Grabador de audio con VAD (Voice Activity Detection).

    Captura audio del micrófono y detecta silencio automáticamente.
    Cuando hay silencio por X segundos, dispara un callback.

    Ejemplo:
    --------
    ```python
    recorder = AudioRecorder(silence_timeout=3.0)
    recorder.on_silence = lambda: print("Silencio detectado!")
    recorder.start()
    # Cuando haya 3 segundos de silencio, se llama on_silence
    ```
    """

    def __init__(
        self,
        sample_rate: int | None = None,
        channels: int | None = None,
        silence_timeout: float | None = None,
        silence_threshold: float | None = None,
    ):
        """
        Inicializa el grabador con VAD.

        Args:
            sample_rate: Frecuencia de muestreo (default: settings.audio_sample_rate)
            channels: Número de canales (default: settings.audio_channels)
            silence_timeout: Segundos de silencio (default: settings.silence_timeout)
            silence_threshold: Umbral de volumen para silencio (default: 0.02)
        """
        self.sample_rate = sample_rate or settings.audio_sample_rate
        self.channels = channels or settings.audio_channels
        self.silence_timeout = silence_timeout or settings.silence_timeout
        self.silence_threshold = (
            silence_threshold
            if silence_threshold is not None
            else settings.silence_threshold
        )

        self._recording = False
        self._audio_chunks: list[np.ndarray] = []
        self._stream = None
        self._lock = threading.Lock()
        self._start_time: float | None = None

        # El detector de voz. Silero si está disponible; si no, el RMS heredado.
        # El RMS falló en el primer smoke test real: con umbral 0.02 no reconoció
        # una voz por debajo del umbral y cuatro de seis turnos corrieron entero.
        self._vad = None
        if settings.vad_backend == "silero":
            from jarbis_body.audio.vad import SileroVad

            candidate = SileroVad()
            if candidate.load():
                self._vad = candidate
            else:
                logger.warning("Silero no disponible; VAD por RMS (umbral fijo)")

        # Cuánto espera a que empieces a hablar antes de darse por vencido.
        # Distinto de silence_timeout, que es cuánto silencio cierra el turno
        # UNA VEZ que hablaste.
        self.max_wait = settings.vad_max_wait_s

        # En push-to-talk el VAD no cierra el turno: cierra soltar el botón.
        # Sigue detectando voz (para saber si dijiste algo), solo no decide.
        self.auto_close = True
        # Tope duro igual: un botón trabado no puede grabar para siempre.
        self.hard_limit = settings.recording_hard_limit_s

        # VAD
        self._last_sound_time: float | None = None
        self._silence_triggered = False
        # El arreglo que encontró el smoke test: el reloj del silencio no puede
        # arrancar al abrir el micrófono, porque entonces el turno se cierra
        # antes de que llegues a hablar. Se arma recién cuando hubo voz.
        self._heard_speech = False
        self.on_silence: callable | None = None  # Callback cuando hay silencio

        # Se loguea el valor RESUELTO, no el parámetro: antes decía "silence=Nones"
        # cuando venía de settings, que es justo cuando querés saber cuánto es.
        backend = "silero" if self._vad is not None else f"rms({self.silence_threshold})"
        logger.info(
            f"AudioRecorder: {self.sample_rate}Hz, {self.channels}ch, "
            f"vad={backend}, silencio={self.silence_timeout}s, espera={self.max_wait}s"
        )

    def _audio_callback(self, indata, frames, time_info, status):
        """Callback llamado por sounddevice para cada chunk de audio."""
        if status:
            logger.warning(f"Audio status: {status}")

        if self._recording:
            with self._lock:
                self._audio_chunks.append(indata.copy())

            # VAD: ¿hay voz en este chunk?
            current_time = time.time()
            if self._vad is not None:
                hay_voz = self._vad.is_speech(indata)
            else:
                hay_voz = np.abs(indata).mean() > self.silence_threshold

            if hay_voz:
                # Hay voz. Recién acá se arma el cierre por silencio.
                self._last_sound_time = current_time
                self._silence_triggered = False
                if not self._heard_speech:
                    self._heard_speech = True
                    logger.debug("Voz detectada; el VAD queda armado")
                return

            if self._silence_triggered:
                return

            if not self.auto_close:
                # Push-to-talk: solo el tope duro puede cerrar desde acá.
                if (
                    self._start_time is not None
                    and current_time - self._start_time >= self.hard_limit
                ):
                    self._close_turn(f"tope de {self.hard_limit:.0f}s grabando")
                return

            if self._heard_speech:
                # Hablaste y te callaste: contar el silencio para cerrar.
                silence_duration = current_time - (self._last_sound_time or current_time)
                if silence_duration >= self.silence_timeout:
                    self._close_turn(f"silencio de {silence_duration:.1f}s")
            elif self._start_time is not None:
                # Todavía no dijiste nada. No se cierra por silencio —se cierra
                # recién si pasó max_wait sin una sola palabra, para no quedar
                # grabando para siempre con un micrófono muteado.
                waited = current_time - self._start_time
                if waited >= self.max_wait:
                    self._close_turn(f"{waited:.0f}s sin que digas nada")

    def _close_turn(self, reason: str) -> None:
        """Dispara el cierre del turno en otro thread, una sola vez."""
        self._silence_triggered = True
        if not self.on_silence:
            return
        logger.info(f"Cierro el turno: {reason}")
        # El callback transcribe, así que no puede correr en el thread de audio.
        threading.Thread(target=self.on_silence, daemon=True).start()

    def start(self) -> bool:
        """
        Inicia la grabación.

        Returns:
            True si inició correctamente
        """
        if self._recording:
            logger.warning("Already recording")
            return False

        try:
            import sounddevice as sd

            self._audio_chunks = []
            self._recording = True
            self._start_time = time.time()
            # NO se setea _last_sound_time acá: hacerlo arrancaba el reloj
            # del silencio antes de que hubiera voz, y el turno se cerraba en
            # 0,4 s si tardabas en hablar.
            self._last_sound_time = None
            self._silence_triggered = False
            self._heard_speech = False
            if self._vad is not None:
                self._vad.reset()

            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype=np.float32,
                device=settings.audio_input_device,
                callback=self._audio_callback,
            )
            self._stream.start()

            logger.info("Recording started")
            return True

        except Exception as e:
            logger.error(f"Failed to start recording: {e}")
            self._recording = False
            return False

    def stop(self) -> np.ndarray | None:
        """
        Detiene la grabación y retorna el audio.

        Returns:
            numpy array con el audio (float32), o None si hubo error
        """
        if not self._recording:
            logger.warning("Not recording")
            return None

        self._recording = False
        self._start_time = None  # Resetear timestamp

        try:
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None

            with self._lock:
                if not self._audio_chunks:
                    logger.warning("No audio recorded")
                    return None

                # Concatenar todos los chunks
                audio = np.concatenate(self._audio_chunks, axis=0)
                self._audio_chunks = []

            duration = len(audio) / self.sample_rate
            logger.info(f"Recording stopped: {duration:.1f}s, {len(audio)} samples")

            return audio

        except Exception as e:
            logger.error(f"Error stopping recording: {e}")
            return None

    @property
    def is_recording(self) -> bool:
        """True si está grabando."""
        return self._recording

    @property
    def duration(self) -> float:
        """Duración actual de la grabación en segundos."""
        if not self._audio_chunks:
            return 0.0
        with self._lock:
            total_samples = sum(len(chunk) for chunk in self._audio_chunks)
        return total_samples / self.sample_rate


class VoiceTranscriber:
    """
    Transcriptor de voz usando faster-whisper.

    Carga el modelo de Whisper una vez y permite transcribir
    múltiples audios sin recargar.

    Ejemplo:
    --------
    ```python
    transcriber = VoiceTranscriber()
    result = transcriber.transcribe(audio_array)
    print(result.text)  # "Crea una función que sume dos números"
    ```
    """

    def __init__(
        self,
        model_name: str | None = None,
        language: str | None = None,
        device: str = "auto",
    ):
        """
        Inicializa el transcriptor.

        Args:
            model_name: Nombre del modelo (tiny, base, small, medium, large)
            language: Idioma para transcripción (es, en, etc.)
            device: Dispositivo (auto, cpu, cuda)
        """
        self.model_name = model_name or settings.whisper_model
        self.language = language or settings.whisper_language
        self.device = device

        self._model = None
        self._model_loaded = False

        logger.info(f"VoiceTranscriber initialized: model={self.model_name}, lang={self.language}")

    def _ensure_model_loaded(self) -> bool:
        """Carga el modelo si no está cargado."""
        if self._model_loaded:
            return True

        try:
            from faster_whisper import WhisperModel

            logger.info(f"Loading Whisper model '{self.model_name}'...")
            start = time.time()

            # Determinar compute type basado en dispositivo
            # device y compute_type salen de settings: en este hardware no hay
            # GPU NVIDIA, así que siempre cpu/int8. Tenerlo en config permite
            # cambiarlo sin tocar código si algún día hay otra máquina.
            compute_type = settings.whisper_compute_type
            device = settings.whisper_device
            logger.info(
                f"Cargando Whisper '{self.model_name}' ({device}/{compute_type})..."
            )
            self._model = WhisperModel(
                self.model_name,
                device=device,
                compute_type=compute_type,
            )

            elapsed = time.time() - start
            logger.info(f"Model loaded in {elapsed:.1f}s")
            self._model_loaded = True
            return True

        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}")
            return False

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int | None = None,
    ) -> TranscriptionResult | None:
        """
        Transcribe un array de audio a texto.

        Args:
            audio: numpy array con audio (float32)
            sample_rate: Sample rate del audio (default: settings.audio_sample_rate)

        Returns:
            TranscriptionResult o None si hubo error
        """
        if not self._ensure_model_loaded():
            return None

        sample_rate = sample_rate or settings.audio_sample_rate

        try:
            start = time.time()

            # Asegurar que el audio sea mono y float32
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            audio = audio.astype(np.float32)

            # Transcribir.
            #
            # `initial_prompt` sesga el vocabulario de Whisper y no es opcional
            # acá: sin él "MyJarbis" se transcribía "MiFaris" de forma
            # consistente. Con él, bien. Es el arreglo del problema 1 de
            # PLAN_MEJORAS, y antes vivía solo en el banco de pruebas.
            #
            # `beam_size` sale de settings porque es LA palanca de latencia
            # medida: en este hardware beam 5 cuesta 2,8 s por frase y beam 1
            # cuesta 2,6 s, pero cambiar de modelo `small` a `base` baja a
            # 0,82 s. Ver docs/STT.md.
            segments, info = self._model.transcribe(
                audio,
                language=self.language,
                beam_size=settings.whisper_beam_size,
                vad_filter=True,  # Filtrar silencios
                initial_prompt=settings.whisper_initial_prompt or None,
            )

            # Concatenar segmentos
            text_parts = []
            confidences = []

            for segment in segments:
                text_parts.append(segment.text.strip())
                # avg_logprob es negativo, convertir a probabilidad
                if hasattr(segment, 'avg_logprob'):
                    confidences.append(np.exp(segment.avg_logprob))

            text = " ".join(text_parts)
            avg_confidence = np.mean(confidences) if confidences else 0.0

            elapsed = time.time() - start
            duration = len(audio) / sample_rate

            result = TranscriptionResult(
                text=text,
                language=info.language,
                duration_s=duration,
                transcription_time_s=elapsed,
                confidence=float(avg_confidence),
            )

            logger.info(
                f"Transcribed {duration:.1f}s audio in {elapsed:.1f}s: "
                f"'{text[:50]}{'...' if len(text) > 50 else ''}'"
            )

            return result

        except Exception as e:
            logger.error(f"Transcription error: {e}")
            return None

    def transcribe_file(self, file_path: str | Path) -> TranscriptionResult | None:
        """
        Transcribe un archivo de audio.

        Args:
            file_path: Ruta al archivo (wav, mp3, etc.)

        Returns:
            TranscriptionResult o None si hubo error
        """
        if not self._ensure_model_loaded():
            return None

        try:
            start = time.time()

            segments, info = self._model.transcribe(
                str(file_path),
                language=self.language,
                beam_size=settings.whisper_beam_size,
                initial_prompt=settings.whisper_initial_prompt or None,
                vad_filter=True,
            )

            text_parts = []
            confidences = []

            for segment in segments:
                text_parts.append(segment.text.strip())
                if hasattr(segment, 'avg_logprob'):
                    confidences.append(np.exp(segment.avg_logprob))

            text = " ".join(text_parts)
            avg_confidence = np.mean(confidences) if confidences else 0.0
            elapsed = time.time() - start

            result = TranscriptionResult(
                text=text,
                language=info.language,
                duration_s=info.duration,
                transcription_time_s=elapsed,
                confidence=float(avg_confidence),
            )

            logger.info(f"Transcribed file: '{text[:50]}...'")
            return result

        except Exception as e:
            logger.error(f"File transcription error: {e}")
            return None


class VoiceMode:
    """
    Orquestador de Voice Mode.

    Combina AudioRecorder y VoiceTranscriber para proveer
    una API simple de grabación + transcripción.

    Ejemplo:
    --------
    ```python
    voice = VoiceMode()

    # Callback cuando hay transcripción
    voice.on_transcription(lambda r: print(f"Dijiste: {r.text}"))

    # Iniciar grabación (cuando usuario hace ✋)
    voice.start_recording()

    # Detener y transcribir (cuando usuario hace ✊)
    result = voice.stop_and_transcribe()
    print(result.text)  # "Crea una función que sume dos números"
    ```
    """

    def __init__(
        self,
        model_name: str | None = None,
        language: str | None = None,
        preload_model: bool = False,
    ):
        """
        Inicializa VoiceMode.

        Args:
            model_name: Modelo de Whisper a usar
            language: Idioma para transcripción
            preload_model: Si True, carga el modelo inmediatamente
        """
        self._recorder = AudioRecorder()
        self._transcriber = VoiceTranscriber(
            model_name=model_name,
            language=language,
        )

        self._status = VoiceModeStatus.IDLE
        self._callbacks: list[TranscriptionCallback] = []
        self._last_result: TranscriptionResult | None = None

        if preload_model:
            self._transcriber._ensure_model_loaded()

        logger.info("VoiceMode initialized")

    @property
    def status(self) -> VoiceModeStatus:
        """Estado actual del VoiceMode."""
        return self._status

    @property
    def is_recording(self) -> bool:
        """True si está grabando."""
        return self._status == VoiceModeStatus.RECORDING

    @property
    def recording_duration(self) -> float:
        """Duración actual de grabación en segundos."""
        return self._recorder.duration

    @property
    def last_result(self) -> TranscriptionResult | None:
        """Último resultado de transcripción."""
        return self._last_result

    def on_transcription(self, callback: TranscriptionCallback) -> None:
        """
        Registra callback para cuando hay nueva transcripción.

        Args:
            callback: Función(TranscriptionResult)
        """
        self._callbacks.append(callback)

    def _notify_transcription(self, result: TranscriptionResult) -> None:
        """Notifica a todos los callbacks."""
        for cb in self._callbacks:
            try:
                cb(result)
            except Exception as e:
                logger.error(f"Error in transcription callback: {e}")

    def start_recording(self) -> bool:
        """
        Inicia grabación de audio.

        Returns:
            True si inició correctamente
        """
        if self._status != VoiceModeStatus.IDLE:
            logger.warning(f"Cannot start recording in status {self._status.name}")
            return False

        if self._recorder.start():
            self._status = VoiceModeStatus.RECORDING
            return True

        self._status = VoiceModeStatus.ERROR
        return False

    def stop_recording(self) -> np.ndarray | None:
        """
        Detiene grabación sin transcribir.

        Returns:
            Audio grabado o None
        """
        if self._status != VoiceModeStatus.RECORDING:
            logger.warning("Not recording")
            return None

        audio = self._recorder.stop()
        self._status = VoiceModeStatus.IDLE
        return audio

    def stop_and_transcribe(self) -> TranscriptionResult | None:
        """
        Detiene grabación y transcribe.

        Returns:
            TranscriptionResult o None si hubo error
        """
        if self._status != VoiceModeStatus.RECORDING:
            logger.warning("Not recording")
            return None

        # Detener grabación
        audio = self._recorder.stop()

        if audio is None or len(audio) < self._recorder.sample_rate * 0.5:
            # Menos de 0.5 segundos de audio
            logger.warning("Audio too short to transcribe")
            self._status = VoiceModeStatus.IDLE
            return None

        # Transcribir
        self._status = VoiceModeStatus.TRANSCRIBING
        result = self._transcriber.transcribe(audio)

        if result:
            self._last_result = result
            self._notify_transcription(result)

        self._status = VoiceModeStatus.IDLE
        return result

    def transcribe_audio(self, audio: np.ndarray) -> TranscriptionResult | None:
        """
        Transcribe audio sin grabar.

        Args:
            audio: numpy array con audio

        Returns:
            TranscriptionResult o None
        """
        if self._status == VoiceModeStatus.RECORDING:
            logger.warning("Cannot transcribe while recording")
            return None

        self._status = VoiceModeStatus.TRANSCRIBING
        result = self._transcriber.transcribe(audio)
        self._status = VoiceModeStatus.IDLE

        if result:
            self._last_result = result

        return result

    def preload_model(self) -> bool:
        """
        Pre-carga el modelo de Whisper.

        Útil para evitar el delay en la primera transcripción.

        Returns:
            True si se cargó correctamente
        """
        return self._transcriber._ensure_model_loaded()

    def to_dict(self) -> dict:
        """Convierte el estado a diccionario."""
        return {
            "status": self._status.name,
            "is_recording": self.is_recording,
            "recording_duration": round(self.recording_duration, 1),
            "model": self._transcriber.model_name,
            "language": self._transcriber.language,
            "last_result": {
                "text": self._last_result.text,
                "duration_s": self._last_result.duration_s,
                "confidence": round(self._last_result.confidence, 2),
            } if self._last_result else None,
        }
