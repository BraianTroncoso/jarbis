"""
Servidor del visor: la cara mínima.

Sirve `web/index.html` y expone el archivo de estado en `/state.json`. Nada más:
sin framework, sin build, sin dependencias. Es el mismo patrón que ya anda en
`claude-spirit-box` (hook → JSON en disco → servidor chico → HTML con polling),
que tiene la ventaja de que el visor y el cuerpo son independientes: cualquiera
de los dos puede reiniciarse sin romper al otro.

    python -m jarbis_body.face          # en 3334
    python -m jarbis_body.face --port 9000

Esto NO es el widget definitivo. Es lo que mata el problema 4 de PLAN_MEJORAS
("no sé si me está escuchando o procesando") sin instalar nada, para que mañana
se pueda decidir el widget nativo mirando algo que ya funciona en vez de
imaginarlo.
"""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import sys
from pathlib import Path

from jarbis_body.config import settings

DEFAULT_PORT = 3334

# Comandos que el botón puede mandarle al cuerpo. Es un subconjunto a propósito:
# la cara no puede hacer hablar al cuerpo con texto arbitrario, solo abrir el
# micrófono, cortar y mandarlo a dormir.
ALLOWED_COMMANDS = {"listen", "close", "stop", "sleep", "mode"}

# Header obligatorio en los POST. No es un secreto: es un anti-CSRF.
#
# Sin esto, cualquier página que estés visitando puede hacerle POST a
# localhost:3334 desde tu navegador y abrirte el micrófono. Un header custom
# fuerza al navegador a hacer preflight CORS, que este servidor no responde, así
# que el pedido nunca llega. Es la misma clase de agujero que el CVE-2026-25253
# de OpenClaw, donde estar en loopback no alcanzó.
CSRF_HEADER = "X-Jarbis"


class FaceHandler(http.server.BaseHTTPRequestHandler):
    """Lee el estado y, con el botón, le manda comandos al cuerpo."""

    web_dir: Path
    state_file: Path

    def do_GET(self) -> None:  # noqa: N802  (la API de http.server es así)
        route = self.path.split("?")[0]

        if route in ("/", "/index.html"):
            self._send_file(self.web_dir / "index.html", "text/html; charset=utf-8")
        elif route == "/state.json":
            self._send_state()
        elif route == "/health":
            self._send_bytes(b'{"ok":true}', "application/json")
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?")[0].strip("/")

        if route not in ALLOWED_COMMANDS:
            self.send_error(404)
            return

        if not self._csrf_ok():
            self.send_error(403, "origen no permitido")
            return

        ok, detail = self._send_to_body(route, self._read_payload())
        payload = json.dumps({"ok": ok, "command": route, "detail": detail})
        self._send_bytes(payload.encode("utf-8"), "application/json; charset=utf-8")

    def _read_payload(self) -> dict:
        """Lee el cuerpo JSON del POST. Vacío si no hay."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ------------------------------------------------------------------- CSRF

    def _csrf_ok(self) -> bool:
        """
        Dos chequeos baratos que bloquean a una página cualquiera del navegador.

        1. El header custom: fuerza preflight, que no contestamos.
        2. El Origin: si viene y no es nuestro, se rechaza. Un fetch desde una
           página de otro sitio siempre manda Origin.
        """
        if self.headers.get(CSRF_HEADER) != "1":
            return False

        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host", "")
            if origin not in (f"http://{host}", f"https://{host}"):
                return False

        return True

    # ------------------------------------------------------------- al cuerpo

    def _send_to_body(self, command: str, payload: dict | None = None) -> tuple[bool, str]:
        """
        Abre una conexión corta al puente, manda el comando y cierra.

        Se usa el cliente síncrono de websockets porque `http.server` maneja
        cada request en su propio thread, sin event loop. Una conexión por
        apretón de botón es barata y no deja estado colgando.
        """
        from jarbis_body.bridge.protocol import load_or_create_token
        from jarbis_body.config import settings as cfg

        url = f"ws://127.0.0.1:{cfg.bridge_port}"
        try:
            from websockets.sync.client import connect
        except ImportError:
            return False, "falta websockets con soporte sync (>=14)"

        try:
            token = load_or_create_token(cfg.bridge_token_file)
            with connect(url, open_timeout=3) as ws:
                ws.send(json.dumps({"type": "hello", "token": token}))
                welcome = json.loads(ws.recv(timeout=3))
                if welcome.get("type") != "welcome":
                    return False, "el puente rechazó el handshake"
                mensaje = {"type": command}
                # Solo `mode` lleva datos, y se filtra a las claves conocidas
                # para que la cara no pueda inyectar campos arbitrarios.
                if command == "mode" and payload:
                    for clave in ("ptt", "follow_up", "greeting", "tts"):
                        if clave in payload:
                            mensaje[clave] = bool(payload[clave])
                ws.send(json.dumps(mensaje))
            return True, "enviado"
        except Exception as exc:
            # Lo más común: el cuerpo no está corriendo. Que la cara lo diga en
            # vez de tirar 500 hace obvio qué falta prender.
            return False, f"no pude hablar con el cuerpo: {type(exc).__name__}"

    def _send_state(self) -> None:
        """
        Devuelve el estado. Si el archivo no existe todavía, devuelve un estado
        sintético en vez de 404: el visor tiene que poder abrirse antes que el
        cuerpo y mostrar "apagado" en lugar de un error.
        """
        try:
            data = self.state_file.read_bytes()
            # Se valida que sea JSON parseable antes de servirlo. Con la
            # escritura atómica no debería pasar, pero si pasa es mejor devolver
            # el estado de "apagado" que un JSON cortado.
            json.loads(data)
        except (OSError, ValueError):
            data = json.dumps(
                {
                    "state": "dormido",
                    "description": "el cuerpo no está corriendo",
                    "color": "#4a4a55",
                    "updated_at": None,
                    "heard": None,
                    "said": None,
                    "turns": 0,
                }
            ).encode("utf-8")

        self._send_bytes(data, "application/json; charset=utf-8")

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            self._send_bytes(path.read_bytes(), content_type)
        except OSError:
            self.send_error(404, f"no encontré {path.name}")

    def _send_bytes(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # El visor hace polling cada 300 ms; sin esto el navegador cachea.
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        """Silencio. Un log por request a 3 req/s no sirve para nada."""


class QuietServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port: int = DEFAULT_PORT, *, host: str = "127.0.0.1") -> int:
    """Levanta el visor. Bloquea hasta Ctrl+C."""
    web_dir = settings.project_dir / "web"
    index = web_dir / "index.html"

    if not index.exists():
        print(f"No encuentro {index}", file=sys.stderr)
        print("¿Corriste scripts/deploy-body.sh?", file=sys.stderr)
        return 1

    handler = type(
        "BoundFaceHandler",
        (FaceHandler,),
        {"web_dir": web_dir, "state_file": settings.state_file},
    )

    with QuietServer((host, port), handler) as httpd:
        print()
        print("  La cara de Jarbis")
        print(f"    visor   http://{host}:{port}")
        print(f"    estado  {settings.state_file}")
        print("    salir   ctrl+c")
        print()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("  chau")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="jarbis_body.face", description="Visor web del estado de Jarbis."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="dejalo en loopback salvo que sepas lo que estás haciendo",
    )
    args = parser.parse_args()
    return serve(args.port, host=args.host)


if __name__ == "__main__":
    raise SystemExit(main())
