# Jarbis

A local voice body for a Claude Code agent. It listens, it talks, and it can
speak first.

The agent already existed as a brain: [MyJarbis](https://github.com/BraianTroncoso/myjarbis)
for memory, skills for style, Claude Code for thinking. What it had no way of
doing was hearing, speaking, or — above all — **starting** a conversation.

![The five states of the robot](docs/images/states-dark.png)

Everything runs on your machine. Nothing leaves it except the Claude API calls
the agent makes on its own.

---

## The one idea that matters

**The body pushes.** That sounds obvious and it is the entire point.

The previous version of this was an MCP server exposing ten tools, and the brain
had to call `submit_voice_prompt` to ask *"did they say anything?"*. MCP is pull:
the model always speaks first. With that shape a proactive assistant is
impossible by construction — not for lack of code, but because of the direction
of the flow.

Here the body opens a WebSocket and emits events as they happen. That enables the
two things that could not exist before: answering without being polled, and
speaking up without being spoken to.

```
┌─ BODY ─ native on Windows ──────────────────┐
│  wake word → Silero VAD → Whisper → Piper   │
│  (WSL has no real microphone)               │
└───────┬──────────────────────────┬──────────┘
        │                          │ reads state
        │ WebSocket                ▼
        │ the body PUSHES    ┌─────────────────┐
        │                    │ ROBOT  ·  FACE  │
        │                    │ widget   panel  │
┌───────▼─────────────────┐  └─────────────────┘
│  BRAIN ─ in WSL         │
│  Agent SDK, live session│
│  + MyJarbis             │
└───────┬─────────────────┘
        │ every tool call
┌───────▼─────────────────────────────────────────┐
│  GATE ─ PreToolUse hooks                        │
│  reads freely · writes by asking · never destroys │
└─────────────────────────────────────────────────┘
```

---

## What it looks like

A frameless, click-through window floating on top of everything. Tk's
`-transparentcolor` on Windows makes a chosen color both invisible *and*
transparent to clicks, so the widget has a real shape instead of being a
rectangle with a drawing inside it.

![Right-click menu](docs/images/menu.png)

The menu is drawn on a canvas instead of inherited from the OS, so it matches the
rest: same dark, same rounded corners, same accent.

- **click** → opens the mic; the VAD closes the turn when you stop talking
- **click and hold** → records for as long as you hold it
- **drag** → moves it; the position is remembered
- **right-click** → the menu above
- **double-click** → opens the web panel at `http://127.0.0.1:3334`

The robot reads the same `jarbis-state.json` the web panel does. No second
socket, no startup ordering problem.

On a light desktop:

![The states on a light background](docs/images/states-light.png)

---

## Requirements

- **Windows 10/11** for the body. The transparent widget, the global hotkey and
  PortAudio all depend on it.
- **WSL2** for the brain, or any machine that can reach the bridge. The brain is
  just a WebSocket client; it does not have to live in WSL.
- **Python 3.10+** on the WSL side, **3.11+** on the Windows side.
- **Claude Code**, installed and authenticated, for the brain to think with.
- No GPU. Everything here was measured on an i7-1255U with no CUDA.

## Install

```bash
git clone https://github.com/BraianTroncoso/jarbis
cd jarbis

bash scripts/fetch-models.sh     # 239 MB, licenses listed below
pip install claude-agent-sdk     # the brain

# Copy the body to the Windows side. Point it at a Python environment that has
# sounddevice, faster-whisper and onnxruntime installed.
JARBIS_VENV=/mnt/c/Users/you/some-venv bash scripts/deploy-body.sh
```

`deploy-body.sh` finds your Windows profile under `/mnt/c/Users`, writes the
package there, and generates the launchers. Override anything with
`JARBIS_WIN_USER`, `JARBIS_DEST` or `JARBIS_VENV`.

The body has to run natively on Windows: WSL has no real microphone (`/dev/snd`
only exposes `timer`) and PortAudio does not survive the WSLg PulseAudio bridge.

## Run

```
C:\Users\you\jarbis-body\doctor.bat --deep    # diagnose first
C:\Users\you\jarbis-body\jarbis.bat           # body · face · robot · brain
```

`jarbis.bat` is the supervisor: it starts the four components, restarts whatever
dies with exponential backoff, and launches the brain inside WSL via `wsl.exe`.
Put a shortcut to `jarbis-silencioso.vbs` in the Startup folder and it comes up
with Windows, with no console window.

**Click the robot and talk to it.** Hold to talk longer.

### A note on WSL networking

With `networkingMode=NAT` the WSL VM has its own loopback, so `127.0.0.1` on the
Windows side is unreachable from WSL — localhost forwarding only goes
Windows → WSL. The bridge binds to loopback **plus** the `vEthernet (WSL)`
adapter address, which from inside WSL is the default gateway. It deliberately
never binds `0.0.0.0`: that would publish the bridge on whatever Wi-Fi you are
on. `bridge/netif.py` does that filtering and explains itself.

---

## Why some pieces are deliberately not models

**What decides *when* to speak is not an LLM, on purpose.** There is published
evidence that a small deterministic controller beats every LLM-based trigger
architecture at exactly this task, and that frontier models asked "should I
interrupt?" answer yes almost every time. `reflex.py` is a set of filters:
cooldown, time window, don't talk over the user, don't repeat yourself.

**The local model is a reflex, not a cortex.** On DDR4-3200 dual channel the
effective memory bandwidth is around 27 GB/s, which caps a dense 8B model at
4-5 tokens per second. That ceiling is bandwidth, not tuning, so thinking goes to
the API and only the fast paths stay local.

**The STT engine was chosen by measuring, and the answer was "don't change it".**
`compare_stt.py` runs Whisper, Moonshine and NeMo Canary against the same
synthesized phrases. Canary was 7.3× faster and 18 WER points worse. Whisper
stayed.

---

## Layout

```
body/jarbis_body/
  audio/        wake word, Silero VAD, STT, TTS, presence, hotkey, resampling
  robot/        shape (geometry) · render (drawing) · widget (window) · menu
  state/        state machine, and the writer the robot and the panel read
  bridge/       protocol, push server, command client, network binding
  orchestrator.py   wires it together
  reflex.py     decides WHEN to speak first (no LLM)
  greeting.py   the morning greeting, the only trigger that is wired up
  supervisor.py starts the four pieces and replaces them
  doctor.py · face.py
brain/
  client.py     transport: bridge ↔ engine
  engine.py     Agent SDK (live session) · subprocess (fallback) · escalation
hooks/          gate.py (decides) · audit.py (logs) · settings.json
web/index.html  the panel
scripts/        fetch-models · deploy-body · bench_voice · bench_brain ·
                tune_stt · compare_stt · diag_wakeword · preview_robot
tests/          199 tests, none of them need a microphone
```

### The audio layer is inherited

`voicemode.py`, `wakeword.py`, `tts.py`, `presence.py` and `hotkey.py` come from
[**GesturePilot**](https://github.com/BraianTroncoso/GesturePilot) v0.2.0
(MIT, January 2026) almost verbatim — about 2,400 lines
that already worked. The comments still say so, file by file. What was rewritten
is the end of the pipeline (pull to push), the state machine (its timeouts never
fired), and the gate (it did not exist).

---

## The gate

Every tool call the brain makes passes through a `PreToolUse` hook before
anything runs. Reads go through, writes ask, destructive commands are denied.

Two details that are easy to get wrong:

- `PreToolUse` hooks fire **before** any permission-mode check, including under
  `--dangerously-skip-permissions`. That is what makes this a real boundary and
  not a suggestion.
- The denying hook has to be **first** in the array. Ordering is not cosmetic.

It has been tested against a real `rm -rf`.

---

## Tests

```bash
python3 -m pytest tests/ -q          # 199 tests, no microphone needed
```

The calibration benches do need the Windows environment:

```
diag.bat bench_voice.py      # the whole chain, end to end
diag.bat compare_stt.py      # engine vs. latency vs. accuracy
diag.bat diag_wakeword.py    # wake word threshold
diag.bat preview_robot.py    # the five states, as a PNG
```

They use Piper as a *fake mouth*: synthesize phrases, run them through the chain,
compare against the original text. That closes the loop with nobody talking.

---

## Known limits

- **It is barely proactive.** The only wired trigger is the morning greeting, and
  it ships disabled. The reflex has its filters and its tests but no sensors.
- **The wake word is English-only.** The bundled `hey_jarvis.onnx` scores
  0.77-0.95 on English speech and 0.157 on Spanish against a 0.50 threshold, so
  it ships off. Training a Spanish one needs CUDA; the path is documented, the
  work is not done.
- **~8.5 seconds** from when you stop talking to when it answers. Roughly half of
  that is Whisper.
- **The UI and the voice are in Spanish.** The Piper voice, the Whisper language
  and the menu labels are configured for Spanish. All three are settings, not
  assumptions baked into the code.

---

## Memory lives somewhere else

There are deliberately no loose `.md` files. Everything about this project —
decisions, hardware traps, measurements, open threads — lives in a project-local
[MyJarbis](https://github.com/BraianTroncoso/myjarbis) database
(`.myjarbis/memory.db`, SQLite + FTS5), split into four modules: `cuerpo`,
`cara`, `cerebro`, `operacion`.

    myjarbis status                 # where things stand
    myjarbis module use cuerpo      # open a module
    claude  →  /jarbis              # and work with context loaded

MyJarbis is **optional**. Jarbis runs without it; the brain just has less memory
to work with. This README is the front door; the reasoning is in the database.

---

## Licenses

The code is **MIT** — see [LICENSE](LICENSE). The models are not all the same:

| model | license |
|---|---|
| `hey_jarvis.onnx` (livekit-wakeword) | Apache-2.0, code **and** weights |
| `silero_vad.onnx` | MIT |
| `es_AR-daniela-high` | CC BY-SA 4.0 (OpenSLR 61) |
| `es_ES-davefx-medium` | CC0 |
| faster-whisper / CTranslate2 | MIT |
| Piper (`piper1-gpl`) | **GPL-3.0-or-later** |

Watch that last row. Piper moved from `rhasspy/piper` (MIT, archived) to
`OHF-Voice/piper1-gpl` (GPL-3.0). Jarbis invokes Piper as a separate process and
does not link against it, which is why MIT holds here — but if you redistribute a
bundle that ships Piper inside it, that distinction matters.
