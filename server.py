#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.3,<3"]
# ///
"""MCP server that drives Ghostty terminals on macOS through Ghostty's AppleScript
dictionary (Ghostty >= 1.3): open, close, focus, retitle, type into, read and wait
on terminals."""

import fcntl
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

BRIDGE = Path(__file__).with_name("ghostty.js")
STATE_DIR = Path(os.environ.get("GHOSTTY_MCP_STATE", "~/.local/state/ghostty-mcp")).expanduser()


class GhosttyError(ToolError):
    """Raised with a message the calling agent gets to see."""


def call(op: str, **args):
    proc = subprocess.run(
        ["osascript", "-l", "JavaScript", str(BRIDGE), json.dumps({"op": op, **args})],
        capture_output=True, text=True, timeout=60,
    )
    out = proc.stdout.strip()
    if not out:
        raise GhosttyError(proc.stderr.strip() or "no reply from osascript")
    reply = json.loads(out)
    if "error" in reply:
        raise GhosttyError(reply["error"])
    return reply["ok"]


@contextmanager
def locked(name: str):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / f"{name}.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# --- tty tracking -----------------------------------------------------------
# Ghostty 1.3 does not expose a terminal's tty, so the tty that appears when this
# server creates a terminal is recorded in a state file shared by every server
# instance. Ghostty 1.4 adds a `tty` property, which takes precedence.

TTY_FILE = STATE_DIR / "ttys.json"


def load_ttys() -> dict:
    try:
        return json.loads(TTY_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_tty(term_id: str, tty: str):
    with locked("ttys"):
        ttys = load_ttys()
        ttys[term_id] = tty
        TTY_FILE.write_text(json.dumps(ttys))


def prune_ttys(live_ids: set):
    with locked("ttys"):
        ttys = load_ttys()
        kept = {k: v for k, v in ttys.items() if k in live_ids}
        if kept != ttys:
            TTY_FILE.write_text(json.dumps(kept))


def ps_rows() -> list:
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,tpgid=,tty=,args="],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) == 6:
            rows.append(dict(zip(("pid", "ppid", "pgid", "tpgid", "tty", "args"), parts)))
    return rows


def ghostty_ttys(rows=None) -> set:
    rows = rows if rows is not None else ps_rows()
    app = {r["pid"] for r in rows if r["args"].split(" ")[0].endswith("Ghostty.app/Contents/MacOS/ghostty")}
    return {r["tty"] for r in rows if r["ppid"] in app and r["tty"] != "??"}


def capture_new_tty(term_id: str, before: set):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        new = ghostty_ttys() - before
        if len(new) == 1:
            tty = new.pop()
            save_tty(term_id, tty)
            return tty
        if len(new) > 1:
            return None  # another terminal opened at the same time: ambiguous
        time.sleep(0.1)
    return None


def foregrounds(rows=None) -> dict:
    """tty -> {pid, name, args} of the foreground process group leader."""
    rows = rows if rows is not None else ps_rows()
    fg = {}
    for r in rows:
        if r["tty"] != "??" and r["pid"] == r["pgid"] == r["tpgid"]:
            argv0 = r["args"].split(" ")[0]
            fg[r["tty"]] = {"pid": int(r["pid"]), "name": os.path.basename(argv0).lstrip("-"), "args": r["args"]}
    return fg


def tty_of(term: dict, ttys=None):
    tty = term.get("tty")
    if tty:
        return tty.removeprefix("/dev/")
    return (ttys if ttys is not None else load_ttys()).get(term["id"])


# --- terminals ---------------------------------------------------------------

def flat_terminals() -> tuple:
    tree = call("list")
    rows = ps_rows()
    fg, ttys = foregrounds(rows), load_ttys()
    out = []
    for w in tree["windows"]:
        for tab in w["tabs"]:
            for t in tab["terminals"]:
                item = {
                    "id": t["id"], "title": t["title"], "cwd": t["cwd"], "focused_in_tab": t["focused"],
                    "window_id": w["id"], "front_window": w["front"],
                    "tab_id": tab["id"], "tab_index": tab["index"], "tab_selected": tab["selected"],
                }
                tty = tty_of(t, ttys)
                if tty:
                    item["tty"] = tty
                    item["foreground"] = fg.get(tty)
                out.append(item)
    prune_ttys({t["id"] for t in out})
    return tree["version"], out


def find(term_id: str) -> dict:
    for t in flat_terminals()[1]:
        if t["id"] == term_id:
            return t
    raise GhosttyError(f"terminal not found: {term_id}")


def read_lines(term_id: str) -> list:
    """The terminal's whole buffer (history + visible screen; just the screen for
    full-screen programs such as Claude Code or Codex), trailing blanks removed."""
    with locked("clipboard"):  # every instance borrows the same clipboard
        path = Path(call("dump", id=term_id))
    try:
        text = path.read_text(errors="replace")
    finally:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def tail(lines: list, n: int) -> str:
    return "\n".join(lines[-n:] if n > 0 else lines)


SEARCH_TAIL = 60  # lines a pattern is looked for in, unless only new matches count


def count_matches(lines: list, rx) -> int:
    return sum(1 for _ in rx.finditer("\n".join(lines)))


def wait_screen(term_id, pattern=None, idle=None, timeout=30.0, after=None):
    """Poll the screen until `pattern` matches, or until the non-empty screen has been
    unchanged for `idle` seconds (when given), or `timeout` passes.
    With `after` (the pattern's match count in the whole buffer before the wait) only further
    matches count; otherwise the pattern is looked for in the last SEARCH_TAIL lines.
    Returns (lines, reason)."""
    rx = re.compile(pattern, re.MULTILINE) if pattern else None
    deadline = time.monotonic() + timeout
    last, stable_since = None, time.monotonic()
    while True:
        lines = read_lines(term_id)
        now = time.monotonic()
        if lines != last:
            last, stable_since = lines, now
        if rx and (count_matches(lines, rx) > after if after is not None
                   else rx.search("\n".join(lines[-SEARCH_TAIL:]))):
            return lines, "pattern"
        if idle is not None and lines and now - stable_since >= idle:
            return lines, "idle"
        if now >= deadline:
            return lines, "timeout"
        time.sleep(0.4)


# --- input -------------------------------------------------------------------

def zig_escape(text: str) -> str:
    """Escape text for Ghostty's `text:` action, which parses a Zig string literal."""
    out = []
    for ch in text:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\x{o:02x}")
        elif o > 0x7E:
            out.append(f"\\u{{{o:x}}}")
        else:
            out.append(ch)
    return "".join(out)


def type_raw(term_id: str, text: str):
    """Write bytes to the pty as if typed: no bracketed paste, control bytes kept."""
    if not call("action", id=term_id, action="text:" + zig_escape(text)):
        raise GhosttyError("Ghostty rejected the text action")


def press_enter(term_id: str):
    call("keys", id=term_id, keys=[{"key": "enter"}])


KEY_NAMES = {
    "enter": "enter", "return": "enter", "cr": "enter",
    "esc": "escape", "escape": "escape", "tab": "tab",
    "backspace": "backspace", "bs": "backspace", "delete": "delete", "del": "delete",
    "insert": "insert", "ins": "insert", "home": "home", "end": "end",
    "pageup": "pageUp", "pgup": "pageUp", "pagedown": "pageDown", "pgdn": "pageDown",
    "up": "arrowUp", "down": "arrowDown", "left": "arrowLeft", "right": "arrowRight",
    "space": "space",
    **{f"f{i}": f"f{i}" for i in range(1, 26)},
}
MODIFIERS = {
    "ctrl": "control", "control": "control", "shift": "shift",
    "alt": "option", "opt": "option", "option": "option", "meta": "option",
    "cmd": "command", "command": "command", "super": "command",
}
CTRL_BYTES = {"[": "\x1b", "\\": "\x1c", "]": "\x1d", "^": "\x1e", "_": "\x1f", "space": "\x00", "@": "\x00"}


def parse_key(spec: str):
    """'ctrl+c' -> ('raw', '\\x03'); 'shift+tab' -> ('key', 'tab', 'shift'); 'y' -> ('raw', 'y')."""
    if spec == "+":
        return ("raw", "+")
    if spec.endswith("++"):
        head, key = spec[:-2], "+"
    else:
        head, _, key = spec.rpartition("+")
    names = [m.strip().lower() for m in head.split("+")] if head else []
    unknown = [m for m in names if m not in MODIFIERS]
    if unknown or not key:
        raise GhosttyError(f"cannot parse key {spec!r}")
    mods = sorted({MODIFIERS[m] for m in names})
    low = key.lower()
    # Synthetic key events carry no text, so Ctrl/Alt/Shift + character and plain
    # characters are written as the bytes a real keyboard would send.
    if mods == ["control"]:
        if len(key) == 1 and key.isalpha():
            return ("raw", chr(ord(low) - 96))
        if low in CTRL_BYTES:
            return ("raw", CTRL_BYTES[low])
    if mods == ["option"] and len(key) == 1:
        return ("raw", "\x1b" + key)
    if mods == ["shift"] and len(key) == 1:
        return ("raw", key.upper())
    if not mods and (len(key) == 1 or low == "space"):
        return ("raw", " " if low == "space" else key)
    name = KEY_NAMES.get(low)
    if name is None and len(key) == 1 and key.isalnum():
        name = f"digit{key}" if key.isdigit() else low
    if name is None:
        name = key  # allow Ghostty's own names, e.g. numpadEnter
    return ("key", name, ",".join(mods))


def send_key_specs(term_id: str, specs: list, delay_ms: int = 60):
    for i, spec in enumerate(specs):
        if i and delay_ms:
            time.sleep(delay_ms / 1000)
        parsed = parse_key(spec)
        if parsed[0] == "raw":
            type_raw(term_id, parsed[1])
        else:
            key = {"key": parsed[1]}
            if parsed[2]:
                key["modifiers"] = parsed[2]
            call("keys", id=term_id, keys=[key])


# --- server ------------------------------------------------------------------

READ_ONLY = ToolAnnotations(read_only_hint=True)

mcp = MCPServer(
    "ghostty",
    instructions=(
        "Controls the user's Ghostty terminals on macOS. Terminals are addressed by the id from "
        "list_terminals or from the tool that created them. Typical flow: list_terminals or "
        "open_terminal, then send_text / send_keys, then wait_for or read_screen to see the "
        "result, then close_terminal. Text sent to a terminal runs with the user's privileges, "
        "so only send what the user asked for. Foreground process info (zsh, claude, codex...) "
        "is available for terminals this server opened (or every terminal on Ghostty 1.4+).\n"
        "Agent CLIs such as Claude Code or Codex: start one with send_text(text=\"codex\", "
        "wait_idle=3) at a shell prompt; a first message can go on the command line, single-quoted "
        "for the shell (codex 'fix the tests'). Quit with send_text(\"/quit\") (both accept it) or "
        "send_keys([\"ctrl+c\", \"ctrl+c\"]) (both need two quick presses; the first only "
        "interrupts a running turn, so repeat if needed); then read_screen shows the shell back "
        "in the foreground. Type into their prompt with send_text and read answers with "
        "wait_for(idle=...)."
    ),
)


@mcp.tool(annotations=READ_ONLY)
def list_terminals() -> dict:
    """List every Ghostty terminal (window > tab > split) with id, title, cwd, which tab and
    window it is in, and, when known, its tty and foreground process (e.g. zsh, claude, codex)."""
    version, terms = flat_terminals()
    return {"ghostty_version": version, "terminals": terms}


@mcp.tool()
def open_terminal(
    where: Literal["tab", "split", "window"] = "tab",
    target: str | None = None,
    direction: Literal["right", "left", "down", "up"] = "right",
    cwd: str | None = None,
    command: str | None = None,
    env: dict[str, str] | None = None,
    title: str | None = None,
    wait_after_command: bool = False,
    font_size: float | None = None,
) -> dict:
    """Open a new terminal and return it; without `command`, once the shell has drawn its
    prompt, ready for send_text.

    where: "tab" (in the window of `target`, else the front window), "split" (splits `target`,
    else the focused terminal, toward `direction`) or "window".
    command: run this instead of the login shell; the terminal closes when it exits unless
    wait_after_command is true. Without it you get the user's normal shell.
    """
    config = {"cwd": os.path.expanduser(cwd) if cwd else None, "command": command,
              "env": [f"{k}={v}" for k, v in (env or {}).items()], "fontSize": font_size}
    if wait_after_command:
        config["waitAfterCommand"] = True
    before = ghostty_ttys()
    term = call("open", where=where, target=target, direction=direction,
                config={k: v for k, v in config.items() if v})
    tty = tty_of(term, {}) or capture_new_tty(term["id"], before)
    if title:
        set_title(term["id"], title, "tab" if where != "split" else "terminal")
    if not command:  # a command may legitimately print nothing
        wait_screen(term["id"], idle=0.5, timeout=8)
    result = {"id": term["id"], "tty": tty}
    if tty:
        result["foreground"] = foregrounds().get(tty)
    return result


@mcp.tool()
def close_terminal(terminal_id: str, scope: Literal["terminal", "tab", "window"] = "terminal") -> str:
    """Close a terminal (just that split), its whole tab, or its whole window. Whatever runs
    inside is killed without asking; quit an agent first (e.g. send_text "/quit") to end it
    cleanly."""
    call("close", id=terminal_id, scope=scope)
    return f"closed {scope} of {terminal_id}"


@mcp.tool()
def focus_terminal(terminal_id: str) -> str:
    """Focus a terminal and bring its window to the front."""
    call("focus", id=terminal_id)
    return f"focused {terminal_id}"


@mcp.tool()
def set_title(terminal_id: str, title: str, scope: Literal["tab", "terminal"] = "tab") -> str:
    """Set the tab title (stays even when the program changes its own title) or the split's
    own title. An empty title clears the override."""
    action = "set_tab_title" if scope == "tab" else "set_surface_title"
    call("action", id=terminal_id, action=f"{action}:{title}")
    return f"{scope} title set"


@mcp.tool()
def send_text(
    terminal_id: str,
    text: str,
    submit: bool = True,
    mode: Literal["paste", "type"] = "paste",
    wait_for: str | None = None,
    wait_idle: float | None = None,
    timeout: float = 30,
    last_lines: int = 40,
) -> dict:
    """Write text into a terminal and, with submit, press Enter (e.g. text="codex").

    mode "paste" sends it like a paste (bracketed paste: safe for multi-line text in shells
    and TUIs); "type" writes it as keystrokes, for programs that treat pastes specially.
    To see the outcome, pass wait_for (a regex; only matches that appear after sending count)
    and/or wait_idle (seconds without screen changes); the screen tail is then returned.
    """
    after = count_matches(read_lines(terminal_id), re.compile(wait_for, re.MULTILINE)) if wait_for else None
    if mode == "paste":
        call("input", id=terminal_id, text=text)
    else:
        type_raw(terminal_id, text)
    if submit:
        time.sleep(0.15)
        press_enter(terminal_id)
    result = {"sent": True}
    if wait_for or wait_idle:
        lines, reason = wait_screen(terminal_id, wait_for, wait_idle, timeout, after=after)
        result.update(waited=reason, screen=tail(lines, last_lines))
    return result


@mcp.tool()
def send_keys(terminal_id: str, keys: list[str], delay_ms: int = 60) -> str:
    """Press keys in order. Examples: "enter", "esc", "tab", "shift+tab", "up", "down",
    "pageup", "f5", "ctrl+c", "ctrl+d", "ctrl+r", "alt+b", "y", "1".
    ctrl+<letter> and single characters are sent as the exact bytes a keyboard produces, so
    "ctrl+c" interrupts any program (Claude Code and Codex need it twice to quit)."""
    send_key_specs(terminal_id, keys, delay_ms)
    return f"sent {len(keys)} key(s)"


@mcp.tool(annotations=READ_ONLY)
def read_screen(terminal_id: str, last_lines: int = 60) -> dict:
    """Read what a terminal shows. The buffer includes the shell's history above the visible
    screen (full-screen programs like Claude Code or Codex give just their screen);
    last_lines keeps the end of it (0 = everything). Also returns the title and the
    foreground process when known."""
    lines = read_lines(terminal_id)
    term = find(terminal_id)
    return {"title": term["title"], "cwd": term["cwd"], "foreground": term.get("foreground"),
            "total_lines": len(lines), "screen": tail(lines, last_lines)}


@mcp.tool(annotations=READ_ONLY)
def wait_for(
    terminal_id: str,
    pattern: str | None = None,
    idle: float | None = None,
    new_only: bool = False,
    timeout: float = 60,
    last_lines: int = 40,
) -> dict:
    """Wait until a regex `pattern` is in the last 60 lines of the screen (with new_only, until
    it appears again beyond what is there now), or until the screen has stopped changing for
    `idle` seconds (e.g. an agent finished answering), or `timeout` seconds pass.
    Returns why it stopped ("pattern", "idle", "timeout") and the screen tail."""
    if not pattern and idle is None:
        idle = 3.0
    after = None
    if pattern and new_only:
        after = count_matches(read_lines(terminal_id), re.compile(pattern, re.MULTILINE))
    lines, reason = wait_screen(terminal_id, pattern, idle, timeout, after=after)
    return {"waited": reason, "screen": tail(lines, last_lines)}


@mcp.tool()
def ghostty_action(terminal_id: str, action: str) -> dict:
    """Run any Ghostty keybind action on a terminal, e.g. "toggle_split_zoom",
    "resize_split:right,40", "equalize_splits", "goto_split:next", "increase_font_size:2",
    "reset_font_size", "clear_screen", "scroll_to_bottom", "toggle_fullscreen",
    "move_tab:1", "toggle_tab_overview". Full list: `ghostty +list-actions`."""
    return {"performed": bool(call("action", id=terminal_id, action=action))}


if __name__ == "__main__":
    mcp.run()
