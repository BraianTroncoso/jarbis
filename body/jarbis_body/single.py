"""
Uno solo. Que apretar el acceso dos veces no levante dos Jarbis.

## El bug que esto arregla

Jarbis se abre con un acceso en el escritorio, y un acceso en el escritorio se
aprieta dos veces. No había nada que lo impidiera, así que cada click levantaba
un stack entero nuevo: **dos robots en pantalla**, dos cerebros, y dos cuerpos
peleando por el puerto 8765. El segundo cuerpo no podía atarlo, moría con
código 1 y el supervisor lo reponía — un bucle de reintentos que llenaba el log
y no se iba a arreglar solo.

Y lo peor no era el consumo: era que cerrar desde el menú bajaba **uno** de los
dos. Cerrabas Jarbis y Jarbis seguía ahí, que es exactamente el síntoma que
habíamos arreglado.

## Cómo

En Windows, un mutex con nombre. Es la forma canónica y la única sin estado
viejo posible: lo libera el sistema cuando el proceso muere, pase lo que pase
—cierre limpio, excepción, `taskkill`, corte de luz—, así que no existe el caso
de "quedó trabado y ahora no abre".

Un archivo con el PID adentro parece equivalente y no lo es: si el proceso se
muere mal, el archivo queda, y hay que adivinar si el PID de adentro sigue vivo
o si el sistema ya se lo dio a otro programa. Ese camino existe igual acá
abajo, pero sólo como respaldo fuera de Windows — que es donde corren los
tests, porque un guard que no se puede probar es un guard en el que no se
puede confiar.

El alcance es `Local\\`, no `Global\\`: uno por sesión de usuario. Dos personas
con sesión abierta en la misma máquina son dos escritorios, y cada uno tiene
derecho a su Jarbis.
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

# Un nombre que no choque con nada. El prefijo decide el alcance.
MUTEX_NAME = "Local\\Jarbis-Supervisor-f3a1"

# CreateMutexW devuelve un handle válido igual cuando ya existía; la diferencia
# está en el código de error.
_ERROR_ALREADY_EXISTS = 183


# Permiso mínimo para preguntar si un proceso existe. Pedir más puede fallar
# por privilegios y haría parecer muerto a algo vivo.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _pid_alive_windows(pid: int) -> bool | None:
    """
    ¿Vive ese PID? None si no estamos en Windows y hay que usar otra cosa.

    `os.kill(pid, 0)` no sirve acá. Medido en esta máquina: con un proceso vivo
    no lanza nada —bien—, pero con uno muerto lanza `OSError` a secas, no
    `ProcessLookupError`. Un `except OSError: return True` leería "muerto"
    como "vivo", el lock viejo no se reclamaría nunca y Jarbis no volvería a
    abrir. El síntoma sería que el acceso del escritorio no hace nada, sin una
    sola línea en ningún log.
    """
    try:
        import ctypes
    except ImportError:  # pragma: no cover
        return None
    if not hasattr(ctypes, "WinDLL"):
        return None

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool,
                                         ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
    except (OSError, AttributeError):  # pragma: no cover
        return None

    if not handle:
        return False              # no existe, o no se puede ni mirar

    try:
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_uint32)]
        code = ctypes.c_uint32()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        # Un PID se puede reusar, así que "existe" no prueba que sea el nuestro.
        # Pero el lock lo reclamamos sólo cuando está muerto, y confundirse para
        # el otro lado —no reclamar— sólo cuesta un arranque.
        return bool(ok) and code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(handle)


class SingleInstance:
    """
    El permiso para ser el único Jarbis.

        guard = SingleInstance()
        if not guard.acquire():
            print("ya está corriendo")
            return 0

    No hace falta soltarlo a mano: el sistema lo suelta cuando el proceso
    termina. `release()` está para los tests y para los cierres ordenados.
    """

    def __init__(self, lock_file: Path | None = None, name: str = MUTEX_NAME) -> None:
        self._name = name
        self._lock_file = lock_file
        self._handle = None       # el mutex de Windows
        self._held = False

    # ------------------------------------------------------------------ tomar

    def acquire(self) -> bool:
        """True si somos el primero. False si ya hay otro Jarbis."""
        if self._held:
            return True

        # Son TRES respuestas, no dos, y confundir dos de ellas fue el bug:
        # `mutex or archivo` trataba "ya hay otro" igual que "no puedo
        # preguntar", y se caía al archivo — que estaba libre, porque el que
        # tenía el mutex nunca había escrito ninguno. Resultado: el segundo
        # click abría un Jarbis igual, y el tercero no. Al respaldo sólo se
        # pasa cuando el mecanismo bueno NO ESTÁ.
        por_mutex = self._acquire_mutex()
        if por_mutex is not None:
            self._held = por_mutex
            return por_mutex

        if self._acquire_file():
            self._held = True
            return True
        return False

    def _acquire_mutex(self) -> bool | None:
        """
        El camino bueno: Windows.

        Devuelve True si lo tomamos, False si ya lo tiene otro, y **None si el
        mecanismo no está** — que es la única respuesta que habilita el
        respaldo. Un False que se lea como None vuelve a abrir dos Jarbis.
        """
        try:
            import ctypes
        except ImportError:  # pragma: no cover
            return None
        if not hasattr(ctypes, "WinDLL"):
            return None           # no estamos en Windows

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # Las firmas no son opcionales. Sin `restype`, ctypes asume `int`
            # y trunca el HANDLE de 64 bits a 32: medido en esta máquina, el
            # resultado daba falsy y el guard se caía al archivo sin decir
            # nada, que es como se levantaban dos Jarbis igual.
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p
            ]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            handle = kernel32.CreateMutexW(None, True, self._name)
            error = ctypes.get_last_error()
        except (OSError, AttributeError) as exc:  # pragma: no cover
            logger.debug(f"No pude crear el mutex: {exc}")
            return None

        if not handle:
            return None           # falló el mecanismo, no "está ocupado"
        if error == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False          # ocupado: esto NO cae al respaldo

        # Se guarda para que viva lo que viva el proceso: si el handle se cierra
        # —o lo junta el recolector—, el mutex se suelta y el guard deja de
        # guardar nada.
        self._handle = handle
        return True

    def _acquire_file(self) -> bool:
        """El respaldo, fuera de Windows. Con todo lo feo que eso implica."""
        if self._lock_file is None:
            # Sin mutex y sin archivo no hay forma de saberlo. Dejar pasar es
            # peor que negar: negar como mucho no abre, dejar pasar levanta un
            # segundo Jarbis, que es el bug.
            return False

        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._owner_is_alive():
                return False
            # El dueño ya no está: era basura de un cierre mal terminado.
            logger.info("Lock viejo de un Jarbis que ya no corre; lo reclamo")
            try:
                self._lock_file.unlink()
            except OSError:
                return False
            return self._acquire_file()
        except OSError as exc:
            logger.warning(f"No pude tomar el lock: {exc}")
            return False

        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        return True

    def _owner_is_alive(self) -> bool:
        """¿El PID que dejó el lock sigue vivo?"""
        try:
            pid = int(self._lock_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False          # ilegible: tratarlo como basura
        if pid == os.getpid():
            return True

        windows = _pid_alive_windows(pid)
        if windows is not None:
            return windows

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True           # existe, es de otro usuario
        except OSError:
            return True           # ante la duda, no abrir un segundo
        return True

    # ----------------------------------------------------------------- soltar

    def release(self) -> None:
        if self._handle is not None:
            try:
                import ctypes

                ctypes.WinDLL("kernel32").CloseHandle(self._handle)
            except Exception:  # pragma: no cover
                pass
            self._handle = None

        if self._lock_file is not None and self._held:
            try:
                if self._lock_file.exists() and self._owner_is_alive():
                    self._lock_file.unlink()
            except OSError:
                pass

        self._held = False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()
