"""
El orquestador del cuerpo.

Reemplaza `jarvis.py` de GesturePilot. La lógica de conversación es la misma; lo
que cambió es el final del pipeline y el motor del loop.

## Qué se tiró

El original estacionaba la transcripción en `self._pending_prompt` y
`_send_prompt()` era un stub que logueaba "Claude lo va a buscar con la tool MCP
submit_voice_prompt". Eso obligaba al cerebro a preguntar en loop, llenaba la
consola de `{success: false, has_prompt: false}` (problema 2 de PLAN_MEJORAS) y
hacía estructuralmente imposible que Jarbis hablara primero.

Acá la transcripción se **empuja** al puente y listo. No hay estado pendiente que
alguien tenga que venir a buscar.

## El otro cambio: el loop

El original manejaba todo desde `for frame in cam.stream()`, el loop de la
cámara. Consecuencia: con la cámara apagada no avanzaba nada, ni los timeouts de
conversación. Acá el motor son los callbacks de audio y los timers de la máquina
de estados; la cámara es opcional y no conduce nada.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from loguru import logger

from jarbis_body.audio.tts import JarbisTTS
from jarbis_body.audio.voicemode import TranscriptionResult, VoiceMode
from jarbis_body.bridge.protocol import (
    BodyError,
    Interrupted,
    Proactive,
    StateChanged,
    Turn,
    WakeDetected,
)
from jarbis_body.bridge.server import BridgeServer
from jarbis_body.config import settings
from jarbis_body.greeting import GreetingTrigger
from jarbis_body.reflex import Decision, Reflex, Signal
from jarbis_body.state.machine import StateMachine
from jarbis_body.state.states import JarbisState, StateContext
from jarbis_body.state.writer import SessionLog, StateWriter

# Cada cuánto el cuerpo reescribe el archivo de estado aunque nada cambie.
# La cara lo usa para saber si está vivo; tiene que ser bastante menor que el
# umbral de "sin señal" del visor (5 s).
HEARTBEAT_S = 2.0

# Frases que cierran la conversación sin pasar por el cerebro. Heredadas del
# original, que las tenía hardcodeadas en EXIT_PHRASES.
EXIT_PHRASES = (
    "listo nada mas",
    "listo nada más",
    "nada mas",
    "nada más",
    "gracias jarbis",
    "ya esta",
    "ya está",
    "chau",
    "dormite",
    "callate",
    "cállate",
)


class JarbisBody:
    """
    Junta todas las capas y las conecta al puente.

        body = JarbisBody()
        body.start()
        body.wait()
    """

    def __init__(self) -> None:
        self.machine = StateMachine()
        self.writer = StateWriter()
        self.log = SessionLog()
        self.bridge = BridgeServer()
        self.tts = JarbisTTS()

        # El reflejo arranca sin sensores: nada lo despierta todavía. Está acá
        # para que enchufar un sensor mañana sea una línea, no un rediseño.
        self.reflex = Reflex()
        # El único disparador proactivo que está prendido. Arranca apagado por
        # configuración; se prende desde el panel.
        self.greeting = GreetingTrigger(on_fire=self.notice)

        self._voice: VoiceMode | None = None
        self._wake: Any | None = None
        self._hotkey: Any | None = None

        self._turn_started_at: float | None = None
        self._conversation = False
        # Modo, cambiable en caliente desde la cara.
        self._ptt = settings.push_to_talk
        self._follow_up = settings.follow_up
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._heartbeat: threading.Thread | None = None

    # ========================================================================
    # Arranque
    # ========================================================================

    def start(self) -> bool:
        """Levanta todo. Devuelve False si algo esencial no arrancó."""
        logger.info("Arrancando el cuerpo de Jarbis")

        self.machine.on_change(self._on_state_change)
        self.bridge.on_command(self._on_brain_command)
        self.reflex.on_speak(self._on_reflex)

        if not self.bridge.start():
            logger.error("Sin puente no hay cuerpo: el cerebro no podría escuchar")
            return False

        # El TTS se precarga para que la primera respuesta no pague la carga.
        if settings.tts_enabled:
            self.tts.preload()

        self._voice = VoiceMode(preload_model=settings.whisper_preload)
        self._voice.on_transcription(self._on_transcription)
        # El VAD avisa por acá. El original llegaba a este atributo privado desde
        # afuera; se mantiene porque VoiceMode no expone un setter, pero al menos
        # queda dicho en un solo lugar.
        self._voice._recorder.on_silence = self._on_silence

        if settings.wakeword_enabled and not self._start_wakeword():
            logger.warning(
                "Sin wake word: solo vas a poder activarlo con la hotkey "
                f"({settings.hotkey_combo})"
            )

        if settings.hotkey_enabled:
            self._start_hotkey()

        self.writer.update(
            listening=False,
            speaking=False,
            wakeword_backend=settings.wakeword_backend,
            voice=self.tts._voice_name,
            ptt=self._ptt,
            follow_up=self._follow_up,
            greeting=settings.greeting_enabled,
        )
        self.greeting.start()

        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop, name="JarbisHeartbeat", daemon=True
        )
        self._heartbeat.start()

        self.log.append("start", voice=self.tts._voice_name, model=settings.whisper_model)
        logger.info("Cuerpo listo")
        return True

    def _heartbeat_loop(self) -> None:
        """Refresca el archivo de estado para que la cara sepa que está vivo."""
        while not self._stop_event.wait(HEARTBEAT_S):
            try:
                self.writer.heartbeat()
            except Exception as exc:
                logger.debug(f"Latido falló: {exc}")

    def _start_wakeword(self) -> bool:
        """Arranca el detector de wake word según el backend configurado."""
        try:
            if settings.wakeword_backend == "onnx":
                from jarbis_body.audio.wakeword_onnx import OnnxWakeWordDetector

                detector = OnnxWakeWordDetector()
            else:
                from jarbis_body.audio.wakeword import WakeWordDetector

                detector = WakeWordDetector(prefer_openwakeword=False)

            detector.on_wake_word(self._on_wake_word)
            if not detector.start():
                return False
            self._wake = detector
            return True
        except Exception as exc:
            logger.error(f"No se pudo arrancar el wake word: {exc}")
            return False

    def _start_hotkey(self) -> None:
        """
        Arranca las hotkeys globales como camino alternativo de activación.

        Importa que exista aunque el wake word funcione: el wake word es inglés
        y hoy no dispara con pronunciación castellana (ver docs/MAÑANA.md), así
        que mientras eso no se resuelva la hotkey es el camino principal, no el
        de respaldo.
        """
        try:
            from jarbis_body.audio.hotkey import HotkeyAction, HotkeyHandler

            def on_hotkey(action: HotkeyAction) -> None:
                if action is HotkeyAction.TOGGLE_RECORDING:
                    self.activate("hotkey")
                elif action is HotkeyAction.CANCEL_RECORDING:
                    self.tts.stop()
                    self._conversation = False
                    self.machine.transition_to(JarbisState.DORMIDO, force=True)
                elif action is HotkeyAction.STOP_SYSTEM:
                    logger.info("Apagado pedido por hotkey")
                    self._stop_event.set()

            handler = HotkeyHandler()
            handler.on_hotkey(on_hotkey)
            if handler.start():
                self._hotkey = handler
                logger.info("Hotkeys: ctrl+shift+g activar · escape cancelar · ctrl+shift+q salir")
        except Exception as exc:
            # No es fatal, pero sí es grave mientras el wake word no sirva.
            logger.warning(f"Hotkeys no disponibles: {exc}")

    # ========================================================================
    # Entrada: wake word, hotkey, comandos del cerebro
    # ========================================================================

    def _on_wake_word(self, result: Any) -> None:
        """Se escuchó el nombre."""
        self.bridge.emit(WakeDetected(word=result.word, score=result.confidence))
        self.writer.update(last_wake_score=round(result.confidence, 3))
        self.activate("wakeword")

    def activate(self, source: str = "manual") -> bool:
        """
        Abre el micrófono para un turno.

        Si estaba hablando, lo corta primero: hablarle encima tiene que
        funcionar, y eso es lo que hace que se sienta una conversación y no un
        intercambio de mensajes.
        """
        with self._lock:
            if self.machine.state is JarbisState.HABLANDO:
                self.tts.stop()
                self.bridge.emit(Interrupted(reason=source))
                self.log.append("interrupted", source=source)

            if self.machine.state is JarbisState.PENSANDO:
                # El cerebro está trabajando. Cortarlo acá sería tirar el turno;
                # mejor avisar y dejar que decida.
                self.bridge.emit(Interrupted(reason=f"{source}-while-thinking"))
                return False

            if not self.machine.transition_to(JarbisState.ESCUCHANDO, force=True):
                return False

        if self._voice is None:
            return False

        # Si ya venía grabando, hay que cerrar ESO antes de abrir lo nuevo.
        #
        # Sin esto, `start_recording()` devolvía False con un "Already
        # recording" y el orquestador lo reportaba como "no se pudo abrir el
        # micrófono". El turno viejo quedaba con el stream abierto y desde ahí
        # TODO turno siguiente fallaba igual: Jarbis dejaba de contestar y el
        # log culpaba al micrófono. Pasa al hablarle encima, que es justo cuando
        # más ganas tenés de que funcione.
        if self._voice.is_recording:
            logger.info("Ya estaba grabando; descarto ese turno y abro uno nuevo")
            try:
                self._voice.stop_recording()
            except Exception as exc:
                logger.debug(f"Cerrando la grabación anterior: {exc}")

        # El wake word y la grabación comparten el micrófono. Soltarlo antes
        # evita el conflicto de dos InputStream sobre el mismo dispositivo.
        self._pause_wakeword()

        # En push-to-talk el VAD no cierra: cierra `close_turn()` al soltar.
        self._voice._recorder.auto_close = not self._ptt

        if not self._voice.start_recording():
            logger.error(
                "El micrófono no abrió. ¿Lo tiene otra app, o quedó otra "
                "instancia del cuerpo corriendo?"
            )
            self.machine.set_error("micrófono no disponible")
            self._resume_wakeword()
            return False

        self._turn_started_at = time.perf_counter()
        logger.info(f"Escuchando (activado por {source})")
        return True

    def _on_silence(self) -> None:
        """
        El VAD cerró el turno. Corre en su propio thread (lo lanza el recorder),
        así que puede bloquear transcribiendo sin cortar el audio.
        """
        if self._voice is None or not self._voice.is_recording:
            return

        self.machine.transition_to(JarbisState.PENSANDO, force=True)
        try:
            # Esto dispara on_transcription cuando termina.
            self._voice.stop_and_transcribe()
        except Exception as exc:
            logger.error(f"Falló la transcripción: {exc}")
            self.bridge.emit(BodyError(message=str(exc), where="transcripción"))
            self.machine.set_error("falló la transcripción")
        finally:
            self._resume_wakeword()

    def _on_transcription(self, result: TranscriptionResult) -> None:
        """
        Hay texto. Acá está la inversión: se empuja y se termina.

        Antes esto guardaba en `_pending_prompt` y esperaba que el cerebro
        viniera a buscarlo.
        """
        text = (result.text or "").strip()
        latency_ms = (
            (time.perf_counter() - self._turn_started_at) * 1000
            if self._turn_started_at
            else 0.0
        )

        if not text:
            logger.info("Transcripción vacía; volviendo a dormir")
            self.machine.transition_to(JarbisState.DORMIDO, force=True)
            return

        logger.info(f"Escuché: {text!r} ({latency_ms:.0f} ms)")
        self.writer.set_heard(text, latency_ms=round(latency_ms, 1))
        self.log.append(
            "heard",
            text=text,
            confidence=round(result.confidence, 3),
            audio_s=round(result.duration_s, 2),
            stt_s=round(result.transcription_time_s, 2),
            latency_ms=round(latency_ms, 1),
        )

        if self._is_exit_phrase(text):
            logger.info("Frase de cierre; a dormir")
            self._conversation = False
            self.tts.speak("Dale", block=False)
            self.machine.transition_to(JarbisState.DORMIDO, force=True)
            return

        delivered = self.bridge.emit(
            Turn(
                text=text,
                confidence=result.confidence,
                duration_s=result.duration_s,
                latency_ms=latency_ms,
            )
        )

        if not delivered:
            # Sin cerebro conectado no hay a quién preguntarle. Se dice en voz
            # alta en vez de fallar en silencio, que es lo que haría pensar que
            # el micrófono no funciona.
            logger.warning("Nadie escuchando en el puente; el turno se pierde")
            self.tts.speak("No tengo cerebro conectado", block=False)
            self.machine.transition_to(JarbisState.DORMIDO, force=True)

    @staticmethod
    def _is_exit_phrase(text: str) -> bool:
        lowered = text.lower().strip(" .,!¡?¿")
        return any(phrase in lowered for phrase in EXIT_PHRASES)

    # ========================================================================
    # Comandos del cerebro
    # ========================================================================

    def _on_brain_command(self, message: dict[str, Any]) -> None:
        """Ejecuta lo que pidió el cerebro. Corre en el thread del puente."""
        kind = message.get("type")

        if kind == "ping":
            return

        if kind == "say":
            self._say(
                str(message.get("text") or ""),
                then_listen=bool(message.get("then_listen", False)),
            )
        elif kind == "stop":
            self.tts.stop()
        elif kind == "listen":
            self.activate("brain")
        elif kind == "close":
            self.close_turn("botón soltado")
        elif kind == "mode":
            self._set_mode(message)
        elif kind == "sleep":
            self._conversation = False
            self.tts.stop()
            self.machine.transition_to(JarbisState.DORMIDO, force=True)

    def close_turn(self, reason: str = "pedido") -> None:
        """
        Cierra el turno ya y transcribe lo grabado.

        Es la mitad de arriba del push-to-talk: `activate()` abre al apretar,
        esto cierra al soltar. Idempotente — soltar dos veces no transcribe dos
        veces, porque `_on_silence` chequea que siga grabando.
        """
        if self._voice is None or not self._voice.is_recording:
            return
        logger.info(f"Cierro el turno a pedido ({reason})")
        self._on_silence()

    def _set_mode(self, message: dict[str, Any]) -> None:
        """Cambia el modo en caliente, sin reiniciar nada."""
        if "ptt" in message:
            self._ptt = bool(message["ptt"])
            if self._voice is not None:
                self._voice._recorder.auto_close = not self._ptt
        if "follow_up" in message:
            self._follow_up = bool(message["follow_up"])
        if "tts" in message:
            # Silenciar: deja de hablar pero sigue escuchando y contestando por
            # escrito en el panel. Corta también lo que esté diciendo ahora.
            settings.tts_enabled = bool(message["tts"])
            if not settings.tts_enabled:
                self.tts.stop()

        if "greeting" in message:
            # Se cambia el setting en vivo y se rearranca el disparador: así no
            # hace falta reiniciar el cuerpo para prender el saludo.
            settings.greeting_enabled = bool(message["greeting"])
            self.greeting.stop()
            if settings.greeting_enabled:
                self.greeting.start()
            else:
                logger.info("Saludo de la mañana: apagado")

        logger.info(
            f"Modo: {'push-to-talk' if self._ptt else 'automático'}, "
            f"{'conversación' if self._follow_up else 'una pregunta'}, "
            f"saludo {'prendido' if settings.greeting_enabled else 'apagado'}, "
            f"voz {'prendida' if settings.tts_enabled else 'silenciada'}"
        )
        self.writer.update(
            ptt=self._ptt,
            follow_up=self._follow_up,
            greeting=settings.greeting_enabled,
            tts=settings.tts_enabled,
        )

    def _say(self, text: str, *, then_listen: bool = False) -> None:
        """Dice la respuesta del cerebro y decide si sigue escuchando."""
        if not text.strip():
            self.machine.transition_to(JarbisState.DORMIDO, force=True)
            return

        # El cerebro pide `then_listen`, pero la última palabra es tuya: si
        # pusiste "una sola pregunta", contesta y se duerme.
        seguir = then_listen and self._follow_up

        self.writer.set_said(text)
        self.log.append("said", text=text, then_listen=seguir)
        self.machine.transition_to(JarbisState.HABLANDO, force=True)
        self._conversation = seguir

        def run() -> None:
            self.tts.speak(text, block=True)
            # Si se cortó porque le hablaste encima, `activate()` ya movió el
            # estado; no hay que pisarlo.
            if self.machine.state is not JarbisState.HABLANDO:
                return
            if self._conversation:
                self.activate("conversación")
            else:
                self.machine.transition_to(JarbisState.DORMIDO, force=True)

        threading.Thread(target=run, name="JarbisSay", daemon=True).start()

    # ========================================================================
    # Micrófono compartido
    # ========================================================================

    def _pause_wakeword(self) -> None:
        """Suelta el micrófono del wake word para que lo tome la grabación."""
        if self._wake is not None:
            try:
                self._wake.stop()
            except Exception as exc:
                logger.debug(f"Pausando el wake word: {exc}")

    def _resume_wakeword(self) -> None:
        """Devuelve el micrófono al wake word."""
        if self._wake is not None and settings.wakeword_enabled:
            try:
                self._wake.start()
            except Exception as exc:
                logger.debug(f"Reanudando el wake word: {exc}")

    # ========================================================================
    # Estado
    # ========================================================================

    def _on_state_change(
        self, previous: JarbisState, current: JarbisState, context: StateContext
    ) -> None:
        """Refleja el estado en el archivo que lee la cara y en el puente."""
        self.writer.set_state(current, error_message=context.error_message)
        self.bridge.emit(StateChanged(state=current.value, previous=previous.value))

        # El reflejo no se encima: mientras no estés dormido, lo que llegue
        # espera. Es el filtro que evita que te interrumpa a vos mismo.
        self.reflex.set_busy(current is not JarbisState.DORMIDO)

        if current is JarbisState.DORMIDO:
            self._turn_started_at = None

    # ========================================================================
    # Proactividad
    # ========================================================================

    def notice(self, signal: Signal) -> None:
        """
        Punto de entrada para los sensores.

        Un file watcher, un cron o ActivityWatch llaman acá. El reflejo decide
        si amerita hablar; esta función no decide nada.
        """
        self.reflex.consider(signal)

    def _on_reflex(self, decision: Decision) -> None:
        """El reflejo decidió hablar primero."""
        signal = decision.signal
        if signal is None:
            return

        logger.info(f"Hablando primero: {signal.key}")
        self.log.append(
            "proactive", key=signal.key, message=signal.message,
            urgency=signal.urgency.value, reason=decision.reason,
        )

        # Dos clases de señal.
        #
        # Con `ask_brain`, el mensaje es una PREGUNTA para el cerebro y la
        # respuesta sale por el camino normal de un turno. Lo usa el saludo de
        # la mañana: el texto tiene que armarlo el cerebro con tu contexto de
        # MyJarbis, no el cuerpo con una frase fija.
        #
        # Sin `ask_brain`, el mensaje ya viene listo y se dice tal cual. Es para
        # un sensor que sabe exactamente qué avisar y no necesita que nadie
        # piense.
        if signal.data.get("ask_brain"):
            self.machine.transition_to(JarbisState.PENSANDO, force=True)
            if not self.bridge.emit(Turn(text=signal.message, confidence=1.0)):
                logger.warning("Sin cerebro conectado; el saludo se pierde")
                self.machine.transition_to(JarbisState.DORMIDO, force=True)
            return

        self.bridge.emit(
            Proactive(
                message=signal.message,
                key=signal.key,
                urgency=signal.urgency.value,
            )
        )
        self._say(signal.message, then_listen=True)

    # ========================================================================
    # Ciclo de vida
    # ========================================================================

    def wait(self) -> None:
        """Bloquea hasta que se pida el apagado."""
        try:
            while not self._stop_event.wait(0.5):
                pass
        except KeyboardInterrupt:
            logger.info("Ctrl+C")

    def stop(self) -> None:
        """Apaga todo en orden inverso al arranque."""
        logger.info("Apagando el cuerpo")
        self._stop_event.set()

        self.tts.stop()

        self.greeting.stop()

        if self._hotkey is not None:
            try:
                self._hotkey.stop()
            except Exception:
                pass

        self._pause_wakeword()

        if self._voice is not None and self._voice.is_recording:
            try:
                self._voice.stop_recording()
            except Exception:
                pass

        if self._heartbeat is not None:
            self._heartbeat.join(timeout=HEARTBEAT_S + 1)
            self._heartbeat = None

        self.machine.shutdown()
        self.writer.set_state(JarbisState.DORMIDO)
        self.log.append("stop")
        self.bridge.stop()
        logger.info("Listo")
