#!/usr/bin/env bash
# launch-screenpad-hud.sh — Reversible Omarchy/Hyprland launcher for toroid-hud.py
# on the ASUS ZenBook Pro Duo UX582HS lower ScreenPad Plus (DP-3, workspace 11).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
HUD_SCRIPT="${SCRIPT_DIR}/toroid-hud.py"

WORKSPACE="11"
MONITOR="DP-3"
INTERVAL="5"
WINDOW="50"
GEMINI_DIR="${HOME}/.gemini/antigravity-cli"
ATLAS_PATH=""
TERMINAL_BIN=""
DRY_RUN="0"
ONCE_MODE="0"
JSON_MODE="0"
EXPECT_ZERO_VRAM="0"

usage() {
    cat <<'EOF'
Usage: launch-screenpad-hud.sh [OPTIONS]

Launch bin/toroid-hud.py --auto-discover cleanly on the UX582HS lower ScreenPad Plus
(default workspace 11 on monitor DP-3 under Omarchy 4.5.0 per-monitor workspace blocks).

Options:
  --workspace N               Target Hyprland workspace (default: 11)
  --monitor NAME              Target Hyprland monitor (default: DP-3)
  --interval SEC              Watch refresh interval in seconds (default: 5)
  --window W                  Sliding-window size W for iota_W (default: 50)
  --gemini-dir DIR            Antigravity CLI dir for --auto-discover
  --atlas FILE                Optional UNIFIED_OPERATOR_ATLAS.md path
  --terminal BIN              Override terminal emulator (foot|alacritty|kitty|ghostty|xdg-terminal-exec)
  --expect-zero-desktop-vram  Pass --expect-zero-desktop-vram to toroid-hud.py
  --once                      Run a single snapshot in the current terminal and exit
  --json                      Emit canonical JSON (with --once)
  --dry-run                   Print resolved dispatch command without launching
  -h, --help                  Show this help text
EOF
}

require_pos_int() {
    local flag="$1"
    local val="$2"
    if [[ ! "${val}" =~ ^[1-9][0-9]*$ ]]; then
        echo "[launch-screenpad-hud] ERROR: ${flag} requires a positive integer, got '${val}'" >&2
        exit 2
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workspace)
            [[ $# -ge 2 ]] || { echo "[launch-screenpad-hud] ERROR: --workspace requires a value" >&2; exit 2; }
            WORKSPACE="$2"
            shift 2
            ;;
        --monitor)
            [[ $# -ge 2 ]] || { echo "[launch-screenpad-hud] ERROR: --monitor requires a value" >&2; exit 2; }
            MONITOR="$2"
            shift 2
            ;;
        --interval)
            [[ $# -ge 2 ]] || { echo "[launch-screenpad-hud] ERROR: --interval requires a value" >&2; exit 2; }
            INTERVAL="$2"
            shift 2
            ;;
        --window)
            [[ $# -ge 2 ]] || { echo "[launch-screenpad-hud] ERROR: --window requires a value" >&2; exit 2; }
            WINDOW="$2"
            shift 2
            ;;
        --gemini-dir)
            [[ $# -ge 2 ]] || { echo "[launch-screenpad-hud] ERROR: --gemini-dir requires a value" >&2; exit 2; }
            GEMINI_DIR="$2"
            shift 2
            ;;
        --atlas)
            [[ $# -ge 2 ]] || { echo "[launch-screenpad-hud] ERROR: --atlas requires a value" >&2; exit 2; }
            ATLAS_PATH="$2"
            shift 2
            ;;
        --terminal)
            [[ $# -ge 2 ]] || { echo "[launch-screenpad-hud] ERROR: --terminal requires a value" >&2; exit 2; }
            TERMINAL_BIN="$2"
            shift 2
            ;;
        --expect-zero-desktop-vram)
            EXPECT_ZERO_VRAM="1"
            shift
            ;;
        --once)
            ONCE_MODE="1"
            shift
            ;;
        --json)
            JSON_MODE="1"
            shift
            ;;
        --dry-run)
            DRY_RUN="1"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[launch-screenpad-hud] ERROR: unknown option '$1'" >&2
            usage >&2
            exit 2
            ;;
    esac
done

require_pos_int "--workspace" "${WORKSPACE}"
require_pos_int "--interval" "${INTERVAL}"
require_pos_int "--window" "${WINDOW}"

if [[ ! "${MONITOR}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "[launch-screenpad-hud] ERROR: invalid monitor name '${MONITOR}'" >&2
    exit 2
fi

if [[ ! -f "${HUD_SCRIPT}" ]]; then
    echo "[launch-screenpad-hud] ERROR: toroid-hud.py missing at ${HUD_SCRIPT}" >&2
    exit 2
fi

if [[ -z "${ATLAS_PATH}" ]]; then
    DEFAULT_ATLAS="${HOME}/Projects/expedition/cartography/UNIFIED_OPERATOR_ATLAS.md"
    if [[ -f "${DEFAULT_ATLAS}" ]]; then
        ATLAS_PATH="${DEFAULT_ATLAS}"
    fi
fi

HUD_CMD=(
    python3 -I -B "${HUD_SCRIPT}"
    --auto-discover
    --gemini-dir "${GEMINI_DIR}"
    --window "${WINDOW}"
)

if [[ -n "${ATLAS_PATH}" ]]; then
    HUD_CMD+=(--atlas "${ATLAS_PATH}")
fi
if [[ "${EXPECT_ZERO_VRAM}" == "1" ]]; then
    HUD_CMD+=(--expect-zero-desktop-vram)
fi

if [[ "${ONCE_MODE}" == "1" ]]; then
    HUD_CMD+=(--once)
    if [[ "${JSON_MODE}" == "1" ]]; then
        HUD_CMD+=(--json)
    fi
    if [[ "${DRY_RUN}" == "1" ]]; then
        printf 'DRY-RUN ONCE:'
        printf ' %q' "${HUD_CMD[@]}"
        printf '\n'
        exit 0
    fi
    exec "${HUD_CMD[@]}"
fi

HUD_CMD+=(--watch --interval "${INTERVAL}")

resolve_terminal() {
    if [[ -n "${TERMINAL_BIN}" ]]; then
        printf '%s\n' "${TERMINAL_BIN}"
        return 0
    fi
    for cand in foot alacritty kitty ghostty xdg-terminal-exec; do
        if command -v "${cand}" >/dev/null 2>&1; then
            printf '%s\n' "${cand}"
            return 0
        fi
    done
    return 1
}

TERM_NAME="$(resolve_terminal || true)"
if [[ -z "${TERM_NAME}" ]]; then
    echo "[launch-screenpad-hud] ERROR: no supported Wayland terminal found (foot/alacritty/kitty/ghostty/xdg-terminal-exec)" >&2
    exit 2
fi

TERM_BASE="$(basename -- "${TERM_NAME}")"
case "${TERM_BASE}" in
    foot)
        LAUNCH_CMD=("${TERM_NAME}" --app-id aletheia-toroid-hud --title "Aletheia Toroid HUD (${MONITOR})" -- "${HUD_CMD[@]}")
        ;;
    alacritty)
        LAUNCH_CMD=("${TERM_NAME}" --class aletheia-toroid-hud --title "Aletheia Toroid HUD (${MONITOR})" -e "${HUD_CMD[@]}")
        ;;
    kitty)
        LAUNCH_CMD=("${TERM_NAME}" --class aletheia-toroid-hud --title "Aletheia Toroid HUD (${MONITOR})" "${HUD_CMD[@]}")
        ;;
    ghostty)
        LAUNCH_CMD=("${TERM_NAME}" --class=aletheia-toroid-hud --title="Aletheia Toroid HUD (${MONITOR})" -e "${HUD_CMD[@]}")
        ;;
    *)
        LAUNCH_CMD=("${TERM_NAME}" "${HUD_CMD[@]}")
        ;;
esac

sh_quote() {
    local s="$1"
    printf "'%s'" "${s//\'/\'\\\'\'}"
}

lua_quote() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    printf '"%s"' "${s}"
}

QUOTED_LAUNCH=""
for arg in "${LAUNCH_CMD[@]}"; do
    QUOTED_LAUNCH+="$(sh_quote "${arg}") "
done
QUOTED_LAUNCH="${QUOTED_LAUNCH% }"

DISPATCH_RULE="[workspace ${WORKSPACE} silent] ${QUOTED_LAUNCH}"
LUA_DISPATCH_EXPR="hl.dsp.exec_cmd($(lua_quote "${DISPATCH_RULE}"))"

if [[ "${DRY_RUN}" == "1" ]]; then
    printf 'DRY-RUN MONITOR=%s WORKSPACE=%s\n' "${MONITOR}" "${WORKSPACE}"
    printf 'DRY-RUN DISPATCH (omarchy-lua): hyprctl dispatch %s\n' "$(sh_quote "${LUA_DISPATCH_EXPR}")"
    printf 'DRY-RUN DISPATCH (classic-fallback): hyprctl dispatch exec %s\n' "$(sh_quote "${DISPATCH_RULE}")"
    exit 0
fi

if ! command -v hyprctl >/dev/null 2>&1; then
    echo "[launch-screenpad-hud] ERROR: hyprctl not found; use --once for non-Hyprland terminals" >&2
    exit 2
fi

if ! command -v "${TERM_NAME}" >/dev/null 2>&1; then
    echo "[launch-screenpad-hud] ERROR: terminal '${TERM_NAME}' not executable" >&2
    exit 2
fi

DISPATCH_BACKEND=""
if hyprctl dispatch "${LUA_DISPATCH_EXPR}" >/dev/null 2>&1; then
    DISPATCH_BACKEND="omarchy-lua"
elif hyprctl dispatch exec "${DISPATCH_RULE}" >/dev/null 2>&1; then
    DISPATCH_BACKEND="classic-hyprland"
else
    echo "[launch-screenpad-hud] ERROR: hyprctl failed for both Omarchy Lua (hl.dsp.exec_cmd) and classic dispatch exec" >&2
    exit 1
fi
echo "[launch-screenpad-hud] LAUNCHED backend=${DISPATCH_BACKEND} workspace=${WORKSPACE} monitor=${MONITOR} terminal=${TERM_BASE}"
