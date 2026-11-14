"""
Detección de voz con Silero VAD.

## Por qué existe esto

El VAD heredado de GesturePilot compara el RMS del chunk contra un umbral fijo
(`0.02`). En el primer smoke test real eso falló de la peor manera posible:
**cuatro de seis turnos cerraron por "8 segundos sin que digas nada"**, con
alguien hablando. Ese micrófono entregaba la voz por debajo de 0.02, así que el VAD
nunca se armaba, el turno corría el `max_wait` completo, y cada frase cargaba
casi cinco segundos de tiempo muerto.

Un umbral fijo de RMS es intrínsecamente frágil: depende del micrófono, de la
ganancia del sistema, de a qué distancia estás y de cuánto ruido de fondo hay.
Bajarlo lo arregla para una máquina y lo rompe en la siguiente, o empieza a
disparar con el ventilador.

Silero VAD es un modelo entrenado para esto. No le importa el nivel absoluto:
distingue voz de no-voz. 2 MB, MIT, corre en ONNX en microsegundos por ventana.
El modelo ya estaba descargado y configurado como default desde la primera
noche — lo que faltaba era esta implementación, así que el código seguía usando
el RMS.

## La firma, que tampoco está documentada

Sacada inspeccionando el .onnx y después sondeándolo:

    entradas:  input [batch, samples]   ← 576: 64 de contexto + 512 nuevos
               state [2, batch, 128]    ← se realimenta
               sr    int64 escalar
    salidas:   output [batch, 1]        ← probabilidad de voz
               stateN                   ← el estado nuevo

Dos requisitos, ninguno documentado, y los dos silenciosos si los erras:

1. `stateN` se realimenta como `state`. Streaming con estado, igual que el
   wake word.
2. **La entrada son 576 samples, no 512.** Los primeros 64 son los últimos 64
   samples de la ventana anterior. Esto no tira error: el shape es dinámico, así
   que el modelo acepta 512 y devuelve números plausibles pero inútiles. Medido
   con la misma voz sintetizada:

       512 pelados          → probabilidad máxima 0.150, cero ventanas de voz
       512 + 64 de contexto → probabilidad máxima 1.000, 71 de 76 ventanas

   Con 512 el detector parece andar (rechaza bien el silencio y el ruido) pero
   nunca dice "voz". Es exactamente el modo de falla que se arregló en el wake
   word: plausible, callado y mortal.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from loguru import logger

# Silero v5 avanza 512 samples por paso (32 ms a 16 kHz)...
WINDOW_SAMPLES = 512
# ...pero recibe 576: los 64 últimos de la ventana anterior como contexto.
# Sin esos 64 el modelo nunca cruza el umbral. Ver el encabezado.
CONTEXT_SAMPLES = 64
SAMPLE_RATE = 16_000

# Probabilidad por encima de la cual se considera voz. 0.5 es el default del
# modelo y es razonable; es un umbral sobre una probabilidad entrenada, no sobre
# un nivel de audio, así que no depende del micrófono.
DEFAULT_THRESHOLD = 0.5


class SileroVad:
    """
    Detector de voz. Le das audio, te dice si hay alguien hablando.

        vad = SileroVad()
        if vad.is_speech(chunk):
            ...

    Pensado para llamarse desde el callback de audio: mantiene su propio buffer
    de restos, así que acepta chunks de cualquier tamaño.
    """

    def __init__(
        self,
        model_path: Path | None = None,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        from jarbis_body.config import settings

        self._model_path = Path(model_path or settings.vad_model_path)
        self._threshold = threshold
        self._session = None
        self._state: np.ndarray | None = None
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._last_prob = 0.0
        self._available = False

    # -------------------------------------------------------------------- init

    def load(self) -> bool:
        """Abre la sesión ONNX. Devuelve False si el modelo no está."""
        if self._session is not None:
            return True

        if not self._model_path.exists():
            logger.warning(
                f"Silero VAD no encontrado en {self._model_path}; "
                "se usa el VAD por RMS"
            )
            return False

        try:
            import onnxruntime as ort

            options = ort.SessionOptions()
            # Un hilo: el modelo es diminuto y corre en cada ventana de 32 ms.
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")

            self._session = ort.InferenceSession(
                str(self._model_path), options, providers=["CPUExecutionProvider"]
            )
            self.reset()
            self._available = True
            logger.info(f"Silero VAD cargado (umbral {self._threshold})")
            return True
        except Exception as exc:
            logger.error(f"No se pudo cargar Silero VAD: {exc}")
            self._session = None
            return False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def last_probability(self) -> float:
        """Última probabilidad de voz. Útil para calibrar mirando el visor."""
        return self._last_prob

    def reset(self) -> None:
        """Olvida el contexto. Se llama al empezar cada turno."""
        with self._lock:
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
            self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
            self._pending = np.zeros(0, dtype=np.float32)
            self._last_prob = 0.0

    # --------------------------------------------------------------- detección

    def is_speech(self, chunk: np.ndarray) -> bool:
        """
        True si en este chunk hay voz.

        Acepta cualquier largo: lo que no completa una ventana de 512 queda
        guardado para la próxima llamada. Si alguna ventana del chunk pasa el
        umbral, cuenta como voz — para abrir el turno conviene ser optimista,
        porque el costo de un falso positivo es grabar un segundo de más y el de
        un falso negativo es perderte la frase.
        """
        if self._session is None:
            return False

        audio = np.asarray(chunk, dtype=np.float32).reshape(-1)

        with self._lock:
            self._pending = np.concatenate([self._pending, audio])
            windows = []
            while len(self._pending) >= WINDOW_SAMPLES:
                windows.append(self._pending[:WINDOW_SAMPLES])
                self._pending = self._pending[WINDOW_SAMPLES:]

            if not windows:
                return False

            best = 0.0
            for window in windows:
                try:
                    best = max(best, self._infer(window))
                except Exception as exc:
                    logger.debug(f"Silero falló en una ventana: {exc}")
                    return False

            self._last_prob = best
            return best >= self._threshold

    def _infer(self, window: np.ndarray) -> float:
        """
        Una ventana de 512. Le prepende los 64 de contexto y realimenta estado y
        contexto. Las dos realimentaciones son obligatorias.
        """
        assert self._session is not None

        entrada = np.concatenate([self._context, window])
        outputs = self._session.run(
            None,
            {
                "input": entrada.reshape(1, CONTEXT_SAMPLES + WINDOW_SAMPLES),
                "state": self._state,
                "sr": np.array(SAMPLE_RATE, dtype=np.int64),
            },
        )
        probability = float(np.asarray(outputs[0]).reshape(-1)[0])
        self._state = outputs[1]
        self._context = window[-CONTEXT_SAMPLES:].copy()
        return probability

    # ------------------------------------------------------------------ offline

    def probabilities(self, audio: np.ndarray) -> list[float]:
        """
        Probabilidad de cada ventana de un buffer completo.

        Para calibrar sin micrófono: se le pasa un wav y se mira la distribución.
        """
        if not self.load():
            return []

        self.reset()
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        out = []
        with self._lock:
            for start in range(0, len(audio) - WINDOW_SAMPLES + 1, WINDOW_SAMPLES):
                out.append(self._infer(audio[start : start + WINDOW_SAMPLES]))
        return out
