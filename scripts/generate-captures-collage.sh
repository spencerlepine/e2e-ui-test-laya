#!/usr/bin/env bash
# Tile a random sample of output/*/capture.mov into one 1920x1080 grid video.
# Every clip starts at t=0; a clip that ends early turns its tile black.
#
#   scripts/generate-captures-collage.sh --max 20
#   scripts/generate-captures-collage.sh --max 50 --fps 15 --out ~/Desktop/collage.mp4
#
# Requires: ffmpeg + ffprobe (brew install ffmpeg). Works with macOS's stock bash 3.2.
set -euo pipefail

WIDTH=1920
HEIGHT=1080
MAX=20
FPS=24
OUT=""
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INPUT_DIR="$ROOT/output"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--max N] [--fps N] [--out FILE] [--input DIR]

  --max N      Max number of captures to tile; picks a random N if more exist (default: $MAX)
  --fps N      Output frame rate; lower renders faster (default: $FPS)
  --out FILE   Output file (default: output/collage-<timestamp>.mp4)
  --input DIR  Folder containing */capture.mov (default: output/)
EOF
}

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --max) MAX="${2:-}"; shift 2 ;;
    --max=*) MAX="${1#*=}"; shift ;;
    --fps) FPS="${2:-}"; shift 2 ;;
    --fps=*) FPS="${1#*=}"; shift ;;
    --out) OUT="${2:-}"; shift 2 ;;
    --out=*) OUT="${1#*=}"; shift ;;
    --input) INPUT_DIR="${2:-}"; shift 2 ;;
    --input=*) INPUT_DIR="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

case "$MAX" in ''|*[!0-9]*|0) die "--max must be a positive integer (got '$MAX')" ;; esac
case "$FPS" in ''|*[!0-9]*|0) die "--fps must be a positive integer (got '$FPS')" ;; esac
[ -d "$INPUT_DIR" ] || die "input folder not found: $INPUT_DIR"

for tool in ffmpeg ffprobe; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
      die "$tool is not installed. Install it with: brew install ffmpeg"
    fi
    die "$tool is not installed. Install Homebrew (https://brew.sh), then run: brew install ffmpeg"
  fi
done
# Capture the listings first: with pipefail, `ffmpeg | grep -q` fails when grep exits early.
FFMPEG_FILTERS="$(ffmpeg -hide_banner -filters 2>/dev/null || true)"
FFMPEG_ENCODERS="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
grep -q ' xstack ' <<<"$FFMPEG_FILTERS" \
  || die "your ffmpeg has no xstack filter (needs ffmpeg 4.1+). Run: brew upgrade ffmpeg"

# Collect captures that actually contain a video stream, then shuffle.
ALL=()
while IFS= read -r f; do
  ALL+=("$f")
done < <(find "$INPUT_DIR" -mindepth 2 -maxdepth 2 -name capture.mov -type f | awk 'BEGIN{srand()} {print rand() "\t" $0}' | sort -k1,1 | cut -f2-)
[ "${#ALL[@]}" -gt 0 ] || die "no captures found at $INPUT_DIR/*/capture.mov"

FILES=()
DURATIONS=()
for f in "${ALL[@]}"; do
  [ "${#FILES[@]}" -ge "$MAX" ] && break
  d="$(ffprobe -v error -select_streams v:0 -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null || true)"
  case "$d" in ''|N/A) echo "skipping unreadable capture: $f" >&2; continue ;; esac
  FILES+=("$f")
  DURATIONS+=("$d")
done
N="${#FILES[@]}"
[ "$N" -gt 0 ] || die "none of the captures could be read"
echo "Tiling $N of ${#ALL[@]} captures"

LONGEST="$(printf '%s\n' "${DURATIONS[@]}" | sort -g | tail -1)"

# Pick the column count that gives each tile the most usable area, using the first capture's shape.
ASPECT="$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0:s=x "${FILES[0]}" \
  | awk -Fx '$1 > 0 && $2 > 0 { print $1 / $2; exit }')"
ASPECT="${ASPECT:-1.6}"
read -r COLS ROWS TILE_W TILE_H < <(awk -v n="$N" -v W="$WIDTH" -v H="$HEIGHT" -v a="$ASPECT" 'BEGIN {
  best = -1
  for (c = 1; c <= n; c++) {
    r = int((n + c - 1) / c)
    tw = int(W / c / 2) * 2; th = int(H / r / 2) * 2
    vw = tw; vh = vw / a
    if (vh > th) { vh = th; vw = vh * a }
    if (vw * vh > best) { best = vw * vh; bc = c; br = r; btw = tw; bth = th }
  }
  print bc, br, btw, bth
}')
echo "Grid: ${COLS}x${ROWS}, tile ${TILE_W}x${TILE_H}, ${FPS} fps, ${LONGEST}s long"

# Per input: drop frames early (cheap), shrink to the tile, then pad the end with black so an
# ended clip shows black instead of freezing its last frame.
INPUT_ARGS=()
FILTER=""
LAYOUT=""
STACK=""
i=0
for f in "${FILES[@]}"; do
  INPUT_ARGS+=(-i "$f")
  FILTER+="[$i:v:0]setpts=PTS-STARTPTS,fps=$FPS,scale=$TILE_W:$TILE_H:force_original_aspect_ratio=decrease:flags=fast_bilinear,"
  FILTER+="pad=$TILE_W:$TILE_H:(ow-iw)/2:(oh-ih)/2:black,setsar=1,format=yuv420p,"
  FILTER+="tpad=stop_mode=add:stop_duration=$LONGEST:color=black[v$i];"
  STACK+="[v$i]"
  LAYOUT+="$(( (i % COLS) * TILE_W ))_$(( (i / COLS) * TILE_H ))|"
  i=$((i + 1))
done

GRID_W=$((COLS * TILE_W))
GRID_H=$((ROWS * TILE_H))
if [ "$N" -eq 1 ]; then
  FILTER+="[v0]null[grid];"
else
  FILTER+="${STACK}xstack=inputs=$N:layout=${LAYOUT%|}:fill=black[grid];"
fi
FILTER+="[grid]pad=$WIDTH:$HEIGHT:($WIDTH-$GRID_W)/2:($HEIGHT-$GRID_H)/2:black[out]"

# Prefer the Mac's hardware encoder; fall back to libx264.
if grep -q h264_videotoolbox <<<"$FFMPEG_ENCODERS"; then
  ENCODER=(-c:v h264_videotoolbox -b:v 12M)
else
  ENCODER=(-c:v libx264 -preset veryfast -crf 23)
fi

[ -n "$OUT" ] || OUT="$INPUT_DIR/collage-$(date +%Y-%m-%d_%H-%M-%S).mp4"
mkdir -p "$(dirname "$OUT")"

ffmpeg -hide_banner -loglevel error -stats -y \
  "${INPUT_ARGS[@]}" \
  -filter_complex "$FILTER" \
  -map "[out]" -an -t "$LONGEST" -r "$FPS" \
  "${ENCODER[@]}" -pix_fmt yuv420p -movflags +faststart \
  "$OUT"

echo "Wrote $OUT"
