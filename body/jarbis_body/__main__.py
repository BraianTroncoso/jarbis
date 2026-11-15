"""
Punto de entrada del cuerpo.

    run.bat
    .venv-win\\Scripts\\python.exe -m jarbis_body

El log va a archivo y a stderr, nunca a stdout. Es herencia del diseño anterior,
donde stdout era el transporte MCP, pero se mantiene porque sigue siendo útil:
deja stdout libre para que el proceso pueda ser pipeado sin ensuciar la salida.
"""

from __future__ import annotations

import argparse
import signal
import sys
from types import FrameType

from loguru import logger

from jarbis_body.config import settings


def setup_logging(level: str) -> None:
    """Log a stderr y a var/jarbis.log con rotación."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
    )
    logger.add(
        settings.var_dir / "jarbis.log",
        level="DEBUG",
        rotation="10 MB",
        retention=5,
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="jarbis_body", description="El cuerpo de Jarbis: oídos, boca y puente."
    )
    parser.add_argument("--log-level", default=settings.log_level)
    parser.add_argument(
        "--no-wakeword",
        action="store_true",
        help="arrancar sin el detector de nombre (solo hotkey)",
    )
    parser.add_argument(
        "--no-tts", action="store_true", help="no hablar, solo escuchar"
    )
    parser.add_argument(
        "--whisper-model",
        default=None,
        help="pisa el modelo de settings (base es 3,4x más rápido que small)",
    )
    args = parser.parse_args()

    if args.no_wakeword:
        settings.wakeword_enabled = False
    if args.no_tts:
        settings.tts_enabled = False
    if args.whisper_model:
        settings.whisper_model = args.whisper_model

    setup_logging(args.log_level)

    settings.var_dir.mkdir(parents=True, exist_ok=True)

    from jarbis_body.orchestrator import JarbisBody

    body = JarbisBody()

    def shutdown(signum: int, _frame: FrameType | None) -> None:
        logger.info(f"Señal {signum}; apagando")
        body.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    if hasattr(signal, "SIGBREAK"):  # Windows: Ctrl+Break
        signal.signal(signal.SIGBREAK, shutdown)

    if not body.start():
        logger.error("El cuerpo no pudo arrancar")
        body.stop()
        return 1

    logger.info("")
    logger.info("  Jarbis está escuchando.")
    logger.info(f"  puente     ws://{settings.bridge_host}:{settings.bridge_port}")
    logger.info(f"  estado     {settings.state_file}")
    logger.info(f"  activar    ctrl+shift+g  (o decí '{settings.wakeword_backend == 'onnx' and 'hey jarvis' or 'jarbis'}')")
    logger.info("  salir      ctrl+c")
    logger.info("")

    try:
        body.wait()
    finally:
        body.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
