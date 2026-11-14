"""
El cerebro: escucha el puente y contesta.

Corre en WSL, donde ya vive el twin (MyJarbis, las skills, el CLAUDE.md). El
cuerpo corre nativo en Windows porque WSL no tiene acceso real al micrófono.

Este archivo es sólo el transporte: conecta, escucha, y le pasa cada turno al
motor. Pensar es cosa de `engine.py`.

## Cómo se encuentran las dos puntas

Con `networkingMode=NAT` —que es lo que hay configurado— la VM de WSL tiene su
propia pila de red y su propio loopback. Verificado en esta máquina: con el
cuerpo escuchando en el `127.0.0.1` de Windows, desde WSL **no lo alcanza
ninguna dirección**. El reenvío de localhost de WSL va en un solo sentido.

Las dos puntas están unidas por el adaptador virtual `vEthernet (WSL)`, que
desde la VM es el default gateway. Por eso el cerebro se conecta ahí.

    python3 brain/client.py
    python3 brain/client.py --dry-run        # sin llamar a Claude
    python3 brain/client.py --subprocess     # motor viejo, sin el SDK
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import pick_engine  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jarbis.brain")

REPO = Path(__file__).resolve().parent.parent
TWIN_DIR = Path.home() / "dev-own" / "twin"
DEFAULT_PORT = 8765

def _token_candidates() -> list[Path]:
    """
    Dónde buscar el token del puente.

    El cuerpo lo escribe del lado Windows y desde WSL se lee por `/mnt/c`.
    `JARBIS_DEST` apunta al despliegue; si no está, se prueban los perfiles de
    Windows que existan. Y al final, el `var/` del repo, para el caso en que
    cuerpo y cerebro corran en el mismo sistema.
    """
    rutas: list[Path] = []

    destino = os.environ.get("JARBIS_DEST")
    if destino:
        rutas.append(Path(destino) / "var" / "bridge.token")

    usuarios = Path("/mnt/c/Users")
    if usuarios.is_dir():
        ignorar = {"public", "default", "default user", "all users"}
        for perfil in sorted(usuarios.iterdir()):
            if perfil.is_dir() and perfil.name.lower() not in ignorar:
                rutas.append(perfil / "jarbis-body" / "var" / "bridge.token")

    rutas.append(REPO / "var" / "bridge.token")
    return rutas


TOKEN_CANDIDATES = _token_candidates()


def windows_host() -> str:
    """
    La IP del host Windows vista desde WSL: el default gateway.

    Se lee de `/proc/net/route` para no depender de que `ip` esté instalado.
    """
    try:
        with open("/proc/net/route", encoding="utf-8") as handle:
            next(handle)
            for line in handle:
                fields = line.split()
                if len(fields) >= 3 and fields[1] == "00000000":
                    raw = int(fields[2], 16)
                    return ".".join(str((raw >> s) & 0xFF) for s in (0, 8, 16, 24))
    except (OSError, StopIteration, ValueError):
        pass
    return "127.0.0.1"


def default_url() -> str:
    return f"ws://{windows_host()}:{DEFAULT_PORT}"


def find_token() -> str | None:
    """Lee el token del puente de donde esté."""
    for path in TOKEN_CANDIDATES:
        try:
            if path.exists():
                token = path.read_text(encoding="utf-8").strip()
                if token:
                    log.info(f"Token leído de {path}")
                    return token
        except OSError:
            pass
    return None


class Brain:
    """Conecta el puente con el motor de pensamiento."""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        *,
        dry_run: bool = False,
        prefer_subprocess: bool = False,
        model: str = "haiku",
        escalate_to: str = "opus",
        cwd: Path | None = None,
        timeout: float = 240.0,
    ) -> None:
        self.url = url or default_url()
        self.token = token
        self.cwd = cwd or TWIN_DIR
        self._dry_run = dry_run
        self._prefer_subprocess = prefer_subprocess
        self._model = model
        self._escalate_to = escalate_to
        self._timeout = timeout

        self.engine = None
        self._turns = 0
        self._thinking: asyncio.Task | None = None

    # ===================================================================== bucle

    async def run_forever(self) -> None:
        """
        Se conecta y se reconecta con backoff. El cuerpo puede arrancar después.

        El motor se levanta UNA vez, fuera del bucle de reconexión: su costo de
        arranque es justamente lo que se quiere pagar una sola vez.
        """
        self.engine = await pick_engine(
            self.cwd,
            dry_run=self._dry_run,
            prefer_subprocess=self._prefer_subprocess,
            model=self._model,
            escalate_to=self._escalate_to,
            timeout=self._timeout,
        )

        candidatas = [self.url]
        loopback = f"ws://127.0.0.1:{DEFAULT_PORT}"
        if loopback not in candidatas:
            candidatas.append(loopback)

        delay = 1.0
        intento = 0
        while True:
            url = candidatas[intento % len(candidatas)]
            intento += 1
            try:
                await self._session(url)
                # La URL que funcionó pasa al frente y se queda. Sin esto, cada
                # reconexión alternaba con una candidata que en este setup
                # siempre falla, y el backoff crecía al doble.
                if candidatas[0] != url:
                    candidatas.remove(url)
                    candidatas.insert(0, url)
                delay = 1.0
                intento = 0
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.warning(
                    f"{url} no responde ({type(exc).__name__}); reintento en {delay:.0f}s"
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)

    async def _session(self, url: str) -> None:
        import websockets

        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "hello", "token": self.token, "as": "brain"}))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            if welcome.get("type") != "welcome":
                raise RuntimeError(f"handshake rechazado: {welcome}")

            log.info(
                f"Conectado al cuerpo en {url} (protocolo v{welcome.get('protocol')}) "
                f"· motor {self.engine.name}"
            )

            async for raw in ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                await self._handle(ws, message)

    # =================================================================== mensajes

    async def _handle(self, ws, message: dict) -> None:
        kind = message.get("type")

        if kind == "turn":
            await self._on_turn(ws, message)
        elif kind == "interrupted":
            # Le hablaste encima. Cortar lo que esté pensando: la respuesta que
            # venía ya no es a lo que estás diciendo ahora.
            log.info(f"Turno interrumpido ({message.get('reason')})")
            await self._abort()
        elif kind == "wake":
            log.info(f"Te escuchó llamarlo ({message.get('score', 0):.2f})")
        elif kind == "proactive":
            log.info(f"El cuerpo habló primero: {message.get('message','')[:70]}")
        elif kind == "error":
            log.error(f"Error del cuerpo en {message.get('where')}: {message.get('message')}")
        elif kind == "state":
            log.debug(f"Estado del cuerpo: {message.get('state')}")

    async def _abort(self) -> None:
        """Corta el turno en vuelo, si hay uno."""
        if self._thinking and not self._thinking.done():
            await self.engine.interrupt()
            self._thinking.cancel()
            try:
                await self._thinking
            except (asyncio.CancelledError, Exception):
                pass
        self._thinking = None

    async def _on_turn(self, ws, message: dict) -> None:
        text = (message.get("text") or "").strip()
        if not text:
            return

        # Si llegó un turno nuevo con uno en vuelo, gana el nuevo.
        await self._abort()

        self._turns += 1
        log.info(f"[{self._turns}] Dijiste: {text!r}")

        started = time.perf_counter()
        self._thinking = asyncio.create_task(self.engine.think(text))
        try:
            reply = await self._thinking
        except asyncio.CancelledError:
            log.info("Turno cancelado")
            return
        finally:
            self._thinking = None

        elapsed = time.perf_counter() - started
        if not reply:
            reply = "No pude pensar eso. Mirá el log."

        log.info(f"[{self._turns}] Contesto en {elapsed:.1f}s: {reply[:80]!r}")
        await ws.send(
            json.dumps({"type": "say", "text": reply, "then_listen": True})
        )

    async def close(self) -> None:
        await self._abort()
        if self.engine is not None:
            await self.engine.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="jarbis-brain",
        description="El cerebro de Jarbis: escucha el cuerpo y contesta.",
    )
    parser.add_argument("--url", default=None,
                        help="por default ws://<gateway de WSL>:8765")
    parser.add_argument("--token", default=None)
    parser.add_argument("--model", default="haiku", help="modelo base")
    parser.add_argument("--escalate-to", default="opus",
                        help="modelo al que escala en preguntas que piden pensar")
    parser.add_argument("--cwd", default=None, help="proyecto donde corre Claude")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="repite lo que escuchó, sin llamar a Claude")
    parser.add_argument("--subprocess", action="store_true",
                        help="forzar el motor viejo de `claude -p`")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    token = args.token or find_token()
    if not token:
        log.error(
            "No encontré el token del puente. Arrancá el cuerpo primero "
            "(run.bat) — lo genera en su primer arranque."
        )
        return 1

    brain = Brain(
        url=args.url, token=token, dry_run=args.dry_run,
        prefer_subprocess=args.subprocess, model=args.model,
        escalate_to=args.escalate_to,
        cwd=Path(args.cwd) if args.cwd else None, timeout=args.timeout,
    )

    log.info(f"Cerebro arrancando · {brain.url} · proyecto {brain.cwd}")

    async def run() -> None:
        try:
            await brain.run_forever()
        finally:
            await brain.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Chau")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
