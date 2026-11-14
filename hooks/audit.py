#!/usr/bin/env python3
"""
Hook de auditoría: registra y no decide.

Es el primero de los dos `PreToolUse`. Escribe una línea JSONL por cada intento
de uso de herramienta y sale con 0 sin emitir ninguna decisión de permiso.

Por qué existe separado del que decide: Claude Code **corre todos los hooks que
matchean hasta el final antes de mezclar sus respuestas**. Eso significa que este
logger se ejecuta igual cuando el otro hook deniega, así que el registro queda
completo incluso de lo que se bloqueó. Si estuvieran juntos, un `deny` temprano
dejaría huecos en la auditoría.

Y el dato que lo hace valer la pena: `PreToolUse` dispara **antes de cualquier
chequeo de modo de permisos**, incluso con `--dangerously-skip-permissions`. Es
la única capa que no se puede saltear.

Contrato: recibe el payload del hook por stdin, no escribe nada a stdout (para
no inyectar contexto), sale 0 siempre. Un logger que rompe el turno es peor que
no tener logger.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Campos de entrada que no queremos en el registro en texto plano. Loguear el
# contenido completo de cada Write duplicaría el repo dentro del JSONL.
TRUNCATE_AT = 600

# Claves cuyo valor se reemplaza por una marca. El registro es para saber qué
# pasó, no para archivar secretos en un archivo sin cifrar.
SENSITIVE_KEYS = {
    "api_key", "apikey", "token", "secret", "password", "passwd",
    "authorization", "auth", "credential", "credentials", "private_key",
}


def audit_path() -> Path:
    """Dónde va el registro. `JARBIS_AUDIT_LOG` lo pisa."""
    override = os.environ.get("JARBIS_AUDIT_LOG")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "var" / "audit.jsonl"


def redact(value: object, depth: int = 0) -> object:
    """Recorta lo largo y tapa lo sensible, recursivamente."""
    if depth > 6:
        return "<demasiado profundo>"

    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            if key.lower() in SENSITIVE_KEYS:
                out[key] = "<tapado>"
            else:
                out[key] = redact(item, depth + 1)
        return out

    if isinstance(value, list):
        if len(value) > 20:
            return [redact(v, depth + 1) for v in value[:20]] + [
                f"<+{len(value) - 20} más>"
            ]
        return [redact(v, depth + 1) for v in value]

    if isinstance(value, str) and len(value) > TRUNCATE_AT:
        return value[:TRUNCATE_AT] + f"<+{len(value) - TRUNCATE_AT} chars>"

    return value


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        # "null" parsea a None y "[]" a lista: ninguno tiene .get().
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        # Ni siquiera un payload ilegible justifica frenar el turno.
        return 0

    record = {
        "at": datetime.now().isoformat(),
        "event": payload.get("hook_event_name", "PreToolUse"),
        "tool": payload.get("tool_name"),
        "session": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        # Marca de que el turno vino por voz. La pone brain/client.py.
        "voice": os.environ.get("CLAUDE_CODE_VOICE_SESSION") == "1",
        "input": redact(payload.get("tool_input", {})),
    }

    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # Disco lleno, permisos, lo que sea: no es motivo para bloquear.
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
