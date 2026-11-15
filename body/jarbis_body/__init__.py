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
