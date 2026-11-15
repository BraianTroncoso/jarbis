#!/usr/bin/env bash
# Despliega el cuerpo al lado Windows.
#
# El repo vive en WSL, pero el cuerpo tiene que correr nativo en Windows: WSL no
# tiene acceso real al micrófono (`/dev/snd` solo trae `timer`) y PortAudio —que
# es lo que usa sounddevice— no sobrevive el puente PulseAudio de WSLg.
#
# El destino está FUERA de OneDrive a propósito. OneDrive sincroniza, y
# sincronizar un venv con sockets abiertos corrompe cosas: ya se comió un
# `pyvenv.cfg` entero una vez.
#
# No copia el venv: reusa uno que ya exista (son ~2 GB con torch y onnxruntime)
# apuntándole PYTHONPATH al paquete desplegado. Se elige con JARBIS_VENV.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Dónde vive el cuerpo del lado Windows, y qué venv usa.
#
# Se pueden pisar con variables de entorno:
#   JARBIS_DEST=/mnt/c/Users/vos/jarbis-body \
#   JARBIS_VENV=/mnt/c/Users/vos/venvs/jarbis \
#   bash scripts/deploy-body.sh
#
# Y se elige acá si Jarbis vive prendido o se llama cuando lo necesitás:
#   JARBIS_AUTOSTART=1   además arranca con Windows
#   JARBIS_AUTOSTART=0   se saca de la carpeta de Inicio
#   (sin definir)        no se toca lo que ya haya
#   JARBIS_DESKTOP=0     no crear el acceso del escritorio
#
# Si no se pasa nada, se busca el perfil de Windows. Elegir "el más reciente"
# no sirve: /mnt/c/Users tiene cuentas de servicio (WsiAccount, defaultuser0)
# que se modifican solas y ganaban la carrera. Se prefiere el perfil que YA
# tenga un despliegue o un venv; si no hay ninguno, el primero que no sea de
# sistema.
detect_win_user() {
  local users=/mnt/c/Users perfil nombre
  [[ -d $users ]] || return 1
  local sistema='^(public|default|default user|defaultuser[0-9]*|all users|wsiaccount|administrator|wdagutilityaccount)$'

  for perfil in "$users"/*; do
    [[ -d $perfil ]] || continue
    nombre="$(basename "$perfil")"
    shopt -s nocasematch
    [[ $nombre =~ $sistema ]] && { shopt -u nocasematch; continue; }
    shopt -u nocasematch
    if [[ -d "$perfil/jarbis-body" || -d "$perfil/jarbis-venv" ]]; then
      printf '%s' "$nombre"
      return 0
    fi
  done

  for perfil in "$users"/*; do
    [[ -d $perfil ]] || continue
    nombre="$(basename "$perfil")"
    shopt -s nocasematch
    [[ $nombre =~ $sistema ]] && { shopt -u nocasematch; continue; }
    shopt -u nocasematch
    printf '%s' "$nombre"
    return 0
  done
  return 1
}

WIN_USER="${JARBIS_WIN_USER:-$(detect_win_user || true)}"
if [[ -z "$WIN_USER" ]]; then
  echo "ERROR: no pude determinar tu perfil de Windows." >&2
  echo "       Pasalo a mano: JARBIS_WIN_USER=tu-usuario bash scripts/deploy-body.sh" >&2
  exit 1
fi
DEST="${JARBIS_DEST:-/mnt/c/Users/$WIN_USER/jarbis-body}"
VENV="${JARBIS_VENV:-/mnt/c/Users/$WIN_USER/jarbis-venv}"

if [[ ! -x "$VENV/Scripts/python.exe" ]]; then
  echo "ERROR: no encuentro el python del venv en $VENV" >&2
  echo "       Pasalo a mano: JARBIS_VENV=/mnt/c/ruta/al/venv bash scripts/deploy-body.sh" >&2
  exit 1
fi

echo "  repo    $REPO"
echo "  destino $DEST"
echo

mkdir -p "$DEST"/{jarbis_body,models/piper,models/wakeword,var,web}

# El paquete. --delete para que borrar un archivo acá lo borre allá también.
rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "$REPO/body/jarbis_body/" "$DEST/jarbis_body/"

# Modelos: sin --delete, para no borrar lo que se descargó del lado Windows.
rsync -a "$REPO/models/" "$DEST/models/"

# El visor web, si ya existe.
[[ -d "$REPO/web" ]] && rsync -a "$REPO/web/" "$DEST/web/"

# Los scripts de diagnóstico. Van porque `diag.bat` los corre desde el destino,
# no desde el repo: del lado Windows no hay un WSL montado donde buscarlos.
rsync -a --delete --exclude '__pycache__' "$REPO/scripts/" "$DEST/scripts/"

# Lanzadores nativos. Se generan acá para que la ruta del venv quede fija y no
# haya que acordarse del PYTHONPATH a mano.
cat > "$DEST/diag.bat" <<'BAT'
@echo off
REM Corre un script de scripts/ con el venv y las rutas puestas.
REM   diag.bat preview_robot.py --width 150 --shot salida.png
REM
REM Los %2..%9 no son decoracion: sin ellos los argumentos del script se
REM perdian y preview_robot.py se quedaba en el mainloop esperando a nadie.
REM Y no se usa shift porque shift tambien corre %0, y ahi %~dp0 deja de ser
REM la ruta del .bat.
setlocal
set VENV=__VENV_WIN__
set PYTHONPATH=%~dp0
set JARBIS_PROJECT_DIR=%~dp0
set PYTHONUTF8=1
"%VENV%\Scripts\python.exe" "%~dp0scripts\%1" %2 %3 %4 %5 %6 %7 %8 %9
endlocal
BAT

cat > "$DEST/doctor.bat" <<'BAT'
@echo off
REM Diagnóstico del cuerpo. Agregá --deep para probar síntesis y Whisper.
setlocal
set VENV=__VENV_WIN__
set PYTHONPATH=%~dp0
set JARBIS_PROJECT_DIR=%~dp0
set PYTHONUTF8=1
"%VENV%\Scripts\python.exe" -m jarbis_body.doctor %*
endlocal
BAT

cat > "$DEST/jarbis.bat" <<'BAT'
@echo off
REM Levanta Jarbis entero: cuerpo, cara, flork y cerebro.
REM Es el unico comando que hace falta.
setlocal
set VENV=__VENV_WIN__
set PYTHONPATH=%~dp0
set JARBIS_PROJECT_DIR=%~dp0
set PYTHONUTF8=1
"%VENV%\Scripts\python.exe" -m jarbis_body.supervisor %*
endlocal
BAT

# Lanzador silencioso para la carpeta de Inicio: el .vbs arranca el .bat sin
# ventana de consola. Sin esto, prender la máquina abre una ventana negra.
cat > "$DEST/jarbis-silencioso.vbs" <<'VBS'
Set shell = CreateObject("WScript.Shell")
shell.Run """" & Replace(WScript.ScriptFullName, "jarbis-silencioso.vbs", "jarbis.bat") & """", 0, False
VBS

cat > "$DEST/robot.bat" <<'BAT'
@echo off
REM Solo el robot, para probarlo suelto.
setlocal
set VENV=__VENV_WIN__
set PYTHONPATH=%~dp0
set JARBIS_PROJECT_DIR=%~dp0
set PYTHONUTF8=1
start "" "%VENV%\Scripts\pythonw.exe" -m jarbis_body.robot %*
endlocal
BAT

cat > "$DEST/face.bat" <<'BAT'
@echo off
REM El visor del estado. Va de este lado porque lee el archivo que escribe el
REM cuerpo; servirlo desde WSL leeria por 9p una ruta distinta y mostraria
REM estado viejo.
setlocal
set VENV=__VENV_WIN__
set PYTHONPATH=%~dp0
set JARBIS_PROJECT_DIR=%~dp0
set PYTHONUTF8=1
"%VENV%\Scripts\python.exe" -m jarbis_body.face %*
endlocal
BAT

cat > "$DEST/run.bat" <<'BAT'
@echo off
REM Arranca el cuerpo: wake word, STT, TTS y el puente al cerebro.
setlocal
set VENV=__VENV_WIN__
set PYTHONPATH=%~dp0
set JARBIS_PROJECT_DIR=%~dp0
set PYTHONUTF8=1
"%VENV%\Scripts\python.exe" -m jarbis_body %*
endlocal
BAT

# Los .bat se escriben con un marcador y se sustituye acá, para que la ruta del
# venv salga de la variable y no esté hardcodeada en el heredoc.
winpath() { printf '%s' "$1" | sed 's|^/mnt/\(.\)/|\U\1:/|' | tr '/' '\\'; }
VENV_WIN="$(winpath "$VENV")"
DEST_WIN="$(winpath "$DEST")"

# Las barras invertidas de una ruta de Windows son metacaracteres en el lado
# DERECHO de un `s|||` de sed: `\U` pasa a mayúsculas, `\t` es un tabulador,
# `\E` corta el modo. Sin escaparlas,
#   C:\Users\tronc\OneDrive\Escritorio\...
# se escribía
#   C:SERS<TAB>RONCONEDRIVEscritorio...
# y el .bat apuntaba a un venv inexistente. Nada falla al desplegar: falla
# cuando hacés doble click, y el error que ves no menciona ninguna ruta.
VENV_WIN_SED="${VENV_WIN//\\/\\\\}"
for bat in "$DEST"/*.bat; do
  [ -f "$bat" ] && sed -i "s|__VENV_WIN__|$VENV_WIN_SED|g" "$bat"
done

# Y se verifica, porque el modo de falla de arriba es silencioso: el .bat
# queda escrito igual, con una ruta rota adentro.
if grep -lq '__VENV_WIN__' "$DEST"/*.bat 2>/dev/null; then
  echo "ERROR: quedaron marcadores sin reemplazar en los .bat" >&2
  exit 1
fi
if ! grep -qxF "set VENV=$VENV_WIN" "$DEST/jarbis.bat"; then
  echo "ERROR: la ruta del venv se escribió mal en jarbis.bat" >&2
  echo "       esperaba: set VENV=$VENV_WIN" >&2
  grep -n '^set VENV=' "$DEST/jarbis.bat" >&2
  exit 1
fi

# El ícono: lo usa el acceso directo. Uno genérico en un escritorio lleno no se
# encuentra, y entonces la puerta de vuelta existe pero no se usa.
[[ -f "$REPO/assets/jarbis.ico" ]] && cp "$REPO/assets/jarbis.ico" "$DEST/jarbis.ico"

# ── accesos directos ────────────────────────────────────────────────────────
#
# Queda en el destino, no sólo acá: así se puede cambiar de idea sin volver a
# desplegar nada.
#
#   powershell -File shortcuts.ps1 -Autostart     prende el arranque automático
#   powershell -File shortcuts.ps1 -NoAutostart   lo apaga
cat > "$DEST/shortcuts.ps1" <<PS1
# Accesos directos de Jarbis. Generado por scripts/deploy-body.sh.
param([switch]\$Autostart, [switch]\$NoAutostart, [switch]\$NoDesktop)

\$dest   = "$DEST_WIN"
\$target = Join-Path \$dest "jarbis-silencioso.vbs"
\$icon   = Join-Path \$dest "jarbis.ico"

function Set-JarbisShortcut(\$folder) {
  \$path = Join-Path \$folder "Jarbis.lnk"
  \$shell = New-Object -ComObject WScript.Shell
  \$lnk = \$shell.CreateShortcut(\$path)
  \$lnk.TargetPath = \$target
  \$lnk.WorkingDirectory = \$dest
  if (Test-Path \$icon) { \$lnk.IconLocation = "\$icon,0" }
  \$lnk.Description = "Jarbis - tu agente, con cuerpo"
  \$lnk.Save()
  Write-Output "  + \$path"
}

function Remove-JarbisShortcut(\$folder) {
  \$path = Join-Path \$folder "Jarbis.lnk"
  if (-not (Test-Path \$path)) { return }
  # Sólo se borra el que apunta a ESTE despliegue. Borrar por nombre es borrar
  # el acceso de otra cosa que se llame igual.
  \$shell = New-Object -ComObject WScript.Shell
  if (\$shell.CreateShortcut(\$path).TargetPath -ne \$target) {
    Write-Output "  ! \$path apunta a otro lado, no lo toco"
    return
  }
  Remove-Item \$path
  Write-Output "  - \$path"
}

if (-not \$NoDesktop) { Set-JarbisShortcut ([Environment]::GetFolderPath('Desktop')) }

\$startup = [Environment]::GetFolderPath('Startup')
if (\$Autostart)        { Set-JarbisShortcut \$startup }
elseif (\$NoAutostart)  { Remove-JarbisShortcut \$startup }
PS1

PS_ARGS=()
[[ "${JARBIS_DESKTOP:-1}" == "0" ]] && PS_ARGS+=("-NoDesktop")
case "${JARBIS_AUTOSTART:-}" in
  1) PS_ARGS+=("-Autostart") ;;
  0) PS_ARGS+=("-NoAutostart") ;;
esac

echo "  accesos:"
powershell.exe -NoProfile -ExecutionPolicy Bypass \
  -File "$DEST_WIN\\shortcuts.ps1" "${PS_ARGS[@]}" 2>&1 | tr -d '\r' || \
  echo "    (no pude crear los accesos; corré shortcuts.ps1 a mano)"

echo
echo "  desplegado:"
find "$DEST" -maxdepth 2 -type f -name '*.py' | sed "s|$DEST/|    |" | head -20
echo
echo "  lanzadores: jarbis.bat (todo)  ·  diag.bat (scripts)  ·  run.bat  face.bat  robot.bat  doctor.bat"
echo
echo "  probá:  cmd.exe /c '$DEST_WIN\\doctor.bat'"
