#!/usr/bin/env python3
"""
Generate the README diagrams.

    python3 docs/media/gen.py

The architecture used to be an ASCII block. It was unreadable: box-drawing
characters only line up if every glyph is the same width, and the moment a
label needs an accent, an arrow or one more word, the whole frame shears.
Describing the diagram as data and rendering it as SVG means the next change
is a line here instead of counting spaces.

Same visual language as the MyJarbis README mockups — dark terminal palette,
one accent per layer, no gradients — with the accent swapped for the mint the
robot already uses on screen.

Output: `.svg` for editing and `.png` for GitHub, which always renders PNG.
"""

import os

try:
    import cairosvg
except ImportError:  # pragma: no cover - solo afecta al rasterizado
    cairosvg = None

HERE = os.path.dirname(os.path.abspath(__file__))

# ── palette ──────────────────────────────────────────────────────────
# Dark terminal base, shared with MyJarbis. The per-layer colours are the
# same five the robot shows on its face, so the diagram and the widget speak
# the same language.
BG = "#0e0e11"
CARD, EDGE = "#15151d", "#2a2a31"
CHIP, CHIP_EDGE = "#1b1b20", "#32323c"
TXT, DIM, FAINT = "#e7e7ea", "#8a8a9c", "#5d5d6a"
MINT = "#2de2a6"      # the body, and the push itself
AMBER = "#f5c451"     # the brain
CYAN = "#5fb3d4"      # what you see
RED = "#e0413f"       # the gate

# El `monospace` genérico va último y no puede faltar: cairosvg resuelve la
# pila por fontconfig, que no conoce `ui-monospace`, y si no encuentra ninguna
# cae en una tipografía PROPORCIONAL sin avisar.
SANS = "'DejaVu Sans', 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'DejaVu Sans Mono', 'Liberation Mono', 'Cascadia Code', Menlo, monospace"

W, H = 1100, 742
PAD = 44
COL_W = 620                      # la columna del flujo principal
# El hueco entre columnas no es estético: las flechas horizontales llevan su
# rótulo centrado ahí, y con menos espacio se montaban sobre el texto de las
# tarjetas.
SIDE_X = PAD + COL_W + 96        # la columna de lo que ves
SIDE_W = W - SIDE_X - PAD


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Svg:
    def __init__(self):
        self.parts = []

    def add(self, markup):
        self.parts.append(markup)

    # ---------------------------------------------------------------- piezas

    def text(self, x, y, s, size=13, fill=TXT, font=SANS, anchor="start",
             weight="normal", spacing=0):
        extra = f' letter-spacing="{spacing}"' if spacing else ""
        self.add(
            f'<text x="{x}" y="{y}" font-family="{font}" font-size="{size}" '
            f'fill="{fill}" text-anchor="{anchor}" font-weight="{weight}"{extra}>'
            f'{esc(s)}</text>'
        )

    def card(self, x, y, w, h, accent):
        """Una tarjeta con una barra de color del lado izquierdo."""
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" '
            f'fill="{CARD}" stroke="{EDGE}" stroke-width="1"/>'
        )
        self.add(
            f'<path d="M{x} {y + 14} a14 14 0 0 1 14 -14 h0 v{h} h0 '
            f'a14 14 0 0 1 -14 -14 Z" fill="{accent}" opacity="0.9"/>'
        )
        # La barra de arriba: 3px de color pegados al borde redondeado.
        self.add(
            f'<path d="M{x + 1} {y + 15} a14 14 0 0 1 14 -14 h4 v{h - 2} h-4 '
            f'a14 14 0 0 1 -14 -14 Z" fill="{accent}"/>'
        )

    def chip(self, x, y, w, h, label, caption=None, fill=CHIP,
             edge=CHIP_EDGE, color=TXT):
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" '
            f'fill="{fill}" stroke="{edge}" stroke-width="1"/>'
        )
        cy = y + (h / 2 + 4) if caption is None else y + h / 2 - 3
        self.text(x + w / 2, cy, label, size=12.5, fill=color, font=MONO,
                  anchor="middle")
        if caption:
            self.text(x + w / 2, y + h / 2 + 14, caption, size=10.5, fill=FAINT,
                      font=MONO, anchor="middle")

    def arrow_down(self, x, y0, y1, color, label=None, sub=None):
        head = 9
        self.add(
            f'<line x1="{x}" y1="{y0}" x2="{x}" y2="{y1 - head}" '
            f'stroke="{color}" stroke-width="2"/>'
        )
        self.add(
            f'<polygon points="{x - 6},{y1 - head} {x + 6},{y1 - head} {x},{y1}" '
            f'fill="{color}"/>'
        )
        if label:
            mid = (y0 + y1) / 2
            self.text(x + 16, mid - (2 if sub else -4), label, size=12,
                      fill=color, font=MONO, weight="bold")
            if sub:
                self.text(x + 16, mid + 15, sub, size=11, fill=DIM, font=SANS)

    def arrow_pair(self, x0, x1, y, out_color, in_color, out_label, in_label):
        """Dos flechas horizontales: una de ida arriba, otra de vuelta abajo."""
        head = 9
        self.add(f'<line x1="{x0}" y1="{y}" x2="{x1 - head}" y2="{y}" '
                 f'stroke="{out_color}" stroke-width="2" stroke-dasharray="5 4"/>')
        self.add(f'<polygon points="{x1 - head},{y - 5} {x1 - head},{y + 5} '
                 f'{x1},{y} " fill="{out_color}"/>')
        self.text((x0 + x1) / 2, y - 11, out_label, size=10.5, fill=out_color,
                  font=MONO, anchor="middle")

        y2 = y + 42
        self.add(f'<line x1="{x1}" y1="{y2}" x2="{x0 + head}" y2="{y2}" '
                 f'stroke="{in_color}" stroke-width="2" stroke-dasharray="5 4"/>')
        self.add(f'<polygon points="{x0 + head},{y2 - 5} {x0 + head},{y2 + 5} '
                 f'{x0},{y2}" fill="{in_color}"/>')
        self.text((x0 + x1) / 2, y2 + 19, in_label, size=10.5, fill=in_color,
                  font=MONO, anchor="middle")

    def header(self, x, y, name, color, kicker):
        self.text(x, y, name, size=15, fill=color, font=SANS, weight="bold",
                  spacing=1.2)
        self.text(x + COL_W - 36, y, kicker, size=11.5, fill=DIM, font=MONO,
                  anchor="end")

    def render(self):
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
            f'viewBox="0 0 {W} {H}">'
            f'<rect width="{W}" height="{H}" fill="{BG}"/>'
            + "".join(self.parts)
            + "</svg>"
        )


def row_of_chips(s, x, y, width, h, items, color=TXT):
    """Reparte N chips a lo ancho, con las flechitas entre medio."""
    gap = 16
    n = len(items)
    cw = (width - gap * (n - 1)) / n
    for i, item in enumerate(items):
        cx = x + i * (cw + gap)
        label, caption = item if isinstance(item, tuple) else (item, None)
        s.chip(cx, y, cw, h, label, caption, color=color)
        if i < n - 1:
            s.text(cx + cw + gap / 2, y + h / 2 + 4, "›", size=15, fill=FAINT,
                   font=SANS, anchor="middle")


def architecture():
    s = Svg()

    s.text(PAD, 54, "Jarbis", size=25, fill=TXT, weight="bold")
    s.text(PAD + 86, 54, "architecture", size=25, fill=MINT, weight="bold")
    s.text(PAD, 77, "The body pushes. Everything else reacts to it.",
           size=13, fill=DIM)

    # ── el cuerpo ────────────────────────────────────────────────────
    y = 108
    s.card(PAD, y, COL_W, 158, MINT)
    s.header(PAD + 26, y + 32, "BODY", MINT, "native on Windows")
    s.text(PAD + 26, y + 54, "WSL has no real microphone — /dev/snd only exposes a timer",
           size=11.5, fill=DIM)
    row_of_chips(s, PAD + 26, y + 72, COL_W - 52, 56, [
        ("wake word", "hey_jarvis"),
        ("Silero VAD", "576 samples"),
        ("Whisper", "small · int8"),
        ("Piper", "es_AR voice"),
    ])

    # ── el empujón ───────────────────────────────────────────────────
    s.arrow_down(PAD + COL_W / 2 - 110, y + 158, 330, MINT,
                 "WebSocket", "events as they happen, not when asked")

    # ── el cerebro ───────────────────────────────────────────────────
    y = 330
    s.card(PAD, y, COL_W, 150, AMBER)
    s.header(PAD + 26, y + 32, "BRAIN", AMBER, "in WSL")
    s.text(PAD + 26, y + 54, "One live Agent SDK session, not a process per turn",
           size=11.5, fill=DIM)
    row_of_chips(s, PAD + 26, y + 72, COL_W - 52, 52, [
        ("Haiku → Opus", "deterministic"),
        ("interrupt()", "talk over it"),
        ("MyJarbis", "the memory"),
    ])

    # ── la compuerta ─────────────────────────────────────────────────
    s.arrow_down(PAD + COL_W / 2 - 110, 480, 544, DIM, "every tool call")

    y = 544
    s.card(PAD, y, COL_W, 142, RED)
    s.header(PAD + 26, y + 32, "GATE", RED, "PreToolUse hooks")
    s.text(PAD + 26, y + 54,
           "Fires before any permission check, even with --dangerously-skip-permissions",
           size=11.5, fill=DIM)
    gap, inner = 16, COL_W - 52
    cw = (inner - gap * 2) / 3
    # Alinear las tres columnas con espacios no funciona: el chip centra la
    # cadena entera, así que el relleno desaparece y queda "read allow".
    # El veredicto va de subtítulo.
    for i, (label, verdict, color) in enumerate([
        ("read", "goes through", MINT),
        ("write", "asks first", AMBER),
        ("rm -rf", "denied", RED),
    ]):
        s.chip(PAD + 26 + i * (cw + gap), y + 72, cw, 50, label, verdict,
               color=color)

    # ── lo que ves ───────────────────────────────────────────────────
    # La columna arranca y termina a la misma altura que la del flujo: si
    # flota en el medio, el ojo la lee como una nota al margen y no como la
    # otra mitad del sistema.
    s.text(SIDE_X, 77, "what you see", size=13, fill=CYAN, weight="bold",
           font=SANS)

    s.arrow_pair(PAD + COL_W + 4, SIDE_X - 4, 200, CYAN, DIM,
                 "state file", "commands")

    y = 108
    s.card(SIDE_X, y, SIDE_W, 186, CYAN)
    s.text(SIDE_X + 24, y + 32, "ROBOT", size=15, fill=CYAN, font=SANS,
           weight="bold", spacing=1.2)
    s.text(SIDE_X + 24, y + 56, "A floating face with no", size=11.5, fill=DIM)
    s.text(SIDE_X + 24, y + 72, "window and no edges.", size=11.5, fill=DIM)
    s.text(SIDE_X + 24, y + 88, "Click it and talk.", size=11.5, fill=DIM)
    for i, color in enumerate([FAINT, MINT, AMBER, CYAN, "#f73d8f"]):
        cx = SIDE_X + 26 + i * 34
        s.add(f'<rect x="{cx}" y="{y + 108}" width="26" height="22" rx="7" '
              f'fill="{CARD}" stroke="{color}" stroke-width="1.4"/>')
        s.add(f'<rect x="{cx + 6}" y="{y + 115}" width="6" height="8" rx="2.5" '
              f'fill="{color}"/>')
        s.add(f'<rect x="{cx + 15}" y="{y + 115}" width="6" height="8" rx="2.5" '
              f'fill="{color}"/>')
    s.text(SIDE_X + 24, y + 156, "drag it · right-click", size=11, fill=FAINT,
           font=MONO)
    s.text(SIDE_X + 24, y + 172, "for the menu", size=11, fill=FAINT, font=MONO)

    y = 314
    s.card(SIDE_X, y, SIDE_W, 130, CYAN)
    s.text(SIDE_X + 24, y + 32, "FACE", size=15, fill=CYAN, font=SANS,
           weight="bold", spacing=1.2)
    s.text(SIDE_X + 24, y + 56, "The web panel, on", size=11.5, fill=DIM)
    s.text(SIDE_X + 24, y + 78, "127.0.0.1:3334", size=12.5, fill=TXT, font=MONO)
    s.text(SIDE_X + 24, y + 104, "same state file", size=11, fill=FAINT,
           font=MONO)

    y = 464
    s.card(SIDE_X, y, SIDE_W, 222, FAINT)
    s.text(SIDE_X + 24, y + 32, "SUPERVISOR", size=15, fill=TXT, font=SANS,
           weight="bold", spacing=1.2)
    s.text(SIDE_X + 24, y + 56, "Starts the four and restarts", size=11.5,
           fill=DIM)
    s.text(SIDE_X + 24, y + 72, "whatever dies, with backoff", size=11.5,
           fill=DIM)
    s.text(SIDE_X + 24, y + 88, "capped at 30 seconds.", size=11.5, fill=DIM)
    s.text(SIDE_X + 24, y + 120, "body · face · robot · brain", size=11,
           fill=DIM, font=MONO)
    s.text(SIDE_X + 24, y + 152, "jarbis.bat", size=12.5, fill=MINT, font=MONO)
    s.text(SIDE_X + 24, y + 172, "comes up with Windows,", size=11, fill=FAINT)
    s.text(SIDE_X + 24, y + 188, "with no console window.", size=11, fill=FAINT)

    s.text(PAD, H - 16,
           "Both ways would be one arrow in an MCP diagram — and it would point "
           "the wrong way.",
           size=11, fill=FAINT)

    return s.render()


def write(doc, name, scale=2.0):
    svg_path = os.path.join(HERE, name + ".svg")
    with open(svg_path, "w", encoding="utf-8") as handle:
        handle.write(doc)
    if cairosvg is None:
        print("✓", name + ".svg   (sin cairosvg, no se rasterizó)")
        return
    cairosvg.svg2png(bytestring=doc.encode("utf-8"),
                     write_to=os.path.join(HERE, name + ".png"), scale=scale)
    print("✓", name + ".svg  +  " + name + ".png")


if __name__ == "__main__":
    write(architecture(), "architecture")
