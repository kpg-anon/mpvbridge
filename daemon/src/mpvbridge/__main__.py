"""``mpvbridge`` -- a drop-in mpv wrapper that publishes an Android media session.

Everything not consumed by the ``--bridge-*`` options below is handed to mpv untouched, so
``mpvbridge --no-video 'https://youtube.com/playlist?list=...'`` behaves exactly like the same mpv
invocation, plus a media session on the phone.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid

from . import __version__
from .bridge import Bridge
from .cache import PlaylistCache
from .ipc import MpvIpc
from .protocol import DEFAULT_HOST, DEFAULT_PORT, SERVER_BYE
from .server import BridgeServer

log = logging.getLogger("mpvbridge")

APP_ACTIVITY = "io.github.kpganon.termuxmpvcontrols/.MainActivity"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mpvbridge",
        description="Run mpv with an Android media session attached.",
        # mpv has hundreds of options; abbreviation matching would swallow them.
        allow_abbrev=False,
        add_help=False,
    )
    parser.add_argument("--bridge-help", action="help", help="show this message and exit")
    parser.add_argument("--bridge-version", action="version", version=f"mpvbridge {__version__}")
    parser.add_argument(
        "--bridge-port", type=int, default=DEFAULT_PORT, help=f"TCP port (default {DEFAULT_PORT})"
    )
    parser.add_argument("--bridge-host", default=DEFAULT_HOST, help="bind address")
    parser.add_argument("--bridge-token", default=None, help="shared secret the app must send")
    parser.add_argument(
        "--bridge-attach",
        metavar="SOCKET",
        default=None,
        help="attach to an mpv already listening on SOCKET instead of starting one",
    )
    parser.add_argument(
        "--bridge-no-launch",
        action="store_true",
        help="do not try to bring the companion app to the foreground",
    )
    parser.add_argument(
        "--bridge-no-wake-lock", action="store_true", help="do not acquire a Termux wake lock"
    )
    parser.add_argument(
        "--bridge-no-cache",
        action="store_true",
        help="always let mpv resolve the playlist URL instead of using the cached copy",
    )
    parser.add_argument("--bridge-verbose", action="store_true", help="log bridge activity")
    return parser


def run_quietly(command: list[str]) -> None:
    """Best-effort helper for the termux-* and am commands, which may not exist."""
    try:
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", command[0], exc)


def launch_app() -> None:
    if shutil.which("am") is None:
        log.debug("no `am` on PATH; install termux-am to auto-launch the app")
        return
    # You typed this command, so Termux is in the foreground and Android 12+ background
    # activity-start restrictions do not apply.
    run_quietly(["am", "start", "--user", "0", "-n", APP_ACTIVITY])


def source_url_for(args: list[str]) -> str | None:
    """The media argument handed to mpv -- the playlist we can later re-check for new entries.

    mpv rewrites ``playlist-path`` once yt-dlp expands a playlist, so the original URL has to be
    remembered rather than read back out of mpv.
    """
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            continue
        if arg.startswith("-"):
            # `--opt value` style: the next token belongs to this option.
            skip_next = "=" not in arg and arg.startswith("--") and arg in _VALUE_OPTIONS
            continue
        return arg
    return None


#: The few `--opt value` forms worth understanding; mpv overwhelmingly uses `--opt=value`.
_VALUE_OPTIONS = frozenset({"--input-ipc-server", "--profile", "--config-dir"})


def use_cached_playlist(
    mpv_args: list[str], source_url: str | None, cache: PlaylistCache
) -> list[str]:
    """Swap a playlist URL for a locally cached playlist file where we have one.

    Handing mpv the URL makes it run a full yt-dlp flat-playlist fetch before playback starts --
    many seconds for a large playlist, on every single launch. The cached file skips that
    entirely; mpv still resolves each video as it reaches it. Use `refresh-playlist` from the app
    (or --bridge-no-cache) when the source playlist has changed.
    """
    if not source_url or "list=" not in source_url:
        return mpv_args
    ids = cache.load_playlist(source_url)
    if len(ids) < 2:
        return mpv_args
    path = cache.write_m3u(source_url, ids)
    if path is None:
        return mpv_args
    log.info("using cached playlist of %d entries instead of resolving %s", len(ids), source_url)
    rewritten = [arg for arg in mpv_args if arg != source_url]
    rewritten.append(f"--playlist={path}")
    return rewritten


def socket_path_for(args: list[str]) -> tuple[str, bool]:
    """Return the IPC socket path and whether we have to pass it to mpv ourselves."""
    for index, arg in enumerate(args):
        if arg.startswith("--input-ipc-server="):
            return arg.split("=", 1)[1], False
        if arg == "--input-ipc-server" and index + 1 < len(args):
            return args[index + 1], False
    path = os.path.join(tempfile.gettempdir(), f"mpvbridge-{uuid.uuid4().hex[:12]}.sock")
    return path, True


async def run(options: argparse.Namespace, mpv_args: list[str]) -> int:
    process: asyncio.subprocess.Process | None = None

    source_url = source_url_for(mpv_args)
    cache = PlaylistCache()
    if not options.bridge_no_cache:
        mpv_args = use_cached_playlist(mpv_args, source_url, cache)

    if options.bridge_attach:
        socket_path = options.bridge_attach
    else:
        mpv_binary = shutil.which("mpv")
        if mpv_binary is None:
            print("mpvbridge: mpv is not on PATH (pkg install mpv)", file=sys.stderr)
            return 127
        socket_path, inject = socket_path_for(mpv_args)
        argv = [mpv_binary]
        if inject:
            argv.append(f"--input-ipc-server={socket_path}")
        argv.extend(mpv_args)
        log.debug("starting %s", argv)
        process = await asyncio.create_subprocess_exec(*argv)

    if not options.bridge_no_wake_lock:
        run_quietly(["termux-wake-lock"])

    ipc = MpvIpc(socket_path)
    try:
        await ipc.connect()
    except TimeoutError:
        print(f"mpvbridge: mpv never created {socket_path}", file=sys.stderr)
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await process.wait()
        return 1

    bridge_holder: dict[str, Bridge] = {}

    async def on_command(name: str, message: dict) -> None:
        await bridge_holder["bridge"].handle_command(name, message)

    def snapshot_provider() -> list[dict]:
        return bridge_holder["bridge"].snapshot_messages()

    server = BridgeServer(
        on_command=on_command,
        snapshot_provider=snapshot_provider,
        host=options.bridge_host,
        port=options.bridge_port,
        token=options.bridge_token,
    )
    bridge = Bridge(ipc, server, source_url=source_url, cache=cache)
    bridge_holder["bridge"] = bridge

    try:
        await server.start()
    except OSError as exc:
        print(f"mpvbridge: cannot bind {options.bridge_host}:{options.bridge_port}: {exc}",
              file=sys.stderr)
        await ipc.close()
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await process.wait()
        return 1

    await bridge.start()

    if not options.bridge_no_launch:
        launch_app()

    # Ctrl+C reaches mpv too; let mpv decide when playback ends and just follow it out.
    with contextlib.suppress(NotImplementedError, ValueError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, lambda: None)

    # SIGTERM does not reach mpv -- it is a child, not a process-group sibling -- so without this
    # a killed daemon leaves mpv playing forever. Two of those overlapping is very audible.
    def terminate_mpv() -> None:
        if process is not None:
            log.info("terminating mpv on SIGTERM")
            with contextlib.suppress(ProcessLookupError):
                process.terminate()

    with contextlib.suppress(NotImplementedError, ValueError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, terminate_mpv)

    if process is not None:
        exit_code = await process.wait()
    else:
        await ipc.closed.wait()
        exit_code = 0

    await server.broadcast({"type": SERVER_BYE})
    await bridge.stop()
    await server.stop()
    await ipc.close()

    if not options.bridge_no_wake_lock:
        run_quietly(["termux-wake-unlock"])

    with contextlib.suppress(OSError):
        if not options.bridge_attach:
            os.unlink(socket_path)

    return exit_code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    options, mpv_args = build_parser().parse_known_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if options.bridge_verbose else logging.WARNING,
        format="mpvbridge: %(message)s",
        stream=sys.stderr,
    )

    try:
        return asyncio.run(run(options, mpv_args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
