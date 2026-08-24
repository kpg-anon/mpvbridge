#!/usr/bin/env bash
#
# Dev pipeline for termux-mpv-controls, run from the PC in Git Bash.
#
#   tools/dev.sh up          build, install, deploy the daemon, tail everything
#   tools/dev.sh connect     re-establish wireless adb after it drops
#   tools/dev.sh build|install|push|restart|logs|status
#
# The daemon half cannot be driven over adb directly: adb runs as the `shell` user and cannot
# read Termux's home, and Termux's RunCommandService is protected by a permission that
# `pm grant` cannot give to com.android.shell (it only flips permissions a package already
# declares). So the PC drops a zip plus a trigger file on shared storage, and tools/agent.sh --
# started once inside Termux -- picks them up. See README for the one-time setup.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANDROID_CLT="${ANDROID_HOME:-$HOME/scoop/apps/android-clt/current}"
ADB="${ADB:-$ANDROID_CLT/platform-tools/adb.exe}"

PACKAGE="io.github.kpganon.termuxmpvcontrols"
ACTIVITY="$PACKAGE/.MainActivity"
DEVICE_DIR="/sdcard/Download/termux-mpv-controls"
APK="$REPO_ROOT/android-app/app/build/outputs/apk/debug/app-debug.apk"
DEVICE_CACHE="$REPO_ROOT/.dev-device"

MDNS_SERVICE_SUFFIX="_adb-tls-connect._tcp"
LOG_TAGS="MpvSession:V MpvBridgeClient:V MpvPlayer:V MpvSilentAudio:V AndroidRuntime:E"

DEFAULT_MPV_ARGS="--bridge-verbose --vo=null https://www.youtube.com/playlist?list=PLFx03tuShoP4E0Q2fvsCiuQZcW9Gv3Msw"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

# adb mangles absolute device paths under MSYS unless conversion is off, but local paths still
# need converting the other way.
adb_dev() { MSYS_NO_PATHCONV=1 "$ADB" "$@"; }
win_path() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }

serial() { [ -f "$DEVICE_CACHE" ] && cat "$DEVICE_CACHE"; }

device_online() {
    local s
    s="$(serial)" || return 1
    [ -n "$s" ] || return 1
    "$ADB" devices 2>/dev/null | grep -q "^${s}[[:space:]]\+device$"
}

cmd_connect() {
    if device_online; then
        say "already connected: $(serial)"
        return 0
    fi
    say "looking for the device over mDNS..."
    local svc addr
    for _ in $(seq 1 15); do
        svc="$("$ADB" mdns services 2>/dev/null | grep "$MDNS_SERVICE_SUFFIX" | head -1)"
        [ -n "$svc" ] && break
    done
    [ -n "$svc" ] || die "no device found. Turn Wireless debugging back on (it switches itself off when the network changes) and re-run."

    addr="$(echo "$svc" | awk '{print $3}')"
    "$ADB" connect "$addr" >/dev/null 2>&1
    # mDNS auto-connect leaves a second transport for the same phone; drop it or every adb call
    # has to be told which one to use.
    local mdns_name
    mdns_name="$(echo "$svc" | awk '{print $1}')"
    "$ADB" disconnect "${mdns_name}.${MDNS_SERVICE_SUFFIX}" >/dev/null 2>&1
    echo "$addr" > "$DEVICE_CACHE"
    device_online || die "connected to $addr but it is not reporting as ready"
    say "connected: $addr"
}

require_device() {
    device_online || cmd_connect
    export ANDROID_SERIAL="$(serial)"
}

cmd_build() {
    say "building debug APK"
    ( cd "$REPO_ROOT/android-app" && ./gradlew :app:assembleDebug -q ) \
        || die "gradle build failed"
    [ -f "$APK" ] || die "build reported success but $APK is missing"
    say "built $(du -h "$APK" | cut -f1)"
}

cmd_install() {
    require_device
    [ -f "$APK" ] || die "no APK; run: tools/dev.sh build"
    say "installing"
    # -r keeps app data (favorites live in the app), never -d or uninstall.
    "$ADB" install -r "$(win_path "$APK")" 2>&1 | tail -1
    "$ADB" shell pm grant "$PACKAGE" android.permission.POST_NOTIFICATIONS >/dev/null 2>&1
}

pack_daemon() {
    local out="$1"
    python - "$REPO_ROOT" "$out" <<'PY'
import pathlib, sys, zipfile
root = pathlib.Path(sys.argv[1]) / "daemon"
out = pathlib.Path(sys.argv[2])
skip = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for path in root.rglob("*"):
        if path.is_dir() or any(part in skip for part in path.parts):
            continue
        z.write(path, path.relative_to(root))
print(f"{len(zipfile.ZipFile(out).namelist())} files")
PY
}

cmd_push() {
    require_device
    local zip="$REPO_ROOT/.daemon-build.zip"
    say "packing daemon: $(pack_daemon "$zip")"
    adb_dev shell "mkdir -p $DEVICE_DIR"
    adb_dev push "$(win_path "$zip")" "$DEVICE_DIR/daemon.zip" 2>&1 | tail -1

    # A CRLF anywhere in this script makes Termux's bash die on a stray carriage return. Editing it from Windows
    # reintroduces that easily, so strip it on the way out rather than trusting the working copy.
    local staged="$REPO_ROOT/.agent-lf.sh"
    tr -d '\r' < "$REPO_ROOT/tools/agent.sh" > "$staged"
    adb_dev push "$(win_path "$staged")" "$DEVICE_DIR/agent.sh" 2>&1 | tail -1
    rm -f "$zip" "$staged"
}

cmd_restart() {
    require_device
    local args="${MPV_ARGS:-$DEFAULT_MPV_ARGS}"
    say "requesting redeploy"
    adb_dev shell "mkdir -p $DEVICE_DIR"
    printf '%s\n' "$args" > "$REPO_ROOT/.run-args"
    adb_dev push "$(win_path "$REPO_ROOT/.run-args")" "$DEVICE_DIR/run.args" >/dev/null 2>&1
    rm -f "$REPO_ROOT/.run-args"
    adb_dev shell "rm -f $DEVICE_DIR/daemon.log; date > $DEVICE_DIR/deploy.trigger"

    say "waiting for the agent to bring it up"
    for _ in $(seq 1 30); do
        if adb_dev shell "grep -q 'bridge listening' $DEVICE_DIR/daemon.log 2>/dev/null" ; then
            say "daemon is listening"
            return 0
        fi
        adb_dev shell "sleep 1" >/dev/null 2>&1
    done
    warn "no 'bridge listening' within ~30s. Is tools/agent.sh running in Termux?"
    warn "last daemon output:"
    adb_dev shell "tail -20 $DEVICE_DIR/daemon.log 2>/dev/null" || true
    return 1
}

cmd_status() {
    require_device
    echo "--- device ---";        "$ADB" devices | tail -n +2
    echo "--- agent ---";         adb_dev shell "tail -3 $DEVICE_DIR/agent.log 2>/dev/null || echo '(agent has never run)'"
    echo "--- daemon ---";        adb_dev shell "tail -5 $DEVICE_DIR/daemon.log 2>/dev/null || echo '(no daemon log)'"
    echo "--- media button ---";  adb_dev shell "dumpsys media_session | grep -i 'Media button session'"
    echo "--- bridge socket ---"; adb_dev shell "cat /proc/net/tcp | grep -ci 1CBB"
}

cmd_logs() {
    require_device
    say "streaming logcat + daemon output (ctrl-c to stop)"
    adb_dev shell "tail -f -n 40 $DEVICE_DIR/daemon.log 2>/dev/null" | sed 's/^/\x1b[35m[daemon]\x1b[0m /' &
    local tailer=$!
    trap 'kill $tailer 2>/dev/null' EXIT INT TERM
    # shellcheck disable=SC2086
    "$ADB" logcat -s $LOG_TAGS | sed 's/^/\x1b[34m[app]\x1b[0m /'
}

cmd_up() {
    cmd_connect
    cmd_build
    cmd_install
    cmd_push
    cmd_restart
    adb_dev shell am start -n "$ACTIVITY" >/dev/null 2>&1
    cmd_logs
}

usage() {
    sed -n '3,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
}

case "${1:-up}" in
    connect) cmd_connect ;;
    build)   cmd_build ;;
    install) cmd_connect; cmd_install ;;
    push)    cmd_connect; cmd_push ;;
    restart) cmd_connect; cmd_restart ;;
    logs)    cmd_connect; cmd_logs ;;
    status)  cmd_connect; cmd_status ;;
    up)      cmd_up ;;
    *)       usage ;;
esac
