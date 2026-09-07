#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0", "zeroconf>=0.80.0", "pywebpush>=2.0.0", "py-vapid>=1.9.0"]
# ///
"""herdr-remote relay — polls herdr, accepts push events (HTTP POST + WebSocket + UDP), broadcasts to clients."""
import asyncio, hashlib, hmac, json, logging, os, re, shutil, signal, socket, subprocess, threading, time, typing

try:
    from websockets.asyncio.server import serve
except ImportError:
    from websockets.server import serve
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from logging.handlers import RotatingFileHandler
import sys

try:
    from agent_state import complete_agent_update_message
except ModuleNotFoundError:
    from importlib.util import module_from_spec, spec_from_file_location

    _agent_state_spec = spec_from_file_location(
        "herdr_remote_agent_state",
        os.path.join(os.path.dirname(__file__), "agent_state.py"),
    )
    _agent_state_module = module_from_spec(_agent_state_spec)
    _agent_state_spec.loader.exec_module(_agent_state_module)
    complete_agent_update_message = _agent_state_module.complete_agent_update_message

def _get_log_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/herdr-remote")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
        return os.path.join(base, "herdr-remote", "logs")
    if os.path.isdir("/var/log") and os.access("/var/log", os.W_OK):
        return "/var/log/herdr-remote"
    return os.path.expanduser("~/.local/state/herdr-remote/log")

LOG_DIR = os.environ.get("HERDR_LOG_DIR", _get_log_dir())
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "relay.log")
AUDIT_FILE = os.path.join(LOG_DIR, "audit.log")

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_file_handler.setFormatter(_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

log = logging.getLogger("herdr-relay")
log.setLevel(logging.INFO)
log.addHandler(_file_handler)
log.addHandler(_console_handler)
logging.getLogger("websockets").setLevel(logging.WARNING)

HERDR = (
    os.environ.get("HERDR_BIN")
    or shutil.which("herdr")
    or ("herdr" if sys.platform == "win32" else "/opt/homebrew/bin/herdr")
)
REMOTE_HERDR = os.environ.get("HERDR_REMOTE_BIN", "herdr")
WS_PORT = int(os.environ.get("HERDR_RELAY_PORT", "8375"))
RELAY_HOST = os.environ.get("HERDR_RELAY_HOST", "127.0.0.1")
POLL_INTERVAL = 2
# How long to wait for `herdr agent prompt` before confirming acceptance.
# A working agent can hold the command open longer than a phone is willing
# to wait, which made delivered prompts look like failures.
PROMPT_ACK_GRACE = 3
# Keepalive for the WebSocket. The library defaults (20s interval, 20s timeout)
# assume a server peer: a phone that suspends for a moment — screen off, network
# asleep — stops answering pings and gets torn down. The timeout leaves room for
# a client to miss a few pings and come back, while the interval stays short
# enough to keep a Cloudflare or Tailscale tunnel from idling out.
WS_PING_INTERVAL = int(os.environ.get("HERDR_RELAY_PING_INTERVAL", "20"))
WS_PING_TIMEOUT = int(os.environ.get("HERDR_RELAY_PING_TIMEOUT", "90"))
AUTH_TOKEN = os.environ.get("HERDR_RELAY_TOKEN", "")  # Optional: shared secret for relay auth
AUTH_PROTOCOL = 1
AUTH_TIMEOUT_SECONDS = 5

# VAPID Web Push
VAPID_PUBLIC_KEY = os.environ.get("HERDR_VAPID_PUBLIC", "")
VAPID_PRIVATE_KEY = os.environ.get("HERDR_VAPID_PRIVATE", "")
VAPID_SUBJECT = os.environ.get("HERDR_VAPID_SUBJECT", "mailto:herdr@localhost")
push_subscriptions = []  # list of PushSubscription dicts
PUSH_SUBS_FILE = os.path.join(LOG_DIR, "push_subs.json")

# Status history. The browser only ever saw what happened while it was open,
# which is the opposite of what the log is for: the relay is the process that
# stays awake, so it keeps the record. Metadata only — prompts and pane output
# are the sensitive part and never reach disk here.
TIMELINE_FILE = os.path.join(LOG_DIR, "timeline.jsonl")
TIMELINE_LIMIT = 500
timeline_entries = []

# An empty status map makes every agent look like it just changed, so the first
# pass after startup records the world as it found it and stays quiet.
poll_seeded = False

if RELAY_HOST not in {"127.0.0.1", "localhost", "::1"} and not AUTH_TOKEN:
    raise SystemExit("HERDR_RELAY_TOKEN is required when HERDR_RELAY_HOST binds beyond loopback")

# Remote hosts: comma-separated SSH targets
REMOTES = [r.strip() for r in os.environ.get("HERDR_REMOTES", "").split(",") if r.strip()]

TOOL_OPTIONS = ["yes, single permission", "trust, always allow", "no (tab to edit)"]
SUBAGENT_OPTIONS = ["approve all pending", "configure individually", "exit (cancel subagents)"]
CHROME_RE = re.compile(
    r"^[\s\u2500\u2501\u2550_\u2014\u2502|\u25d4\u25d1\u25d5\u25cf\s]+$"
    r"|Kiro\s[\u00b7\u2022]"
    r"|esc to cancel"
    r"|type to queue"
    r"|^\s*[\u25d4\u25d1\u25d5\u25cf]\s+(Shell|Bash)"
)
QUESTION_OPTION_RE = re.compile(
    r"^(?P<cursor>[\uf054>\u203a\u276f\u25b8\u2192])?\s*"
    r"(?P<marker>[\uf046\uf10c\uf192\uf096\uf14a\u25cb\u25c9\u2610\u2611]|\([ o]\)|\[[ xX]\])\s+"
    r"(?P<label>.+?)\s*$"
)
QUESTION_OTHER = "Other (type your own)"


clients = set()
last_statuses = {}
last_blocked_prompts = {}
event_queue = asyncio.Queue()
pane_remote_map = {}
# Client-facing pane ids are namespaced per local herdr session ("crm:w1:p1"),
# because pane/tab/workspace ids are only unique *within* a session. These maps
# take a namespaced id back to the session that owns it and to the bare id the
# herdr CLI expects.
pane_session_map = {}
pane_herdr_ids = {}
workspace_targets = {}
known_panes = set()
agent_cache = {}
_remote_locks = {}
_remote_locks_guard = threading.Lock()
_warned_state = {}


SAFE_RESPONSES = {
    "y", "n", "a", "yes", "no", "trust",
    "yes, single permission", "trust, always allow", "no (tab to edit)",
    "approve all pending", "configure individually", "exit (cancel subagents)",
}
SAFE_KEYS = {"y", "n", "a", "Enter", "Tab", "Escape", "C-c", "Up", "Down", "Left", "Right", "BSpace"} | {
    str(number) for number in range(10)
}


# --- Audit logging ---
_audit_handler = RotatingFileHandler(AUDIT_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
audit_log = logging.getLogger("herdr-audit")
audit_log.setLevel(logging.INFO)
audit_log.addHandler(_audit_handler)
audit_log.propagate = False


# --- WebSocket Origin Validation (CVE mitigation) ---
# Prevents drive-by attacks from malicious webpages when relay runs without token

def relay_host_is_loopback(host: str) -> bool:
    """Check if host is a loopback address."""
    if not host:
        return False
    host = host.lower()
    return host in {"127.0.0.1", "localhost", "::1", "[::1]"}

def normalized_origin(parsed) -> str:
    """Normalize origin to scheme://host:port for comparison."""
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    # Default ports
    if port is None:
        port = 443 if scheme == "https" else 80
    return f"{scheme}://{host}:{port}"

# The advisory, the tests and the installer all name this
# HERDR_RELAY_TRUSTED_ORIGINS, while the code once read HERDR_TRUSTED_ORIGINS.
# Accept both, so a configuration that follows the documentation is never
# silently ignored (upstream issue #33).
def _configured_origins() -> set:
    raw = (
        os.environ.get("HERDR_RELAY_TRUSTED_ORIGINS")
        or os.environ.get("HERDR_TRUSTED_ORIGINS", "")
    )
    import urllib.parse as urlparse

    origins = set()
    for entry in raw.split(","):
        entry = entry.strip().rstrip("/").lower()
        if not entry:
            continue
        origins.add(entry)
        # https://host and https://host:443 name the same origin; store both so
        # either spelling in the configuration matches either spelling on the wire.
        origins.add(normalized_origin(urlparse.urlsplit(entry)))
    return origins


TRUSTED_ORIGINS = _configured_origins()

if AUTH_TOKEN and not TRUSTED_ORIGINS:
    log.warning("Relay token is enabled without trusted browser origins")


def origin_is_allowed(origin: str) -> bool:
    """Decide whether a browser origin may open a relay WebSocket.

    Native clients — the Telegram bot, the TUI, the menu bar app — send no
    Origin header and answer to the token alone. Browsers always send one, and
    they attach stored credentials to any page that asks, so the allowlist is
    what separates the operator's own dashboard from a site they never chose
    to visit.
    """
    import urllib.parse as urlparse

    if not origin:
        return True

    if origin.strip().lower() == "null":
        return False

    try:
        parsed = urlparse.urlsplit(origin)
    except ValueError:
        return False

    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return True

    if TRUSTED_ORIGINS:
        candidates = {origin.strip().rstrip("/").lower(), normalized_origin(parsed)}
        return bool(candidates & TRUSTED_ORIGINS)

    # With no allowlist configured, a page served by this machine is the only
    # plausible caller. A token is no help here: the browser would attach it for
    # a hostile page just as readily.
    return relay_host_is_loopback(parsed.hostname)


def audit(action: str, ip: str, device: str, pane_id: str, detail: str = ""):
    """Append a write action to the audit log as structured JSONL."""
    import datetime
    entry = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "action": action,
        "paneId": pane_id,
        "ip": ip,
        "device": device,
    }
    if detail:
        entry["detail"] = detail[:120]  # truncate like collie
    audit_log.info(json.dumps(entry, separators=(",", ":")))


# --- Web Push helpers ---
def load_timeline():
    """Repopulate the in-memory log from disk, as a restart does."""
    timeline_entries.clear()
    try:
        with open(TIMELINE_FILE, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    timeline_entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return
    del timeline_entries[:-TIMELINE_LIMIT]


def save_timeline():
    """Rewrite the whole capped log, so the file can never outgrow the cap."""
    import tempfile

    directory = os.path.dirname(TIMELINE_FILE)
    handle, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            for entry in timeline_entries:
                out.write(json.dumps(entry) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, TIMELINE_FILE)
    except Exception:
        os.unlink(temporary)
        raise


def record_status_change(agent, status):
    import datetime

    timeline_entries.append({
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "session": agent.get("session_name", ""),
        "project": agent.get("project", ""),
        "agent": agent.get("agent", ""),
        "pane_id": agent.get("pane_id", ""),
        "status": status,
    })
    del timeline_entries[:-TIMELINE_LIMIT]
    try:
        save_timeline()
    except OSError as error:
        log.warning("Could not write the status log: %s", error)


def _load_push_subs():
    global push_subscriptions
    if os.path.isfile(PUSH_SUBS_FILE):
        try:
            with open(PUSH_SUBS_FILE) as f:
                push_subscriptions = json.load(f)
        except Exception:
            push_subscriptions = []


def _save_push_subs():
    with open(PUSH_SUBS_FILE, "w") as f:
        json.dump(push_subscriptions, f)


async def send_web_push(title: str, body: str, url: str = "/", clear: bool = False,
                        tag: str = "herdr-blocked"):
    """Send push notification to all registered subscriptions.
    
    Uses collapse topic + TTL so offline devices get only the latest.
    If clear=True, sends a clear instruction instead of showing a notification.
    """
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("pywebpush not installed, skipping push")
        return
    if clear:
        payload = json.dumps({"type": "clear", "tag": tag})
    else:
        payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    # The tag is also the collapse key: an agent waiting for an answer and one
    # that has finished are different news and must not overwrite each other.
    headers = {"Topic": tag, "TTL": "21600"}  # 6h TTL
    dead = []
    for i, sub in enumerate(push_subscriptions):
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                headers=headers,
            )
        except Exception as e:
            log.warning("Push failed for sub %d: %s", i, e)
            if "410" in str(e) or "404" in str(e):
                dead.append(i)
    if dead:
        for i in reversed(dead):
            push_subscriptions.pop(i)
        _save_push_subs()

_load_push_subs()
load_timeline()


def _invoke_herdr(*args, remote=None, session=None):
    """Run the herdr CLI, optionally against a named local session.

    `session` is only ever applied to local invocations: remote hosts are polled
    exactly as before, over their own herdr install.
    """
    if remote:
        cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote, REMOTE_HERDR, *args]
        with _remote_locks_guard:
            remote_lock = _remote_locks.get(remote)
            if remote_lock is None:
                remote_lock = threading.Lock()
                _remote_locks[remote] = remote_lock
        with remote_lock:
            return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)

    session_args = ["--session", session] if session else []
    cmd = [HERDR, *session_args, *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)


def run_herdr_result(*args, remote=None, session=None):
    return _invoke_herdr(*args, remote=remote, session=session)


def run_herdr(*args, remote=None, session=None):
    try:
        return _invoke_herdr(*args, remote=remote, session=session).stdout.strip()
    except Exception:
        return ""


def _mutate_herdr(*args, remote=None, session=None):
    try:
        return run_herdr_result(*args, remote=remote, session=session).returncode == 0
    except Exception:
        return False


def herdr_target(remote=None, session=None):
    """Invocation kwargs addressing the host/session that owns a pane.

    `session` is omitted when unset so remote calls, and herdr builds predating
    named sessions, keep their historical argv.
    """
    return {"remote": remote} if session is None else {"remote": remote, "session": session}


def agent_target(agent):
    """Invocation kwargs for the host/session an agent record came from."""
    return herdr_target(agent.get("remote"), agent.get("session_name") or None)


def _warn_change(key, message, *args):
    """Log a warning only when this condition differs from the last poll.

    The poll loop runs every couple of seconds; a persistently broken session
    would otherwise flood the log with the same line.
    """
    rendered = message % args if args else message
    if _warned_state.get(key) == rendered:
        return
    _warned_state[key] = rendered
    log.warning(rendered)


def _clear_warning(key):
    _warned_state.pop(key, None)


class HerdrQueryError(RuntimeError):
    """A herdr query returned something unusable — usually a dead session."""


def namespaced_id(session, value):
    """Scope a herdr id to its local session: ("crm", "w1:p1") -> "crm:w1:p1".

    Ids stay bare when there is no session to scope them to, which covers SSH
    remotes and herdr builds without named-session support.
    """
    if not session or not value:
        return value
    return f"{session}:{value}"


def list_local_sessions():
    """Names of the running local herdr sessions.

    Returns `[None]` — meaning "one poll, no --session flag" — when the session
    query itself fails, so a herdr build predating named sessions keeps working
    exactly as before. An empty list means herdr answered and nothing is running.
    """
    raw = run_herdr("session", "list", "--json")
    try:
        sessions = json.loads(raw)["sessions"]
        if not isinstance(sessions, list):
            raise TypeError("sessions is not a list")
    except (json.JSONDecodeError, KeyError, TypeError, IndexError):
        _warn_change(
            "session-discovery",
            "herdr session list unavailable; falling back to the default session only",
        )
        return [None]
    _clear_warning("session-discovery")
    return [
        entry["name"]
        for entry in sessions
        if isinstance(entry, dict) and entry.get("running") and entry.get("name")
    ]


def get_agents_from_host(remote=None, session=None):
    """Agents on one host/session, with ids namespaced to that session.

    Raises HerdrQueryError when herdr answers with something unparseable, so the
    caller can skip this one source and still publish the others.
    """
    raw = run_herdr("pane", "list", **herdr_target(remote, session))
    host_label = remote or "local"
    try:
        data = json.loads(raw)
        panes = data["result"]["panes"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HerdrQueryError(f"unreadable pane list ({exc})") from exc
    return [
        {
            # Client-facing identity: unique across every session on this relay.
            "pane_id": namespaced_id(session, p["pane_id"]),
            "workspace_id": namespaced_id(session, p.get("workspace_id", "")),
            "tab_id": namespaced_id(session, p.get("tab_id", "")),
            "session_name": session or "",
            # Bare herdr identity: what the CLI is actually given, alongside
            # --session. Clients never send these back.
            "herdr_pane_id": p["pane_id"],
            "herdr_workspace_id": p.get("workspace_id", ""),
            "herdr_tab_id": p.get("tab_id", ""),
            "agent": p.get("agent", ""),
            "label": p.get("label", ""),
            "status": p.get("agent_status", "unknown"),
            "cwd": p.get("cwd", ""),
            "project": os.path.basename(p.get("cwd", "")),
            "host": host_label,
            "remote": remote,
        }
        for p in panes if p.get("agent")
    ]


def get_all_agents():
    """Every agent across all running local sessions plus configured remotes.

    A source that fails is skipped for this cycle rather than sinking the poll.
    """
    agents = []
    for session in list_local_sessions():
        label = f"local session {session!r}" if session else "local herdr"
        try:
            agents.extend(get_agents_from_host(remote=None, session=session))
        except HerdrQueryError as exc:
            _warn_change(f"poll:{session}", "skipping %s this cycle: %s", label, exc)
            continue
        _clear_warning(f"poll:{session}")
    for remote in REMOTES:
        try:
            agents.extend(get_agents_from_host(remote=remote))
        except HerdrQueryError as exc:
            _warn_change(f"poll@{remote}", "skipping remote %r this cycle: %s", remote, exc)
            continue
        _clear_warning(f"poll@{remote}")
    return agents


def update_pane_maps(agents):
    current_pane_ids = {agent["pane_id"] for agent in agents}
    current_workspace_ids = set()
    for agent in agents:
        pane_id = agent["pane_id"]
        session = agent.get("session_name") or None
        pane_remote_map[pane_id] = agent.get("remote")
        pane_session_map[pane_id] = session
        pane_herdr_ids[pane_id] = agent.get("herdr_pane_id", pane_id)
        known_panes.add(pane_id)
        agent_cache[pane_id] = agent
        workspace_id = agent.get("workspace_id")
        if workspace_id:
            current_workspace_ids.add(workspace_id)
            workspace_targets[workspace_id] = {
                "workspace_id": workspace_id,
                "herdr_workspace_id": agent.get("herdr_workspace_id", workspace_id),
                "remote": agent.get("remote"),
                "session": session,
            }

    stale = known_panes - current_pane_ids
    if stale:
        known_panes.difference_update(stale)
        for pane_id in stale:
            pane_remote_map.pop(pane_id, None)
            pane_session_map.pop(pane_id, None)
            pane_herdr_ids.pop(pane_id, None)
            last_statuses.pop(pane_id, None)
            last_blocked_prompts.pop(pane_id, None)
            agent_cache.pop(pane_id, None)
    for workspace_id in set(workspace_targets) - current_workspace_ids:
        workspace_targets.pop(workspace_id, None)


class PaneTarget(typing.NamedTuple):
    """Where a client-supplied pane id actually points."""

    pane_id: str        # canonical client-facing id, e.g. "crm:w1:p1"
    herdr_pane_id: str  # bare id for the CLI, e.g. "w1:p1"
    kwargs: dict        # remote/session kwargs for run_herdr


def resolve_pane(pane_id):
    """Resolve a client-supplied pane id, or None when it addresses nothing.

    Accepts the namespaced id the relay publishes, and — when unambiguous — a
    bare herdr id, which is what saved deep links and plugin push events carry.
    """
    if not pane_id:
        return None
    if pane_id in known_panes or pane_id in pane_herdr_ids:
        return PaneTarget(
            pane_id,
            pane_herdr_ids.get(pane_id, pane_id),
            herdr_target(pane_remote_map.get(pane_id), pane_session_map.get(pane_id)),
        )
    matches = [known for known, bare in pane_herdr_ids.items() if bare == pane_id]
    if len(matches) != 1:
        return None
    return resolve_pane(matches[0])


def resolve_workspace(workspace_id):
    """Resolve a client-supplied workspace id to its owning session."""
    if not workspace_id:
        return None
    entry = workspace_targets.get(workspace_id)
    if entry is None:
        matches = [
            candidate for candidate in workspace_targets.values()
            if candidate["herdr_workspace_id"] == workspace_id
        ]
        # Unknown workspaces keep the historical behaviour: addressed bare,
        # against the default session.
        entry = matches[0] if len(matches) == 1 else {
            "herdr_workspace_id": workspace_id, "remote": None, "session": None,
        }
    return entry


def read_pane(pane_id, remote=None, session=None):
    raw = run_herdr(
        "pane", "read", pane_herdr_ids.get(pane_id, pane_id),
        "--lines", "100", "--source", "recent",
        **herdr_target(remote, session),
    )
    lines = [l for l in raw.splitlines() if l.strip() and not CHROME_RE.search(l)]
    display_lines = lines[-50:]
    question = detect_question("\n".join(lines))
    if question and question["text"] and question["text"] not in display_lines:
        option_start = next(
            (
                index for index in range(len(display_lines) - 1, -1, -1)
                if QUESTION_OPTION_RE.match(display_lines[index].strip().strip("\u2502|").strip())
            ),
            None,
        )
        if option_start is not None:
            while option_start > 0 and QUESTION_OPTION_RE.match(
                display_lines[option_start - 1].strip().strip("\u2502|").strip()
            ):
                option_start -= 1
        else:
            option_start = 0
        display_lines.insert(option_start, question["text"])
    return "\n".join(display_lines)


def detect_question(text):
    blocks = []
    current = []
    current_start = None
    lines = text.splitlines()
    for line_index, raw_line in enumerate(lines):
        line = raw_line.strip().strip("\u2502|").strip()
        match = QUESTION_OPTION_RE.match(line)
        if not match:
            if current:
                blocks.append((current_start, current))
                current = []
                current_start = None
            continue
        if current_start is None:
            current_start = line_index
        marker = match.group("marker")
        current.append({
            "label": match.group("label").strip(),
            "selected": bool(match.group("cursor")),
            "multi": marker in {"\uf046", "\uf096", "\uf14a", "\u2610", "\u2611", "[ ]", "[x]", "[X]"},
            "checked": marker in {"\uf046", "\uf14a", "\u2611", "[x]", "[X]"},
        })
    if current:
        blocks.append((current_start, current))

    for block_start, block in reversed(blocks):
        has_other = any(option["label"] == QUESTION_OTHER for option in block)
        has_done = any("Done selecting" in option["label"] for option in block)
        if has_other or has_done:
            question_lines = []
            for raw_line in reversed(lines[:block_start]):
                line = raw_line.strip().strip("\u2502|").strip()
                if not line:
                    if question_lines:
                        break
                    continue
                if (
                    "submit" in line.casefold()
                    or re.fullmatch(r"[\W_]*ask[\W_]*", line, re.IGNORECASE)
                    or not any(character.isalnum() for character in line)
                ):
                    if question_lines:
                        break
                    continue
                question_lines.append(line)
            question_text = " ".join(reversed(question_lines))
            return {
                "options": block,
                "selected_index": next(
                    (index for index, option in enumerate(block) if option["selected"]),
                    0,
                ),
                "multi": any(option["multi"] for option in block) or has_done,
                "text": question_text,
            }
    return None


def detect_approval_options(text):
    lower = text.lower()
    if "yes, single permission" in lower:
        return TOOL_OPTIONS
    if "approve all pending" in lower:
        return SUBAGENT_OPTIONS
    return []


def detect_options(text):
    approval_options = detect_approval_options(text)
    if approval_options:
        return approval_options
    question = detect_question(text)
    if not question:
        return []
    return [
        option["label"]
        for option in question["options"]
        if option["label"] != QUESTION_OTHER and "Done selecting" not in option["label"]
    ]


def custom_editor_active(text):
    return "Enter your response:" in text or (
        "Custom answer:" in text and "submit" in text.lower()
    )

def question_prompt_id(pane_id, content):
    question = detect_question(content)
    if not question:
        normalized = " ".join(content.split())
        return hashlib.sha256(f"{pane_id}\n{normalized}".encode("utf-8")).hexdigest()[:20]
    labels = [
        option["label"] for option in question["options"]
        if option["label"] != QUESTION_OTHER and "Done selecting" not in option["label"]
    ]
    signature = json.dumps(
        {
            "pane_id": pane_id,
            "question": question["text"],
            "multi": question["multi"],
            "labels": labels,
        },
        sort_keys=True,
    )
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]


def prompt_matches(pane_id, prompt_id, remote=None, session=None):
    if not prompt_id:
        return False
    content = read_pane(pane_id, **herdr_target(remote, session))
    return question_prompt_id(pane_id, content) == prompt_id


def blocked_message(pane_id, agent, project, host, content):
    question = detect_question(content) if agent == "omp" else None
    options = detect_options(content) if agent == "omp" else detect_approval_options(content)
    return {
        "type": "blocked",
        "pane_id": pane_id,
        "agent": agent,
        "project": project,
        "host": host,
        "prompt": content[-500:],
        "prompt_id": question_prompt_id(pane_id, content),
        "options": [] if question and question["multi"] else options,
        "multi_options": options if question and question["multi"] else [],
        "selected_options": [
            option["label"] for option in question["options"]
            if option["multi"] and option["label"] != QUESTION_OTHER
            and "Done selecting" not in option["label"] and option["checked"]
        ] if question else [],
        "interaction": "omp_question" if question else "prompt",
        "multi": bool(question and question["multi"]),
        "update": False,
    }


def pane_is_omp(pane_id, remote=None):
    return any(
        agent["pane_id"] == pane_id and agent["agent"] == "omp" and agent.get("remote") == remote
        for agent in get_all_agents()
    )


def move_question_cursor(pane_id, question, target_index, remote=None, session=None):
    selected_index = question["selected_index"]
    direction = "Down" if target_index >= selected_index else "Up"
    keys = [direction] * abs(target_index - selected_index)
    return not keys or _mutate_herdr(
        "pane", "send-keys", pane_herdr_ids.get(pane_id, pane_id), *keys,
        **herdr_target(remote, session),
    )


def toggle_question_option(pane_id, option_label, remote=None, session=None):
    if not pane_is_omp(pane_id, remote=remote):
        return False
    target = herdr_target(remote, session)
    question = detect_question(read_pane(pane_id, **target))
    if not question or not question["multi"]:
        return False
    target_index = next((
        index
        for index, option in enumerate(question["options"])
        if option["label"].casefold() == option_label.casefold()
    ), None)
    if target_index is None or not move_question_cursor(
        pane_id, question, target_index, **target
    ):
        return False
    return _mutate_herdr(
        "pane", "send-keys", pane_herdr_ids.get(pane_id, pane_id), "Enter", **target
    )


def submit_multi_question(pane_id, remote=None, session=None):
    if not pane_is_omp(pane_id, remote=remote):
        return False
    target = herdr_target(remote, session)
    herdr_pane_id = pane_herdr_ids.get(pane_id, pane_id)
    content = read_pane(pane_id, **target)
    question = detect_question(content)
    if not question or not question["multi"]:
        return False
    done_index = next((
        index
        for index, option in enumerate(question["options"])
        if "Done selecting" in option["label"]
    ), None)
    if done_index is not None:
        if not move_question_cursor(pane_id, question, done_index, **target):
            return False
        return _mutate_herdr("pane", "send-keys", herdr_pane_id, "Enter", **target)
    if "Submit" in content and any(
        marker in content for marker in ("\uf14a", "\uf046", "\u2611", "[x]", "[X]")
    ):
        return _mutate_herdr("pane", "send-keys", herdr_pane_id, "Tab", "Enter", **target)
    return False


def respond_to_question(pane_id, text, question, remote=None, session=None):
    options = question["options"]
    target_index = next(
        (index for index, option in enumerate(options) if option["label"].casefold() == text.casefold()),
        None,
    )
    custom_response = target_index is None
    if custom_response:
        target_index = next(
            (index for index, option in enumerate(options) if option["label"] == QUESTION_OTHER),
            None,
        )
    if target_index is None:
        return False

    target = herdr_target(remote, session)
    herdr_pane_id = pane_herdr_ids.get(pane_id, pane_id)
    selected_index = question["selected_index"]
    direction = "Down" if target_index >= selected_index else "Up"
    keys = [direction] * abs(target_index - selected_index) + ["Enter"]
    if not _mutate_herdr("pane", "send-keys", herdr_pane_id, *keys, **target):
        return False
    if not custom_response:
        return True
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        editor_content = read_pane(pane_id, **target)
        if "Enter your response:" in editor_content or (
            "Custom answer:" in editor_content and "submit" in editor_content.lower()
        ):
            break
        time.sleep(0.05)
    else:
        return False
    return _mutate_herdr(
        "pane", "send-text", herdr_pane_id, text, **target
    ) and _mutate_herdr("pane", "send-keys", herdr_pane_id, "Enter", **target)


async def broadcast(msg):
    data = json.dumps(msg)
    dead = set()
    for ws in list(clients):
        try:
            await ws.send(data)
        except (ConnectionClosedError, ConnectionClosedOK):
            dead.add(ws)
        except Exception:
            dead.add(ws)
    if dead:
        log.debug("Removed %d dead client(s)", len(dead))
    clients.difference_update(dead)

async def send_current_snapshot(ws):
    agents = get_all_agents()
    update_pane_maps(agents)
    await ws.send(json.dumps({"type": "agents", "agents": agents}))
    for agent in agents:
        if agent["status"] != "blocked":
            continue
        content = read_pane(agent["pane_id"], **agent_target(agent))
        await ws.send(json.dumps(blocked_message(
            agent["pane_id"],
            agent["agent"],
            agent["project"],
            agent.get("host", "local"),
            content,
        )))


async def poll_loop():
    while True:
        try:
            await _poll_once()
        except Exception:
            log.exception("poll cycle failed; retrying")
        await asyncio.sleep(POLL_INTERVAL)


async def _poll_once():
        agents = get_all_agents()
        update_pane_maps(agents)
        # Always broadcast (even empty list) so clients stay in sync
        await broadcast({"type": "agents", "agents": agents})
        global poll_seeded
        for a in agents:
            pid, status = a["pane_id"], a["status"]
            previous = last_statuses.get(pid)
            if status == "blocked":
                content = read_pane(pid, **agent_target(a))
                message = blocked_message(
                    pid,
                    a["agent"],
                    a["project"],
                    a.get("host", "local"),
                    content,
                )
                fingerprint = (
                    message["prompt_id"],
                    tuple(message["selected_options"]),
                    message["prompt"],
                )
                previous = last_blocked_prompts.get(pid)
                if previous != fingerprint:
                    message["update"] = previous is not None and previous[0] == message["prompt_id"]
                    last_blocked_prompts[pid] = fingerprint
                    await broadcast(message)
                    await send_web_push(
                        title=f"\U0001f411 {a['project']} blocked",
                        body=content[:120],
                        url=f"/?pane={pid}",
                    )
            else:
                if previous == "blocked":
                    await send_web_push("", "", clear=True, tag="herdr-blocked")
                last_blocked_prompts.pop(pid, None)
                # "done" means finished and not yet looked at: herdr moves the
                # pane back to idle the moment someone opens it, which is also
                # when the notification stops being true.
                if status == "done" and previous != "done" and poll_seeded:
                    await send_web_push(
                        title=f"\u2705 {a['project']} finished",
                        body=f"{a['agent']} is done and waiting to be seen.",
                        url=f"/?pane={pid}",
                        tag="herdr-done",
                    )
                elif previous == "done" and status != "done":
                    await send_web_push("", "", clear=True, tag="herdr-done")
            if poll_seeded and previous != status:
                record_status_change(a, status)
            last_statuses[pid] = status
        poll_seeded = True
async def event_push():
    while True:
        event = await event_queue.get()
        pane_id = event.get("pane_id", "")
        # Plugin push events carry a bare herdr pane id and no session, so map it
        # onto the namespaced id the rest of the relay speaks. Panes never polled,
        # and bare ids two sessions both claim, are left as sent and reconciled by
        # the next poll rather than routed to a guess.
        resolved = resolve_pane(pane_id)
        if resolved is not None and resolved.pane_id != pane_id:
            pane_id = resolved.pane_id
            event = {**event, "pane_id": pane_id}
        elif resolved is None and pane_id and pane_id not in known_panes:
            log.debug("push event for unresolved pane %s; awaiting poll", pane_id)
        update = None
        if pane_id and event.get("type") == "agent_event":
            update = complete_agent_update_message(
                event,
                current=agent_cache.get(pane_id),
                local_hostname=socket.gethostname(),
            )
            if update is None:
                continue
        agent_data = update["agent"] if update else event
        status = agent_data.get("status", "")
        host = agent_data.get("host", "local")
        event_remote = pane_remote_map.get(pane_id)

        if pane_id and event.get("type") == "agent_event":
            agents = get_all_agents()
            if status == "blocked" and not any(
                agent["pane_id"] == pane_id for agent in agents
            ):
                agents.append({
                    "pane_id": pane_id,
                    "herdr_pane_id": pane_herdr_ids.get(pane_id, pane_id),
                    "session_name": pane_session_map.get(pane_id) or "",
                    "agent": agent_data.get("agent", ""),
                    "status": status,
                    "cwd": agent_data.get("cwd", ""),
                    "project": agent_data.get("project", ""),
                    "host": host,
                    "remote": event_remote,
                })
            update_pane_maps(agents)
            await broadcast({"type": "agents", "agents": agents})
            agent_cache[pane_id] = {**agent_cache.get(pane_id, {}), **agent_data}
            if status != "blocked":
                await broadcast(update)

        if status == "blocked" and pane_id:
            remote = pane_remote_map.get(pane_id)
            if remote or host == "local":
                content = read_pane(
                    pane_id, **herdr_target(remote, pane_session_map.get(pane_id))
                )
            else:
                content = event.get("prompt", "Agent is blocked")
            message = blocked_message(
                pane_id,
                agent_data.get("agent", ""),
                agent_data.get("project", ""),
                host,
                content or agent_data.get("prompt", "Agent is blocked"),
            )
            last_blocked_prompts[pane_id] = (
                message["prompt_id"],
                tuple(message["selected_options"]),
                message["prompt"],
            )
            await broadcast(message)


async def process_request(connection, request):
    """Handle HTTP POST on the same port as WebSocket."""
    from websockets.http11 import Response
    from websockets.datastructures import Headers

    # The dashboard shell loads before the browser holds a token, so demanding
    # one here would force the secret into the page URL. Agent state stays
    # behind the WebSocket gate; these files carry none of it.
    static_files = {
        "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json", "no-cache"),
        "/icons/icon-192.png": ("icons/icon-192.png", "image/png", "no-cache"),
        "/icons/icon-512.png": ("icons/icon-512.png", "image/png", "no-cache"),
        "/HackNerdFont-Regular.woff2": ("HackNerdFont-Regular.woff2", "font/woff2", "public, max-age=31536000, immutable"),
        "/HackNerdFont-LICENSE.txt": ("HackNerdFont-LICENSE.txt", "text/plain; charset=utf-8", "public, max-age=31536000, immutable"),
    }
    public_paths = {
        "/", "/index.html", "/security.js",
        "/sw.js", "/logo.svg", "/api/vapid-public-key",
        *static_files,
    }
    # A client that keeps a trailing slash sends "//api/...". It means the same
    # file, so collapse repeats before deciding whether the path is public.
    request_path = re.sub(r"/{2,}", "/", (request.path or "/").split("?", 1)[0]) or "/"

    upgrade = None
    origin = ""
    for key, value in request.headers.raw_items():
        if key.lower() == "upgrade":
            upgrade = value.lower()
        elif key.lower() == "origin":
            origin = value
    if upgrade == "websocket":
        if not origin_is_allowed(origin):
            headers = Headers([("Content-Type", "text/plain")])
            return Response(403, "Forbidden", headers, b"Origin not allowed\n")
        return None

    # Token auth (if configured)
    if AUTH_TOKEN and request_path not in public_paths:
        token = None
        for key, value in request.headers.raw_items():
            if key.lower() == "authorization":
                token = value.replace("Bearer ", "")
        # Also check query param ?token=
        if not token and "token=" in (request.path or ""):
            import urllib.parse
            _, qs = request.path.split("?", 1) if "?" in request.path else (request.path, "")
            params = urllib.parse.parse_qs(qs)
            token = params.get("token", [None])[0]
        if token != AUTH_TOKEN:
            headers = Headers([("Content-Type", "text/plain")])
            return Response(401, "Unauthorized", headers, b"Invalid token\n")

    # For CORS preflight
    if request.path and "OPTIONS" in str(request.headers):
        headers = Headers([
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
        ])
        return Response(204, "No Content", headers, b"")

    # ⚠ EVENT PUSH MUST BE HANDLED FIRST — ORDER IS LOAD-BEARING.
    # A pushed event arrives as `?d=<urlencoded json>` on ANY path.
    # The README shows POST to :8375 without naming a path, so `/` is common.
    # Every static route below `return`s, so if reached first the event is
    # dropped while caller still gets 200. Add new static routes BELOW, never above.
    import urllib.parse as _urlparse
    if "?" in (request.path or ""):
        _, qs = (request.path or "").split("?", 1)
        params = _urlparse.parse_qs(qs)
        if "d" in params:
            try:
                event = json.loads(params["d"][0])  # parse_qs already decodes
                event_queue.put_nowait(event)
                log.debug("push: received event type=%s", event.get("type", "unknown"))
            except Exception as e:
                log.warning("push: unparseable event payload (%d bytes): %s", len(params["d"][0]), e)
            headers = Headers([("Access-Control-Allow-Origin", "*")])
            return Response(200, "OK", headers, b"ok\n")

    # Serve web app for GET / or GET /index.html
    path = request_path
    if path in ("/", "/index.html"):
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        index_path = os.path.join(web_dir, "index.html")
        if os.path.isfile(index_path):
            with open(index_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-cache"),
            ])
            return Response(200, "OK", headers, body)

    # Serve service worker
    if path == "/sw.js":
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        sw_path = os.path.join(web_dir, "sw.js")
        if os.path.isfile(sw_path):
            with open(sw_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "application/javascript"),
                ("Cache-Control", "no-cache"),
                ("Service-Worker-Allowed", "/"),
            ])
            return Response(200, "OK", headers, body)

    # Serve the security helper the dashboard loads before it can connect
    if path == "/security.js":
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        helper_path = os.path.join(web_dir, "security.js")
        if os.path.isfile(helper_path):
            with open(helper_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "application/javascript"),
                ("Cache-Control", "no-cache"),
            ])
            return Response(200, "OK", headers, body)

    # Serve VAPID public key
    if path == "/api/vapid-public-key":
        body = json.dumps({"publicKey": VAPID_PUBLIC_KEY}).encode()
        headers = Headers([
            ("Content-Type", "application/json"),
            ("Access-Control-Allow-Origin", "*"),
        ])
        return Response(200, "OK", headers, body)

    # Serve logo.svg
    if path == "/logo.svg":
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        svg_path = os.path.join(web_dir, "logo.svg")
        if os.path.isfile(svg_path):
            with open(svg_path, "rb") as f:
                body = f.read()
            headers = Headers([("Content-Type", "image/svg+xml")])
            return Response(200, "OK", headers, body)

    if path in static_files:
        filename, content_type, cache_control = static_files[path]
        asset_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "web", filename
        )
        if os.path.isfile(asset_path):
            with open(asset_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", content_type),
                ("Cache-Control", cache_control),
            ])
            return Response(200, "OK", headers, body)

    # Fallback for unmatched paths
    headers = Headers([("Access-Control-Allow-Origin", "*")])
    return Response(404, "Not Found", headers, b"not found\n")


async def authenticate_client(ws) -> bool:
    if not AUTH_TOKEN:
        return True
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT_SECONDS)
        message = json.loads(raw) if isinstance(raw, str) else None
        valid = (
            isinstance(message, dict)
            and message.get("type") == "auth"
            and message.get("protocol") == AUTH_PROTOCOL
            and isinstance(message.get("token"), str)
            and hmac.compare_digest(
                message["token"].encode("utf-8"), AUTH_TOKEN.encode("utf-8")
            )
        )
    except (
        asyncio.TimeoutError,
        ConnectionClosedError,
        ConnectionClosedOK,
        json.JSONDecodeError,
        UnicodeError,
        TypeError,
    ):
        valid = False
    if not valid:
        await ws.close(code=1008, reason="Unauthorized")
        return False
    await ws.send(json.dumps({"type": "auth_result", "protocol": AUTH_PROTOCOL, "ok": True}))
    return True


async def report_late_prompt_failure(ws, task, pane_id, request_id):
    """Report a prompt that was confirmed on acceptance but then failed.

    Confirmation goes out as soon as the agent holds the command past the
    grace window, so a failure has to travel on its own afterwards.
    """
    try:
        result = await task
    except Exception as exc:
        log.warning("agent_prompt command failed for pane %s: %s", pane_id, exc)
    else:
        if result.returncode == 0:
            return
        log.warning(
            "agent_prompt command failed for pane %s with exit %s",
            pane_id, result.returncode,
        )
    message = {"type": "error", "message": "Agent prompt submission failed"}
    if isinstance(request_id, str) and request_id:
        message["request_id"] = request_id
    try:
        await ws.send(json.dumps(message))
    except Exception:
        log.debug("Could not report late agent_prompt failure for pane %s", pane_id)


async def handle_client(ws):
    if not await authenticate_client(ws):
        return

    remote_addr = ws.remote_address
    ip = remote_addr[0] if remote_addr else "unknown"
    ua = ws.request.headers.get("User-Agent", "unknown") if ws.request else "unknown"
    origin = ws.request.headers.get("Origin", "") if ws.request else ""
    command_connection = (
        ws.request.headers.get("X-Herdr-Remote-Command") == "1"
        if ws.request
        else False
    )

    device = "unknown"
    ua_lower = ua.lower()
    if "iphone" in ua_lower or "ipad" in ua_lower:
        device = "iOS"
    elif "android" in ua_lower:
        device = "Android"
    elif "macintosh" in ua_lower or "mac os" in ua_lower:
        device = "macOS"
    elif "windows" in ua_lower:
        device = "Windows"
    elif "linux" in ua_lower:
        device = "Linux"
    elif "telegram" in ua_lower or "bot" in ua_lower:
        device = "bot"
    elif "python" in ua_lower:
        device = "script"

    log.info("Client connected: ip=%s device=%s origin=%s", ip, device, origin or "-")
    clients.add(ws)
    connected_at = time.monotonic()
    try:
        if not command_connection:
            await send_current_snapshot(ws)
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get("type")
            if msg_type == "question_toggle":
                target = resolve_pane(msg["pane_id"])
                option = msg.get("option", "")
                if target is None or not option:
                    await ws.send(json.dumps({"type": "error", "message": "invalid question option"}))
                    continue
                pane_id = target.pane_id
                if not prompt_matches(pane_id, msg.get("prompt_id", ""), **target.kwargs):
                    await ws.send(json.dumps({"type": "error", "message": "question changed; refresh and try again"}))
                    continue
                if not toggle_question_option(pane_id, option, **target.kwargs):
                    await ws.send(json.dumps({"type": "error", "message": "question option toggle failed"}))
            elif msg_type == "question_submit":
                target = resolve_pane(msg["pane_id"])
                if target is None:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                pane_id = target.pane_id
                if not prompt_matches(pane_id, msg.get("prompt_id", ""), **target.kwargs):
                    await ws.send(json.dumps({"type": "error", "message": "question changed; refresh and try again"}))
                    continue
                if not submit_multi_question(pane_id, **target.kwargs):
                    await ws.send(json.dumps({"type": "error", "message": "question submission failed"}))
            elif msg_type == "respond":
                target = resolve_pane(msg["pane_id"])
                request_id = msg.get("request_id")

                def command_error(message):
                    response = {"type": "error", "message": message}
                    if request_id:
                        response["request_id"] = request_id
                    return response

                if target is None:
                    await ws.send(json.dumps(command_error("unknown pane_id")))
                    continue
                pane_id = target.pane_id
                text = msg.get("text", "").strip()
                if not text or len(text) > 1000:
                    await ws.send(json.dumps(command_error("response empty or too long")))
                    continue
                content = read_pane(pane_id, **target.kwargs)
                if question_prompt_id(pane_id, content) != msg.get("prompt_id", ""):
                    await ws.send(json.dumps(command_error("prompt changed; refresh and try again")))
                    continue
                question = detect_question(content) if pane_is_omp(
                    pane_id, remote=target.kwargs["remote"]
                ) else None
                log.info("Response from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("respond", ip, device, pane_id, f"text={text!r}")
                if question:
                    delivered = respond_to_question(pane_id, text, question, **target.kwargs)
                elif custom_editor_active(content) or text.lower() in SAFE_RESPONSES:
                    delivered = _mutate_herdr(
                        "pane", "send-text", target.herdr_pane_id, text, **target.kwargs
                    ) and _mutate_herdr(
                        "pane", "send-keys", target.herdr_pane_id, "Enter", **target.kwargs
                    )
                else:
                    await ws.send(json.dumps({
                        **command_error("free-text response requires a detected question"),
                    }))
                    continue
                if not delivered:
                    await ws.send(json.dumps(command_error("response delivery failed")))
                    continue
                response = {"type": "command_result", "command": "respond", "ok": True}
                if request_id:
                    response["request_id"] = request_id
                await ws.send(json.dumps(response))
            elif msg_type == "agent_event":
                event_queue.put_nowait(msg)
            elif msg_type == "read_pane":
                target = resolve_pane(msg["pane_id"])
                if target is None:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                lines = msg.get("lines", "30")
                read_format = msg.get("format", "text")
                if read_format not in {"text", "ansi"}:
                    await ws.send(json.dumps({"type": "error", "message": "invalid pane read format"}))
                    continue
                content = run_herdr(
                    "pane", "read", target.herdr_pane_id, "--lines", str(lines),
                    "--source", "recent", "--format", read_format, **target.kwargs
                )
                # Echo the id the client asked with, so its own filtering matches.
                await ws.send(json.dumps({
                    "type": "pane_content", "pane_id": msg["pane_id"], "content": content
                }))
            elif msg_type == "get_timeline":
                await ws.send(json.dumps({
                    "type": "timeline",
                    "entries": timeline_entries,
                }))
            elif msg_type == "send_keys":
                target = resolve_pane(msg["pane_id"])
                request_id = msg.get("request_id")

                def command_error(message):
                    response = {"type": "error", "message": message}
                    if request_id:
                        response["request_id"] = request_id
                    return response

                if target is None:
                    await ws.send(json.dumps(command_error("unknown pane_id")))
                    continue
                pane_id = target.pane_id
                keys = msg.get("keys", [])
                if not all(k in SAFE_KEYS for k in keys):
                    await ws.send(json.dumps(command_error("keys contain disallowed values")))
                    continue
                content = read_pane(pane_id, **target.kwargs)
                if detect_approval_options(content) and any(key.isdigit() for key in keys):
                    if question_prompt_id(pane_id, content) != msg.get("prompt_id", ""):
                        await ws.send(json.dumps(command_error("prompt changed; refresh and try again")))
                        continue
                log.info("Keys from %s (%s): pane=%s keys=%s", ip, device, pane_id, keys)
                audit("send_keys", ip, device, pane_id, f"keys={keys}")
                try:
                    result = run_herdr_result(
                        "pane", "send-keys", target.herdr_pane_id, *keys, **target.kwargs
                    )
                except Exception as exc:
                    log.warning("send_keys command failed for pane %s: %s", pane_id, exc)
                    await ws.send(json.dumps(command_error("send_keys command failed")))
                    continue
                if result.returncode != 0:
                    log.warning("send_keys command failed for pane %s with exit %s", pane_id, result.returncode)
                    await ws.send(json.dumps(command_error("send_keys command failed")))
                    continue
                response = {"type": "command_result", "command": "send_keys", "ok": True}
                if request_id:
                    response["request_id"] = request_id
                await ws.send(json.dumps(response))
            elif msg_type == "send_text":
                target = resolve_pane(msg["pane_id"])
                if target is None:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                pane_id = target.pane_id
                text = msg.get("text", "")
                if not text or len(text) > 1000:
                    await ws.send(json.dumps({"type": "error", "message": "text empty or too long"}))
                    continue
                log.info("Text from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("send_text", ip, device, pane_id, f"text={text!r}")
                run_herdr("pane", "send-text", target.herdr_pane_id, text, **target.kwargs)
            elif msg_type == "agent_prompt":
                # Use 'herdr agent prompt' for proper submission (works with Codex, Claude, etc.)
                request_id = msg.get("request_id")

                def prompt_error(message):
                    response = {"type": "error", "message": message}
                    if isinstance(request_id, str) and request_id:
                        response["request_id"] = request_id
                    return response

                if request_id is not None and (
                    not isinstance(request_id, str) or not request_id or len(request_id) > 128
                ):
                    await ws.send(json.dumps(prompt_error("Invalid prompt request_id")))
                    continue
                requested_pane_id = msg.get("pane_id")
                if not isinstance(requested_pane_id, str):
                    await ws.send(json.dumps(prompt_error("unknown pane_id")))
                    continue
                target = resolve_pane(requested_pane_id)
                if target is None:
                    await ws.send(json.dumps(prompt_error("unknown pane_id")))
                    continue
                pane_id = target.pane_id
                text = msg.get("text", "")
                if not isinstance(text, str) or not text or len(text) > 10000:
                    await ws.send(json.dumps(prompt_error("text empty or too long")))
                    continue
                log.info("Agent prompt from %s (%s): pane=%s chars=%d", ip, device, pane_id, len(text))
                audit("agent_prompt", ip, device, pane_id, f"chars={len(text)}")
                prompt_task = asyncio.create_task(asyncio.to_thread(
                    run_herdr_result,
                    "agent", "prompt", target.herdr_pane_id, text, **target.kwargs,
                ))
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(prompt_task), PROMPT_ACK_GRACE
                    )
                except asyncio.TimeoutError:
                    # The agent is still busy. Confirm acceptance now so the
                    # client stops waiting on a prompt that did arrive, and let
                    # any failure follow separately.
                    response = {"type": "command_result", "command": "agent_prompt", "ok": True}
                    if request_id:
                        response["request_id"] = request_id
                    await ws.send(json.dumps(response))
                    asyncio.create_task(
                        report_late_prompt_failure(ws, prompt_task, pane_id, request_id)
                    )
                    continue
                except Exception as exc:
                    log.warning("agent_prompt command failed for pane %s: %s", pane_id, exc)
                    await ws.send(json.dumps(prompt_error("Agent prompt submission failed")))
                    continue
                if result.returncode != 0:
                    log.warning(
                        "agent_prompt command failed for pane %s with exit %s",
                        pane_id, result.returncode,
                    )
                    await ws.send(json.dumps(prompt_error("Agent prompt submission failed")))
                    continue
                response = {"type": "command_result", "command": "agent_prompt", "ok": True}
                if request_id:
                    response["request_id"] = request_id
                await ws.send(json.dumps(response))
            elif msg_type == "create_tab":
                workspace_id = msg.get("workspace_id", "")
                workspace = resolve_workspace(workspace_id)
                if workspace:
                    log.info("Create tab from %s (%s): workspace=%s", ip, device, workspace_id)
                    audit("create_tab", ip, device, "", f"workspace={workspace_id}")
                    run_herdr(
                        "tab", "create", "--workspace", workspace["herdr_workspace_id"],
                        "--focus", **herdr_target(workspace["remote"], workspace["session"])
                    )
                    await ws.send(json.dumps({"type": "tab_created", "ok": True}))
                else:
                    await ws.send(json.dumps({"type": "error", "message": "workspace_id required"}))
            elif msg_type == "push_subscribe":
                sub = msg.get("subscription")
                if sub and sub not in push_subscriptions:
                    push_subscriptions.append(sub)
                    _save_push_subs()
                    log.info("Push subscription added from %s (%s)", ip, device)
                await ws.send(json.dumps({"type": "push_subscribed", "ok": True}))
            elif msg_type == "push_unsubscribe":
                sub = msg.get("subscription")
                if sub and sub in push_subscriptions:
                    push_subscriptions.remove(sub)
                    _save_push_subs()
                await ws.send(json.dumps({"type": "push_unsubscribed", "ok": True}))
    except (ConnectionClosedError, ConnectionClosedOK):
        pass
    finally:
        duration = int(time.monotonic() - connected_at)
        log.info("Client disconnected: ip=%s device=%s duration=%ds", ip, device, duration)
        clients.discard(ws)


class UDPPlugin(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        try:
            event_queue.put_nowait(json.loads(data.decode()))
        except Exception:
            pass


def stop_mdns(zc, info):
    """Tear down discovery without ever raising.

    zeroconf's synchronous API waits on its own event loop, and calling it while
    the relay's loop is shutting down times out. That exception used to escape
    main and exit non-zero, so every ordinary restart was recorded as a failure
    — which is exactly the signal a real crash needs to stand out from.
    """
    try:
        if info is not None:
            zc.unregister_service(info)
    except Exception as error:
        log.warning("mDNS unregister failed: %s", error)
    finally:
        try:
            zc.close()
        except Exception as error:
            log.warning("mDNS close failed: %s", error)


def start_mdns():
    # A relay reached over a private network or a tunnel has nobody to announce
    # itself to on the local link.
    if os.environ.get("HERDR_MDNS", "1").strip().lower() in {"0", "false", "no", "off"}:
        log.info("mDNS disabled")
        return None, None
    try:
        from zeroconf import Zeroconf, ServiceInfo
        import socket as sock_mod
        ip = sock_mod.gethostbyname(sock_mod.gethostname())
        info = ServiceInfo(
            "_herdr-remote._tcp.local.", "herdr-remote._herdr-remote._tcp.local.",
            addresses=[sock_mod.inet_aton(ip)], port=WS_PORT,
        )
        zc = Zeroconf()
        threading.Thread(target=zc.register_service, args=(info,), daemon=True).start()
        log.info("mDNS registering at %s", ip)
        return zc, info
    except Exception as e:
        log.warning("mDNS skipped: %s", e)
        return None, None


async def main():
    loop = asyncio.get_running_loop()
    zc = info = udp_transport = server = None
    tasks = []
    loop_signal_handlers = []
    fallback_signal_handlers = {}
    stop = loop.create_future()

    def resolve_stop():
        if not stop.done():
            stop.set_result(None)

    def request_stop(*_):
        loop.call_soon_threadsafe(resolve_stop)

    try:
        zc, info = start_mdns()
        try:
            udp_transport, _ = await loop.create_datagram_endpoint(
                UDPPlugin, local_addr=("127.0.0.1", 8376)
            )
        except OSError:
            log.warning("UDP 8376 in use, plugin push disabled")
        tasks = [asyncio.create_task(poll_loop()), asyncio.create_task(event_push())]
        server = await serve(
            handle_client, RELAY_HOST, WS_PORT,
            process_request=process_request,
            ping_interval=WS_PING_INTERVAL,
            ping_timeout=WS_PING_TIMEOUT,
        )
        local_sessions = list_local_sessions()
        hosts = [
            f"local:{session}" if session else "local" for session in local_sessions
        ] or ["local (no running session)"]
        hosts += REMOTES
        log.info("herdr-remote relay on %s:%d (WebSocket + HTTP POST)", RELAY_HOST, WS_PORT)
        log.info("Polling: %s", ", ".join(hosts))
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_stop)
                loop_signal_handlers.append(sig)
            except NotImplementedError:
                fallback_signal_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, request_stop)
        await stop
    finally:
        for sig in loop_signal_handlers:
            loop.remove_signal_handler(sig)
        for sig, handler in fallback_signal_handlers.items():
            signal.signal(sig, handler)
        if server is not None:
            server.close()
            await server.wait_closed()
        if udp_transport is not None:
            udp_transport.close()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if zc is not None:
            # In a worker thread: the synchronous teardown would otherwise block
            # the loop it is being shut down from.
            await asyncio.to_thread(stop_mdns, zc, info)


if __name__ == "__main__":
    asyncio.run(main())
