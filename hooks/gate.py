#!/usr/bin/env python3
"""
Hook de compuerta: el que decide.

Política: **leer libre, escribir con permiso, destruir nunca.**

Es el segundo `PreToolUse` y va **primero en el array** del settings, por el
issue #39344 de claude-code: un hook que devuelve `"ask"` desactiva en silencio
las reglas de `permissions.deny` del settings.json. Poniendo el que deniega antes
del que pregunta, el `deny` se evalúa igual. (Acá los dos viven en el mismo
archivo, pero el orden en el array sigue importando para no pisar reglas
declarativas.)

Precedencia de Claude Code: gana el más restrictivo, `deny > defer > ask > allow`.

Por qué una compuerta y no confiar en el modo de permisos: un twin que puede
hablar primero también puede actuar primero. El modo de permisos protege cuando
hay alguien mirando la terminal; a las 3 de la mañana, con la voz como única
interfaz, no hay nadie mirando. Y `PreToolUse` dispara incluso bajo
`--dangerously-skip-permissions`, así que es la única capa que no se puede
saltear por accidente.

    echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}' | python3 gate.py
"""

from __future__ import annotations

import json
import re
import sys

# ============================================================================
# Política
# ============================================================================

# Herramientas que solo leen. Pasan sin preguntar: el twin tiene que poder
# mirar tus cosas sin interrumpirte, que es la mitad del punto de tenerlo.
READ_ONLY_TOOLS = {
    "Read", "Glob", "Grep", "NotebookRead", "TodoWrite",
    "WebFetch", "WebSearch", "Task", "AskUserQuestion",
    "ListAgents", "ToolSearch", "Skill", "BashOutput",
}

# Herramientas que modifican el disco: se pregunta.
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Comandos de shell que se niegan sin apelación. No "se pregunta": se niega.
# Un modelo convencido de que hace falta un `rm -rf` puede ser muy convincente
# a las 3 AM.
DENY_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf]", "rm recursivo o forzado"),
    (r"\brm\s+-{1,2}(recursive|force)", "rm recursivo o forzado"),
    (r"\bmkfs(\.|\s)", "formateo de disco"),
    (r"\bdd\s+.*\bof=/dev/", "escritura directa a un dispositivo"),
    (r">\s*/dev/(sd|nvme|hd)", "escritura directa a un disco"),
    (r"\bshred\b", "borrado irrecuperable"),
    (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/(\s|$)", "chmod 777 en la raíz"),
    (r"\bgit\s+push\s+.*--force(-with-lease)?\b", "push forzado"),
    (r"\bgit\s+push\s+.*\+", "push forzado con refspec"),
    (r"\bgit\s+reset\s+--hard\b", "reset --hard: descarta trabajo sin backup"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*[dfx]", "git clean: borra archivos sin rastro"),
    (r"\bhistory\s+-c", "borrado del historial"),
    (r"\bcrontab\s+-r", "borrado del crontab"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "apagado o reinicio"),
    (r"\bkill\s+-9\s+1\b", "matar init"),
    # Secretos: no se leen ni se mandan a ningún lado por voz.
    (r"\.ssh/id_(rsa|ed25519|ecdsa)\b", "clave privada SSH"),
    (r"\b(cat|less|more|head|tail|strings)\s+[^|;&]*\.env\b", "lectura de un .env"),
    (r"\bcredentials\.json\b", "archivo de credenciales"),
    (r"\.claude/\.credentials", "credenciales de Claude Code"),
]

# Comandos de shell que requieren confirmación. Son reversibles pero salen de la
# máquina o cambian estado que importa.
ASK_PATTERNS: list[tuple[str, str]] = [
    (r"\bgit\s+push\b", "push a un remoto"),
    (r"\bgit\s+commit\b", "commit"),
    (r"\bgit\s+merge\b", "merge"),
    (r"\bgit\s+rebase\b", "rebase"),
    (r"\bgit\s+tag\b", "tag"),
    (r"\b(npm|pnpm|yarn|pip|pipx|uv|cargo|gem)\s+(publish|install|add|uninstall|remove)\b",
     "instalación o publicación de paquetes"),
    (r"\b(docker|podman)\s+(run|rm|rmi|push|compose)\b", "operación de contenedores"),
    (r"\b(gh|glab)\s+(pr|issue|release|repo)\s+(create|merge|close|delete|edit)\b",
     "operación en GitHub/GitLab"),
    (r"\bcurl\b[^|;&]*-(X|-request)\s*(POST|PUT|PATCH|DELETE)", "request HTTP que escribe"),
    (r"\b(ssh|scp|rsync)\b", "acceso a una máquina remota"),
    (r"\b(aws|gcloud|az|wrangler|vercel|netlify|fly|turso)\b", "comando de cloud"),
    (r"\bsudo\b", "privilegios de root"),
    (r"\bsystemctl\s+(start|stop|restart|enable|disable)\b", "cambio de servicio"),
    (r"\brm\b", "borrado de archivos"),
    (r"\bmv\s+[^|;&]*\s+/", "mover algo a una ruta absoluta"),
    (r"\b(mail|sendmail|msmtp|mutt)\b", "envío de mail"),
    (r"\bpowershell(\.exe)?\b", "comando de PowerShell"),
]

# Rutas que no se escriben nunca, ni preguntando.
PROTECTED_PATHS = [
    (r"\.ssh/", "configuración de SSH"),
    (r"\.aws/", "credenciales de AWS"),
    (r"\.claude/\.credentials", "credenciales de Claude Code"),
    (r"\.gnupg/", "claves GPG"),
    (r"/etc/(passwd|shadow|sudoers)", "archivo de sistema"),
    (r"(^|/)\.env($|\.)", "archivo de entorno con secretos"),
    (r"\.myjarbis/memory\.db", "base de MyJarbis (usá las tools, no el archivo)"),
]


def decide(tool: str, tool_input: dict) -> tuple[str, str]:
    """
    Devuelve (decisión, motivo) con decisión en allow | ask | deny.

    El orden importa: primero lo que se niega, después lo que se pregunta,
    y recién al final lo que pasa.
    """
    # --- lectura libre -----------------------------------------------------
    if tool in READ_ONLY_TOOLS:
        return "allow", "herramienta de solo lectura"

    # --- escritura de archivos ---------------------------------------------
    if tool in WRITE_TOOLS:
        path = str(
            tool_input.get("file_path")
            or tool_input.get("notebook_path")
            or tool_input.get("path")
            or ""
        )
        for pattern, label in PROTECTED_PATHS:
            if re.search(pattern, path):
                return "deny", f"ruta protegida: {label}"
        return "ask", f"escribe en {path or 'un archivo'}"

    # --- shell --------------------------------------------------------------
    if tool in ("Bash", "BashTool"):
        command = str(tool_input.get("command") or "")

        for pattern, label in DENY_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return "deny", label

        for pattern, label in ASK_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return "ask", label

        return "allow", "comando sin efectos destructivos conocidos"

    # --- MCP ----------------------------------------------------------------
    if tool.startswith("mcp__"):
        # Las tools de MyJarbis que solo leen pasan; las que escriben preguntan.
        read_prefixes = (
            "search", "load_", "get_", "list_", "current_", "resume", "find_"
        )
        name = tool.split("__")[-1]
        if any(name.startswith(prefix) for prefix in read_prefixes):
            return "allow", "tool MCP de lectura"
        return "ask", f"tool MCP que escribe: {tool}"

    # --- desconocido --------------------------------------------------------
    # Default seguro: si no lo conozco, pregunto. Una herramienta nueva no
    # debería heredar permiso por omisión.
    return "ask", f"herramienta no clasificada: {tool}"


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        # `json.loads` devuelve None para "null" y una lista para "[]"; ninguno
        # tiene .get(). Lo encontró el test, no la revisión: un hook que
        # crashea con entrada rara deja el turno sin compuerta.
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        # Payload ilegible: no se puede evaluar, así que se pregunta.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": "no pude leer el payload del hook",
                    }
                }
            )
        )
        return 0

    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    decision, reason = decide(tool, tool_input)

    # `allow` se emite igual en vez de salir callado: así el motivo queda en el
    # transcript y se puede auditar por qué algo pasó.
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": f"[jarbis] {reason}",
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
