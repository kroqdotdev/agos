# agentd

agentd is the computer-use control daemon of agos. It drives one X11 display
(screenshots, mouse, keyboard, windows, clipboard, accessibility tree) and
exposes it to agents over REST, MCP (stdio and Streamable HTTP) and native
Anthropic / OpenAI / Gemini tool-call adapters. It adds what a bare
computer-use tool lacks: a coordinate contract with a stale-frame guard,
per-session state, a global input lock, a human takeover lease, scoped tokens
and a JSONL audit log.

The contract with the rest of agos is the "agentd" section of
[`docs/spec.md`](../docs/spec.md). This file is the full reference.

- [Run it locally](#run-it-locally)
- [Configuration](#configuration)
- [Auth and tokens](#auth-and-tokens)
- [Coordinates, sessions and stale frames](#coordinates-sessions-and-stale-frames)
- [Canonical actions](#canonical-actions)
- [REST API](#rest-api)
- [Takeover lease](#takeover-lease)
- [MCP](#mcp)
- [Provider adapters](#provider-adapters) (verified shapes and sources)
- [MCP SDK choice](#mcp-sdk-choice-v2)
- [Accessibility tree](#accessibility-tree)
- [Audit log](#audit-log)
- [systemd](#systemd)
- [Tests](#tests)
- [Limitations](#limitations)

## Run it locally

Requirements on the host: `xdotool`, `xclip`, `xrandr` (x11-xserver-utils),
an X server. Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
cd agentd
uv sync
uv run pytest                     # starts its own Xvfb on :90-:99

# A private display to play with (never point experiments at a live desktop):
Xvfb :93 -screen 0 1280x800x24 -nolisten tcp &
mkdir -p /tmp/agentd && cat > /tmp/agentd/config.toml <<'EOF'
display = ":93"
listen = "127.0.0.1:18799"
socket = "/tmp/agentd/agentd.sock"
tokens_file = "/tmp/agentd/tokens.toml"
audit_log = "/tmp/agentd/audit.jsonl"
EOF
TOKEN=$(uv run agentd --config /tmp/agentd/config.toml token create --scopes admin --name me)
uv run agentd --config /tmp/agentd/config.toml serve &
curl -s -H "Authorization: Bearer $TOKEN" -X POST localhost:18799/v1/screenshot -d '{}' | head -c 300
uv run agentd --config /tmp/agentd/config.toml status
```

Open `http://127.0.0.1:18799/ui`, paste the token once (kept in the
browser's localStorage) and use Take over / Hand back.

In the image, agentd is installed with
`python3 -m venv --system-site-packages /opt/agentd && /opt/agentd/bin/pip install ./agentd`
(the build backend is hatchling) and runs as the `agent` user's
`agentd.service`.

### Use it as Claude Code's `desktop` server on any X11 desktop

`agentd mcp --mode local` drives an existing X display directly, without the
server, so it also works outside agos. It is a drop-in for single-tool
`computer` MCP servers in the style of Anthropic's reference implementation:
same tool name, same action enum and arguments, same "coordinates are pixels
of the last screenshot" model, plus a resolution guard. Add it to Claude Code
with (adjust the checkout path and display):

```bash
claude mcp add --scope user desktop -- \
  uv run --quiet --project ~/code/agos/agentd agentd --display :1 mcp --mode local
```

Differences you will notice compared with the old server:

- Images follow the current limits (2576 px long edge, 3.75 MP): a 3440x1440
  screen is sent as 2576x1078 instead of 1568x656. Set
  `AGENTD_MAX_IMAGE_LONG_EDGE=1568 AGENTD_MAX_IMAGE_PIXELS=1150000` in `env`
  for the old size.
- After an action it waits until the screen is quiet (300 ms, max 3 s)
  instead of a fixed 0.5 s.
- `key` takes `repeat`; `wait`/`hold_key` allow 300 s; drag honours `text`
  modifiers; zoom output is scaled to the screenshot size.
- The tool no longer blocks the event loop, a failed modifier no longer stays
  pressed, and `cursor_position` before the first screenshot explains itself.
- Audit lines go to `~/.local/state/agentd/audit.jsonl`.

Verified: Claude Code 2.1.289 connects to `agentd mcp` (it negotiates protocol
`2025-11-25` through `initialize`), checked with an isolated
`CLAUDE_CONFIG_DIR` and a private Xvfb.

## Configuration

`~/.config/agentd/config.toml` (or `--config`, or `$AGENTD_CONFIG`).
Precedence: defaults < file < `AGENTD_<KEY>` environment < CLI flags
(`--display`, `--socket`, `serve --listen`, `mcp --mode`). Lists accept JSON
(`AGENTD_ON_TAKEOVER='["/bin/hook","control"]'`) or shell words.

| Key | Default | Meaning |
|---|---|---|
| `display` | `":1"` | X display to drive |
| `listen` | `"127.0.0.1:8765"` | TCP `host:port`; `""` disables TCP |
| `socket` | `$XDG_RUNTIME_DIR/agentd.sock` | Unix socket; `""` disables it |
| `tokens_file` | `~/.config/agentd/tokens.toml` | hashed bearer tokens |
| `audit_log` | `~/.local/state/agentd/audit.jsonl` | JSONL audit; `""` keeps it in memory only |
| `audit_max_bytes` / `audit_backups` | 10 MiB / 5 | size rotation (`.1` … `.5`) |
| `audit_text` | `true` | log typed text (first 200 chars); `false` logs only its length |
| `max_image_long_edge` | 2576 | screenshot long-edge limit |
| `max_image_pixels` | 3750000 | pixel budget, enforced on the 28-px padded area (≤ 4783 Anthropic visual tokens) |
| `default_format` | `"png"` | `png`, `jpeg` or `webp` |
| `jpeg_quality` / `webp_quality` | 85 / 85 | lossy quality |
| `draw_cursor` | `false` | draw the pointer into screenshots (XFixes) |
| `settle_ms` / `settle_timeout_ms` | 300 / 3000 | quiet period and cap for `wait_for_stable` |
| `stable_change_fraction` | 0.0005 | share of 160x100 thumbnail cells that may change and still count as quiet |
| `on_takeover` / `on_handback` | `[]` | hook argv run on lease changes |
| `viewer_url` | `""` | viewer link shown in `/ui` |
| `mcp_mode` | `"auto"` | `agentd mcp` mode (the image should use `"remote"`) |
| `browser_command` | `["agos-browser"]` | used by Gemini `open_web_browser` when no browser window exists |
| `search_url` | `"https://www.google.com/"` | Gemini `search` |
| `cdp_url` | `"http://127.0.0.1:9222"` | DevTools endpoint used to report the current URL to Gemini |
| `exec_timeout_max` | 600 | cap for `/exec` timeouts (s) |
| `a11y_max_nodes` / `a11y_max_depth` | 400 / 12 | default AT-SPI walk limits |

## Auth and tokens

| Transport | Rule |
|---|---|
| TCP (`listen`) | `Authorization: Bearer <token>` on every request, including from loopback (`tailscale serve` proxies from localhost) |
| Unix socket | the peer's uid (`SO_PEERCRED`) must equal agentd's uid; the socket file is created 0600; requests run as an admin principal named `unix:uid=N` |
| MCP stdio | trusted (the agent spawned the process) |

Scopes: `observe`, `input`, `exec`, `files`, `takeover`, `admin` (implies
all). `tokens.toml` is mode 0600 and holds only SHA-256 hashes:

```toml
[[token]]
name = "ci"
sha256 = "5f2b…"
scopes = ["observe", "input"]
created = "2026-10-05T10:55:34Z"
```

```sh
agentd token create --scopes observe,input --name ci   # prints the token once
echo "$AGENTD_TOKEN" | agentd token create --scopes admin --name admin --stdin
agentd token list
agentd token revoke ci
```

The server re-reads the file when it changes. `AGENTD_TOKEN` in the server's
environment is additionally accepted as an admin token and never written to
disk. Failed authentications are audited.

## Coordinates, sessions and stale frames

Every screenshot is downscaled (never upscaled) to fit both image limits and
returns metadata:

```json
{"session":"default","frame_id":4182,"epoch":3,"screen":[1280,800],
 "image":[1280,800],"scale":1.0,"coord_space":"image","cursor":[640,412],
 "format":"png","mime_type":"image/png","data":"<base64>"}
```

- **Sessions** hold their own coordinate basis (the last screenshots they
  took). `default` exists implicitly; `POST /v1/sessions` mints `sess_…`; any
  id matching `[A-Za-z0-9_.:@-]{1,96}` is created on first use (LRU-capped at
  256).
- **`coord_space`**: `image` (default; pixels of the session's last
  screenshot), `screen` (physical pixels) or `normalized` (0–999 grid,
  mapped with `int(v / 1000 * size)` like Google's reference code).
- **`frame_id`** is global and increasing. **`epoch`** starts at 1 and is
  bumped by every handback; frames from older epochs are stale.
- **STALE_FRAME**: an input action is rejected, and a fresh screenshot is
  returned (it becomes the session's basis), when
  - it uses `image`/`normalized` coordinates and the session has no
    screenshot yet, or the screen size changed since it;
  - its `expect_frame_id` predates the current epoch or is not one of the
    session's recent frames (with a valid `expect_frame_id`, that frame's
    scale is used);
  - it has no `expect_frame_id`, and the session's last screenshot predates
    the current epoch (a human had control since).
- Image coordinates map to the centre of the image pixel, so
  image → screen → image round-trips exactly when downscaled.

## Canonical actions

```json
{"type":"screenshot","format":"png"}
{"type":"click","x":10,"y":20,"button":"left|right|middle|back|forward","count":1,"modifiers":["ctrl"]}
{"type":"move","x":10,"y":20,"modifiers":[]}
{"type":"mouse_down","button":"left"}          {"type":"mouse_up","button":"left"}
{"type":"drag","path":[[10,20],[300,400]],"button":"left","modifiers":["shift"]}
{"type":"scroll","x":10,"y":20,"dx":0,"dy":3,"modifiers":[]}
{"type":"type","text":"hello"}
{"type":"key","keys":"ctrl+l","repeat":1}
{"type":"key_down","keys":"shift"}             {"type":"key_up","keys":"shift"}
{"type":"hold_key","keys":"shift","duration":1.5}
{"type":"wait","duration":1.0}
{"type":"wait_for_stable","timeout":3.0,"settle_ms":300}
{"type":"zoom","region":[x0,y0,x1,y1],"format":"png"}
{"type":"cursor_position"}
```

All accept `coord_space` and `expect_frame_id`.

- `x`/`y` are optional for `click`, `mouse_down/up` and `scroll` (current
  pointer). `count` is 1–3. A one-point `drag` path starts at the pointer;
  paths accept `[x,y]` or `{"x":..,"y":..}` points and are interpolated so
  drag targets see motion.
- `scroll` `dx`/`dy` are wheel clicks (−100…100; `dy > 0` = down, `dx > 0` =
  right).
- `keys` uses xdotool keysym syntax (`"ctrl+shift+t"`, `"alt+Tab"`,
  sequences separated by spaces: `"ctrl+a BackSpace"`). Provider spellings
  are normalized (`ENTER`→`Return`, `ArrowLeft`→`Left`, `PageDown`→
  `Page_Down`, `Control+A`→`ctrl+a`, `cmd`/`meta`/`win`→`super`). Unknown
  names are rejected with `INVALID_ACTION`, because xdotool silently ignores
  them. `repeat` is 1–100.
- `modifiers`: `ctrl`, `shift`, `alt`, `super` (aliases accepted), as a list
  or `"ctrl+shift"`. They are pressed inside a `try` and released in reverse
  order in `finally`, so a failing keydown or click never leaves a modifier
  stuck. Drags also release the button on failure.
- `wait`/`hold_key` durations are 0–300 s. `hold_key` ends early if a human
  takes over; `type` checks the lease between 50-character chunks.
- `zoom` crops `region` (in `coord_space`) at full resolution and scales it to
  fit the session's screenshot size (upscaling small regions, aspect kept). It
  does not change the basis: coordinates stay in full-screenshot space.
- `wait_for_stable` captures the screen every 50 ms, compares 160x100
  grayscale thumbnails, and returns `{"stable": bool, "waited_ms": n}` once
  no more than `stable_change_fraction` of the cells changed for `settle_ms`
  (a blinking caret does not count), or at `timeout`.

**Batches** (`POST /v1/actions`): the whole list is validated first; then
actions run in order under the global input lock (observation-only batches
skip the lock) and stop at the first failure. With `screenshot_after`
(default `true`) the daemon waits for the screen to settle after input and
attaches a screenshot.

```json
{"session":"default","ok":false,
 "results":[{"index":0,"type":"move","ok":true,"screen_xy":[100,120]},
            {"index":1,"type":"key","ok":false,"error":{"code":"HUMAN_IN_CONTROL","message":"…"}}],
 "skipped":[2,3],
 "error":{"code":"HUMAN_IN_CONTROL","message":"…"},
 "screenshot":{…metadata…,"data":"…"}}
```

Per-step extras: `screen_xy` for pointer actions, `x`/`y`/`coord_space` for
`cursor_position`, `stable`/`waited_ms` for `wait_for_stable`, `screenshot`
for `screenshot`/`zoom`.

## REST API

Prefix `/v1`. Errors are `{"error":{"code","message"}}` with the status from
the table below. Request bodies are JSON (max 16 MiB).

| Code | HTTP | When |
|---|---|---|
| `UNAUTHORIZED` | 401 | no/invalid bearer token on TCP (`WWW-Authenticate: Bearer`) |
| `FORBIDDEN` | 403 | token lacks the scope |
| `NOT_FOUND` | 404 | unknown window id, no matching accessible window |
| `HUMAN_IN_CONTROL` | 409 | input while the takeover lease is held |
| `STALE_FRAME` | 409 | see above; body carries `screenshot` |
| `CONFIRMATION_REQUIRED` | 409 | an OpenAI `pending_safety_checks` / Gemini `require_confirmation` was not acknowledged |
| `INVALID_ACTION` | 400 | malformed request or action |
| `DISPLAY_UNAVAILABLE` | 503 | X server not reachable |
| `A11Y_UNAVAILABLE` | 503 | AT-SPI bindings not importable |
| `INTERNAL` | 500 | xdotool/xclip failure, timeouts, bugs |

| Method & path | Scope | Request → response |
|---|---|---|
| `GET /health` | – | `{"ok":true,"display":":1","version":"0.1.0"}` |
| `GET /status` | observe | `{version, display, display_ok, screen, epoch, frame_id, lease, input_busy, sessions[], session_count, held, uptime_s, viewer_url}` |
| `POST /sessions` | observe | `{}` → `{"session":"sess_…"}` |
| `POST /screenshot` | observe | `{session?, format?}` → metadata + `mime_type` + base64 `data` |
| `POST /actions` | input (observe if no input action) | `{session?, actions, screenshot_after?, format?}` → batch result; HTTP status of the first error |
| `POST /adapters/anthropic` | input | see [Anthropic](#anthropic); `?session=`, `?screenshot_after=false`, `?format=` |
| `POST /adapters/openai` | input | see [OpenAI](#openai) |
| `POST /adapters/gemini` | input | see [Gemini](#gemini) |
| `GET /windows` | observe | `{"windows":[{id, title, class, instance, pid, geometry:[x,y,w,h], active, desktop, hidden}]}` (EWMH `_NET_CLIENT_LIST`, else mapped top-level windows) |
| `POST /windows/{id}/activate` | input | `{"ok":true,"id":…}`; honours the lease and input lock |
| `GET /clipboard` | files | `{"text":"…"}` |
| `PUT /clipboard` | files | `{"text":"…"}` → `{"ok":true,"length":n}` |
| `POST /launch` | exec | `{argv, env?, cwd?}` → `{"pid":…}`; detached (own session, no stdio), `DISPLAY` set |
| `POST /exec` | exec | `{argv \| command, timeout?=30, cwd?, env?, stdin?}` → `{exit_code, stdout, stderr, timed_out, truncated, duration_ms}` (1 MiB per stream; the process group is killed on timeout) |
| `GET /a11y` | observe | `?window=&max_depth=&max_nodes=&coord_space=&session=` → see [Accessibility](#accessibility-tree) |
| `GET /takeover` | observe | `{"held":false,"epoch":1}` or `{held, by, reason, since, principal, expires_in?, epoch}` |
| `POST /takeover` | takeover | `{by?, reason?, ttl?}` → lease (idempotent; a second call renews) |
| `DELETE /takeover` | takeover | `{by?}` → `{"held":false,"epoch":n,"changed":bool}` |
| `POST /display` | admin | `{width, height}` → `{"screen":[w,h]}` via xrandr (`-s`, then `--newmode/--addmode/--output`, then `--fb`) |
| `GET /audit` | takeover | `?limit=50` → `{"entries":[…]}` (in-memory tail) |
| `GET /ui` | – | the control page (static; it calls the API with the pasted token) |
| `/mcp` | any valid token | MCP Streamable HTTP |

The adapter session comes from `?session=` or the `X-Agentd-Session` header,
so provider bodies are forwarded untouched.

## Takeover lease

`POST /v1/takeover`, `agentd takeover [--reason] [--ttl]` or the UI's Take
over button:

1. records the lease (`by`, `reason`, `since`, the token name);
2. releases keys and buttons the agent holds (`key_down`, `mouse_down`),
   interrupts `hold_key` and stops `type` between chunks;
3. runs `on_takeover` with `AGENTD_EVENT=on_takeover`, `AGENTD_LEASE_BY`,
   `AGENTD_LEASE_REASON` and `DISPLAY` (15 s timeout; failures are audited,
   never fatal).

While held, every input action (also via adapters, MCP and window
activation) fails with `HUMAN_IN_CONTROL`; screenshots, zoom, windows,
status, a11y and the clipboard keep working. Handback (`DELETE`,
`agentd handback`, the UI, or `ttl` expiry) clears the lease, bumps the
epoch so every earlier screenshot is stale, and runs `on_handback`.

## MCP

Tools (same scopes as REST):

| Tool | Arguments | Result |
|---|---|---|
| `computer` | `action` (17 Anthropic actions), `coordinate`, `start_coordinate`, `text`, `scroll_direction`, `scroll_amount`, `duration`, `region`, `repeat`, `session` | text + image (every action except `cursor_position` and `zoom` returns a fresh screenshot). STALE_FRAME returns "… was NOT performed …" plus the fresh screenshot (not an error); other failures are `isError` with `CODE: message`. |
| `windows` | `action` = `list` \| `activate`, `id` | `{"windows":[…]}` / `{"ok":true}` |
| `clipboard_get` / `clipboard_set` | – / `text` | `{"text"}` / `{"ok","length"}` |
| `launch` | `argv`, `env?`, `cwd?` | `{"pid"}` |
| `wait_for_stable` | `timeout?`, `settle_ms?` | `{"stable","waited_ms"}` |
| `status` | – | as `GET /v1/status` |
| `a11y_tree` | `window?`, `max_depth?`, `max_nodes?`, `coord_space?`, `session?` | as `GET /v1/a11y` |

Transports:

- **Streamable HTTP** at `/mcp` on the TCP listener and the Unix socket,
  bearer auth like REST. DNS-rebinding Host checks are off because every
  request must carry a token (or arrive over the peer-checked socket) and
  `tailscale serve` forwards the tailnet hostname. Both protocol eras work:
  handshake clients (`initialize`, `Mcp-Session-Id`; e.g. protocol
  `2025-11-25`) and stateless `2026-07-28` clients.
- **stdio**: `agentd mcp`. `--mode remote` forwards every call to the running
  server over the Unix socket (one lease, one input lock, one audit log; it
  retries for 10 s at the first call). `--mode local` drives the display
  in-process (no shared lease). `auto` (default) picks remote when the socket
  exists and answers, else local. **The image should render
  `mcp_mode = "remote"`** so an in-VM agent can never bypass a takeover by
  starting before `agentd serve`.

Default coordinate session for `computer`: `mcp-<Mcp-Session-Id>` for
handshake-era HTTP clients, `tok-<token name>` for stateless HTTP clients, a
fresh `sess_…` per `agentd mcp` process in remote mode, `default` in local
mode. Pass `session` to choose.

## Provider adapters

Each adapter maps a provider's tool call to canonical actions, runs them as
one batch (stop at the first failure), and renders the provider's own result
shape. Request-level problems (bad JSON, auth, an unacknowledged safety
check) return the error JSON. Once actions were attempted, the response is
HTTP 200 with the provider-shaped body, failures are expressed in the
provider's channel, and agentd's code is in `X-Agentd-Error-Code` /
`X-Agentd-Error-Message`. The shapes below were checked against the official
documentation on 2026-10-05.

### Anthropic

Sources:
[computer use tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool),
[tool reference](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-reference),
[Messages API](https://platform.claude.com/docs/en/api/messages/create),
[vision limits](https://platform.claude.com/docs/en/build-with-claude/vision),
reference implementation
[computer.py](https://github.com/anthropics/anthropic-quickstarts/blob/main/computer-use-demo/computer_use_demo/tools/computer.py) /
[loop.py](https://github.com/anthropics/anthropic-quickstarts/blob/main/computer-use-demo/computer_use_demo/loop.py).

- **`computer_toolset_20260801`** (GA, no beta header; Claude Opus 4.8/5/5.5,
  Sonnet 5/5.5, Fable/Mythos 5.x; Claude 5.5+ reject the older versions on the
  Claude API). Declared as `{"type": "computer_toolset_20260801", "configs":
  {"zoom": {"enabled": false}}}`; `name`/`display_*`/`enable_zoom` are
  rejected. Claude emits one `tool_use` per member, named after the action,
  with `"toolset_name": "computer"` and no `action` field:

  ```json
  {"type":"tool_use","id":"toolu_01…","name":"left_click","toolset_name":"computer","input":{"coordinate":[512,742]}}
  {"type":"tool_use","id":"toolu_01…","name":"key","toolset_name":"computer","input":{"text":"Tab","repeat":4}}
  ```

  17 members: `screenshot {}`, `zoom {region}`, `left_click` /
  `right_click` / `middle_click` / `double_click` / `triple_click`
  `{coordinate?, text? modifiers}`, `left_click_drag {start_coordinate,
  coordinate, text?}`, `mouse_move {coordinate}`, `left_mouse_down {}`,
  `left_mouse_up {}`, `cursor_position {}` (text like `X=512, Y=384`),
  `scroll {scroll_direction, scroll_amount, coordinate?, text?}`,
  `type {text}`, `key {text, repeat? 1–100}`, `hold_key {text, duration ≤
  300}`, `wait {duration ≤ 300}`. Results must echo `toolset_name` and may
  contain only text and image blocks. Batch rule: run in order, stop at the
  first failure, answer every later block with `is_error: true` and exactly
  `"Not executed: an earlier computer action in this turn failed."`; an
  extra image may ride on the last result. Coordinates are screenshot
  pixels; zoom output is scaled to fit the usual screenshot size. Images:
  ≤ 2576 px long edge and ≤ 4784 visual tokens (`⌈w/28⌉·⌈h/28⌉`, ≈ 3.75 MP);
  the toolset rejects oversized images instead of downscaling.
- **`computer_20251124` / `computer_20250124`** (beta headers
  `computer-use-2025-11-24` / `computer-use-2025-01-24`): one tool
  `{"type":"computer_20251124","name":"computer","display_width_px":…,
  "display_height_px":…}`; `input` carries `action` plus the same
  parameters (`zoom` needs `enable_zoom`; no `repeat`). The reference
  implementation reads click modifiers from `key` for `20250124`; agentd
  accepts `text` or `key`.

`POST /v1/adapters/anthropic` accepts

1. a bare `input` with `action` → `{"content":[…], "is_error": bool}`;
2. one `tool_use` block (either version) → one `tool_result` block
   (`tool_use_id`, `toolset_name` echoed when present, `content`,
   `is_error` when failed);
3. a list of `tool_use` blocks → a list of `tool_result` blocks with the
   batch rule above (a malformed block fails at its position).

Content: `screenshot`/`zoom` → an image block (`{"type":"image","source":
{"type":"base64","media_type":"image/png","data":…}}`), `cursor_position`
→ `X=…, Y=…`, others → `OK`; failures → `CODE: message`. After input or
waits a fresh screenshot is appended to the last attempted result (disable
with `?screenshot_after=false`); a STALE_FRAME failure carries the refreshed
screenshot.

### OpenAI

Sources:
[computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use),
[integration recipes and key maps](https://developers.openai.com/api/docs/guides/tools-computer-use-integration),
[Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create),
[openai-python types](https://github.com/openai/openai-python/tree/main/src/openai/types/responses)
(`computer_tool.py`, `response_computer_tool_call.py`, `computer_action.py`,
`response_input_param.py`),
[openai-cua-sample-app](https://github.com/openai/openai-cua-sample-app).

- Tool: `{"type": "computer"}` (GA, no display fields; the legacy
  `computer_use_preview` had `display_width`, `display_height`,
  `environment`). The guide names `gpt-6.1-sol` for the computer tool and
  recommends code execution for GPT-6 Astra; the `computer` tool "remains
  supported".
- Call item (GA batches actions):

  ```json
  {"type":"computer_call","id":"cu_…","call_id":"call_002","status":"completed","pending_safety_checks":[],
   "actions":[{"type":"click","button":"left","x":405,"y":157},{"type":"type","text":"penguin"}]}
  ```

  Actions: `click {button: left|right|wheel|back|forward, x, y, keys?}`,
  `double_click {x, y, keys?}`, `drag {path: [{x,y},…], keys?}`,
  `move {x, y, keys?}`, `scroll {x, y, scroll_x, scroll_y, keys?}`,
  `keypress {keys: [...]}`, `type {text}`, `wait {}`, `screenshot {}`.
  `keys` on mouse actions are modifiers held for the action. Key names are
  upper case (`CTRL`, `ENTER`, `ARROWLEFT`, `META`/`CMD`→super, …).
- Output item:

  ```json
  {"type":"computer_call_output","call_id":"call_002",
   "output":{"type":"computer_screenshot","image_url":"data:image/png;base64,…","detail":"original"}}
  ```

  with `acknowledged_safety_checks: [{id, code?, message?}]` when the user
  approved pending checks.

Mapping: `wheel` → middle button; scroll deltas are pixels and become
`max(1, |round(delta / 100)|)` wheel clicks (OpenAI's xdotool recipe);
`wait` sleeps 2 s (the guide's handlers); `keypress` keys are pressed
together. The adapter accepts a `computer_call` (GA `actions` or legacy
`action`), a bare action, `{"actions": [...]}` or a list, and always answers
with a `computer_call_output` carrying the latest screenshot (`detail:
"original"`; the docs use it although the SDK schema does not list it).
If `pending_safety_checks` is non-empty and not every id appears in the
request's `acknowledged_safety_checks`, nothing runs and the response is
`409 CONFIRMATION_REQUIRED`; acknowledged checks are echoed.

### Gemini

Sources:
[Gemini API computer use](https://ai.google.dev/gemini-api/docs/computer-use),
[Vertex AI computer use](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/computer-use),
[REST reference: generateContent](https://ai.google.dev/api/generate-content) and
[Interactions API](https://ai.google.dev/api/interactions-api),
reference implementation
[google-gemini/computer-use-preview](https://github.com/google-gemini/computer-use-preview)
(`agent.py`, `computers/playwright/playwright.py`).

- Tool: `{"computer_use": {"environment": "ENVIRONMENT_BROWSER" | "ENVIRONMENT_DESKTOP" | …,
  "excluded_predefined_functions": [...]}}` (Interactions API:
  `{"type": "computer_use", "environment": "desktop"}`). Coordinates are a
  normalized 1000x1000 grid (0–999); the reference denormalizes with
  `int(x / 1000 * width)`.
- **Legacy function set** (gemini-2.5-computer-use-preview-10-2025,
  gemini-3-flash-preview, gemini-3.1-pro-preview): `open_web_browser`,
  `wait_5_seconds`, `go_back`, `go_forward`, `search`, `navigate {url}`,
  `click_at {x, y}`, `hover_at {x, y}`, `type_text_at {x, y, text,
  press_enter?, clear_before_typing? = true}`, `key_combination {keys:
  "Control+A"}`, `scroll_document {direction}`, `scroll_at {x, y, direction,
  magnitude? = 800}`, `drag_and_drop {x, y, destination_x, destination_y}`.
- **Current function set** (gemini-3.5-flash and later; every call also
  carries `intent`): `click`, `double_click`, `triple_click`,
  `middle_click`, `right_click`, `mouse_down`, `mouse_up`, `move` (all
  `{x, y}`), `type {text, press_enter? = false}`, `drag_and_drop {start_x,
  start_y, end_x, end_y}`, `wait {seconds? = 1}`, `press_key {key}`,
  `key_down {key}`, `key_up {key}`, `hotkey {keys: [...]}`,
  `take_screenshot`, `scroll {x, y, direction, magnitude_in_pixels? = 300}`;
  browser adds `go_back`, `navigate {url}`, `go_forward`; mobile adds
  `open_app`, `list_apps`, `long_press`.
- Response (generateContent): `{"id"?, "name", "response": {"url": …},
  "parts": [{"inlineData": {"mimeType": "image/png", "data": …}}]}`;
  Interactions: `{"type": "function_result", "name", "call_id", "result":
  [{"type": "text", "text": "{\"url\": …}"}, {"type": "image", "data": …,
  "mime_type": "image/png"}]}`.
- Safety: a call whose args contain `"safety_decision": {"decision":
  "require_confirmation", …}` must be answered with
  `"safety_acknowledgement": "true"` in `response` after a human approved.

The adapter accepts `{name, args, id?}`, `{"function_call": …}` /
`{"functionCall": …}`, Interactions `{"type": "function_call", "name",
"arguments", "id"}`, a list of calls, or `{"function_calls": [...],
"safety_acknowledgement": true}`. It answers in the request's style (one
response, or a list for lists). `url` is the most recently focused page of
the Chromium DevTools endpoint (`cdp_url`), or `""`. Calls with an
unacknowledged `require_confirmation` return `409 CONFIRMATION_REQUIRED`
(nothing runs); acknowledged ones echo `"safety_acknowledgement": "true"`.
For a list of calls, the final screenshot is attached to the last attempted
response; failed calls get `response.error`, later ones `"Not executed: …"`.

Mapping notes:

- Magnitudes (`magnitude`, `magnitude_in_pixels`) are read on the 0–999 grid
  along the scroll axis and converted to wheel clicks at 100 px per click
  (the docs describe `magnitude_in_pixels` both as pixels and as 0–999;
  both readings give the same click count at common sizes).
- `drag_and_drop` accepts both argument spellings (the reference code reads
  `x, y, destination_x, destination_y`; the docs list `start_*`/`end_*`).
- `type_text_at` clicks, clears with `ctrl+a` `BackSpace` when
  `clear_before_typing` (default true), types, and presses Return only when
  `press_enter` is true (the reference code defaults it to false).
- `key_combination`/`hotkey`/`press_key` names go through the key alias table
  (`Control`→`ctrl`, `Enter`→`Return`, …); letters in combinations are lower
  case.
- `long_press` = mouse down, wait `seconds`, mouse up. `open_app`,
  `list_apps` are rejected (desktop only).
- **Browser functions without a desktop equivalent are keyboard shortcuts
  sent to the focused browser window**: `navigate` = `ctrl+l`, type the URL,
  `Return`; `search` = the same with `search_url`; `go_back` = `alt+Left`;
  `go_forward` = `alt+Right`; `scroll_document up/down` = `Page_Up` /
  `Page_Down` (left/right scroll at the screen centre). `open_web_browser`
  activates an existing Chromium/Firefox window, or launches
  `browser_command` (needs the `exec` scope) and waits up to 15 s for its
  window.

## MCP SDK choice (v2)

agentd depends on `mcp>=2.3,<3` (2.3.0 on 2026-10-05). Investigated in a
scratch venv before adopting it:

- `FastMCP` is now `mcp.server.mcpserver.MCPServer`; tools are registered the
  same way (`@mcp.tool()`), async tools run on the event loop, and
  `Context.request_context.request` exposes the Starlette request, which is
  how HTTP tool calls see the authenticated principal.
- stdio (`MCPServer.run()`), Streamable HTTP (`streamable_http_app()` /
  `session_manager.handle_request`) and an in-process `mcp.Client(server)`
  for tests are all available.
- Bearer verification exists (`TokenVerifier` + `AuthSettings`), but it is
  built around OAuth resource-server metadata (`issuer_url`,
  `resource_server_url`). agentd uses its own ASGI wrapper instead, so REST
  and MCP share one token store, one scope model and one error format.
- Compatibility: the SDK speaks both protocol eras. It answers `initialize`
  for `2024-11-05` … `2025-11-25` (with `Mcp-Session-Id`) and the stateless
  `2026-07-28` envelope. Tested: an mcp 1.30 client (stdio and HTTP)
  negotiated `2025-11-25`, the v2 client negotiated `2026-07-28`, and
  Claude Code 2.1.289 connected with `2025-11-25`.
- Cost: v2 pulls `httpx2`, `mcp-types`, `opentelemetry-api` and
  `pyjwt[crypto]` (cryptography); all have wheels for amd64/arm64 on
  Python 3.12 and 3.13.

## Accessibility tree

`GET /v1/a11y` / `a11y_tree` uses `gi.repository.Atspi` when importable (the
image venv sees Debian's `python3-gi` + `gir1.2-atspi-2.0`). It walks one
window (title or app-name substring `window`, else the window in the
`active` state), depth-first, skipping nodes that are not `showing`, bounded
by `max_depth`, `max_nodes` and an 8 s budget. Each element:

```json
{"id":3,"parent":1,"depth":2,"role":"push button","name":"Save",
 "states":["enabled","focusable","sensitive","showing","visible"],
 "bbox":[50,70,250,87],"bbox_screen":[100,140,500,174]}
```

`bbox` is `[x0, y0, x1, y1]` in the request's `coord_space` (default the
session's screenshot space; before the first screenshot, the space the next
screenshot will use, with a `note`). Editable text fields also carry `text`
(first 200 characters; never password fields). The response adds `window`,
`app`, `truncated`, `count`, `frame_id`. Without the bindings the call fails
with `503 A11Y_UNAVAILABLE`. Apps must expose AT-SPI (Chromium needs
`--force-renderer-accessibility`, which `agos-browser` passes).

## Audit log

One JSON object per line (`ts` UTC with ms, `event`, …), mode 0600, rotated
by size. Events: `start`, `stop`, `action` (principal, transport, session,
source, sanitized canonical action, `result` = `ok` or error code, `ms`,
`epoch`), `adapter` (unmappable provider calls), `lease` (`takeover`,
`renew`, `handback`), `hook`, `auth` (failed authentication, rejected socket
peers), `window`, `clipboard` (lengths only), `launch`, `exec`, `display`,
`error`.

```json
{"ts":"2026-10-05T10:53:10.130Z","event":"action","principal":"ci","transport":"tcp","session":"default","source":"actions","action":{"type":"move","x":300,"y":310},"result":"STALE_FRAME","message":"no screenshot has been taken in this session yet","ms":13,"epoch":2}
```

## systemd

```ini
[Service]
Type=notify
ExecStart=/opt/agentd/bin/agentd serve
WatchdogSec=30
Restart=always
```

`agentd serve` sends `READY=1` after both listeners are bound, `WATCHDOG=1`
every `WATCHDOG_USEC/2` from the event loop, and `STOPPING=1` on SIGTERM. It
needs no libsystemd. On shutdown it releases held keys/buttons and removes
the socket. A stale socket file is replaced; a live one (another agentd) is
an error.

## Tests

`uv run pytest` (≈ 200 tests, ~12 s). Integration tests start their own
`Xvfb` on a free display in `:90`–`:99` (never `:1`), unset `DISPLAY` and
`AGENTD_*` first, and use a python-xlib helper window
(`tests/xwin_helper.py`) that reports key and button events.

- Unit: geometry and the three coordinate spaces, key normalization, action
  validation, config precedence, token hashing/scopes/reload, audit
  rotation, the stale-frame guard, lease (blocking, epoch bump, TTL, hooks,
  key release, interruption), batches, modifier release on failure, input
  lock, every adapter's mapping and rendering, a11y bound conversion,
  sd_notify, SO_PEERCRED rejection.
- Integration (private Xvfb): screenshot sizes/formats/metadata, click and
  cursor round-trip at scale 0.5, typing into a window, key repeat, modifier
  click/drag/scroll, modifier state after failures, clipboard via xclip,
  window listing (fallback and EWMH), zoom, cursor drawing, resolution
  change → STALE_FRAME, REST endpoints via Starlette's TestClient, the real
  server over TCP + Unix socket, MCP over Streamable HTTP with bearer auth
  and per-tool scopes, MCP stdio (local and forwarding), in-memory MCP, and
  the CLI.

Also checked outside the suite: in a `debian:trixie` container
(Python 3.13, `--system-site-packages` venv, `pip install ./agentd`) the
suite passes and the AT-SPI walk returns the expected elements of a GTK 3
window; `/ui` was exercised in headless Chrome.

## Limitations

- X11 only (the backend interface is display-agnostic; no Wayland backend).
- `wait_for_stable` polls thumbnails; XDamage would be cheaper.
- `launch` starts plain detached processes, not systemd scopes.
- Human input is not detected automatically; takeover is explicit.
- The Gemini adapter's browser functions assume the focused window is a
  browser that understands `ctrl+l` / `alt+Left`.
- Not verified against live provider APIs (no model calls were made); the
  shapes come from the documentation and reference code linked above.
