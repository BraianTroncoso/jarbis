"""
Arranca el robot.

    python -m jarbis_body.robot
    python -m jarbis_body.robot --width 160
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="jarbis_body.robot", description="El robot flotante de Jarbis."
    )
    parser.add_argument("--width", type=int, default=128,
                        help="ancho de la pantallita en píxeles (default 128)")
    args = parser.parse_args()

    try:
        from jarbis_body.robot.widget import RobotWidget
    except ImportError as exc:
        print(f"No pude cargar la interfaz: {exc}", file=sys.stderr)
        print("¿Está tkinter en este Python?", file=sys.stderr)
        return 1

    try:
        RobotWidget(width=args.width).run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
