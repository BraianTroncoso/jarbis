"""
El supervisor: levanta las cuatro piezas y las mantiene vivas.

    python -m jarbis_body.supervisor

Jarbis son cuatro procesos: el cuerpo (audio y puente), la cara (el panel web),
el robot (la mascota) y el cerebro (que vive en WSL). Arrancarlos a mano es
cuatro comandos y acordarse del orden; esto es uno solo.

## Por qué cuatro procesos y no uno

Cada uno tiene su propio bucle y no se llevan bien en el mismo proceso: el
cuerpo corre asyncio en un thread aparte más los callbacks de PortAudio, la cara
es un `http.server` con un thread por request, y tkinter **exige** ser el thread
principal. Meterlos juntos sería pelear contra tres runtimes a la vez para
ahorrar tres PIDs.

Y separados tienen una propiedad que importa: si el robot se cuelga, seguís
pudiendo hablarle por el panel. Si el cerebro se cae, el cuerpo sigue
escuchando y lo dice.

## La política de reinicio

Si algo se muere, se reinicia. Con backoff, porque un proceso que falla al
arrancar en bucle cerrado es peor que uno caído: consume CPU y llena el log.
Después de varios intentos seguidos se baja los brazos con ese componente y lo
deja anotado, en vez de reintentar para siempre.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jarbis")

# Cada cuánto se revisa que sigan vivos.
CHECK_S = 2.0
# Después de este tiempo andando, se considera que arrancó bien y se resetea el
# contador de fallas. Sin esto, un proceso que anda tres horas y después se cae
# arrastraría los fallos de la primera mañana.
HEALTHY_S = 60.0
# Cuántas fallas seguidas antes de darse por vencido con un componente.
MAX_FAILS = 5


@dataclass
class Component:
    """Una pieza supervisada."""

    name: str
    cmd: list[str]
    essential: bool = False     # si muere y no vuelve, ¿se baja todo?
    optional: bool = False      # si no arranca nunca, ¿seguimos igual?

    proc: subprocess.Popen | None = field(default=None, init=False)
    fails: int = field(default=0, init=False)
    started_at: float = field(default=0.0, init=False)
    next_try: float = field(default=0.0, init=False)
    dead: bool = field(default=False, init=False)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def backoff(self) -> float:
        """1, 2, 4, 8... hasta 30 segundos."""
        return min(2.0 ** self.fails, 30.0)


class Supervisor:
    def __init__(self, components: list[Component]) -> None:
        self.components = components
        self._stopping = False

    # ------------------------------------------------------------------ arranque

    def start(self, component: Component) -> bool:
        try:
            component.proc = subprocess.Popen(
                component.cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Sin ventana de consola: esto arranca con Windows y nadie
                # quiere cuatro ventanas negras al prender la máquina.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            component.started_at = time.monotonic()
            log.info(f"{component.name}: arrancado (pid {component.proc.pid})")
            return True
        except Exception as exc:
            log.error(f"{component.name}: no arrancó — {exc}")
            component.fails += 1
            component.next_try = time.monotonic() + component.backoff()
            return False

    def stop(self, component: Component) -> None:
        if component.proc is None:
            return
        try:
            component.proc.terminate()
            component.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            component.proc.kill()
        except Exception:
            pass
        component.proc = None

    # --------------------------------------------------------------------- bucle

    def run(self) -> int:
        log.info("Arrancando Jarbis")

        for component in self.components:
            self.start(component)
            # El cuerpo primero y con margen: es el que crea el token del puente
            # y abre el puerto. Los demás lo buscan, así que si todavía no está
            # arrancan reintentando — anda igual, pero el log queda sucio.
            if component.name == "cuerpo":
                time.sleep(6.0)

        log.info("Todo arriba. Ctrl+C para bajar.")

        try:
            while not self._stopping:
                time.sleep(CHECK_S)
                self._check()
        except KeyboardInterrupt:
            log.info("Bajando")

        self.shutdown()
        return 0

    def _check(self) -> None:
        ahora = time.monotonic()

        for component in self.components:
            if component.dead:
                continue

            if component.alive:
                # Lleva lo suficiente andando: se le perdonan las fallas viejas.
                if component.fails and ahora - component.started_at > HEALTHY_S:
                    log.info(f"{component.name}: estable, reseteo el contador")
                    component.fails = 0
                continue

            if component.proc is not None:
                code = component.proc.poll()
                log.warning(f"{component.name}: se murió (código {code})")
                component.proc = None
                component.fails += 1
                component.next_try = ahora + component.backoff()

            if ahora < component.next_try:
                continue

            if component.fails >= MAX_FAILS:
                component.dead = True
                nivel = log.error if component.essential else log.warning
                nivel(
                    f"{component.name}: {component.fails} fallas seguidas, "
                    "lo dejo. Mirá su log."
                )
                if component.essential:
                    log.error("Era esencial; bajo todo.")
                    self._stopping = True
                continue

            log.info(f"{component.name}: reintento {component.fails}")
            self.start(component)

    def shutdown(self) -> None:
        # En orden inverso: el cuerpo último, para que los demás puedan
        # despedirse mientras el puente sigue abierto.
        for component in reversed(self.components):
            if component.alive:
                log.info(f"{component.name}: bajando")
                self.stop(component)
        log.info("Listo")


# ============================================================================
# Los componentes
# ============================================================================


def build_components(
    *, with_robot: bool = True, with_brain: bool = True, robot_width: int = 128
) -> list[Component]:
    """Arma la lista según el entorno."""
    python = sys.executable
    # El robot usa pythonw: sin consola. Los demás ya van sin ventana por
    # `CREATE_NO_WINDOW`, pero tkinter se lleva mejor con su propio binario.
    pythonw = str(Path(python).with_name("pythonw.exe"))
    if not Path(pythonw).exists():
        pythonw = python

    components = [
        Component("cuerpo", [python, "-m", "jarbis_body"], essential=True),
        Component("cara", [python, "-m", "jarbis_body.face"]),
    ]

    if with_robot:
        components.append(
            Component(
                "robot",
                [pythonw, "-m", "jarbis_body.robot", "--width", str(robot_width)],
                optional=True,
            )
        )

    if with_brain:
        # El cerebro vive en WSL. `wsl.exe -e bash -lc` para que levante el
        # perfil y encuentre python3 y el `claude` del usuario.
        repo = "~/dev-own/jarbis"
        comando = (
            f"cd {repo} && PYTHONPATH=$PWD/body JARBIS_PROJECT_DIR=$PWD "
            f"exec python3 brain/client.py"
        )
        components.append(
            Component("cerebro", ["wsl.exe", "-e", "bash", "-lc", comando])
        )

    return components


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="jarbis_body.supervisor",
        description="Levanta Jarbis entero y lo mantiene vivo.",
    )
    parser.add_argument("--no-robot", action="store_true")
    parser.add_argument("--no-brain", action="store_true",
                        help="si querés correr el cerebro a mano en WSL")
    parser.add_argument("--robot-width", type=int, default=128)
    args = parser.parse_args()

    from jarbis_body.config import settings

    settings.var_dir.mkdir(parents=True, exist_ok=True)
    logging.getLogger().addHandler(
        logging.FileHandler(settings.var_dir / "supervisor.log", encoding="utf-8")
    )

    components = build_components(
        with_robot=not args.no_robot,
        with_brain=not args.no_brain,
        robot_width=args.robot_width,
    )
    supervisor = Supervisor(components)

    def bajar(_sig: int, _frame: object) -> None:
        supervisor._stopping = True

    signal.signal(signal.SIGINT, bajar)
    signal.signal(signal.SIGTERM, bajar)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, bajar)

    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
