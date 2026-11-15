"""
Qué motor de transcripción conviene, medido con tus frases.

    .venv-win\\Scripts\\python.exe scripts\\compare_stt.py

Whisper `small` cuesta 2,8 segundos por frase en esta CPU, que es la mitad de lo
que esperás después de callarte. Hay alternativas empaquetadas con español en
sherpa-onnx; esto las mide contra las mismas frases en vez de elegir por
reputación.

Las frases llevan jerga técnica a propósito (nombres de producto, anglicismos). Ahí es
donde los modelos se separan, no en "hola cómo estás".

El riesgo que hay que mirar, y por eso la última frase mezcla idiomas:
**Moonshine es un modelo por idioma y no hace code-switching**, y vos metés
inglés técnico todo el tiempo. Canary tiene inglés y español en el mismo modelo.

Deja la tabla en `docs/STT-COMPARATIVA.md`.
"""

from __future__ import annotations

import re
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "body"))

from jarbis_body.audio.resample import wav_bytes_to_mono  # noqa: E402
from jarbis_body.audio.tts import JarbisTTS  # noqa: E402
from jarbis_body.config import settings  # noqa: E402

FRASES = [
    "Guardá esto en MyJarbis",
    "Abrí el proyecto de Dmeter",
    "El token de Turso vence en tres días",
    "Corré los tests y decime si pasan",
    "Cerrá la sesión y dejame el handoff",
    # La de code-switching: es la que puede hundir a un modelo monolingüe.
    "Hacé un commit y pusheá a la branch de deploy",
]

STT_DIR = REPO / "models" / "stt"
MOONSHINE = STT_DIR / "sherpa-onnx-moonshine-base-es-quantized-2026-02-27"
CANARY = STT_DIR / "sherpa-onnx-nemo-canary-180m-flash-en-es-de-fr-int8"


def normalizar(texto: str) -> list[str]:
    texto = unicodedata.normalize("NFD", texto.lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9]+", texto)


def wer(referencia: str, hipotesis: str) -> float:
    ref, hyp = normalizar(referencia), normalizar(hipotesis)
    if not ref:
        return 0.0
    previa = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        actual = [i]
        for j, h in enumerate(hyp, 1):
            actual.append(min(previa[j] + 1, actual[j - 1] + 1, previa[j - 1] + (r != h)))
        previa = actual
    return previa[-1] / len(ref)


# ============================================================================
# Los motores
# ============================================================================


def motor_whisper():
    from faster_whisper import WhisperModel

    modelo = WhisperModel(
        settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )

    def transcribir(audio: np.ndarray) -> str:
        segmentos, _ = modelo.transcribe(
            audio,
            language=settings.whisper_language,
            beam_size=settings.whisper_beam_size,
            vad_filter=True,
            initial_prompt=settings.whisper_initial_prompt,
        )
        return " ".join(s.text.strip() for s in segmentos).strip()

    return transcribir


def motor_moonshine():
    import sherpa_onnx

    rec = sherpa_onnx.OfflineRecognizer.from_moonshine(
        preprocessor=str(MOONSHINE / "preprocess.onnx"),
        encoder=str(MOONSHINE / "encoder_model.ort"),
        uncached_decoder=str(MOONSHINE / "decoder_model_merged.ort"),
        cached_decoder=str(MOONSHINE / "decoder_model_merged.ort"),
        tokens=str(MOONSHINE / "tokens.txt"),
        num_threads=2,
    )

    def transcribir(audio: np.ndarray) -> str:
        stream = rec.create_stream()
        stream.accept_waveform(16000, audio)
        rec.decode_stream(stream)
        return stream.result.text.strip()

    return transcribir


def motor_canary():
    import sherpa_onnx

    rec = sherpa_onnx.OfflineRecognizer.from_nemo_canary(
        encoder=str(CANARY / "encoder.int8.onnx"),
        decoder=str(CANARY / "decoder.int8.onnx"),
        tokens=str(CANARY / "tokens.txt"),
        src_lang="es",
        tgt_lang="es",
        num_threads=2,
    )

    def transcribir(audio: np.ndarray) -> str:
        stream = rec.create_stream()
        stream.accept_waveform(16000, audio)
        rec.decode_stream(stream)
        return stream.result.text.strip()

    return transcribir


MOTORES = [
    ("whisper small", motor_whisper),
    ("moonshine base es", motor_moonshine),
    ("canary 180m es", motor_canary),
]


def main() -> int:
    tts = JarbisTTS()
    if not tts.preload():
        print("no cargó Piper")
        return 1

    print("sintetizando las frases...")
    clips = []
    for frase in FRASES:
        data = tts.synthesize(frase)
        if not data:
            continue
        audio = wav_bytes_to_mono(data, 16000)
        clips.append((frase, audio, len(audio) / 16000))
    total = sum(d for _, _, d in clips)
    print(f"  {len(clips)} clips, {total:.1f}s de audio\n")

    filas = []
    transcripciones: dict[str, list[str]] = {}

    for etiqueta, constructor in MOTORES:
        print(f"--- {etiqueta} ---")
        try:
            t0 = time.perf_counter()
            transcribir = constructor()
            carga = time.perf_counter() - t0
        except Exception as exc:
            print(f"  no arrancó: {type(exc).__name__}: {str(exc)[:110]}\n")
            filas.append((etiqueta, None, None, None))
            continue

        errores, tiempos, textos = [], [], []
        for frase, audio, _dur in clips:
            t = time.perf_counter()
            try:
                texto = transcribir(audio)
            except Exception as exc:
                texto = f"<error: {type(exc).__name__}>"
            tiempos.append(time.perf_counter() - t)
            errores.append(wer(frase, texto))
            textos.append(texto)
            print(f"  {tiempos[-1]:5.2f}s  {texto[:62]}")

        transcripciones[etiqueta] = textos
        filas.append((
            etiqueta,
            sum(errores) / len(errores),
            sum(tiempos) / len(tiempos),
            carga,
        ))
        print(f"  WER {filas[-1][1]*100:.1f}%  ·  {filas[-1][2]:.2f}s/frase  "
              f"·  carga {carga:.1f}s\n")

    # ------------------------------------------------------------------ reporte
    lineas = [
        "# Qué motor de transcripción conviene",
        "",
        "Medido en un i7-1255U sin GPU, con frases que tienen jerga técnica",
        f"sintetizadas con `{settings.tts_voice}`. {len(clips)} frases, {total:.1f}s de audio.",
        "",
        "| motor | WER | s/frase | carga |",
        "|---|---|---|---|",
    ]
    for etiqueta, w, t, c in filas:
        if w is None:
            lineas.append(f"| {etiqueta} | no arrancó | — | — |")
        else:
            lineas.append(f"| {etiqueta} | {w*100:.1f}% | **{t:.2f}s** | {c:.1f}s |")

    lineas += ["", "## Qué transcribió cada uno", ""]
    for etiqueta, textos in transcripciones.items():
        lineas += [f"### {etiqueta}", ""]
        for frase, texto in zip(FRASES, textos):
            lineas.append(f"- `{frase}` → {texto}")
        lineas.append("")

    lineas += [
        "## Cómo leer esto",
        "",
        "El WER absoluto es alto en todos porque las frases tienen nombres propios",
        "que ningún modelo acierta (Dmeter, Turso, MyJarbis) y porque la voz es",
        "sintética. Lo que vale es la comparación entre motores y los tiempos.",
        "",
        "La última frase mezcla español e inglés a propósito: ahí se ve si un",
        "modelo monolingüe se rompe con el code-switching.",
    ]

    out = REPO / "docs" / "STT-COMPARATIVA.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    print("\n".join(lineas[4:12]))
    print(f"\nReporte en {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
