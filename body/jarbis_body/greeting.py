"""
El saludo de la mañana: la única proactividad que está prendida.

El alcance fue explícito: *"todavía no, una configuración de que le
podemos despertar nosotros al presionar, o configurarle el wake up para que me
diga buen día señor y ya"*. Así que el reflejo tiene **un solo disparador**, y
este es.

## Por qué no es un texto fijo

"Buen día señor" dicho todas las mañanas es un despertador con voz. Lo que hace
útil a un saludo es que te diga lo que importa hoy: qué vence, qué quedó a
medias, qué estabas por hacer. Eso lo sabe el cerebro, que tiene MyJarbis.

Entonces el disparador no manda texto para decir: manda una **pregunta** al
cerebro, por el mismo camino que si le hubieras hablado vos. El cuerpo no
inventa nada y el saludo sale con tu contexto real.

## Cómo evita ser molesto

Pasa por los mismos filtros que cualquier otra señal del reflejo (cooldown,
horario, no encimarse si estás hablando) más dos propios:

- una sola vez por día, y queda anotado en disco para que reiniciar la máquina
  tres veces en la mañana no implique tres saludos
- sólo dentro de una ventana horaria; si prendiste la máquina a las 15, ya pasó

Y arranca **apagado**. Se prende desde el panel cuando lo quieras.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, time
from pathlib import Path

from loguru import logger

from jarbis_body.config import settings
from jarbis_body.reflex import Signal, Urgency

# Cada cuánto se fija si llegó el momento. Un minuto alcanza de sobra para algo
# que pasa una vez por día, y no se nota en la CPU.
CHECK_S = 60.0


class GreetingTrigger:
    """
    Dispara el saludo una vez por día, dentro de la ventana horaria.

        trigger = GreetingTrigger(on_fire=body.notice)
        trigger.start()
    """

    def __init__(
        self,
        on_fire,
        state_path: Path | None = None,
    ) -> None:
        self._on_fire = on_fire
        self._path = state_path or (settings.var_dir / "greeting.json")
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ control

    def start(self) -> bool:
        """Arranca el chequeo periódico. False si el saludo está apagado."""
        if not settings.greeting_enabled:
            logger.info("Saludo de la mañana: apagado")
            return False

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="JarbisGreeting", daemon=True
        )
        self._thread.start()
        logger.info(
            f"Saludo de la mañana: entre {settings.greeting_from} y "
            f"{settings.greeting_until}"
        )
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -------------------------------------------------------------------- bucle

    def _loop(self) -> None:
        # Un chequeo apenas arranca: si prendiste la máquina a las 9:05, no
        # tiene sentido esperar al minuto siguiente.
        self._check()
        while not self._stop.wait(CHECK_S):
            self._check()

    def _check(self, now: datetime | None = None) -> bool:
        """Evalúa si corresponde saludar. Devuelve si disparó."""
        now = now or datetime.now()

        if not settings.greeting_enabled:
            return False
        if not self._in_window(now.time()):
            return False
        if self._already_today(now.date()):
            return False

        logger.info("Saludando")
        self._mark(now.date())

        self._on_fire(
            Signal(
                key="saludo-matutino",
                message=settings.greeting_prompt,
                urgency=Urgency.NORMAL,
                at=now,
                # El orquestador ve esto y manda la señal al CEREBRO como si le
                # hubieras hablado, en vez de decir el texto tal cual.
                data={"ask_brain": True},
            )
        )
        return True

    # ------------------------------------------------------------------ ventana

    @staticmethod
    def _parse(value: str) -> time:
        """'8:30' o '08:30' a time. Si no parsea, un default razonable."""
        try:
            hh, _, mm = value.partition(":")
            return time(int(hh), int(mm or 0))
        except (ValueError, TypeError):
            logger.warning(f"Horario de saludo inválido: {value!r}")
            return time(9, 0)

    def _in_window(self, moment: time) -> bool:
        desde = self._parse(settings.greeting_from)
        hasta = self._parse(settings.greeting_until)
        if desde <= hasta:
            return desde <= moment <= hasta
        # Ventana que cruza medianoche, por si algún día trabajás de noche.
        return moment >= desde or moment <= hasta

    # ---------------------------------------------------------------- una vez

    def _already_today(self, today: date) -> bool:
        """
        ¿Ya saludó hoy?

        Se guarda en disco, no en memoria: el supervisor reinicia procesos y
        encender la máquina dos veces en la mañana no puede ser dos saludos.
        """
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data.get("last") == today.isoformat()
        except (OSError, ValueError):
            return False

    def _mark(self, today: date) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"last": today.isoformat()}), encoding="utf-8"
            )
        except OSError as exc:
            # Si no se puede anotar, mejor no saludar que saludar en loop.
            logger.error(f"No pude anotar el saludo: {exc}")

    def reset(self) -> None:
        """Olvida que ya saludó. Para probarlo sin esperar a mañana."""
        try:
            self._path.unlink()
        except OSError:
            pass
