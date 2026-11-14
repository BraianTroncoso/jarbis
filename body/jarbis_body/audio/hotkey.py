"""
Handler de hotkeys globales para GesturePilot.

Este módulo permite activar la grabación de voz mediante
combinaciones de teclas que funcionan desde cualquier aplicación.

Hotkeys por defecto:
- Ctrl+Shift+G: Activar/desactivar grabación de voz
- Ctrl+Shift+Q: Detener GesturePilot

Usa la librería `keyboard` para Windows.
Para Linux/Mac se podría usar `pynput` como alternativa.
"""

import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Optional

from loguru import logger

# Intentar importar keyboard (Windows)
try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False
    logger.warning("'keyboard' library not found. Hotkeys disabled. Install with: pip install keyboard")


class HotkeyAction(Enum):
    """Acciones que pueden ser disparadas por hotkeys."""
    TOGGLE_RECORDING = auto()  # Ctrl+Shift+G
    STOP_SYSTEM = auto()       # Ctrl+Shift+Q
    CANCEL_RECORDING = auto()  # Escape


@dataclass
class HotkeyConfig:
    """Configuración de un hotkey."""
    action: HotkeyAction
    keys: str  # Formato keyboard: "ctrl+shift+g"
    description: str


# Hotkeys por defecto
DEFAULT_HOTKEYS = [
    HotkeyConfig(
        action=HotkeyAction.TOGGLE_RECORDING,
        keys="ctrl+shift+g",
        description="Activar/desactivar grabación de voz",
    ),
    HotkeyConfig(
        action=HotkeyAction.STOP_SYSTEM,
        keys="ctrl+shift+q",
        description="Detener GesturePilot",
    ),
    HotkeyConfig(
        action=HotkeyAction.CANCEL_RECORDING,
        keys="escape",
        description="Cancelar grabación actual",
    ),
]


# Tipo para callbacks
HotkeyCallback = Callable[[HotkeyAction], None]


class HotkeyHandler:
    """
    Handler de hotkeys globales.

    Uso:
    ----
    ```python
    handler = HotkeyHandler()

    def on_hotkey(action: HotkeyAction):
        if action == HotkeyAction.TOGGLE_RECORDING:
            print("Grabación activada!")

    handler.on_hotkey(on_hotkey)
    handler.start()

    # ... el handler corre en background ...

    handler.stop()
    ```
    """

    def __init__(
        self,
        hotkeys: list[HotkeyConfig] | None = None,
        suppress: bool = False,  # Si True, bloquea la tecla original
    ):
        """
        Inicializa el handler.

        Args:
            hotkeys: Lista de configuraciones de hotkeys
            suppress: Si bloquear las teclas originales
        """
        if not HAS_KEYBOARD:
            logger.warning("HotkeyHandler created but keyboard library not available")

        self._hotkeys = hotkeys or DEFAULT_HOTKEYS
        self._suppress = suppress
        self._callbacks: list[HotkeyCallback] = []
        self._registered_hotkeys: list[str] = []
        self._running = False
        self._cooldown: dict[HotkeyAction, float] = {}  # Para evitar múltiples disparos
        self._cooldown_time = 0.5  # Segundos entre activaciones

    def on_hotkey(self, callback: HotkeyCallback) -> None:
        """
        Registra un callback para cuando se presiona un hotkey.

        Args:
            callback: Función(HotkeyAction) a llamar
        """
        self._callbacks.append(callback)

    def start(self) -> bool:
        """
        Inicia el listener de hotkeys.

        Returns:
            True si se inició correctamente
        """
        if not HAS_KEYBOARD:
            logger.error("Cannot start HotkeyHandler: keyboard library not available")
            return False

        if self._running:
            logger.warning("HotkeyHandler already running")
            return True

        logger.info("Starting HotkeyHandler...")

        # Registrar cada hotkey
        for config in self._hotkeys:
            try:
                keyboard.add_hotkey(
                    config.keys,
                    lambda c=config: self._on_hotkey_pressed(c),
                    suppress=self._suppress,
                )
                self._registered_hotkeys.append(config.keys)
                logger.debug(f"Registered hotkey: {config.keys} -> {config.action.name}")
            except Exception as e:
                logger.error(f"Failed to register hotkey {config.keys}: {e}")

        self._running = True
        logger.info(f"HotkeyHandler started with {len(self._registered_hotkeys)} hotkeys")

        return True

    def stop(self) -> None:
        """Detiene el listener de hotkeys."""
        if not self._running:
            return

        logger.info("Stopping HotkeyHandler...")

        # Desregistrar hotkeys
        for keys in self._registered_hotkeys:
            try:
                keyboard.remove_hotkey(keys)
            except Exception as e:
                logger.debug(f"Failed to remove hotkey {keys}: {e}")

        self._registered_hotkeys.clear()
        self._running = False
        logger.info("HotkeyHandler stopped")

    def _on_hotkey_pressed(self, config: HotkeyConfig) -> None:
        """
        Callback interno cuando se presiona un hotkey.

        Incluye cooldown para evitar múltiples disparos.
        """
        now = time.time()

        # Verificar cooldown
        last_time = self._cooldown.get(config.action, 0)
        if now - last_time < self._cooldown_time:
            logger.debug(f"Hotkey {config.keys} ignored (cooldown)")
            return

        self._cooldown[config.action] = now

        logger.info(f"Hotkey pressed: {config.keys} -> {config.action.name}")

        # Notificar callbacks
        for callback in self._callbacks:
            try:
                callback(config.action)
            except Exception as e:
                logger.error(f"Error in hotkey callback: {e}")

    @property
    def is_running(self) -> bool:
        """True si el handler está corriendo."""
        return self._running

    @property
    def registered_hotkeys(self) -> list[str]:
        """Lista de hotkeys registrados."""
        return self._registered_hotkeys.copy()


class HotkeyManager:
    """
    Manager de alto nivel para hotkeys en GesturePilot.

    Integra el HotkeyHandler con el flujo de GesturePilot.

    Uso:
    ----
    ```python
    manager = HotkeyManager()

    # Configurar callbacks
    manager.on_toggle_recording = lambda: print("Toggle!")
    manager.on_cancel = lambda: print("Cancel!")
    manager.on_stop = lambda: print("Stop!")

    manager.start()
    ```
    """

    def __init__(self):
        self._handler = HotkeyHandler()
        self._handler.on_hotkey(self._dispatch)

        # Callbacks públicos
        self.on_toggle_recording: Optional[Callable[[], None]] = None
        self.on_cancel: Optional[Callable[[], None]] = None
        self.on_stop: Optional[Callable[[], None]] = None

    def _dispatch(self, action: HotkeyAction) -> None:
        """Despacha la acción al callback correspondiente."""
        if action == HotkeyAction.TOGGLE_RECORDING:
            if self.on_toggle_recording:
                self.on_toggle_recording()
        elif action == HotkeyAction.CANCEL_RECORDING:
            if self.on_cancel:
                self.on_cancel()
        elif action == HotkeyAction.STOP_SYSTEM:
            if self.on_stop:
                self.on_stop()

    def start(self) -> bool:
        """Inicia el manager."""
        return self._handler.start()

    def stop(self) -> None:
        """Detiene el manager."""
        self._handler.stop()

    @property
    def is_running(self) -> bool:
        """True si está corriendo."""
        return self._handler.is_running


# =============================================================================
# Funciones de conveniencia
# =============================================================================

_global_handler: Optional[HotkeyHandler] = None


def register_global_hotkey(
    keys: str,
    callback: Callable[[], None],
    suppress: bool = False,
) -> bool:
    """
    Registra un hotkey global de forma simple.

    Args:
        keys: Combinación de teclas (ej: "ctrl+shift+g")
        callback: Función a llamar cuando se presiona
        suppress: Si bloquear la tecla original

    Returns:
        True si se registró correctamente
    """
    if not HAS_KEYBOARD:
        return False

    try:
        keyboard.add_hotkey(keys, callback, suppress=suppress)
        logger.info(f"Registered global hotkey: {keys}")
        return True
    except Exception as e:
        logger.error(f"Failed to register hotkey {keys}: {e}")
        return False


def unregister_global_hotkey(keys: str) -> bool:
    """
    Desregistra un hotkey global.

    Args:
        keys: Combinación de teclas a desregistrar

    Returns:
        True si se desregistró correctamente
    """
    if not HAS_KEYBOARD:
        return False

    try:
        keyboard.remove_hotkey(keys)
        logger.info(f"Unregistered global hotkey: {keys}")
        return True
    except Exception as e:
        logger.error(f"Failed to unregister hotkey {keys}: {e}")
        return False
