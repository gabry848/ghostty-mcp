# ghostty-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents (Claude Code, Codex, Claude Desktop, or any MCP client) drive your [Ghostty](https://ghostty.org) terminals. Agents can open, close and rearrange terminals, type into them and read what they show. With the same tools they can also start and stop other agent sessions, such as Claude Code or Codex, in their own tabs and splits.

It talks to Ghostty through Ghostty's native AppleScript dictionary. No plugin and no Ghostty config change are needed.

**Requirements:**
- macOS
- Ghostty 1.3 or later
- [uv](https://docs.astral.sh/uv/)

Tested with Ghostty 1.3.1, Claude Code 2.1.311 and Codex 0.164.0.

## Installation

```sh
git clone https://github.com/Gabry848/ghostty-mcp.git
cd ghostty-mcp
```

Then register the server with your client. `uv` installs the Python dependencies on first launch.

**Claude Code** (available in every project):

```sh
claude mcp add --scope user ghostty -- uv run --script "$PWD/server.py"
```

**Codex:**

```sh
codex mcp add ghostty -- uv run --script "$PWD/server.py"
```

**Claude Desktop and other JSON-configured clients:**

```json
{
  "mcpServers": {
    "ghostty": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--script", "/path/to/ghostty-mcp/server.py"]
    }
  }
}
```

GUI apps don't see your shell's `PATH`, so give the full path to `uv`; `which uv` prints it.

### Permissions

If the client runs inside Ghostty, as Claude Code or Codex usually do, nothing needs to be granted. If it runs in another app, such as Claude Desktop, macOS asks once for permission to let it control Ghostty. You can change that later in System Settings → Privacy & Security → Automation.

## Tools

| Tool | What it does |
|---|---|
| `list_terminals` | Lists every window, tab and split with its id, title and working directory. Terminals this server opened also show their tty and foreground process (`zsh`, `claude`, `codex`…) |
| `open_terminal` | Opens a new tab, split or window, with optional working directory, command, environment variables and title. It returns once the shell prompt is ready |
| `close_terminal` | Closes a split, a whole tab or a whole window |
| `focus_terminal` | Focuses a terminal and brings its window to the front |
| `set_title` | Sets the tab title or the split's own title |
| `send_text` | Writes text, pasted or typed as keystrokes, and optionally presses Enter (`submit=false` leaves it unsent). It can then wait for a regex or for the screen to settle and return the output |
| `send_keys` | Presses keys in order: `enter`, `esc`, `tab`, `shift+tab`, `up`, `pageup`, `f5`, `ctrl+c`, `ctrl+d`, `alt+b`, `y`… |
| `read_screen` | Returns the end of the terminal's contents, together with its title and foreground process |
| `wait_for` | Waits until a regex appears, or until the screen stops changing, or a timeout passes |
| `ghostty_action` | Runs any [Ghostty action](https://ghostty.org/docs/config/keybind/reference), e.g. `toggle_split_zoom`, `resize_split:right,40`, `equalize_splits`, `increase_font_size:2`, `clear_screen` |

`list_terminals`, `read_screen` and `wait_for` are marked read-only, so clients can approve them automatically.

### Driving Claude Code and Codex

There are no agent-specific tools. The server's instructions tell the calling agent how to do it with the tools above:

- **Start a session:** at a shell prompt, call `send_text(text="codex", wait_idle=3)`. A first message can go on the command line, quoted for the shell: `codex 'fix the failing tests'`.
- **Talk to it:** `send_text` types into the agent's prompt, and `wait_for(idle=…)` waits for the answer.
- **Quit:** both agents accept `send_text("/quit")`. Alternatively, `send_keys(["ctrl+c", "ctrl+c"])` works for both. They need two quick presses, and the first one only interrupts a running turn.
- **Check:** `read_screen` shows the shell back in `foreground`.

Example requests:

- *"Open a split on the right, start Codex in `~/code/api` and ask it to summarise the README"*
- *"In the ‘build’ tab run `npm test` and tell me whether it passes"*
- *"Quit the Claude session in the left split"*

## How it works

- **`ghostty.js`** is a JXA bridge (`osascript -l JavaScript`) to Ghostty's AppleScript dictionary. It takes one JSON request and returns JSON.
- **`server.py`** is the MCP server (Python, `mcp` SDK 2.x, a single [inline-metadata script](https://docs.astral.sh/uv/guides/scripts/)). It handles key encoding, screen reading, waiting and tty tracking.

A few things that are not obvious:

- **Reading the screen.** Ghostty has no command to read a terminal's contents. The server runs Ghostty's `write_screen_file:copy,plain` action instead. That action writes the buffer to a temp file and puts the file's path on the clipboard. The server first saves every clipboard item of every type (text, images, files). It then reads the path, puts your clipboard back exactly as it was and deletes the file. A lock stops concurrent server instances from colliding.
- **Keys.** Ghostty's synthetic key events carry no text, and Claude Code ignores a Ctrl-C sent as a key event. So Ctrl+letter, Alt+character and plain characters are written as the exact bytes a keyboard would send, through Ghostty's `text:` action. Enter, arrows, Tab, Esc and the like go through as key events, so Ghostty encodes them for whatever mode the program is in.
- **Foreground process.** Ghostty 1.3 doesn't expose a terminal's tty. When the server opens a terminal, it records the tty that appears, in `~/.local/state/ghostty-mcp/ttys.json`, and reads the foreground process group with `ps`. Terminals you opened by hand don't have this information. Ghostty 1.4 is expected to add a `tty` property, and the server already uses it when it is present.
- **Waiting.** With `send_text(wait_for=…)`, only matches that appear after the text was sent count, so old output can't cause a false positive.

## Caveats

- **Security.** Text sent to a terminal runs with your privileges, outside the permission checks that Claude Code applies to its own Bash tool. Keep `send_text` and `send_keys` on manual approval unless you trust the context.
- **Focus.** Ghostty brings new windows, tabs and splits to the front.
- **Clipboard managers.** Clipboard managers such as Raycast or Paste may record the temp-file paths that pass through the clipboard during screen reads.
- **Speed.** Each call starts `osascript`, so a screen read takes about 0.3–0.5 s. Waits re-read the screen every 0.4 s.
- **API stability.** Ghostty's AppleScript API is new, and its developers have said that 1.4 may change it in incompatible ways.
- **AppleScript must be on.** The server does not work if Ghostty's config has `macos-applescript = false`.

## License

[MIT](LICENSE)
