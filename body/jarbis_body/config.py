"""
Configuración del cuerpo de Jarbis.

Heredado de GesturePilot v0.2.0 (config.py), con el prefijo de entorno cambiado
de GESTURE_ a JARBIS_ y los campos nuevos del puente, el wake word ONNX y el
archivo de estado.

Prioridad de carga (menor a mayor): defaults de acá → .env → entorno del sistema.

    from jarbis_body.config import settings
    print(settings.whisper_model)  # small
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ============================================================================
# Raíz del proyecto
# ============================================================================
# El cuerpo corre nativo en Windows pero el repo vive en WSL, y el layout difiere
# entre los dos: en el repo el paquete está en `body/jarbis_body/`, desplegado
# está en la raíz. Así que NO se puede contar los `.parent` — ese es justamente
# el bug 4 del plan (tts.py encadenaba cuatro y se rompía fuera del repo).
#
# Se busca hacia arriba el directorio que tenga `models/`, que es el marcador
# del proyecto. `JARBIS_PROJECT_DIR` pisa todo si está seteada.
PACKAGE_DIR = Path(__file__).resolve().parent


def _find_project_dir() -> Path:
    """Primer ancestro con un `models/` adentro; si no hay, el padre del paquete."""
    for candidate in (PACKAGE_DIR, *PACKAGE_DIR.parents):
        if (candidate / "models").is_dir():
            return candidate
    return PACKAGE_DIR.parent


PROJECT_DIR = _find_project_dir()


class JarbisSettings(BaseSettings):
    """Configuración principal del cuerpo."""

    model_config = SettingsConfigDict(
        env_prefix="JARBIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # =========================================================================
    # Rutas
    # =========================================================================

    project_dir: Path = Field(
        default=PROJECT_DIR,
        description="Raíz del proyecto (contiene models/, var/, web/)",
    )

    models_dir: Path | None = Field(
        default=None,
        description="Directorio de modelos (piper/, wakeword/). Derivado de project_dir",
    )

    var_dir: Path | None = Field(
        default=None,
        description="Estado runtime: state json, logs, jsonl. Derivado de project_dir",
    )

    # =========================================================================
    # Speech-to-text (faster-whisper)
    # =========================================================================

    whisper_model: str = Field(
        default="small",
        description="Modelo Whisper: tiny, base, small, medium, large",
    )

    whisper_language: str = Field(
        default="es",
        description="Idioma fijo de transcripción. Fijarlo evita autodetección errática",
    )

    whisper_device: str = Field(
        default="cpu",
        description="cpu o cuda. En este hardware siempre cpu (no hay GPU NVIDIA)",
    )

    whisper_compute_type: str = Field(
        default="int8",
        description="int8 en CPU; float16 solo tiene sentido con CUDA",
    )

    whisper_beam_size: int = Field(
        default=5,
        description="Beam search. 5 para el turno real, 1 para el wake word fallback",
        ge=1,
        le=10,
    )

    whisper_preload: bool = Field(
        default=True,
        description=(
            "Cargar el modelo al arrancar en vez de en el primer turno. "
            "Mata el problema 3 de PLAN_MEJORAS (latencia de carga por turno)"
        ),
    )

    # El initial_prompt le sesga el vocabulario a Whisper. Ataca el problema 1 de
    # PLAN_MEJORAS directamente: sin esto, "MyJarbis" salía "Mi Charbis" y
    # "Age of Empires" salía "Acro Vampire".
    whisper_initial_prompt: str = Field(
        default=(
            "Contexto técnico: MyJarbis, Jarbis, Dmeter, Turso, SQLite, MCP, "
            "Claude Code, WSL, Windows, Python, TypeScript, Laravel, Vue, "
            "Tauri, Ollama, Whisper, Piper, commit, push, merge, branch, "
            "deploy, endpoint, LinkedIn, carrusel."
        ),
        description=(
            "Sesgo de vocabulario para Whisper. Poné acá los nombres propios y la "
            "jerga que usás: sin esto, 'MyJarbis' se transcribe 'MiFaris'"
        ),
    )

    # =========================================================================
    # Wake word
    # =========================================================================

    wakeword_backend: str = Field(
        default="onnx",
        description="onnx (livekit hey_jarvis) | whisper (fallback heredado)",
    )

    wakeword_model_path: Path | None = Field(
        default=None,
        description="Modelo ONNX del wake word. Derivado de models_dir",
    )

    wakeword_threshold: float = Field(
        default=0.5,
        description="Umbral de activación. Subir si hay falsos positivos",
        ge=0.0,
        le=1.0,
    )

    wakeword_enabled: bool = Field(
        default=True,
        description="Escuchar el nombre de forma continua",
    )

    # =========================================================================
    # Voice activity detection
    # =========================================================================

    vad_backend: str = Field(
        default="silero",
        description="silero (ONNX, robusto a ruido) | rms (casero heredado)",
    )

    vad_model_path: Path | None = Field(
        default=None,
        description="Modelo ONNX de Silero VAD. Derivado de models_dir",
    )

    silence_timeout: float = Field(
        default=0.7,
        description=(
            "Segundos de silencio para cerrar el turno. El heredado era 2.0, "
            "que se sentía lento; 0.7 es el presupuesto de latencia del plan"
        ),
        ge=0.3,
        le=10.0,
    )

    vad_max_wait_s: float = Field(
        default=8.0,
        description=(
            "Cuánto espera a que empieces a hablar antes de cerrar el turno "
            "vacío. Distinto de silence_timeout, que cuenta el silencio DESPUÉS "
            "de que hablaste"
        ),
        ge=1.0,
        le=60.0,
    )

    recording_hard_limit_s: float = Field(
        default=90.0,
        description=(
            "Tope absoluto de grabación. Solo aplica en push-to-talk, para que "
            "un botón trabado no grabe indefinidamente"
        ),
        ge=5.0,
        le=600.0,
    )

    push_to_talk: bool = Field(
        default=False,
        description=(
            "True: el turno cierra al soltar el botón. False: cierra el VAD "
            "cuando te callás. La cara lo cambia en caliente"
        ),
    )

    follow_up: bool = Field(
        default=True,
        description=(
            "True: después de contestar vuelve a escuchar (conversación). "
            "False: contesta y se duerme (una sola pregunta)"
        ),
    )

    silence_threshold: float = Field(
        default=0.02,
        description="Umbral RMS del VAD casero. Solo aplica con vad_backend=rms",
        ge=0.001,
        le=0.5,
    )

    # =========================================================================
    # Text-to-speech (Piper)
    # =========================================================================

    tts_enabled: bool = Field(default=True, description="Hablar las respuestas")

    tts_voice: str = Field(
        default="es_AR-daniela-high",
        description="Voz Piper. es_AR-daniela-high es la única argentina del catálogo",
    )

    tts_voice_fallback: str = Field(
        default="es_ES-davefx-medium",
        description="Voz de respaldo (CC0, ya presente en el repo)",
    )

    # =========================================================================
    # Audio
    # =========================================================================

    audio_sample_rate: int = Field(
        default=16000,
        description="16 kHz es lo que esperan Whisper, Silero y el wake word",
        ge=8000,
        le=48000,
    )

    audio_channels: int = Field(default=1, description="Mono para STT", ge=1, le=2)

    audio_input_device: int | None = Field(
        default=None,
        description="Índice de sounddevice. None = default del sistema",
    )

    # =========================================================================
    # Puente al cerebro
    # =========================================================================

    bridge_host: str = Field(
        default="127.0.0.1",
        description="Dirección base. NUNCA 0.0.0.0: esta máquina tiene Wi-Fi",
    )

    bridge_allow_wsl: bool = Field(
        default=True,
        description=(
            "Atarse también a la IP del adaptador virtual de WSL. Hace falta "
            "porque con networkingMode=NAT la VM NO alcanza el loopback de "
            "Windows. Sigue sin publicarse en la LAN"
        ),
    )

    bridge_port: int = Field(default=8765, description="Puerto del WebSocket", ge=1024, le=65535)

    bridge_token_file: Path | None = Field(
        default=None,
        description="Token compartido. Loopback solo no alcanza como autenticación",
    )

    # =========================================================================
    # Presencia (cámara) — apagada por default
    # =========================================================================

    presence_enabled: bool = Field(
        default=False,
        description="Detectar presencia por cámara. Apagado por default (decisión 17)",
    )

    camera_index: int = Field(default=0, description="Índice de cámara", ge=0)
    camera_width: int = Field(default=640, ge=320, le=1920)
    camera_height: int = Field(default=480, ge=240, le=1080)
    camera_fps: int = Field(default=30, ge=15, le=60)
    camera_mirror: bool = Field(default=True)
    min_detection_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    min_tracking_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    absence_timeout: float = Field(
        default=3.0,
        description="Segundos sin verte para considerarte ausente",
        ge=0.5,
        le=60.0,
    )

    # =========================================================================
    # Hotkey
    # =========================================================================

    hotkey_enabled: bool = Field(default=True, description="Activar por teclado")
    hotkey_combo: str = Field(default="ctrl+shift+g", description="Combo de activación")

    # =========================================================================
    # Estado y timeouts
    # =========================================================================

    listening_timeout_s: int = Field(
        default=30,
        description="Timeout del estado LISTENING",
        ge=5,
        le=120,
    )

    conversation_timeout_s: float = Field(
        default=30.0,
        description="Segundos de modo conversación antes de volver a pedir el wake word",
        ge=5.0,
        le=300.0,
    )

    # =========================================================================
    # Saludo de la mañana
    # =========================================================================

    greeting_enabled: bool = Field(
        default=False,
        description=(
            "Saludar una vez por día. Arranca apagado a propósito: que te hable "
            "sin que le hables es un cambio de comportamiento, se prende cuando "
            "vos quieras"
        ),
    )

    greeting_from: str = Field(default="08:00", description="Inicio de la ventana")
    greeting_until: str = Field(default="11:00", description="Fin de la ventana")

    greeting_prompt: str = Field(
        default=(
            "Es la mañana y todavía no hablamos. Saludame corto y decime en una "
            "sola oración lo más urgente que tengo hoy, mirando MyJarbis. Si no "
            "hay nada urgente, decímelo y listo. No preguntes nada."
        ),
        description=(
            "Lo que se le pide al CEREBRO. No es lo que dice: el saludo lo arma "
            "él con tu contexto real, por eso es una pregunta y no un texto"
        ),
    )

    # =========================================================================
    # Observabilidad
    # =========================================================================

    log_level: str = Field(default="INFO", description="DEBUG, INFO, WARNING, ERROR")

    state_file: Path | None = Field(
        default=None,
        description="Estado en vivo que lee la cara (visor web / widget)",
    )

    session_log: Path | None = Field(
        default=None,
        description="Historial de turnos en JSONL",
    )

    feedback_sound: bool = Field(default=True, description="Sonidos de feedback")


    # =========================================================================
    # Derivación de rutas
    # =========================================================================

    def model_post_init(self, _context: object) -> None:
        """
        Completa las rutas que quedaron en None a partir de `project_dir`.

        Hace que pisar JARBIS_PROJECT_DIR arrastre a todas las demás, en vez de
        tener que setear siete variables de entorno. Cualquiera se puede pisar
        igual de forma individual.
        """
        root = self.project_dir
        if self.models_dir is None:
            self.models_dir = root / "models"
        if self.var_dir is None:
            self.var_dir = root / "var"
        if self.wakeword_model_path is None:
            self.wakeword_model_path = self.models_dir / "wakeword" / "hey_jarvis.onnx"
        if self.vad_model_path is None:
            self.vad_model_path = self.models_dir / "wakeword" / "silero_vad.onnx"
        if self.bridge_token_file is None:
            self.bridge_token_file = self.var_dir / "bridge.token"
        if self.state_file is None:
            self.state_file = self.var_dir / "jarbis-state.json"
        if self.session_log is None:
            self.session_log = self.var_dir / "session.jsonl"

    # =========================================================================
    # Derivados
    # =========================================================================

    @property
    def piper_dir(self) -> Path:
        """Directorio de voces Piper."""
        return self.models_dir / "piper"

    def piper_model_path(self, voice: str | None = None) -> Path:
        """Ruta del .onnx de una voz Piper."""
        return self.piper_dir / f"{voice or self.tts_voice}.onnx"

    def piper_config_path(self, voice: str | None = None) -> Path:
        """Ruta del .onnx.json de una voz Piper."""
        return self.piper_dir / f"{voice or self.tts_voice}.onnx.json"


# Instancia global: una sola configuración para toda la app.
settings = JarbisSettings()
