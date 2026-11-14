#!/usr/bin/env bash
# Baja los modelos. Son 239 MB, no van al repo.
#
# Todo lo que se baja acá tiene licencia verificada:
#   hey_jarvis.onnx          Apache-2.0 (código Y pesos) — ver models/wakeword/NOTICE
#   silero_vad.onnx          MIT
#   es_AR-daniela-high       CC BY-SA 4.0 (OpenSLR 61) — la única voz argentina
#   es_ES-davefx-medium      CC0 — respaldo, ya venía en GesturePilot
#   en_US-lessac-medium      solo para el banco de pruebas del wake word
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPER="$REPO/models/piper"
WAKE="$REPO/models/wakeword"
mkdir -p "$PIPER" "$WAKE"

HF="https://huggingface.co/rhasspy/piper-voices/resolve/main"
LK="https://github.com/livekit/livekit-wakeword/releases/download/v2.1.2"
SILERO="https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"

fallos=0

get() {
  local url="$1" dest="$2" nombre="$3"
  if [[ -s "$dest" ]]; then
    printf '  ya está   %-34s %s\n' "$nombre" "$(du -h "$dest" | cut -f1)"
    return 0
  fi
  printf '  bajando   %-34s' "$nombre"
  if curl -sfL --max-time 300 -o "$dest" "$url"; then
    printf '%s\n' "$(du -h "$dest" | cut -f1)"
  else
    printf 'FALLÓ\n'
    rm -f "$dest"
    fallos=$((fallos + 1))
  fi
}

echo
echo "  Wake word y VAD"
get "$LK/hey_jarvis.onnx" "$WAKE/hey_jarvis.onnx" "hey_jarvis.onnx"
get "$LK/NOTICE"          "$WAKE/NOTICE"          "NOTICE (licencia)"
get "$SILERO"             "$WAKE/silero_vad.onnx" "silero_vad.onnx"

echo
echo "  Voces Piper"
for voz in "es/es_AR/daniela/high:es_AR-daniela-high" \
           "es/es_ES/davefx/medium:es_ES-davefx-medium" \
           "en/en_US/lessac/medium:en_US-lessac-medium"; do
  ruta="${voz%%:*}"
  nombre="${voz##*:}"
  get "$HF/$ruta/$nombre.onnx"      "$PIPER/$nombre.onnx"      "$nombre.onnx"
  get "$HF/$ruta/$nombre.onnx.json" "$PIPER/$nombre.onnx.json" "$nombre.onnx.json"
done

echo
if (( fallos )); then
  echo "  $fallos descarga(s) fallaron. Reintentá; lo que ya bajó no se vuelve a pedir."
  exit 1
fi
echo "  Todo en su lugar: $(du -sh "$REPO/models" | cut -f1)"
echo "  Ahora: bash scripts/deploy-body.sh"
echo
