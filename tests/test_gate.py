"""
Tests de la compuerta.

Corren los hooks como procesos de verdad, con payloads JSON de `PreToolUse` por
stdin, igual que los invoca Claude Code. No se importa `decide()` directo a
propósito: si el contrato de entrada/salida se rompe, el test tiene que fallar.

Lo que se verifica:

1. lo que lee, pasa
2. lo que escribe, pregunta
3. lo que destruye, se niega — y *se niega*, no se pregunta
4. los secretos, se niegan
5. el auditor registra igual lo que se denegó (es lo que justifica separarlo)
6. ningún hook rompe el turno, ni con basura en la entrada

    pytest tests/test_gate.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
GATE = HOOKS / "gate.py"
AUDIT = HOOKS / "audit.py"


def run_gate(tool: str, tool_input: dict) -> tuple[str, str]:
    """Corre gate.py con un payload y devuelve (decisión, motivo)."""
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "session_id": "test",
            "cwd": "/tmp",
        }
    )
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, f"gate.py salió con {proc.returncode}: {proc.stderr}"
    out = json.loads(proc.stdout)
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    return specific["permissionDecision"], specific["permissionDecisionReason"]


# ============================================================================
# Leer libre
# ============================================================================


@pytest.mark.parametrize(
    "tool",
    ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Task", "Skill"],
)
def test_las_herramientas_de_lectura_pasan(tool):
    decision, _ = run_gate(tool, {"file_path": "/home/user/proyecto/main.py"})
    assert decision == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "git log --oneline -10",
        "cat README.md",
        "pytest tests/ -v",
        "python3 -c 'print(1)'",
        "grep -rn foo src/",
        "npm run build",
    ],
)
def test_los_comandos_inocuos_pasan(command):
    decision, reason = run_gate("Bash", {"command": command})
    assert decision == "allow", f"{command!r} debería pasar ({reason})"


def test_las_tools_mcp_de_lectura_pasan():
    for tool in [
        "mcp__myjarbis__search",
        "mcp__myjarbis__load_module",
        "mcp__myjarbis__get_project_state",
        "mcp__myjarbis__list_modules",
        "mcp__myjarbis__current_project",
    ]:
        decision, _ = run_gate(tool, {})
        assert decision == "allow", tool


# ============================================================================
# Escribir pregunta
# ============================================================================


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_escribir_archivos_pregunta(tool):
    decision, _ = run_gate(tool, {"file_path": "/home/user/proyecto/main.py"})
    assert decision == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'algo'",
        "git push origin master",
        "npm install express",
        "pip install requests",
        "docker run -it ubuntu",
        "ssh servidor",
        "curl -X POST https://api.ejemplo.com/cosa",
        "gh pr create --title x",
        "aws s3 cp archivo s3://bucket/",
        "rm archivo.txt",
        "systemctl restart nginx",
    ],
)
def test_los_comandos_con_efectos_preguntan(command):
    decision, reason = run_gate("Bash", {"command": command})
    assert decision == "ask", f"{command!r} debería preguntar ({reason})"


def test_las_tools_mcp_que_escriben_preguntan():
    for tool in [
        "mcp__myjarbis__save_observation",
        "mcp__myjarbis__end_session",
        "mcp__myjarbis__update_progress",
        "mcp__myjarbis__set_project_state",
    ]:
        decision, _ = run_gate(tool, {})
        assert decision == "ask", tool


def test_una_herramienta_desconocida_pregunta():
    """Default seguro: nada hereda permiso por omisión."""
    decision, reason = run_gate("HerramientaQueNoExiste", {})
    assert decision == "ask"
    assert "no clasificada" in reason


# ============================================================================
# Destruir se niega — esto es lo que no puede fallar
# ============================================================================


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -rf ~/dev-own",
        "rm -fr algo",
        "rm --recursive --force cosa",
        "sudo rm -rf /var",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "echo x > /dev/sda",
        "shred -u archivo",
        ":(){ :|:& };:",
        "git push --force origin master",
        "git push origin +master",
        "git reset --hard HEAD~5",
        "git clean -fdx",
        "history -c",
        "crontab -r",
        "shutdown -h now",
        "reboot",
    ],
)
def test_los_comandos_destructivos_se_niegan(command):
    decision, reason = run_gate("Bash", {"command": command})
    assert decision == "deny", f"{command!r} TIENE que negarse, dio {decision} ({reason})"


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.ssh/id_rsa",
        "cat .env",
        "head -5 /home/user/proyecto/.env",
        "cat credentials.json",
        "cat ~/.claude/.credentials.json",
    ],
)
def test_leer_secretos_se_niega(command):
    decision, reason = run_gate("Bash", {"command": command})
    assert decision == "deny", f"{command!r} TIENE que negarse ({reason})"


@pytest.mark.parametrize(
    "path",
    [
        "/home/user/.ssh/config",
        "/home/user/.aws/credentials",
        "/home/user/.claude/.credentials.json",
        "/etc/passwd",
        "/home/user/proyecto/.env",
        "/home/user/dev-own/twin/.myjarbis/memory.db",
    ],
)
def test_escribir_en_rutas_protegidas_se_niega(path):
    decision, reason = run_gate("Write", {"file_path": path})
    assert decision == "deny", f"{path!r} TIENE que negarse ({reason})"


def test_negar_gana_sobre_preguntar():
    """
    `sudo rm -rf` matchea tanto un patrón de ASK (sudo) como uno de DENY (rm -rf).
    Tiene que ganar el deny: la precedencia de Claude Code es deny > ask, y la
    compuerta tiene que coincidir, no contradecirla.
    """
    decision, _ = run_gate("Bash", {"command": "sudo rm -rf /tmp/cosa"})
    assert decision == "deny"


# ============================================================================
# Robustez: un hook que rompe el turno es peor que no tener hook
# ============================================================================


@pytest.mark.parametrize("entrada", ["", "no soy json", "[]", "null", '{"tool_name": 5}'])
def test_el_gate_nunca_rompe(entrada):
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=entrada,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, f"gate.py murió con {entrada!r}: {proc.stderr}"
    out = json.loads(proc.stdout)
    # Con entrada dudosa, el default es preguntar.
    assert out["hookSpecificOutput"]["permissionDecision"] in ("ask", "allow", "deny")


@pytest.mark.parametrize(
    "entrada", ["", "no soy json", "{}", "null", "[]", '{"tool_input": "texto"}']
)
def test_el_auditor_nunca_rompe(entrada, tmp_path):
    proc = subprocess.run(
        [sys.executable, str(AUDIT)],
        input=entrada,
        capture_output=True,
        text=True,
        timeout=20,
        env={"JARBIS_AUDIT_LOG": str(tmp_path / "audit.jsonl"), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, f"audit.py murió con {entrada!r}: {proc.stderr}"
    # No escribe a stdout: inyectaría contexto en el turno.
    assert proc.stdout.strip() == ""


# ============================================================================
# El auditor
# ============================================================================


def test_el_auditor_registra_lo_que_se_deniega(tmp_path):
    """
    La razón de existir del hook separado: tiene que registrar incluso lo que la
    compuerta bloqueó. Claude Code corre todos los hooks que matchean hasta el
    final antes de mezclar respuestas, así que esto es alcanzable.
    """
    log = tmp_path / "audit.jsonl"
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
            "session_id": "abc123",
        }
    )
    subprocess.run(
        [sys.executable, str(AUDIT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=20,
        env={"JARBIS_AUDIT_LOG": str(log), "PATH": "/usr/bin:/bin"},
        check=True,
    )

    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["tool"] == "Bash"
    assert record["input"]["command"] == "rm -rf /"
    assert record["session"] == "abc123"
    assert "at" in record


def test_el_auditor_tapa_los_secretos(tmp_path):
    log = tmp_path / "audit.jsonl"
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "deploy",
                "env": {"API_KEY": "sk-secreto-de-verdad", "DEBUG": "1"},
                "token": "tok-123",
            },
        }
    )
    subprocess.run(
        [sys.executable, str(AUDIT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=20,
        env={"JARBIS_AUDIT_LOG": str(log), "PATH": "/usr/bin:/bin"},
        check=True,
    )

    contenido = log.read_text(encoding="utf-8")
    assert "sk-secreto-de-verdad" not in contenido
    assert "tok-123" not in contenido
    assert "<tapado>" in contenido
    # Lo que no es secreto queda legible.
    assert "DEBUG" in contenido


def test_el_auditor_recorta_lo_gigante(tmp_path):
    log = tmp_path / "audit.jsonl"
    payload = json.dumps(
        {"tool_name": "Write", "tool_input": {"content": "x" * 50_000}}
    )
    subprocess.run(
        [sys.executable, str(AUDIT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=20,
        env={"JARBIS_AUDIT_LOG": str(log), "PATH": "/usr/bin:/bin"},
        check=True,
    )
    # Sin recorte, cada Write duplicaría el archivo dentro del registro.
    assert log.stat().st_size < 2_000
    assert "chars>" in log.read_text(encoding="utf-8")


def test_el_auditor_es_append_only(tmp_path):
    log = tmp_path / "audit.jsonl"
    for i in range(3):
        subprocess.run(
            [sys.executable, str(AUDIT)],
            input=json.dumps({"tool_name": "Read", "tool_input": {"n": i}}),
            capture_output=True,
            text=True,
            timeout=20,
            env={"JARBIS_AUDIT_LOG": str(log), "PATH": "/usr/bin:/bin"},
            check=True,
        )
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["input"]["n"] for line in lines] == [0, 1, 2]
