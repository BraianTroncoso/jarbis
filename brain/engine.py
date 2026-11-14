"""
Los dos motores de pensamiento, y cuándo escalar de modelo.

## Por qué hay dos

El primero que funcionó fue `claude -p`: un proceso nuevo por turno. Cero
dependencias, cero superficie nueva, y andaba. Pero se midió y el costo es
brutal: un `claude -p` que sólo dice "ok" tarda **6,9 segundos** antes de pensar
nada. Eso es node arrancando, la config cargando, el MCP de MyJarbis
levantándose y la sesión restaurándose, en cada frase que decís.

`ClaudeSDKClient` mantiene **una sesión viva** a la que se le empujan mensajes.
Ese arranque se paga una sola vez, al principio. Y habilita `interrupt()`, o sea
poder hablarle encima mientras contesta — que en una conversación por voz no es
un lujo, es la diferencia entre hablar con alguien y dejarle un mensaje.

El de subprocess queda como respaldo: si el SDK no arranca (no está instalado,
cambió la API, lo que sea), el cuerpo sigue andando más lento en vez de no andar.

## El escalado de modelo

Haiku contesta casi todo lo que le vas a decir por voz. Para lo que de verdad
pide pensar, escala a Opus.

**La decisión de escalar es determinística, no la toma un modelo.** Un modelo
decidiendo si hace falta un modelo más caro es un modelo que siempre dice que
sí, y además agrega una llamada antes de la llamada. Son reglas sobre el texto,
se leen en un vistazo y se pueden discutir.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger("jarbis.brain")

REPO = Path(__file__).resolve().parent.parent
GATE_SETTINGS = REPO / "hooks" / "settings.json"

# Instrucción de forma. Lo que escriba lo va a leer Piper en voz alta, así que
# markdown, bullets y bloques de código se escuchan como ruido.
VOICE_PROMPT = """\
Estás contestando por voz: lo que escribas lo va a leer un sintetizador en voz \
alta. Reglas de forma, no negociables:

- Texto plano. Sin markdown, sin asteriscos, sin backticks, sin bullets, sin \
numeración, sin tablas, sin encabezados.
- Dos o tres oraciones como máximo. Si la respuesta completa no cabe, decí lo \
esencial y ofrecé el detalle.
- Sin emojis ni símbolos que no se pronuncien.
- Si tenés que nombrar una ruta o un comando, decilo en palabras.
- Castellano rioplatense, como se habla.

Si la respuesta de verdad necesita más largo (una lista, un diff, código), no lo \
dictes: hacé el trabajo con las herramientas, dejalo en un archivo, y contá en \
una oración qué hiciste y dónde quedó.
"""


# ============================================================================
# Escalado
# ============================================================================

# Palabras que marcan una pregunta que pide pensar, no recordar. Van sin tildes
# y se comparan contra el texto normalizado, porque Whisper las pone y las saca
# según el día.
ESCALATION_WORDS = {
    "analiza", "analizar", "analisis", "compara", "comparar", "evalua",
    "planifica", "planificar", "plan", "disena", "disenar", "arquitectura",
    "refactor", "refactoriza", "optimiza", "optimizar", "debuggea", "depura",
    "por que", "porque fallo", "investiga", "investigar", "profundiza",
    "pensa bien", "con cuidado", "en detalle", "escribi el codigo",
    "implementa", "implementar", "corregi", "arregla el",
}

# A partir de acá la frase ya es lo bastante larga como para que valga la pena.
ESCALATION_WORDS_COUNT = 28


def _normalize(text: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text).strip()


def needs_opus(text: str) -> tuple[bool, str]:
    """
    ¿Esta pregunta pide el modelo caro? Devuelve (sí/no, motivo).

    Reglas sobre el texto, sin llamar a nadie. El motivo se loguea para que el
    criterio se pueda revisar leyendo el log en vez de adivinando.
    """
    plain = _normalize(text)

    for word in ESCALATION_WORDS:
        if word in plain:
            return True, f"dijiste '{word}'"

    palabras = len(plain.split())
    if palabras >= ESCALATION_WORDS_COUNT:
        return True, f"son {palabras} palabras"

    return False, "corta y directa"


# ============================================================================
# El motor con sesión viva
# ============================================================================


class SdkEngine:
    """
    Una sesión de Claude Code viva, a la que se le empujan mensajes.

    El arranque se paga una vez. Después cada turno es sólo el ida y vuelta con
    el modelo.
    """

    name = "agent-sdk"

    def __init__(
        self,
        cwd: Path,
        model: str = "haiku",
        escalate_to: str = "opus",
        timeout: float = 240.0,
    ) -> None:
        self.cwd = cwd
        self.model = model
        self.escalate_to = escalate_to
        self.timeout = timeout
        self._client = None
        self._current_model = model

    async def start(self) -> bool:
        """Levanta la sesión. False si el SDK no está o no conecta."""
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
        except ImportError:
            log.warning("claude-agent-sdk no instalado; uso el motor de subprocess")
            return False

        options = ClaudeAgentOptions(
            cwd=str(self.cwd),
            model=self.model,
            # `setting_sources` es lo que hace que la sesión levante el CLAUDE.md
            # del twin, su configuración de MCP (MyJarbis) y sus skills. Sin
            # esto el SDK arranca pelado y Jarbis no sabría nada de tu contexto
            # — es la diferencia principal contra `claude -p`, que las carga
            # solo por correr dentro del proyecto.
            setting_sources=["user", "project", "local"],
            # La compuerta viaja con la sesión, igual que antes con --settings.
            settings=str(GATE_SETTINGS),
            # Se agrega a lo que ya trae Claude Code en vez de reemplazarlo: la
            # instrucción de voz es sobre la FORMA de la respuesta, no sobre
            # quién es.
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": VOICE_PROMPT,
            },
            env={**os.environ, "CLAUDE_CODE_VOICE_SESSION": "1",
                 "CLAUDE_PROJECT_DIR": str(REPO)},
        )

        try:
            self._client = ClaudeSDKClient(options=options)
            await self._client.connect()
        except Exception as exc:
            log.warning(f"El SDK no conectó ({type(exc).__name__}: {exc})")
            self._client = None
            return False

        log.info(f"Sesión viva lista · modelo {self.model} · proyecto {self.cwd}")
        return True

    async def think(self, text: str) -> str:
        """Un turno. Escala de modelo si la pregunta lo pide."""
        import asyncio

        if self._client is None:
            return "No tengo sesión."

        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        escalate, reason = needs_opus(text)
        target = self.escalate_to if escalate else self.model
        if target != self._current_model:
            try:
                await self._client.set_model(target)
                self._current_model = target
                log.info(f"Modelo → {target} ({reason})")
            except Exception as exc:
                log.warning(f"No pude cambiar de modelo: {exc}")

        partes: list[str] = []

        async def recolectar() -> None:
            """
            Drena la respuesta hasta el ResultMessage.

            Va en una corrutina aparte porque `asyncio.timeout` es de Python
            3.11 y acá hay 3.10 — `wait_for` envuelve corrutinas, no generadores
            asíncronos. La primera versión usaba `asyncio.timeout` y fallaba con
            AttributeError en cada turno: el banco marcó el SDK como 26.000
            veces más rápido, que era el síntoma de que no hacía nada.
            """
            async for message in self._client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            partes.append(block.text)
                elif isinstance(message, ResultMessage):
                    break

        try:
            await self._client.query(text)
            await asyncio.wait_for(recolectar(), timeout=self.timeout)
        except asyncio.TimeoutError:
            log.error(f"No contestó en {self.timeout:.0f}s")
            await self.interrupt()
            return "Me colgué pensando. Probá de nuevo."
        except Exception as exc:
            log.error(f"Falló el turno: {type(exc).__name__}: {exc}")
            return "Algo falló del lado del cerebro."

        return " ".join(p.strip() for p in partes if p.strip()).strip()

    async def interrupt(self) -> None:
        """Corta el turno en curso. Es lo que permite hablarle encima."""
        if self._client is None:
            return
        try:
            await self._client.interrupt()
        except Exception as exc:
            log.debug(f"Interrupt falló: {exc}")

    async def stop(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception:
            pass
        self._client = None


# ============================================================================
# El motor de respaldo
# ============================================================================


class SubprocessEngine:
    """
    Un `claude -p` por turno. Lento pero sin dependencias.

    Queda como red: si el SDK no arranca, el cuerpo sigue andando.
    """

    name = "subprocess"

    def __init__(
        self,
        cwd: Path,
        model: str = "haiku",
        escalate_to: str = "opus",
        timeout: float = 240.0,
    ) -> None:
        self.cwd = cwd
        self.model = model
        self.escalate_to = escalate_to
        self.timeout = timeout
        self.session_id: str | None = None

    async def start(self) -> bool:
        import shutil

        if shutil.which("claude") is None:
            log.error("No encontré el comando `claude` en el PATH")
            return False
        log.info(f"Motor de subprocess · proyecto {self.cwd}")
        return True

    async def think(self, text: str) -> str:
        import asyncio

        return await asyncio.to_thread(self._think_sync, text)

    def _think_sync(self, text: str) -> str:
        escalate, reason = needs_opus(text)
        model = self.escalate_to if escalate else self.model
        if escalate:
            log.info(f"Modelo → {model} ({reason})")

        cmd = [
            "claude", "-p", text,
            "--output-format", "json",
            "--model", model,
            "--append-system-prompt", VOICE_PROMPT,
            "--settings", str(GATE_SETTINGS),
        ]
        if self.session_id:
            cmd += ["--resume", self.session_id]

        try:
            proc = subprocess.run(
                cmd, cwd=str(self.cwd), capture_output=True, text=True,
                timeout=self.timeout,
                env={**os.environ, "CLAUDE_CODE_VOICE_SESSION": "1",
                     "CLAUDE_PROJECT_DIR": str(REPO)},
            )
        except subprocess.TimeoutExpired:
            return "Me colgué pensando. Probá de nuevo."
        except FileNotFoundError:
            return "No tengo acceso a Claude Code."

        if proc.returncode != 0:
            log.error(f"claude salió con {proc.returncode}: {proc.stderr[:300]}")
            if self.session_id and "resume" in (proc.stderr or "").lower():
                self.session_id = None
            return "Algo falló del lado del cerebro."

        return self._parse(proc.stdout)

    def _parse(self, stdout: str) -> str:
        stdout = stdout.strip()
        if not stdout:
            return ""
        try:
            payload = json.loads(stdout)
        except ValueError:
            return stdout
        if isinstance(payload, list):
            payload = payload[-1] if payload else {}
        if isinstance(payload, dict):
            new_session = payload.get("session_id")
            if new_session and new_session != self.session_id:
                self.session_id = new_session
            for key in ("result", "text", "response", "content"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return stdout

    async def interrupt(self) -> None:
        """No se puede: cada turno es un proceso que ya está corriendo."""

    async def stop(self) -> None:
        pass


# ============================================================================
# El motor de prueba
# ============================================================================


class EchoEngine:
    """Repite lo que escuchó. Para probar el puente sin gastar tokens."""

    name = "dry-run"

    async def start(self) -> bool:
        log.warning("Modo dry-run: no se llama a Claude")
        return True

    async def think(self, text: str) -> str:
        time.sleep(0.4)
        return f"Escuché: {text}"

    async def interrupt(self) -> None:
        pass

    async def stop(self) -> None:
        pass


async def pick_engine(
    cwd: Path,
    *,
    dry_run: bool = False,
    prefer_subprocess: bool = False,
    model: str = "haiku",
    escalate_to: str = "opus",
    timeout: float = 240.0,
):
    """
    Elige el mejor motor que arranque.

    SDK primero por velocidad; si no arranca, subprocess. Que el cuerpo ande
    lento es mucho mejor que que no ande.
    """
    if dry_run:
        engine = EchoEngine()
        await engine.start()
        return engine

    if not prefer_subprocess:
        sdk = SdkEngine(cwd, model=model, escalate_to=escalate_to, timeout=timeout)
        if await sdk.start():
            return sdk
        log.warning("Caigo al motor de subprocess (más lento)")

    fallback = SubprocessEngine(cwd, model=model, escalate_to=escalate_to, timeout=timeout)
    await fallback.start()
    return fallback
