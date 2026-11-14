"""
Wake word con el modelo ONNX de livekit-wakeword v2.

Reemplaza el fallback heredado de GesturePilot, que corría un Whisper entero
cada 1,5 segundos solo para escuchar el nombre y después comparaba contra una
lista de errores de transcripción ("jarvis", "yarvis", "carvis", "jarbis"...).
Eso funcionaba pero costaba un núcleo entero en reposo.

Este modelo cuesta ~16,5 ms por segundo de audio (1,7% de un núcleo) y marca 89%
de detección a 0,5 falsos positivos por hora, contra el 46% de openWakeWord.
Pesos y código bajo Apache-2.0, pesos incluidos (ver models/wakeword/NOTICE).

## La firma, que no está documentada en el repo

La saqué inspeccionando el .onnx. Es un modelo **streaming con estado**, no uno
que recibe una ventana y ya:

    entradas:  chunk [1,1280] float   ← audio crudo, 80 ms a 16 kHz
               audio [1,352], sub0, sub1, sub2, k, v, conv, ctx, step
    salidas:   score [1] float        ← la probabilidad
               audio_out, sub0_out, ... step_out

Cada salida `*_out` se realimenta como la entrada del paso siguiente. Si no se
realimentan, el modelo devuelve siempre lo mismo.

Y la parte que no es obvia y costó encontrar: **el estado no arranca en ceros,
arranca calentado**. Hay que correr 25 frames de silencio (2 segundos) antes de
darle audio real, porque así se entrenó. Arrancando de ceros crudos el modelo
devuelve ~0.048 para absolutamente todo, incluida una voz inglesa diciendo el
nombre perfecto: parece que anda, nunca dispara, y no hay forma de darse cuenta
sin comparar contra el cliente oficial. Después de detectar se vuelve al estado
caliente, no a ceros.

La metadata del archivo confirma: wake_word="hey jarvis", threshold=0.5,
format="livekit-wakeword-v2".
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import numpy as np
from loguru import logger

from jarbis_body.config import settings

# El modelo come exactamente 1280 samples por paso: 80 ms a 16 kHz.
CHUNK_SAMPLES = 1280
SAMPLE_RATE = 16_000

# Nombre de entrada → nombre de salida que lo realimenta.
_STATE_PAIRS = {
    "audio": "audio_out",
    "sub0": "sub0_out",
    "sub1": "sub1_out",
    "sub2": "sub2_out",
    "k": "k_out",
    "v": "v_out",
    "conv": "conv_out",
    "ctx": "ctx_out",
    "step": "step_out",
}

# Frames de silencio para calentar el estado antes de escuchar audio real.
# El valor sale del cliente oficial (WARMUP_FRAMES): 25 × 80 ms = 2 s.
_WARMUP_FRAMES = 25

# Después de detectar, cuántos pasos ignorar antes de volver a escuchar el
# nombre, para que no vuelva a disparar con la cola de la misma frase.
_COOLDOWN_STEPS = 25


class WakeWordStatus(str, Enum):
    STOPPED = "stopped"
    LISTENING = "listening"
    ERROR = "error"


@dataclass
class WakeWordResult:
    """Una detección."""

    word: str
    confidence: float
    timestamp: datetime


WakeWordCallback = Callable[[WakeWordResult], None]


class OnnxWakeWordDetector:
    """
    Detector streaming sobre el modelo de livekit.

    Push: te suscribís con `on_wake_word()` y te avisa desde su propio thread.

        det = OnnxWakeWordDetector()
        det.on_wake_word(lambda r: print("me llamaste", r.confidence))
        det.start()
    """

    def __init__(
        self,
        model_path: Path | None = None,
        threshold: float | None = None,
        device: int | None = None,
    ) -> None:
        self._model_path = Path(model_path or settings.wakeword_model_path)
        self._threshold = threshold if threshold is not None else settings.wakeword_threshold
        self._device = device if device is not None else settings.audio_input_device

        self._session = None
        self._word = "hey jarvis"
        self._state: dict[str, np.ndarray] = {}
        self._warm_state: dict[str, np.ndarray] = {}
        self._cooldown = 0

        self._callbacks: list[WakeWordCallback] = []
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
        self._thread: threading.Thread | None = None
        self._stream = None
        self._running = threading.Event()
        self._status = WakeWordStatus.STOPPED
        self._last_score = 0.0

    # ------------------------------------------------------------ suscripción

    def on_wake_word(self, callback: WakeWordCallback) -> None:
        self._callbacks.append(callback)

    @property
    def status(self) -> WakeWordStatus:
        return self._status

    @property
    def is_listening(self) -> bool:
        return self._status is WakeWordStatus.LISTENING

    @property
    def last_score(self) -> float:
        """Último score, útil para calibrar el umbral mirando el visor."""
        return self._last_score

    @property
    def wake_word(self) -> str:
        return self._word

    # -------------------------------------------------------------------- init

    def _load(self) -> bool:
        """Abre la sesión ONNX y prepara los tensores de estado en cero."""
        if self._session is not None:
            return True

        if not self._model_path.exists():
            logger.error(f"Modelo de wake word no encontrado: {self._model_path}")
            return False

        try:
            import onnxruntime as ort

            options = ort.SessionOptions()
            # Un solo hilo: el modelo es chico y corre continuo. Dejarle los 12
            # threads le roba CPU a lo que estés haciendo vos, que es justo lo
            # que un detector always-on no tiene que hacer.
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            # allow_spinning=0 evita que los hilos de ORT hagan busy-wait entre
            # chunks. Sin esto un modelo always-on quema CPU esperando.
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")

            self._session = ort.InferenceSession(
                str(self._model_path), options, providers=["CPUExecutionProvider"]
            )

            meta = self._session.get_modelmeta().custom_metadata_map or {}
            self._word = meta.get("wake_word", self._word)
            if "threshold" in meta:
                try:
                    # El umbral del archivo es la referencia; el de settings manda
                    # solo si lo cambiaste a mano.
                    if self._threshold == settings.wakeword_threshold == 0.5:
                        self._threshold = float(meta["threshold"])
                except ValueError:
                    pass

            self._warm_state: dict[str, np.ndarray] = {}
            self._warm_up()
            logger.info(
                f"Wake word ONNX cargado: '{self._word}' "
                f"(umbral {self._threshold}, {self._model_path.name})"
            )
            return True
        except Exception as exc:
            logger.error(f"No se pudo cargar el wake word: {exc}")
            self._session = None
            return False

    def _warm_up(self) -> None:
        """
        Construye el estado inicial: ceros + 25 frames de silencio.

        Esto es obligatorio. El modelo se entrenó partiendo de un estado ya
        calentado sobre silencio, así que alimentarlo desde ceros crudos lo deja
        en una región donde el score es constante (~0.048) y nunca cruza el
        umbral. El resultado se guarda para que `_reset_state()` vuelva acá sin
        pagar los 25 pasos otra vez.
        """
        assert self._session is not None

        self._state = {}
        for spec in self._session.get_inputs():
            if spec.name == "chunk":
                continue
            dtype = np.int64 if "int64" in spec.type else np.float32
            shape = tuple(d if isinstance(d, int) else 1 for d in spec.shape)
            self._state[spec.name] = np.zeros(shape, dtype=dtype)

        silence = np.zeros(CHUNK_SAMPLES, dtype=np.float32)
        for _ in range(_WARMUP_FRAMES):
            self._infer(silence)

        # Copia del estado caliente. Es el punto de partida de todo stream.
        self._warm_state = {k: v.copy() for k, v in self._state.items()}

    def _reset_state(self) -> None:
        """
        Vuelve al estado caliente, no a ceros.

        Se llama después de cada detección y antes de cada clip offline: sin
        esto el contexto de la frase anterior sesga la siguiente.
        """
        if not getattr(self, "_warm_state", None):
            self._warm_up()
            return
        self._state = {k: v.copy() for k, v in self._warm_state.items()}

    # ------------------------------------------------------------------ control

    def start(self) -> bool:
        """Empieza a escuchar el nombre de forma continua."""
        if self.is_listening:
            return True

        if not self._load():
            self._status = WakeWordStatus.ERROR
            return False

        try:
            import sounddevice as sd
        except Exception as exc:
            logger.error(f"sounddevice no disponible: {exc}")
            self._status = WakeWordStatus.ERROR
            return False

        self._running.set()
        self._thread = threading.Thread(
            target=self._consume, name="JarbisWakeWord", daemon=True
        )
        self._thread.start()

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=CHUNK_SAMPLES,
                device=self._device,
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as exc:
            logger.error(f"No se pudo abrir el micrófono: {exc}")
            self._running.clear()
            self._status = WakeWordStatus.ERROR
            return False

        self._status = WakeWordStatus.LISTENING
        logger.info(f"Escuchando '{self._word}'")
        return True

    def stop(self) -> None:
        """Deja de escuchar y libera el micrófono."""
        self._running.clear()

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                logger.debug(f"Cerrando el stream del wake word: {exc}")
            self._stream = None

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        self._status = WakeWordStatus.STOPPED
        logger.info("Wake word detenido")

    # -------------------------------------------------------------------- audio

    def _on_audio(self, indata, frames, time_info, status) -> None:
        """
        Callback de sounddevice. Corre en el thread de audio de PortAudio, así que
        acá NO se infiere: se encola y se vuelve. Bloquear este callback produce
        cortes audibles.
        """
        if status:
            logger.debug(f"Estado del stream de audio: {status}")
        try:
            self._queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            # Se tiró un chunk. Mejor perder 80 ms que acumular latencia.
            pass

    def _consume(self) -> None:
        """Thread de inferencia: saca chunks de la cola y los pasa por el modelo."""
        while self._running.is_set():
            try:
                chunk = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                score = self._infer(chunk)
            except Exception as exc:
                logger.error(f"Falló la inferencia del wake word: {exc}")
                continue

            self._last_score = score

            if self._cooldown > 0:
                self._cooldown -= 1
                continue

            if score >= self._threshold:
                self._cooldown = _COOLDOWN_STEPS
                self._reset_state()
                self._emit(score)

    def _infer(self, chunk: np.ndarray) -> float:
        """Un paso del modelo, realimentando los nueve tensores de estado."""
        assert self._session is not None

        # El stream puede entregar un bloque corto al cerrar; se rellena.
        if len(chunk) < CHUNK_SAMPLES:
            chunk = np.pad(chunk, (0, CHUNK_SAMPLES - len(chunk)))

        feeds = {"chunk": chunk[:CHUNK_SAMPLES].reshape(1, CHUNK_SAMPLES).astype(np.float32)}
        feeds.update(self._state)

        outputs = self._session.run(None, feeds)
        names = [o.name for o in self._session.get_outputs()]
        result = dict(zip(names, outputs))

        # Realimentación: sin esto el score es basura.
        for input_name, output_name in _STATE_PAIRS.items():
            if output_name in result:
                self._state[input_name] = result[output_name]

        return float(np.asarray(result["score"]).reshape(-1)[0])

    def _emit(self, score: float) -> None:
        result = WakeWordResult(
            word=self._word, confidence=score, timestamp=datetime.now()
        )
        logger.info(f"Wake word detectado: '{self._word}' ({score:.3f})")
        for callback in self._callbacks:
            try:
                callback(result)
            except Exception as exc:
                logger.error(f"Callback de wake word falló: {exc}")

    # ------------------------------------------------------------------ offline

    def score_audio(self, audio: np.ndarray) -> float:
        """
        Pasa un buffer completo por el modelo y devuelve el score máximo.

        Es la forma de verificar el detector sin micrófono: se le da un wav
        sintetizado con Piper diciendo el nombre y se mira si pasa el umbral.
        Lo usa el banco de pruebas.
        """
        if not self._load():
            return 0.0

        self._reset_state()
        best = 0.0
        for start in range(0, len(audio) - CHUNK_SAMPLES + 1, CHUNK_SAMPLES):
            score = self._infer(audio[start : start + CHUNK_SAMPLES])
            best = max(best, score)
        return best
