#!/data/data/com.termux/files/usr/bin/bash
#
# Dev deploy agent for termux-mpv-controls. Run this once inside Termux and leave it running:
#
#   bash /sdcard/Download/termux-mpv-controls/agent.sh
#
# It watches shared storage for a trigger file dropped by tools/dev.sh on the PC, then redeploys
# and restarts the daemon. This exists because the PC has no way to run a command inside Termux:
# adb runs as the `shell` user, which cannot read Termux's home, and Termux's RunCommandService is
# guarded by a permission `pm grant` will not give to com.android.shell.
#
# This is a development convenience. For normal listening just run `mpvbridge ...` yourself -- the
# daemon started here is detached, so mpv has no terminal and no keyboard control.

set -uo pipefail

DEVICE_DIR="/sdcard/Download/termux-mpv-controls"
INSTALL_DIR="$HOME/termux-mpv-controls"
TRIGGER="$DEVICE_DIR/deploy.trigger"
ZIP="$DEVICE_DIR/daemon.zip"
ARGS_FILE="$DEVICE_DIR/run.args"
DAEMON_LOG="$DEVICE_DIR/daemon.log"
AGENT_LOG="$DEVICE_DIR/agent.log"
PID_FILE="$DEVICE_DIR/daemon.pid"
POLL_SECONDS=2

mkdir -p "$DEVICE_DIR" "$INSTALL_DIR"

log() {
    printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$AGENT_LOG"
}

stop_daemon() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid="$(cat "$PID_FILE" 2>/dev/null)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            log "stopping daemon pid $pid"
            kill "$pid" 2>/dev/null
            for _ in $(seq 1 10); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.3
            done
            kill -9 "$pid" 2>/dev/null
        fi
        rm -f "$PID_FILE"
    fi
    # Anything the PID file missed -- a daemon started by hand, or one left by a crashed agent.
    pkill -f 'python -m mpvbridge' 2>/dev/null
    pkill -f 'bin/mpvbridge' 2>/dev/null

    # mpv is a child process, so killing the daemon orphans it and it keeps playing. Match only
    # mpv instances started by a bridge (their IPC socket path is ours), never a hand-run mpv.
    pkill -f 'input-ipc-server=.*mpvbridge-' 2>/dev/null

    for _ in $(seq 1 10); do
        pgrep -f 'input-ipc-server=.*mpvbridge-' >/dev/null 2>&1 || break
        sleep 0.3
    done
    pkill -9 -f 'input-ipc-server=.*mpvbridge-' 2>/dev/null
    sleep 0.5
}

start_daemon() {
    local args
    args="$(cat "$ARGS_FILE" 2>/dev/null)"
    if [ -z "$args" ]; then
        log "no run.args on device; not starting"
        return 1
    fi

    log "starting: mpvbridge $args"
    : > "$DAEMON_LOG"
    # shellcheck disable=SC2086
    PYTHONPATH="$INSTALL_DIR/src" nohup python -m mpvbridge $args >>"$DAEMON_LOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    log "daemon pid $pid"

    # Surface an immediate crash rather than leaving the PC waiting for a line that never comes.
    sleep 2
    if ! kill -0 "$pid" 2>/dev/null; then
        log "daemon exited immediately; tail of its output:"
        tail -20 "$DAEMON_LOG" | tee -a "$AGENT_LOG"
        rm -f "$PID_FILE"
        return 1
    fi
}

deploy() {
    log "--- deploy requested ---"
    stop_daemon

    if [ ! -f "$ZIP" ]; then
        log "no daemon.zip at $ZIP"
        return 1
    fi
    rm -rf "$INSTALL_DIR/src"
    if ! unzip -o -q "$ZIP" -d "$INSTALL_DIR"; then
        log "unzip failed"
        return 1
    fi
    log "unpacked $(find "$INSTALL_DIR/src" -name '*.py' | wc -l) python files"

    start_daemon
}

trap 'log "agent stopping"; stop_daemon; exit 0' INT TERM

log "agent watching $TRIGGER (poll ${POLL_SECONDS}s)"
log "termux-mpv-controls dev agent ready"

while true; do
    if [ -f "$TRIGGER" ]; then
        rm -f "$TRIGGER"
        deploy || log "deploy failed"
    fi
    sleep "$POLL_SECONDS"
done
