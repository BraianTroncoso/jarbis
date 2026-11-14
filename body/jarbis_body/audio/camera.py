"""
Módulo de captura de cámara usando OpenCV.

OpenCV (Open Computer Vision) es la librería estándar para trabajar con
imágenes y video en Python. Este módulo encapsula toda la lógica de:
- Abrir/cerrar la cámara
- Capturar frames
- Configurar resolución y FPS

Conceptos importantes:
---------------------
1. Context Manager (__enter__/__exit__):
   Permite usar "with Camera() as cam:" para garantizar que la cámara
   se libere correctamente, incluso si hay errores.

2. Frame:
   Una imagen capturada de la cámara. Es un array NumPy de shape
   (height, width, 3) donde 3 son los canales BGR (Blue, Green, Red).
   Nota: OpenCV usa BGR, no RGB como la mayoría de librerías.

3. VideoCapture:
   Clase de OpenCV que maneja la conexión con la cámara.
"""

from dataclasses import dataclass
from typing import Generator

import cv2
import numpy as np
from loguru import logger

from jarbis_body.config import settings


@dataclass
class Frame:
    """
    Representa un frame capturado de la cámara.

    Usamos @dataclass para crear una clase simple que solo guarda datos.
    Es más limpio que usar un diccionario y tiene type hints.

    Attributes:
        image: Array NumPy con la imagen BGR (height, width, 3)
        width: Ancho en píxeles
        height: Alto en píxeles
        timestamp: Timestamp en milisegundos desde que se abrió la cámara
    """

    image: np.ndarray
    width: int
    height: int
    timestamp: float

    @property
    def rgb(self) -> np.ndarray:
        """
        Convierte la imagen de BGR a RGB.

        OpenCV usa BGR (Blue, Green, Red) por razones históricas,
        pero MediaPipe y la mayoría de librerías usan RGB.
        """
        return cv2.cvtColor(self.image, cv2.COLOR_BGR2RGB)


class CameraError(Exception):
    """Excepción para errores relacionados con la cámara."""

    pass


class Camera:
    """
    Maneja la captura de video desde una cámara.

    Uso básico:
    -----------
    ```python
    with Camera() as cam:
        for frame in cam.stream():
            # Procesar frame.image
            pass
    ```

    El "with" garantiza que la cámara se libere al terminar,
    incluso si hay una excepción.
    """

    def __init__(
        self,
        camera_index: int | None = None,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
    ):
        """
        Inicializa la cámara con configuración opcional.

        Args:
            camera_index: Índice de la cámara (0 = default). Si None, usa settings.
            width: Ancho de captura. Si None, usa settings.
            height: Alto de captura. Si None, usa settings.
            fps: Frames por segundo. Si None, usa settings.
        """
        # Usamos los valores de settings si no se especifican
        # El operador "if x is None" permite pasar 0 como valor válido
        self.camera_index = camera_index if camera_index is not None else settings.camera_index
        self.width = width if width is not None else settings.camera_width
        self.height = height if height is not None else settings.camera_height
        self.fps = fps if fps is not None else settings.camera_fps

        # VideoCapture se inicializa en __enter__
        self._capture: cv2.VideoCapture | None = None
        self._is_open = False

    def __enter__(self) -> "Camera":
        """
        Context manager: se ejecuta al entrar en "with Camera() as cam:".

        Abre la cámara y configura resolución/FPS.
        Retorna self para que puedas usar "as cam".
        """
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        Context manager: se ejecuta al salir del "with", incluso con errores.

        Los parámetros contienen info sobre excepciones (si las hay).
        Retornar None (o False) propaga la excepción normalmente.
        """
        self.close()

    def open(self) -> None:
        """
        Abre la conexión con la cámara.

        Raises:
            CameraError: Si no se puede abrir la cámara
        """
        if self._is_open:
            logger.warning("Camera already open")
            return

        logger.info(f"Opening camera {self.camera_index}...")

        # cv2.VideoCapture(index) abre la cámara
        # En Linux, 0 suele ser /dev/video0
        self._capture = cv2.VideoCapture(self.camera_index)

        if not self._capture.isOpened():
            raise CameraError(
                f"Could not open camera {self.camera_index}. "
                "Check if camera is connected and not in use by another app."
            )

        # Configurar resolución y FPS
        # CAP_PROP_* son constantes de OpenCV para propiedades de la cámara
        self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self._capture.set(cv2.CAP_PROP_FPS, self.fps)

        # Verificar qué resolución se obtuvo realmente
        # (algunas cámaras no soportan todas las resoluciones)
        actual_width = int(self._capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = int(self._capture.get(cv2.CAP_PROP_FPS))

        logger.info(
            f"Camera opened: {actual_width}x{actual_height} @ {actual_fps}fps "
            f"(requested: {self.width}x{self.height} @ {self.fps}fps)"
        )

        self._is_open = True

    def close(self) -> None:
        """
        Cierra la conexión con la cámara.

        Libera los recursos del sistema. Siempre llamar al terminar.
        """
        if self._capture is not None:
            self._capture.release()
            self._capture = None
            self._is_open = False
            logger.info("Camera closed")

    def read_frame(self) -> Frame | None:
        """
        Lee un único frame de la cámara.

        Returns:
            Frame con la imagen capturada, o None si falla la lectura

        Raises:
            CameraError: Si la cámara no está abierta
        """
        if not self._is_open or self._capture is None:
            raise CameraError("Camera is not open. Call open() first or use 'with' statement.")

        # read() retorna (success: bool, frame: ndarray)
        success, image = self._capture.read()

        if not success:
            logger.warning("Failed to read frame from camera")
            return None

        # Obtener timestamp (milisegundos desde que se abrió la cámara)
        timestamp = self._capture.get(cv2.CAP_PROP_POS_MSEC)

        return Frame(
            image=image,
            width=image.shape[1],  # shape es (height, width, channels)
            height=image.shape[0],
            timestamp=timestamp,
        )

    def stream(self) -> Generator[Frame, None, None]:
        """
        Genera frames continuamente (generator/iterator).

        Uso:
        ----
        ```python
        for frame in camera.stream():
            process(frame)
            if should_stop:
                break
        ```

        Yields:
            Frame: Cada frame capturado de la cámara

        Nota sobre Generators:
        ----------------------
        Un generator es una función que usa "yield" en vez de "return".
        Cada vez que llamas next() (o usas for), ejecuta hasta el siguiente yield.
        Esto es eficiente porque no carga todos los frames en memoria.
        """
        logger.info("Starting camera stream...")

        while self._is_open:
            frame = self.read_frame()
            if frame is not None:
                yield frame

    @property
    def is_open(self) -> bool:
        """Retorna True si la cámara está abierta."""
        return self._is_open


# =============================================================================
# Función de utilidad para probar la cámara
# =============================================================================


def test_camera(duration_seconds: float = 5.0) -> bool:
    """
    Prueba la cámara mostrando una ventana con el video.

    Args:
        duration_seconds: Cuántos segundos mostrar el video

    Returns:
        True si la prueba fue exitosa, False si hubo errores
    """
    try:
        with Camera() as cam:
            import time

            start_time = time.time()
            frame_count = 0

            logger.info(f"Testing camera for {duration_seconds} seconds...")
            logger.info("Press 'q' to quit early")

            for frame in cam.stream():
                frame_count += 1

                # Mostrar frame en ventana
                cv2.imshow("Camera Test - Press Q to quit", frame.image)

                # Esperar 1ms y verificar si se presionó 'q'
                # waitKey retorna el código ASCII de la tecla presionada
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    logger.info("User pressed 'q', stopping...")
                    break

                # Verificar timeout
                elapsed = time.time() - start_time
                if elapsed >= duration_seconds:
                    break

            # Calcular FPS real
            elapsed = time.time() - start_time
            actual_fps = frame_count / elapsed if elapsed > 0 else 0

            logger.info(f"Test complete: {frame_count} frames in {elapsed:.1f}s ({actual_fps:.1f} FPS)")

            # Cerrar ventana
            cv2.destroyAllWindows()
            return True

    except CameraError as e:
        logger.error(f"Camera test failed: {e}")
        return False
