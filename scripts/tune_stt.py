"""
Calibración del STT: qué combinación de modelo y parámetros sirve en esta máquina.

El primer banco dio RTF 1,74 — más lento que tiempo real — y, lo más raro, ~2,9 s
por frase sin importar si el audio duraba 1,3 s o 2,2 s. Eso no es costo
proporcional al audio: es el costo fijo de la ventana de 30 segundos con la que
Whisper trabaja siempre. Para comandos cortos, que es todo lo que le vas a decir
a Jarbis, ese costo fijo es TODO el costo.

Este script mide las palancas reales:

- `beam_size`: 5 (default) contra 1 (greedy)
- `condition_on_previous_text`: arrastra contexto entre segmentos y cuesta
- `model`: base contra small
- `initial_prompt`: ¿la jerga ayuda o alucina?

Sin micrófono: sintetiza con Piper y compara contra el texto original.
"""

from __future__ import annotations

import re
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "body"))

from jarbis_body.audio.resample import wav_bytes_to_mono  # noqa: E402
from jarbis_body.audio.tts import JarbisTTS  # noqa: E402
from jarbis_body.config import settings  # noqa: E402

PHRASES = [
    "Guardá esto en MyJarbis",
    "Hacé un commit y pusheá a la rama principal",
    "Abrí el proyecto de Dmeter",
    "El token de Turso vence en tres días",
    "Corré los tests y decime si pasan",
    "Cerrá la sesión y dejame el handoff",
]


def normalize(text: str) -> list[str]:
    """Baja a minúsculas, saca tildes y puntuación. Para comparar palabras."""
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9]+", text)


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate por distancia de edición a nivel palabra."""
    ref, hyp = normalize(reference), normalize(hypothesis)
    if not ref:
        return 0.0
    # Levenshtein con una sola fila.
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        current = [i]
        for j, h in enumerate(hyp, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h))
            )
        previous = current
    return previous[-1] / len(ref)


def main() -> int:
    from faster_whisper import WhisperModel

    tts = JarbisTTS()
    if not tts.preload():
        print("no se pudo cargar Piper")
        return 1

    print("sintetizando frases de prueba...")
    clips: list[tuple[str, object, float]] = []
    for phrase in PHRASES:
        data = tts.synthesize(phrase)
        if not data:
            print(f"  falló: {phrase!r}")
            continue
        audio = wav_bytes_to_mono(data, 16_000)
        clips.append((phrase, audio, len(audio) / 16_000))
    total_audio = sum(duration for _, _, duration in clips)
    print(f"  {len(clips)} clips, {total_audio:.1f}s de audio en total")
    print()

    # (etiqueta, modelo, kwargs de transcribe)
    configs = [
        ("small beam5 prompt", "small", {"beam_size": 5, "initial_prompt": settings.whisper_initial_prompt}),
        ("small beam1 prompt", "small", {"beam_size": 1, "initial_prompt": settings.whisper_initial_prompt}),
        ("small beam1 sin prompt", "small", {"beam_size": 1, "initial_prompt": None}),
        ("small beam1 prompt sin-cond", "small", {"beam_size": 1, "initial_prompt": settings.whisper_initial_prompt, "condition_on_previous_text": False}),
        ("base beam1 prompt", "base", {"beam_size": 1, "initial_prompt": settings.whisper_initial_prompt}),
        ("base beam5 prompt", "base", {"beam_size": 5, "initial_prompt": settings.whisper_initial_prompt}),
    ]

    rows: list[tuple[str, float, float, float, float]] = []
    loaded: dict[str, object] = {}
    transcripts: dict[str, list[str]] = {}

    for label, model_name, kwargs in configs:
        if model_name not in loaded:
            print(f"cargando modelo '{model_name}'...")
            loaded[model_name] = WhisperModel(
                model_name,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
            )
        model = loaded[model_name]

        errors: list[float] = []
        times: list[float] = []
        texts: list[str] = []

        for phrase, audio, _duration in clips:
            start = time.perf_counter()
            segments, _info = model.transcribe(
                audio,
                language=settings.whisper_language,
                vad_filter=True,
                **kwargs,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            times.append(time.perf_counter() - start)
            errors.append(wer(phrase, text))
            texts.append(text)

        transcripts[label] = texts
        mean_wer = sum(errors) / len(errors)
        mean_time = sum(times) / len(times)
        rows.append((label, mean_wer, mean_time, sum(times), sum(times) / total_audio))
        print(
            f"  {label:<30} WER {mean_wer * 100:5.1f}%  "
            f"{mean_time:.2f}s/frase  RTF {sum(times) / total_audio:.2f}"
        )

    # ------------------------------------------------------------------ reporte
    lines = [
        "# Calibración del STT",
        "",
        f"Hardware: i7-1255U, DDR4-3200 dual channel, sin GPU. {len(clips)} frases "
        f"sintetizadas con `{settings.tts_voice}`, {total_audio:.1f}s de audio.",
        "",
        "El tiempo por frase es casi constante porque Whisper procesa una ventana",
        "de 30 segundos siempre, sin importar que le des 1,5 s de audio. Para",
        "comandos cortos ese costo fijo es el costo total, así que lo que importa",
        "es la columna de segundos por frase, no el RTF.",
        "",
        "| configuración | WER | s/frase | RTF |",
        "|---|---|---|---|",
    ]
    for label, mean_wer, mean_time, _total, rtf in rows:
        lines.append(f"| {label} | {mean_wer * 100:.1f}% | {mean_time:.2f}s | {rtf:.2f} |")

    lines += ["", "## Transcripciones", ""]
    for label in transcripts:
        lines += [f"### {label}", ""]
        for phrase, text in zip(PHRASES, transcripts[label]):
            lines.append(f"- `{phrase}` → {text}")
        lines.append("")

    out = Path(__file__).resolve().parent.parent / "docs" / "STT.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print(f"Reporte en {out}")

    best = min(rows, key=lambda r: (r[1], r[2]))
    print(f"Mejor por WER: {best[0]} ({best[1] * 100:.1f}%, {best[2]:.2f}s/frase)")
    fastest = min(rows, key=lambda r: r[2])
    print(f"Más rápida:    {fastest[0]} ({fastest[1] * 100:.1f}%, {fastest[2]:.2f}s/frase)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
