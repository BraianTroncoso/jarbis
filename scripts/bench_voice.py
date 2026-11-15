"""
Banco de pruebas de la cadena de voz, sin micrófono.

    .venv-win\\Scripts\\python.exe scripts\\bench_voice.py

Cierra el lazo completo usando Piper como "boca falsa": sintetiza frases, las
pasa por el wake word y por Whisper, y compara lo transcripto contra lo que se
dijo. Es la única forma de verificar la cadena de noche, sin nadie hablándole.

Lo que NO reemplaza: tu voz real con ruido de fondo. Un wav sintético es limpio
y el VAD y el umbral del wake word se calibran distinto con ruido. Eso queda
para la prueba de mañana.

Mide también la latencia de cada etapa y la deja en docs/BENCH.md.
"""

from __future__ import annotations

import io
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body.audio.resample import wav_bytes_to_mono  # noqa: E402
from jarbis_body.config import settings  # noqa: E402

TARGET_RATE = 16_000

# Frases para el wake word: tiene que disparar con las primeras y NO con las
# últimas. Los negativos importan tanto como los positivos — un detector que
# dispara con todo es inútil.
WAKE_POSITIVE = ["Hey Jarvis", "hey jarvis", "Ey Jarvis, escuchame"]
WAKE_NEGATIVE = [
    "Hola, qué tal",
    "Pasame el café",
    "Servicio de limpieza",
    "Hay varias cosas pendientes",
]

# Frases para el STT, con la jerga que antes transcribía mal.
STT_PHRASES = [
    "Guardá esto en MyJarbis",
    "Hacé un commit y pusheá a la rama principal",
    "Abrí el proyecto de Dmeter",
    "El token de Turso vence en tres días",
    "Corré los tests y decime si pasan",
]


def wav_bytes_to_mono16k(data: bytes) -> tuple[np.ndarray, int]:
    """
    Decodifica un WAV de Piper a float32 mono a 16 kHz.

    Usa el resampler de FFmpeg vía PyAV. La primera versión usaba interpolación
    lineal de numpy y los resultados eran engañosos: el aliasing de bajar 22 kHz
    a 16 kHz sin filtro le arruinaba la transcripción y parecía culpa de Whisper.
    """
    with wave.open(io.BytesIO(data), "rb") as handle:
        rate = handle.getframerate()
    return wav_bytes_to_mono(data, TARGET_RATE), rate


def main() -> int:
    from jarbis_body.audio.tts import JarbisTTS
    from jarbis_body.audio.wakeword_onnx import OnnxWakeWordDetector

    report: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        report.append(line)

    say()
    say("# Banco de voz de Jarbis")
    say()
    say(f"- voz: `{settings.tts_voice}`")
    say(f"- whisper: `{settings.whisper_model}` ({settings.whisper_device}/{settings.whisper_compute_type})")
    say(f"- wake word: `{settings.wakeword_model_path.name}` umbral {settings.wakeword_threshold}")
    say()

    # ---------------------------------------------------------------- síntesis
    tts = JarbisTTS()
    t0 = time.perf_counter()
    if not tts.preload():
        say("**FALLA**: no se pudo cargar la voz Piper.")
        return 1
    say(f"Carga de Piper: {(time.perf_counter() - t0) * 1000:.0f} ms")

    def synth(text: str) -> tuple[np.ndarray, float]:
        start = time.perf_counter()
        data = tts.synthesize(text)
        elapsed = time.perf_counter() - start
        if not data:
            raise RuntimeError(f"Piper no sintetizó: {text!r}")
        audio, _ = wav_bytes_to_mono16k(data)
        return audio, elapsed

    # -------------------------------------------------------------- wake word
    say()
    say("## Wake word")
    say()
    detector = OnnxWakeWordDetector()
    threshold = settings.wakeword_threshold

    say("| frase | esperado | score | resultado |")
    say("|---|---|---|---|")

    wake_ok = 0
    wake_total = 0
    for phrase, expected in [(p, True) for p in WAKE_POSITIVE] + [
        (n, False) for n in WAKE_NEGATIVE
    ]:
        audio, _ = synth(phrase)
        score = detector.score_audio(audio)
        fired = score >= threshold
        passed = fired == expected
        wake_total += 1
        wake_ok += int(passed)
        say(
            f"| {phrase} | {'dispara' if expected else 'no dispara'} "
            f"| {score:.3f} | {'ok' if passed else 'FALLA'} |"
        )

    say()
    say(f"Wake word: {wake_ok}/{wake_total}")

    # -------------------------------------------------------------------- stt
    say()
    say("## Transcripción")
    say()

    from faster_whisper import WhisperModel

    t0 = time.perf_counter()
    model = WhisperModel(
        settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )
    say(f"Carga de Whisper: {(time.perf_counter() - t0) * 1000:.0f} ms")
    say()
    say("| dicho | transcripto | audio | stt | RTF |")
    say("|---|---|---|---|---|")

    rtfs: list[float] = []
    for phrase in STT_PHRASES:
        audio, synth_s = synth(phrase)
        audio_s = len(audio) / TARGET_RATE

        start = time.perf_counter()
        segments, _info = model.transcribe(
            audio,
            language=settings.whisper_language,
            beam_size=settings.whisper_beam_size,
            vad_filter=True,
            initial_prompt=settings.whisper_initial_prompt,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        stt_s = time.perf_counter() - start

        rtf = stt_s / audio_s if audio_s else 0.0
        rtfs.append(rtf)
        say(
            f"| {phrase} | {text} | {audio_s:.1f}s | {stt_s:.2f}s | {rtf:.2f} |"
        )

    say()
    if rtfs:
        say(f"RTF promedio: {sum(rtfs) / len(rtfs):.2f} (menor que 1 = más rápido que tiempo real)")

    # ------------------------------------------------------------------ reporte
    out = Path(__file__).resolve().parent.parent / "docs" / "BENCH.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(report) + "\n", encoding="utf-8")
    print()
    print(f"Reporte en {out}")

    return 0 if wake_ok == wake_total else 2


if __name__ == "__main__":
    raise SystemExit(main())
