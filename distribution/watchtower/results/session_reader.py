"""Bounded, read-only result snapshots from one explicitly identified Codex rollout.

This adapter supports the typed JSONL records produced by Codex 0.157.1 and
older event_msg/response_item records. It never starts Codex, reads credentials,
resumes a session, or exposes reasoning/tool stdout as an assistant answer.
The caller must verify the selected pane's provider, session ID and account home.
"""

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import threading
from urllib.parse import unquote, urlsplit


_SESSION = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif"}
_DOCUMENT = {".md", ".txt", ".pdf", ".html", ".csv", ".xlsx", ".docx", ".pptx"}
_EXTENSIONS = _IMAGE | _DOCUMENT | {".mp4", ".webm", ".wav", ".mp3", ".zip", ".json"}
_SUFFIX = "(?:" + "|".join(re.escape(value) for value in sorted(_EXTENSIONS)) + ")"
_LINK = re.compile(r"!?\[([^\]\n]{0,256})\]\(\s*(?:<([^>]{1,4096})>|([^\n]{1,4096}?))\s*\)")
_BARE = re.compile(r"(?<![A-Za-z0-9:/\\])(?:file://[^\r\n<>\"`]*?|[A-Za-z]:[/\\][^\r\n<>\"`]*?|/[^\s<>\"`]*?)" + _SUFFIX + r"(?=$|[\s)\]>,;])", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANSI = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:/-]{1,120}\Z")
_WAIT_TOOLS = {
    "request_user_input": "user", "functions.request_user_input": "user",
    "request_user_input_async": "user", "functions.request_user_input_async": "user",
    "collaboration.wait_agent": "agents", "collaboration.wait_agents": "agents",
    "collaboration.wait_threads": "agents", "mcp__codex_app__wait_threads": "agents",
    "functions.mcp__codex_app__wait_threads": "agents",
}
_HISTORY_FIELDS = ("key", "task", "state", "messages", "activity", "artifacts")


def _text_size(value):
    """Bound retained history without repeatedly serializing its contents."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_text_size(item) for item in value.values())
    if isinstance(value, list):
        return sum(_text_size(item) for item in value)
    return 0


def _text(value, limit=65536):
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub("", _ANSI.sub("", value))[:limit]


def _content(value):
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, list):
        return ""
    return "\n".join(_text(part.get("text")) for part in value[:128]
                     if isinstance(part, dict) and part.get("type") in
                     ("text", "Text", "input_text", "output_text"))[:65536]


def _stamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    except ValueError:
        return None


def _key(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _within(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _same_path(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def _artifact_path(value, cwd):
    """Normalize explicit local or HTTP(S) references; never open or fetch them."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    value = value.strip().strip('<>"`')
    if value.startswith("file://"):
        # Wrapped URLs occur in exported Markdown; only join inside a URI.
        value = re.sub(r"\r?\n[ \t]*", "", value)
        parsed = urlsplit(value)
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            return None  # No implicit remote Windows share access.
        value = unquote(parsed.path)
        if re.match(r"/[A-Za-z]:/", value):
            value = value[1:]
    elif value.startswith(("https://", "http://")):
        if _CONTROL.search(value) or "\n" in value or "\r" in value:
            return None
        parsed = urlsplit(value)
        if not parsed.netloc or parsed.username or parsed.password:
            return None
        return value
    elif re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", value) and not re.match(r"[A-Za-z]:[/\\]", value):
        return None
    if not value or _CONTROL.search(value) or "\n" in value or "\r" in value or value.startswith(("\\\\", "//")):
        return None
    # Markdown :line suffixes identify a location in a file, not its filename.
    value = re.sub(r":\d+(?::\d+)?$", "", value)
    # Codex Markdown links may prefix a Windows drive with a URI-style slash.
    # pathlib resolves /C:/... incorrectly on Windows; keep the absolute drive.
    if re.match(r"^/[A-Za-z]:[/\\]", value):
        value = value[1:]
    if PureWindowsPath(value).is_absolute():
        return str(PureWindowsPath(value)).replace("\\", "/")
    path = Path(value)
    if not path.is_absolute():
        if not cwd or not value or value.startswith("~"):
            return None
        path = Path(cwd) / path
    try:
        return str(path.resolve())
    except (OSError, ValueError, RuntimeError):
        return None


def _empty(session_id, provider="codex", error=None):
    return dict(version=1, provider_supported=provider == "codex", provider=provider,
                session_id=session_id, state="unavailable" if provider == "codex" else "unsupported",
                task=dict(id=None, request="", status="unknown", timestamp=None),
                key=None, history=[], waiting=None, messages=[], activity=[], artifacts=[],
                error=error, truncated=False, revision="")


class _Snapshot:
    def __init__(self, session_id, cwd, max_messages, max_artifacts, max_turns, history_chars):
        self.value = _empty(session_id)
        self.value.update(state="idle", error=None)
        self.cwd = cwd
        self.max_messages = max_messages
        self.max_artifacts = max_artifacts
        self.max_turns = max_turns
        self.history_chars = history_chars
        self.history_sizes = []
        self.history_size = 0
        self.record_offset = 0
        self.known = False
        self.turn_open = False
        self.message_keys = set()
        self.artifact_keys = set()
        self.activity_keys = set()
        self.calls = OrderedDict()
        self.pending_calls = {}
        self.retired_turn_ids = OrderedDict()

    def archive(self):
        if not self.value["key"]:
            return
        previous = {key: deepcopy(self.value[key]) for key in _HISTORY_FIELDS}
        if previous["task"]["id"]:
            self.retired_turn_ids[previous["task"]["id"]] = None
            while len(self.retired_turn_ids) > 128:
                self.retired_turn_ids.popitem(last=False)
        if previous["state"] == "running":
            previous["state"] = "interrupted"
            previous["task"]["status"] = "interrupted"
        size = _text_size(previous)
        self.value["history"].append(previous)
        self.history_sizes.append(size)
        self.history_size += size
        while len(self.history_sizes) > self.max_turns or self.history_size > self.history_chars:
            self.history_size -= self.history_sizes.pop(0)
            self.value["history"].pop(0)
            self.value["truncated"] = True

    def start(self, turn_id=None, timestamp=None, force=False):
        turn_id = _text(turn_id, 120) or None
        task = self.value["task"]
        if not force and turn_id in self.retired_turn_ids:
            return
        if not force and turn_id and task["id"] == turn_id:
            return  # Mirrored start/completion records do not create a new turn.
        if not force and self.turn_open and ((turn_id and not task["id"]) or
                                            (not turn_id and not task["request"] and
                                             not self.value["messages"] and not self.value["activity"])):
            if turn_id:
                task["id"] = turn_id
            return
        self.archive()
        key = _key(self.value["session_id"] + ":" + str(self.record_offset))
        self.value.update(state="running", task=dict(id=turn_id, request="", status="running", timestamp=timestamp),
                          key=key, waiting=None, messages=[], activity=[], artifacts=[])
        self.message_keys.clear()
        self.artifact_keys.clear()
        self.activity_keys.clear()
        self.calls.clear()
        self.pending_calls.clear()
        self.turn_open = True

    def ensure_current(self, timestamp):
        if self.value["key"] is None:
            self.start(timestamp=timestamp)

    def user_message(self, text, timestamp, turn_id=None):
        text = _text(text, 8192)
        if not text or turn_id in self.retired_turn_ids:
            return
        task = self.value["task"]
        if turn_id and turn_id == task["id"] and task["request"]:
            # A same-turn answer/steering message is not a second task. Keep its
            # original request and identity so completion remains attributable.
            return
        if (turn_id and task["id"] and turn_id != task["id"]) or not self.value["key"]:
            self.start(turn_id, timestamp)
        elif not self.turn_open or (task["request"] != text and task["request"]):
            # Some rollout variants omit turn_started before a follow-up.
            self.start(turn_id, timestamp, force=True)
        if turn_id and self.value["task"]["id"] is None:
            self.value["task"]["id"] = _text(turn_id, 120) or None
        self.value["task"]["request"] = text

    def waiting(self):
        self.value["waiting"] = None
        if not self.turn_open:
            return
        # Explicit requests for the user take precedence over parallel waits.
        for kind in ("user", "agents"):
            for name in self.pending_calls.values():
                if _WAIT_TOOLS.get(name) == kind:
                    self.value["waiting"] = dict(kind=kind, tool=name)
                    return

    def tool_call(self, call_id, name, timestamp, completed=False):
        self.ensure_current(timestamp)
        call_id = _text(call_id, 256)
        name = name if isinstance(name, str) and _TOOL_NAME.fullmatch(name) else "tool"
        if call_id:
            if completed:
                self.pending_calls.pop(call_id, None)
            elif self.turn_open and call_id not in self.calls and name in _WAIT_TOOLS:
                if len(self.pending_calls) < 256:
                    self.pending_calls[call_id] = name
                else:
                    self.value["truncated"] = True
            # Retain completed IDs as tombstones: a mirrored start is stale.
            if call_id not in self.calls:
                self.calls[call_id] = name
            while len(self.calls) > 256:
                removable = next((key for key in self.calls if key not in self.pending_calls), None)
                if removable is None:
                    break
                del self.calls[removable]
        self.waiting()

    def finish(self, state, turn_id=None):
        if turn_id and (self.value["task"]["id"] not in (None, turn_id) or turn_id in self.retired_turn_ids):
            return False  # A late event from the prior turn cannot finish this one.
        self.value["state"] = state
        self.value["task"].update(status=state, id=self.value["task"]["id"] or turn_id)
        self.turn_open = False
        self.pending_calls.clear()
        self.waiting()
        return True

    def message(self, value, phase, item_id, timestamp):
        if phase not in (None, "commentary", "final_answer"):
            return
        text = _text(value)
        if not text:
            return
        self.ensure_current(timestamp)
        kind = "final" if phase == "final_answer" else "commentary"
        key = kind, text
        if key in self.message_keys:
            return
        if kind == "final":
            # Legacy records can repeat an unclassified message at completion.
            self.value["messages"] = [m for m in self.value["messages"]
                                      if not (m["text"] == text and m["kind"] == "commentary")]
        self.message_keys.add(key)
        self.value["messages"].append(dict(id=_text(item_id, 120) or _key(kind + text), kind=kind,
                                           text=text, timestamp=timestamp))
        if len(self.value["messages"]) > self.max_messages:
            self.value["messages"] = self.value["messages"][-self.max_messages:]
            self.message_keys = {(m["kind"], m["text"]) for m in self.value["messages"]}
            self.value["truncated"] = True
        self.references(text, "assistant")

    def artifact(self, value, source, label=None):
        path = _artifact_path(value, self.cwd)
        if not path or path in self.artifact_keys:
            return
        parsed = urlsplit(path) if path.startswith(("https://", "http://")) else None
        suffix = Path(unquote(parsed.path) if parsed else path).suffix.lower()
        if suffix not in _EXTENSIONS and not (source == "image_generation" and parsed):
            return
        if len(self.value["artifacts"]) >= self.max_artifacts:
            self.value["truncated"] = True
            return
        self.artifact_keys.add(path)
        name = _text(label, 160) or Path(unquote(parsed.path) if parsed else path.replace("\\", "/")).name
        self.value["artifacts"].append(dict(id=_key(path), name=name or "Generated file", path=path,
                                            kind="image" if suffix in _IMAGE or source == "image_generation" else
                                            ("document" if suffix in _DOCUMENT else "file"), source=source))

    def references(self, text, source):
        for match in _LINK.finditer(text):
            self.artifact(match[2] or match[3], source, match[1])
        # Explicit angle-wrapped file URIs may contain a visual line wrap.
        for match in re.finditer(r"<(file://[^>]{1,4096})>", text, re.I):
            self.artifact(match[1], source)
        for match in _BARE.finditer(text):
            self.artifact(match[0], source)

    def resources(self, value, source="tool", depth=0):
        """Inspect typed resource fields only; arbitrary stdout is never a result."""
        if depth > 6:
            return
        if isinstance(value, list):
            for item in value[:128]:
                self.resources(item, source, depth + 1)
        elif isinstance(value, dict):
            typ = value.get("type")
            if typ in ("resource_link", "resource"):
                self.artifact(value.get("uri"), source, value.get("name"))
            for key in ("savedPath", "saved_path", "output_path", "output_file", "image_url"):
                candidate = value.get(key)
                if isinstance(candidate, dict):
                    candidate = candidate.get("url")
                self.artifact(candidate, source)
            for key in ("content", "contentItems", "content_items", "resource", "resources", "result", "structuredContent", "output"):
                child = value.get(key)
                if isinstance(child, (dict, list)):
                    self.resources(child, source, depth + 1)
                elif isinstance(child, str) and len(child) <= 262144:
                    try:
                        structured = json.loads(child)
                    except ValueError:
                        structured = None
                    if isinstance(structured, (dict, list)):
                        self.resources(structured, source, depth + 1)
                    elif source == "image_generation":
                        self.references(child, source)
            if source == "image_generation" and typ in ("text", "Text"):
                self.references(_text(value.get("text")), source)

    def activity(self, kind, text, timestamp, item_id=None):
        self.ensure_current(timestamp)
        key = (kind, item_id or text, timestamp)
        if key in self.activity_keys:
            return
        self.activity_keys.add(key)
        self.value["activity"].append(dict(kind=kind, text=text, timestamp=timestamp))
        self.value["activity"] = self.value["activity"][-80:]
        if len(self.activity_keys) > 160:
            self.activity_keys = {key}

    def item(self, item, timestamp, turn_id=None, completed=True):
        if not isinstance(item, dict):
            return
        typ = item.get("type")
        is_user = typ in ("UserMessage", "userMessage")
        if turn_id and (turn_id in self.retired_turn_ids or
                        (not is_user and self.value["task"]["id"] not in (None, turn_id))):
            # Starts establish identity; late per-item events do not switch it.
            return
        if is_user:
            self.known = True
            self.user_message(_content(item.get("content")), timestamp, turn_id)
        elif typ in ("AgentMessage", "agentMessage"):
            self.known = True
            self.message(item.get("text") or _content(item.get("content")), item.get("phase"), item.get("id"), timestamp)
        elif typ in ("CommandExecution", "commandExecution"):
            self.known = True
            code = item.get("exitCode", item.get("exit_code"))
            self.activity("tool", "Command finished" if code in (0, None) else "Command failed", timestamp, item.get("id"))
        elif typ in ("McpToolCall", "mcpToolCall", "DynamicToolCall", "dynamicToolCall"):
            self.known = True
            name = item.get("tool", item.get("name", ""))
            safe = name if isinstance(name, str) and _TOOL_NAME.fullmatch(name) else "tool"
            status = item.get("status")
            done = completed or status in ("completed", "failed", "cancelled", "canceled")
            self.tool_call(item.get("call_id", item.get("callId", item.get("id"))), safe, timestamp, completed=done)
            self.activity("tool", "Used " + safe, timestamp, item.get("id"))
            source = "image_generation" if any(value in safe.lower() for value in ("imagegen", "image_gen", "generate_image")) else "tool"
            self.resources(item, source)
        elif typ in ("ImageGeneration", "imageGeneration") or (typ == "Extension" and item.get("kind") == "image_gen.generation"):
            self.known = True
            self.activity("tool", "Generated image" if item.get("status") == "completed" else "Image generation", timestamp, item.get("id"))
            self.resources(item, "image_generation")
            self.artifact(item.get("result"), "image_generation")

    def consume(self, event, record_offset=0):
        self.record_offset = record_offset
        if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
            return
        payload = event["payload"]
        timestamp = _stamp(event.get("timestamp"))
        typ = event.get("type")
        kind = payload.get("type")
        if typ == "event_msg":
            if kind in ("task_started", "turn_started"):
                self.known = True
                self.start(_text(payload.get("turn_id"), 120) or None, timestamp)
            elif kind == "user_message":
                self.known = True
                self.user_message(payload.get("message"), timestamp, payload.get("turn_id"))
            elif kind == "agent_message":
                self.known = True
                self.message(payload.get("message"), payload.get("phase"), payload.get("id"), timestamp)
            elif kind in ("item_started", "item_completed"):
                self.item(payload.get("item"), timestamp, payload.get("turn_id"), completed=kind == "item_completed")
            elif kind in ("task_complete", "turn_complete"):
                self.known = True
                self.ensure_current(timestamp)
                if self.finish("complete", _text(payload.get("turn_id"), 120) or None):
                    self.message(payload.get("last_agent_message"), "final_answer", None, timestamp)
            elif kind in ("turn_aborted", "task_aborted"):
                self.known = True
                self.ensure_current(timestamp)
                self.finish("interrupted", _text(payload.get("turn_id"), 120) or None)
            elif kind == "error":
                self.known = True
                self.activity("error", "The agent reported an error; see its terminal for details.", timestamp)
            return
        if typ != "response_item":
            return
        if kind == "message" and payload.get("role") == "user":
            self.known = True
            if self.turn_open and self.value["task"]["id"]:
                # Model-input records can include context/instructions and
                # multiple user messages for one turn. In the typed schema,
                # UserMessage/event_msg identifies the actual request; an
                # anonymous input record must not split or retire that turn.
                return
            self.user_message(_content(payload.get("content")), timestamp)
        elif kind == "message" and payload.get("role") == "assistant":
            if payload.get("channel") not in (None, "commentary", "final"):
                return
            self.known = True
            # Only the explicitly user-facing assistant phase is eligible.
            self.message(_content(payload.get("content")), payload.get("phase"), payload.get("id"), timestamp)
        elif kind in ("function_call", "custom_tool_call"):
            self.known = True
            name = payload.get("name", "")
            name = name if isinstance(name, str) and _TOOL_NAME.fullmatch(name) else "tool"
            call_id = payload.get("call_id")
            self.tool_call(call_id, name, timestamp)
            self.activity("tool", "Used " + name, timestamp, call_id)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            self.known = True
            name = self.calls.get(payload.get("call_id"), "")
            self.tool_call(payload.get("call_id"), name, timestamp, completed=True)
            source = "image_generation" if any(value in name.lower() for value in ("imagegen", "image_gen", "generate_image")) else "tool"
            self.resources(payload, source)
        elif kind == "image_generation_call":
            self.known = True
            self.ensure_current(timestamp)
            self.resources(payload, "image_generation")
            self.artifact(payload.get("result"), "image_generation")


class CodexSessionReader:
    """Read only a matching rollout in the explicitly supplied account home.

    Current-turn messages are bounded, final/commentary are separate, and prior
    turns appear only in detached ``history`` snapshots. At most ``max_turns``
    previous turns and 2 Mi characters of history are retained. Cache hits stat one file;
    appended data is parsed incrementally. A first large read uses a bounded
    tail, reports ``truncated``, and still validates the first session_meta.
    Artifact references are evidence, not permission to open arbitrary files.
    """

    def __init__(self, max_read_bytes=16 * 1024 * 1024, max_record_bytes=8 * 1024 * 1024,
                 max_entries=30000, max_messages=80, max_artifacts=80, cache_size=16, max_turns=20):
        self.max_read_bytes = max(1024, max_read_bytes)
        self.max_record_bytes = max(1024, max_record_bytes)
        self.max_entries = max_entries
        self.max_messages = max_messages
        self.max_artifacts = max_artifacts
        self.max_turns = min(20, max(0, max_turns))
        self.cache_size = cache_size
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def _find(self, home, session_id):
        matches = []
        entries = 0
        for base in (home / "sessions", home / "archived_sessions"):
            if not base.is_dir() or not _within(base, home):
                continue
            pending = [base]
            while pending:
                directory = pending.pop()
                with os.scandir(directory) as stream:
                    for entry in stream:
                        entries += 1
                        if entries > self.max_entries:
                            return None, "session_lookup_limit"
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if _within(Path(entry.path), base):
                                pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith("-" + session_id.lower() + ".jsonl"):
                            path = Path(entry.path)
                            if _within(path, base):
                                matches.append(path)
        if len(matches) != 1:
            return None, "session_not_found" if not matches else "ambiguous_session"
        return matches[0], None

    def _header(self, stream, session_id, cwd):
        line = stream.readline(min(self.max_record_bytes, 512 * 1024) + 1)
        if not line.endswith(b"\n") or len(line) > min(self.max_record_bytes, 512 * 1024):
            return None, "invalid_session_metadata"
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            return None, "invalid_session_metadata"
        meta = event.get("payload") if isinstance(event, dict) and event.get("type") == "session_meta" else None
        if not isinstance(meta, dict) or not isinstance(meta.get("id"), str) or meta["id"].lower() != session_id.lower():
            return None, "session_identity_mismatch"
        stored_cwd = meta.get("cwd")
        if not isinstance(stored_cwd, str) or not Path(stored_cwd).is_absolute():
            return None, "invalid_session_metadata"
        if cwd and not _same_path(stored_cwd, cwd):
            return None, "session_cwd_mismatch"
        return stored_cwd, None

    def read(self, session_id, home, cwd=None, provider="codex"):
        result = _empty(session_id, provider)
        if provider != "codex":
            result["error"] = "unsupported_provider"
            return result
        if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
            result["error"] = "invalid_session_id"
            return result
        try:
            if not isinstance(home, (str, os.PathLike)) or not Path(home).is_absolute():
                raise ValueError()
            home = Path(home).resolve()
            if not home.is_dir() or (cwd is not None and (not isinstance(cwd, (str, os.PathLike)) or not Path(cwd).is_absolute())):
                raise ValueError()
        except (OSError, ValueError, TypeError, RuntimeError):
            result["error"] = "invalid_home_or_cwd"
            return result
        key = (os.path.normcase(str(home)), session_id.lower(), os.path.normcase(str(cwd)) if cwd else None)
        with self._lock:
            try:
                cached = self._cache.get(key)
                if cached and not cached["path"].exists():
                    cached = None
                path, error = (cached["path"], None) if cached else self._find(home, session_id)
                if error:
                    result["error"] = error
                    return result
                if not _within(path, home):
                    result["error"] = "session_identity_mismatch"
                    return result
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
                reset = (not cached or identity != cached["identity"] or stat.st_size < cached["offset"] or
                         (stat.st_size == cached["offset"] and stat.st_mtime_ns != cached["mtime"]))
                if cached and not reset and stat.st_size == cached["offset"] and stat.st_mtime_ns == cached["mtime"]:
                    self._cache.move_to_end(key)
                    return deepcopy(cached["result"])
                with path.open("rb") as stream:
                    header_cwd, error = self._header(stream, session_id, cwd)
                    if error:
                        self._cache.pop(key, None)
                        result["error"] = error
                        return result
                    if reset:
                        state = _Snapshot(session_id, header_cwd, self.max_messages, self.max_artifacts,
                                          self.max_turns, min(self.max_read_bytes, 2 * 1024 * 1024))
                        cached = dict(path=path, identity=identity, offset=stream.tell(), mtime=0,
                                      snapshot=state, pending=b"", discarding=False)
                        if stat.st_size - stream.tell() > self.max_read_bytes:
                            stream.seek(stat.st_size - self.max_read_bytes)
                            # Drop the initial partial record without allocating it.
                            cached["discarding"] = True
                            cached["offset"] = stream.tell()
                            state.value["truncated"] = True
                    stream.seek(cached["offset"])
                    data = stream.read(self.max_read_bytes)
                    cached["offset"] = stream.tell()
                snapshot = cached["snapshot"]
                data = cached["pending"] + data
                record_offset = cached["offset"] - len(data)
                records = data.split(b"\n")
                cached["pending"] = records.pop()
                if cached["discarding"] and records:
                    record_offset += len(records.pop(0)) + 1
                    cached["discarding"] = False
                for record in records:
                    offset = record_offset
                    record_offset += len(record) + 1
                    if not record:
                        continue
                    if len(record) > self.max_record_bytes:
                        snapshot.value["truncated"] = True
                        continue
                    try:
                        event = json.loads(record)
                    except (ValueError, UnicodeError):
                        snapshot.value["truncated"] = True
                        continue
                    snapshot.consume(event, offset)
                if len(cached["pending"]) > self.max_record_bytes:
                    cached["pending"] = b""
                    cached["discarding"] = True
                    snapshot.value["truncated"] = True
                cached["mtime"] = stat.st_mtime_ns
                result = deepcopy(snapshot.value)
                if not snapshot.known and stat.st_size > 0:
                    result.update(state="unavailable", error="history_schema_unknown")
                if cached["offset"] < stat.st_size or cached["pending"] or cached["discarding"]:
                    result["truncated"] = True
                result["revision"] = _key(str(identity) + ":" + str(cached["offset"]) + ":" + str(stat.st_mtime_ns))
                cached["result"] = result
                self._cache[key] = cached
                self._cache.move_to_end(key)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
                return deepcopy(result)
            except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
                self._cache.pop(key, None)
                result["error"] = "session_unavailable"
                return result


SessionReader = CodexSessionReader
