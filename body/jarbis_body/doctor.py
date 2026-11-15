"""
Diagnóstico del cuerpo.

    .venv-win\\Scripts\\python.exe -m jarbis_body.doctor

Chequea todo lo que tiene que estar antes de que valga la pena hablarle al
micrófono, y dice exactamente qué falta. Pensado para correrse sin que haya
nadie delante: no graba, no reproduce, no pide permisos.

Códigos de salida: 0 todo bien, 1 hay fallas duras, 2 solo advertencias.
"""

from __future__ import annotations

import importlib
import platform
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

# Dependencias que tienen que importar para que el cuerpo arranque.
REQUIRED_MODULES = [
    "numpy",
    "loguru",
    "pydantic",
    "pydantic_settings",
    "sounddevice",
    "faster_whisper",
    "piper",
    "onnxruntime",
]

# Dependencias que habilitan funciones opcionales.
OPTIONAL_MODULES = [
    ("websockets", "puente WebSocket al cerebro"),
    ("uvicorn", "servidor del visor web"),
    ("keyboard", "hotkey global"),
    ("cv2", "presencia por cámara"),
    ("mediapipe", "presencia por cámara"),
]

OK = "OK  "
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class Check:
    level: str
    label: str
    detail: str = ""


class Doctor:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def ok(self, label: str, detail: str = "") -> None:
        self.checks.append(Check(OK, label, detail))

    def warn(self, label: str, detail: str = "") -> None:
        self.checks.append(Check(WARN, label, detail))

    def fail(self, label: str, detail: str = "") -> None:
        self.checks.append(Check(FAIL, label, detail))

    # ------------------------------------------------------------------ chequeos

    def check_platform(self) -> None:
        system = platform.system()
        self.ok("python", f"{sys.version.split()[0]} ({platform.machine()})")

        if system == "Windows":
            self.ok("plataforma", f"Windows {platform.release()}")
        else:
            # No es fatal: los tests de lógica pura corren en WSL. Pero el audio no.
            self.warn(
                "plataforma",
                f"{system} — el audio solo funciona nativo en Windows, "
                "esto sirve para tests de lógica",
            )

        if sys.version_info < (3, 10):
            self.fail("version de python", "hace falta 3.10 o más nuevo")

    def check_modules(self) -> None:
        for name in REQUIRED_MODULES:
            try:
                module = importlib.import_module(name)
                version = getattr(module, "__version__", "?")
                self.ok(f"import {name}", str(version))
            except Exception as exc:
                self.fail(f"import {name}", f"{type(exc).__name__}: {exc}")

        for name, purpose in OPTIONAL_MODULES:
            try:
                module = importlib.import_module(name)
                version = getattr(module, "__version__", "?")
                self.ok(f"import {name}", f"{version} — {purpose}")
            except Exception:
                self.warn(f"import {name}", f"ausente — sin {purpose}")

    def check_config(self) -> None:
        try:
            from jarbis_body.config import settings
        except Exception as exc:
            self.fail("configuración", f"no se pudo cargar: {exc}")
            return

        self.ok("configuración", f"proyecto en {settings.project_dir}")

        for label, directory in [
            ("models/", settings.models_dir),
            ("var/", settings.var_dir),
        ]:
            if directory.is_dir():
                self.ok(f"directorio {label}", str(directory))
            else:
                self.warn(f"directorio {label}", f"no existe: {directory}")

    def check_voices(self) -> None:
        from jarbis_body.config import settings

        found = sorted(settings.piper_dir.glob("*.onnx")) if settings.piper_dir.is_dir() else []
        if not found:
            self.fail("voces piper", f"ninguna en {settings.piper_dir}")
            return

        names = [path.stem for path in found]
        self.ok("voces piper", ", ".join(names))

        if settings.tts_voice in names:
            self.ok("voz configurada", settings.tts_voice)
        elif settings.tts_voice_fallback in names:
            self.warn(
                "voz configurada",
                f"'{settings.tts_voice}' falta; va a usar '{settings.tts_voice_fallback}'",
            )
        else:
            self.fail(
                "voz configurada",
                f"ni '{settings.tts_voice}' ni el respaldo están en disco",
            )

    def check_wakeword(self) -> None:
        from jarbis_body.config import settings

        if settings.wakeword_backend == "onnx":
            if settings.wakeword_model_path.exists():
                size_kb = settings.wakeword_model_path.stat().st_size / 1024
                self.ok("wake word onnx", f"{settings.wakeword_model_path.name} ({size_kb:.0f} KB)")
            else:
                self.warn(
                    "wake word onnx",
                    f"falta {settings.wakeword_model_path} — va a caer al fallback por whisper",
                )
        else:
            self.ok("wake word", f"backend '{settings.wakeword_backend}'")

        if settings.vad_backend == "silero":
            if settings.vad_model_path.exists():
                self.ok("silero vad", settings.vad_model_path.name)
            else:
                self.warn(
                    "silero vad",
                    f"falta {settings.vad_model_path} — va a caer al VAD por RMS",
                )

    def check_audio_devices(self) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:
            self.fail("dispositivos de audio", f"sounddevice no importa: {exc}")
            return

        try:
            devices = sd.query_devices()
        except Exception as exc:
            self.fail(
                "dispositivos de audio",
                f"{type(exc).__name__}: {exc} — en WSL esto es esperado",
            )
            return

        inputs = [d for d in devices if d.get("max_input_channels", 0) > 0]
        if not inputs:
            self.fail("entrada de audio", "no hay ningún dispositivo de captura")
            return

        self.ok("entrada de audio", f"{len(inputs)} dispositivo(s)")
        try:
            default_in = sd.query_devices(kind="input")
            self.ok("micrófono default", str(default_in.get("name", "?")))
        except Exception:
            self.warn("micrófono default", "no se pudo determinar")

    def check_ports(self) -> None:
        from jarbis_body.config import settings

        for label, port in [("puente", settings.bridge_port), ("visor web", 3334)]:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.3)
                in_use = probe.connect_ex(("127.0.0.1", port)) == 0
            if in_use:
                self.warn(f"puerto {label} ({port})", "ocupado — ¿ya hay una instancia?")
            else:
                self.ok(f"puerto {label} ({port})", "libre")

    def check_synthesis(self) -> None:
        """
        Síntesis real a disco. Cierra el lazo de la voz sin necesitar parlantes,
        que es la única forma de verificarla de noche.
        """
        from jarbis_body.config import settings

        try:
            from jarbis_body.audio.tts import JarbisTTS
        except Exception as exc:
            self.fail("síntesis", f"no se pudo importar el TTS: {exc}")
            return

        target = settings.var_dir / "doctor-sintesis.wav"
        try:
            tts = JarbisTTS()
            if tts.synthesize_to_file("Probando la voz de Jarbis.", target):
                size_kb = target.stat().st_size / 1024
                self.ok("síntesis", f"{target.name} ({size_kb:.0f} KB)")
            else:
                self.fail("síntesis", "Piper no devolvió audio")
        except Exception as exc:
            self.fail("síntesis", f"{type(exc).__name__}: {exc}")

    def check_whisper(self) -> None:
        """Verifica que el modelo de Whisper se pueda instanciar (lo descarga si falta)."""
        from jarbis_body.config import settings

        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            self.fail("whisper", f"no importa: {exc}")
            return

        try:
            WhisperModel(
                settings.whisper_model,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
            )
            self.ok(
                "whisper",
                f"modelo '{settings.whisper_model}' "
                f"({settings.whisper_device}/{settings.whisper_compute_type})",
            )
        except Exception as exc:
            self.fail("whisper", f"{type(exc).__name__}: {str(exc)[:120]}")

    # -------------------------------------------------------------------- salida

    def run(self, *, deep: bool = False) -> int:
        self.check_platform()
        self.check_modules()
        self.check_config()
        self.check_voices()
        self.check_wakeword()
        self.check_audio_devices()
        self.check_ports()
        if deep:
            self.check_synthesis()
            self.check_whisper()

        print()
        print("  Jarbis — diagnóstico del cuerpo")
        print("  " + "-" * 58)
        for check in self.checks:
            print(f"  [{check.level}] {check.label:<24} {check.detail}")
        print("  " + "-" * 58)

        fails = sum(1 for c in self.checks if c.level == FAIL)
        warns = sum(1 for c in self.checks if c.level == WARN)
        oks = sum(1 for c in self.checks if c.level == OK)
        print(f"  {oks} ok · {warns} advertencias · {fails} fallas")
        if not deep:
            print("  (corré con --deep para probar síntesis y carga de Whisper)")
        print()

        if fails:
            return 1
        return 2 if warns else 0


def main() -> int:
    deep = "--deep" in sys.argv
    return Doctor().run(deep=deep)


if __name__ == "__main__":
    # Permite correrlo con `python body/jarbis_body/doctor.py` además de -m.
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
