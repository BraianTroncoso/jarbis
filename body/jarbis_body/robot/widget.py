"""
El robot flotante.

Una pantallita oscura sin bordes, siempre arriba, con forma de verdad: los
píxeles que no dibuja dejan pasar el click al escritorio.

## Cómo se logra la forma

`-transparentcolor` es una extensión de Tk sólo para Windows, y es lo que hace
posible todo esto: el color que se le pase se vuelve **transparente y además
click-through**. Se pinta todo el fondo de un magenta imposible y queda
flotando sólo la pantalla.

Sin eso habría un rectángulo gris alrededor, que es exactamente lo que hace que
un widget se sienta pegoteado y no parte del escritorio.

## Lee, no pregunta

El estado sale de `jarbis-state.json`, el mismo archivo que lee el panel web.
Sin socket propio y sin orden de arranque: el robot se puede abrir y cerrar sin
que el cuerpo se entere, y si el cuerpo está apagado lo muestra en vez de
romperse. Para mandar comandos usa `BridgeCommand`, que abre una conexión corta
y la cierra.
"""

from __future__ import annotations

import json
import tkinter as tk
import webbrowser
from pathlib import Path
from typing import Any

from jarbis_body.bridge.command import BridgeCommand
from jarbis_body.config import settings
from jarbis_body.robot import render, shape
from jarbis_body.robot.idle import IdleLife
from jarbis_body.robot.menu import Item, RobotMenu

# Cada cuánto se relee el archivo de estado. Igual que el panel web.
STATE_MS = 300
# 20 fps: alcanza para un parpadeo y una onda, y deja la CPU tranquila. Esto
# corre todo el día.
ANIM_MS = 50
# Dormido no hay nada que animar, así que se baja a 4 fps. Antes dibujaba a 20
# con los ojos cerrados: estar en reposo costaba lo mismo que estar atento.
ANIM_SLEEP_MS = 250

# Cuánto queda el globo en pantalla.
BUBBLE_MS = 7000

# Espacio alrededor de la pantalla, para que el halo no quede cortado.
PAD = 14
# Alto reservado arriba para el globo de diálogo.
BUBBLE_SPACE = 86

# Cuánto hay que mantener apretado para que sea push-to-talk y no un click.
HOLD_MS = 350


class RobotWidget:
    """El robot en pantalla."""

    def __init__(
        self,
        width: int = 128,
        state_file: Path | None = None,
        position_file: Path | None = None,
    ) -> None:
        self._scale = shape.scale_for(width)
        self._state_file = state_file or settings.state_file
        self._position_file = position_file or (settings.var_dir / "robot-pos.json")
        self._quit_mark = settings.var_dir / "quit.mark"
        self._command = BridgeCommand(kind="robot")

        # Estado leído del cuerpo.
        self._state = "dormido"
        self._heard: str | None = None
        self._said: str | None = None
        self._alive = False
        self._muted = False

        # Animación.
        self._phase = 0.0
        self._blink_until = 0.0
        self._next_blink = 0.0
        self._bubble_text: str | None = None
        self._bubble_until = 0.0

        # Interacción.
        self._press_x = 0
        self._press_y = 0
        self._holding = False
        self._hold_job: str | None = None
        self._dragging = False
        self._win_x = 0
        self._win_y = 0

        # La vida en reposo: mirada, parpadeo y respiración. Vive aparte y sin
        # tkinter para poder probarla.
        self._life = IdleLife()
        self._vida = self._life.update(self._now(), None, (0.0, 0.0))
        self._awake = False
        self._last_tick = self._now()

        # La diferencia entre "me cerraron" y "me morí". La lee el supervisor.
        self.closed_by_user = False

        self._build()

    # ========================================================================
    # Ventana
    # ========================================================================

    def _build(self) -> None:
        pantalla_w = shape.SCREEN_W * self._scale
        pantalla_h = shape.SCREEN_H * self._scale
        self._win_w = int(pantalla_w + PAD * 2)
        self._win_h = int(pantalla_h + PAD * 2 + BUBBLE_SPACE)

        self.root = tk.Tk()
        self.root.title("Jarbis")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", shape.CHROMA)
        except tk.TclError:
            # Fuera de Windows no existe: se degrada a una ventana oscura.
            self.root.configure(bg=shape.SCREEN_BG)

        x, y = self._load_position()
        self.root.geometry(f"{self._win_w}x{self._win_h}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root, width=self._win_w, height=self._win_h,
            bg=shape.CHROMA, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        # La pantalla va abajo; arriba queda el espacio del globo.
        self._dx = PAD
        self._dy = PAD + BUBBLE_SPACE

        self.menu = RobotMenu(self.root)
        self._bind()

    def _bind(self) -> None:
        c = self.canvas
        # Botón izquierdo: hablar. Click corto abre el micrófono y el VAD
        # cierra; mantener apretado graba mientras lo tengas.
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Double-Button-1>", self._on_double)
        # Botón derecho: menú al soltar sin mover, arrastrar para reubicar.
        # Va en el derecho a propósito: el izquierdo ya significa "hablar", y
        # mezclar arrastrar con mantener-apretado haría que un pulso de la mano
        # te cancele la frase.
        c.bind("<ButtonPress-3>", self._on_right_press)
        c.bind("<B3-Motion>", self._on_right_drag)
        c.bind("<ButtonRelease-3>", self._on_right_release)

    # ========================================================================
    # Posición
    # ========================================================================

    def _load_position(self) -> tuple[int, int]:
        """Dónde estaba la última vez; por default, abajo a la derecha."""
        try:
            data = json.loads(self._position_file.read_text(encoding="utf-8"))
            x, y = int(data["x"]), int(data["y"])
            # Una posición vieja puede quedar fuera de pantalla si cambiaste de
            # monitor, y el robot sería invisible.
            if self._on_screen(x, y):
                return x, y
        except Exception:
            pass

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        return sw - self._win_w - 26, sh - self._win_h - 70

    def _on_screen(self, x: int, y: int) -> bool:
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        return -self._win_w + 60 < x < sw - 60 and -40 < y < sh - 60

    def _save_position(self) -> None:
        try:
            self._position_file.parent.mkdir(parents=True, exist_ok=True)
            self._position_file.write_text(
                json.dumps({"x": self.root.winfo_x(), "y": self.root.winfo_y()}),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ========================================================================
    # Interacción
    # ========================================================================

    def _on_press(self, event: tk.Event) -> None:
        self._life.saw_you(self._now())
        """
        Apretaste. Todavía no se sabe si es click o mantener, así que se agenda:
        si a los 350 ms seguís apretando, es push-to-talk.
        """
        self.menu.close()
        self._press_x, self._press_y = event.x_root, event.y_root
        self._holding = False
        self._hold_job = self.root.after(HOLD_MS, self._begin_hold)

    def _begin_hold(self) -> None:
        self._hold_job = None
        self._holding = True
        self._command.mode(ptt=True)
        self._command.listen()
        self._flash("te escucho")

    def _on_release(self, _event: tk.Event) -> None:
        if self._hold_job is not None:
            # Soltaste antes del umbral: fue un click corto.
            self.root.after_cancel(self._hold_job)
            self._hold_job = None
            self._talk_click()
            return

        if self._holding:
            self._holding = False
            self._command.close()
            self._command.mode(ptt=False)

    def _talk_click(self) -> None:
        """Click corto: modo automático, el VAD cierra cuando te callás."""
        self._command.mode(ptt=False)
        ok, detalle = self._command.listen()
        self._flash("te escucho" if ok else detalle)

    def _on_double(self, _event: tk.Event) -> None:
        # El doble click llega después de un click simple, que ya pidió
        # escuchar. Se cancela eso antes de abrir el panel.
        self._command.sleep()
        self._open_panel()

    def _on_right_press(self, event: tk.Event) -> None:
        self._life.saw_you(self._now())
        self._press_x, self._press_y = event.x_root, event.y_root
        self._dragging = False
        self._win_x = self.root.winfo_x()
        self._win_y = self.root.winfo_y()

    def _on_right_drag(self, event: tk.Event) -> None:
        dx = event.x_root - self._press_x
        dy = event.y_root - self._press_y
        # Umbral: sin esto el temblor de la mano al hacer click derecho movería
        # el robot un píxel y se comería el menú.
        if not self._dragging and abs(dx) + abs(dy) < 6:
            return
        self._dragging = True
        self.menu.close()
        self.root.geometry(f"+{self._win_x + dx}+{self._win_y + dy}")

    def _on_right_release(self, event: tk.Event) -> None:
        if self._dragging:
            self._dragging = False
            self._save_position()
            return
        self._show_menu(event.x_root, event.y_root)

    def _show_menu(self, x: int, y: int) -> None:
        self.menu.show(x, y, [
            Item("Hablar", self._talk_click, accent=True),
            Item(),
            Item("Abrir el panel", self._open_panel),
            Item("Silenciar", self._toggle_mute, toggle=self._muted),
            Item(),
            Item("Cerrar Jarbis", self.quit),
            # Cerrar ahora baja todo y no vuelve solo, así que el menú tiene
            # que decir dónde está la puerta de vuelta. Sin acción: es un
            # renglón apagado, no un ítem.
            Item("vuelve desde el escritorio"),
        ])

    def _open_panel(self) -> None:
        webbrowser.open("http://127.0.0.1:3334")

    def _toggle_mute(self) -> None:
        nuevo = not self._muted
        ok, detalle = self._command.send("mode", {"tts": not nuevo})
        if not ok:
            self._flash(detalle)
            return
        # No se toca `self._muted` acá: lo va a traer el archivo de estado en la
        # próxima lectura. Una sola fuente de verdad.
        self._flash("silenciado" if nuevo else "te hablo de nuevo")

    def _flash(self, texto: str) -> None:
        """Globo corto, sin esperar al archivo de estado."""
        self._bubble_text = texto
        self._bubble_until = self._now() + 2.5

    # ========================================================================
    # Estado
    # ========================================================================

    @staticmethod
    def _now() -> float:
        import time

        return time.monotonic()

    def _poll_state(self) -> None:
        try:
            data: dict[str, Any] = json.loads(
                self._state_file.read_text(encoding="utf-8")
            )
            self._apply_state(data)
        except (OSError, ValueError):
            self._alive = False
            self._state = "dormido"

        self.root.after(STATE_MS, self._poll_state)

    def _apply_state(self, data: dict[str, Any]) -> None:
        from datetime import datetime

        # El cuerpo late cada 2 s; más viejo que 5 es que está apagado. Mismo
        # criterio que el panel web.
        updated = data.get("updated_at")
        self._alive = False
        if updated:
            try:
                age = (datetime.now() - datetime.fromisoformat(updated)).total_seconds()
                self._alive = age < 5.0
            except ValueError:
                pass

        self._state = data.get("state", "dormido") if self._alive else "dormido"
        # El mute lo dicta el cuerpo, no el robot: así el menú abre con el valor
        # real aunque lo hayas cambiado desde el panel.
        if "tts" in data:
            self._muted = not bool(data["tts"])

        # Sólo se muestra lo nuevo: sin esto el globo reaparecería en cada
        # lectura, tres veces por segundo.
        heard, said = data.get("heard"), data.get("said")
        if heard and heard != self._heard:
            self._heard = heard
            self._show_bubble(heard)
        if said and said != self._said:
            self._said = said
            self._show_bubble(said)

    def _show_bubble(self, texto: str) -> None:
        self._bubble_text = texto
        self._bubble_until = self._now() + BUBBLE_MS / 1000

    # ========================================================================
    # Dibujo
    # ========================================================================

    def _face_center(self) -> tuple[float, float]:
        """El centro de la carita en coordenadas de pantalla.

        No es el centro de la ventana: arriba hay reservado el alto del globo,
        así que el centro geométrico cae por encima de la cara y la mirada
        quedaría corrida hacia abajo todo el tiempo.
        """
        return (
            self.root.winfo_rootx() + self._dx + shape.SCREEN_W * self._scale / 2,
            self.root.winfo_rooty() + self._dy + shape.SCREEN_H * self._scale / 2,
        )

    def _cursor(self) -> tuple[float, float] | None:
        try:
            x, y = self.root.winfo_pointerxy()
        except tk.TclError:
            return None
        # Tk devuelve (-1, -1) cuando el puntero no está en esta pantalla.
        return None if x < 0 or y < 0 else (float(x), float(y))

    def _animate(self) -> None:
        ahora = self._now()

        vida = self._life.update(ahora, self._cursor(), self._face_center())
        self._awake = vida.awake
        self._vida = vida

        # El reposo respira más lento que el latido de los estados activos: un
        # ciclo de seis segundos contra uno de uno y medio. Y el paso sale del
        # tiempo real, no del intervalo nominal: dormido tickea cinco veces más
        # lento, así que con un paso fijo el ciclo duraría cinco veces más.
        dt = max(0.0, min(ahora - self._last_tick, 0.5))
        self._last_tick = ahora
        ciclo = 6.0 if self._quieto() else 1.5
        self._phase = (self._phase + dt / ciclo) % 1.0

        self._draw()
        self.root.after(
            ANIM_MS if (self._awake or not self._quieto()) else ANIM_SLEEP_MS,
            self._animate,
        )

    def _quieto(self) -> bool:
        """¿El cuerpo no está haciendo nada? Entonces manda la vida en reposo."""
        return self._state == "dormido"

    def _draw(self) -> None:
        self.canvas.delete("all")

        vida = self._vida
        # Despierto es una forma de dibujar el reposo, no un estado del cuerpo:
        # mientras escucha, piensa o habla, la expresión ya está ocupada.
        estado = self._state
        anim = render.Anim(phase=self._phase,
                           blinking=self._now() < self._blink_until)
        if self._quieto():
            if vida.awake:
                estado = "despierto"
            anim = render.Anim(
                phase=self._phase,
                blinking=vida.blinking,
                gaze_x=vida.gaze_x,
                gaze_y=vida.gaze_y,
            )

        render.draw_robot(
            self.canvas, estado, self._dx, self._dy, self._scale, anim,
        )

        if not self._alive:
            self._draw_offline()

        self._draw_bubble()

    def _draw_offline(self) -> None:
        """Marca discreta de que el cuerpo no está corriendo."""
        self.canvas.create_text(
            self._dx + shape.SCREEN_W * self._scale / 2,
            self._dy + shape.SCREEN_H * self._scale + 9,
            text="apagado", fill="#55555f", font=("Segoe UI", 7),
        )

    def _draw_bubble(self) -> None:
        """El globo, arriba de la pantalla."""
        if not self._bubble_text or self._now() > self._bubble_until:
            return

        texto = self._bubble_text
        if len(texto) > 120:
            texto = texto[:117] + "..."

        c = self.canvas
        pad = 10
        pal = shape.palette(self._state)

        item = c.create_text(
            self._win_w / 2, BUBBLE_SPACE / 2 + 4, text=texto, fill="#e8e8f0",
            font=("Segoe UI", 8), width=self._win_w - pad * 3,
            justify="center", anchor="center",
        )
        x0, y0, x1, y1 = c.bbox(item)

        caja = c.create_polygon(
            shape.rounded_rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad, 11),
            fill="#15151d", outline=pal.bubble, width=1,
        )
        c.tag_lower(caja, item)

    # ========================================================================
    # Ciclo de vida
    # ========================================================================

    def run(self) -> bool:
        """Corre hasta que se cierre. Devuelve si lo cerraste vos."""
        self._poll_state()
        self._animate()
        self.root.mainloop()
        return self.closed_by_user

    def quit(self) -> None:
        """Cerrar de verdad: baja el robot y, con él, Jarbis entero."""
        self.closed_by_user = True
        self._leave_quit_mark()
        self._save_position()
        self.menu.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _leave_quit_mark(self) -> None:
        """
        La seña que acompaña al código de salida, y el rastro de quién la dejó.

        El código 42 solo no alcanza. Un código de salida es un número que
        cualquiera puede producir: un proceso que se rompe, un kill, un
        lanzador que propaga o trunca el código de su hijo. Confiar en el
        número solo significa que algún día Jarbis se apaga entero y el log
        dice "lo cerraste vos" sin que hayas tocado nada.

        Por eso el supervisor pide las dos cosas, y esta función se llama
        desde un solo lugar: el ítem del menú.

        Y además se anota quién llamó. Durante el desarrollo vi dos apagados
        que no pude reproducir ni explicar; si vuelve a pasar, esto es la
        diferencia entre un misterio y un archivo con la pila de llamadas.
        """
        import time
        import traceback

        try:
            self._quit_mark.parent.mkdir(parents=True, exist_ok=True)
            self._quit_mark.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            # Si no se puede escribir, el supervisor repone el robot. Molesto,
            # pero infinitamente mejor que apagar todo por las dudas.
            pass

        try:
            with (settings.var_dir / "robot-quit.log").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
                    + "".join(traceback.format_stack()[:-1])
                )
        except OSError:
            pass
