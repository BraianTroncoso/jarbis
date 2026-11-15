"""
Jarbis — el cuerpo.

Corre nativo en Windows porque WSL no tiene acceso real al micrófono ni a la
cámara (`/dev/snd` solo tiene `timer`, y PortAudio no sobrevive al puente de
WSLg). Habla con el cerebro, que vive en WSL, por WebSocket en loopback.

Linaje: GesturePilot v0.2.0 (MIT, enero 2026). Las capas de audio son las
mismas; lo que cambió es la dirección del flujo. Antes el cerebro preguntaba
"¿dijo algo?" por una tool MCP (pull). Ahora el cuerpo empuja (push), que es lo
único que permite que Jarbis hable primero.
"""

__version__ = "0.3.0"

# Código de salida que significa "lo cerré yo, a propósito".
#
# El supervisor repone todo lo que se muere, que es exactamente lo que querés
# cuando algo se cae — y exactamente lo que NO querés cuando cerraste el robot
# a mano: lo cerrabas y volvía a aparecer dos segundos después. Un código de
# salida es la forma más barata de separar las dos cosas: ya viaja de hijo a
# padre, no necesita archivo ni socket, y no puede quedar viejo en disco.
EXIT_USER_QUIT = 42
