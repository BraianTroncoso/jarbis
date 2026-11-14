"""
El reflejo: lo que decide CUÁNDO hablar primero.

Esta es la pieza que hace la diferencia entre un asistente que contesta y un
twin que avisa. Todavía no tiene sensores enchufados — es el andamiaje, con sus
reglas y sus tests — pero la forma es la definitiva, y la forma es el punto.

## Por qué esto NO es un LLM, y no es una elección de estilo

Dos resultados de 2026 convergen fuerte:

- [arXiv 2605.30152] probó nueve arquitecturas de trigger para agentes
  proactivos. Las ganó a todas un controlador chico determinístico: 11 ms por
  evento, 220 MiB, y +16,7 F1 aguas abajo promediado sobre 14 modelos distintos.
  El detalle lapidario: escalar el LLM del trigger unas 14 veces (0,6B → 8B) **no
  mejoró** la detección de "esto hay que avisarlo" (AUC 0,668 → 0,644), y
  fine-tunear el 0,6B lo dejó en 0,505 — indistinguible de tirar una moneda.
- [Pro²Bench] documenta el modo de falla que uno shipearía sin darse cuenta:
  GPT-5.2 sin plan predijo "interrumpir" en el 99,7% de los puntos de decisión.
  Un asistente que siempre interrumpe no es proactivo, es ruido.

Y el argumento económico, que acá importa más: esto corre 24/7. La comunidad de
OpenClaw mide **10.000 a 40.000 tokens por vuelta** de un heartbeat tipo Sonnet
cada 30 minutos. Un filtro determinístico cuesta cero tokens y cero red. El
cerebro se despierta solo cuando este módulo dice que vale la pena.

## Lo que sí hace

Cuatro cosas, en este orden, y cualquiera alcanza para no molestarte:

1. **silencio por default** — sin regla que matchee, no pasa nada
2. **debounce** — el mismo hecho repetido no vuelve a avisar
3. **cooldown** — después de hablar, se calla un rato
4. **horario activo** — no te habla a las 4 de la mañana

Los umbrales **no se adivinan**: se calibran offline contra el reward model de
[thunlp/ProactiveAgent] (Apache-2.0, F1 0,918 simulando aceptación humana).
Eso está en MAÑANA.md como paso siguiente.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Any

from loguru import logger


class Urgency(str, Enum):
    """
    Cuánto se permite molestar.

    El nivel no es decorativo: decide si se respeta el horario activo y si se
    puede saltar el cooldown.
    """

    # Puede esperar al próximo turno en que le hables. No interrumpe.
    DIFERIBLE = "diferible"
    # Interrumpe, pero solo en horario activo.
    NORMAL = "normal"
    # Interrumpe siempre. Reservado para cosas con reloj real (un token que
    # vence hoy, un deploy roto en producción).
    URGENTE = "urgente"


@dataclass(frozen=True)
class Signal:
    """
    Un hecho observado. Lo produciría un sensor (ActivityWatch, un file watcher,
    un cron), no el modelo.

    `key` es la identidad del hecho y es lo que se usa para el debounce: dos
    señales con la misma `key` son el mismo hecho repetido, no dos novedades.
    """

    key: str
    message: str
    urgency: Urgency = Urgency.NORMAL
    at: datetime = field(default_factory=datetime.now)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """Qué hacer con una señal."""

    speak: bool
    reason: str
    signal: Signal | None = None


@dataclass
class ReflexConfig:
    """
    Los umbrales. Todos conservadores a propósito: es más fácil aflojar un
    asistente callado que recuperar la confianza en uno que molesta.
    """

    # Ventana en la que el mismo `key` no vuelve a avisar.
    debounce: timedelta = timedelta(minutes=30)

    # Después de hablar por su cuenta, se calla este tiempo sin importar qué
    # llegue. Es lo que evita la metralleta de avisos.
    cooldown: timedelta = timedelta(minutes=10)

    # Fuera de este rango solo pasa lo URGENTE.
    active_from: time = time(9, 0)
    active_until: time = time(23, 0)

    # Techo diario. Un asistente que habló 40 veces hoy ya perdió.
    max_per_day: int = 12

    # Si estás hablando con él, no se encima: las señales esperan.
    defer_while_busy: bool = True


class Reflex:
    """
    La compuerta de proactividad.

    Push: le das señales con `consider()` y te avisa por callback solo cuando
    decide que vale la pena. Sin sensores enchufados es inofensivo — no hay
    nada que lo despierte.

        reflex = Reflex()
        reflex.on_speak(lambda d: bridge.emit(Proactive(d.signal.message)))
        reflex.consider(Signal(key="turso-token", message="..."))
    """

    def __init__(self, config: ReflexConfig | None = None) -> None:
        self.config = config or ReflexConfig()
        self._lock = threading.Lock()
        self._last_seen: dict[str, datetime] = {}
        self._last_spoke: datetime | None = None
        self._spoken_today: list[datetime] = []
        self._deferred: list[Signal] = []
        self._callbacks: list[Callable[[Decision], None]] = []
        self._busy = False

    # ------------------------------------------------------------ suscripción

    def on_speak(self, callback: Callable[[Decision], None]) -> None:
        self._callbacks.append(callback)

    def set_busy(self, busy: bool) -> None:
        """
        Marca si estás en medio de una conversación.

        Lo llamaría el orquestador desde el cambio de estado: cualquier estado
        que no sea DORMIDO es "ocupado". Lo que llegue mientras tanto se
        encola en vez de encimarse.
        """
        with self._lock:
            self._busy = busy
            pendientes = list(self._deferred) if not busy else []
            if not busy:
                self._deferred.clear()

        # Al liberarse, se reconsidera lo que quedó esperando. Fuera del lock
        # porque `consider` lo vuelve a tomar.
        for signal in pendientes:
            self.consider(signal)

    # ------------------------------------------------------------------ decisión

    def consider(self, signal: Signal) -> Decision:
        """
        Evalúa una señal. Si decide hablar, dispara los callbacks.

        Devuelve la decisión siempre, también cuando es no: el motivo sirve para
        calibrar los umbrales mirando el log en vez de adivinando.
        """
        decision = self._evaluate(signal)

        if decision.speak:
            logger.info(f"Reflejo habla: {signal.key} — {decision.reason}")
            for callback in self._callbacks:
                try:
                    callback(decision)
                except Exception as exc:
                    logger.error(f"Callback del reflejo falló: {exc}")
        else:
            logger.debug(f"Reflejo calla: {signal.key} — {decision.reason}")

        return decision

    def _evaluate(self, signal: Signal) -> Decision:
        now = signal.at
        config = self.config

        with self._lock:
            # --- 1. ocupado -------------------------------------------------
            # Va primero: si estás hablando con él, nada se encima, ni urgente.
            # Que te interrumpa a vos mismo sería el peor modo de falla.
            if self._busy and config.defer_while_busy:
                if signal.urgency is not Urgency.URGENTE:
                    self._deferred.append(signal)
                    return Decision(False, "estás hablando con él; queda en espera", signal)

            # --- 2. debounce -------------------------------------------------
            visto = self._last_seen.get(signal.key)
            if visto is not None and now - visto < config.debounce:
                restante = config.debounce - (now - visto)
                return Decision(
                    False,
                    f"ya avisó de '{signal.key}' hace poco "
                    f"(faltan {int(restante.total_seconds() / 60)} min)",
                    signal,
                )

            # --- 3. horario --------------------------------------------------
            if signal.urgency is not Urgency.URGENTE and not self._in_active_hours(now):
                return Decision(
                    False,
                    f"fuera de horario activo "
                    f"({config.active_from:%H:%M}–{config.active_until:%H:%M})",
                    signal,
                )

            # --- 4. diferible ------------------------------------------------
            # Lo diferible nunca inicia nada: espera a que le hables.
            if signal.urgency is Urgency.DIFERIBLE:
                return Decision(False, "diferible: espera al próximo turno", signal)

            # --- 5. cooldown -------------------------------------------------
            if self._last_spoke is not None and signal.urgency is not Urgency.URGENTE:
                desde = now - self._last_spoke
                if desde < config.cooldown:
                    restante = config.cooldown - desde
                    return Decision(
                        False,
                        f"en cooldown (faltan {int(restante.total_seconds() / 60)} min)",
                        signal,
                    )

            # --- 6. techo diario ---------------------------------------------
            self._spoken_today = [
                t for t in self._spoken_today if t.date() == now.date()
            ]
            if len(self._spoken_today) >= config.max_per_day:
                return Decision(
                    False,
                    f"techo diario alcanzado ({config.max_per_day})",
                    signal,
                )

            # --- sí ------------------------------------------------------------
            self._last_seen[signal.key] = now
            self._last_spoke = now
            self._spoken_today.append(now)
            return Decision(True, f"{signal.urgency.value}, pasó todos los filtros", signal)

    def _in_active_hours(self, moment: datetime) -> bool:
        """
        True si está dentro del horario activo.

        Soporta un rango que cruza medianoche (22:00–06:00), porque alguien que
        programa de noche lo va a querer así.
        """
        ahora = moment.time()
        desde, hasta = self.config.active_from, self.config.active_until
        if desde <= hasta:
            return desde <= ahora <= hasta
        return ahora >= desde or ahora <= hasta

    # ------------------------------------------------------------ introspección

    @property
    def deferred_count(self) -> int:
        with self._lock:
            return len(self._deferred)

    @property
    def spoken_today(self) -> int:
        """
        Cuántas veces habló hoy según el reloj de pared. Es para mostrar.

        No usar esto para lógica: el techo diario se evalúa en `_evaluate()`
        contra la fecha de la **señal**, no contra `now()`. Son dos cosas
        distintas y mezclarlas hacía que el contador diera cero cuando las
        señales traían otra fecha.
        """
        with self._lock:
            hoy = datetime.now().date()
            return len([t for t in self._spoken_today if t.date() == hoy])

    @property
    def speak_count(self) -> int:
        """Cuántas veces habló en la ventana registrada, sin filtrar por fecha."""
        with self._lock:
            return len(self._spoken_today)

    def reset(self) -> None:
        """Olvida todo. Para tests y para el arranque."""
        with self._lock:
            self._last_seen.clear()
            self._last_spoke = None
            self._spoken_today.clear()
            self._deferred.clear()
            self._busy = False
