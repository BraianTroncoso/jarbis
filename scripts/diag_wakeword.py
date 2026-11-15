"""
Diagnóstico del wake word: ¿puede disparar con algo?

El banco de voz dio score ~0.05 para todas las frases en español, incluida
"Hey Jarvis". Eso puede ser una de tres cosas y hay que separarlas:

1. el remuestreo casero arruinaba el audio    → se corrige usando PyAV
2. el modelo está entrenado en inglés          → se prueba con una voz inglesa
3. el detector está mal implementado           → ya descartado: la realimentación
                                                 de estado cambia el score y el
                                                 `step` avanza

Este script sintetiza la misma frase con la voz argentina y con la inglesa, y
prueba variantes de escritura para empujar a la voz española hacia la fonética
inglesa. Si la voz inglesa dispara y la argentina no, el problema es fonético y
no del código.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body.audio.resample import resample, wav_bytes_to_mono  # noqa: E402
from jarbis_body.audio.tts import JarbisTTS  # noqa: E402
from jarbis_body.audio.wakeword_onnx import OnnxWakeWordDetector  # noqa: E402
from jarbis_body.config import settings  # noqa: E402

# Variantes en español, buscando acercarse al /dʒ/ inglés desde la ortografía
# castellana. La voz lee con fonética española, así que "Yarvis" y "Dyarvis"
# suenan más parecido al inglés que "Jarvis" (que sale con jota velar).
ES_VARIANTS = [
    "Hey Jarvis",
    "Hey Yarvis",
    "Hey Dyarvis",
    "Hei Yárvis",
    "Jei Yarvis",
    "Hey Jarbis",
    "Jarbis",
]

EN_VARIANTS = [
    "Hey Jarvis",
    "hey jarvis",
    "Hey Jarvis, are you there",
]

NEGATIVES = ["Hola qué tal", "Pasame el café", "Mañana lo vemos"]


def synth(voice: str, text: str) -> np.ndarray:
    tts = JarbisTTS(voice=voice)
    data = tts.synthesize(text)
    if not data:
        raise RuntimeError(f"no sintetizó: {voice} / {text!r}")
    return wav_bytes_to_mono(data, 16_000)


def save(audio: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(audio, -1.0, 1.0)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes((pcm * 32767).astype(np.int16).tobytes())


def main() -> int:
    detector = OnnxWakeWordDetector()
    threshold = settings.wakeword_threshold
    out_dir = settings.var_dir / "diag"

    voices = {
        path.stem for path in settings.piper_dir.glob("*.onnx")
    }
    print(f"voces disponibles: {', '.join(sorted(voices))}")
    print(f"umbral: {threshold}")
    print()

    results: list[tuple[str, str, float]] = []

    def probe(voice: str, text: str, tag: str) -> None:
        if voice not in voices:
            print(f"  (salteo {voice}: no está en disco)")
            return
        audio = synth(voice, text)
        score = detector.score_audio(audio)
        results.append((voice, text, score))
        mark = "DISPARA" if score >= threshold else "       "
        print(f"  {mark} {score:.4f}  [{voice}] {text!r}")
        save(audio, out_dir / f"{tag}.wav")

    es_voice = "es_AR-daniela-high"
    en_voice = "en_US-lessac-medium"

    print("== voz argentina, variantes ortográficas ==")
    for i, text in enumerate(ES_VARIANTS):
        probe(es_voice, text, f"es_{i}")

    print()
    print("== voz inglesa (verdad positiva de referencia) ==")
    for i, text in enumerate(EN_VARIANTS):
        probe(en_voice, text, f"en_{i}")

    print()
    print("== negativos (no deben disparar) ==")
    for i, text in enumerate(NEGATIVES):
        probe(es_voice, text, f"neg_{i}")

    # Robustez al remuestreo: el mismo audio a 22 kHz nativo vs remuestreado.
    print()
    print("== el remuestreo importa? ==")
    if en_voice in voices:
        tts = JarbisTTS(voice=en_voice)
        data = tts.synthesize("Hey Jarvis")
        with wave.open(__import__("io").BytesIO(data), "rb") as handle:
            native_rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
        native = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        print(f"  frecuencia nativa de la voz: {native_rate} Hz")
        good = resample(native, native_rate, 16_000)
        duration = len(native) / native_rate
        linear_len = int(duration * 16_000)
        idx = np.linspace(0, len(native) - 1, linear_len)
        crude = np.interp(idx, np.arange(len(native)), native).astype(np.float32)
        print(f"  con PyAV (anti-alias):   {detector.score_audio(good):.4f}")
        print(f"  lineal casero (aliasing): {detector.score_audio(crude):.4f}")

    best_en = max(
        (s for v, _, s in results if v == en_voice), default=0.0
    )
    best_es = max((s for v, _, s in results if v == es_voice), default=0.0)

    print()
    print(f"mejor score con voz inglesa:   {best_en:.4f}")
    print(f"mejor score con voz argentina: {best_es:.4f}")
    print(f"wavs guardados en {out_dir}")

    if best_en >= threshold and best_es < threshold:
        print()
        print("VEREDICTO: el detector funciona pero el modelo es inglés.")
        print("           Con pronunciación castellana no dispara.")
        return 3
    if best_en < threshold:
        print()
        print("VEREDICTO: no dispara ni con voz inglesa. Revisar la implementación.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
