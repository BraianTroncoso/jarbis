"""
Detector de presencia usando MediaPipe Face Detection.

Este módulo detecta si hay una persona frente a la cámara.
Se usa para:
- Activar el sistema solo cuando el usuario está presente
- Ignorar wake words cuando no hay nadie (evita falsos positivos)
- Pausar detección cuando el usuario se aleja

MediaPipe Face Detection es muy eficiente y usa poca CPU.
Solo necesitamos saber SI hay una cara, no sus detalles.
"""

import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from loguru import logger

from jarbis_body.audio.camera import Frame

# Importar MediaPipe
import mediapipe as mp

# Verificar APIs disponibles
HAS_LEGACY_API = hasattr(mp, "solutions") and hasattr(mp.solutions, "face_detection")
HAS_TASKS_API = hasattr(mp, "tasks")

logger.debug(f"Face Detection - Legacy API: {HAS_LEGACY_API}, Tasks API: {HAS_TASKS_API}")


@dataclass
class PresenceResult:
    """
    Resultado de la detección de presencia.

    Attributes:
        detected: True si se detectó una persona
        confidence: Confianza de la detección (0.0 a 1.0)
        face_count: Número de caras detectadas
        timestamp: Timestamp de la detección
    """
    detected: bool = False
    confidence: float = 0.0
    face_count: int = 0
    timestamp: float = 0.0

    def __bool__(self) -> bool:
        """Permite usar el resultado como booleano."""
        return self.detected


# =============================================================================
# Detector con API Legacy (mp.solutions.face_detection)
# =============================================================================

class PresenceDetectorLegacy:
    """Detector de presencia usando la API legacy de MediaPipe."""

    def __init__(
        self,
        min_detection_confidence: float = 0.5,
        model_selection: int = 0,  # 0 = corto alcance, 1 = largo alcance
    ):
        self.min_detection_confidence = min_detection_confidence
        self.model_selection = model_selection

        self._face_detection = mp.solutions.face_detection.FaceDetection(
            min_detection_confidence=min_detection_confidence,
            model_selection=model_selection,
        )

        logger.info(f"PresenceDetector initialized (Legacy API, confidence={min_detection_confidence})")

    def detect(self, frame: Frame) -> PresenceResult:
        """Detecta presencia en un frame."""
        rgb_image = frame.rgb
        results = self._face_detection.process(rgb_image)

        if results.detections is None or len(results.detections) == 0:
            return PresenceResult(
                detected=False,
                confidence=0.0,
                face_count=0,
                timestamp=time.time(),
            )

        # Obtener la mayor confianza
        max_confidence = max(d.score[0] for d in results.detections)

        return PresenceResult(
            detected=True,
            confidence=max_confidence,
            face_count=len(results.detections),
            timestamp=time.time(),
        )

    def close(self) -> None:
        """Libera recursos."""
        self._face_detection.close()
        logger.info("PresenceDetector closed (Legacy)")


# =============================================================================
# Detector con API Nueva (mp.tasks.vision.FaceDetector)
# =============================================================================

# URL del modelo de detección de caras
FACE_DETECTOR_MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
MODEL_DIR = Path(__file__).parent.parent / "gesture" / "models"
FACE_MODEL_PATH = MODEL_DIR / "blaze_face_short_range.tflite"


def download_face_model_if_needed() -> Path:
    """Descarga el modelo de detección de caras si no existe."""
    if FACE_MODEL_PATH.exists():
        return FACE_MODEL_PATH

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Downloading face detector model to {FACE_MODEL_PATH}...")

    try:
        urllib.request.urlretrieve(FACE_DETECTOR_MODEL_URL, FACE_MODEL_PATH)
        logger.info("Face model downloaded successfully")
    except Exception as e:
        logger.error(f"Failed to download face model: {e}")
        raise

    return FACE_MODEL_PATH


class PresenceDetectorTasks:
    """Detector de presencia usando la API nueva de MediaPipe (mp.tasks)."""

    def __init__(
        self,
        min_detection_confidence: float = 0.5,
    ):
        self.min_detection_confidence = min_detection_confidence

        # Descargar modelo si es necesario
        model_path = download_face_model_if_needed()

        from mediapipe.tasks import python as mp_tasks
        from mediapipe.tasks.python import vision

        base_options = mp_tasks.BaseOptions(model_asset_path=str(model_path))
        options = vision.FaceDetectorOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.IMAGE,
            min_detection_confidence=min_detection_confidence,
        )

        self._detector = vision.FaceDetector.create_from_options(options)
        logger.info(f"PresenceDetector initialized (Tasks API, confidence={min_detection_confidence})")

    def detect(self, frame: Frame) -> PresenceResult:
        """Detecta presencia en un frame."""
        from mediapipe import Image, ImageFormat

        rgb_image = frame.rgb
        mp_image = Image(image_format=ImageFormat.SRGB, data=rgb_image)

        results = self._detector.detect(mp_image)

        if not results.detections:
            return PresenceResult(
                detected=False,
                confidence=0.0,
                face_count=0,
                timestamp=time.time(),
            )

        # Obtener la mayor confianza
        max_confidence = max(d.categories[0].score for d in results.detections)

        return PresenceResult(
            detected=True,
            confidence=max_confidence,
            face_count=len(results.detections),
            timestamp=time.time(),
        )

    def close(self) -> None:
        """Libera recursos."""
        self._detector.close()
        logger.info("PresenceDetector closed (Tasks)")


# =============================================================================
# Detector unificado
# =============================================================================

class PresenceDetector:
    """
    Detector de presencia que elige automáticamente la API correcta.

    Uso:
    ----
    ```python
    detector = PresenceDetector()

    with Camera() as cam:
        for frame in cam.stream():
            presence = detector.detect(frame)
            if presence:
                print(f"Usuario presente (confianza: {presence.confidence:.2f})")
            else:
                print("No hay nadie")
    ```
    """

    def __init__(
        self,
        min_detection_confidence: float = 0.5,
        absence_timeout: float = 3.0,  # Segundos sin cara para considerar ausente
    ):
        """
        Inicializa el detector.

        Args:
            min_detection_confidence: Confianza mínima para detectar cara
            absence_timeout: Segundos sin detectar cara para reportar ausencia
        """
        self.min_detection_confidence = min_detection_confidence
        self.absence_timeout = absence_timeout

        # Estado
        self._last_seen: float = 0.0
        self._is_present: bool = False

        # Elegir implementación
        if HAS_LEGACY_API:
            self._impl = PresenceDetectorLegacy(
                min_detection_confidence=min_detection_confidence,
            )
        elif HAS_TASKS_API:
            self._impl = PresenceDetectorTasks(
                min_detection_confidence=min_detection_confidence,
            )
        else:
            raise RuntimeError(
                "MediaPipe no tiene una API de Face Detection compatible."
            )

        logger.info(
            f"PresenceDetector ready: confidence={min_detection_confidence}, "
            f"timeout={absence_timeout}s"
        )

    def detect(self, frame: Frame) -> PresenceResult:
        """
        Detecta presencia con histéresis (evita parpadeo).

        La detección tiene un timeout: si no se ve cara por N segundos,
        entonces se considera ausente. Esto evita falsos negativos
        cuando el usuario voltea brevemente o parpadea.
        """
        result = self._impl.detect(frame)
        now = time.time()

        if result.detected:
            self._last_seen = now
            self._is_present = True
        else:
            # Verificar si pasó el timeout
            if now - self._last_seen > self.absence_timeout:
                self._is_present = False

        # Retornar resultado con histéresis aplicada
        return PresenceResult(
            detected=self._is_present,
            confidence=result.confidence if result.detected else 0.0,
            face_count=result.face_count,
            timestamp=now,
        )

    def detect_raw(self, frame: Frame) -> PresenceResult:
        """Detecta presencia sin histéresis (resultado directo)."""
        return self._impl.detect(frame)

    @property
    def is_present(self) -> bool:
        """True si el usuario está presente (con histéresis)."""
        return self._is_present

    @property
    def seconds_since_seen(self) -> float:
        """Segundos desde la última vez que se vio al usuario."""
        if self._last_seen == 0:
            return float('inf')
        return time.time() - self._last_seen

    def close(self) -> None:
        """Libera recursos."""
        self._impl.close()


# =============================================================================
# Clase de conveniencia para chequeo rápido
# =============================================================================

class PresenceChecker:
    """
    Checker de presencia que corre en background y mantiene estado.

    Uso típico en GesturePilot:
    - Corre continuamente en el thread de detección
    - Actualiza flag `user_present`
    - Otros componentes consultan este flag

    ```python
    checker = PresenceChecker()
    checker.start()

    # En otro lugar...
    if checker.is_present:
        # Usuario está, activar wake word
        pass

    checker.stop()
    ```
    """

    def __init__(
        self,
        min_detection_confidence: float = 0.5,
        absence_timeout: float = 3.0,
        check_interval: float = 0.5,  # Cada cuántos segundos verificar
    ):
        self.min_detection_confidence = min_detection_confidence
        self.absence_timeout = absence_timeout
        self.check_interval = check_interval

        self._detector: Optional[PresenceDetector] = None
        self._is_present: bool = False
        self._confidence: float = 0.0
        self._last_check: float = 0.0

    def check(self, frame: Frame) -> bool:
        """
        Verifica presencia si ha pasado el intervalo.

        Llamar en cada frame; internamente controla la frecuencia.

        Args:
            frame: Frame de la cámara

        Returns:
            True si el usuario está presente
        """
        now = time.time()

        # Verificar solo cada check_interval segundos
        if now - self._last_check < self.check_interval:
            return self._is_present

        self._last_check = now

        # Inicializar detector lazy
        if self._detector is None:
            self._detector = PresenceDetector(
                min_detection_confidence=self.min_detection_confidence,
                absence_timeout=self.absence_timeout,
            )

        result = self._detector.detect(frame)
        self._is_present = result.detected
        self._confidence = result.confidence

        return self._is_present

    @property
    def is_present(self) -> bool:
        """True si el usuario está presente."""
        return self._is_present

    @property
    def confidence(self) -> float:
        """Confianza de la última detección."""
        return self._confidence

    def close(self) -> None:
        """Libera recursos."""
        if self._detector:
            self._detector.close()
            self._detector = None
