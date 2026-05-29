#!/usr/bin/env bash
set -euo pipefail

LABEL="com.granola2md.daily"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTES_DIR="$SCRIPT_DIR/notes"
LOG_DIR="$HOME/Library/Logs/granola2md"
RUN_HOUR="${GRANOLA2MD_HOUR:-8}"
RUN_MINUTE="${GRANOLA2MD_MINUTE:-0}"

# Resolve real python3 binary (not an alias)
PYTHON="$(command -v python3 2>/dev/null || true)"
if [[ -L "$PYTHON" ]]; then
    PYTHON="$(readlink -f "$PYTHON")"
fi
if [[ ! -x "$PYTHON" ]]; then
    echo "Error: python3 not found." >&2
    exit 1
fi

install() {
    if [[ -f "$PLIST_PATH" ]]; then
        echo "Service already installed. Run '$0 uninstall' first."
        exit 1
    fi

    mkdir -p "$LOG_DIR"

    cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPT_DIR}/granola2md.py</string>
        <string>--yes</string>
        <string>${NOTES_DIR}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>${RUN_HOUR}</integer>
        <key>Minute</key>
        <integer>${RUN_MINUTE}</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/granola2md.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/granola2md.error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>${HOME}</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLIST

    launchctl load -w "$PLIST_PATH"
    echo "Installed: $LABEL"
    echo "Runs daily at $(printf '%02d:%02d' "$RUN_HOUR" "$RUN_MINUTE")"
    echo "Logs: $LOG_DIR/"
}

uninstall() {
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl unload -w "$PLIST_PATH"
    fi
    rm -f "$PLIST_PATH"
    echo "Removed: $LABEL"
}

usage() {
    echo "Usage: $0 [install|uninstall]"
    echo ""
    echo "  install    Register the launchd service (runs daily at $(printf '%02d:%02d' "$RUN_HOUR" "$RUN_MINUTE"))"
    echo "  uninstall  Unload and remove the launchd service"
    echo ""
    echo "  GRANOLA2MD_HOUR=9 $0 install   # override run hour (default: 8)"
}

case "${1:-}" in
    install)   install ;;
    uninstall) uninstall ;;
    *)         usage; exit 1 ;;
esac
