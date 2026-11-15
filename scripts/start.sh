#!/usr/bin/env bash
# Arranca el lado WSL: el cerebro y la cara.
#
# El cuerpo va aparte porque corre nativo en Windows y necesita su propia
# consola para que el micrófono y la hotkey global funcionen:
#
#   cmd.exe /c 'C:\Users\<vos>\jarbis-body\run.bat'
#
# El orden no importa. El cerebro reconecta con backoff si el cuerpo todavía no
# está, y la cara muestra "cuerpo apagado" en vez de romperse.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

export PYTHONPATH="$REPO/body"
export JARBIS_PROJECT_DIR="$REPO"

mkdir -p var
BRAIN_LOG="var/brain.out"
FACE_LOG="var/face.out"

limpiar() {
  echo
  echo "  cerrando..."
  [[ -n "${BRAIN_PID:-}" ]] && kill "$BRAIN_PID" 2>/dev/null
  [[ -n "${FACE_PID:-}" ]] && kill "$FACE_PID" 2>/dev/null
  wait 2>/dev/null
  echo "  chau"
}
trap limpiar EXIT INT TERM

echo
echo "  Jarbis — lado WSL"
echo

python3 -m jarbis_body.face >"$FACE_LOG" 2>&1 &
FACE_PID=$!

python3 brain/client.py "$@" >"$BRAIN_LOG" 2>&1 &
BRAIN_PID=$!

sleep 2

# Verificar que los dos arrancaron antes de prometer nada.
if ! kill -0 "$FACE_PID" 2>/dev/null; then
  echo "  la cara no arrancó:"
  tail -5 "$FACE_LOG" | sed 's/^/    /'
fi
if ! kill -0 "$BRAIN_PID" 2>/dev/null; then
  echo "  el cerebro no arrancó:"
  tail -8 "$BRAIN_LOG" | sed 's/^/    /'
  echo
  echo "  Si dice que falta el token: arrancá el cuerpo primero, lo genera él."
  exit 1
fi

echo "    cara      http://127.0.0.1:3334"
echo "    cerebro   pid $BRAIN_PID  ·  log var/brain.out"
echo
echo "  Falta el cuerpo, en otra consola:"
echo "    cmd.exe /c '$(printf '%s' "${JARBIS_DEST:-/mnt/c/Users/$USER/jarbis-body}" | sed 's|^/mnt/\(.\)/|\U\1:/|' | tr '/' '\\')\\run.bat'"
echo
echo "  Activar con ctrl+shift+g. Ctrl+C acá cierra cerebro y cara."
echo

# Seguir el log del cerebro, que es donde se ve la conversación.
tail -f "$BRAIN_LOG"
