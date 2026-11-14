"""
Remuestreo de audio.

Hace falta porque Piper sintetiza a la frecuencia de cada voz (22.050 Hz en
`es_AR-daniela-high`) y todo lo que consume audio acá —Whisper, Silero, el wake
word— espera 16.000 Hz.

Importa hacerlo bien. La primera versión del banco de pruebas usaba
interpolación lineal de numpy y los resultados fueron engañosos: bajar de 22 kHz
a 16 kHz sin filtro pasa-bajos mete aliasing, y el aliasing le arruina la
transcripción a Whisper. Las frases salían mal y parecía culpa del modelo.

Se usa el resampler de FFmpeg vía PyAV, que ya está instalado porque
faster-whisper depende de él. La interpolación lineal queda solo como respaldo si
PyAV no está, y avisa cuando cae ahí.
"""

from __future__ import annotations

import numpy as np
from loguru import logger

_warned_fallback = False


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """
    Remuestrea float32 mono con filtro anti-aliasing.

    Args:
        audio: mono float32 en [-1, 1]
        src_rate: frecuencia de origen
        dst_rate: frecuencia de destino

    Returns:
        float32 mono a `dst_rate`
    """
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    if src_rate == dst_rate or audio.size == 0:
        return audio

    try:
        return _resample_av(audio, src_rate, dst_rate)
    except Exception as exc:
        global _warned_fallback
        if not _warned_fallback:
            logger.warning(
                f"PyAV no pudo remuestrear ({exc}); cayendo a interpolación lineal. "
                "Ojo: sin filtro anti-aliasing la calidad de transcripción baja."
            )
            _warned_fallback = True
        return _resample_linear(audio, src_rate, dst_rate)


def _resample_av(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Remuestreo por FFmpeg (swresample), con su filtro anti-aliasing."""
    import av
    from av.audio.resampler import AudioResampler

    # FFmpeg quiere int16 planar para 's16'; se trabaja en fltp para no perder
    # precisión antes del filtro.
    frame = av.AudioFrame.from_ndarray(
        audio.reshape(1, -1), format="fltp", layout="mono"
    )
    frame.sample_rate = src_rate
    frame.pts = None

    resampler = AudioResampler(format="fltp", layout="mono", rate=dst_rate)

    chunks: list[np.ndarray] = []
    for out_frame in resampler.resample(frame):
        chunks.append(out_frame.to_ndarray().reshape(-1))
    # Hay que drenar el resampler: se queda con cola en los buffers internos.
    for out_frame in resampler.resample(None):
        chunks.append(out_frame.to_ndarray().reshape(-1))

    if not chunks:
        raise RuntimeError("el resampler no devolvió frames")
    return np.concatenate(chunks).astype(np.float32)


def _resample_linear(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Respaldo sin dependencias. Sin filtro: mete aliasing al bajar de frecuencia."""
    duration = len(audio) / src_rate
    dst_len = max(1, int(duration * dst_rate))
    src_idx = np.linspace(0, len(audio) - 1, dst_len)
    return np.interp(src_idx, np.arange(len(audio)), audio).astype(np.float32)


def wav_bytes_to_mono(data: bytes, dst_rate: int = 16_000) -> np.ndarray:
    """Decodifica un WAV PCM 16-bit a float32 mono a `dst_rate`."""
    import io
    import wave

    with wave.open(io.BytesIO(data), "rb") as handle:
        src_rate = handle.getframerate()
        channels = handle.getnchannels()
        frames = handle.readframes(handle.getnframes())

    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return resample(audio, src_rate, dst_rate)
