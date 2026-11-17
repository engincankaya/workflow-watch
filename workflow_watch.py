#!/usr/bin/env python3
"""workflow-watch: a local, live viewer for Claude Code Workflow runs.

Start it with ``python3 workflow_watch.py``. It serves one page on 127.0.0.1
that lists every Workflow run of every project on this computer and draws
each run as a graph of its agents, updated while the run works.

It reads Claude Code's internal transcript files under ``~/.claude/projects``
(or ``$CLAUDE_CONFIG_DIR/projects``). Those files are not a documented
interface; see README.md for what the tool depends on.

Only the Python standard library is used (Python 3.9 or later). The page is
served from this file; the one third-party piece, the acorn JavaScript parser
(MIT licence), is embedded at the end of the file.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "0.1.0"

DEFAULT_PORT = 7766
DEFAULT_STALE_MINUTES = 45
DEFAULT_SCAN_DAYS = 3
REFRESH_INTERVAL = 1.0          # seconds between two scans of the disk
READ_CHUNK = 8 * 1024 * 1024    # bytes read from one file per step
MAX_TEXT = 4000                 # characters kept for one step detail
MAX_PROMPT = 200_000            # characters kept for one agent prompt
MAX_SCRIPT_BYTES = 2_000_000    # a script file larger than this is not read
PREVIEW = 400                   # characters of a result shown in summaries
NOTIFY_GRACE_MS = 60_000        # file activity this long after a "stopped"
                                # report means the run is still going

OFFSET_KEY = "\u0000offset"     # where a record's line starts in its file
MAX_LINE_BYTES = 64 * 1024 * 1024
MAX_FIELD = 400_000             # characters of one field in a step's full detail

RUN_ID_RE = re.compile(r"^wf_[A-Za-z0-9_-]+$")
NOTIFY_FIELD_RE = {
    name: re.compile(r"<%s>(.*?)</%s>" % (name, name), re.S)
    for name in ("task-id", "tool-use-id", "status", "summary")
}
SCRIPT_NAME_RE = re.compile(r"\bname\s*:\s*(['\"`])([^'\"`\n]{1,200})\1")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def iso_ms(value):
    """ISO-8601 text (as written by Claude Code) to epoch milliseconds."""
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


def _clock(ms):
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError, TypeError):
        return "?"


def num(value):
    """A JSON number or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def clip(text, limit=MAX_TEXT):
    if text is None:
        return None
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False, indent=1)
        except (TypeError, ValueError):
            text = str(text)
    if len(text) > limit:
        return text[:limit] + "\n… (%d more characters)" % (len(text) - limit)
    return text


def preview(value, limit=PREVIEW):
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def block_text(content):
    """Text of a message content: a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def tool_summary(name, tool_input):
    """A short, human description of one tool call."""
    if not isinstance(tool_input, dict):
        return preview(tool_input, 160) or ""
    for key in ("description",):
        if isinstance(tool_input.get(key), str) and tool_input[key].strip():
            desc = tool_input[key].strip()
            if name == "Bash" and isinstance(tool_input.get("command"), str):
                return desc[:200]
            return desc[:200]
    for key in ("command", "file_path", "path", "pattern", "url", "query",
                "prompt", "skill", "notebook_path", "summary"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            first = value.strip().splitlines()[0]
            return first[:200]
    for value in tool_input.values():
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:200]
    return preview(tool_input, 160) or ""


def clip_fields(value, depth=0):
    """A copy of a JSON value whose long strings are cut to MAX_FIELD."""
    if isinstance(value, str):
        return clip(value, MAX_FIELD)
    if depth > 30:
        return None
    if isinstance(value, list):
        return [clip_fields(v, depth + 1) for v in value[:5000]]
    if isinstance(value, dict):
        return {str(k): clip_fields(v, depth + 1) for k, v in list(value.items())[:500]}
    return value


def read_line_at(path, offset):
    """The JSON object on the line that starts at ``offset``, or None."""
    if not isinstance(offset, int) or offset < 0:
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            line = handle.readline(MAX_LINE_BYTES)
        value = json.loads(line.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def find_block(record, block_type, test):
    if not isinstance(record, dict) or not isinstance(record.get("message"), dict):
        return None
    content = record["message"].get("content")
    if isinstance(content, str) and block_type == "text":
        return {"type": "text", "text": content}
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == block_type and test(block):
            return block
    return None


def safe_stat(path):
    try:
        return path.stat()
    except OSError:
        return None


def safe_listdir(path):
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


def read_json_file(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle), None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, "%s: %s" % (path.name, exc.__class__.__name__)


def read_text_file(path, limit=MAX_SCRIPT_BYTES):
    try:
        st = path.stat()
        if st.st_size > limit:
            return None, "%s is larger than %d bytes" % (path.name, limit)
        return path.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:
        return None, "%s: %s" % (path.name, exc.__class__.__name__)


# --------------------------------------------------------------------------
# Incremental JSON-lines reader
# --------------------------------------------------------------------------

class JsonlTail:
    """Reads a JSON-lines file in steps, remembering how far it got.

    Only the bytes appended since the last call are read. A last line that
    is not complete yet stays in a buffer until its end arrives. Lines that
    are not valid JSON are counted and skipped.
    """

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.buffer = b""
        self.inode = None
        self.bad_lines = 0
        self.mtime = None
        self.size = 0
        self.bytes_read = 0

    def read(self, accept=None):
        """Return (records, reset). ``reset`` is True when the file was
        replaced or truncated and the caller must forget what it knew."""
        st = safe_stat(self.path)
        if st is None:
            return [], False
        reset = False
        if self.inode is not None and (st.st_ino != self.inode or st.st_size < self.offset):
            self.offset, self.buffer, self.bad_lines = 0, b"", 0
            reset = True
        self.inode = st.st_ino
        self.mtime = st.st_mtime
        self.size = st.st_size
        records = []
        if st.st_size <= self.offset:
            self._flush_complete_buffer(records, accept)
            return records, reset
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                while True:
                    chunk = handle.read(READ_CHUNK)
                    if not chunk:
                        break
                    start = self.offset - len(self.buffer)
                    self.offset += len(chunk)
                    self.bytes_read += len(chunk)
                    data = self.buffer + chunk
                    lines = data.split(b"\n")
                    self.buffer = lines.pop()
                    for line in lines:
                        self._parse(line, records, accept, start)
                        start += len(line) + 1
        except OSError:
            return records, reset
        self._flush_complete_buffer(records, accept)
        return records, reset

    def _flush_complete_buffer(self, records, accept):
        # A last line without its newline is accepted only when it is a whole
        # JSON object; otherwise it waits for the rest.
        if not self.buffer.strip():
            return
        try:
            value = json.loads(self.buffer.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(value, dict):
            if accept is None or accept(self.buffer):
                value[OFFSET_KEY] = self.offset - len(self.buffer)
                records.append(value)
            self.buffer = b""

    def _parse(self, line, records, accept, offset=None):
        line = line.strip()
        if not line:
            return
        if accept is not None and not accept(line):
            return
        try:
            value = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.bad_lines += 1
            return
        if isinstance(value, dict):
            value[OFFSET_KEY] = offset
            records.append(value)
        else:
            self.bad_lines += 1


# --------------------------------------------------------------------------
# Session transcript: launches, launch results, notifications
# --------------------------------------------------------------------------

SESSION_NEEDLES = (b'"Workflow"', b"toolUseResult", b"task-notification",
                   b"continued-in")


def session_line_wanted(line):
    head = line[:60]
    if b"api-request" in head:
        return False
    return any(needle in line for needle in SESSION_NEEDLES)


class SessionIndex:
    """What one main-session transcript says about workflow runs."""

    def __init__(self, path, project):
        self.path = path
        self.project = project
        self.session_id = path.stem
        self.tail = JsonlTail(path)
        self.cwd = None
        self.launches = {}        # tool_use_id -> launch
        self.launch_results = {}  # tool_use_id -> dict or error text
        self.notifications = {}   # task id -> list of notifications

    def refresh(self):
        if self.cwd is None:
            self._find_cwd()
        records, reset = self.tail.read(session_line_wanted)
        if reset:
            self.launches, self.launch_results, self.notifications = {}, {}, {}
        for record in records:
            try:
                self._take(record)
            except Exception:  # never let one odd line stop the scan
                self.tail.bad_lines += 1

    def _find_cwd(self):
        try:
            with self.path.open("rb") as handle:
                for index, line in enumerate(handle):
                    if index > 200:
                        break
                    if b'"cwd"' not in line:
                        continue
                    try:
                        value = json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(value, dict) and isinstance(value.get("cwd"), str):
                        self.cwd = value["cwd"]
                        return
        except OSError:
            return

    def _take(self, record):
        kind = record.get("type")
        if isinstance(record.get("cwd"), str) and self.cwd is None:
            self.cwd = record["cwd"]
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content")
        if kind == "assistant" and isinstance(content, list):
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("name") == "Workflow" and block.get("id")):
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    self.launches[block["id"]] = {
                        "toolUseId": block["id"],
                        "time": iso_ms(record.get("timestamp")),
                        "input": tool_input,
                        "cwd": record.get("cwd") if isinstance(record.get("cwd"), str) else None,
                    }
        if kind == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id")
                    if tool_use_id in self.launches:
                        result = record.get("toolUseResult")
                        if isinstance(result, dict):
                            result = dict(result)
                            result["_time"] = iso_ms(record.get("timestamp"))
                            self.launch_results[tool_use_id] = result
                        else:
                            self.launch_results[tool_use_id] = {
                                "error": block_text(block.get("content")) or str(result),
                                "_time": iso_ms(record.get("timestamp")),
                            }
        texts = []
        if isinstance(record.get("content"), str):
            texts.append(record["content"])
        if isinstance(content, (str, list)):
            texts.append(block_text(content))
        for text in texts:
            if "<task-notification>" in text:
                self._take_notification(text, iso_ms(record.get("timestamp")))

    def _take_notification(self, text, when):
        for part in text.split("<task-notification>")[1:]:
            fields = {}
            for name, pattern in NOTIFY_FIELD_RE.items():
                match = pattern.search(part)
                if match:
                    fields[name] = match.group(1).strip()
            task_id = fields.get("task-id")
            if not task_id:
                continue
            note = {
                "taskId": task_id,
                "toolUseId": fields.get("tool-use-id"),
                "status": fields.get("status"),
                "summary": fields.get("summary"),
                "time": when,
            }
            seen = self.notifications.setdefault(task_id, [])
            if not any(n["status"] == note["status"] and n["time"] == note["time"] for n in seen):
                seen.append(note)


# --------------------------------------------------------------------------
# One agent transcript
# --------------------------------------------------------------------------

class AgentTranscript:
    """Facts read from ``agent-<id>.jsonl``, built up incrementally."""

    def __init__(self, path):
        self.tail = JsonlTail(path)
        self.reset()

    def reset(self):
        self._offset = None
        self.first_time = None
        self.last_time = None
        self.prompt = None
        self.steps = []
        self.tool_steps = {}       # tool_use_id -> step index
        self.seen_blocks = set()
        self.usage = {}            # message id -> usage
        self.model = None
        self.cwd = None
        self.git_branch = None
        self.tool_calls = 0
        self.last_tool = None
        self.last_text = None
        self.structured = None
        self.errors = 0

    def refresh(self):
        records, reset = self.tail.read()
        if reset:
            self.reset()
        for record in records:
            try:
                self._take(record)
            except Exception:
                self.tail.bad_lines += 1

    @property
    def last_activity(self):
        times = [t for t in (self.last_time, (self.tail.mtime or 0) * 1000 or None) if t]
        return max(times) if times else None

    def tokens(self):
        """(context tokens of the last request, output tokens in total)."""
        if not self.usage:
            return None, None
        output = 0
        for usage in self.usage.values():
            output += num(usage.get("output_tokens")) or 0
        last = list(self.usage.values())[-1]
        context = sum(num(last.get(key)) or 0 for key in (
            "input_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens", "output_tokens"))
        return context, output

    def _add_step(self, step):
        step["i"] = len(self.steps)
        if self._offset is not None:
            step["off"] = self._offset
        self.steps.append(step)
        return step

    def _take(self, record):
        self._offset = record.get(OFFSET_KEY)
        kind = record.get("type")
        when = iso_ms(record.get("timestamp"))
        if when and kind in ("user", "assistant", "attachment", "system"):
            if self.first_time is None or when < self.first_time:
                self.first_time = when
            if self.last_time is None or when > self.last_time:
                self.last_time = when
        if isinstance(record.get("cwd"), str):
            self.cwd = record["cwd"]
        if isinstance(record.get("gitBranch"), str):
            self.git_branch = record["gitBranch"]
        message = record.get("message") if isinstance(record.get("message"), dict) else None
        if kind == "user" and message is not None:
            self._take_user(message, when, record)
        elif kind == "assistant" and message is not None:
            self._take_assistant(message, when)
        elif kind == "attachment":
            attachment = record.get("attachment")
            if isinstance(attachment, dict) and attachment.get("type") == "structured_output":
                self.structured = attachment.get("data")
        elif kind == "system":
            text = record.get("content")
            if isinstance(text, str) and text.strip():
                self._add_step({"kind": "system", "time": when, "text": clip(text, 600)})

    def _take_user(self, message, when, record):
        content = message.get("content")
        if isinstance(content, str):
            if self.prompt is None or "computed task" in content[:200]:
                if self.prompt is None or "computed task" in content[:200]:
                    self.prompt = content[:MAX_PROMPT]
            if not self.steps and "[Workflow harness" in content[:40]:
                return
            if self.steps:
                self._add_step({"kind": "user", "time": when, "text": clip(content)})
            return
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                index = self.tool_steps.get(block.get("tool_use_id"))
                text = block_text(block.get("content"))
                is_error = bool(block.get("is_error"))
                if is_error:
                    self.errors += 1
                if index is not None:
                    step = self.steps[index]
                    step["result"] = clip(text)
                    step["isError"] = is_error
                    step["end"] = when
                    if self._offset is not None:
                        step["roff"] = self._offset
                else:
                    self._add_step({"kind": "tool_result", "time": when,
                                    "text": clip(text), "isError": is_error})
            elif block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if self.prompt is None:
                    self.prompt = text[:MAX_PROMPT]
                elif self.steps:
                    self._add_step({"kind": "user", "time": when, "text": clip(text)})

    def _take_assistant(self, message, when):
        msg_id = message.get("id") or ""
        if isinstance(message.get("model"), str) and message.get("model") != "<synthetic>":
            self.model = message["model"]
        if isinstance(message.get("usage"), dict) and msg_id:
            self.usage[msg_id] = message["usage"]
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                tool_id = block.get("id") or ""
                if tool_id in self.tool_steps:
                    continue
                name = block.get("name") or "?"
                tool_input = block.get("input")
                step = self._add_step({
                    "kind": "tool", "time": when, "name": name,
                    "summary": tool_summary(name, tool_input),
                    "input": clip(tool_input),
                })
                if isinstance(tool_input, dict) and name == "Bash":
                    step["command"] = clip(tool_input.get("command"), 600)
                if tool_id:
                    self.tool_steps[tool_id] = step["i"]
                    step["toolId"] = tool_id
                self.tool_calls += 1
                self.last_tool = {"name": name, "summary": step["summary"], "time": when,
                                  "step": step["i"]}
            elif btype == "text":
                text = block.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                key = (msg_id, "t", hashlib.sha1(text.encode("utf-8", "replace")).hexdigest())
                if key in self.seen_blocks:
                    continue
                self.seen_blocks.add(key)
                self._add_step({"kind": "text", "time": when, "text": clip(text)})
                self.last_text = {"text": clip(text, 1500), "time": when}
            elif btype in ("thinking", "redacted_thinking"):
                text = block.get("thinking") if isinstance(block.get("thinking"), str) else ""
                key = (msg_id, "th", hashlib.sha1(text.encode("utf-8", "replace")).hexdigest())
                if key in self.seen_blocks:
                    continue
                self.seen_blocks.add(key)
                self._add_step({"kind": "thinking", "time": when, "text": clip(text)})


# --------------------------------------------------------------------------
# One workflow run
# --------------------------------------------------------------------------

class Run:
    def __init__(self, run_id, project, session_dir):
        self.run_id = run_id
        self.project = project
        self.session_dir = session_dir
        self.session_id = session_dir.name
        self.run_dir = session_dir / "subagents" / "workflows" / run_id
        self.record_path = session_dir / "workflows" / (run_id + ".json")
        self.journal = JsonlTail(self.run_dir / "journal.jsonl")
        self.record = None
        self.record_mtime = None
        self.agents = {}         # agentId -> dict
        self.order = []          # agent ids in start order
        self.events = 0
        self.unknown_events = {}
        self.notes = []
        self.transcripts = {}    # agentId -> AgentTranscript
        self.metas = {}          # agentId -> meta dict
        self.meta_errors = set()
        self.dir_mtime = None
        self.agent_files = {}    # agentId -> (transcript path, meta path)
        self.detail_loaded = False

    # -- reading ------------------------------------------------------------

    def refresh(self, full):
        """Read what changed. ``full`` also reads agent transcripts."""
        self._refresh_record()
        records, reset = self.journal.read()
        if reset:
            self.agents, self.order, self.events, self.unknown_events = {}, [], 0, {}
        for record in records:
            self._take_event(record)
        self._scan_agent_files()
        if full or not self.finished:
            for agent_id in list(self.agent_files):
                self._refresh_agent(agent_id)

    def _refresh_record(self):
        st = safe_stat(self.record_path)
        if st is None:
            self.record, self.record_mtime = None, None
            return
        if self.record_mtime == st.st_mtime and self.record is not None:
            return
        data, error = read_json_file(self.record_path)
        if error:
            self._note("Run record could not be read (%s)." % error)
            return
        if isinstance(data, dict):
            self.record = data
            self.record_mtime = st.st_mtime

    def _take_event(self, record):
        self.events += 1
        kind = record.get("type")
        agent_id = record.get("agentId")
        if kind == "launched":
            return
        if kind == "started" and agent_id:
            agent = self._agent(agent_id)
            agent["key"] = record.get("key")
            agent["label"] = record.get("label") or agent.get("label")
            agent["phase"] = record.get("phase") or agent.get("phase")
            agent["started"] = True
            if agent["state"] not in ("done", "failed"):
                agent["state"] = "running"
            return
        if kind == "result" and agent_id:
            agent = self._agent(agent_id)
            agent["key"] = agent.get("key") or record.get("key")
            if not agent.get("started"):
                agent["cached"] = True
            result = record.get("result")
            agent["hasResult"] = True
            agent["result"] = result
            agent["state"] = "failed" if result is None else "done"
            if isinstance(record.get("toolsUsed"), list):
                agent["toolsUsed"] = [str(t) for t in record["toolsUsed"]][:50]
            return
        label = str(kind) if kind is not None else "(no type)"
        self.unknown_events[label] = self.unknown_events.get(label, 0) + 1

    def _agent(self, agent_id):
        agent = self.agents.get(agent_id)
        if agent is None:
            agent = {"agentId": agent_id, "state": "running", "order": len(self.order)}
            self.agents[agent_id] = agent
            self.order.append(agent_id)
        return agent

    def _scan_agent_files(self):
        st = safe_stat(self.run_dir)
        if st is None:
            return
        if self.dir_mtime == st.st_mtime and self.agent_files:
            return
        self.dir_mtime = st.st_mtime
        for path in safe_listdir(self.run_dir):
            name = path.name
            if not name.startswith("agent-"):
                continue
            if name.endswith(".meta.json"):
                agent_id = name[len("agent-"):-len(".meta.json")]
            elif name.endswith(".jsonl"):
                agent_id = name[len("agent-"):-len(".jsonl")]
            else:
                continue
            self.agent_files[agent_id] = (self.run_dir / ("agent-%s.jsonl" % agent_id),
                                          self.run_dir / ("agent-%s.meta.json" % agent_id))

    def _refresh_agent(self, agent_id):
        transcript_path, meta_path = self.agent_files[agent_id]
        if agent_id not in self.metas and agent_id not in self.meta_errors:
            data, error = read_json_file(meta_path)
            if isinstance(data, dict):
                self.metas[agent_id] = data
            elif error:
                self.meta_errors.add(agent_id)
        transcript = self.transcripts.get(agent_id)
        if transcript is None:
            transcript = AgentTranscript(transcript_path)
            self.transcripts[agent_id] = transcript
        transcript.refresh()

    def _note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    # -- facts ----------------------------------------------------------------

    @property
    def finished(self):
        return self.record is not None

    def progress_items(self):
        if not self.record:
            return {}
        items = {}
        for item in self.record.get("workflowProgress") or []:
            if isinstance(item, dict) and item.get("type") == "workflow_agent" and item.get("agentId"):
                items[item["agentId"]] = item
        return items

    def last_activity(self):
        times = []
        st = safe_stat(self.journal.path)
        if st:
            times.append(st.st_mtime * 1000)
        for transcript_path, _meta in self.agent_files.values():
            st = safe_stat(transcript_path)
            if st:
                times.append(st.st_mtime * 1000)
        return max(times) if times else None

    def agent_ids(self):
        ids = list(self.order)
        for agent_id in self.progress_items():
            if agent_id not in self.agents:
                ids.append(agent_id)
        for agent_id in self.agent_files:
            if agent_id not in ids:
                ids.append(agent_id)
        return ids

    def agent_view(self, agent_id, now_ms, stale_ms):
        """Summary of one agent, merged from journal, record, meta, transcript."""
        base = self.agents.get(agent_id) or {"agentId": agent_id, "order": 10_000}
        progress = self.progress_items().get(agent_id) or {}
        meta = self.metas.get(agent_id) or {}
        transcript = self.transcripts.get(agent_id)
        view = {
            "agentId": agent_id,
            "order": base.get("order"),
            "key": base.get("key"),
            "label": base.get("label") or progress.get("label") or meta.get("description") or agent_id,
            "phase": base.get("phase") or progress.get("phaseTitle") or meta.get("workflowPhase"),
            "cached": bool(base.get("cached")),
            "hasResult": bool(base.get("hasResult")),
            "resultPreview": preview(base.get("result")) if base.get("hasResult") else (
                progress.get("resultPreview")),
            "toolsUsed": base.get("toolsUsed"),
            "metaMissing": agent_id in self.meta_errors or (
                agent_id in self.agent_files and not self.agent_files[agent_id][1].exists()),
            "model": None,
            "attempt": progress.get("attempt"),
        }
        state = base.get("state")
        if state is None:
            state = progress.get("state") or ("running" if not self.finished else "unknown")
        if state == "running" and self.finished:
            state = progress.get("state") or "unknown"
        view["state"] = state
        model = progress.get("model") or (transcript.model if transcript else None) or meta.get("model")
        view["model"] = model
        start = num(progress.get("startedAt"))
        end = None
        if transcript:
            start = start or transcript.first_time
            view["toolCalls"] = transcript.tool_calls
            context, output = transcript.tokens()
            view["contextTokens"] = context
            view["outputTokens"] = output
            view["lastActivity"] = transcript.last_activity
            view["lastTool"] = transcript.last_tool
            view["lastText"] = transcript.last_text
            view["errors"] = transcript.errors
            if state in ("done", "failed"):
                end = transcript.last_time
        if num(progress.get("toolCalls")) is not None:
            view["toolCalls"] = progress["toolCalls"]
        if num(progress.get("tokens")) is not None:
            view["tokens"] = progress["tokens"]
        if num(progress.get("durationMs")) is not None and start:
            end = start + progress["durationMs"]
        if num(progress.get("lastProgressAt")) is not None:
            view["lastActivity"] = max(view.get("lastActivity") or 0, progress["lastProgressAt"])
        if progress.get("lastToolName") and not view.get("lastTool"):
            view["lastTool"] = {"name": progress.get("lastToolName"),
                                "summary": progress.get("lastToolSummary") or "", "time": None}
        view["start"] = start
        view["end"] = end
        if state == "running":
            last = view.get("lastActivity")
            view["quiet"] = bool(last and now_ms - last > stale_ms)
        return view


# --------------------------------------------------------------------------
# The store: every project, session and run under the root
# --------------------------------------------------------------------------

class Store:
    def __init__(self, root, stale_minutes=DEFAULT_STALE_MINUTES, scan_days=DEFAULT_SCAN_DAYS):
        self.root = Path(root)
        self.stale_ms = stale_minutes * 60_000
        self.scan_days = scan_days
        self.lock = threading.RLock()
        self.runs = {}            # run id -> Run
        self.sessions = {}        # session jsonl path -> SessionIndex
        self.last_refresh = None   # monotonic() may start near 0 in a new process
        self.notes = []
        self.scan_count = 0

    # -- scanning -------------------------------------------------------------

    def refresh(self, force=False):
        with self.lock:
            now = time.monotonic()
            if (not force and self.last_refresh is not None
                    and now - self.last_refresh < REFRESH_INTERVAL):
                return
            self.last_refresh = now
            self.scan_count += 1
            self.notes = []
            if not self.root.is_dir():
                self.notes.append("The root folder %s does not exist or cannot be read." % self.root)
                return
            cutoff = time.time() - self.scan_days * 86400
            for project in safe_listdir(self.root):
                if not project.is_dir():
                    continue
                self._scan_project(project, cutoff)
            for run in self.runs.values():
                try:
                    run.refresh(full=run.detail_loaded)
                except Exception as exc:  # pragma: no cover - last line of defence
                    run._note("Reading stopped with %s." % exc.__class__.__name__)

    def _scan_project(self, project, cutoff):
        entries = safe_listdir(project)
        session_dirs = {p.name: p for p in entries if p.is_dir()}
        for path in entries:
            if path.suffix != ".jsonl" or not path.is_file():
                continue
            st = safe_stat(path)
            has_runs = (project / path.stem / "workflows").is_dir() or (
                project / path.stem / "subagents" / "workflows").is_dir()
            if st is None or (st.st_mtime < cutoff and path not in self.sessions):
                continue
            if not has_runs and st.st_mtime < cutoff:
                continue
            index = self.sessions.get(path)
            if index is None:
                index = SessionIndex(path, project.name)
                self.sessions[path] = index
            index.refresh()
        for session_id, session_dir in session_dirs.items():
            for run_id in self._run_ids(session_dir):
                if run_id not in self.runs:
                    self.runs[run_id] = Run(run_id, project.name, session_dir)

    @staticmethod
    def _run_ids(session_dir):
        ids = set()
        for path in safe_listdir(session_dir / "workflows"):
            if path.suffix == ".json" and RUN_ID_RE.match(path.stem):
                ids.add(path.stem)
        for path in safe_listdir(session_dir / "subagents" / "workflows"):
            if path.is_dir() and RUN_ID_RE.match(path.name):
                ids.add(path.name)
        return ids

    # -- facts from the session transcripts ------------------------------------

    def launch_for(self, run_id):
        """(launch, launch result, session index) for a run id."""
        for index in self.sessions.values():
            for tool_use_id, result in index.launch_results.items():
                if isinstance(result, dict) and result.get("runId") == run_id:
                    return index.launches.get(tool_use_id), result, index
        return None, None, None

    def notifications_for(self, run_id, task_id):
        found = []
        for index in self.sessions.values():
            if task_id and task_id in index.notifications:
                found.extend(index.notifications[task_id])
            else:
                for notes in index.notifications.values():
                    for note in notes:
                        if note.get("summary") and run_id in note["summary"]:
                            found.append(note)
        found.sort(key=lambda n: n.get("time") or 0)
        return found

    def pending_launches(self, now_ms):
        """Workflow calls that have no result yet (waiting for approval or
        just starting) in a session that was written recently."""
        pending = []
        for index in self.sessions.values():
            mtime = (index.tail.mtime or 0) * 1000
            if now_ms - mtime > self.stale_ms:
                continue
            for tool_use_id, launch in index.launches.items():
                if tool_use_id in index.launch_results:
                    continue
                pending.append((tool_use_id, launch, index))
        return pending

    # -- views ------------------------------------------------------------------

    def run_summary(self, run, now_ms):
        launch, launch_result, index = self.launch_for(run.run_id)
        record = run.record or {}
        task_id = record.get("taskId") or (launch_result or {}).get("taskId")
        notes = self.notifications_for(run.run_id, task_id)
        name = record.get("workflowName") or (launch_result or {}).get("workflowName")
        if not name and launch:
            name = launch["input"].get("name")
        summary = record.get("summary") or (launch_result or {}).get("summary")
        start = num(record.get("startTime")) or (launch_result or {}).get("_time") or (
            launch or {}).get("time")
        agent_views = [run.agent_view(a, now_ms, self.stale_ms) for a in run.agent_ids()]
        if not start:
            starts = [v["start"] for v in agent_views if v.get("start")]
            start = min(starts) if starts else None
        last_activity = run.last_activity()
        status, status_note = self._status(run, notes, last_activity, now_ms)
        if status not in ("running", "starting"):
            # the run is over: an agent without a result did not finish
            for view in agent_views:
                if view["state"] == "running":
                    view["state"] = "stopped"
                    view["quiet"] = False
        end = None
        if run.finished:
            if num(record.get("durationMs")) is not None and start:
                end = start + record["durationMs"]
            else:
                end = iso_ms(record.get("timestamp"))
        elif status in ("stopped", "probably stopped") and last_activity:
            end = last_activity
        started = len([v for v in agent_views if v["state"] != "unknown"])
        done = len([v for v in agent_views if v["state"] in ("done", "failed")])
        running = len([v for v in agent_views if v["state"] == "running"])
        tokens = num(record.get("totalTokens"))
        tokens_exact = tokens is not None
        if tokens is None:
            known = [v.get("tokens") or v.get("contextTokens") for v in agent_views]
            known = [k for k in known if k]
            tokens = sum(known) if known else None
        tool_calls = num(record.get("totalToolCalls"))
        if tool_calls is None:
            counted = [v.get("toolCalls") for v in agent_views if v.get("toolCalls") is not None]
            tool_calls = sum(counted) if counted else None
        cwd = (index.cwd if index else None) or self._session_cwd(run)
        return {
            "id": run.run_id,
            "runId": run.run_id,
            "kind": "run",
            "name": name or "(unnamed workflow)",
            "summary": summary,
            "project": run.project,
            "cwd": cwd,
            "sessionId": run.session_id,
            "taskId": task_id,
            "status": status,
            "statusNote": status_note,
            "recordStatus": record.get("status"),
            "start": start,
            "end": end,
            "lastActivity": last_activity,
            "agentsStarted": max(started, num(record.get("agentCount")) or 0),
            "agentsDone": done,
            "agentsRunning": running,
            "tokens": tokens,
            "tokensExact": tokens_exact,
            "toolCalls": tool_calls,
            "hasRecord": run.finished,
            "notifications": [{"status": n.get("status"), "time": n.get("time"),
                               "summary": n.get("summary")} for n in notes],
        }, agent_views

    def _session_cwd(self, run):
        for transcript in run.transcripts.values():
            if transcript.cwd:
                return transcript.cwd
        index = self.sessions.get(run.session_dir.with_suffix(".jsonl"))
        if index is None:
            path = run.session_dir.parent / (run.session_id + ".jsonl")
            if path.is_file():
                index = SessionIndex(path, run.project)
                index._find_cwd()
                return index.cwd
            return None
        return index.cwd

    def _status(self, run, notes, last_activity, now_ms):
        if run.finished:
            status = run.record.get("status") or "finished"
            return ("finished" if status == "completed" else str(status)), None
        note = None
        if notes:
            latest = notes[-1]
            reported = latest.get("status")
            # Observed: after a session restart Claude Code reports a run as
            # "stopped" while its agents go on writing. Files that change
            # after the report win over the report.
            if reported and reported != "running":
                moved_on = (last_activity and latest.get("time")
                            and last_activity > latest["time"] + NOTIFY_GRACE_MS)
                if not moved_on:
                    return str(reported), latest.get("summary")
                note = ("Claude Code reported this run as %s at %s, but its files kept "
                        "changing after that." % (reported, _clock(latest["time"])))
        if last_activity is None:
            return "starting", note
        if now_ms - last_activity > self.stale_ms:
            minutes = int((now_ms - last_activity) / 60000)
            return "probably stopped", "No file of this run changed for %d minutes." % minutes
        return "running", note

    def list_runs(self):
        self.refresh()
        now_ms = time.time() * 1000
        with self.lock:
            runs = []
            for run in self.runs.values():
                try:
                    summary, _agents = self.run_summary(run, now_ms)
                except Exception as exc:  # pragma: no cover
                    summary = {"id": run.run_id, "runId": run.run_id, "kind": "run",
                               "name": run.run_id, "status": "unreadable",
                               "statusNote": exc.__class__.__name__, "project": run.project}
                runs.append(summary)
            for tool_use_id, launch, index in self.pending_launches(now_ms):
                runs.append(self._pending_summary(tool_use_id, launch, index))
            runs.sort(key=lambda r: r.get("start") or r.get("lastActivity") or 0, reverse=True)
            return {"now": now_ms, "root": str(self.root), "runs": runs,
                    "notes": list(self.notes), "staleMinutes": self.stale_ms / 60000}

    def _pending_summary(self, tool_use_id, launch, index):
        script, _origin, _note = self._launch_script(launch)
        name = launch["input"].get("name")
        if not name and script:
            match = SCRIPT_NAME_RE.search(script)
            name = match.group(2) if match else None
        return {
            "id": "pending-" + tool_use_id,
            "kind": "pending",
            "name": name or "(new workflow)",
            "summary": "Waiting for approval or starting.",
            "project": index.project,
            "cwd": launch.get("cwd") or index.cwd,
            "sessionId": index.session_id,
            "status": "awaiting launch",
            "start": launch.get("time"),
            "lastActivity": launch.get("time"),
            "agentsStarted": 0, "agentsDone": 0, "agentsRunning": 0,
        }

    def _launch_script(self, launch):
        """(script text, where it came from, note) for a Workflow call."""
        if not launch:
            return None, None, None
        tool_input = launch.get("input") or {}
        if isinstance(tool_input.get("script"), str):
            return tool_input["script"], "launch", None
        path_text = tool_input.get("scriptPath")
        if isinstance(path_text, str) and path_text:
            text, error = read_text_file(Path(path_text))
            if text is not None:
                return text, "file", ("The script was read from %s now; it may have been "
                                      "edited after the run started." % path_text)
            return None, None, "The script file %s could not be read (%s)." % (path_text, error)
        if isinstance(tool_input.get("name"), str):
            return None, None, "Saved workflow %s: its script is not in the transcript." % tool_input["name"]
        return None, None, None

    def run_detail(self, run_id, version=None):
        self.refresh()
        now_ms = time.time() * 1000
        with self.lock:
            if run_id.startswith("pending-"):
                return self._pending_detail(run_id[len("pending-"):], now_ms)
            run = self.runs.get(run_id)
            if run is None:
                return None
            if not run.detail_loaded:
                run.detail_loaded = True
                run.refresh(full=True)
            summary, agents = self.run_summary(run, now_ms)
            agents.sort(key=lambda a: (a.get("order") if a.get("order") is not None else 10_000,
                                       a.get("start") or 0))
            stamp = self._version(run, summary)
            if version and version == stamp:
                return {"unchanged": True, "version": stamp, "now": now_ms}
            record = run.record or {}
            notes = list(run.notes)
            if run.journal.bad_lines:
                notes.append("%d journal line(s) could not be read." % run.journal.bad_lines)
            for kind, count in run.unknown_events.items():
                notes.append("Journal has %d event(s) of an unknown type %r; they were skipped."
                             % (count, kind))
            missing_meta = [a["label"] for a in agents if a.get("metaMissing")]
            if missing_meta:
                notes.append("No meta file for: %s." % ", ".join(missing_meta[:10]))
            bad = sum(t.tail.bad_lines for t in run.transcripts.values())
            if bad:
                notes.append("%d agent transcript line(s) could not be read." % bad)
            if not run.run_dir.is_dir() and not run.finished:
                notes.append("The run folder is missing.")
            return {
                "version": stamp,
                "now": now_ms,
                "run": summary,
                "agents": agents,
                "phases": record.get("phases") if isinstance(record.get("phases"), list) else None,
                "logs": record.get("logs") if isinstance(record.get("logs"), list) else None,
                "result": record.get("result") if run.finished else None,
                "hasResult": run.finished and "result" in record,
                "notes": notes,
            }

    def _version(self, run, summary):
        parts = [summary["status"], str(run.record_mtime), str(run.journal.offset),
                 str(len(summary.get("notifications") or []))]
        for agent_id in sorted(run.transcripts):
            parts.append("%s:%d" % (agent_id, run.transcripts[agent_id].tail.offset))
        for agent_id in sorted(run.agent_files):
            parts.append(agent_id)
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]

    def run_static(self, run_id):
        """Script source, args and launch facts: fetched once per page view."""
        self.refresh()
        with self.lock:
            if run_id.startswith("pending-"):
                found = self._find_pending(run_id[len("pending-"):])
                if not found:
                    return None
                launch, index = found
                script, origin, note = self._launch_script(launch)
                return {"script": script, "scriptOrigin": origin, "scriptNote": note,
                        "args": launch["input"].get("args"), "hasArgs": "args" in launch["input"]}
            run = self.runs.get(run_id)
            if run is None:
                return None
            record = run.record or {}
            launch, launch_result, _index = self.launch_for(run_id)
            script, origin, note = None, None, None
            if isinstance(record.get("script"), str):
                script, origin = record["script"], "record"
            if script is None and launch:
                script, origin, note = self._launch_script(launch)
            if script is None:
                script_path = record.get("scriptPath") or (launch_result or {}).get("scriptPath")
                if isinstance(script_path, str) and script_path:
                    text, error = read_text_file(Path(script_path))
                    if text is not None:
                        script, origin = text, "file"
                        note = ("The script was read from %s now; it may have been edited "
                                "after the run started." % script_path)
            if script is None:
                for path in safe_listdir(run.session_dir / "workflows" / "scripts"):
                    if path.name.endswith("-%s.js" % run_id):
                        text, _error = read_text_file(path)
                        if text is not None:
                            script, origin = text, "file"
            args_known = "args" in record or bool(launch and "args" in launch["input"])
            args = record.get("args") if "args" in record else (
                launch["input"].get("args") if launch else None)
            return {"script": script, "scriptOrigin": origin, "scriptNote": note,
                    "args": args, "hasArgs": args_known,
                    "resumeFrom": (launch or {}).get("input", {}).get("resumeFromRunId")}

    def _find_pending(self, tool_use_id):
        for index in self.sessions.values():
            if tool_use_id in index.launches and tool_use_id not in index.launch_results:
                return index.launches[tool_use_id], index
        return None

    def _pending_detail(self, tool_use_id, now_ms):
        found = self._find_pending(tool_use_id)
        if not found:
            # it was launched meanwhile: point the page at the run
            for index in self.sessions.values():
                result = index.launch_results.get(tool_use_id)
                if isinstance(result, dict):
                    return {"moved": result.get("runId"), "now": now_ms,
                            "error": result.get("error")}
            return None
        launch, index = found
        summary = self._pending_summary(tool_use_id, launch, index)
        return {"version": "pending", "now": now_ms, "run": summary, "agents": [],
                "phases": None, "logs": None, "result": None, "hasResult": False,
                "notes": []}

    def step_detail(self, run_id, agent_id, index):
        """The full input and result of one step, read again from the file at
        the place the step was found, so long texts are not kept in memory."""
        self.refresh()
        with self.lock:
            run = self.runs.get(run_id)
            transcript = run.transcripts.get(agent_id) if run else None
            if transcript is None or not 0 <= index < len(transcript.steps):
                return None
            step = dict(transcript.steps[index])
            path = transcript.tail.path
        detail = {"i": index, "kind": step.get("kind"), "name": step.get("name"),
                  "time": step.get("time"), "end": step.get("end"),
                  "isError": bool(step.get("isError")), "notes": []}
        record = read_line_at(path, step.get("off"))
        if step.get("kind") == "tool":
            block = find_block(record, "tool_use", lambda b: b.get("id") == step.get("toolId"))
            if block is None:
                detail["notes"].append("The call could not be read again from the transcript.")
                detail["input"] = None
            else:
                detail["input"] = clip_fields(block.get("input"))
            if step.get("roff") is not None:
                result_record = read_line_at(path, step.get("roff"))
                block = find_block(result_record, "tool_result",
                                   lambda b: b.get("tool_use_id") == step.get("toolId"))
                if block is not None:
                    detail["result"] = clip(block_text(block.get("content")), MAX_FIELD)
                    detail["isError"] = bool(block.get("is_error"))
                    images = [b for b in block.get("content") or [] if isinstance(b, dict)
                              and b.get("type") == "image"] if isinstance(block.get("content"), list) else []
                    if images:
                        detail["notes"].append("The result holds %d image(s), not shown." % len(images))
            return detail
        if step.get("kind") in ("text", "thinking"):
            key = "text" if step["kind"] == "text" else "thinking"
            block = find_block(record, step["kind"], lambda b: isinstance(b.get(key), str)
                               and b.get(key)[:200] == (step.get("text") or "")[:200])
            detail["text"] = clip(block.get(key), MAX_FIELD) if block else step.get("text")
            return detail
        detail["text"] = step.get("text")
        return detail

    def agent_detail(self, run_id, agent_id, start_step=0):
        self.refresh()
        now_ms = time.time() * 1000
        with self.lock:
            run = self.runs.get(run_id)
            if run is None or (agent_id not in run.agents and agent_id not in run.agent_files
                               and agent_id not in run.progress_items()):
                return None
            if not run.detail_loaded:
                run.detail_loaded = True
                run.refresh(full=True)
            view = run.agent_view(agent_id, now_ms, self.stale_ms)
            transcript = run.transcripts.get(agent_id)
            base = run.agents.get(agent_id) or {}
            steps = transcript.steps[start_step:] if transcript else []
            progress = run.progress_items().get(agent_id) or {}
            meta = run.metas.get(agent_id)
            result = base.get("result") if base.get("hasResult") else None
            if result is None and transcript and transcript.structured is not None and base.get("hasResult"):
                result = transcript.structured
            return {
                "now": now_ms,
                "agent": view,
                "meta": meta,
                "prompt": (transcript.prompt if transcript else None) or progress.get("promptPreview")
                if start_step == 0 else None,
                "steps": steps,
                "stepCount": len(transcript.steps) if transcript else 0,
                "from": start_step,
                "result": result,
                "hasResult": bool(base.get("hasResult")),
                "cwd": transcript.cwd if transcript else None,
                "gitBranch": transcript.git_branch if transcript else None,
            }


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "workflow-watch/" + VERSION
    store = None          # set by make_server
    allowed_hosts = ()

    def log_message(self, fmt, *args):  # keep the terminal quiet
        return

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.allowed_hosts

    def _send(self, status, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                         "form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
        self._send(status, body, "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            self._send(403, "Forbidden host.\n", "text/plain; charset=utf-8")
            return
        url = urlparse(self.path)
        query = parse_qs(url.query)

        def arg(name, default=None):
            values = query.get(name)
            return values[0] if values else default

        path = url.path
        try:
            if path in ("/", "/index.html"):
                self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            elif path == "/app.css":
                self._send(200, APP_CSS, "text/css; charset=utf-8")
            elif path == "/app.js":
                self._send(200, APP_JS, "text/javascript; charset=utf-8")
            elif path == "/highlight.js":
                self._send(200, HIGHLIGHT_JS, "text/javascript; charset=utf-8")
            elif path == "/graph.js":
                self._send(200, GRAPH_JS, "text/javascript; charset=utf-8")
            elif path == "/acorn.js":
                self._send(200, ACORN_JS, "text/javascript; charset=utf-8")
            elif path == "/api/runs":
                self._json(self.store.list_runs())
            elif path == "/api/run":
                data = self.store.run_detail(arg("id", ""), arg("v"))
                self._json(data if data is not None else {"error": "unknown run"},
                           200 if data is not None else 404)
            elif path == "/api/static":
                data = self.store.run_static(arg("id", ""))
                self._json(data if data is not None else {"error": "unknown run"},
                           200 if data is not None else 404)
            elif path == "/api/step":
                try:
                    index = int(arg("i", "-1"))
                except ValueError:
                    index = -1
                data = self.store.step_detail(arg("run", ""), arg("agent", ""), index)
                self._json(data if data is not None else {"error": "unknown step"},
                           200 if data is not None else 404)
            elif path == "/api/agent":
                try:
                    start = max(0, int(arg("from", "0")))
                except ValueError:
                    start = 0
                data = self.store.agent_detail(arg("run", ""), arg("agent", ""), start)
                self._json(data if data is not None else {"error": "unknown agent"},
                           200 if data is not None else 404)
            else:
                self._send(404, "Not found.\n", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            return


def make_server(store, port):
    handler = type("BoundHandler", (Handler,), {})
    handler.store = store
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    real_port = server.server_address[1]
    handler.allowed_hosts = ("127.0.0.1:%d" % real_port, "localhost:%d" % real_port)
    return server


def default_root():
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config).expanduser() if config else Path.home() / ".claude"
    return base / "projects"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Live viewer for Claude Code Workflow runs (read only, local only).")
    parser.add_argument("--port", type=int, default=None,
                        help="port on 127.0.0.1 (default %d, or the next free one)" % DEFAULT_PORT)
    parser.add_argument("--root", default=None,
                        help="the Claude projects folder (default: $CLAUDE_CONFIG_DIR/projects "
                             "or ~/.claude/projects)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--stale-minutes", type=float, default=DEFAULT_STALE_MINUTES,
                        help="silence after which a run without a record is shown as "
                             "'probably stopped' (default %d)" % DEFAULT_STALE_MINUTES)
    parser.add_argument("--scan-days", type=float, default=DEFAULT_SCAN_DAYS,
                        help="read session transcripts changed in the last N days "
                             "(default %d); older runs show from their records" % DEFAULT_SCAN_DAYS)
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser() if args.root else default_root()
    store = Store(root, args.stale_minutes, args.scan_days)
    store.refresh(force=True)

    ports = [args.port] if args.port is not None else range(DEFAULT_PORT, DEFAULT_PORT + 20)
    server, error = None, None
    for port in ports:
        try:
            server = make_server(store, port)
            break
        except OSError as exc:
            error = exc
    if server is None:
        print("Could not open a port: %s" % error, file=sys.stderr)
        return 1
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("workflow-watch %s" % VERSION)
    print("Reading  %s" % root)
    print("Serving  %s   (Ctrl+C stops it)" % url)
    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workflow Watch</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="/app.css">
</head>
<body>
<header class="top">
  <a class="brand" href="#/">Workflow Watch</a>
  <span id="crumb" class="crumb"></span>
  <span class="spacer"></span>
  <span id="pulse" class="pulse" title="Last update"></span>
</header>
<main id="main"></main>
<aside id="drawer" class="drawer" hidden></aside>
<script src="/acorn.js"></script>
<script src="/highlight.js"></script>
<script src="/graph.js"></script>
<script src="/app.js"></script>
</body>
</html>
"""

APP_CSS = r""":root {
  --bg: #f6f7f9;
  --panel: #ffffff;
  --panel-2: #f0f2f5;
  --text: #1d2330;
  --muted: #5f6b7a;
  --faint: #8a94a3;
  --line: #d9dee5;
  --line-strong: #b9c1cc;
  --accent: #2f6fde;
  --run: #17984a;
  --run-bg: #e8f7ee;
  --done: #3c6e9e;
  --done-bg: #eef3f8;
  --fail: #c93a3a;
  --fail-bg: #fbeceb;
  --quiet: #b7791f;
  --quiet-bg: #fdf5e6;
  --idle: #9aa3ae;
  --edge: #9aa5b3;
  --code-bg: #f3f4f6;
  --shadow: 0 1px 2px rgba(20, 30, 50, .06), 0 2px 8px rgba(20, 30, 50, .05);
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14171c;
    --panel: #1c2027;
    --panel-2: #232831;
    --text: #e3e7ed;
    --muted: #a3adba;
    --faint: #7c8796;
    --line: #2f3540;
    --line-strong: #46505e;
    --accent: #6ea0ff;
    --run: #3ccf74;
    --run-bg: #16301f;
    --done: #8fb5dc;
    --done-bg: #1d2733;
    --fail: #ff7b72;
    --fail-bg: #3a1f1f;
    --quiet: #e3b04b;
    --quiet-bg: #33291a;
    --idle: #6b7583;
    --edge: #5d6876;
    --code-bg: #171a20;
    --shadow: 0 1px 2px rgba(0, 0, 0, .3);
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #14171c; --panel: #1c2027; --panel-2: #232831; --text: #e3e7ed; --muted: #a3adba;
  --faint: #7c8796; --line: #2f3540; --line-strong: #46505e; --accent: #6ea0ff; --run: #3ccf74;
  --run-bg: #16301f; --done: #8fb5dc; --done-bg: #1d2733; --fail: #ff7b72; --fail-bg: #3a1f1f;
  --quiet: #e3b04b; --quiet-bg: #33291a; --idle: #6b7583; --edge: #5d6876; --code-bg: #171a20;
  --shadow: 0 1px 2px rgba(0, 0, 0, .3); color-scheme: dark;
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; }
pre { margin: 0; white-space: pre-wrap; word-break: break-word; }
button {
  font: inherit; color: var(--text); background: var(--panel); border: 1px solid var(--line-strong);
  border-radius: 6px; padding: 3px 10px; cursor: pointer;
}
button:hover { border-color: var(--accent); }
select, input[type="search"] {
  font: inherit; color: var(--text); background: var(--panel); border: 1px solid var(--line-strong);
  border-radius: 6px; padding: 3px 8px; max-width: 100%;
}

.top {
  position: sticky; top: 0; z-index: 20; display: flex; align-items: center; gap: 12px;
  padding: 10px 16px; background: var(--panel); border-bottom: 1px solid var(--line);
}
.brand { font-weight: 650; color: var(--text); }
.crumb { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.spacer { flex: 1; }
.pulse { font-size: 12px; color: var(--faint); }
.pulse.bad { color: var(--fail); }

main { padding: 16px; max-width: 1500px; margin: 0 auto; }
.section { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; box-shadow: var(--shadow); margin-bottom: 16px; }
.section > h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 0; padding: 10px 14px; border-bottom: 1px solid var(--line); font-weight: 600; }
.section > .body { padding: 12px 14px; }
.note { color: var(--quiet); background: var(--quiet-bg); border-radius: 6px; padding: 6px 10px; margin: 0 0 8px; font-size: 13px; }
.empty { color: var(--muted); padding: 24px; text-align: center; }

/* status */
.badge { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; padding: 1px 8px; border-radius: 999px; background: var(--panel-2); color: var(--muted); white-space: nowrap; }
.badge::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
.st-running { color: var(--run); background: var(--run-bg); }
.st-running::before { animation: blink 1.6s ease-in-out infinite; }
.st-finished, .st-done, .st-completed { color: var(--done); background: var(--done-bg); }
.st-failed, .st-stopped, .st-killed, .st-error { color: var(--fail); background: var(--fail-bg); }
.st-quiet, .st-probably-stopped, .st-awaiting-launch, .st-starting { color: var(--quiet); background: var(--quiet-bg); }
@keyframes blink { 50% { opacity: .35; } }
@media (prefers-reduced-motion: reduce) { .st-running::before, .gnode.running { animation: none !important; } }

/* run list */
.filters { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 12px; }
.filters label { color: var(--muted); font-size: 13px; display: inline-flex; gap: 6px; align-items: center; }
table.grid { width: 100%; border-collapse: collapse; }
table.grid th { text-align: left; font-weight: 600; font-size: 12px; color: var(--muted); padding: 8px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
table.grid td { padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
table.grid tr.click { cursor: pointer; }
table.grid tr.click:hover td { background: var(--panel-2); }
table.grid tr.is-running td:first-child { box-shadow: inset 3px 0 0 var(--run); }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.sub { color: var(--muted); font-size: 12px; }
.wrap-scroll { overflow-x: auto; }
.name-cell { font-weight: 600; }

/* run header */
.head { display: grid; gap: 6px; }
.head h1 { font-size: 20px; margin: 0; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.head .desc { color: var(--muted); }
.facts { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 8px 16px; margin-top: 8px; }
.fact .k { font-size: 11px; color: var(--faint); text-transform: uppercase; letter-spacing: .04em; }
.fact .v { font-variant-numeric: tabular-nums; word-break: break-all; }
.phase-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.chip { font-size: 12px; border: 1px solid var(--line); border-radius: 999px; padding: 1px 10px; color: var(--muted); background: var(--panel-2); }
.chip b { color: var(--text); font-weight: 600; }

/* graph */
.graph-tools { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.graph-tools .sub { margin-left: 8px; }
.graph-wrap { overflow: auto; max-height: 78vh; background:
  radial-gradient(circle, var(--line) 1px, transparent 1px) 0 0 / 18px 18px; border-radius: 0 0 10px 10px; }
.canvas { position: relative; transform-origin: 0 0; }
.canvas svg { position: absolute; left: 0; top: 0; overflow: visible; }
.edge { fill: none; stroke: var(--edge); stroke-width: 1.4; }
.edge.back { stroke-dasharray: 4 4; }
.edge-label { position: absolute; transform: translateX(-50%); font-size: 11px; color: var(--muted); background: var(--bg); padding: 0 4px; border-radius: 4px; white-space: nowrap; }

.gnode { position: absolute; border-radius: 9px; background: var(--panel); border: 1.5px solid var(--line-strong); box-shadow: var(--shadow); padding: 6px 10px; overflow: hidden; cursor: pointer; }
.gnode:hover { border-color: var(--accent); }
.gnode .l1 { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 13px; }
.gnode .l2, .gnode .l3 { font-size: 11.5px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.gnode.idle { border-style: dashed; border-color: var(--line-strong); background: var(--panel); opacity: .78; }
.gnode.running { border: 2.5px solid var(--run); background: var(--run-bg); animation: glow 2s ease-in-out infinite; }
.gnode.quiet { border: 2.5px solid var(--quiet); background: var(--quiet-bg); }
.gnode.done { border-color: var(--done); }
.gnode.failed { border: 2px solid var(--fail); background: var(--fail-bg); }
.gnode.ended { border-color: var(--idle); opacity: .85; }
.gnode.selected { outline: 3px solid var(--accent); outline-offset: 2px; }
@keyframes glow { 50% { box-shadow: 0 0 0 5px rgba(23, 152, 74, .16); } }
.gnode .tick-mark { float: right; font-size: 12px; }
.gnode.phase { cursor: default; border: none; box-shadow: none; background: transparent; text-align: center; padding: 4px 0; }
.gnode.phase .l1 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); border-bottom: 1px solid var(--line-strong); }
.gnode.exit { border-radius: 999px; text-align: center; padding: 4px 8px; cursor: default; background: var(--panel-2); }
.gnode.exit .l1 { font-weight: 500; font-size: 12px; color: var(--muted); }
.gnode.branch { cursor: default; border-radius: 6px; background: var(--panel-2); text-align: center; padding: 3px 10px; }
.gnode.branch .l1 { font-weight: 500; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.gnode.branch .l2 { font-size: 10.5px; }
.gnode.fork { cursor: default; border-radius: 6px; background: var(--panel-2); text-align: center; padding: 2px 8px; }
.gnode.fork .l1 { font-size: 11.5px; font-weight: 500; color: var(--muted); }
.gnode.container { cursor: default; background: transparent; box-shadow: none; border: 1.5px dashed var(--line-strong); padding: 4px 10px; overflow: visible; }
.gnode.container.group { border-style: solid; }
.gnode.container.running { border: 2px dashed var(--run); background: transparent; animation: none; }
.gnode.container .l1 { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; font-size: 12px; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.gnode.container .count { float: right; margin-left: 8px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--text); }

/* agents table */
.phase-row td { background: var(--panel-2); font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.now-line { font-size: 12px; color: var(--muted); max-width: 520px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* drawer */
@media (min-width: 1100px) {
  body.with-drawer main { margin-right: min(760px, 50vw); max-width: none; }
  body.with-drawer .drawer { width: min(760px, 50vw); }
}
.drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(760px, 100vw); background: var(--panel); border-left: 1px solid var(--line); box-shadow: -6px 0 24px rgba(0,0,0,.12); z-index: 40; display: flex; flex-direction: column; }
.drawer[hidden] { display: none; }
.drawer .dhead { padding: 12px 16px; border-bottom: 1px solid var(--line); display: flex; gap: 10px; align-items: flex-start; }
.drawer .dhead h3 { margin: 0; font-size: 16px; word-break: break-all; }
.drawer .dbody { overflow: auto; overflow-anchor: auto; padding: 12px 16px 40px; flex: 1; }
.drawer h4 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 18px 0 6px; }
.now-box { border: 1.5px solid var(--run); background: var(--run-bg); border-radius: 8px; padding: 8px 10px; }
.now-box.quiet { border-color: var(--quiet); background: var(--quiet-bg); }
.instances { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.instances button.on { border-color: var(--accent); background: var(--panel-2); font-weight: 600; }
.steps { list-style: none; margin: 0; padding: 0; }
.steps li { border-bottom: 1px solid var(--line); padding: 5px 0; }
.steps .srow { display: flex; gap: 8px; align-items: baseline; cursor: pointer; }
.steps .stime { color: var(--faint); font-size: 11.5px; font-variant-numeric: tabular-nums; flex: none; width: 64px; }
.steps .skind { font-size: 11px; font-weight: 600; flex: none; min-width: 74px; max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.steps .stext { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
.steps li.open .stext { white-space: normal; }
.steps li.open.k-tool .stext { white-space: nowrap; }
.steps .sdetail { margin: 6px 0 4px 82px; background: var(--code-bg); border-radius: 6px; padding: 6px 8px; max-height: 420px; overflow: auto; }
.steps li.err .skind, .steps li.err .stext { color: var(--fail); }
.steps li.k-text .stext { color: var(--text); }
.steps li.k-thinking { color: var(--faint); }
.box { background: var(--code-bg); border-radius: 6px; padding: 8px 10px; max-height: 480px; overflow: auto; }
details > summary { cursor: pointer; color: var(--muted); font-size: 13px; }
details[open] > summary { margin-bottom: 6px; }

/* value renderer */
.val-obj { margin: 0; display: grid; grid-template-columns: minmax(80px, max-content) 1fr; gap: 3px 10px; }
.val-obj > dt { color: var(--muted); font-size: 12.5px; font-weight: 600; word-break: break-word; }
.val-obj > dd { margin: 0; min-width: 0; }
.val-list { margin: 0; padding-left: 20px; }
.val-list > li { margin: 2px 0; }
.val-str { white-space: pre-wrap; word-break: break-word; }
.val-lit { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; color: var(--done); }
.val-nested { border-left: 2px solid var(--line); padding-left: 8px; }

@media (max-width: 720px) {
  main { padding: 10px; }
  .facts { grid-template-columns: 1fr 1fr; }
  .hide-sm { display: none; }
  .steps .sdetail { margin-left: 0; }
}

/* ---- code, terminal, diff ---- */
:root {
  --tk-keyword: #cf222e; --tk-string: #0a3069; --tk-comment: #6e7781; --tk-number: #0550ae;
  --tk-fn: #8250df; --tk-type: #953800; --tk-prop: #0550ae; --tk-literal: #0550ae; --tk-builtin: #8250df;
  --tk-var: #953800; --tk-tag: #116329; --tk-attr: #0550ae; --tk-op: #cf222e; --tk-deco: #8250df;
  --tk-add: #1a7f37; --tk-del: #cf222e; --tk-warn: #9a6700;
  --code-text: #1f2328; --code-bg2: #f6f8fa; --code-line: #d0d7de; --ln: #8c959f;
  --add-bg: #dafbe1; --del-bg: #ffebe9; --add-sign: #1a7f37; --del-sign: #cf222e;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --tk-keyword: #ff7b72; --tk-string: #a5d6ff; --tk-comment: #8b949e; --tk-number: #79c0ff;
    --tk-fn: #d2a8ff; --tk-type: #ffa657; --tk-prop: #79c0ff; --tk-literal: #79c0ff; --tk-builtin: #d2a8ff;
    --tk-var: #ffa657; --tk-tag: #7ee787; --tk-attr: #79c0ff; --tk-op: #ff7b72; --tk-deco: #d2a8ff;
    --tk-add: #3fb950; --tk-del: #f85149; --tk-warn: #d29922;
    --code-text: #e6edf3; --code-bg2: #0d1117; --code-line: #30363d; --ln: #6e7681;
    --add-bg: rgba(46, 160, 67, .18); --del-bg: rgba(248, 81, 73, .18); --add-sign: #3fb950; --del-sign: #f85149;
  }
}
:root[data-theme="dark"] {
  --tk-keyword: #ff7b72; --tk-string: #a5d6ff; --tk-comment: #8b949e; --tk-number: #79c0ff;
  --tk-fn: #d2a8ff; --tk-type: #ffa657; --tk-prop: #79c0ff; --tk-literal: #79c0ff; --tk-builtin: #d2a8ff;
  --tk-var: #ffa657; --tk-tag: #7ee787; --tk-attr: #79c0ff; --tk-op: #ff7b72; --tk-deco: #d2a8ff;
  --tk-add: #3fb950; --tk-del: #f85149; --tk-warn: #d29922;
  --code-text: #e6edf3; --code-bg2: #0d1117; --code-line: #30363d; --ln: #6e7681;
  --add-bg: rgba(46, 160, 67, .18); --del-bg: rgba(248, 81, 73, .18); --add-sign: #3fb950; --del-sign: #f85149;
}
/* the terminal is dark in both themes */
.terminal, .code.term {
  --tk-keyword: #ff7b72; --tk-string: #a5d6ff; --tk-comment: #8b949e; --tk-number: #79c0ff;
  --tk-fn: #7ee787; --tk-type: #ffa657; --tk-prop: #79c0ff; --tk-literal: #79c0ff; --tk-builtin: #d2a8ff;
  --tk-var: #ffa657; --tk-tag: #7ee787; --tk-attr: #79c0ff; --tk-op: #ff7b72; --tk-deco: #d2a8ff;
  --tk-add: #3fb950; --tk-del: #ff7b72; --tk-warn: #e3b341;
  --code-text: #e6edf3; --code-bg2: #0d1117; --code-line: #30363d; --ln: #6e7681;
}
.code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; line-height: 1.5;
  background: var(--code-bg2); color: var(--code-text); border: 1px solid var(--code-line); border-radius: 8px;
  overflow: auto; max-height: 560px; margin: 6px 0; }
.code-body { display: table; min-width: 100%; padding: 6px 0; }
.code .cl { display: table-row; }
.code .ln { display: table-cell; text-align: right; padding: 0 10px 0 12px; color: var(--ln); user-select: none;
  min-width: 2.5em; white-space: nowrap; border-right: 1px solid var(--code-line); }
.code .lc { display: table-cell; padding: 0 12px; white-space: pre; tab-size: 4; }
.code .cl.wrapline .lc { white-space: pre-wrap; word-break: break-word; }
.code.term { background: transparent; border: none; margin: 0; max-height: 480px; }
.code.cmd .cl:first-child .lc::before { content: "$ "; color: #7ee787; font-weight: 600; }
.code.cmd .lc { white-space: pre-wrap; word-break: break-word; }
.code .more { display: block; margin: 4px 12px 10px; font-size: 12px; }
.terminal { background: #0d1117; color: #e6edf3; border-radius: 8px; border: 1px solid #30363d; padding: 4px 0; margin: 6px 0; }
.terminal .code.term + .code.term, .terminal .texit + .code.term { border-top: 1px solid #30363d; }
.texit { font: 600 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: #3fb950; padding: 4px 12px 0; }
.texit.bad { color: #ff7b72; }
.term-pending { color: #8b949e; padding: 4px 12px 6px; font: 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.tk-keyword { color: var(--tk-keyword); } .tk-string { color: var(--tk-string); }
.tk-comment { color: var(--tk-comment); font-style: italic; } .tk-number { color: var(--tk-number); }
.tk-fn { color: var(--tk-fn); } .tk-type { color: var(--tk-type); } .tk-prop { color: var(--tk-prop); }
.tk-literal { color: var(--tk-literal); } .tk-builtin { color: var(--tk-builtin); } .tk-var { color: var(--tk-var); }
.tk-tag { color: var(--tk-tag); } .tk-attr { color: var(--tk-attr); } .tk-op { color: var(--tk-op); }
.tk-deco { color: var(--tk-deco); } .tk-add { color: var(--tk-add); } .tk-del { color: var(--tk-del); }
.tk-warn { color: var(--tk-warn); }
.diff .dadd-row { background: var(--add-bg); } .diff .ddel-row { background: var(--del-bg); }
.diff .dadd-row .dsign { color: var(--add-sign); font-weight: 700; } .diff .ddel-row .dsign { color: var(--del-sign); font-weight: 700; }
.diff .dskip .lc { color: var(--ln); font-style: italic; }
.dstat { font: 600 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.dadd { color: var(--add-sign); } .ddel { color: var(--del-sign); }

.fhead { display: flex; align-items: center; gap: 2px; flex-wrap: wrap; font: 12.5px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px 8px 0 0; padding: 6px 10px; margin-top: 6px; }
.fhead + .code { border-top-left-radius: 0; border-top-right-radius: 0; margin-top: 0; border-top: none; }
.fdir { color: var(--muted); overflow: hidden; text-overflow: ellipsis; }
.fname { font-weight: 700; color: var(--text); }
.fchips { margin-left: auto; display: inline-flex; gap: 6px; align-items: center; padding-left: 10px; }
.tchip { display: inline-block; font: 600 11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; padding: 1px 8px; border-radius: 999px;
  background: var(--panel-2); border: 1px solid var(--line); color: var(--muted); margin-right: 4px; }
.tchip.ok { color: var(--run); border-color: var(--run); } .tchip.bad { color: var(--fail); border-color: var(--fail); }
.tdesc { font-weight: 600; margin: 2px 0 4px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.tlabel { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); margin: 10px 0 2px; font-weight: 600; }
.tform { display: grid; gap: 4px; margin: 4px 0; }
.trow { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.trow.tcol { display: block; }
.trow .tk { font-size: 12px; color: var(--muted); min-width: 64px; font-weight: 600; }
.tpat { font: 12.5px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: var(--code-bg2); color: var(--tk-string);
  border: 1px solid var(--code-line); border-radius: 5px; padding: 1px 6px; }
.prose { white-space: pre-wrap; word-break: break-word; line-height: 1.5; }
.terr { margin: 6px 0; } .terr .code { border-color: var(--fail); }
.tpending { color: var(--muted); font-style: italic; margin: 6px 0; }
.ttime { color: var(--faint); font-size: 11.5px; margin-top: 6px; }
.rnotes { margin: 6px 0; } .rnotes summary { font-size: 12px; }
.todos { list-style: none; margin: 4px 0; padding: 0; } .todos li { display: flex; gap: 8px; padding: 2px 0; }
.todos .todo-completed { color: var(--muted); text-decoration: line-through; } .todos .todo-in_progress { font-weight: 600; }
.now-box .code { max-height: 220px; }
.steps .sdetail { background: transparent; padding: 4px 0 8px; max-height: none; overflow: visible; margin-left: 82px; }
.steps .skind.tool-bash { color: #2da44e; } .steps .skind.tool-edit, .steps .skind.tool-multiedit { color: #bf8700; }
.steps .skind.tool-write { color: #8250df; } .steps .skind.tool-read { color: #0969da; }
.steps .skind.tool-grep, .steps .skind.tool-glob { color: #1b7c83; } .steps .skind.tool-structuredoutput { color: var(--done); }
@media (max-width: 720px) { .steps .sdetail { margin-left: 0; } }
"""

HIGHLIGHT_JS = r"""/* workflow-watch: a small syntax highlighter and line diff. It returns
   tokens ({c: class, t: text}); the page turns them into spans with
   textContent, so no text is ever parsed as HTML. */
(function (root) {
  "use strict";

  var MAX_HIGHLIGHT = 400000;   // characters; longer text stays plain

  function words(list) { return "\\b(?:" + list.split(/\s+/).filter(Boolean).join("|") + ")\\b"; }

  var STR_D = '"(?:\\\\.|[^"\\\\\\n])*"?';
  var STR_S = "'(?:\\\\.|[^'\\\\\\n])*'?";
  var NUM = "\\b(?:0[xX][\\da-fA-F_]+|0[oObB][0-7_]+|\\d[\\d_]*(?:\\.\\d[\\d_]*)?(?:[eE][+-]?\\d+)?[jJnLlfFuU]*)\\b";
  var FN = "\\b[A-Za-z_$][\\w$]*(?=\\s*\\()";
  var TYPE = "\\b[A-Z][A-Za-z0-9_]*[a-z][A-Za-z0-9_]*\\b";
  var END = "(?![\\s\\S])";   // end of the whole text; $ is the end of a line with the m flag
  var C_BLOCK = "/\\*[\\s\\S]*?(?:\\*/|" + END + ")";
  var C_LINE = "//[^\\n]*";

  var LANGS = {
    python: [
      ["comment", "#[^\\n]*"],
      ["string", "[rRbBuUfF]{0,2}\"\"\"[\\s\\S]*?(?:\"\"\"|" + END + ")|[rRbBuUfF]{0,2}'''[\\s\\S]*?(?:'''|" + END + ")"],
      ["string", "[rRbBuUfF]{0,2}" + STR_D + "|[rRbBuUfF]{0,2}" + STR_S],
      ["deco", "@[\\w.]+"],
      ["keyword", words("and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case")],
      ["literal", words("True False None self cls")],
      ["builtin", words("print len range list dict set tuple str int float bool open isinstance super enumerate zip map filter sorted any all min max sum repr type getattr setattr hasattr")],
      ["number", NUM],
      ["fn", FN],
      ["type", TYPE],
      ["op", "->|:=|[=!<>]=?|[-+*/%&|^~]=?"],
    ],
    js: [
      ["comment", C_LINE + "|" + C_BLOCK],
      ["string", "`(?:\\\\[\\s\\S]|[^\\\\`])*`?"],
      ["string", STR_D + "|" + STR_S],
      ["keyword", words("async await break case catch class const continue debugger default delete do else export extends finally for from function get if import in instanceof let new of return set static super switch this throw try typeof var void while with yield as interface type enum implements declare readonly keyof namespace abstract private protected public satisfies")],
      ["literal", words("true false null undefined NaN Infinity")],
      ["builtin", words("string number boolean any unknown never object console Promise Array Object JSON Math Map Set Error")],
      ["number", NUM],
      ["tag", "</?[A-Z][\\w.]*|</?[a-z][\\w-]*(?=[\\s/>])"],
      ["fn", FN],
      ["type", TYPE],
      ["op", "=>|[=!<>]=?=?|&&|\\|\\||\\?\\?|[-+*/%&|^~]=?|\\.\\.\\."],
    ],
    json: [
      ["prop", "\"(?:\\\\.|[^\"\\\\\\n])*\"(?=\\s*:)"],
      ["string", STR_D],
      ["literal", words("true false null")],
      ["number", "-?" + NUM],
    ],
    bash: [
      ["comment", "(?:^|(?<=\\s))#[^\\n]*"],
      ["string", "'[^']*'?"],
      ["string", "\"(?:\\\\.|[^\"\\\\])*\"?"],
      ["var", "\\$\\{[^}\\n]*\\}?|\\$\\(\\(|\\$[A-Za-z_][\\w]*|\\$[@*#?$!0-9]"],
      ["keyword", words("if then else elif fi for while until do done case esac in function select return export local readonly unset set source alias time")],
      ["var", "(?<=(?:^|[|;&({`\\s]))[A-Za-z_]\\w*(?==)"],
      ["fn", "(?<=(?:^|[|;&({`]|\\$\\(|&&|\\|\\|)\\s*(?:[A-Za-z_]\\w*=\\S*\\s+)*(?:sudo\\s+|env\\s+|exec\\s+|time\\s+)?)[A-Za-z_./~][\\w./~+-]*"],
      ["attr", "(?<=\\s)--?[A-Za-z0-9][\\w-]*"],
      ["number", "(?<=[\\s=:])\\d+\\b"],
      ["op", "&&|\\|\\||[|;&<>]+|\\\\$"],
    ],
    css: [
      ["comment", C_BLOCK],
      ["string", STR_D + "|" + STR_S],
      ["keyword", "@[\\w-]+|!important"],
      ["prop", "[\\w-]+(?=\\s*:[^:{])"],
      ["number", "#[\\da-fA-F]{3,8}\\b|-?\\d*\\.?\\d+(?:px|em|rem|vh|vw|%|s|ms|fr|deg)?\\b"],
      ["fn", FN],
      ["tag", "[.#][\\w-]+"],
    ],
    html: [
      ["comment", "<!--[\\s\\S]*?(?:-->|" + END + ")"],
      ["tag", "</?[\\w:.-]+|/?>"],
      ["attr", "[\\w:@.-]+(?==)"],
      ["string", STR_D + "|" + STR_S],
    ],
    yaml: [
      ["comment", "(?:^|(?<=\\s))#[^\\n]*"],
      ["keyword", "^\\s*\\[[^\\]\\n]+\\]"],
      ["prop", "^[ \\t-]*[\\w.\\-\"' /]+?(?=\\s*[:=](?:\\s|$))"],
      ["string", STR_D + "|" + STR_S],
      ["literal", words("true false null yes no on off True False")],
      ["number", NUM],
      ["op", "^\\s*-(?=\\s)|[|>]-?$"],
    ],
    sql: [
      ["comment", "--[^\\n]*|" + C_BLOCK],
      ["string", STR_S],
      ["prop", "\"(?:[^\"\\n])*\""],
      ["keyword", words("select from where and or not insert into values update set delete create table index view drop alter add column primary key foreign references join left right inner outer full on group by order having limit offset as distinct union all case when then else end is null in exists between like returning begin commit rollback with for share nowait skip locked constraint default unique check cascade grant revoke trigger function")],
      ["number", NUM],
      ["fn", FN],
    ],
    markdown: [
      ["keyword", "^#{1,6}\\s[^\\n]*"],
      ["string", "```[\\s\\S]*?(?:```|" + END + ")|`[^`\\n]+`"],
      ["type", "\\*\\*[^*\\n]+\\*\\*|__[^_\\n]+__"],
      ["fn", "\\[[^\\]\\n]+\\]\\([^)\\n]+\\)"],
      ["op", "^\\s*(?:[-*+]|\\d+\\.)\\s|^\\s*>"],
    ],
    clike: [
      ["comment", C_LINE + "|" + C_BLOCK + "|(?:^|(?<=\\s))#(?![\\[!{])[^\\n]*"],
      ["string", "\"\"\"[\\s\\S]*?(?:\"\"\"|" + END + ")|`[^`]*`?"],
      ["string", STR_D + "|" + STR_S],
      ["keyword", words("abstract as async await break case catch chan class const continue def default defer do else elif elsif end enum export extends extern final finally fn for foreach func go goto if impl implements import in interface internal let loop match mod module mut namespace new nil override package private protected pub public range rescue return select self sealed static struct super switch synchronized template this throw throws trait try type typedef union unless unsafe use using val var virtual void volatile when where while yield")],
      ["literal", words("true false null nil None NULL nullptr")],
      ["builtin", words("int long short char byte float double bool boolean string String usize isize u8 u16 u32 u64 i8 i16 i32 i64 f32 f64 error any")],
      ["number", NUM],
      ["fn", FN],
      ["type", TYPE],
      ["op", "=>|->|::|[=!<>]=?|&&|\\|\\||[-+*/%&|^~]=?"],
    ],
    grep: [
      ["tag", "^[^\\n:]+?(?=:\\d+[:-]|$)"],
      ["number", "(?<=^[^\\n:]+?:)\\d+(?=[:-])"],
    ],
    log: [
      // only lines that look like test or tool output, not every sentence with "error" in it
      ["del", "^.*(?:\\bFAILED\\b|\\bFAIL\\b|\\bERROR\\b|^E {2,}|Traceback \\(most recent call last\\)|\\b[A-Z]\\w*(?:Error|Exception)\\b|\\b[1-9]\\d* (?:failed|errors?)\\b|^(?:error|fatal|panic)\\b|✖|✗).*$"],
      ["add", "^.*(?:\\bPASSED\\b|\\bPASS\\b|\\b\\d+ passed\\b|^OK\\b|✔|✓).*$"],
      ["warn", "^.*(?:\\bWARN(?:ING)?\\b|^warning:|\\b\\w*Warning\\b|\\bSKIPPED\\b|\\b\\d+ skipped\\b).*$"],
    ],
  };
  LANGS.ts = LANGS.js;

  var EXT = {
    py: "python", pyi: "python", pyw: "python",
    js: "js", mjs: "js", cjs: "js", jsx: "js", ts: "js", tsx: "js", mts: "js", cts: "js", vue: "html", svelte: "html",
    json: "json", jsonl: "json", jsonc: "js", map: "json", lock: "json",
    sh: "bash", bash: "bash", zsh: "bash", fish: "bash", env: "bash",
    css: "css", scss: "css", sass: "css", less: "css",
    html: "html", htm: "html", xml: "html", svg: "html", xhtml: "html",
    yml: "yaml", yaml: "yaml", toml: "yaml", ini: "yaml", cfg: "yaml", conf: "yaml", properties: "yaml",
    sql: "sql", psql: "sql",
    md: "markdown", markdown: "markdown", mdx: "markdown", rst: "markdown", txt: null,
    go: "clike", java: "clike", kt: "clike", kts: "clike", c: "clike", h: "clike", cc: "clike", cpp: "clike",
    hpp: "clike", cs: "clike", rs: "clike", swift: "clike", scala: "clike", rb: "clike", php: "clike",
    dart: "clike", groovy: "clike", gradle: "clike", m: "clike", lua: "clike", r: "clike", pl: "clike",
    tf: "clike", proto: "clike", graphql: "clike", gql: "clike",
  };
  var NAMES = { dockerfile: "bash", makefile: "bash", ".bashrc": "bash", ".zshrc": "bash", ".env": "bash",
                ".gitignore": "bash", "requirements.txt": "yaml" };

  function langFor(path) {
    if (!path) return null;
    var name = String(path).split(/[\\/]/).pop().toLowerCase();
    if (NAMES[name] !== undefined) return NAMES[name];
    if (/^dockerfile/.test(name)) return "bash";
    if (/^\.env/.test(name)) return "bash";
    var dot = name.lastIndexOf(".");
    if (dot < 0) return null;
    var ext = name.slice(dot + 1);
    return EXT[ext] !== undefined ? EXT[ext] : null;
  }

  /* Guess a language for text that has no file name. */
  function sniff(text) {
    var head = String(text || "").slice(0, 2000).trim();
    if (!head) return null;
    if (/^[\[{]/.test(head)) { try { JSON.parse(text); return "json"; } catch (e) { /* not JSON */ } }
    if (/^(?:def |class |import |from \w+ import |@\w)/m.test(head) && /:\s*$/m.test(head)) return "python";
    if (/^(?:const |let |var |import |export |function |async function )/m.test(head)) return "js";
    if (/^\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE|WITH)\b/i.test(head)) return "sql";
    if (/^<(?:!doctype|html|\?xml|svg|div)/i.test(head)) return "html";
    if (/^#{1,6} /m.test(head)) return "markdown";
    return null;
  }

  var compiled = {};
  function regexFor(lang) {
    if (compiled[lang]) return compiled[lang];
    var rules = LANGS[lang];
    var source = rules.map(function (r) { return "(" + r[1] + ")"; }).join("|");
    var flags = "gm" + (lang === "sql" ? "i" : "");
    var re;
    try { re = new RegExp(source, flags); }
    catch (e) {   // a browser without lookbehind: drop the rules that use it
      var safe = rules.filter(function (r) { return r[1].indexOf("(?<") < 0; });
      rules = safe;
      re = new RegExp(safe.map(function (r) { return "(" + r[1] + ")"; }).join("|"), flags);
    }
    compiled[lang] = { re: re, classes: rules.map(function (r) { return r[0]; }) };
    return compiled[lang];
  }

  /* Tokens of a text: [{c: class or "", t: text}]. */
  function tokens(text, lang) {
    text = String(text == null ? "" : text);
    if (!lang || !LANGS[lang] || text.length > MAX_HIGHLIGHT) return [{ c: "", t: text }];
    var rx = regexFor(lang), re = rx.re, out = [], pos = 0, m;
    re.lastIndex = 0;
    while ((m = re.exec(text)) !== null) {
      if (m[0] === "") { re.lastIndex++; continue; }
      if (m.index > pos) out.push({ c: "", t: text.slice(pos, m.index) });
      var cls = "";
      for (var g = 1; g < m.length; g++) if (m[g] !== undefined) { cls = rx.classes[g - 1]; break; }
      out.push({ c: cls, t: m[0] });
      pos = m.index + m[0].length;
    }
    if (pos < text.length) out.push({ c: "", t: text.slice(pos) });
    return out;
  }

  /* Tokens split into lines: [[{c, t}, ...], ...]. */
  function lines(text, lang) {
    var result = [[]];
    tokens(text, lang).forEach(function (tok) {
      var parts = tok.t.split("\n");
      parts.forEach(function (part, i) {
        if (i > 0) result.push([]);
        if (part) result[result.length - 1].push({ c: tok.c, t: part });
      });
    });
    return result;
  }

  /* Line diff of two texts (longest common subsequence). Returns rows
     {k: " " | "-" | "+", a: old line index or null, b: new line index or null}. */
  function diffLines(oldText, newText) {
    var a = String(oldText || "").split("\n"), b = String(newText || "").split("\n");
    var start = 0;
    while (start < a.length && start < b.length && a[start] === b[start]) start++;
    var endA = a.length, endB = b.length;
    while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
    var rows = [], i;
    for (i = 0; i < start; i++) rows.push({ k: " ", a: i, b: i });
    var n = endA - start, m = endB - start, middle = [];
    if (n * m > 4000000) {
      for (i = start; i < endA; i++) middle.push({ k: "-", a: i, b: null });
      for (i = start; i < endB; i++) middle.push({ k: "+", a: null, b: i });
    } else {
      var table = [];
      for (i = 0; i <= n; i++) table.push(new Uint32Array(m + 1));
      for (i = n - 1; i >= 0; i--) {
        for (var j = m - 1; j >= 0; j--) {
          table[i][j] = a[start + i] === b[start + j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
        }
      }
      var x = 0, y = 0;
      while (x < n || y < m) {
        if (x < n && y < m && a[start + x] === b[start + y]) { middle.push({ k: " ", a: start + x, b: start + y }); x++; y++; }
        else if (x < n && (y >= m || table[x + 1][y] >= table[x][y + 1])) { middle.push({ k: "-", a: start + x, b: null }); x++; }
        else { middle.push({ k: "+", a: null, b: start + y }); y++; }
      }
    }
    rows = rows.concat(middle);
    for (i = 0; endA + i < a.length; i++) rows.push({ k: " ", a: endA + i, b: endB + i });
    return rows;
  }

  var api = { tokens: tokens, lines: lines, langFor: langFor, sniff: sniff, diffLines: diffLines, languages: Object.keys(LANGS) };
  root.WWHighlight = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
"""

GRAPH_JS = r"""/* workflow-watch: turn a Workflow script into a flow graph, match the run's
   agents to it, and lay it out. Pure functions; no DOM access here, so the
   same code runs in the page and in the Node test. Needs the acorn global. */
(function (root) {
  "use strict";

  var MAX_INLINE_DEPTH = 3;

  // ---------------------------------------------------------------- parse

  function parseScript(source) {
    var ac = root.acorn;
    if (!ac) throw new Error("acorn is not loaded");
    var opts = { ecmaVersion: "latest", sourceType: "module", locations: true,
                 allowAwaitOutsideFunction: true, allowReturnOutsideFunction: true,
                 allowImportExportEverywhere: true };
    return ac.parse(source, opts);
  }

  function literalValue(node) {
    if (!node) return undefined;
    switch (node.type) {
      case "Literal": return node.value;
      case "TemplateLiteral":
        if (node.expressions.length === 0) return node.quasis.map(function (q) { return q.value.cooked; }).join("");
        return undefined;
      case "ArrayExpression":
        var arr = [];
        for (var i = 0; i < node.elements.length; i++) {
          var v = literalValue(node.elements[i]);
          if (v === undefined) return undefined;
          arr.push(v);
        }
        return arr;
      case "ObjectExpression":
        var obj = {};
        for (var j = 0; j < node.properties.length; j++) {
          var p = node.properties[j];
          if (p.type !== "Property" || p.computed) return undefined;
          var key = p.key.type === "Identifier" ? p.key.name : p.key.value;
          var val = literalValue(p.value);
          if (val === undefined) return undefined;
          obj[key] = val;
        }
        return obj;
      case "UnaryExpression":
        if (node.operator === "-" && node.argument.type === "Literal") return -node.argument.value;
        return undefined;
      default: return undefined;
    }
  }

  function findMeta(ast) {
    for (var i = 0; i < ast.body.length; i++) {
      var st = ast.body[i];
      if (st.type === "ExportNamedDeclaration" && st.declaration &&
          st.declaration.type === "VariableDeclaration") {
        var decls = st.declaration.declarations;
        for (var j = 0; j < decls.length; j++) {
          if (decls[j].id.type === "Identifier" && decls[j].id.name === "meta") {
            return { value: literalValue(decls[j].init) || null, node: st };
          }
        }
      }
    }
    return { value: null, node: null };
  }

  // ------------------------------------------------------------ build IR

  function Builder(source) {
    this.src = source;
    this.fns = {};
    this.consts = {};
    this.nextId = 1;
    this.agents = [];
    this.phase = null;
  }

  Builder.prototype.text = function (node, max) {
    var t = this.src.slice(node.start, node.end).replace(/\s+/g, " ").trim();
    max = max || 80;
    return t.length > max ? t.slice(0, max - 1) + "…" : t;
  };

  Builder.prototype.id = function (prefix) { return prefix + (this.nextId++); };

  Builder.prototype.registerFunctions = function (body) {
    var self = this;
    body.forEach(function (st) {
      var target = st.type === "ExportNamedDeclaration" ? st.declaration : st;
      if (!target) return;
      if (target.type === "FunctionDeclaration" && target.id) self.fns[target.id.name] = target;
      if (target.type === "VariableDeclaration") {
        target.declarations.forEach(function (d) {
          if (d.id.type === "Identifier" && d.init &&
              (d.init.type === "ArrowFunctionExpression" || d.init.type === "FunctionExpression")) {
            self.fns[d.id.name] = d.init;
          }
          if (d.id.type === "Identifier" && d.init && d.init.type === "ObjectExpression") {
            self.consts[d.id.name] = d.init;
          }
        });
      }
    });
  };

  Builder.prototype.seq = function (statements, ctx) {
    var items = [];
    for (var i = 0; i < statements.length; i++) {
      items = items.concat(this.stmt(statements[i], ctx));
    }
    return { k: "seq", items: items };
  };

  Builder.prototype.block = function (node, ctx) {
    if (!node) return { k: "seq", items: [] };
    if (node.type === "BlockStatement") return this.seq(node.body, ctx);
    return { k: "seq", items: this.stmt(node, ctx) };
  };

  Builder.prototype.stmt = function (node, ctx) {
    if (!node) return [];
    var self = this;
    switch (node.type) {
      case "ExpressionStatement": return this.expr(node.expression, ctx);
      case "VariableDeclaration":
        var out = [];
        node.declarations.forEach(function (d) {
          if (!d.init) return;
          if (d.id.type === "Identifier" && d.init.type === "ObjectExpression") self.consts[d.id.name] = d.init;
          if ((d.init.type === "ArrowFunctionExpression" || d.init.type === "FunctionExpression")) {
            if (d.id.type === "Identifier") self.fns[d.id.name] = d.init;
            return;
          }
          out = out.concat(self.expr(d.init, ctx));
        });
        return out;
      case "FunctionDeclaration":
        if (node.id) this.fns[node.id.name] = node;
        return [];
      case "ExportNamedDeclaration":
        if (node.declaration && node.declaration.type === "VariableDeclaration") {
          var isMeta = node.declaration.declarations.some(function (d) {
            return d.id.type === "Identifier" && d.id.name === "meta";
          });
          if (isMeta) return [];
        }
        return node.declaration ? this.stmt(node.declaration, ctx) : [];
      case "ExportDefaultDeclaration":
        return this.stmt(node.declaration, ctx);
      case "ReturnStatement":
        var r = node.argument ? this.expr(node.argument, ctx) : [];
        if (!ctx.inFn) r.push({ k: "exit", id: this.id("x"), text: "return", end: true });
        return r;
      case "ThrowStatement":
        var t = this.expr(node.argument, ctx);
        t.push({ k: "exit", id: this.id("x"), text: "throw " + this.text(node.argument, 40), end: true });
        return t;
      case "BreakStatement":
        return ctx.loop ? [{ k: "exit", id: this.id("x"), text: "break", brk: true }] : [];
      case "ContinueStatement":
        return ctx.loop ? [{ k: "exit", id: this.id("x"), text: "continue", cont: true }] : [];
      case "IfStatement":
        var testItems = this.expr(node.test, ctx);
        var yes = this.block(node.consequent, ctx);
        var no = this.block(node.alternate, ctx);
        if (interesting(yes) || interesting(no)) {
          testItems.push({ k: "branch", id: this.id("b"), test: this.text(node.test, 70),
                           line: node.loc.start.line,
                           branches: [{ label: "yes", seq: yes }, { label: "no", seq: no }] });
        }
        return testItems;
      case "SwitchStatement":
        var head = this.expr(node.discriminant, ctx);
        var cases = node.cases.map(function (c) {
          return { label: c.test ? "case " + self.text(c.test, 30) : "default",
                   seq: self.seq(c.consequent, Object.assign({}, ctx, { loop: true })) };
        });
        if (cases.some(function (c) { return interesting(c.seq); })) {
          head.push({ k: "branch", id: this.id("b"), test: "switch " + this.text(node.discriminant, 60),
                      line: node.loc.start.line, branches: cases });
        }
        return head;
      case "ForStatement":
      case "ForOfStatement":
      case "ForInStatement":
      case "WhileStatement":
      case "DoWhileStatement":
        var pre = [];
        if (node.type === "ForStatement" && node.init) pre = this.stmtOrExpr(node.init, ctx);
        if ((node.type === "ForOfStatement" || node.type === "ForInStatement") && node.right) pre = this.expr(node.right, ctx);
        var body = this.block(node.body, Object.assign({}, ctx, { loop: true }));
        if (hasAgent(body)) {
          pre.push({ k: "loop", id: this.id("l"), title: this.loopTitle(node), line: node.loc.start.line, body: body });
        }
        return pre;
      case "BlockStatement":
        return this.seq(node.body, ctx).items;
      case "TryStatement":
        var items = this.seq(node.block.body, ctx).items;
        if (node.handler) {
          var h = this.block(node.handler.body, ctx);
          if (interesting(h)) items.push({ k: "branch", id: this.id("b"), test: "on error", line: node.handler.loc.start.line,
                                           branches: [{ label: "catch", seq: h }, { label: "ok", seq: { k: "seq", items: [] } }] });
        }
        if (node.finalizer) items = items.concat(this.seq(node.finalizer.body, ctx).items);
        return items;
      case "LabeledStatement":
        return this.stmt(node.body, ctx);
      default:
        return [];
    }
  };

  Builder.prototype.stmtOrExpr = function (node, ctx) {
    return /Declaration$/.test(node.type) ? this.stmt(node, ctx) : this.expr(node, ctx);
  };

  Builder.prototype.loopTitle = function (node) {
    switch (node.type) {
      case "ForStatement":
        return "loop: " + (node.test ? this.text(node.test, 60) : "forever");
      case "ForOfStatement":
        return "for each of " + this.text(node.right, 50);
      case "ForInStatement":
        return "for each key of " + this.text(node.right, 50);
      case "WhileStatement":
        return "while " + this.text(node.test, 60);
      case "DoWhileStatement":
        return "repeat while " + this.text(node.test, 55);
    }
    return "loop";
  };

  function calleeName(node) {
    if (node.type === "Identifier") return node.name;
    return null;
  }

  function memberName(node) {
    if (node.type === "MemberExpression" && !node.computed && node.property.type === "Identifier") return node.property.name;
    return null;
  }

  function isFn(node) {
    return !!node && (node.type === "ArrowFunctionExpression" || node.type === "FunctionExpression" ||
                      node.type === "FunctionDeclaration");
  }

  Builder.prototype.fnBody = function (fn, ctx) {
    var inner = Object.assign({}, ctx, { inFn: true, loop: false });
    if (!fn) return { k: "seq", items: [] };
    if (fn.type === "Identifier" && this.fns[fn.name]) fn = this.fns[fn.name];
    if (!isFn(fn)) return { k: "seq", items: this.expr(fn, ctx) };
    if (fn.body.type === "BlockStatement") return this.seq(fn.body.body, inner);
    if (isFn(fn.body)) return this.fnBody(fn.body, inner);   // () => () => agent(...)
    return { k: "seq", items: this.expr(fn.body, inner) };
  };

  Builder.prototype.agentNode = function (call, ctx) {
    var node = { k: "agent", id: this.id("a"), line: call.loc.start.line,
                 label: null, labelRe: null, labelLiteral: 0, phase: ctx.phase || null,
                 phaseStatic: false, model: null, schema: false, isolation: null, prompt: null };
    var promptArg = call.arguments[0];
    if (promptArg) node.prompt = this.text(promptArg, 140);
    var props = this.resolveOpts(call.arguments[1], {}, 0);
    if (props) {
      var self = this;
      Object.keys(props).forEach(function (key) {
        var p = { value: props[key] };
        if (key === "label") self.applyLabel(node, p.value);
        else if (key === "phase") {
          var ph = literalValue(p.value);
          if (typeof ph === "string") { node.phase = ph; node.phaseStatic = true; }
          else { node.phase = self.text(p.value, 40); node.phaseStatic = false; node.phaseExpr = true; }
        } else if (key === "model") {
          var m = literalValue(p.value); node.model = typeof m === "string" ? m : self.text(p.value, 30);
        } else if (key === "schema") node.schema = true;
        else if (key === "isolation") {
          var iso = literalValue(p.value); node.isolation = typeof iso === "string" ? iso : "?";
        } else if (key === "agentType") {
          var at = literalValue(p.value); node.agentType = typeof at === "string" ? at : null;
        }
      });
    }
    if (!node.label) node.label = node.prompt ? "agent(" + node.prompt.slice(0, 40) + ")" : "agent";
    this.agents.push(node);
    return node;
  };

  /* The options object of an agent() call, as {key: value node}. Follows a
     constant (const base = {...}), spreads, and a helper that builds the
     object (const opts = (label, phase) => ({ label, phase, ... })). */
  Builder.prototype.resolveOpts = function (node, env, depth) {
    if (!node || depth > 4) return null;
    var self = this;
    if (node.type === "Identifier") {
      if (env[node.name]) return this.resolveOpts(env[node.name], {}, depth + 1);
      if (this.consts[node.name]) return this.resolveOpts(this.consts[node.name], {}, depth + 1);
      return null;
    }
    if (node.type === "ObjectExpression") {
      var props = {};
      node.properties.forEach(function (p) {
        if (p.type === "SpreadElement") {
          var spread = self.resolveOpts(p.argument, env, depth + 1);
          if (spread) Object.keys(spread).forEach(function (k) { props[k] = spread[k]; });
          return;
        }
        if (p.type !== "Property" || p.computed) return;
        var key = p.key.type === "Identifier" ? p.key.name : p.key.value;
        var value = p.value;
        if (value.type === "Identifier" && env[value.name]) value = env[value.name];
        props[key] = value;
      });
      return props;
    }
    if (node.type === "CallExpression" && node.callee.type === "Identifier") {
      var fn = this.fns[node.callee.name];
      if (!fn) return null;
      var body = fn.body;
      if (body.type === "BlockStatement") {
        var ret = body.body.filter(function (s) { return s.type === "ReturnStatement"; })[0];
        body = ret ? ret.argument : null;
      }
      if (!body) return null;
      var inner = {};
      fn.params.forEach(function (param, i) {
        if (param.type === "Identifier" && node.arguments[i]) {
          var a = node.arguments[i];
          inner[param.name] = (a.type === "Identifier" && env[a.name]) ? env[a.name] : a;
        }
      });
      return this.resolveOpts(body, inner, depth + 1);
    }
    return null;
  };

  function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  Builder.prototype.applyLabel = function (node, value) {
    if (value.type === "Literal" && typeof value.value === "string") {
      node.label = value.value;
      node.labelRe = "^" + escapeRe(value.value) + "$";
      node.labelLiteral = value.value.length + 1000;
      return;
    }
    if (value.type === "TemplateLiteral") {
      var shown = "", re = "^", lit = 0, self = this;
      value.quasis.forEach(function (q, i) {
        var cooked = q.value.cooked == null ? q.value.raw : q.value.cooked;
        shown += cooked; re += escapeRe(cooked); lit += cooked.length;
        if (i < value.expressions.length) {
          shown += "${" + self.text(value.expressions[i], 24) + "}";
          re += "([\\s\\S]*?)";
        }
      });
      node.label = shown;
      node.labelRe = re + "$";
      node.labelLiteral = lit;
      return;
    }
    if (value.type === "BinaryExpression" && value.operator === "+") {
      // "dev:" + stage
      var parts = flattenPlus(value), label = "", rx = "^", n = 0, me = this;
      parts.forEach(function (p) {
        if (p.type === "Literal" && typeof p.value === "string") { label += p.value; rx += escapeRe(p.value); n += p.value.length; }
        else { label += "${" + me.text(p, 24) + "}"; rx += "([\\s\\S]*?)"; }
      });
      node.label = label; node.labelRe = rx + "$"; node.labelLiteral = n;
      return;
    }
    node.label = this.text(value, 40);
    node.labelRe = null;
  };

  function flattenPlus(node) {
    if (node.type === "BinaryExpression" && node.operator === "+") return flattenPlus(node.left).concat(flattenPlus(node.right));
    return [node];
  }

  Builder.prototype.expr = function (node, ctx) {
    if (!node) return [];
    var self = this;
    switch (node.type) {
      case "AwaitExpression": return this.expr(node.argument, ctx);
      case "ParenthesizedExpression": return this.expr(node.expression, ctx);
      case "ChainExpression": return this.expr(node.expression, ctx);
      case "CallExpression": return this.call(node, ctx);
      case "NewExpression": return this.list(node.arguments, ctx);
      case "AssignmentExpression":
        if (isFn(node.right) && node.left.type === "Identifier") { this.fns[node.left.name] = node.right; return []; }
        return this.expr(node.right, ctx);
      case "ConditionalExpression":
        var test = this.expr(node.test, ctx);
        var yes = { k: "seq", items: this.expr(node.consequent, ctx) };
        var no = { k: "seq", items: this.expr(node.alternate, ctx) };
        if (interesting(yes) || interesting(no)) {
          test.push({ k: "branch", id: this.id("b"), test: this.text(node.test, 70), line: node.loc.start.line,
                      branches: [{ label: "yes", seq: yes }, { label: "no", seq: no }] });
          return test;
        }
        return test;
      case "LogicalExpression":
        var left = this.expr(node.left, ctx);
        var right = { k: "seq", items: this.expr(node.right, ctx) };
        if (interesting(right)) {
          left.push({ k: "branch", id: this.id("b"), test: this.text(node.left, 60) + " " + node.operator + " …",
                      line: node.loc.start.line,
                      branches: [{ label: node.operator === "&&" ? "if true" : "if not", seq: right },
                                 { label: "else", seq: { k: "seq", items: [] } }] });
        }
        return left;
      case "SequenceExpression": return this.list(node.expressions, ctx);
      case "ArrayExpression": return this.list(node.elements, ctx);
      case "ObjectExpression":
        return this.list(node.properties.map(function (p) { return p.type === "Property" ? p.value : p.argument; }), ctx);
      case "TemplateLiteral": return this.list(node.expressions, ctx);
      case "TaggedTemplateExpression": return this.expr(node.quasi, ctx);
      case "BinaryExpression": return this.expr(node.left, ctx).concat(this.expr(node.right, ctx));
      case "UnaryExpression": return this.expr(node.argument, ctx);
      case "SpreadElement": return this.expr(node.argument, ctx);
      case "MemberExpression":
        return this.expr(node.object, ctx).concat(node.computed ? this.expr(node.property, ctx) : []);
      case "ArrowFunctionExpression":
      case "FunctionExpression":
        // A function that is only defined here (passed somewhere we do not
        // understand): show its agents as a group so they are not lost.
        var body = this.fnBody(node, ctx);
        return hasAgent(body) ? [{ k: "group", id: this.id("g"), title: "callback", body: body }] : [];
      default:
        return [];
    }
  };

  Builder.prototype.list = function (nodes, ctx) {
    var out = [];
    for (var i = 0; i < nodes.length; i++) if (nodes[i]) out = out.concat(this.expr(nodes[i], ctx));
    return out;
  };

  Builder.prototype.fanoutBody = function (fn, ctx) {
    return this.fnBody(fn, Object.assign({}, ctx, { inFn: true }));
  };

  Builder.prototype.call = function (node, ctx) {
    var self = this;
    var name = calleeName(node.callee);
    var member = memberName(node.callee);
    var args = node.arguments;
    if (name === "agent") return [this.agentNode(node, ctx)];
    if (name === "phase") {
      var title = literalValue(args[0]);
      title = typeof title === "string" ? title : (args[0] ? this.text(args[0], 40) : "phase");
      ctx.phase = title;
      return [{ k: "phase", id: this.id("p"), title: title, line: node.loc.start.line }];
    }
    if (name === "log") return [];
    if (name === "workflow") {
      var wf = literalValue(args[0]);
      return [{ k: "sub", id: this.id("w"), title: "workflow " + (typeof wf === "string" ? wf : (args[0] ? this.text(args[0], 40) : "")),
                line: node.loc.start.line }];
    }
    if (name === "parallel") return this.parallelCall(node, args[0], ctx, "parallel");
    if (name === "pipeline") {
      var stages = args.slice(1).map(function (fn, i) {
        var seq = self.fanoutBody(fn, ctx);
        return { label: "stage " + (i + 1), seq: seq };
      });
      var items = [];
      stages.forEach(function (s) { items = items.concat(s.seq.items); });
      return this.expr(args[0], ctx).concat([{ k: "each", id: this.id("e"), kind: "pipeline",
        title: "pipeline over " + (args[0] ? this.text(args[0], 50) : "items"), line: node.loc.start.line,
        body: { k: "seq", items: items } }]);
    }
    if (member === "all" || member === "allSettled" || member === "race" || member === "any") {
      var obj = node.callee.object;
      if (obj.type === "Identifier" && obj.name === "Promise") return this.parallelCall(node, args[0], ctx, "Promise." + member, true);
    }
    if (member === "from" && node.callee.object.type === "Identifier" && node.callee.object.name === "Array" && args[1]) {
      var body = this.fanoutBody(args[1], ctx);
      if (hasAgent(body)) {
        return [{ k: "each", id: this.id("e"), kind: "times", title: "repeat " + this.text(args[0], 40),
                  line: node.loc.start.line, body: body }];
      }
      return [];
    }
    if (member && /^(map|flatMap|forEach|filter|some|every|reduce|find)$/.test(member) && args[0]) {
      var fnArg = member === "reduce" ? args[0] : args[0];
      var fbody = this.fanoutBody(fnArg, ctx);
      var before = this.expr(node.callee.object, ctx);
      if (hasAgent(fbody)) {
        return before.concat([{ k: "each", id: this.id("e"), kind: member,
          title: "for each of " + this.text(node.callee.object, 50), line: node.loc.start.line, body: fbody }]);
      }
      return before;
    }
    if (member === "then" || member === "catch" || member === "finally") {
      var head = this.expr(node.callee.object, ctx);
      args.forEach(function (fn) { head = head.concat(self.fnBody(fn, ctx).items); });
      return head;
    }
    if (name && this.fns[name] && (ctx.depth || 0) < MAX_INLINE_DEPTH) {
      var argItems = this.list(args, ctx);
      var inner = this.fnBody(this.fns[name], Object.assign({}, ctx, { depth: (ctx.depth || 0) + 1 }));
      if (hasAgent(inner) || interesting(inner)) {
        return argItems.concat([{ k: "group", id: this.id("g"), title: name + "()", line: node.loc.start.line, body: inner }]);
      }
      return argItems;
    }
    // Unknown call: look inside the callee and the arguments.
    var out = this.expr(node.callee, ctx);
    args.forEach(function (a) { out = out.concat(isFn(a) ? self.expr(a, ctx) : self.expr(a, ctx)); });
    return out;
  };

  Builder.prototype.parallelCall = function (node, arg, ctx, title, promises) {
    var self = this;
    if (!arg) return [];
    if (arg.type === "ArrayExpression") {
      var branches = [], loose = [];
      arg.elements.forEach(function (el, i) {
        if (!el) return;
        if (el.type === "SpreadElement") { loose = loose.concat(self.expr(el.argument, ctx)); return; }
        var seq = promises ? { k: "seq", items: self.expr(el, ctx) } : self.fanoutBody(el, ctx);
        if (interesting(seq)) branches.push({ label: "", seq: seq });
      });
      var out = [];
      if (branches.length) out.push({ k: "par", id: this.id("r"), title: title, line: node.loc.start.line, branches: branches });
      return out.concat(loose);
    }
    // parallel(items.map(x => () => agent(...))) and similar
    return this.expr(arg, ctx).map(function (item) {
      if (item.k === "each" && !item.retitled) { item.title = title + ": " + item.title; item.retitled = true; }
      return item;
    });
  };

  function hasAgent(node) {
    if (!node) return false;
    switch (node.k) {
      case "agent": case "sub": return true;
      case "seq": return node.items.some(hasAgent);
      case "branch": case "par": return node.branches.some(function (b) { return hasAgent(b.seq); });
      case "loop": case "each": case "group": return hasAgent(node.body);
      default: return false;
    }
  }

  function interesting(node) {
    if (!node) return false;
    if (node.k === "seq") return node.items.some(function (i) { return i.k !== "phase" ? interesting(i) : false; });
    if (node.k === "exit") return true;
    return hasAgent(node);
  }

  function buildGraph(source) {
    var ast = parseScript(source);
    var meta = findMeta(ast);
    var b = new Builder(source);
    b.registerFunctions(ast.body);
    var ctx = { phase: null, inFn: false, loop: false, depth: 0 };
    var seq = b.seq(ast.body, ctx);
    return { meta: meta.value, seq: seq, agents: b.agents };
  }

  // ------------------------------------------------------ fallback graph

  /* Without a script, draw the agents by phase, in start order. */
  function runtimeGraph(agents, phases) {
    var order = [];
    (phases || []).forEach(function (p) { if (p && p.title && order.indexOf(p.title) < 0) order.push(p.title); });
    agents.forEach(function (a) { var p = a.phase || ""; if (order.indexOf(p) < 0) order.push(p); });
    var items = [], nodes = [], n = 1;
    order.forEach(function (title) {
      var inPhase = agents.filter(function (a) { return (a.phase || "") === title; });
      if (!inPhase.length) return;
      if (title) items.push({ k: "phase", id: "p" + (n++), title: title });
      inPhase.forEach(function (a) {
        var node = { k: "agent", id: "a" + (n++), label: a.label, labelRe: null, exactAgent: a.agentId,
                     phase: a.phase, model: null };
        nodes.push(node);
        items.push(node);
      });
    });
    return { meta: null, seq: { k: "seq", items: items }, agents: nodes, runtimeOnly: true };
  }

  // -------------------------------------------------------------- match

  /* Give each run agent a node of the graph. Returns
     { byNode: {nodeId: [agent, ...]}, unmatched: [agent, ...] } */
  function matchAgents(graph, agents) {
    var byNode = {}, unmatched = [], lastUnlabeled = null;
    var nodes = graph.agents;
    var compiled = nodes.map(function (n) {
      var re = null;
      if (n.labelRe) { try { re = new RegExp(n.labelRe); } catch (e) { re = null; } }
      return { node: n, re: re };
    });
    agents.forEach(function (a) {
      var exact = compiled.filter(function (c) { return c.node.exactAgent === a.agentId; });
      var cands = exact.length ? exact : compiled.filter(function (c) { return c.re && c.re.test(a.label || ""); });
      if (cands.length > 1 && a.phase) {
        var samePhase = cands.filter(function (c) { return c.node.phaseStatic && c.node.phase === a.phase; });
        if (samePhase.length) cands = samePhase;
        else {
          var notOther = cands.filter(function (c) { return !(c.node.phaseStatic && c.node.phase !== a.phase); });
          if (notOther.length) cands = notOther;
        }
      }
      if (!cands.length) {
        // nodes whose label we could not read: match by phase, then take
        // them in script order, one agent each, the last one repeating
        cands = compiled.filter(function (c) {
          return !c.re && !c.node.exactAgent && (!a.phase || !c.node.phase || c.node.phase === a.phase);
        });
        if (cands.length > 1) {
          var phased = cands.filter(function (c) { return (c.node.phase || null) === (a.phase || null); });
          if (phased.length) cands = phased;
          var fresh = cands.filter(function (c) { return !byNode[c.node.id]; });
          if (fresh.length) cands = fresh;
          else {
            var used = cands.filter(function (c) { return c.node.id === lastUnlabeled; });
            if (used.length) cands = used;
          }
        }
        if (cands.length) lastUnlabeled = cands[0].node.id;
      }
      if (cands.length > 1) {
        cands = cands.slice().sort(function (x, y) { return (y.node.labelLiteral || 0) - (x.node.labelLiteral || 0); });
        var best = cands[0].node.labelLiteral || 0;
        cands = cands.filter(function (c) { return (c.node.labelLiteral || 0) === best; });
      }
      if (!cands.length) { unmatched.push(a); return; }
      var target = cands[0].node;   // first in script order among the best
      (byNode[target.id] = byNode[target.id] || []).push(a);
    });
    return { byNode: byNode, unmatched: unmatched };
  }

  // ------------------------------------------------------------- layout

  var NODE_W = 230, NODE_H = 62, GAP = 26, PAD = 16, HEAD = 26, BR_LABEL = 18, COL_GAP = 22;
  var PHASE_H = 26, EXIT_W = 120, EXIT_H = 28, DIAMOND_H = 40;

  /* Measure a tree. Each measured item: {w, h, kind, ...children}. */
  function measure(node) {
    switch (node.k) {
      case "seq":
        var items = node.items.map(measure);
        if (!items.length) return { k: "seq", w: 24, h: 16, items: items, empty: true };
        var w = 0, h = 0;
        items.forEach(function (m, i) { w = Math.max(w, m.w); h += m.h + (i ? GAP : 0); });
        return { k: "seq", w: w, h: h, items: items, ends: items[items.length - 1].ends };
      case "agent": case "sub":
        return { k: node.k, w: NODE_W, h: NODE_H, node: node };
      case "phase":
        return { k: "phase", w: NODE_W, h: PHASE_H, node: node };
      case "exit":
        return { k: "exit", w: EXIT_W, h: EXIT_H, node: node, ends: true };
      case "branch": case "par":
        var brs = node.branches.map(function (b) { return { label: b.label, m: measure(b.seq) }; });
        var bw = 0, bh = 0;
        brs.forEach(function (b, i) { bw += b.m.w + (i ? COL_GAP : 0); bh = Math.max(bh, b.m.h); });
        var headH = node.k === "branch" ? DIAMOND_H : node.bare ? 0 : 22;
        var allEnd = brs.length > 0 && brs.every(function (b) { return b.m.ends; });
        return { k: node.k, node: node, brs: brs, headH: headH,
                 w: Math.max(bw, node.k === "branch" ? NODE_W : 140),
                 h: headH + GAP + BR_LABEL + bh + GAP, ends: allEnd };
      case "loop": case "each": case "group":
        var body = measure(node.body);
        return { k: node.k, node: node, body: body, w: Math.max(body.w + PAD * 2 + (node.k === "loop" ? 18 : 0), 260),
                 h: HEAD + PAD + body.h + PAD };
      default:
        return { k: "seq", w: 24, h: 16, items: [], empty: true };
    }
  }

  /* Place a measured tree. Pushes boxes and edges into out. */
  function place(m, x, y, out) {
    var cx = x + m.w / 2;
    switch (m.k) {
      case "seq":
        if (m.empty) { out.edges.push({ pts: [[cx, y], [cx, y + m.h]], plain: true }); return; }
        var yy = y, prev = null;
        m.items.forEach(function (it) {
          var ix = x + (m.w - it.w) / 2;
          if (prev && !prev.ends) out.edges.push({ pts: [[cx, yy - GAP], [cx, yy]] });
          place(it, ix, yy, out);
          yy += it.h + GAP;
          prev = it;
        });
        return;
      case "agent": case "sub": case "phase": case "exit":
        out.boxes.push({ kind: m.k, x: x, y: y, w: m.w, h: m.h, node: m.node });
        return;
      case "branch": case "par":
        var top = y + m.headH;
        if (m.k === "branch") out.boxes.push({ kind: "branch", x: cx - NODE_W / 2, y: y, w: NODE_W, h: m.headH, node: m.node });
        else if (!m.node.bare) out.boxes.push({ kind: "fork", x: cx - 70, y: y, w: 140, h: m.headH, node: m.node });
        var total = 0;
        m.brs.forEach(function (b, i) { total += b.m.w + (i ? COL_GAP : 0); });
        var bx = x + (m.w - total) / 2;
        var forkY = top + GAP / 2, brTop = top + GAP + BR_LABEL, joinY = y + m.h - GAP / 2;
        m.brs.forEach(function (b) {
          var bcx = bx + b.m.w / 2;
          out.edges.push({ pts: [[cx, top], [cx, forkY], [bcx, forkY], [bcx, brTop]] });
          if (b.label) out.labels.push({ x: bcx, y: top + GAP + 2, text: b.label });
          place(b.m, bx, brTop, out);
          if (!b.m.ends) out.edges.push({ pts: [[bcx, brTop + b.m.h], [bcx, joinY], [cx, joinY], [cx, y + m.h]], plain: true });
          bx += b.m.w + COL_GAP;
        });
        return;
      case "loop": case "each": case "group":
        out.boxes.push({ kind: m.k, x: x, y: y, w: m.w, h: m.h, node: m.node, container: true });
        var inner = x + (m.w - m.body.w) / 2 - (m.k === "loop" ? 9 : 0);
        var bodyTop = y + HEAD + PAD;
        place(m.body, inner, bodyTop, out);
        if (m.k === "loop") {
          var right = x + m.w - 12, bcx2 = inner + m.body.w / 2;
          out.edges.push({ pts: [[bcx2, bodyTop + m.body.h], [bcx2, bodyTop + m.body.h + PAD / 2],
                                 [right, bodyTop + m.body.h + PAD / 2], [right, bodyTop - PAD / 2],
                                 [bcx2, bodyTop - PAD / 2], [bcx2, bodyTop]], back: true });
        }
        return;
    }
  }

  function layout(graph) {
    var startEnd = { k: "seq", items: [{ k: "exit", id: "start", text: "start", start: true }]
                     .concat(graph.seq.items) };
    var last = graph.seq.items[graph.seq.items.length - 1];
    if (!last || last.k !== "exit" || !last.end) startEnd.items.push({ k: "exit", id: "end", text: "end", end: true });
    var m = measure(startEnd);
    var out = { boxes: [], edges: [], labels: [] };
    var margin = 24;
    place(m, margin, margin, out);
    out.width = m.w + margin * 2;
    out.height = m.h + margin * 2;
    return out;
  }

  /* The run agents shown by one node of the (possibly expanded) graph. */
  function instancesFor(node, match) {
    if (!node || !match) return [];
    if (node.baseId) {
      var all = (match.columns && match.columns[node.baseId]) || match.byNode[node.baseId] || [];
      return all[node.instanceIndex] ? [all[node.instanceIndex]] : [];
    }
    return match.byNode[node.id] || [];
  }

  function bindClone(node, index) {
    var copy = Object.assign({}, node);
    if (node.id) copy.id = node.id + "#" + index;
    if (node.k === "agent") { copy.baseId = node.id; copy.instanceIndex = index; }
    if (node.items) copy.items = node.items.map(function (n) { return bindClone(n, index); });
    if (node.branches) copy.branches = node.branches.map(function (b) { return { label: b.label, seq: bindClone(b.seq, index) }; });
    if (node.body) copy.body = bindClone(node.body, index);
    return copy;
  }

  /* Draw each started item of a fan-out (parallel map, pipeline) as its own
     column, so every parallel agent has its own node. Above ``limit`` items
     the fan-out stays one node with a count. Returns {graph, signature}. */
  /* The values a label template captured, as one key: "review:${f}" and
     "verify:${f}" give the same key for the same item. */
  function captureKey(node, agent) {
    if (!node.labelRe) return null;
    try {
      var m = new RegExp(node.labelRe).exec(agent.label || "");
      return m && m.length > 1 ? m.slice(1).join("\u0001") : null;
    } catch (e) { return null; }
  }

  /* Put the agents of each step of a fan-out into columns: one column per
     item. Agents whose labels carry the same item value share a column;
     the rest fill free columns in start order. */
  function assignColumns(agentNodes, match, count) {
    var keys = [];
    agentNodes.forEach(function (an) {
      if (keys.length) return;
      var inst = match.byNode[an.id] || [];
      if (inst.length) keys = inst.map(function (a) { return captureKey(an, a); });
    });
    match.columns = match.columns || {};
    agentNodes.forEach(function (an) {
      var inst = match.byNode[an.id] || [];
      var placed = new Array(count), rest = [];
      inst.forEach(function (a) {
        var k = captureKey(an, a), idx = k === null ? -1 : keys.indexOf(k);
        while (idx >= 0 && placed[idx]) idx = keys.indexOf(k, idx + 1);
        if (idx >= 0 && idx < count) placed[idx] = a; else rest.push(a);
      });
      for (var i = 0; i < count && rest.length; i++) if (!placed[i]) placed[i] = rest.shift();
      match.columns[an.id] = placed;
    });
  }

  function expandFanouts(graph, match, limit) {
    limit = limit || 24;
    var sig = [];
    function expand(node) {
      switch (node.k) {
        case "seq": return Object.assign({}, node, { items: node.items.map(expand) });
        case "branch": case "par":
          return Object.assign({}, node, { branches: node.branches.map(function (b) { return { label: b.label, seq: expand(b.seq) }; }) });
        case "loop": case "group": return Object.assign({}, node, { body: expand(node.body) });
        case "each":
          var count = 0;
          agentsBelow(node.body).forEach(function (a) { count = Math.max(count, (match.byNode[a.id] || []).length); });
          sig.push(node.id + ":" + Math.min(count, limit + 1));
          if (count > 1 && count <= limit) {
            assignColumns(agentsBelow(node.body), match, count);
            var branches = [];
            for (var i = 0; i < count; i++) branches.push({ label: "", seq: bindClone(node.body, i) });
            return Object.assign({}, node, { expanded: count,
              body: { k: "seq", items: [{ k: "par", id: node.id + "~cols", title: "", bare: true, branches: branches }] } });
          }
          return Object.assign({}, node, { body: expand(node.body) });
        default: return node;
      }
    }
    var seq = expand(graph.seq);
    return { graph: Object.assign({}, graph, { seq: seq }), signature: sig.join(",") };
  }

  /* Collect the agent nodes below a container node. */
  function agentsBelow(node) {
    var found = [];
    (function walk(n) {
      if (!n) return;
      if (n.k === "agent") found.push(n);
      if (n.items) n.items.forEach(walk);
      if (n.branches) n.branches.forEach(function (b) { walk(b.seq); });
      if (n.body) walk(n.body);
    })(node);
    return found;
  }

  var api = { buildGraph: buildGraph, runtimeGraph: runtimeGraph, matchAgents: matchAgents,
              layout: layout, agentsBelow: agentsBelow, expandFanouts: expandFanouts, instancesFor: instancesFor, findMeta: findMeta, parseScript: parseScript,
              literalValue: literalValue };
  root.WWGraph = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
"""

APP_JS = r"""/* workflow-watch page. Every piece of data goes into the page through
   textContent or attributes set by code: transcripts are untrusted text. */
(function () {
  "use strict";

  var POLL_MS = 2000;
  var G = window.WWGraph;

  // ------------------------------------------------------------ helpers

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "text") node.textContent = String(v);
        else if (k === "class") node.className = v;
        else if (k === "on") Object.keys(v).forEach(function (ev) { node.addEventListener(ev, v[ev]); });
        else if (k === "data") Object.keys(v).forEach(function (d) { node.dataset[d] = v[d]; });
        else if (k === "style") node.style.cssText = v;   // CSSOM, allowed by the page's CSP
        else node.setAttribute(k, v === true ? "" : String(v));
      });
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    });
    return node;
  }
  function svgEl(tag, attrs) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function fmtTime(ms) {
    if (!ms) return "—";
    var d = new Date(ms), now = new Date();
    var t = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
    if (d.toDateString() === now.toDateString()) return t;
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + t;
  }
  function fmtClock(ms) {
    if (!ms) return "";
    var d = new Date(ms);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function fmtDur(ms) {
    if (ms === null || ms === undefined || isNaN(ms)) return "—";
    if (ms < 0) ms = 0;
    var s = Math.floor(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    s = s % 60;
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + pad(s) + "s";
    return s + "s";
  }
  function fmtAgo(ms) {
    if (!ms) return "—";
    var d = Date.now() - ms;
    if (d < 5000) return "just now";
    return fmtDur(d) + " ago";
  }
  function fmtTokens(n) {
    if (n === null || n === undefined) return "—";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  }
  function stClass(status) { return "st-" + String(status || "unknown").toLowerCase().replace(/[^a-z]+/g, "-"); }
  function badge(status) { return el("span", { class: "badge " + stClass(status), text: status || "unknown" }); }

  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      return r.json().then(function (body) { if (!r.ok) { var e = new Error(body.error || r.status); e.status = r.status; throw e; } return body; });
    });
  }
  function setPulse(ok, text) {
    var p = document.getElementById("pulse");
    p.textContent = text;
    p.className = "pulse" + (ok ? "" : " bad");
  }
  function store(key, value) { try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { return null; } return null; }

  /* Live clocks: elements with data-since (elapsed) or data-ago. */
  function tickClocks() {
    document.querySelectorAll("[data-since]").forEach(function (n) {
      var since = Number(n.dataset.since), until = n.dataset.until ? Number(n.dataset.until) : Date.now();
      n.textContent = fmtDur(until - since);
    });
    document.querySelectorAll("[data-ago]").forEach(function (n) {
      n.textContent = (n.dataset.prefix || "") + fmtAgo(Number(n.dataset.ago));
    });
  }
  setInterval(tickClocks, 1000);
  function since(start, end) {
    if (!start) return el("span", { text: "—" });
    var s = el("span", { class: "num", data: end ? { since: String(start), until: String(end) } : { since: String(start) } });
    s.textContent = fmtDur((end || Date.now()) - start);
    return s;
  }
  function ago(ms, prefix) {
    if (!ms) return el("span", { text: "—" });
    var s = el("span", { data: { ago: String(ms), prefix: prefix || "" } });
    s.textContent = (prefix || "") + fmtAgo(ms);
    return s;
  }

  /* Render any JSON value readably, without knowing its keys. */
  function renderValue(v, depth) {
    depth = depth || 0;
    if (v === null) return el("span", { class: "val-lit", text: "null" });
    if (typeof v === "string") {
      return el("div", { class: "val-str", text: v });
    }
    if (typeof v !== "object") return el("span", { class: "val-lit", text: String(v) });
    if (depth > 8) return el("pre", { text: JSON.stringify(v, null, 1) });
    if (Array.isArray(v)) {
      if (!v.length) return el("span", { class: "val-lit", text: "[ ]" });
      var list = el("ol", { class: "val-list" });
      v.slice(0, 300).forEach(function (item) { list.appendChild(el("li", null, [renderValue(item, depth + 1)])); });
      if (v.length > 300) list.appendChild(el("li", { class: "sub", text: "… " + (v.length - 300) + " more" }));
      return list;
    }
    var keys = Object.keys(v);
    if (!keys.length) return el("span", { class: "val-lit", text: "{ }" });
    var dl = el("dl", { class: "val-obj" + (depth ? " val-nested" : "") });
    keys.forEach(function (k) {
      dl.appendChild(el("dt", { text: k }));
      dl.appendChild(el("dd", null, [renderValue(v[k], depth + 1)]));
    });
    return dl;
  }

  // ------------------------------------------------------------- router

  var view = null;   // the active view: {stop()}
  function route() {
    if (view && view.stop) view.stop();
    closeDrawer();
    var main = document.getElementById("main");
    clear(main);
    var hash = location.hash || "#/";
    var m = hash.match(/^#\/run\/([^/?]+)/);
    if (m) view = runView(main, decodeURIComponent(m[1]));
    else view = listView(main);
  }
  window.addEventListener("hashchange", route);

  // ---------------------------------------------------------- run list

  function listView(main) {
    document.getElementById("crumb").textContent = "All runs";
    document.title = "Workflow Watch";
    var stopped = false, timer = null;
    var projectSel = el("select", { "aria-label": "Project" }, [el("option", { value: "", text: "All projects" })]);
    var activeOnly = el("input", { type: "checkbox" });
    var search = el("input", { type: "search", placeholder: "Filter by name", "aria-label": "Filter by name" });
    projectSel.value = store("ww.project") || "";
    activeOnly.checked = store("ww.active") === "1";
    var filters = el("div", { class: "filters" }, [
      el("label", null, ["Project", projectSel]),
      el("label", null, [activeOnly, "Only running"]),
      search,
    ]);
    var notes = el("div");
    var tableBox = el("div", { class: "wrap-scroll" });
    var info = el("div", { class: "sub" });
    main.appendChild(el("section", { class: "section" }, [
      el("h2", { text: "Workflow runs" }),
      el("div", { class: "body" }, [filters, notes, tableBox, info]),
    ]));
    var last = null;
    [projectSel, activeOnly, search].forEach(function (c) {
      c.addEventListener("input", function () {
        store("ww.project", projectSel.value); store("ww.active", activeOnly.checked ? "1" : "0");
        if (last) render(last);
      });
    });

    function render(data) {
      last = data;
      clear(notes);
      (data.notes || []).forEach(function (n) { notes.appendChild(el("p", { class: "note", text: n })); });
      var projects = {};
      data.runs.forEach(function (r) { projects[r.cwd || r.project] = true; });
      var current = projectSel.value;
      var names = Object.keys(projects).sort();
      var have = Array.prototype.map.call(projectSel.options, function (o) { return o.value; }).slice(1);
      if (have.join("\n") !== names.join("\n")) {
        while (projectSel.options.length > 1) projectSel.remove(1);
        names.forEach(function (n) { projectSel.appendChild(el("option", { value: n, text: n })); });
        projectSel.value = names.indexOf(current) >= 0 ? current : "";
      }
      var q = search.value.trim().toLowerCase();
      var rows = data.runs.filter(function (r) {
        if (projectSel.value && (r.cwd || r.project) !== projectSel.value) return false;
        if (activeOnly.checked && ["running", "starting", "awaiting launch"].indexOf(r.status) < 0) return false;
        if (q && (r.name || "").toLowerCase().indexOf(q) < 0 && (r.id || "").toLowerCase().indexOf(q) < 0) return false;
        return true;
      });
      clear(tableBox);
      if (!rows.length) {
        tableBox.appendChild(el("div", { class: "empty", text: data.runs.length ? "No run matches the filter." :
          "No workflow runs found under " + data.root + "." }));
      } else {
        var tbody = el("tbody");
        rows.forEach(function (r) {
          var live = ["running", "starting", "awaiting launch"].indexOf(r.status) >= 0;
          var tr = el("tr", { class: "click" + (r.status === "running" ? " is-running" : ""), tabindex: "0",
            on: { click: function () { location.hash = "#/run/" + encodeURIComponent(r.id); },
                  keydown: function (e) { if (e.key === "Enter") location.hash = "#/run/" + encodeURIComponent(r.id); } } }, [
            el("td", null, [badge(r.status)]),
            el("td", null, [el("div", { class: "name-cell", text: r.name }), el("div", { class: "sub mono", text: r.id })]),
            el("td", { class: "hide-sm" }, [el("div", { text: r.cwd || r.project }), el("div", { class: "sub mono", text: r.sessionId })]),
            el("td", { class: "num" }, [fmtTime(r.start)]),
            el("td", { class: "num" }, [since(r.start, live ? null : (r.end || r.lastActivity))]),
            el("td", { class: "num" }, [(r.agentsDone || 0) + " / " + (r.agentsStarted || 0) +
              (r.agentsRunning ? " · " + r.agentsRunning + " running" : "")]),
            el("td", { class: "num hide-sm" }, [r.tokens ? (r.tokensExact ? "" : "~") + fmtTokens(r.tokens) : "—"]),
            el("td", { class: "num hide-sm" }, [live ? ago(r.lastActivity) : el("span", { text: "" })]),
          ]);
          tbody.appendChild(tr);
        });
        tableBox.appendChild(el("table", { class: "grid" }, [
          el("thead", null, [el("tr", null, ["Status", "Workflow", "Project", "Started", "Duration", "Agents done", "Tokens", "Last activity"].map(function (h, i) {
            return el("th", { class: (i >= 3 ? "num" : "") + (i === 2 || i >= 6 ? " hide-sm" : ""), text: h });
          }))]),
          tbody,
        ]));
      }
      info.textContent = "Reading " + data.root + " · " + data.runs.length + " runs · a run without any file change for " +
        data.staleMinutes + " min is shown as 'probably stopped'.";
    }

    function poll() {
      if (stopped) return;
      getJSON("/api/runs").then(function (data) {
        if (stopped) return;
        setPulse(true, "updated " + fmtClock(Date.now()));
        render(data);
      }).catch(function (e) { setPulse(false, "cannot reach the server (" + e.message + ")"); })
        .then(function () { if (!stopped) timer = setTimeout(poll, POLL_MS); });
    }
    poll();
    return { stop: function () { stopped = true; clearTimeout(timer); } };
  }

  // ----------------------------------------------------------- run view

  function runView(main, runId) {
    var stopped = false, timer = null, version = null;
    var st = { detail: null, stat: null, graph: null, graphNote: null, nodeEls: {}, match: null,
               zoom: 1, userZoom: false, runtimeKey: null, selected: null };
    document.getElementById("crumb").textContent = runId;

    var headBox = el("div", { class: "body" });
    var notesBox = el("div");
    var graphNoteBox = el("div");
    var canvasWrap = el("div", { class: "graph-wrap" });
    var zoomLabel = el("span", { class: "sub" });
    var tools = el("div", { class: "graph-tools" }, [
      el("button", { text: "−", title: "Zoom out", on: { click: function () { setZoom(st.zoom / 1.2); } } }),
      el("button", { text: "+", title: "Zoom in", on: { click: function () { setZoom(st.zoom * 1.2); } } }),
      el("button", { text: "Fit", title: "Fit the width", on: { click: fitZoom } }),
      el("button", { text: "100%", on: { click: function () { setZoom(1); } } }),
      zoomLabel,
    ]);
    var agentsBox = el("div", { class: "wrap-scroll" });
    var logsSection = el("section", { class: "section", hidden: true });
    var resultSection = el("section", { class: "section", hidden: true });
    var argsBox = el("div");
    var scriptBox = el("div");

    main.appendChild(el("section", { class: "section" }, [headBox]));
    main.appendChild(notesBox);
    main.appendChild(el("section", { class: "section" }, [
      el("h2", { text: "Flow" }),
      el("div", { class: "body" }, [graphNoteBox, tools]),
      canvasWrap,
    ]));
    main.appendChild(el("section", { class: "section" }, [el("h2", { text: "Agents" }), el("div", { class: "body" }, [agentsBox])]));
    main.appendChild(logsSection);
    main.appendChild(resultSection);
    main.appendChild(el("section", { class: "section" }, [el("h2", { text: "Input and script" }), el("div", { class: "body" }, [argsBox, scriptBox])]));

    window.addEventListener("resize", function () { if (!stopped) applyZoom(); });
    function setZoom(z) {
      st.userZoom = true;
      st.zoom = Math.max(0.3, Math.min(2, z));
      applyZoom();
    }
    function fitZoom() {
      var canvas = canvasWrap.querySelector(".canvas");
      if (!canvas || !st.layout) return;
      setZoom(Math.min(1.4, (canvasWrap.clientWidth - 8) / st.layout.width));
    }
    function applyZoom() {
      var canvas = canvasWrap.querySelector(".canvas");
      zoomLabel.textContent = Math.round(st.zoom * 100) + "%";
      if (!canvas || !st.layout) return;
      canvas.style.transform = "scale(" + st.zoom + ")";
      canvas.style.left = Math.max(0, (canvasWrap.clientWidth - st.layout.width * st.zoom) / 2) + "px";
      var sizer = canvasWrap.querySelector(".sizer");
      if (sizer) { sizer.style.width = st.layout.width * st.zoom + "px"; sizer.style.height = st.layout.height * st.zoom + "px"; }
    }

    // -- static: script and args, read once (again when the run ends)
    function loadStatic() {
      return getJSON("/api/static?id=" + encodeURIComponent(runId)).then(function (data) {
        st.stat = data;
        renderStatic();
        buildGraph();
      }).catch(function () { /* the poll shows the error */ });
    }

    function renderStatic() {
      clear(argsBox); clear(scriptBox);
      var s = st.stat || {};
      if (s.hasArgs) {
        argsBox.appendChild(el("details", null, [el("summary", { text: "Input (args)" }), el("div", { class: "box" }, [renderValue(s.args)])]));
      } else argsBox.appendChild(el("p", { class: "sub", text: "The input of this run is not known." }));
      if (s.script) {
        var lines = s.script.split("\n");
        var numbered = lines.map(function (l, i) { var n = String(i + 1); return "      ".slice(n.length) + n + "  " + l; }).join("\n");
        scriptBox.appendChild(el("details", null, [
          el("summary", { text: "Script (" + lines.length + " lines, from the " + (s.scriptOrigin || "?") + ")" }),
          s.scriptNote ? el("p", { class: "note", text: s.scriptNote }) : null,
          el("pre", { class: "box mono", text: numbered }),
        ]));
      } else {
        scriptBox.appendChild(el("p", { class: "sub", text: s.scriptNote || "The script of this run is not available." }));
      }
      if (s.resumeFrom) scriptBox.appendChild(el("p", { class: "sub", text: "Resumed from run " + s.resumeFrom + "." }));
    }

    function buildGraph() {
      st.graph = null; st.graphNote = null;
      var s = st.stat || {};
      if (s.script) {
        try { st.graph = G.buildGraph(s.script); }
        catch (e) { st.graphNote = "The script could not be read as JavaScript (" + e.message + "). The flow below shows the agents by phase."; }
      } else {
        st.graphNote = "The script is not available. The flow below shows the agents by phase, in start order.";
      }
      drawGraph();
    }

    function currentGraph() {
      if (st.graph) return st.graph;
      var agents = (st.detail && st.detail.agents) || [];
      var key = agents.map(function (a) { return a.agentId; }).join(",");
      if (st.runtimeKey !== key || !st.runtimeGraph) {
        st.runtimeKey = key;
        st.runtimeGraph = G.runtimeGraph(agents, st.detail && st.detail.phases);
        st.runtimeChanged = true;
      }
      return st.runtimeGraph;
    }

    function shownGraph() {
      var graph = currentGraph();
      var agents = (st.detail && st.detail.agents) || [];
      st.match = G.matchAgents(graph, agents);
      return G.expandFanouts(graph, st.match);
    }

    function drawGraph() {
      var shown = shownGraph();
      var graph = shown.graph;
      st.signature = shown.signature;
      st.runtimeChanged = false;
      clear(graphNoteBox);
      if (st.graphNote) graphNoteBox.appendChild(el("p", { class: "note", text: st.graphNote }));
      var lay = G.layout(graph);
      st.layout = lay;
      st.nodeEls = {};
      var canvas = el("div", { class: "canvas", style: "width:" + lay.width + "px;height:" + lay.height + "px" });
      var svg = svgEl("svg", { width: lay.width, height: lay.height, "aria-hidden": "true" });
      var defs = svgEl("defs");
      var marker = svgEl("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" });
      marker.appendChild(svgEl("path", { d: "M0,0 L10,5 L0,10 z", fill: "currentColor", class: "edge-head" }));
      defs.appendChild(marker);
      svg.appendChild(defs);
      svg.style.color = "var(--edge)";
      lay.edges.forEach(function (e) {
        var d = e.pts.map(function (p, i) { return (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ");
        var path = svgEl("path", { d: d, class: "edge" + (e.back ? " back" : "") });
        if (!e.plain) path.setAttribute("marker-end", "url(#arrow)");
        svg.appendChild(path);
      });
      canvas.appendChild(svg);
      lay.labels.forEach(function (l) {
        canvas.appendChild(el("div", { class: "edge-label", style: "left:" + l.x + "px;top:" + l.y + "px", text: l.text }));
      });
      // containers first, so the nodes sit above them
      lay.boxes.slice().sort(function (a, b) { return (b.container ? 1 : 0) - (a.container ? 1 : 0); }).forEach(function (b) {
        var n = b.node, box;
        var style = "left:" + b.x + "px;top:" + b.y + "px;width:" + b.w + "px;height:" + b.h + "px";
        if (b.kind === "agent") {
          box = el("div", { class: "gnode idle", style: style, tabindex: "0", role: "button" }, [
            el("div", { class: "l1" }, [el("span", { class: "tick-mark" }), el("span", { class: "lbl", text: n.label })]),
            el("div", { class: "l2" }), el("div", { class: "l3" }),
          ]);
          box.addEventListener("click", function () { openNode(n); });
          box.addEventListener("keydown", function (e) { if (e.key === "Enter") openNode(n); });
          box.title = (n.label || "") + (n.line ? "  (script line " + n.line + ")" : "");
        } else if (b.kind === "sub") {
          box = el("div", { class: "gnode idle", style: style }, [el("div", { class: "l1", text: n.title }), el("div", { class: "l2", text: "child workflow" })]);
        } else if (b.kind === "phase") {
          box = el("div", { class: "gnode phase", style: style }, [el("div", { class: "l1", text: n.title })]);
        } else if (b.kind === "exit") {
          box = el("div", { class: "gnode exit", style: style }, [el("div", { class: "l1", text: n.text })]);
        } else if (b.kind === "branch") {
          box = el("div", { class: "gnode branch", style: style, title: n.test + (n.line ? "  (line " + n.line + ")" : "") }, [
            el("div", { class: "l1", text: "if " + n.test }), el("div", { class: "l2 sub", text: n.line ? "line " + n.line : "" })]);
        } else if (b.kind === "fork") {
          box = el("div", { class: "gnode fork", style: style }, [el("div", { class: "l1", text: n.title + " ⇉" })]);
        } else if (b.container) {
          var title = n.k === "loop" ? "↻ " + n.title : n.k === "each" ? "⇶ " + n.title : n.title;
          box = el("div", { class: "gnode container " + n.k, style: style, title: title + (n.line ? "  (line " + n.line + ")" : "") }, [
            el("div", { class: "l1" }, [el("span", { class: "count" }), el("span", { text: title })]),
          ]);
        }
        if (box) { canvas.appendChild(box); if (n && n.id) st.nodeEls[n.id] = { el: box, node: n, kind: b.kind }; }
      });
      clear(canvasWrap);
      canvasWrap.style.position = "relative";
      canvas.style.position = "absolute";
      canvas.style.left = "0";
      canvas.style.top = "0";
      canvasWrap.appendChild(el("div", { class: "sizer" }));
      canvasWrap.appendChild(canvas);
      if (!st.userZoom) {
        var fit = (canvasWrap.clientWidth - 8) / lay.width;
        st.zoom = fit < 1 ? Math.max(0.45, fit) : 1;
      }
      applyZoom();
      paintGraph();
    }

    function nodeState(instances) {
      if (!instances || !instances.length) return "idle";
      if (instances.some(function (a) { return a.state === "running" && !a.quiet; })) return "running";
      if (instances.some(function (a) { return a.state === "running"; })) return "quiet";
      if (instances.some(function (a) { return a.state === "failed"; })) return "failed";
      if (instances.every(function (a) { return a.state === "done"; })) return "done";
      return "ended";
    }

    function paintGraph() {
      var d = st.detail;
      if (!d) return;
      var shown = shownGraph();
      if (st.runtimeChanged || shown.signature !== st.signature) { drawGraph(); return; }
      Object.keys(st.nodeEls).forEach(function (id) {
        var info = st.nodeEls[id], n = info.node, box = info.el;
        if (info.kind === "agent") {
          var inst = G.instancesFor(n, st.match);
          var state = nodeState(inst);
          box.className = "gnode " + state + (st.selected === id ? " selected" : "");
          var mark = box.querySelector(".tick-mark"), l2 = box.querySelector(".l2"), l3 = box.querySelector(".l3");
          var lbl = box.querySelector(".lbl");
          var latest = inst.length ? inst[inst.length - 1] : null;
          lbl.textContent = latest ? latest.label : n.label;
          box.title = (latest ? latest.label + "  ·  " : "") + "script: " + (n.label || "") + (n.line ? " (line " + n.line + ")" : "");
          mark.textContent = state === "done" ? "✔" : state === "failed" ? "✖" : state === "ended" ? "–" : inst.length > 1 ? "×" + inst.length : "";
          if (inst.length > 1 && state === "done") mark.textContent = "✔ ×" + inst.length;
          var meta = [];
          if (n.phase) meta.push(n.phase);
          var model = (latest && latest.model) || n.model;
          if (model) meta.push(model);
          if (inst.length > 1) meta.push(inst.length + " runs");
          l2.textContent = meta.join(" · ");
          clear(l3);
          if (!latest) l3.textContent = "not started";
          else if (latest.state === "running") {
            var tool = latest.lastTool;
            l3.appendChild(document.createTextNode(tool ? "▸ " + tool.name + ": " + (tool.summary || "") + " · " : "working · "));
            l3.appendChild(ago(latest.lastActivity));
          } else if (latest.state === "failed") l3.textContent = "returned nothing · " + fmtDur((latest.end || 0) - (latest.start || 0));
          else if (latest.state !== "done") l3.textContent = "ended without a result";
          else {
            l3.textContent = (latest.cached ? "from cache" : "done in " + fmtDur((latest.end || 0) - (latest.start || 0))) +
              (latest.toolCalls != null ? " · " + latest.toolCalls + " tools" : "");
          }
        } else if (info.kind === "loop" || info.kind === "each" || info.kind === "group") {
          var below = G.agentsBelow(n.body), all = [];
          var maxCount = 0;
          below.forEach(function (an) { var i = G.instancesFor(an, st.match); all = all.concat(i); maxCount = Math.max(maxCount, i.length); });
          if (n.k === "each" && n.expanded) maxCount = n.expanded;
          var cst = nodeState(all);
          box.className = "gnode container " + n.k + (cst === "running" ? " running" : "");
          var count = box.querySelector(".count");
          if (n.k === "loop") count.textContent = maxCount ? "round " + maxCount : "";
          else if (n.k === "each") {
            var done = all.filter(function (a) { return a.state === "done" || a.state === "failed"; }).length;
            count.textContent = all.length ? done + " / " + all.length + " done" : "";
          } else count.textContent = "";
        }
      });
      if (st.match.unmatched.length) {
        var names = st.match.unmatched.map(function (a) { return a.label; });
        var exists = graphNoteBox.querySelector(".unmatched");
        var text = "Not placed in the flow (their label does not match the script): " + names.slice(0, 12).join(", ") +
          (names.length > 12 ? " …" : "") + ". They are in the Agents list below.";
        if (!exists) graphNoteBox.appendChild(el("p", { class: "note unmatched", text: text }));
        else exists.textContent = text;
      }
    }

    function openNode(n) {
      var inst = G.instancesFor(n, st.match);
      st.selected = n.id;
      paintGraph();
      openDrawer(runId, n, inst, function () { st.selected = null; paintGraph(); });
    }

    // -- header, agents, logs, result
    function renderHead() {
      var d = st.detail, r = d.run;
      document.title = r.name + " · Workflow Watch";
      document.getElementById("crumb").textContent = r.name + " · " + r.id;
      clear(headBox);
      var live = ["running", "starting", "awaiting launch"].indexOf(r.status) >= 0;
      var desc = (st.graph && st.graph.meta && st.graph.meta.description) || r.summary;
      headBox.appendChild(el("div", { class: "head" }, [
        el("h1", null, [el("span", { text: r.name }), badge(r.status)]),
        desc ? el("div", { class: "desc", text: desc }) : null,
        r.statusNote ? el("div", { class: "sub", text: r.statusNote }) : null,
      ]));
      var facts = [
        ["Project", r.cwd || r.project], ["Session", r.sessionId], ["Run", r.runId || r.id], ["Task", r.taskId || "—"],
        ["Started", fmtTime(r.start)], [live ? "Elapsed" : "Duration", since(r.start, live ? null : (r.end || r.lastActivity))],
        ["Agents", (r.agentsDone || 0) + " done of " + (r.agentsStarted || 0) + (r.agentsRunning ? " · " + r.agentsRunning + " running" : "")],
        ["Tool calls", r.toolCalls != null ? String(r.toolCalls) : "—"],
        ["Tokens", r.tokens ? (r.tokensExact ? "" : "~") + fmtTokens(r.tokens) : "—"],
        ["Last activity", live ? ago(r.lastActivity) : fmtTime(r.lastActivity)],
      ];
      headBox.appendChild(el("div", { class: "facts" }, facts.map(function (f) {
        return el("div", { class: "fact" }, [el("div", { class: "k", text: f[0] }), el("div", { class: "v" }, [f[1]])]);
      })));
      var phases = (st.graph && st.graph.meta && st.graph.meta.phases) || d.phases;
      if (phases && phases.length) {
        var chips = el("div", { class: "phase-chips" });
        phases.forEach(function (p) {
          var title = p && p.title ? String(p.title) : String(p);
          var inPhase = d.agents.filter(function (a) { return a.phase === title; });
          var done = inPhase.filter(function (a) { return a.state === "done" || a.state === "failed"; }).length;
          var chip = el("span", { class: "chip", title: p && p.detail ? String(p.detail) : "" }, [el("b", { text: title }), " " + done + "/" + inPhase.length]);
          chips.appendChild(chip);
        });
        headBox.appendChild(chips);
      }
    }

    function renderNotes() {
      clear(notesBox);
      (st.detail.notes || []).forEach(function (n) { notesBox.appendChild(el("p", { class: "note", text: n })); });
    }

    function renderAgents() {
      var d = st.detail;
      clear(agentsBox);
      if (!d.agents.length) { agentsBox.appendChild(el("div", { class: "empty", text: "No agent has started yet." })); return; }
      var order = [];
      var phases = (st.graph && st.graph.meta && st.graph.meta.phases) || d.phases || [];
      phases.forEach(function (p) { var t = p && p.title ? String(p.title) : String(p); if (order.indexOf(t) < 0) order.push(t); });
      d.agents.forEach(function (a) { var t = a.phase || ""; if (order.indexOf(t) < 0) order.push(t); });
      var tbody = el("tbody");
      order.forEach(function (phase) {
        var list = d.agents.filter(function (a) { return (a.phase || "") === phase; });
        if (!list.length) return;
        tbody.appendChild(el("tr", { class: "phase-row" }, [el("td", { colspan: "8", text: phase || "(no phase)" })]));
        list.forEach(function (a) {
          var state = a.state === "running" && a.quiet ? "quiet" : a.state;
          var now = "";
          if (a.state === "running" && a.lastTool) now = "▸ " + a.lastTool.name + ": " + (a.lastTool.summary || "");
          else if (a.resultPreview) now = a.resultPreview;
          tbody.appendChild(el("tr", { class: "click" + (a.state === "running" ? " is-running" : ""), tabindex: "0",
            on: { click: function () { openAgentById(a.agentId); },
                  keydown: function (e) { if (e.key === "Enter") openAgentById(a.agentId); } } }, [
            el("td", null, [badge(state)]),
            el("td", null, [el("div", { class: "name-cell", text: a.label }), el("div", { class: "now-line", text: now })]),
            el("td", { class: "hide-sm", text: a.model || "—" }),
            el("td", { class: "num", text: fmtTime(a.start) }),
            el("td", { class: "num" }, [since(a.start, a.state === "running" ? null : a.end)]),
            el("td", { class: "num", text: a.toolCalls != null ? String(a.toolCalls) : "—" }),
            el("td", { class: "num hide-sm", text: fmtTokens(a.tokens || a.contextTokens) }),
            el("td", { class: "num hide-sm" }, [a.state === "running" ? ago(a.lastActivity) : el("span", { text: a.cached ? "cached" : "" })]),
          ]));
        });
      });
      agentsBox.appendChild(el("table", { class: "grid" }, [
        el("thead", null, [el("tr", null, ["State", "Agent", "Model", "Started", "Duration", "Tools", "Tokens", "Activity"].map(function (h, i) {
          return el("th", { class: (i >= 3 ? "num" : "") + (i === 2 || i >= 6 ? " hide-sm" : ""), text: h });
        }))]),
        tbody,
      ]));
    }

    function openAgentById(agentId) {
      var a = st.detail.agents.filter(function (x) { return x.agentId === agentId; })[0];
      if (!a) return;
      var nodeId = null;
      Object.keys(st.nodeEls).forEach(function (id) {
        var info = st.nodeEls[id];
        if (info.kind === "agent" && G.instancesFor(info.node, st.match).some(function (x) { return x.agentId === agentId; })) nodeId = id;
      });
      var node = nodeId ? st.nodeEls[nodeId].node : { id: null, label: a.label };
      st.selected = nodeId;
      paintGraph();
      var inst = nodeId ? G.instancesFor(node, st.match) : [a];
      openDrawer(runId, node, inst, function () { st.selected = null; paintGraph(); }, agentId);
    }

    function renderLogsAndResult() {
      var d = st.detail;
      clear(logsSection); clear(resultSection);
      if (d.logs && d.logs.length) {
        logsSection.hidden = false;
        logsSection.appendChild(el("h2", { text: "Log lines" }));
        logsSection.appendChild(el("div", { class: "body" }, [el("pre", { class: "box", text: d.logs.map(String).join("\n") })]));
      } else logsSection.hidden = true;
      if (d.hasResult) {
        resultSection.hidden = false;
        resultSection.appendChild(el("h2", { text: "What the script returned" }));
        resultSection.appendChild(el("div", { class: "body" }, [el("div", { class: "box" }, [renderValue(d.result)])]));
      } else resultSection.hidden = true;
    }

    var lastStatus = null;
    function poll() {
      if (stopped) return;
      getJSON("/api/run?id=" + encodeURIComponent(runId) + (version ? "&v=" + version : "")).then(function (data) {
        if (stopped) return;
        setPulse(true, "updated " + fmtClock(Date.now()));
        if (data.moved) { location.hash = "#/run/" + encodeURIComponent(data.moved); return; }
        if (data.unchanged) { paintGraph(); return; }
        version = data.version;
        st.detail = data;
        renderHead(); renderNotes(); renderAgents(); renderLogsAndResult();
        paintGraph();
        if (lastStatus !== null && lastStatus !== data.run.status) loadStatic();
        lastStatus = data.run.status;
        refreshDrawer(runId, st.match);
      }).catch(function (e) {
        if (e.status === 404) {
          clear(headBox);
          headBox.appendChild(el("div", { class: "empty", text: "This run is not known (any more)." }));
          stopped = true;
          return;
        }
        setPulse(false, "cannot reach the server (" + e.message + ")");
      }).then(function () { if (!stopped) timer = setTimeout(poll, POLL_MS); });
    }
    loadStatic().then(poll);
    return { stop: function () { stopped = true; clearTimeout(timer); } };
  }

  // ------------------------------------------------------------- drawer

  var drawer = { run: null, node: null, instances: [], agentId: null, steps: [], from: 0, timer: null, onClose: null,
                 showThinking: store("ww.thinking") === "1", busy: false };

  function closeDrawer() {
    var d = document.getElementById("drawer");
    d.hidden = true;
    clear(d);
    drawer.dom = null;
    clearTimeout(drawer.timer);
    var cb = drawer.onClose;
    drawer.run = null; drawer.onClose = null;
    document.body.classList.remove("with-drawer");
    window.dispatchEvent(new Event("resize"));
    if (cb) cb();
  }
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && drawer.run) closeDrawer(); });

  function openDrawer(runId, node, instances, onClose, agentId) {
    clearTimeout(drawer.timer);
    drawer.onClose = null;
    drawer.run = runId; drawer.node = node; drawer.instances = instances.slice();
    document.body.classList.add("with-drawer");
    window.dispatchEvent(new Event("resize"));
    drawer.onClose = onClose;
    var pick = agentId;
    if (!pick && instances.length) {
      var running = instances.filter(function (a) { return a.state === "running"; });
      pick = (running.length ? running[running.length - 1] : instances[instances.length - 1]).agentId;
    }
    selectAgent(pick || null);
  }

  /* After each run poll: keep the open node's list of agents fresh, and
     show the first agent of a node that just started. */
  function refreshDrawer(runId, match) {
    if (drawer.run !== runId || !match || !drawer.node || !drawer.node.id) return;
    var inst = G.instancesFor(drawer.node, match);
    if (inst.length === drawer.instances.length) { drawer.instances = inst.slice(); return; }
    drawer.instances = inst.slice();
    if (!drawer.agentId && inst.length) selectAgent(inst[inst.length - 1].agentId);
    else renderDrawer();
  }

  function selectAgent(agentId) {
    clearTimeout(drawer.timer);
    drawer.agentId = agentId; drawer.steps = []; drawer.from = 0; drawer.data = null; drawer.dom = null;
    var d = document.getElementById("drawer");
    d.hidden = false;
    renderDrawer();
    if (agentId) pollAgent();
  }

  function pollAgent() {
    var runId = drawer.run, agentId = drawer.agentId;
    if (!runId || !agentId) return;
    getJSON("/api/agent?run=" + encodeURIComponent(runId) + "&agent=" + encodeURIComponent(agentId) + "&from=" + drawer.from)
      .then(function (data) {
        if (drawer.run !== runId || drawer.agentId !== agentId) return;
        if (data.from === 0 && drawer.data && drawer.data.prompt && !data.prompt) data.prompt = drawer.data.prompt;
        if (data.from !== 0 && drawer.data) data.prompt = drawer.data.prompt;
        if (data.stepCount < drawer.from) { drawer.steps = []; }   // transcript was replaced
        // a step that was open may have got its tool result: refresh the last few
        drawer.steps = drawer.steps.slice(0, data.from).concat(data.steps);
        drawer.from = Math.max(0, drawer.steps.length - 3);
        drawer.data = data;
        renderDrawer();
      })
      .catch(function () { /* keep the last view */ })
      .then(function () {
        if (drawer.run === runId && drawer.agentId === agentId) {
          var live = drawer.data && drawer.data.agent && drawer.data.agent.state === "running";
          drawer.timer = setTimeout(pollAgent, live ? POLL_MS : POLL_MS * 3);
        }
      });
  }

  /* The panel is built once per agent and then updated in place. Rebuilding
     it on every poll moved what the reader was looking at; now only the parts
     that changed are replaced, new steps are appended at the end, and the
     browser's scroll anchoring keeps the reader's place. */
  function drawerSkeleton() {
    var box = document.getElementById("drawer");
    clear(box);
    var dom = { sig: {}, items: {} };
    dom.title = el("h3");
    dom.status = el("div", { class: "sub" });
    box.appendChild(el("div", { class: "dhead" }, [
      el("div", { style: "flex:1;min-width:0" }, [dom.title, dom.status]),
      el("button", { text: "Close", on: { click: closeDrawer } }),
    ]));
    dom.body = el("div", { class: "dbody" });
    box.appendChild(dom.body);
    dom.instances = el("div");
    dom.top = el("div");
    dom.result = el("div");
    dom.prompt = el("div");
    dom.count = el("span");
    var toggle = el("input", { type: "checkbox", on: { change: function (e) {
      drawer.showThinking = e.target.checked; store("ww.thinking", e.target.checked ? "1" : "0"); renderDrawer(); } } });
    toggle.checked = drawer.showThinking;
    dom.stepsHead = el("h4", null, [dom.count, "  ", el("label", { class: "sub", style: "text-transform:none;letter-spacing:0;font-weight:400" }, [toggle, " show thinking"])]);
    dom.list = el("ul", { class: "steps" });
    dom.meta = el("div");
    [dom.instances, dom.top, dom.result, dom.prompt, dom.stepsHead, dom.list, dom.meta].forEach(function (n) { dom.body.appendChild(n); });
    return dom;
  }

  function changed(dom, key, value) {
    var sig = JSON.stringify(value);
    if (dom.sig[key] === sig) return false;
    dom.sig[key] = sig;
    return true;
  }

  function stepSig(s) { return [s.result !== undefined, !!s.isError, s.summary || "", (s.text || "").length].join("|"); }

  function stepItem(s) {
    var kind = s.kind === "tool" ? s.name : s.kind === "text" ? "message" : s.kind === "tool_result" ? "result" : s.kind;
    var li = el("li", { data: { i: String(s.i) } }, [
      el("div", { class: "srow" }, [
        el("span", { class: "stime", text: fmtClock(s.time) }),
        el("span", { class: "skind" + (s.kind === "tool" ? " tool-" + String(s.name || "").toLowerCase().replace(/[^a-z]/g, "") : ""), text: kind }),
        el("span", { class: "stext" }),
      ]),
    ]);
    var item = { li: li, s: s, sig: null };
    li.firstChild.addEventListener("click", function () {
      var open = li.classList.toggle("open");
      var det = li.querySelector(".sdetail");
      if (open && !det) li.appendChild(stepDetail(item.s));
      if (!open && det) li.removeChild(det);
    });
    updateStepItem(item, s);
    return item;
  }

  function updateStepItem(item, s) {
    var sig = stepSig(s);
    if (item.sig === sig) return;
    var wasPending = item.sig !== null && item.s.result === undefined && s.result !== undefined;
    item.sig = sig;
    item.s = s;
    item.li.className = "k-" + s.kind + (s.isError ? " err" : "") + (item.li.classList.contains("open") ? " open" : "");
    var text = s.kind === "tool" ? (s.summary || "") : (s.text || "");
    item.li.querySelector(".stext").textContent = text.split("\n")[0] || (s.kind === "thinking" ? "(hidden by the model)" : "");
    var det = item.li.querySelector(".sdetail");
    if (det && wasPending) item.li.replaceChild(stepDetail(s), det);   // its result arrived
  }

  function renderDrawer() {
    if (!drawer.dom) drawer.dom = drawerSkeleton();
    var dom = drawer.dom;
    var data = drawer.data, a = data && data.agent;
    var node = drawer.node || {};
    dom.title.textContent = a ? a.label : (node.label || "Agent");
    if (changed(dom, "status", [a && a.state, a && a.quiet, drawer.agentId, node.line])) {
      clear(dom.status);
      dom.status.appendChild(a ? badge(a.state === "running" && a.quiet ? "quiet" : a.state) : badge(drawer.agentId ? "loading" : "not started"));
      if (node.line) dom.status.appendChild(document.createTextNode("  script line " + node.line));
    }
    if (changed(dom, "instances", drawer.instances.map(function (x) { return [x.agentId, x.state, x.label]; }).concat([drawer.agentId]))) {
      clear(dom.instances);
      if (drawer.instances.length > 1) {
        dom.instances.appendChild(el("h4", { text: "Runs of this step (" + drawer.instances.length + ")" }));
        var inst = el("div", { class: "instances" });
        drawer.instances.forEach(function (x) {
          inst.appendChild(el("button", { class: x.agentId === drawer.agentId ? "on" : "", text: x.label + (x.state === "running" ? " ●" : ""),
            on: { click: function () { selectAgent(x.agentId); } } }));
        });
        dom.instances.appendChild(inst);
      }
    }

    if (!drawer.agentId || !data) {
      if (changed(dom, "top", ["empty", drawer.agentId, !!data])) {
        clear(dom.top);
        if (!drawer.agentId) {
          dom.top.appendChild(el("p", { class: "sub", text: "This step has not started in this run." }));
          if (node.prompt) { dom.top.appendChild(el("h4", { text: "Prompt in the script" })); dom.top.appendChild(el("pre", { class: "box", text: node.prompt })); }
        } else dom.top.appendChild(el("p", { class: "sub", text: "Loading…" }));
      }
      return;
    }

    var lastStep = a.lastTool && a.lastTool.step != null ? drawer.steps[a.lastTool.step] : null;
    if (changed(dom, "top", [a.state, a.quiet, a.phase, a.model, a.start, a.end, a.toolCalls, a.tokens, a.contextTokens,
                             a.outputTokens, data.cwd, data.gitBranch, a.attempt, a.lastTool && [a.lastTool.step, a.lastTool.time],
                             !!(lastStep && lastStep.command), lastStep && lastStep.result !== undefined, a.lastText && a.lastText.time])) {
      clear(dom.top);
      var facts = [
        ["Phase", a.phase || "—"], ["Model", a.model || "—"], ["Started", fmtTime(a.start)],
        [a.state === "running" ? "Elapsed" : "Duration", since(a.start, a.state === "running" ? null : a.end)],
        ["Tool calls", a.toolCalls != null ? String(a.toolCalls) : "—"],
        ["Tokens", a.tokens ? fmtTokens(a.tokens) : (a.contextTokens ? fmtTokens(a.contextTokens) + " context, " + fmtTokens(a.outputTokens) + " out" : "—")],
        ["Agent id", a.agentId], ["Working dir", data.cwd || "—"],
      ];
      if (data.gitBranch) facts.push(["Branch", data.gitBranch]);
      if (a.attempt && a.attempt > 1) facts.push(["Attempt", String(a.attempt)]);
      dom.top.appendChild(el("div", { class: "facts" }, facts.map(function (f) {
        return el("div", { class: "fact" }, [el("div", { class: "k", text: f[0] }), el("div", { class: "v" }, [f[1]])]);
      })));
      dom.lastActivity = null;
      if (a.state === "running") {
        dom.top.appendChild(el("h4", { text: "Now" }));
        var now = el("div", { class: "now-box" + (a.quiet ? " quiet" : "") });
        if (a.lastTool) {
          now.appendChild(el("div", null, [el("b", { text: a.lastTool.name }), " " + (a.lastTool.summary || "")]));
          if (lastStep && lastStep.command) now.appendChild(codeBlock(lastStep.command, { lang: "bash", lineNumbers: false, term: true, cmd: true, maxLines: 12 }));
          now.appendChild(el("div", { class: "sub" }, [ago(a.lastTool.time, "called "), lastStep && lastStep.result === undefined ? " · waiting for its result" : ""]));
        } else now.appendChild(el("div", { text: "Starting…" }));
        dom.lastActivity = ago(a.lastActivity, "last activity ");
        now.appendChild(el("div", { class: "sub" }, [dom.lastActivity]));
        dom.top.appendChild(now);
        if (a.lastText) {
          dom.top.appendChild(el("h4", { text: "Last message" }));
          dom.top.appendChild(el("div", { class: "box" }, [codeBlockOrProse(a.lastText.text)]));
        }
      }
    } else if (dom.lastActivity && a.lastActivity) {
      dom.lastActivity.dataset.ago = String(a.lastActivity);   // the clock redraws it
    }

    if (changed(dom, "result", [data.hasResult, a.state, a.cached, a.resultPreview || ""])) {
      clear(dom.result);
      if (data.hasResult) {
        dom.result.appendChild(el("h4", { text: a.cached ? "Returned (from cache)" : "Returned" }));
        dom.result.appendChild(el("div", { class: "box" }, [renderValue(data.result)]));
      } else if (a.state !== "running" && a.resultPreview) {
        dom.result.appendChild(el("h4", { text: "Returned (preview)" }));
        dom.result.appendChild(el("div", { class: "box val-str", text: a.resultPreview }));
      }
    }

    if (data.prompt && changed(dom, "prompt", data.prompt.length)) {
      clear(dom.prompt);
      dom.prompt.appendChild(el("details", null, [el("summary", { text: "Prompt (" + data.prompt.length + " characters)" }),
        el("div", { class: "box" }, [codeBlockOrProse(data.prompt)])]));
    }

    dom.count.textContent = "Steps (" + data.stepCount + ")";
    if (changed(dom, "thinking", drawer.showThinking)) { clear(dom.list); dom.items = {}; }
    var shown = drawer.steps.filter(function (s) { return drawer.showThinking || s.kind !== "thinking"; });
    if (!shown.length) {
      if (!dom.list.firstChild) dom.list.appendChild(el("li", { class: "sub empty-steps", text: "No steps yet." }));
    } else {
      var placeholder = dom.list.querySelector(".empty-steps");
      if (placeholder) dom.list.removeChild(placeholder);
    }
    shown.forEach(function (s) {
      var item = dom.items[s.i];
      if (item) updateStepItem(item, s);
      else { item = stepItem(s); dom.items[s.i] = item; dom.list.appendChild(item.li); }
    });

    if (data.meta && changed(dom, "meta", true)) {
      clear(dom.meta);
      dom.meta.appendChild(el("details", null, [el("summary", { text: "Agent facts (meta file)" }), el("div", { class: "box" }, [renderValue(data.meta)])]));
    }
  }

  // --------------------------------------------------------- tool views
  /* Each tool call is shown in a form that fits it: a terminal for Bash, a
     diff for Edit, a code file for Read and Write, a form for the rest.
     Never raw JSON. All text goes in through textContent. */

  var H = window.WWHighlight;
  var PREVIEW_LINES = 300;

  function stripAnsi(text) { return String(text || "").replace(/\u001b\[[0-9;?]*[ -\/]*[@-~]/g, ""); }

  function tokenSpans(parent, toks) {
    toks.forEach(function (t) {
      parent.appendChild(t.c ? el("span", { class: "tk-" + t.c, text: t.t }) : document.createTextNode(t.t));
    });
    if (!toks.length) parent.appendChild(document.createTextNode("​"));
  }

  /* A code block with optional line numbers. opts: lang, numbers (array),
     start, lineNumbers (false hides), term, cmd, maxLines. */
  function codeBlock(text, opts) {
    opts = opts || {};
    text = String(text == null ? "" : text);
    var lines = H.lines(text, opts.lang || null);
    if (lines.length > 1 && !lines[lines.length - 1].length && /\n$/.test(text)) lines.pop();
    var wrap = el("div", { class: "code" + (opts.term ? " term" : "") + (opts.cmd ? " cmd" : "") });
    var body = el("div", { class: "code-body" });
    wrap.appendChild(body);
    var start = opts.start || 1;
    function row(i) {
      var r = el("div", { class: "cl" + (opts.rowClass ? " " + opts.rowClass(i) : "") });
      if (opts.lineNumbers !== false) {
        var n = opts.numbers ? opts.numbers[i] : start + i;
        r.appendChild(el("span", { class: "ln", text: n == null ? "" : String(n) }));
      }
      var c = el("span", { class: "lc" });
      tokenSpans(c, lines[i]);
      r.appendChild(c);
      return r;
    }
    var limit = Math.min(lines.length, opts.maxLines || PREVIEW_LINES);
    for (var i = 0; i < limit; i++) body.appendChild(row(i));
    if (limit < lines.length) {
      var more = el("button", { class: "more", text: "Show all " + lines.length + " lines" });
      more.addEventListener("click", function () {
        for (var j = limit; j < lines.length; j++) body.appendChild(row(j));
        wrap.removeChild(more);
      });
      wrap.appendChild(more);
    }
    return wrap;
  }

  function chip(text, cls) { return el("span", { class: "tchip" + (cls ? " " + cls : ""), text: text }); }

  function fileHeader(path, chips) {
    path = String(path || "");
    var cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
    var head = el("div", { class: "fhead", title: path }, [
      el("span", { class: "fdir", text: cut >= 0 ? path.slice(0, cut + 1) : "" }),
      el("span", { class: "fname", text: cut >= 0 ? path.slice(cut + 1) : path }),
    ]);
    var box = el("div", { class: "fchips" });
    (chips || []).forEach(function (c) { if (c) box.appendChild(c); });
    head.appendChild(box);
    return head;
  }

  function label(text) { return el("div", { class: "tlabel", text: text }); }

  function proseBlock(text) { return el("div", { class: "prose", text: String(text || "") }); }

  /* Split "Exit code 2\n..." and the reminders Claude Code appends. */
  function splitResult(text) {
    text = stripAnsi(text);
    var exit = null, notes = [];
    var m = /^Exit code (\d+)\n?/.exec(text);
    if (m) { exit = Number(m[1]); text = text.slice(m[0].length); }
    text = text.replace(/\n*<system-reminder>[\s\S]*?<\/system-reminder>\n*/g, function (r) {
      notes.push(r.replace(/<\/?system-reminder>/g, "").trim()); return "\n";
    }).replace(/\s+$/, "");
    return { text: text, exit: exit, notes: notes };
  }

  function resultNotes(parts, notes) {
    if (!notes.length) return;
    parts.push(el("details", { class: "rnotes" }, [el("summary", { text: "Notes Claude Code added to the result (" + notes.length + ")" }),
      el("div", { class: "prose sub", text: notes.join("\n\n") })]));
  }

  function lineStats(rows) {
    var add = 0, del = 0;
    rows.forEach(function (r) { if (r.k === "+") add++; else if (r.k === "-") del++; });
    return el("span", { class: "dstat" }, [el("span", { class: "dadd", text: "+" + add }), " ", el("span", { class: "ddel", text: "−" + del })]);
  }

  /* A diff of two texts with three lines of context around each change. */
  function diffBlock(oldText, newText, lang) {
    var rows = H.diffLines(oldText, newText);
    var a = H.lines(oldText, lang), b = H.lines(newText, lang);
    var keep = rows.map(function () { return false; });
    rows.forEach(function (r, i) {
      if (r.k !== " ") for (var j = Math.max(0, i - 3); j <= Math.min(rows.length - 1, i + 3); j++) keep[j] = true;
    });
    var wrap = el("div", { class: "code diff" });
    var body = el("div", { class: "code-body" });
    wrap.appendChild(body);
    var skipped = 0;
    function flushSkip() {
      if (skipped) body.appendChild(el("div", { class: "cl dskip" }, [el("span", { class: "ln", text: "" }), el("span", { class: "lc", text: "⋯ " + skipped + " unchanged line" + (skipped > 1 ? "s" : "") })]));
      skipped = 0;
    }
    rows.forEach(function (r, i) {
      if (!keep[i]) { skipped++; return; }
      flushSkip();
      var toks = r.k === "+" ? b[r.b] : a[r.a];
      var line = el("div", { class: "cl" + (r.k === "+" ? " dadd-row" : r.k === "-" ? " ddel-row" : "") }, [
        el("span", { class: "ln dsign", text: r.k === " " ? "" : r.k === "-" ? "−" : "+" }),
      ]);
      var c = el("span", { class: "lc" });
      tokenSpans(c, toks || []);
      line.appendChild(c);
      body.appendChild(line);
    });
    flushSkip();
    return { el: wrap, stats: lineStats(rows) };
  }

  function errorBox(text) { return el("div", { class: "terr" }, [codeBlock(stripAnsi(text), { lang: "log", lineNumbers: false })]); }

  function pending(parts) { parts.push(el("div", { class: "tpending", text: "Waiting for the result…" })); }

  function viewBash(inp, d) {
    var parts = [];
    var chips = [];
    if (inp.run_in_background) chips.push(chip("in background"));
    if (inp.timeout) chips.push(chip("timeout " + fmtDur(Number(inp.timeout))));
    if (inp.description) parts.push(el("div", { class: "tdesc" }, [el("span", { text: inp.description })].concat(chips)));
    else if (chips.length) parts.push(el("div", { class: "tdesc" }, chips));
    var term = el("div", { class: "terminal" });
    term.appendChild(codeBlock(inp.command || "", { lang: "bash", lineNumbers: false, term: true, cmd: true }));
    if (d.result === undefined) term.appendChild(el("div", { class: "tpending term-pending", text: "running…" }));
    else {
      var r = splitResult(d.result);
      if (r.exit !== null || d.isError) term.appendChild(el("div", { class: "texit" + (r.exit === 0 ? "" : " bad"), text: r.exit !== null ? "exit code " + r.exit : "error" }));
      if (r.text) term.appendChild(codeBlock(r.text, { lang: "log", lineNumbers: false, term: true, maxLines: 400 }));
      else term.appendChild(el("div", { class: "tpending term-pending", text: "(no output)" }));
      parts.push(term);
      resultNotes(parts, r.notes);
      return parts;
    }
    parts.push(term);
    return parts;
  }

  function viewRead(inp, d) {
    var chips = [];
    if (inp.offset || inp.limit) chips.push(chip("lines " + (inp.offset || 1) + (inp.limit ? "–" + ((inp.offset || 1) + inp.limit - 1) : "+")));
    if (inp.pages) chips.push(chip("pages " + inp.pages));
    var parts = [fileHeader(inp.file_path || inp.notebook_path || inp.path, chips)];
    if (d.result === undefined) { pending(parts); return parts; }
    if (d.isError) { parts.push(errorBox(d.result)); return parts; }
    var r = splitResult(d.result);
    var nums = [], code = [], tail = [], numbered = 0;
    r.text.split("\n").forEach(function (line) {
      var m = /^\s*(\d+)(?:(?:\t|→)(.*))?$/.exec(line);
      if (m && (m[2] !== undefined || numbered) && !tail.length) { nums.push(Number(m[1])); code.push(m[2] || ""); numbered++; }
      else if (numbered) tail.push(line);
      else { nums.push(null); code.push(line); }
    });
    var lang = H.langFor(inp.file_path) || H.sniff(code.join("\n"));
    if (numbered) parts.push(codeBlock(code.join("\n"), { lang: lang, numbers: nums }));
    else parts.push(codeBlock(r.text, { lang: lang || "log", lineNumbers: false }));
    if (tail.join("").trim()) parts.push(el("div", { class: "prose sub", text: tail.join("\n").trim() }));
    resultNotes(parts, r.notes);
    return parts;
  }

  function viewWrite(inp, d) {
    var content = String(inp.content || "");
    var count = content ? content.split("\n").length - (/\n$/.test(content) ? 1 : 0) : 0;
    var parts = [fileHeader(inp.file_path, [chip(count + " lines"), d.result !== undefined && !d.isError ? chip(/created/i.test(d.result) ? "new file" : "written", "ok") : null])];
    parts.push(codeBlock(content, { lang: H.langFor(inp.file_path) || H.sniff(content) }));
    if (d.result === undefined) pending(parts);
    else if (d.isError) parts.push(errorBox(d.result));
    return parts;
  }

  function viewEdit(inp, d) {
    var lang = H.langFor(inp.file_path);
    var edits = Array.isArray(inp.edits) ? inp.edits : [inp];
    var parts = [], chips = [];
    if (inp.replace_all) chips.push(chip("replace all"));
    var diffs = edits.map(function (e) { return diffBlock(e.old_string || "", e.new_string || "", lang); });
    if (diffs.length === 1) chips.push(diffs[0].stats);
    else chips.push(chip(diffs.length + " edits"));
    if (d.result !== undefined) chips.push(d.isError ? chip("failed", "bad") : chip("applied", "ok"));
    parts.push(fileHeader(inp.file_path, chips));
    diffs.forEach(function (df, i) {
      if (diffs.length > 1) parts.push(el("div", { class: "tlabel" }, ["Edit " + (i + 1) + "  ", df.stats, edits[i].replace_all ? "  · replace all" : ""]));
      parts.push(df.el);
    });
    if (d.result === undefined) pending(parts);
    else if (d.isError) parts.push(errorBox(d.result));
    return parts;
  }

  function viewSearch(inp, d, name) {
    var parts = [];
    var form = el("div", { class: "tform" });
    var pattern = inp.pattern || inp.query || "";
    form.appendChild(el("div", { class: "trow" }, [el("span", { class: "tk", text: name === "Glob" ? "files" : "pattern" }), el("code", { class: "tpat", text: pattern })]));
    var chips = [];
    ["path", "glob", "type", "output_mode", "-i", "-n", "multiline", "head_limit", "-A", "-B", "-C"].forEach(function (k) {
      if (inp[k] !== undefined && inp[k] !== false && k !== "path") chips.push(chip(k.replace(/^-/, "") + (inp[k] === true ? "" : " " + inp[k])));
    });
    if (inp.path) form.appendChild(el("div", { class: "trow" }, [el("span", { class: "tk", text: "in" }), el("span", { class: "mono", text: inp.path })]));
    if (chips.length) form.appendChild(el("div", { class: "trow" }, [el("span", { class: "tk", text: "options" })].concat(chips)));
    parts.push(form);
    if (d.result === undefined) { pending(parts); return parts; }
    if (d.isError) { parts.push(errorBox(d.result)); return parts; }
    var r = splitResult(d.result);
    parts.push(label("Found"));
    parts.push(codeBlock(r.text || "(nothing)", { lang: "grep", lineNumbers: false, maxLines: 200 }));
    resultNotes(parts, r.notes);
    return parts;
  }

  function viewWeb(inp, d, name) {
    var parts = [];
    var form = el("div", { class: "tform" });
    if (inp.url) form.appendChild(el("div", { class: "trow" }, [el("span", { class: "tk", text: "url" }), el("span", { class: "mono", text: inp.url })]));
    if (inp.query) form.appendChild(el("div", { class: "trow" }, [el("span", { class: "tk", text: "query" }), el("span", { text: inp.query })]));
    if (inp.mode) form.appendChild(el("div", { class: "trow" }, [el("span", { class: "tk", text: "mode" }), chip(inp.mode)]));
    parts.push(form);
    if (inp.prompt) { parts.push(label("Asked")); parts.push(proseBlock(inp.prompt)); }
    if (d.result === undefined) { pending(parts); return parts; }
    if (d.isError) { parts.push(errorBox(d.result)); return parts; }
    var r = splitResult(d.result);
    parts.push(label(name === "WebSearch" ? "Results" : "Answer"));
    parts.push(codeBlock(r.text, { lang: "markdown", lineNumbers: false, maxLines: 200, rowClass: function () { return "wrapline"; } }));
    resultNotes(parts, r.notes);
    return parts;
  }

  function viewTodos(inp) {
    var list = el("ul", { class: "todos" });
    (inp.todos || []).forEach(function (t) {
      var st = t.status || "pending";
      list.appendChild(el("li", { class: "todo-" + st }, [el("span", { class: "tbox", text: st === "completed" ? "☑" : st === "in_progress" ? "◐" : "☐" }),
        el("span", { text: t.content || t.activeForm || "" })]));
    });
    return [list];
  }

  function viewAgent(inp, d) {
    var parts = [];
    var form = el("div", { class: "tform" });
    if (inp.description) form.appendChild(el("div", { class: "trow" }, [el("span", { class: "tk", text: "task" }), el("b", { text: inp.description })]));
    if (inp.subagent_type) form.appendChild(el("div", { class: "trow" }, [el("span", { class: "tk", text: "type" }), chip(inp.subagent_type)]));
    parts.push(form);
    if (inp.prompt) { parts.push(label("Prompt")); parts.push(codeBlock(inp.prompt, { lang: "markdown", lineNumbers: false, maxLines: 120, rowClass: function () { return "wrapline"; } })); }
    genericResult(parts, d);
    return parts;
  }

  function viewStructured(inp, d) {
    if (typeof inp.__unparsedToolInput === "string") {
      var raw = inp.__unparsedToolInput, value = null;
      try { value = JSON.parse(raw); } catch (e) { value = null; }
      var p = [label("The agent tried to return this, but it could not be read as the asked shape")];
      p.push(value !== null && typeof value === "object" ? el("div", { class: "box" }, [renderValue(value)]) : codeBlock(raw, { lang: "json", lineNumbers: false, rowClass: function () { return "wrapline"; } }));
      if (d.result !== undefined) { p.push(label(d.isError ? "Why it was refused" : "Result")); p.push(d.isError ? errorBox(d.result) : proseBlock(d.result)); }
      return p;
    }
    var parts = [label("The value the agent returned"), el("div", { class: "box" }, [renderValue(inp)])];
    if (d.isError) parts.push(errorBox(d.result));
    return parts;
  }

  function looksLikeCode(text) { return /\n/.test(text) && /[{}();=<>]|^\s{2,}\S/m.test(text); }

  function genericForm(inp) {
    var form = el("div", { class: "tform" });
    if (!inp || typeof inp !== "object") { form.appendChild(proseBlock(String(inp == null ? "" : inp))); return form; }
    Object.keys(inp).forEach(function (k) {
      var v = inp[k];
      if (v === undefined || k === "__unparsedToolInput") return;
      var value;
      if (typeof v === "string") {
        if (v.length < 140 && v.indexOf("\n") < 0) value = el("span", { class: /path|file|url|command|pattern/.test(k) ? "mono" : "", text: v });
        else value = looksLikeCode(v) ? codeBlock(v, { lang: H.sniff(v), lineNumbers: false, maxLines: 150 }) : proseBlock(v);
      } else if (typeof v === "number" || typeof v === "boolean" || v === null) value = chip(String(v));
      else value = el("div", { class: "box" }, [renderValue(v)]);
      form.appendChild(el("div", { class: "trow" + (typeof v === "string" && v.length >= 140 || (v && typeof v === "object") ? " tcol" : "") },
        [el("span", { class: "tk", text: k }), value]));
    });
    return form;
  }

  function genericResult(parts, d) {
    if (d.result === undefined) { pending(parts); return; }
    if (d.isError) { parts.push(label("Error")); parts.push(errorBox(d.result)); return; }
    var r = splitResult(d.result);
    if (!r.text) { parts.push(el("div", { class: "sub", text: "(empty result)" })); return; }
    parts.push(label("Result"));
    var parsed = null;
    if (/^[\[{]/.test(r.text)) { try { parsed = JSON.parse(r.text); } catch (e) { parsed = null; } }
    if (parsed !== null && typeof parsed === "object") parts.push(el("div", { class: "box" }, [renderValue(parsed)]));
    else parts.push(codeBlock(r.text, { lang: H.sniff(r.text) || "log", lineNumbers: false, maxLines: 200, rowClass: function () { return "wrapline"; } }));
    resultNotes(parts, r.notes);
  }

  function viewGeneric(inp, d) {
    var parts = [genericForm(inp)];
    genericResult(parts, d);
    return parts;
  }

  function viewTool(d) {
    var inp = d.input && typeof d.input === "object" ? d.input : {};
    var name = d.name || "";
    var parts;
    try {
      if (name === "Bash" || name === "Monitor" || name === "BashOutput") parts = inp.command !== undefined ? viewBash(inp, d) : viewGeneric(inp, d);
      else if (name === "Read" || name === "NotebookRead") parts = viewRead(inp, d);
      else if (name === "Write") parts = viewWrite(inp, d);
      else if (name === "Edit" || name === "MultiEdit") parts = viewEdit(inp, d);
      else if (name === "Grep" || name === "Glob" || name === "LS") parts = viewSearch(inp, d, name);
      else if (name === "WebFetch" || name === "WebSearch") parts = viewWeb(inp, d, name);
      else if (name === "TodoWrite") parts = viewTodos(inp);
      else if (name === "Agent" || name === "Task") parts = viewAgent(inp, d);
      else if (name === "StructuredOutput") parts = viewStructured(inp, d);
      else parts = viewGeneric(inp, d);
    } catch (e) {
      parts = [el("p", { class: "note", text: "This call could not be shown in its own form (" + e.message + ")." }), genericForm(inp)];
      genericResult(parts, d);
    }
    if (d.time) {
      var timing = "called " + fmtClock(d.time);
      if (d.end) timing += " · took " + fmtDur(d.end - d.time);
      parts.push(el("div", { class: "ttime", text: timing }));
    }
    (d.notes || []).forEach(function (n) { parts.push(el("p", { class: "note", text: n })); });
    return parts;
  }

  /* The open body of one step: fetched in full from the server. */
  var stepCache = {};
  function stepDetail(s) {
    var box = el("div", { class: "sdetail" });
    var key = drawer.run + "|" + drawer.agentId + "|" + s.i;
    var cached = stepCache[key];
    var fresh = cached && (cached.kind !== "tool" || cached.result !== undefined || s.result === undefined);
    function fill(d) {
      clear(box);
      var parts;
      if (d.kind === "tool") parts = viewTool(d);
      else if (d.kind === "text" || d.kind === "thinking" || d.kind === "user" || d.kind === "system")
        parts = [el("div", { class: "prose" + (d.kind === "thinking" ? " sub" : "") }, [codeBlockOrProse(d.text)])];
      else parts = [codeBlock(String(d.text || ""), { lang: "log", lineNumbers: false })];
      parts.forEach(function (p) { box.appendChild(p); });
    }
    if (fresh) { fill(cached); return box; }
    box.appendChild(el("div", { class: "tpending", text: "Loading…" }));
    var runId = drawer.run, agentId = drawer.agentId;
    getJSON("/api/step?run=" + encodeURIComponent(runId) + "&agent=" + encodeURIComponent(agentId) + "&i=" + s.i)
      .then(function (d) { stepCache[key] = d; fill(d); })
      .catch(function (e) {
        clear(box);
        // fall back to what the step list holds
        fill({ kind: s.kind, name: s.name, text: s.text, input: null, result: s.result, isError: s.isError,
               notes: ["The full step could not be loaded (" + e.message + ")."] });
      });
    return box;
  }

  /* An assistant message: prose, with fenced code blocks shown as code. */
  function codeBlockOrProse(text) {
    text = String(text || "");
    var wrap = el("div");
    var re = /```([\w+-]*)\n([\s\S]*?)(?:```|$)/g, pos = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > pos) wrap.appendChild(el("div", { class: "prose", text: text.slice(pos, m.index).replace(/\n+$/, "") }));
      var lang = m[1] ? (H.langFor("x." + m[1]) || (H.languages.indexOf(m[1]) >= 0 ? m[1] : null)) : H.sniff(m[2]);
      wrap.appendChild(codeBlock(m[2].replace(/\n$/, ""), { lang: lang, lineNumbers: false }));
      pos = re.lastIndex;
    }
    if (pos < text.length) wrap.appendChild(el("div", { class: "prose", text: text.slice(pos) }));
    return wrap;
  }

  // first route
  route();
})();
"""

# acorn 8.14.0, https://github.com/acornjs/acorn, embedded unchanged by
# tools/vendor_acorn.py. Its licence:
#
#   MIT License
#
#   Copyright (C) 2012-2022 by various contributors (see AUTHORS)
#
#   Permission is hereby granted, free of charge, to any person obtaining a copy
#   of this software and associated documentation files (the "Software"), to deal
#   in the Software without restriction, including without limitation the rights
#   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#   copies of the Software, and to permit persons to whom the Software is
#   furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
#   THE SOFTWARE.
ACORN_JS = r"""(function (global, factory) {
  typeof exports === 'object' && typeof module !== 'undefined' ? factory(exports) :
  typeof define === 'function' && define.amd ? define(['exports'], factory) :
  (global = typeof globalThis !== 'undefined' ? globalThis : global || self, factory(global.acorn = {}));
})(this, (function (exports) { 'use strict';

  // This file was generated. Do not modify manually!
  var astralIdentifierCodes = [509, 0, 227, 0, 150, 4, 294, 9, 1368, 2, 2, 1, 6, 3, 41, 2, 5, 0, 166, 1, 574, 3, 9, 9, 7, 9, 32, 4, 318, 1, 80, 3, 71, 10, 50, 3, 123, 2, 54, 14, 32, 10, 3, 1, 11, 3, 46, 10, 8, 0, 46, 9, 7, 2, 37, 13, 2, 9, 6, 1, 45, 0, 13, 2, 49, 13, 9, 3, 2, 11, 83, 11, 7, 0, 3, 0, 158, 11, 6, 9, 7, 3, 56, 1, 2, 6, 3, 1, 3, 2, 10, 0, 11, 1, 3, 6, 4, 4, 68, 8, 2, 0, 3, 0, 2, 3, 2, 4, 2, 0, 15, 1, 83, 17, 10, 9, 5, 0, 82, 19, 13, 9, 214, 6, 3, 8, 28, 1, 83, 16, 16, 9, 82, 12, 9, 9, 7, 19, 58, 14, 5, 9, 243, 14, 166, 9, 71, 5, 2, 1, 3, 3, 2, 0, 2, 1, 13, 9, 120, 6, 3, 6, 4, 0, 29, 9, 41, 6, 2, 3, 9, 0, 10, 10, 47, 15, 343, 9, 54, 7, 2, 7, 17, 9, 57, 21, 2, 13, 123, 5, 4, 0, 2, 1, 2, 6, 2, 0, 9, 9, 49, 4, 2, 1, 2, 4, 9, 9, 330, 3, 10, 1, 2, 0, 49, 6, 4, 4, 14, 10, 5350, 0, 7, 14, 11465, 27, 2343, 9, 87, 9, 39, 4, 60, 6, 26, 9, 535, 9, 470, 0, 2, 54, 8, 3, 82, 0, 12, 1, 19628, 1, 4178, 9, 519, 45, 3, 22, 543, 4, 4, 5, 9, 7, 3, 6, 31, 3, 149, 2, 1418, 49, 513, 54, 5, 49, 9, 0, 15, 0, 23, 4, 2, 14, 1361, 6, 2, 16, 3, 6, 2, 1, 2, 4, 101, 0, 161, 6, 10, 9, 357, 0, 62, 13, 499, 13, 245, 1, 2, 9, 726, 6, 110, 6, 6, 9, 4759, 9, 787719, 239];

  // This file was generated. Do not modify manually!
  var astralIdentifierStartCodes = [0, 11, 2, 25, 2, 18, 2, 1, 2, 14, 3, 13, 35, 122, 70, 52, 268, 28, 4, 48, 48, 31, 14, 29, 6, 37, 11, 29, 3, 35, 5, 7, 2, 4, 43, 157, 19, 35, 5, 35, 5, 39, 9, 51, 13, 10, 2, 14, 2, 6, 2, 1, 2, 10, 2, 14, 2, 6, 2, 1, 4, 51, 13, 310, 10, 21, 11, 7, 25, 5, 2, 41, 2, 8, 70, 5, 3, 0, 2, 43, 2, 1, 4, 0, 3, 22, 11, 22, 10, 30, 66, 18, 2, 1, 11, 21, 11, 25, 71, 55, 7, 1, 65, 0, 16, 3, 2, 2, 2, 28, 43, 28, 4, 28, 36, 7, 2, 27, 28, 53, 11, 21, 11, 18, 14, 17, 111, 72, 56, 50, 14, 50, 14, 35, 39, 27, 10, 22, 251, 41, 7, 1, 17, 2, 60, 28, 11, 0, 9, 21, 43, 17, 47, 20, 28, 22, 13, 52, 58, 1, 3, 0, 14, 44, 33, 24, 27, 35, 30, 0, 3, 0, 9, 34, 4, 0, 13, 47, 15, 3, 22, 0, 2, 0, 36, 17, 2, 24, 20, 1, 64, 6, 2, 0, 2, 3, 2, 14, 2, 9, 8, 46, 39, 7, 3, 1, 3, 21, 2, 6, 2, 1, 2, 4, 4, 0, 19, 0, 13, 4, 31, 9, 2, 0, 3, 0, 2, 37, 2, 0, 26, 0, 2, 0, 45, 52, 19, 3, 21, 2, 31, 47, 21, 1, 2, 0, 185, 46, 42, 3, 37, 47, 21, 0, 60, 42, 14, 0, 72, 26, 38, 6, 186, 43, 117, 63, 32, 7, 3, 0, 3, 7, 2, 1, 2, 23, 16, 0, 2, 0, 95, 7, 3, 38, 17, 0, 2, 0, 29, 0, 11, 39, 8, 0, 22, 0, 12, 45, 20, 0, 19, 72, 200, 32, 32, 8, 2, 36, 18, 0, 50, 29, 113, 6, 2, 1, 2, 37, 22, 0, 26, 5, 2, 1, 2, 31, 15, 0, 328, 18, 16, 0, 2, 12, 2, 33, 125, 0, 80, 921, 103, 110, 18, 195, 2637, 96, 16, 1071, 18, 5, 26, 3994, 6, 582, 6842, 29, 1763, 568, 8, 30, 18, 78, 18, 29, 19, 47, 17, 3, 32, 20, 6, 18, 433, 44, 212, 63, 129, 74, 6, 0, 67, 12, 65, 1, 2, 0, 29, 6135, 9, 1237, 42, 9, 8936, 3, 2, 6, 2, 1, 2, 290, 16, 0, 30, 2, 3, 0, 15, 3, 9, 395, 2309, 106, 6, 12, 4, 8, 8, 9, 5991, 84, 2, 70, 2, 1, 3, 0, 3, 1, 3, 3, 2, 11, 2, 0, 2, 6, 2, 64, 2, 3, 3, 7, 2, 6, 2, 27, 2, 3, 2, 4, 2, 0, 4, 6, 2, 339, 3, 24, 2, 24, 2, 30, 2, 24, 2, 30, 2, 24, 2, 30, 2, 24, 2, 30, 2, 24, 2, 7, 1845, 30, 7, 5, 262, 61, 147, 44, 11, 6, 17, 0, 322, 29, 19, 43, 485, 27, 229, 29, 3, 0, 496, 6, 2, 3, 2, 1, 2, 14, 2, 196, 60, 67, 8, 0, 1205, 3, 2, 26, 2, 1, 2, 0, 3, 0, 2, 9, 2, 3, 2, 0, 2, 0, 7, 0, 5, 0, 2, 0, 2, 0, 2, 2, 2, 1, 2, 0, 3, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2, 1, 2, 0, 3, 3, 2, 6, 2, 3, 2, 3, 2, 0, 2, 9, 2, 16, 6, 2, 2, 4, 2, 16, 4421, 42719, 33, 4153, 7, 221, 3, 5761, 15, 7472, 16, 621, 2467, 541, 1507, 4938, 6, 4191];

  // This file was generated. Do not modify manually!
  var nonASCIIidentifierChars = "\u200c\u200d\xb7\u0300-\u036f\u0387\u0483-\u0487\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7\u0610-\u061a\u064b-\u0669\u0670\u06d6-\u06dc\u06df-\u06e4\u06e7\u06e8\u06ea-\u06ed\u06f0-\u06f9\u0711\u0730-\u074a\u07a6-\u07b0\u07c0-\u07c9\u07eb-\u07f3\u07fd\u0816-\u0819\u081b-\u0823\u0825-\u0827\u0829-\u082d\u0859-\u085b\u0897-\u089f\u08ca-\u08e1\u08e3-\u0903\u093a-\u093c\u093e-\u094f\u0951-\u0957\u0962\u0963\u0966-\u096f\u0981-\u0983\u09bc\u09be-\u09c4\u09c7\u09c8\u09cb-\u09cd\u09d7\u09e2\u09e3\u09e6-\u09ef\u09fe\u0a01-\u0a03\u0a3c\u0a3e-\u0a42\u0a47\u0a48\u0a4b-\u0a4d\u0a51\u0a66-\u0a71\u0a75\u0a81-\u0a83\u0abc\u0abe-\u0ac5\u0ac7-\u0ac9\u0acb-\u0acd\u0ae2\u0ae3\u0ae6-\u0aef\u0afa-\u0aff\u0b01-\u0b03\u0b3c\u0b3e-\u0b44\u0b47\u0b48\u0b4b-\u0b4d\u0b55-\u0b57\u0b62\u0b63\u0b66-\u0b6f\u0b82\u0bbe-\u0bc2\u0bc6-\u0bc8\u0bca-\u0bcd\u0bd7\u0be6-\u0bef\u0c00-\u0c04\u0c3c\u0c3e-\u0c44\u0c46-\u0c48\u0c4a-\u0c4d\u0c55\u0c56\u0c62\u0c63\u0c66-\u0c6f\u0c81-\u0c83\u0cbc\u0cbe-\u0cc4\u0cc6-\u0cc8\u0cca-\u0ccd\u0cd5\u0cd6\u0ce2\u0ce3\u0ce6-\u0cef\u0cf3\u0d00-\u0d03\u0d3b\u0d3c\u0d3e-\u0d44\u0d46-\u0d48\u0d4a-\u0d4d\u0d57\u0d62\u0d63\u0d66-\u0d6f\u0d81-\u0d83\u0dca\u0dcf-\u0dd4\u0dd6\u0dd8-\u0ddf\u0de6-\u0def\u0df2\u0df3\u0e31\u0e34-\u0e3a\u0e47-\u0e4e\u0e50-\u0e59\u0eb1\u0eb4-\u0ebc\u0ec8-\u0ece\u0ed0-\u0ed9\u0f18\u0f19\u0f20-\u0f29\u0f35\u0f37\u0f39\u0f3e\u0f3f\u0f71-\u0f84\u0f86\u0f87\u0f8d-\u0f97\u0f99-\u0fbc\u0fc6\u102b-\u103e\u1040-\u1049\u1056-\u1059\u105e-\u1060\u1062-\u1064\u1067-\u106d\u1071-\u1074\u1082-\u108d\u108f-\u109d\u135d-\u135f\u1369-\u1371\u1712-\u1715\u1732-\u1734\u1752\u1753\u1772\u1773\u17b4-\u17d3\u17dd\u17e0-\u17e9\u180b-\u180d\u180f-\u1819\u18a9\u1920-\u192b\u1930-\u193b\u1946-\u194f\u19d0-\u19da\u1a17-\u1a1b\u1a55-\u1a5e\u1a60-\u1a7c\u1a7f-\u1a89\u1a90-\u1a99\u1ab0-\u1abd\u1abf-\u1ace\u1b00-\u1b04\u1b34-\u1b44\u1b50-\u1b59\u1b6b-\u1b73\u1b80-\u1b82\u1ba1-\u1bad\u1bb0-\u1bb9\u1be6-\u1bf3\u1c24-\u1c37\u1c40-\u1c49\u1c50-\u1c59\u1cd0-\u1cd2\u1cd4-\u1ce8\u1ced\u1cf4\u1cf7-\u1cf9\u1dc0-\u1dff\u200c\u200d\u203f\u2040\u2054\u20d0-\u20dc\u20e1\u20e5-\u20f0\u2cef-\u2cf1\u2d7f\u2de0-\u2dff\u302a-\u302f\u3099\u309a\u30fb\ua620-\ua629\ua66f\ua674-\ua67d\ua69e\ua69f\ua6f0\ua6f1\ua802\ua806\ua80b\ua823-\ua827\ua82c\ua880\ua881\ua8b4-\ua8c5\ua8d0-\ua8d9\ua8e0-\ua8f1\ua8ff-\ua909\ua926-\ua92d\ua947-\ua953\ua980-\ua983\ua9b3-\ua9c0\ua9d0-\ua9d9\ua9e5\ua9f0-\ua9f9\uaa29-\uaa36\uaa43\uaa4c\uaa4d\uaa50-\uaa59\uaa7b-\uaa7d\uaab0\uaab2-\uaab4\uaab7\uaab8\uaabe\uaabf\uaac1\uaaeb-\uaaef\uaaf5\uaaf6\uabe3-\uabea\uabec\uabed\uabf0-\uabf9\ufb1e\ufe00-\ufe0f\ufe20-\ufe2f\ufe33\ufe34\ufe4d-\ufe4f\uff10-\uff19\uff3f\uff65";

  // This file was generated. Do not modify manually!
  var nonASCIIidentifierStartChars = "\xaa\xb5\xba\xc0-\xd6\xd8-\xf6\xf8-\u02c1\u02c6-\u02d1\u02e0-\u02e4\u02ec\u02ee\u0370-\u0374\u0376\u0377\u037a-\u037d\u037f\u0386\u0388-\u038a\u038c\u038e-\u03a1\u03a3-\u03f5\u03f7-\u0481\u048a-\u052f\u0531-\u0556\u0559\u0560-\u0588\u05d0-\u05ea\u05ef-\u05f2\u0620-\u064a\u066e\u066f\u0671-\u06d3\u06d5\u06e5\u06e6\u06ee\u06ef\u06fa-\u06fc\u06ff\u0710\u0712-\u072f\u074d-\u07a5\u07b1\u07ca-\u07ea\u07f4\u07f5\u07fa\u0800-\u0815\u081a\u0824\u0828\u0840-\u0858\u0860-\u086a\u0870-\u0887\u0889-\u088e\u08a0-\u08c9\u0904-\u0939\u093d\u0950\u0958-\u0961\u0971-\u0980\u0985-\u098c\u098f\u0990\u0993-\u09a8\u09aa-\u09b0\u09b2\u09b6-\u09b9\u09bd\u09ce\u09dc\u09dd\u09df-\u09e1\u09f0\u09f1\u09fc\u0a05-\u0a0a\u0a0f\u0a10\u0a13-\u0a28\u0a2a-\u0a30\u0a32\u0a33\u0a35\u0a36\u0a38\u0a39\u0a59-\u0a5c\u0a5e\u0a72-\u0a74\u0a85-\u0a8d\u0a8f-\u0a91\u0a93-\u0aa8\u0aaa-\u0ab0\u0ab2\u0ab3\u0ab5-\u0ab9\u0abd\u0ad0\u0ae0\u0ae1\u0af9\u0b05-\u0b0c\u0b0f\u0b10\u0b13-\u0b28\u0b2a-\u0b30\u0b32\u0b33\u0b35-\u0b39\u0b3d\u0b5c\u0b5d\u0b5f-\u0b61\u0b71\u0b83\u0b85-\u0b8a\u0b8e-\u0b90\u0b92-\u0b95\u0b99\u0b9a\u0b9c\u0b9e\u0b9f\u0ba3\u0ba4\u0ba8-\u0baa\u0bae-\u0bb9\u0bd0\u0c05-\u0c0c\u0c0e-\u0c10\u0c12-\u0c28\u0c2a-\u0c39\u0c3d\u0c58-\u0c5a\u0c5d\u0c60\u0c61\u0c80\u0c85-\u0c8c\u0c8e-\u0c90\u0c92-\u0ca8\u0caa-\u0cb3\u0cb5-\u0cb9\u0cbd\u0cdd\u0cde\u0ce0\u0ce1\u0cf1\u0cf2\u0d04-\u0d0c\u0d0e-\u0d10\u0d12-\u0d3a\u0d3d\u0d4e\u0d54-\u0d56\u0d5f-\u0d61\u0d7a-\u0d7f\u0d85-\u0d96\u0d9a-\u0db1\u0db3-\u0dbb\u0dbd\u0dc0-\u0dc6\u0e01-\u0e30\u0e32\u0e33\u0e40-\u0e46\u0e81\u0e82\u0e84\u0e86-\u0e8a\u0e8c-\u0ea3\u0ea5\u0ea7-\u0eb0\u0eb2\u0eb3\u0ebd\u0ec0-\u0ec4\u0ec6\u0edc-\u0edf\u0f00\u0f40-\u0f47\u0f49-\u0f6c\u0f88-\u0f8c\u1000-\u102a\u103f\u1050-\u1055\u105a-\u105d\u1061\u1065\u1066\u106e-\u1070\u1075-\u1081\u108e\u10a0-\u10c5\u10c7\u10cd\u10d0-\u10fa\u10fc-\u1248\u124a-\u124d\u1250-\u1256\u1258\u125a-\u125d\u1260-\u1288\u128a-\u128d\u1290-\u12b0\u12b2-\u12b5\u12b8-\u12be\u12c0\u12c2-\u12c5\u12c8-\u12d6\u12d8-\u1310\u1312-\u1315\u1318-\u135a\u1380-\u138f\u13a0-\u13f5\u13f8-\u13fd\u1401-\u166c\u166f-\u167f\u1681-\u169a\u16a0-\u16ea\u16ee-\u16f8\u1700-\u1711\u171f-\u1731\u1740-\u1751\u1760-\u176c\u176e-\u1770\u1780-\u17b3\u17d7\u17dc\u1820-\u1878\u1880-\u18a8\u18aa\u18b0-\u18f5\u1900-\u191e\u1950-\u196d\u1970-\u1974\u1980-\u19ab\u19b0-\u19c9\u1a00-\u1a16\u1a20-\u1a54\u1aa7\u1b05-\u1b33\u1b45-\u1b4c\u1b83-\u1ba0\u1bae\u1baf\u1bba-\u1be5\u1c00-\u1c23\u1c4d-\u1c4f\u1c5a-\u1c7d\u1c80-\u1c8a\u1c90-\u1cba\u1cbd-\u1cbf\u1ce9-\u1cec\u1cee-\u1cf3\u1cf5\u1cf6\u1cfa\u1d00-\u1dbf\u1e00-\u1f15\u1f18-\u1f1d\u1f20-\u1f45\u1f48-\u1f4d\u1f50-\u1f57\u1f59\u1f5b\u1f5d\u1f5f-\u1f7d\u1f80-\u1fb4\u1fb6-\u1fbc\u1fbe\u1fc2-\u1fc4\u1fc6-\u1fcc\u1fd0-\u1fd3\u1fd6-\u1fdb\u1fe0-\u1fec\u1ff2-\u1ff4\u1ff6-\u1ffc\u2071\u207f\u2090-\u209c\u2102\u2107\u210a-\u2113\u2115\u2118-\u211d\u2124\u2126\u2128\u212a-\u2139\u213c-\u213f\u2145-\u2149\u214e\u2160-\u2188\u2c00-\u2ce4\u2ceb-\u2cee\u2cf2\u2cf3\u2d00-\u2d25\u2d27\u2d2d\u2d30-\u2d67\u2d6f\u2d80-\u2d96\u2da0-\u2da6\u2da8-\u2dae\u2db0-\u2db6\u2db8-\u2dbe\u2dc0-\u2dc6\u2dc8-\u2dce\u2dd0-\u2dd6\u2dd8-\u2dde\u3005-\u3007\u3021-\u3029\u3031-\u3035\u3038-\u303c\u3041-\u3096\u309b-\u309f\u30a1-\u30fa\u30fc-\u30ff\u3105-\u312f\u3131-\u318e\u31a0-\u31bf\u31f0-\u31ff\u3400-\u4dbf\u4e00-\ua48c\ua4d0-\ua4fd\ua500-\ua60c\ua610-\ua61f\ua62a\ua62b\ua640-\ua66e\ua67f-\ua69d\ua6a0-\ua6ef\ua717-\ua71f\ua722-\ua788\ua78b-\ua7cd\ua7d0\ua7d1\ua7d3\ua7d5-\ua7dc\ua7f2-\ua801\ua803-\ua805\ua807-\ua80a\ua80c-\ua822\ua840-\ua873\ua882-\ua8b3\ua8f2-\ua8f7\ua8fb\ua8fd\ua8fe\ua90a-\ua925\ua930-\ua946\ua960-\ua97c\ua984-\ua9b2\ua9cf\ua9e0-\ua9e4\ua9e6-\ua9ef\ua9fa-\ua9fe\uaa00-\uaa28\uaa40-\uaa42\uaa44-\uaa4b\uaa60-\uaa76\uaa7a\uaa7e-\uaaaf\uaab1\uaab5\uaab6\uaab9-\uaabd\uaac0\uaac2\uaadb-\uaadd\uaae0-\uaaea\uaaf2-\uaaf4\uab01-\uab06\uab09-\uab0e\uab11-\uab16\uab20-\uab26\uab28-\uab2e\uab30-\uab5a\uab5c-\uab69\uab70-\uabe2\uac00-\ud7a3\ud7b0-\ud7c6\ud7cb-\ud7fb\uf900-\ufa6d\ufa70-\ufad9\ufb00-\ufb06\ufb13-\ufb17\ufb1d\ufb1f-\ufb28\ufb2a-\ufb36\ufb38-\ufb3c\ufb3e\ufb40\ufb41\ufb43\ufb44\ufb46-\ufbb1\ufbd3-\ufd3d\ufd50-\ufd8f\ufd92-\ufdc7\ufdf0-\ufdfb\ufe70-\ufe74\ufe76-\ufefc\uff21-\uff3a\uff41-\uff5a\uff66-\uffbe\uffc2-\uffc7\uffca-\uffcf\uffd2-\uffd7\uffda-\uffdc";

  // These are a run-length and offset encoded representation of the
  // >0xffff code points that are a valid part of identifiers. The
  // offset starts at 0x10000, and each pair of numbers represents an
  // offset to the next range, and then a size of the range.

  // Reserved word lists for various dialects of the language

  var reservedWords = {
    3: "abstract boolean byte char class double enum export extends final float goto implements import int interface long native package private protected public short static super synchronized throws transient volatile",
    5: "class enum extends super const export import",
    6: "enum",
    strict: "implements interface let package private protected public static yield",
    strictBind: "eval arguments"
  };

  // And the keywords

  var ecma5AndLessKeywords = "break case catch continue debugger default do else finally for function if return switch throw try var while with null true false instanceof typeof void delete new in this";

  var keywords$1 = {
    5: ecma5AndLessKeywords,
    "5module": ecma5AndLessKeywords + " export import",
    6: ecma5AndLessKeywords + " const class extends export import super"
  };

  var keywordRelationalOperator = /^in(stanceof)?$/;

  // ## Character categories

  var nonASCIIidentifierStart = new RegExp("[" + nonASCIIidentifierStartChars + "]");
  var nonASCIIidentifier = new RegExp("[" + nonASCIIidentifierStartChars + nonASCIIidentifierChars + "]");

  // This has a complexity linear to the value of the code. The
  // assumption is that looking up astral identifier characters is
  // rare.
  function isInAstralSet(code, set) {
    var pos = 0x10000;
    for (var i = 0; i < set.length; i += 2) {
      pos += set[i];
      if (pos > code) { return false }
      pos += set[i + 1];
      if (pos >= code) { return true }
    }
    return false
  }

  // Test whether a given character code starts an identifier.

  function isIdentifierStart(code, astral) {
    if (code < 65) { return code === 36 }
    if (code < 91) { return true }
    if (code < 97) { return code === 95 }
    if (code < 123) { return true }
    if (code <= 0xffff) { return code >= 0xaa && nonASCIIidentifierStart.test(String.fromCharCode(code)) }
    if (astral === false) { return false }
    return isInAstralSet(code, astralIdentifierStartCodes)
  }

  // Test whether a given character is part of an identifier.

  function isIdentifierChar(code, astral) {
    if (code < 48) { return code === 36 }
    if (code < 58) { return true }
    if (code < 65) { return false }
    if (code < 91) { return true }
    if (code < 97) { return code === 95 }
    if (code < 123) { return true }
    if (code <= 0xffff) { return code >= 0xaa && nonASCIIidentifier.test(String.fromCharCode(code)) }
    if (astral === false) { return false }
    return isInAstralSet(code, astralIdentifierStartCodes) || isInAstralSet(code, astralIdentifierCodes)
  }

  // ## Token types

  // The assignment of fine-grained, information-carrying type objects
  // allows the tokenizer to store the information it has about a
  // token in a way that is very cheap for the parser to look up.

  // All token type variables start with an underscore, to make them
  // easy to recognize.

  // The `beforeExpr` property is used to disambiguate between regular
  // expressions and divisions. It is set on all token types that can
  // be followed by an expression (thus, a slash after them would be a
  // regular expression).
  //
  // The `startsExpr` property is used to check if the token ends a
  // `yield` expression. It is set on all token types that either can
  // directly start an expression (like a quotation mark) or can
  // continue an expression (like the body of a string).
  //
  // `isLoop` marks a keyword as starting a loop, which is important
  // to know when parsing a label, in order to allow or disallow
  // continue jumps to that label.

  var TokenType = function TokenType(label, conf) {
    if ( conf === void 0 ) conf = {};

    this.label = label;
    this.keyword = conf.keyword;
    this.beforeExpr = !!conf.beforeExpr;
    this.startsExpr = !!conf.startsExpr;
    this.isLoop = !!conf.isLoop;
    this.isAssign = !!conf.isAssign;
    this.prefix = !!conf.prefix;
    this.postfix = !!conf.postfix;
    this.binop = conf.binop || null;
    this.updateContext = null;
  };

  function binop(name, prec) {
    return new TokenType(name, {beforeExpr: true, binop: prec})
  }
  var beforeExpr = {beforeExpr: true}, startsExpr = {startsExpr: true};

  // Map keyword names to token types.

  var keywords = {};

  // Succinct definitions of keyword token types
  function kw(name, options) {
    if ( options === void 0 ) options = {};

    options.keyword = name;
    return keywords[name] = new TokenType(name, options)
  }

  var types$1 = {
    num: new TokenType("num", startsExpr),
    regexp: new TokenType("regexp", startsExpr),
    string: new TokenType("string", startsExpr),
    name: new TokenType("name", startsExpr),
    privateId: new TokenType("privateId", startsExpr),
    eof: new TokenType("eof"),

    // Punctuation token types.
    bracketL: new TokenType("[", {beforeExpr: true, startsExpr: true}),
    bracketR: new TokenType("]"),
    braceL: new TokenType("{", {beforeExpr: true, startsExpr: true}),
    braceR: new TokenType("}"),
    parenL: new TokenType("(", {beforeExpr: true, startsExpr: true}),
    parenR: new TokenType(")"),
    comma: new TokenType(",", beforeExpr),
    semi: new TokenType(";", beforeExpr),
    colon: new TokenType(":", beforeExpr),
    dot: new TokenType("."),
    question: new TokenType("?", beforeExpr),
    questionDot: new TokenType("?."),
    arrow: new TokenType("=>", beforeExpr),
    template: new TokenType("template"),
    invalidTemplate: new TokenType("invalidTemplate"),
    ellipsis: new TokenType("...", beforeExpr),
    backQuote: new TokenType("`", startsExpr),
    dollarBraceL: new TokenType("${", {beforeExpr: true, startsExpr: true}),

    // Operators. These carry several kinds of properties to help the
    // parser use them properly (the presence of these properties is
    // what categorizes them as operators).
    //
    // `binop`, when present, specifies that this operator is a binary
    // operator, and will refer to its precedence.
    //
    // `prefix` and `postfix` mark the operator as a prefix or postfix
    // unary operator.
    //
    // `isAssign` marks all of `=`, `+=`, `-=` etcetera, which act as
    // binary operators with a very low precedence, that should result
    // in AssignmentExpression nodes.

    eq: new TokenType("=", {beforeExpr: true, isAssign: true}),
    assign: new TokenType("_=", {beforeExpr: true, isAssign: true}),
    incDec: new TokenType("++/--", {prefix: true, postfix: true, startsExpr: true}),
    prefix: new TokenType("!/~", {beforeExpr: true, prefix: true, startsExpr: true}),
    logicalOR: binop("||", 1),
    logicalAND: binop("&&", 2),
    bitwiseOR: binop("|", 3),
    bitwiseXOR: binop("^", 4),
    bitwiseAND: binop("&", 5),
    equality: binop("==/!=/===/!==", 6),
    relational: binop("</>/<=/>=", 7),
    bitShift: binop("<</>>/>>>", 8),
    plusMin: new TokenType("+/-", {beforeExpr: true, binop: 9, prefix: true, startsExpr: true}),
    modulo: binop("%", 10),
    star: binop("*", 10),
    slash: binop("/", 10),
    starstar: new TokenType("**", {beforeExpr: true}),
    coalesce: binop("??", 1),

    // Keyword token types.
    _break: kw("break"),
    _case: kw("case", beforeExpr),
    _catch: kw("catch"),
    _continue: kw("continue"),
    _debugger: kw("debugger"),
    _default: kw("default", beforeExpr),
    _do: kw("do", {isLoop: true, beforeExpr: true}),
    _else: kw("else", beforeExpr),
    _finally: kw("finally"),
    _for: kw("for", {isLoop: true}),
    _function: kw("function", startsExpr),
    _if: kw("if"),
    _return: kw("return", beforeExpr),
    _switch: kw("switch"),
    _throw: kw("throw", beforeExpr),
    _try: kw("try"),
    _var: kw("var"),
    _const: kw("const"),
    _while: kw("while", {isLoop: true}),
    _with: kw("with"),
    _new: kw("new", {beforeExpr: true, startsExpr: true}),
    _this: kw("this", startsExpr),
    _super: kw("super", startsExpr),
    _class: kw("class", startsExpr),
    _extends: kw("extends", beforeExpr),
    _export: kw("export"),
    _import: kw("import", startsExpr),
    _null: kw("null", startsExpr),
    _true: kw("true", startsExpr),
    _false: kw("false", startsExpr),
    _in: kw("in", {beforeExpr: true, binop: 7}),
    _instanceof: kw("instanceof", {beforeExpr: true, binop: 7}),
    _typeof: kw("typeof", {beforeExpr: true, prefix: true, startsExpr: true}),
    _void: kw("void", {beforeExpr: true, prefix: true, startsExpr: true}),
    _delete: kw("delete", {beforeExpr: true, prefix: true, startsExpr: true})
  };

  // Matches a whole line break (where CRLF is considered a single
  // line break). Used to count lines.

  var lineBreak = /\r\n?|\n|\u2028|\u2029/;
  var lineBreakG = new RegExp(lineBreak.source, "g");

  function isNewLine(code) {
    return code === 10 || code === 13 || code === 0x2028 || code === 0x2029
  }

  function nextLineBreak(code, from, end) {
    if ( end === void 0 ) end = code.length;

    for (var i = from; i < end; i++) {
      var next = code.charCodeAt(i);
      if (isNewLine(next))
        { return i < end - 1 && next === 13 && code.charCodeAt(i + 1) === 10 ? i + 2 : i + 1 }
    }
    return -1
  }

  var nonASCIIwhitespace = /[\u1680\u2000-\u200a\u202f\u205f\u3000\ufeff]/;

  var skipWhiteSpace = /(?:\s|\/\/.*|\/\*[^]*?\*\/)*/g;

  var ref = Object.prototype;
  var hasOwnProperty = ref.hasOwnProperty;
  var toString = ref.toString;

  var hasOwn = Object.hasOwn || (function (obj, propName) { return (
    hasOwnProperty.call(obj, propName)
  ); });

  var isArray = Array.isArray || (function (obj) { return (
    toString.call(obj) === "[object Array]"
  ); });

  var regexpCache = Object.create(null);

  function wordsRegexp(words) {
    return regexpCache[words] || (regexpCache[words] = new RegExp("^(?:" + words.replace(/ /g, "|") + ")$"))
  }

  function codePointToString(code) {
    // UTF-16 Decoding
    if (code <= 0xFFFF) { return String.fromCharCode(code) }
    code -= 0x10000;
    return String.fromCharCode((code >> 10) + 0xD800, (code & 1023) + 0xDC00)
  }

  var loneSurrogate = /(?:[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?:[^\uD800-\uDBFF]|^)[\uDC00-\uDFFF])/;

  // These are used when `options.locations` is on, for the
  // `startLoc` and `endLoc` properties.

  var Position = function Position(line, col) {
    this.line = line;
    this.column = col;
  };

  Position.prototype.offset = function offset (n) {
    return new Position(this.line, this.column + n)
  };

  var SourceLocation = function SourceLocation(p, start, end) {
    this.start = start;
    this.end = end;
    if (p.sourceFile !== null) { this.source = p.sourceFile; }
  };

  // The `getLineInfo` function is mostly useful when the
  // `locations` option is off (for performance reasons) and you
  // want to find the line/column position for a given character
  // offset. `input` should be the code string that the offset refers
  // into.

  function getLineInfo(input, offset) {
    for (var line = 1, cur = 0;;) {
      var nextBreak = nextLineBreak(input, cur, offset);
      if (nextBreak < 0) { return new Position(line, offset - cur) }
      ++line;
      cur = nextBreak;
    }
  }

  // A second argument must be given to configure the parser process.
  // These options are recognized (only `ecmaVersion` is required):

  var defaultOptions = {
    // `ecmaVersion` indicates the ECMAScript version to parse. Must be
    // either 3, 5, 6 (or 2015), 7 (2016), 8 (2017), 9 (2018), 10
    // (2019), 11 (2020), 12 (2021), 13 (2022), 14 (2023), or `"latest"`
    // (the latest version the library supports). This influences
    // support for strict mode, the set of reserved words, and support
    // for new syntax features.
    ecmaVersion: null,
    // `sourceType` indicates the mode the code should be parsed in.
    // Can be either `"script"` or `"module"`. This influences global
    // strict mode and parsing of `import` and `export` declarations.
    sourceType: "script",
    // `onInsertedSemicolon` can be a callback that will be called when
    // a semicolon is automatically inserted. It will be passed the
    // position of the inserted semicolon as an offset, and if
    // `locations` is enabled, it is given the location as a `{line,
    // column}` object as second argument.
    onInsertedSemicolon: null,
    // `onTrailingComma` is similar to `onInsertedSemicolon`, but for
    // trailing commas.
    onTrailingComma: null,
    // By default, reserved words are only enforced if ecmaVersion >= 5.
    // Set `allowReserved` to a boolean value to explicitly turn this on
    // an off. When this option has the value "never", reserved words
    // and keywords can also not be used as property names.
    allowReserved: null,
    // When enabled, a return at the top level is not considered an
    // error.
    allowReturnOutsideFunction: false,
    // When enabled, import/export statements are not constrained to
    // appearing at the top of the program, and an import.meta expression
    // in a script isn't considered an error.
    allowImportExportEverywhere: false,
    // By default, await identifiers are allowed to appear at the top-level scope only if ecmaVersion >= 2022.
    // When enabled, await identifiers are allowed to appear at the top-level scope,
    // but they are still not allowed in non-async functions.
    allowAwaitOutsideFunction: null,
    // When enabled, super identifiers are not constrained to
    // appearing in methods and do not raise an error when they appear elsewhere.
    allowSuperOutsideMethod: null,
    // When enabled, hashbang directive in the beginning of file is
    // allowed and treated as a line comment. Enabled by default when
    // `ecmaVersion` >= 2023.
    allowHashBang: false,
    // By default, the parser will verify that private properties are
    // only used in places where they are valid and have been declared.
    // Set this to false to turn such checks off.
    checkPrivateFields: true,
    // When `locations` is on, `loc` properties holding objects with
    // `start` and `end` properties in `{line, column}` form (with
    // line being 1-based and column 0-based) will be attached to the
    // nodes.
    locations: false,
    // A function can be passed as `onToken` option, which will
    // cause Acorn to call that function with object in the same
    // format as tokens returned from `tokenizer().getToken()`. Note
    // that you are not allowed to call the parser from the
    // callback—that will corrupt its internal state.
    onToken: null,
    // A function can be passed as `onComment` option, which will
    // cause Acorn to call that function with `(block, text, start,
    // end)` parameters whenever a comment is skipped. `block` is a
    // boolean indicating whether this is a block (`/* */`) comment,
    // `text` is the content of the comment, and `start` and `end` are
    // character offsets that denote the start and end of the comment.
    // When the `locations` option is on, two more parameters are
    // passed, the full `{line, column}` locations of the start and
    // end of the comments. Note that you are not allowed to call the
    // parser from the callback—that will corrupt its internal state.
    // When this option has an array as value, objects representing the
    // comments are pushed to it.
    onComment: null,
    // Nodes have their start and end characters offsets recorded in
    // `start` and `end` properties (directly on the node, rather than
    // the `loc` object, which holds line/column data. To also add a
    // [semi-standardized][range] `range` property holding a `[start,
    // end]` array with the same numbers, set the `ranges` option to
    // `true`.
    //
    // [range]: https://bugzilla.mozilla.org/show_bug.cgi?id=745678
    ranges: false,
    // It is possible to parse multiple files into a single AST by
    // passing the tree produced by parsing the first file as
    // `program` option in subsequent parses. This will add the
    // toplevel forms of the parsed file to the `Program` (top) node
    // of an existing parse tree.
    program: null,
    // When `locations` is on, you can pass this to record the source
    // file in every node's `loc` object.
    sourceFile: null,
    // This value, if given, is stored in every node, whether
    // `locations` is on or off.
    directSourceFile: null,
    // When enabled, parenthesized expressions are represented by
    // (non-standard) ParenthesizedExpression nodes
    preserveParens: false
  };

  // Interpret and default an options object

  var warnedAboutEcmaVersion = false;

  function getOptions(opts) {
    var options = {};

    for (var opt in defaultOptions)
      { options[opt] = opts && hasOwn(opts, opt) ? opts[opt] : defaultOptions[opt]; }

    if (options.ecmaVersion === "latest") {
      options.ecmaVersion = 1e8;
    } else if (options.ecmaVersion == null) {
      if (!warnedAboutEcmaVersion && typeof console === "object" && console.warn) {
        warnedAboutEcmaVersion = true;
        console.warn("Since Acorn 8.0.0, options.ecmaVersion is required.\nDefaulting to 2020, but this will stop working in the future.");
      }
      options.ecmaVersion = 11;
    } else if (options.ecmaVersion >= 2015) {
      options.ecmaVersion -= 2009;
    }

    if (options.allowReserved == null)
      { options.allowReserved = options.ecmaVersion < 5; }

    if (!opts || opts.allowHashBang == null)
      { options.allowHashBang = options.ecmaVersion >= 14; }

    if (isArray(options.onToken)) {
      var tokens = options.onToken;
      options.onToken = function (token) { return tokens.push(token); };
    }
    if (isArray(options.onComment))
      { options.onComment = pushComment(options, options.onComment); }

    return options
  }

  function pushComment(options, array) {
    return function(block, text, start, end, startLoc, endLoc) {
      var comment = {
        type: block ? "Block" : "Line",
        value: text,
        start: start,
        end: end
      };
      if (options.locations)
        { comment.loc = new SourceLocation(this, startLoc, endLoc); }
      if (options.ranges)
        { comment.range = [start, end]; }
      array.push(comment);
    }
  }

  // Each scope gets a bitset that may contain these flags
  var
      SCOPE_TOP = 1,
      SCOPE_FUNCTION = 2,
      SCOPE_ASYNC = 4,
      SCOPE_GENERATOR = 8,
      SCOPE_ARROW = 16,
      SCOPE_SIMPLE_CATCH = 32,
      SCOPE_SUPER = 64,
      SCOPE_DIRECT_SUPER = 128,
      SCOPE_CLASS_STATIC_BLOCK = 256,
      SCOPE_VAR = SCOPE_TOP | SCOPE_FUNCTION | SCOPE_CLASS_STATIC_BLOCK;

  function functionFlags(async, generator) {
    return SCOPE_FUNCTION | (async ? SCOPE_ASYNC : 0) | (generator ? SCOPE_GENERATOR : 0)
  }

  // Used in checkLVal* and declareName to determine the type of a binding
  var
      BIND_NONE = 0, // Not a binding
      BIND_VAR = 1, // Var-style binding
      BIND_LEXICAL = 2, // Let- or const-style binding
      BIND_FUNCTION = 3, // Function declaration
      BIND_SIMPLE_CATCH = 4, // Simple (identifier pattern) catch binding
      BIND_OUTSIDE = 5; // Special case for function names as bound inside the function

  var Parser = function Parser(options, input, startPos) {
    this.options = options = getOptions(options);
    this.sourceFile = options.sourceFile;
    this.keywords = wordsRegexp(keywords$1[options.ecmaVersion >= 6 ? 6 : options.sourceType === "module" ? "5module" : 5]);
    var reserved = "";
    if (options.allowReserved !== true) {
      reserved = reservedWords[options.ecmaVersion >= 6 ? 6 : options.ecmaVersion === 5 ? 5 : 3];
      if (options.sourceType === "module") { reserved += " await"; }
    }
    this.reservedWords = wordsRegexp(reserved);
    var reservedStrict = (reserved ? reserved + " " : "") + reservedWords.strict;
    this.reservedWordsStrict = wordsRegexp(reservedStrict);
    this.reservedWordsStrictBind = wordsRegexp(reservedStrict + " " + reservedWords.strictBind);
    this.input = String(input);

    // Used to signal to callers of `readWord1` whether the word
    // contained any escape sequences. This is needed because words with
    // escape sequences must not be interpreted as keywords.
    this.containsEsc = false;

    // Set up token state

    // The current position of the tokenizer in the input.
    if (startPos) {
      this.pos = startPos;
      this.lineStart = this.input.lastIndexOf("\n", startPos - 1) + 1;
      this.curLine = this.input.slice(0, this.lineStart).split(lineBreak).length;
    } else {
      this.pos = this.lineStart = 0;
      this.curLine = 1;
    }

    // Properties of the current token:
    // Its type
    this.type = types$1.eof;
    // For tokens that include more information than their type, the value
    this.value = null;
    // Its start and end offset
    this.start = this.end = this.pos;
    // And, if locations are used, the {line, column} object
    // corresponding to those offsets
    this.startLoc = this.endLoc = this.curPosition();

    // Position information for the previous token
    this.lastTokEndLoc = this.lastTokStartLoc = null;
    this.lastTokStart = this.lastTokEnd = this.pos;

    // The context stack is used to superficially track syntactic
    // context to predict whether a regular expression is allowed in a
    // given position.
    this.context = this.initialContext();
    this.exprAllowed = true;

    // Figure out if it's a module code.
    this.inModule = options.sourceType === "module";
    this.strict = this.inModule || this.strictDirective(this.pos);

    // Used to signify the start of a potential arrow function
    this.potentialArrowAt = -1;
    this.potentialArrowInForAwait = false;

    // Positions to delayed-check that yield/await does not exist in default parameters.
    this.yieldPos = this.awaitPos = this.awaitIdentPos = 0;
    // Labels in scope.
    this.labels = [];
    // Thus-far undefined exports.
    this.undefinedExports = Object.create(null);

    // If enabled, skip leading hashbang line.
    if (this.pos === 0 && options.allowHashBang && this.input.slice(0, 2) === "#!")
      { this.skipLineComment(2); }

    // Scope tracking for duplicate variable names (see scope.js)
    this.scopeStack = [];
    this.enterScope(SCOPE_TOP);

    // For RegExp validation
    this.regexpState = null;

    // The stack of private names.
    // Each element has two properties: 'declared' and 'used'.
    // When it exited from the outermost class definition, all used private names must be declared.
    this.privateNameStack = [];
  };

  var prototypeAccessors = { inFunction: { configurable: true },inGenerator: { configurable: true },inAsync: { configurable: true },canAwait: { configurable: true },allowSuper: { configurable: true },allowDirectSuper: { configurable: true },treatFunctionsAsVar: { configurable: true },allowNewDotTarget: { configurable: true },inClassStaticBlock: { configurable: true } };

  Parser.prototype.parse = function parse () {
    var node = this.options.program || this.startNode();
    this.nextToken();
    return this.parseTopLevel(node)
  };

  prototypeAccessors.inFunction.get = function () { return (this.currentVarScope().flags & SCOPE_FUNCTION) > 0 };

  prototypeAccessors.inGenerator.get = function () { return (this.currentVarScope().flags & SCOPE_GENERATOR) > 0 && !this.currentVarScope().inClassFieldInit };

  prototypeAccessors.inAsync.get = function () { return (this.currentVarScope().flags & SCOPE_ASYNC) > 0 && !this.currentVarScope().inClassFieldInit };

  prototypeAccessors.canAwait.get = function () {
    for (var i = this.scopeStack.length - 1; i >= 0; i--) {
      var scope = this.scopeStack[i];
      if (scope.inClassFieldInit || scope.flags & SCOPE_CLASS_STATIC_BLOCK) { return false }
      if (scope.flags & SCOPE_FUNCTION) { return (scope.flags & SCOPE_ASYNC) > 0 }
    }
    return (this.inModule && this.options.ecmaVersion >= 13) || this.options.allowAwaitOutsideFunction
  };

  prototypeAccessors.allowSuper.get = function () {
    var ref = this.currentThisScope();
      var flags = ref.flags;
      var inClassFieldInit = ref.inClassFieldInit;
    return (flags & SCOPE_SUPER) > 0 || inClassFieldInit || this.options.allowSuperOutsideMethod
  };

  prototypeAccessors.allowDirectSuper.get = function () { return (this.currentThisScope().flags & SCOPE_DIRECT_SUPER) > 0 };

  prototypeAccessors.treatFunctionsAsVar.get = function () { return this.treatFunctionsAsVarInScope(this.currentScope()) };

  prototypeAccessors.allowNewDotTarget.get = function () {
    var ref = this.currentThisScope();
      var flags = ref.flags;
      var inClassFieldInit = ref.inClassFieldInit;
    return (flags & (SCOPE_FUNCTION | SCOPE_CLASS_STATIC_BLOCK)) > 0 || inClassFieldInit
  };

  prototypeAccessors.inClassStaticBlock.get = function () {
    return (this.currentVarScope().flags & SCOPE_CLASS_STATIC_BLOCK) > 0
  };

  Parser.extend = function extend () {
      var plugins = [], len = arguments.length;
      while ( len-- ) plugins[ len ] = arguments[ len ];

    var cls = this;
    for (var i = 0; i < plugins.length; i++) { cls = plugins[i](cls); }
    return cls
  };

  Parser.parse = function parse (input, options) {
    return new this(options, input).parse()
  };

  Parser.parseExpressionAt = function parseExpressionAt (input, pos, options) {
    var parser = new this(options, input, pos);
    parser.nextToken();
    return parser.parseExpression()
  };

  Parser.tokenizer = function tokenizer (input, options) {
    return new this(options, input)
  };

  Object.defineProperties( Parser.prototype, prototypeAccessors );

  var pp$9 = Parser.prototype;

  // ## Parser utilities

  var literal = /^(?:'((?:\\[^]|[^'\\])*?)'|"((?:\\[^]|[^"\\])*?)")/;
  pp$9.strictDirective = function(start) {
    if (this.options.ecmaVersion < 5) { return false }
    for (;;) {
      // Try to find string literal.
      skipWhiteSpace.lastIndex = start;
      start += skipWhiteSpace.exec(this.input)[0].length;
      var match = literal.exec(this.input.slice(start));
      if (!match) { return false }
      if ((match[1] || match[2]) === "use strict") {
        skipWhiteSpace.lastIndex = start + match[0].length;
        var spaceAfter = skipWhiteSpace.exec(this.input), end = spaceAfter.index + spaceAfter[0].length;
        var next = this.input.charAt(end);
        return next === ";" || next === "}" ||
          (lineBreak.test(spaceAfter[0]) &&
           !(/[(`.[+\-/*%<>=,?^&]/.test(next) || next === "!" && this.input.charAt(end + 1) === "="))
      }
      start += match[0].length;

      // Skip semicolon, if any.
      skipWhiteSpace.lastIndex = start;
      start += skipWhiteSpace.exec(this.input)[0].length;
      if (this.input[start] === ";")
        { start++; }
    }
  };

  // Predicate that tests whether the next token is of the given
  // type, and if yes, consumes it as a side effect.

  pp$9.eat = function(type) {
    if (this.type === type) {
      this.next();
      return true
    } else {
      return false
    }
  };

  // Tests whether parsed token is a contextual keyword.

  pp$9.isContextual = function(name) {
    return this.type === types$1.name && this.value === name && !this.containsEsc
  };

  // Consumes contextual keyword if possible.

  pp$9.eatContextual = function(name) {
    if (!this.isContextual(name)) { return false }
    this.next();
    return true
  };

  // Asserts that following token is given contextual keyword.

  pp$9.expectContextual = function(name) {
    if (!this.eatContextual(name)) { this.unexpected(); }
  };

  // Test whether a semicolon can be inserted at the current position.

  pp$9.canInsertSemicolon = function() {
    return this.type === types$1.eof ||
      this.type === types$1.braceR ||
      lineBreak.test(this.input.slice(this.lastTokEnd, this.start))
  };

  pp$9.insertSemicolon = function() {
    if (this.canInsertSemicolon()) {
      if (this.options.onInsertedSemicolon)
        { this.options.onInsertedSemicolon(this.lastTokEnd, this.lastTokEndLoc); }
      return true
    }
  };

  // Consume a semicolon, or, failing that, see if we are allowed to
  // pretend that there is a semicolon at this position.

  pp$9.semicolon = function() {
    if (!this.eat(types$1.semi) && !this.insertSemicolon()) { this.unexpected(); }
  };

  pp$9.afterTrailingComma = function(tokType, notNext) {
    if (this.type === tokType) {
      if (this.options.onTrailingComma)
        { this.options.onTrailingComma(this.lastTokStart, this.lastTokStartLoc); }
      if (!notNext)
        { this.next(); }
      return true
    }
  };

  // Expect a token of a given type. If found, consume it, otherwise,
  // raise an unexpected token error.

  pp$9.expect = function(type) {
    this.eat(type) || this.unexpected();
  };

  // Raise an unexpected token error.

  pp$9.unexpected = function(pos) {
    this.raise(pos != null ? pos : this.start, "Unexpected token");
  };

  var DestructuringErrors = function DestructuringErrors() {
    this.shorthandAssign =
    this.trailingComma =
    this.parenthesizedAssign =
    this.parenthesizedBind =
    this.doubleProto =
      -1;
  };

  pp$9.checkPatternErrors = function(refDestructuringErrors, isAssign) {
    if (!refDestructuringErrors) { return }
    if (refDestructuringErrors.trailingComma > -1)
      { this.raiseRecoverable(refDestructuringErrors.trailingComma, "Comma is not permitted after the rest element"); }
    var parens = isAssign ? refDestructuringErrors.parenthesizedAssign : refDestructuringErrors.parenthesizedBind;
    if (parens > -1) { this.raiseRecoverable(parens, isAssign ? "Assigning to rvalue" : "Parenthesized pattern"); }
  };

  pp$9.checkExpressionErrors = function(refDestructuringErrors, andThrow) {
    if (!refDestructuringErrors) { return false }
    var shorthandAssign = refDestructuringErrors.shorthandAssign;
    var doubleProto = refDestructuringErrors.doubleProto;
    if (!andThrow) { return shorthandAssign >= 0 || doubleProto >= 0 }
    if (shorthandAssign >= 0)
      { this.raise(shorthandAssign, "Shorthand property assignments are valid only in destructuring patterns"); }
    if (doubleProto >= 0)
      { this.raiseRecoverable(doubleProto, "Redefinition of __proto__ property"); }
  };

  pp$9.checkYieldAwaitInDefaultParams = function() {
    if (this.yieldPos && (!this.awaitPos || this.yieldPos < this.awaitPos))
      { this.raise(this.yieldPos, "Yield expression cannot be a default value"); }
    if (this.awaitPos)
      { this.raise(this.awaitPos, "Await expression cannot be a default value"); }
  };

  pp$9.isSimpleAssignTarget = function(expr) {
    if (expr.type === "ParenthesizedExpression")
      { return this.isSimpleAssignTarget(expr.expression) }
    return expr.type === "Identifier" || expr.type === "MemberExpression"
  };

  var pp$8 = Parser.prototype;

  // ### Statement parsing

  // Parse a program. Initializes the parser, reads any number of
  // statements, and wraps them in a Program node.  Optionally takes a
  // `program` argument.  If present, the statements will be appended
  // to its body instead of creating a new node.

  pp$8.parseTopLevel = function(node) {
    var exports = Object.create(null);
    if (!node.body) { node.body = []; }
    while (this.type !== types$1.eof) {
      var stmt = this.parseStatement(null, true, exports);
      node.body.push(stmt);
    }
    if (this.inModule)
      { for (var i = 0, list = Object.keys(this.undefinedExports); i < list.length; i += 1)
        {
          var name = list[i];

          this.raiseRecoverable(this.undefinedExports[name].start, ("Export '" + name + "' is not defined"));
        } }
    this.adaptDirectivePrologue(node.body);
    this.next();
    node.sourceType = this.options.sourceType;
    return this.finishNode(node, "Program")
  };

  var loopLabel = {kind: "loop"}, switchLabel = {kind: "switch"};

  pp$8.isLet = function(context) {
    if (this.options.ecmaVersion < 6 || !this.isContextual("let")) { return false }
    skipWhiteSpace.lastIndex = this.pos;
    var skip = skipWhiteSpace.exec(this.input);
    var next = this.pos + skip[0].length, nextCh = this.input.charCodeAt(next);
    // For ambiguous cases, determine if a LexicalDeclaration (or only a
    // Statement) is allowed here. If context is not empty then only a Statement
    // is allowed. However, `let [` is an explicit negative lookahead for
    // ExpressionStatement, so special-case it first.
    if (nextCh === 91 || nextCh === 92) { return true } // '[', '\'
    if (context) { return false }

    if (nextCh === 123 || nextCh > 0xd7ff && nextCh < 0xdc00) { return true } // '{', astral
    if (isIdentifierStart(nextCh, true)) {
      var pos = next + 1;
      while (isIdentifierChar(nextCh = this.input.charCodeAt(pos), true)) { ++pos; }
      if (nextCh === 92 || nextCh > 0xd7ff && nextCh < 0xdc00) { return true }
      var ident = this.input.slice(next, pos);
      if (!keywordRelationalOperator.test(ident)) { return true }
    }
    return false
  };

  // check 'async [no LineTerminator here] function'
  // - 'async /*foo*/ function' is OK.
  // - 'async /*\n*/ function' is invalid.
  pp$8.isAsyncFunction = function() {
    if (this.options.ecmaVersion < 8 || !this.isContextual("async"))
      { return false }

    skipWhiteSpace.lastIndex = this.pos;
    var skip = skipWhiteSpace.exec(this.input);
    var next = this.pos + skip[0].length, after;
    return !lineBreak.test(this.input.slice(this.pos, next)) &&
      this.input.slice(next, next + 8) === "function" &&
      (next + 8 === this.input.length ||
       !(isIdentifierChar(after = this.input.charCodeAt(next + 8)) || after > 0xd7ff && after < 0xdc00))
  };

  // Parse a single statement.
  //
  // If expecting a statement and finding a slash operator, parse a
  // regular expression literal. This is to handle cases like
  // `if (foo) /blah/.exec(foo)`, where looking at the previous token
  // does not help.

  pp$8.parseStatement = function(context, topLevel, exports) {
    var starttype = this.type, node = this.startNode(), kind;

    if (this.isLet(context)) {
      starttype = types$1._var;
      kind = "let";
    }

    // Most types of statements are recognized by the keyword they
    // start with. Many are trivial to parse, some require a bit of
    // complexity.

    switch (starttype) {
    case types$1._break: case types$1._continue: return this.parseBreakContinueStatement(node, starttype.keyword)
    case types$1._debugger: return this.parseDebuggerStatement(node)
    case types$1._do: return this.parseDoStatement(node)
    case types$1._for: return this.parseForStatement(node)
    case types$1._function:
      // Function as sole body of either an if statement or a labeled statement
      // works, but not when it is part of a labeled statement that is the sole
      // body of an if statement.
      if ((context && (this.strict || context !== "if" && context !== "label")) && this.options.ecmaVersion >= 6) { this.unexpected(); }
      return this.parseFunctionStatement(node, false, !context)
    case types$1._class:
      if (context) { this.unexpected(); }
      return this.parseClass(node, true)
    case types$1._if: return this.parseIfStatement(node)
    case types$1._return: return this.parseReturnStatement(node)
    case types$1._switch: return this.parseSwitchStatement(node)
    case types$1._throw: return this.parseThrowStatement(node)
    case types$1._try: return this.parseTryStatement(node)
    case types$1._const: case types$1._var:
      kind = kind || this.value;
      if (context && kind !== "var") { this.unexpected(); }
      return this.parseVarStatement(node, kind)
    case types$1._while: return this.parseWhileStatement(node)
    case types$1._with: return this.parseWithStatement(node)
    case types$1.braceL: return this.parseBlock(true, node)
    case types$1.semi: return this.parseEmptyStatement(node)
    case types$1._export:
    case types$1._import:
      if (this.options.ecmaVersion > 10 && starttype === types$1._import) {
        skipWhiteSpace.lastIndex = this.pos;
        var skip = skipWhiteSpace.exec(this.input);
        var next = this.pos + skip[0].length, nextCh = this.input.charCodeAt(next);
        if (nextCh === 40 || nextCh === 46) // '(' or '.'
          { return this.parseExpressionStatement(node, this.parseExpression()) }
      }

      if (!this.options.allowImportExportEverywhere) {
        if (!topLevel)
          { this.raise(this.start, "'import' and 'export' may only appear at the top level"); }
        if (!this.inModule)
          { this.raise(this.start, "'import' and 'export' may appear only with 'sourceType: module'"); }
      }
      return starttype === types$1._import ? this.parseImport(node) : this.parseExport(node, exports)

      // If the statement does not start with a statement keyword or a
      // brace, it's an ExpressionStatement or LabeledStatement. We
      // simply start parsing an expression, and afterwards, if the
      // next token is a colon and the expression was a simple
      // Identifier node, we switch to interpreting it as a label.
    default:
      if (this.isAsyncFunction()) {
        if (context) { this.unexpected(); }
        this.next();
        return this.parseFunctionStatement(node, true, !context)
      }

      var maybeName = this.value, expr = this.parseExpression();
      if (starttype === types$1.name && expr.type === "Identifier" && this.eat(types$1.colon))
        { return this.parseLabeledStatement(node, maybeName, expr, context) }
      else { return this.parseExpressionStatement(node, expr) }
    }
  };

  pp$8.parseBreakContinueStatement = function(node, keyword) {
    var isBreak = keyword === "break";
    this.next();
    if (this.eat(types$1.semi) || this.insertSemicolon()) { node.label = null; }
    else if (this.type !== types$1.name) { this.unexpected(); }
    else {
      node.label = this.parseIdent();
      this.semicolon();
    }

    // Verify that there is an actual destination to break or
    // continue to.
    var i = 0;
    for (; i < this.labels.length; ++i) {
      var lab = this.labels[i];
      if (node.label == null || lab.name === node.label.name) {
        if (lab.kind != null && (isBreak || lab.kind === "loop")) { break }
        if (node.label && isBreak) { break }
      }
    }
    if (i === this.labels.length) { this.raise(node.start, "Unsyntactic " + keyword); }
    return this.finishNode(node, isBreak ? "BreakStatement" : "ContinueStatement")
  };

  pp$8.parseDebuggerStatement = function(node) {
    this.next();
    this.semicolon();
    return this.finishNode(node, "DebuggerStatement")
  };

  pp$8.parseDoStatement = function(node) {
    this.next();
    this.labels.push(loopLabel);
    node.body = this.parseStatement("do");
    this.labels.pop();
    this.expect(types$1._while);
    node.test = this.parseParenExpression();
    if (this.options.ecmaVersion >= 6)
      { this.eat(types$1.semi); }
    else
      { this.semicolon(); }
    return this.finishNode(node, "DoWhileStatement")
  };

  // Disambiguating between a `for` and a `for`/`in` or `for`/`of`
  // loop is non-trivial. Basically, we have to parse the init `var`
  // statement or expression, disallowing the `in` operator (see
  // the second parameter to `parseExpression`), and then check
  // whether the next token is `in` or `of`. When there is no init
  // part (semicolon immediately after the opening parenthesis), it
  // is a regular `for` loop.

  pp$8.parseForStatement = function(node) {
    this.next();
    var awaitAt = (this.options.ecmaVersion >= 9 && this.canAwait && this.eatContextual("await")) ? this.lastTokStart : -1;
    this.labels.push(loopLabel);
    this.enterScope(0);
    this.expect(types$1.parenL);
    if (this.type === types$1.semi) {
      if (awaitAt > -1) { this.unexpected(awaitAt); }
      return this.parseFor(node, null)
    }
    var isLet = this.isLet();
    if (this.type === types$1._var || this.type === types$1._const || isLet) {
      var init$1 = this.startNode(), kind = isLet ? "let" : this.value;
      this.next();
      this.parseVar(init$1, true, kind);
      this.finishNode(init$1, "VariableDeclaration");
      if ((this.type === types$1._in || (this.options.ecmaVersion >= 6 && this.isContextual("of"))) && init$1.declarations.length === 1) {
        if (this.options.ecmaVersion >= 9) {
          if (this.type === types$1._in) {
            if (awaitAt > -1) { this.unexpected(awaitAt); }
          } else { node.await = awaitAt > -1; }
        }
        return this.parseForIn(node, init$1)
      }
      if (awaitAt > -1) { this.unexpected(awaitAt); }
      return this.parseFor(node, init$1)
    }
    var startsWithLet = this.isContextual("let"), isForOf = false;
    var containsEsc = this.containsEsc;
    var refDestructuringErrors = new DestructuringErrors;
    var initPos = this.start;
    var init = awaitAt > -1
      ? this.parseExprSubscripts(refDestructuringErrors, "await")
      : this.parseExpression(true, refDestructuringErrors);
    if (this.type === types$1._in || (isForOf = this.options.ecmaVersion >= 6 && this.isContextual("of"))) {
      if (awaitAt > -1) { // implies `ecmaVersion >= 9` (see declaration of awaitAt)
        if (this.type === types$1._in) { this.unexpected(awaitAt); }
        node.await = true;
      } else if (isForOf && this.options.ecmaVersion >= 8) {
        if (init.start === initPos && !containsEsc && init.type === "Identifier" && init.name === "async") { this.unexpected(); }
        else if (this.options.ecmaVersion >= 9) { node.await = false; }
      }
      if (startsWithLet && isForOf) { this.raise(init.start, "The left-hand side of a for-of loop may not start with 'let'."); }
      this.toAssignable(init, false, refDestructuringErrors);
      this.checkLValPattern(init);
      return this.parseForIn(node, init)
    } else {
      this.checkExpressionErrors(refDestructuringErrors, true);
    }
    if (awaitAt > -1) { this.unexpected(awaitAt); }
    return this.parseFor(node, init)
  };

  pp$8.parseFunctionStatement = function(node, isAsync, declarationPosition) {
    this.next();
    return this.parseFunction(node, FUNC_STATEMENT | (declarationPosition ? 0 : FUNC_HANGING_STATEMENT), false, isAsync)
  };

  pp$8.parseIfStatement = function(node) {
    this.next();
    node.test = this.parseParenExpression();
    // allow function declarations in branches, but only in non-strict mode
    node.consequent = this.parseStatement("if");
    node.alternate = this.eat(types$1._else) ? this.parseStatement("if") : null;
    return this.finishNode(node, "IfStatement")
  };

  pp$8.parseReturnStatement = function(node) {
    if (!this.inFunction && !this.options.allowReturnOutsideFunction)
      { this.raise(this.start, "'return' outside of function"); }
    this.next();

    // In `return` (and `break`/`continue`), the keywords with
    // optional arguments, we eagerly look for a semicolon or the
    // possibility to insert one.

    if (this.eat(types$1.semi) || this.insertSemicolon()) { node.argument = null; }
    else { node.argument = this.parseExpression(); this.semicolon(); }
    return this.finishNode(node, "ReturnStatement")
  };

  pp$8.parseSwitchStatement = function(node) {
    this.next();
    node.discriminant = this.parseParenExpression();
    node.cases = [];
    this.expect(types$1.braceL);
    this.labels.push(switchLabel);
    this.enterScope(0);

    // Statements under must be grouped (by label) in SwitchCase
    // nodes. `cur` is used to keep the node that we are currently
    // adding statements to.

    var cur;
    for (var sawDefault = false; this.type !== types$1.braceR;) {
      if (this.type === types$1._case || this.type === types$1._default) {
        var isCase = this.type === types$1._case;
        if (cur) { this.finishNode(cur, "SwitchCase"); }
        node.cases.push(cur = this.startNode());
        cur.consequent = [];
        this.next();
        if (isCase) {
          cur.test = this.parseExpression();
        } else {
          if (sawDefault) { this.raiseRecoverable(this.lastTokStart, "Multiple default clauses"); }
          sawDefault = true;
          cur.test = null;
        }
        this.expect(types$1.colon);
      } else {
        if (!cur) { this.unexpected(); }
        cur.consequent.push(this.parseStatement(null));
      }
    }
    this.exitScope();
    if (cur) { this.finishNode(cur, "SwitchCase"); }
    this.next(); // Closing brace
    this.labels.pop();
    return this.finishNode(node, "SwitchStatement")
  };

  pp$8.parseThrowStatement = function(node) {
    this.next();
    if (lineBreak.test(this.input.slice(this.lastTokEnd, this.start)))
      { this.raise(this.lastTokEnd, "Illegal newline after throw"); }
    node.argument = this.parseExpression();
    this.semicolon();
    return this.finishNode(node, "ThrowStatement")
  };

  // Reused empty array added for node fields that are always empty.

  var empty$1 = [];

  pp$8.parseCatchClauseParam = function() {
    var param = this.parseBindingAtom();
    var simple = param.type === "Identifier";
    this.enterScope(simple ? SCOPE_SIMPLE_CATCH : 0);
    this.checkLValPattern(param, simple ? BIND_SIMPLE_CATCH : BIND_LEXICAL);
    this.expect(types$1.parenR);

    return param
  };

  pp$8.parseTryStatement = function(node) {
    this.next();
    node.block = this.parseBlock();
    node.handler = null;
    if (this.type === types$1._catch) {
      var clause = this.startNode();
      this.next();
      if (this.eat(types$1.parenL)) {
        clause.param = this.parseCatchClauseParam();
      } else {
        if (this.options.ecmaVersion < 10) { this.unexpected(); }
        clause.param = null;
        this.enterScope(0);
      }
      clause.body = this.parseBlock(false);
      this.exitScope();
      node.handler = this.finishNode(clause, "CatchClause");
    }
    node.finalizer = this.eat(types$1._finally) ? this.parseBlock() : null;
    if (!node.handler && !node.finalizer)
      { this.raise(node.start, "Missing catch or finally clause"); }
    return this.finishNode(node, "TryStatement")
  };

  pp$8.parseVarStatement = function(node, kind, allowMissingInitializer) {
    this.next();
    this.parseVar(node, false, kind, allowMissingInitializer);
    this.semicolon();
    return this.finishNode(node, "VariableDeclaration")
  };

  pp$8.parseWhileStatement = function(node) {
    this.next();
    node.test = this.parseParenExpression();
    this.labels.push(loopLabel);
    node.body = this.parseStatement("while");
    this.labels.pop();
    return this.finishNode(node, "WhileStatement")
  };

  pp$8.parseWithStatement = function(node) {
    if (this.strict) { this.raise(this.start, "'with' in strict mode"); }
    this.next();
    node.object = this.parseParenExpression();
    node.body = this.parseStatement("with");
    return this.finishNode(node, "WithStatement")
  };

  pp$8.parseEmptyStatement = function(node) {
    this.next();
    return this.finishNode(node, "EmptyStatement")
  };

  pp$8.parseLabeledStatement = function(node, maybeName, expr, context) {
    for (var i$1 = 0, list = this.labels; i$1 < list.length; i$1 += 1)
      {
      var label = list[i$1];

      if (label.name === maybeName)
        { this.raise(expr.start, "Label '" + maybeName + "' is already declared");
    } }
    var kind = this.type.isLoop ? "loop" : this.type === types$1._switch ? "switch" : null;
    for (var i = this.labels.length - 1; i >= 0; i--) {
      var label$1 = this.labels[i];
      if (label$1.statementStart === node.start) {
        // Update information about previous labels on this node
        label$1.statementStart = this.start;
        label$1.kind = kind;
      } else { break }
    }
    this.labels.push({name: maybeName, kind: kind, statementStart: this.start});
    node.body = this.parseStatement(context ? context.indexOf("label") === -1 ? context + "label" : context : "label");
    this.labels.pop();
    node.label = expr;
    return this.finishNode(node, "LabeledStatement")
  };

  pp$8.parseExpressionStatement = function(node, expr) {
    node.expression = expr;
    this.semicolon();
    return this.finishNode(node, "ExpressionStatement")
  };

  // Parse a semicolon-enclosed block of statements, handling `"use
  // strict"` declarations when `allowStrict` is true (used for
  // function bodies).

  pp$8.parseBlock = function(createNewLexicalScope, node, exitStrict) {
    if ( createNewLexicalScope === void 0 ) createNewLexicalScope = true;
    if ( node === void 0 ) node = this.startNode();

    node.body = [];
    this.expect(types$1.braceL);
    if (createNewLexicalScope) { this.enterScope(0); }
    while (this.type !== types$1.braceR) {
      var stmt = this.parseStatement(null);
      node.body.push(stmt);
    }
    if (exitStrict) { this.strict = false; }
    this.next();
    if (createNewLexicalScope) { this.exitScope(); }
    return this.finishNode(node, "BlockStatement")
  };

  // Parse a regular `for` loop. The disambiguation code in
  // `parseStatement` will already have parsed the init statement or
  // expression.

  pp$8.parseFor = function(node, init) {
    node.init = init;
    this.expect(types$1.semi);
    node.test = this.type === types$1.semi ? null : this.parseExpression();
    this.expect(types$1.semi);
    node.update = this.type === types$1.parenR ? null : this.parseExpression();
    this.expect(types$1.parenR);
    node.body = this.parseStatement("for");
    this.exitScope();
    this.labels.pop();
    return this.finishNode(node, "ForStatement")
  };

  // Parse a `for`/`in` and `for`/`of` loop, which are almost
  // same from parser's perspective.

  pp$8.parseForIn = function(node, init) {
    var isForIn = this.type === types$1._in;
    this.next();

    if (
      init.type === "VariableDeclaration" &&
      init.declarations[0].init != null &&
      (
        !isForIn ||
        this.options.ecmaVersion < 8 ||
        this.strict ||
        init.kind !== "var" ||
        init.declarations[0].id.type !== "Identifier"
      )
    ) {
      this.raise(
        init.start,
        ((isForIn ? "for-in" : "for-of") + " loop variable declaration may not have an initializer")
      );
    }
    node.left = init;
    node.right = isForIn ? this.parseExpression() : this.parseMaybeAssign();
    this.expect(types$1.parenR);
    node.body = this.parseStatement("for");
    this.exitScope();
    this.labels.pop();
    return this.finishNode(node, isForIn ? "ForInStatement" : "ForOfStatement")
  };

  // Parse a list of variable declarations.

  pp$8.parseVar = function(node, isFor, kind, allowMissingInitializer) {
    node.declarations = [];
    node.kind = kind;
    for (;;) {
      var decl = this.startNode();
      this.parseVarId(decl, kind);
      if (this.eat(types$1.eq)) {
        decl.init = this.parseMaybeAssign(isFor);
      } else if (!allowMissingInitializer && kind === "const" && !(this.type === types$1._in || (this.options.ecmaVersion >= 6 && this.isContextual("of")))) {
        this.unexpected();
      } else if (!allowMissingInitializer && decl.id.type !== "Identifier" && !(isFor && (this.type === types$1._in || this.isContextual("of")))) {
        this.raise(this.lastTokEnd, "Complex binding patterns require an initialization value");
      } else {
        decl.init = null;
      }
      node.declarations.push(this.finishNode(decl, "VariableDeclarator"));
      if (!this.eat(types$1.comma)) { break }
    }
    return node
  };

  pp$8.parseVarId = function(decl, kind) {
    decl.id = this.parseBindingAtom();
    this.checkLValPattern(decl.id, kind === "var" ? BIND_VAR : BIND_LEXICAL, false);
  };

  var FUNC_STATEMENT = 1, FUNC_HANGING_STATEMENT = 2, FUNC_NULLABLE_ID = 4;

  // Parse a function declaration or literal (depending on the
  // `statement & FUNC_STATEMENT`).

  // Remove `allowExpressionBody` for 7.0.0, as it is only called with false
  pp$8.parseFunction = function(node, statement, allowExpressionBody, isAsync, forInit) {
    this.initFunction(node);
    if (this.options.ecmaVersion >= 9 || this.options.ecmaVersion >= 6 && !isAsync) {
      if (this.type === types$1.star && (statement & FUNC_HANGING_STATEMENT))
        { this.unexpected(); }
      node.generator = this.eat(types$1.star);
    }
    if (this.options.ecmaVersion >= 8)
      { node.async = !!isAsync; }

    if (statement & FUNC_STATEMENT) {
      node.id = (statement & FUNC_NULLABLE_ID) && this.type !== types$1.name ? null : this.parseIdent();
      if (node.id && !(statement & FUNC_HANGING_STATEMENT))
        // If it is a regular function declaration in sloppy mode, then it is
        // subject to Annex B semantics (BIND_FUNCTION). Otherwise, the binding
        // mode depends on properties of the current scope (see
        // treatFunctionsAsVar).
        { this.checkLValSimple(node.id, (this.strict || node.generator || node.async) ? this.treatFunctionsAsVar ? BIND_VAR : BIND_LEXICAL : BIND_FUNCTION); }
    }

    var oldYieldPos = this.yieldPos, oldAwaitPos = this.awaitPos, oldAwaitIdentPos = this.awaitIdentPos;
    this.yieldPos = 0;
    this.awaitPos = 0;
    this.awaitIdentPos = 0;
    this.enterScope(functionFlags(node.async, node.generator));

    if (!(statement & FUNC_STATEMENT))
      { node.id = this.type === types$1.name ? this.parseIdent() : null; }

    this.parseFunctionParams(node);
    this.parseFunctionBody(node, allowExpressionBody, false, forInit);

    this.yieldPos = oldYieldPos;
    this.awaitPos = oldAwaitPos;
    this.awaitIdentPos = oldAwaitIdentPos;
    return this.finishNode(node, (statement & FUNC_STATEMENT) ? "FunctionDeclaration" : "FunctionExpression")
  };

  pp$8.parseFunctionParams = function(node) {
    this.expect(types$1.parenL);
    node.params = this.parseBindingList(types$1.parenR, false, this.options.ecmaVersion >= 8);
    this.checkYieldAwaitInDefaultParams();
  };

  // Parse a class declaration or literal (depending on the
  // `isStatement` parameter).

  pp$8.parseClass = function(node, isStatement) {
    this.next();

    // ecma-262 14.6 Class Definitions
    // A class definition is always strict mode code.
    var oldStrict = this.strict;
    this.strict = true;

    this.parseClassId(node, isStatement);
    this.parseClassSuper(node);
    var privateNameMap = this.enterClassBody();
    var classBody = this.startNode();
    var hadConstructor = false;
    classBody.body = [];
    this.expect(types$1.braceL);
    while (this.type !== types$1.braceR) {
      var element = this.parseClassElement(node.superClass !== null);
      if (element) {
        classBody.body.push(element);
        if (element.type === "MethodDefinition" && element.kind === "constructor") {
          if (hadConstructor) { this.raiseRecoverable(element.start, "Duplicate constructor in the same class"); }
          hadConstructor = true;
        } else if (element.key && element.key.type === "PrivateIdentifier" && isPrivateNameConflicted(privateNameMap, element)) {
          this.raiseRecoverable(element.key.start, ("Identifier '#" + (element.key.name) + "' has already been declared"));
        }
      }
    }
    this.strict = oldStrict;
    this.next();
    node.body = this.finishNode(classBody, "ClassBody");
    this.exitClassBody();
    return this.finishNode(node, isStatement ? "ClassDeclaration" : "ClassExpression")
  };

  pp$8.parseClassElement = function(constructorAllowsSuper) {
    if (this.eat(types$1.semi)) { return null }

    var ecmaVersion = this.options.ecmaVersion;
    var node = this.startNode();
    var keyName = "";
    var isGenerator = false;
    var isAsync = false;
    var kind = "method";
    var isStatic = false;

    if (this.eatContextual("static")) {
      // Parse static init block
      if (ecmaVersion >= 13 && this.eat(types$1.braceL)) {
        this.parseClassStaticBlock(node);
        return node
      }
      if (this.isClassElementNameStart() || this.type === types$1.star) {
        isStatic = true;
      } else {
        keyName = "static";
      }
    }
    node.static = isStatic;
    if (!keyName && ecmaVersion >= 8 && this.eatContextual("async")) {
      if ((this.isClassElementNameStart() || this.type === types$1.star) && !this.canInsertSemicolon()) {
        isAsync = true;
      } else {
        keyName = "async";
      }
    }
    if (!keyName && (ecmaVersion >= 9 || !isAsync) && this.eat(types$1.star)) {
      isGenerator = true;
    }
    if (!keyName && !isAsync && !isGenerator) {
      var lastValue = this.value;
      if (this.eatContextual("get") || this.eatContextual("set")) {
        if (this.isClassElementNameStart()) {
          kind = lastValue;
        } else {
          keyName = lastValue;
        }
      }
    }

    // Parse element name
    if (keyName) {
      // 'async', 'get', 'set', or 'static' were not a keyword contextually.
      // The last token is any of those. Make it the element name.
      node.computed = false;
      node.key = this.startNodeAt(this.lastTokStart, this.lastTokStartLoc);
      node.key.name = keyName;
      this.finishNode(node.key, "Identifier");
    } else {
      this.parseClassElementName(node);
    }

    // Parse element value
    if (ecmaVersion < 13 || this.type === types$1.parenL || kind !== "method" || isGenerator || isAsync) {
      var isConstructor = !node.static && checkKeyName(node, "constructor");
      var allowsDirectSuper = isConstructor && constructorAllowsSuper;
      // Couldn't move this check into the 'parseClassMethod' method for backward compatibility.
      if (isConstructor && kind !== "method") { this.raise(node.key.start, "Constructor can't have get/set modifier"); }
      node.kind = isConstructor ? "constructor" : kind;
      this.parseClassMethod(node, isGenerator, isAsync, allowsDirectSuper);
    } else {
      this.parseClassField(node);
    }

    return node
  };

  pp$8.isClassElementNameStart = function() {
    return (
      this.type === types$1.name ||
      this.type === types$1.privateId ||
      this.type === types$1.num ||
      this.type === types$1.string ||
      this.type === types$1.bracketL ||
      this.type.keyword
    )
  };

  pp$8.parseClassElementName = function(element) {
    if (this.type === types$1.privateId) {
      if (this.value === "constructor") {
        this.raise(this.start, "Classes can't have an element named '#constructor'");
      }
      element.computed = false;
      element.key = this.parsePrivateIdent();
    } else {
      this.parsePropertyName(element);
    }
  };

  pp$8.parseClassMethod = function(method, isGenerator, isAsync, allowsDirectSuper) {
    // Check key and flags
    var key = method.key;
    if (method.kind === "constructor") {
      if (isGenerator) { this.raise(key.start, "Constructor can't be a generator"); }
      if (isAsync) { this.raise(key.start, "Constructor can't be an async method"); }
    } else if (method.static && checkKeyName(method, "prototype")) {
      this.raise(key.start, "Classes may not have a static property named prototype");
    }

    // Parse value
    var value = method.value = this.parseMethod(isGenerator, isAsync, allowsDirectSuper);

    // Check value
    if (method.kind === "get" && value.params.length !== 0)
      { this.raiseRecoverable(value.start, "getter should have no params"); }
    if (method.kind === "set" && value.params.length !== 1)
      { this.raiseRecoverable(value.start, "setter should have exactly one param"); }
    if (method.kind === "set" && value.params[0].type === "RestElement")
      { this.raiseRecoverable(value.params[0].start, "Setter cannot use rest params"); }

    return this.finishNode(method, "MethodDefinition")
  };

  pp$8.parseClassField = function(field) {
    if (checkKeyName(field, "constructor")) {
      this.raise(field.key.start, "Classes can't have a field named 'constructor'");
    } else if (field.static && checkKeyName(field, "prototype")) {
      this.raise(field.key.start, "Classes can't have a static field named 'prototype'");
    }

    if (this.eat(types$1.eq)) {
      // To raise SyntaxError if 'arguments' exists in the initializer.
      var scope = this.currentThisScope();
      var inClassFieldInit = scope.inClassFieldInit;
      scope.inClassFieldInit = true;
      field.value = this.parseMaybeAssign();
      scope.inClassFieldInit = inClassFieldInit;
    } else {
      field.value = null;
    }
    this.semicolon();

    return this.finishNode(field, "PropertyDefinition")
  };

  pp$8.parseClassStaticBlock = function(node) {
    node.body = [];

    var oldLabels = this.labels;
    this.labels = [];
    this.enterScope(SCOPE_CLASS_STATIC_BLOCK | SCOPE_SUPER);
    while (this.type !== types$1.braceR) {
      var stmt = this.parseStatement(null);
      node.body.push(stmt);
    }
    this.next();
    this.exitScope();
    this.labels = oldLabels;

    return this.finishNode(node, "StaticBlock")
  };

  pp$8.parseClassId = function(node, isStatement) {
    if (this.type === types$1.name) {
      node.id = this.parseIdent();
      if (isStatement)
        { this.checkLValSimple(node.id, BIND_LEXICAL, false); }
    } else {
      if (isStatement === true)
        { this.unexpected(); }
      node.id = null;
    }
  };

  pp$8.parseClassSuper = function(node) {
    node.superClass = this.eat(types$1._extends) ? this.parseExprSubscripts(null, false) : null;
  };

  pp$8.enterClassBody = function() {
    var element = {declared: Object.create(null), used: []};
    this.privateNameStack.push(element);
    return element.declared
  };

  pp$8.exitClassBody = function() {
    var ref = this.privateNameStack.pop();
    var declared = ref.declared;
    var used = ref.used;
    if (!this.options.checkPrivateFields) { return }
    var len = this.privateNameStack.length;
    var parent = len === 0 ? null : this.privateNameStack[len - 1];
    for (var i = 0; i < used.length; ++i) {
      var id = used[i];
      if (!hasOwn(declared, id.name)) {
        if (parent) {
          parent.used.push(id);
        } else {
          this.raiseRecoverable(id.start, ("Private field '#" + (id.name) + "' must be declared in an enclosing class"));
        }
      }
    }
  };

  function isPrivateNameConflicted(privateNameMap, element) {
    var name = element.key.name;
    var curr = privateNameMap[name];

    var next = "true";
    if (element.type === "MethodDefinition" && (element.kind === "get" || element.kind === "set")) {
      next = (element.static ? "s" : "i") + element.kind;
    }

    // `class { get #a(){}; static set #a(_){} }` is also conflict.
    if (
      curr === "iget" && next === "iset" ||
      curr === "iset" && next === "iget" ||
      curr === "sget" && next === "sset" ||
      curr === "sset" && next === "sget"
    ) {
      privateNameMap[name] = "true";
      return false
    } else if (!curr) {
      privateNameMap[name] = next;
      return false
    } else {
      return true
    }
  }

  function checkKeyName(node, name) {
    var computed = node.computed;
    var key = node.key;
    return !computed && (
      key.type === "Identifier" && key.name === name ||
      key.type === "Literal" && key.value === name
    )
  }

  // Parses module export declaration.

  pp$8.parseExportAllDeclaration = function(node, exports) {
    if (this.options.ecmaVersion >= 11) {
      if (this.eatContextual("as")) {
        node.exported = this.parseModuleExportName();
        this.checkExport(exports, node.exported, this.lastTokStart);
      } else {
        node.exported = null;
      }
    }
    this.expectContextual("from");
    if (this.type !== types$1.string) { this.unexpected(); }
    node.source = this.parseExprAtom();
    if (this.options.ecmaVersion >= 16)
      { node.attributes = this.parseWithClause(); }
    this.semicolon();
    return this.finishNode(node, "ExportAllDeclaration")
  };

  pp$8.parseExport = function(node, exports) {
    this.next();
    // export * from '...'
    if (this.eat(types$1.star)) {
      return this.parseExportAllDeclaration(node, exports)
    }
    if (this.eat(types$1._default)) { // export default ...
      this.checkExport(exports, "default", this.lastTokStart);
      node.declaration = this.parseExportDefaultDeclaration();
      return this.finishNode(node, "ExportDefaultDeclaration")
    }
    // export var|const|let|function|class ...
    if (this.shouldParseExportStatement()) {
      node.declaration = this.parseExportDeclaration(node);
      if (node.declaration.type === "VariableDeclaration")
        { this.checkVariableExport(exports, node.declaration.declarations); }
      else
        { this.checkExport(exports, node.declaration.id, node.declaration.id.start); }
      node.specifiers = [];
      node.source = null;
    } else { // export { x, y as z } [from '...']
      node.declaration = null;
      node.specifiers = this.parseExportSpecifiers(exports);
      if (this.eatContextual("from")) {
        if (this.type !== types$1.string) { this.unexpected(); }
        node.source = this.parseExprAtom();
        if (this.options.ecmaVersion >= 16)
          { node.attributes = this.parseWithClause(); }
      } else {
        for (var i = 0, list = node.specifiers; i < list.length; i += 1) {
          // check for keywords used as local names
          var spec = list[i];

          this.checkUnreserved(spec.local);
          // check if export is defined
          this.checkLocalExport(spec.local);

          if (spec.local.type === "Literal") {
            this.raise(spec.local.start, "A string literal cannot be used as an exported binding without `from`.");
          }
        }

        node.source = null;
      }
      this.semicolon();
    }
    return this.finishNode(node, "ExportNamedDeclaration")
  };

  pp$8.parseExportDeclaration = function(node) {
    return this.parseStatement(null)
  };

  pp$8.parseExportDefaultDeclaration = function() {
    var isAsync;
    if (this.type === types$1._function || (isAsync = this.isAsyncFunction())) {
      var fNode = this.startNode();
      this.next();
      if (isAsync) { this.next(); }
      return this.parseFunction(fNode, FUNC_STATEMENT | FUNC_NULLABLE_ID, false, isAsync)
    } else if (this.type === types$1._class) {
      var cNode = this.startNode();
      return this.parseClass(cNode, "nullableID")
    } else {
      var declaration = this.parseMaybeAssign();
      this.semicolon();
      return declaration
    }
  };

  pp$8.checkExport = function(exports, name, pos) {
    if (!exports) { return }
    if (typeof name !== "string")
      { name = name.type === "Identifier" ? name.name : name.value; }
    if (hasOwn(exports, name))
      { this.raiseRecoverable(pos, "Duplicate export '" + name + "'"); }
    exports[name] = true;
  };

  pp$8.checkPatternExport = function(exports, pat) {
    var type = pat.type;
    if (type === "Identifier")
      { this.checkExport(exports, pat, pat.start); }
    else if (type === "ObjectPattern")
      { for (var i = 0, list = pat.properties; i < list.length; i += 1)
        {
          var prop = list[i];

          this.checkPatternExport(exports, prop);
        } }
    else if (type === "ArrayPattern")
      { for (var i$1 = 0, list$1 = pat.elements; i$1 < list$1.length; i$1 += 1) {
        var elt = list$1[i$1];

          if (elt) { this.checkPatternExport(exports, elt); }
      } }
    else if (type === "Property")
      { this.checkPatternExport(exports, pat.value); }
    else if (type === "AssignmentPattern")
      { this.checkPatternExport(exports, pat.left); }
    else if (type === "RestElement")
      { this.checkPatternExport(exports, pat.argument); }
  };

  pp$8.checkVariableExport = function(exports, decls) {
    if (!exports) { return }
    for (var i = 0, list = decls; i < list.length; i += 1)
      {
      var decl = list[i];

      this.checkPatternExport(exports, decl.id);
    }
  };

  pp$8.shouldParseExportStatement = function() {
    return this.type.keyword === "var" ||
      this.type.keyword === "const" ||
      this.type.keyword === "class" ||
      this.type.keyword === "function" ||
      this.isLet() ||
      this.isAsyncFunction()
  };

  // Parses a comma-separated list of module exports.

  pp$8.parseExportSpecifier = function(exports) {
    var node = this.startNode();
    node.local = this.parseModuleExportName();

    node.exported = this.eatContextual("as") ? this.parseModuleExportName() : node.local;
    this.checkExport(
      exports,
      node.exported,
      node.exported.start
    );

    return this.finishNode(node, "ExportSpecifier")
  };

  pp$8.parseExportSpecifiers = function(exports) {
    var nodes = [], first = true;
    // export { x, y as z } [from '...']
    this.expect(types$1.braceL);
    while (!this.eat(types$1.braceR)) {
      if (!first) {
        this.expect(types$1.comma);
        if (this.afterTrailingComma(types$1.braceR)) { break }
      } else { first = false; }

      nodes.push(this.parseExportSpecifier(exports));
    }
    return nodes
  };

  // Parses import declaration.

  pp$8.parseImport = function(node) {
    this.next();

    // import '...'
    if (this.type === types$1.string) {
      node.specifiers = empty$1;
      node.source = this.parseExprAtom();
    } else {
      node.specifiers = this.parseImportSpecifiers();
      this.expectContextual("from");
      node.source = this.type === types$1.string ? this.parseExprAtom() : this.unexpected();
    }
    if (this.options.ecmaVersion >= 16)
      { node.attributes = this.parseWithClause(); }
    this.semicolon();
    return this.finishNode(node, "ImportDeclaration")
  };

  // Parses a comma-separated list of module imports.

  pp$8.parseImportSpecifier = function() {
    var node = this.startNode();
    node.imported = this.parseModuleExportName();

    if (this.eatContextual("as")) {
      node.local = this.parseIdent();
    } else {
      this.checkUnreserved(node.imported);
      node.local = node.imported;
    }
    this.checkLValSimple(node.local, BIND_LEXICAL);

    return this.finishNode(node, "ImportSpecifier")
  };

  pp$8.parseImportDefaultSpecifier = function() {
    // import defaultObj, { x, y as z } from '...'
    var node = this.startNode();
    node.local = this.parseIdent();
    this.checkLValSimple(node.local, BIND_LEXICAL);
    return this.finishNode(node, "ImportDefaultSpecifier")
  };

  pp$8.parseImportNamespaceSpecifier = function() {
    var node = this.startNode();
    this.next();
    this.expectContextual("as");
    node.local = this.parseIdent();
    this.checkLValSimple(node.local, BIND_LEXICAL);
    return this.finishNode(node, "ImportNamespaceSpecifier")
  };

  pp$8.parseImportSpecifiers = function() {
    var nodes = [], first = true;
    if (this.type === types$1.name) {
      nodes.push(this.parseImportDefaultSpecifier());
      if (!this.eat(types$1.comma)) { return nodes }
    }
    if (this.type === types$1.star) {
      nodes.push(this.parseImportNamespaceSpecifier());
      return nodes
    }
    this.expect(types$1.braceL);
    while (!this.eat(types$1.braceR)) {
      if (!first) {
        this.expect(types$1.comma);
        if (this.afterTrailingComma(types$1.braceR)) { break }
      } else { first = false; }

      nodes.push(this.parseImportSpecifier());
    }
    return nodes
  };

  pp$8.parseWithClause = function() {
    var nodes = [];
    if (!this.eat(types$1._with)) {
      return nodes
    }
    this.expect(types$1.braceL);
    var attributeKeys = {};
    var first = true;
    while (!this.eat(types$1.braceR)) {
      if (!first) {
        this.expect(types$1.comma);
        if (this.afterTrailingComma(types$1.braceR)) { break }
      } else { first = false; }

      var attr = this.parseImportAttribute();
      var keyName = attr.key.type === "Identifier" ? attr.key.name : attr.key.value;
      if (hasOwn(attributeKeys, keyName))
        { this.raiseRecoverable(attr.key.start, "Duplicate attribute key '" + keyName + "'"); }
      attributeKeys[keyName] = true;
      nodes.push(attr);
    }
    return nodes
  };

  pp$8.parseImportAttribute = function() {
    var node = this.startNode();
    node.key = this.type === types$1.string ? this.parseExprAtom() : this.parseIdent(this.options.allowReserved !== "never");
    this.expect(types$1.colon);
    if (this.type !== types$1.string) {
      this.unexpected();
    }
    node.value = this.parseExprAtom();
    return this.finishNode(node, "ImportAttribute")
  };

  pp$8.parseModuleExportName = function() {
    if (this.options.ecmaVersion >= 13 && this.type === types$1.string) {
      var stringLiteral = this.parseLiteral(this.value);
      if (loneSurrogate.test(stringLiteral.value)) {
        this.raise(stringLiteral.start, "An export name cannot include a lone surrogate.");
      }
      return stringLiteral
    }
    return this.parseIdent(true)
  };

  // Set `ExpressionStatement#directive` property for directive prologues.
  pp$8.adaptDirectivePrologue = function(statements) {
    for (var i = 0; i < statements.length && this.isDirectiveCandidate(statements[i]); ++i) {
      statements[i].directive = statements[i].expression.raw.slice(1, -1);
    }
  };
  pp$8.isDirectiveCandidate = function(statement) {
    return (
      this.options.ecmaVersion >= 5 &&
      statement.type === "ExpressionStatement" &&
      statement.expression.type === "Literal" &&
      typeof statement.expression.value === "string" &&
      // Reject parenthesized strings.
      (this.input[statement.start] === "\"" || this.input[statement.start] === "'")
    )
  };

  var pp$7 = Parser.prototype;

  // Convert existing expression atom to assignable pattern
  // if possible.

  pp$7.toAssignable = function(node, isBinding, refDestructuringErrors) {
    if (this.options.ecmaVersion >= 6 && node) {
      switch (node.type) {
      case "Identifier":
        if (this.inAsync && node.name === "await")
          { this.raise(node.start, "Cannot use 'await' as identifier inside an async function"); }
        break

      case "ObjectPattern":
      case "ArrayPattern":
      case "AssignmentPattern":
      case "RestElement":
        break

      case "ObjectExpression":
        node.type = "ObjectPattern";
        if (refDestructuringErrors) { this.checkPatternErrors(refDestructuringErrors, true); }
        for (var i = 0, list = node.properties; i < list.length; i += 1) {
          var prop = list[i];

        this.toAssignable(prop, isBinding);
          // Early error:
          //   AssignmentRestProperty[Yield, Await] :
          //     `...` DestructuringAssignmentTarget[Yield, Await]
          //
          //   It is a Syntax Error if |DestructuringAssignmentTarget| is an |ArrayLiteral| or an |ObjectLiteral|.
          if (
            prop.type === "RestElement" &&
            (prop.argument.type === "ArrayPattern" || prop.argument.type === "ObjectPattern")
          ) {
            this.raise(prop.argument.start, "Unexpected token");
          }
        }
        break

      case "Property":
        // AssignmentProperty has type === "Property"
        if (node.kind !== "init") { this.raise(node.key.start, "Object pattern can't contain getter or setter"); }
        this.toAssignable(node.value, isBinding);
        break

      case "ArrayExpression":
        node.type = "ArrayPattern";
        if (refDestructuringErrors) { this.checkPatternErrors(refDestructuringErrors, true); }
        this.toAssignableList(node.elements, isBinding);
        break

      case "SpreadElement":
        node.type = "RestElement";
        this.toAssignable(node.argument, isBinding);
        if (node.argument.type === "AssignmentPattern")
          { this.raise(node.argument.start, "Rest elements cannot have a default value"); }
        break

      case "AssignmentExpression":
        if (node.operator !== "=") { this.raise(node.left.end, "Only '=' operator can be used for specifying default value."); }
        node.type = "AssignmentPattern";
        delete node.operator;
        this.toAssignable(node.left, isBinding);
        break

      case "ParenthesizedExpression":
        this.toAssignable(node.expression, isBinding, refDestructuringErrors);
        break

      case "ChainExpression":
        this.raiseRecoverable(node.start, "Optional chaining cannot appear in left-hand side");
        break

      case "MemberExpression":
        if (!isBinding) { break }

      default:
        this.raise(node.start, "Assigning to rvalue");
      }
    } else if (refDestructuringErrors) { this.checkPatternErrors(refDestructuringErrors, true); }
    return node
  };

  // Convert list of expression atoms to binding list.

  pp$7.toAssignableList = function(exprList, isBinding) {
    var end = exprList.length;
    for (var i = 0; i < end; i++) {
      var elt = exprList[i];
      if (elt) { this.toAssignable(elt, isBinding); }
    }
    if (end) {
      var last = exprList[end - 1];
      if (this.options.ecmaVersion === 6 && isBinding && last && last.type === "RestElement" && last.argument.type !== "Identifier")
        { this.unexpected(last.argument.start); }
    }
    return exprList
  };

  // Parses spread element.

  pp$7.parseSpread = function(refDestructuringErrors) {
    var node = this.startNode();
    this.next();
    node.argument = this.parseMaybeAssign(false, refDestructuringErrors);
    return this.finishNode(node, "SpreadElement")
  };

  pp$7.parseRestBinding = function() {
    var node = this.startNode();
    this.next();

    // RestElement inside of a function parameter must be an identifier
    if (this.options.ecmaVersion === 6 && this.type !== types$1.name)
      { this.unexpected(); }

    node.argument = this.parseBindingAtom();

    return this.finishNode(node, "RestElement")
  };

  // Parses lvalue (assignable) atom.

  pp$7.parseBindingAtom = function() {
    if (this.options.ecmaVersion >= 6) {
      switch (this.type) {
      case types$1.bracketL:
        var node = this.startNode();
        this.next();
        node.elements = this.parseBindingList(types$1.bracketR, true, true);
        return this.finishNode(node, "ArrayPattern")

      case types$1.braceL:
        return this.parseObj(true)
      }
    }
    return this.parseIdent()
  };

  pp$7.parseBindingList = function(close, allowEmpty, allowTrailingComma, allowModifiers) {
    var elts = [], first = true;
    while (!this.eat(close)) {
      if (first) { first = false; }
      else { this.expect(types$1.comma); }
      if (allowEmpty && this.type === types$1.comma) {
        elts.push(null);
      } else if (allowTrailingComma && this.afterTrailingComma(close)) {
        break
      } else if (this.type === types$1.ellipsis) {
        var rest = this.parseRestBinding();
        this.parseBindingListItem(rest);
        elts.push(rest);
        if (this.type === types$1.comma) { this.raiseRecoverable(this.start, "Comma is not permitted after the rest element"); }
        this.expect(close);
        break
      } else {
        elts.push(this.parseAssignableListItem(allowModifiers));
      }
    }
    return elts
  };

  pp$7.parseAssignableListItem = function(allowModifiers) {
    var elem = this.parseMaybeDefault(this.start, this.startLoc);
    this.parseBindingListItem(elem);
    return elem
  };

  pp$7.parseBindingListItem = function(param) {
    return param
  };

  // Parses assignment pattern around given atom if possible.

  pp$7.parseMaybeDefault = function(startPos, startLoc, left) {
    left = left || this.parseBindingAtom();
    if (this.options.ecmaVersion < 6 || !this.eat(types$1.eq)) { return left }
    var node = this.startNodeAt(startPos, startLoc);
    node.left = left;
    node.right = this.parseMaybeAssign();
    return this.finishNode(node, "AssignmentPattern")
  };

  // The following three functions all verify that a node is an lvalue —
  // something that can be bound, or assigned to. In order to do so, they perform
  // a variety of checks:
  //
  // - Check that none of the bound/assigned-to identifiers are reserved words.
  // - Record name declarations for bindings in the appropriate scope.
  // - Check duplicate argument names, if checkClashes is set.
  //
  // If a complex binding pattern is encountered (e.g., object and array
  // destructuring), the entire pattern is recursively checked.
  //
  // There are three versions of checkLVal*() appropriate for different
  // circumstances:
  //
  // - checkLValSimple() shall be used if the syntactic construct supports
  //   nothing other than identifiers and member expressions. Parenthesized
  //   expressions are also correctly handled. This is generally appropriate for
  //   constructs for which the spec says
  //
  //   > It is a Syntax Error if AssignmentTargetType of [the production] is not
  //   > simple.
  //
  //   It is also appropriate for checking if an identifier is valid and not
  //   defined elsewhere, like import declarations or function/class identifiers.
  //
  //   Examples where this is used include:
  //     a += …;
  //     import a from '…';
  //   where a is the node to be checked.
  //
  // - checkLValPattern() shall be used if the syntactic construct supports
  //   anything checkLValSimple() supports, as well as object and array
  //   destructuring patterns. This is generally appropriate for constructs for
  //   which the spec says
  //
  //   > It is a Syntax Error if [the production] is neither an ObjectLiteral nor
  //   > an ArrayLiteral and AssignmentTargetType of [the production] is not
  //   > simple.
  //
  //   Examples where this is used include:
  //     (a = …);
  //     const a = …;
  //     try { … } catch (a) { … }
  //   where a is the node to be checked.
  //
  // - checkLValInnerPattern() shall be used if the syntactic construct supports
  //   anything checkLValPattern() supports, as well as default assignment
  //   patterns, rest elements, and other constructs that may appear within an
  //   object or array destructuring pattern.
  //
  //   As a special case, function parameters also use checkLValInnerPattern(),
  //   as they also support defaults and rest constructs.
  //
  // These functions deliberately support both assignment and binding constructs,
  // as the logic for both is exceedingly similar. If the node is the target of
  // an assignment, then bindingType should be set to BIND_NONE. Otherwise, it
  // should be set to the appropriate BIND_* constant, like BIND_VAR or
  // BIND_LEXICAL.
  //
  // If the function is called with a non-BIND_NONE bindingType, then
  // additionally a checkClashes object may be specified to allow checking for
  // duplicate argument names. checkClashes is ignored if the provided construct
  // is an assignment (i.e., bindingType is BIND_NONE).

  pp$7.checkLValSimple = function(expr, bindingType, checkClashes) {
    if ( bindingType === void 0 ) bindingType = BIND_NONE;

    var isBind = bindingType !== BIND_NONE;

    switch (expr.type) {
    case "Identifier":
      if (this.strict && this.reservedWordsStrictBind.test(expr.name))
        { this.raiseRecoverable(expr.start, (isBind ? "Binding " : "Assigning to ") + expr.name + " in strict mode"); }
      if (isBind) {
        if (bindingType === BIND_LEXICAL && expr.name === "let")
          { this.raiseRecoverable(expr.start, "let is disallowed as a lexically bound name"); }
        if (checkClashes) {
          if (hasOwn(checkClashes, expr.name))
            { this.raiseRecoverable(expr.start, "Argument name clash"); }
          checkClashes[expr.name] = true;
        }
        if (bindingType !== BIND_OUTSIDE) { this.declareName(expr.name, bindingType, expr.start); }
      }
      break

    case "ChainExpression":
      this.raiseRecoverable(expr.start, "Optional chaining cannot appear in left-hand side");
      break

    case "MemberExpression":
      if (isBind) { this.raiseRecoverable(expr.start, "Binding member expression"); }
      break

    case "ParenthesizedExpression":
      if (isBind) { this.raiseRecoverable(expr.start, "Binding parenthesized expression"); }
      return this.checkLValSimple(expr.expression, bindingType, checkClashes)

    default:
      this.raise(expr.start, (isBind ? "Binding" : "Assigning to") + " rvalue");
    }
  };

  pp$7.checkLValPattern = function(expr, bindingType, checkClashes) {
    if ( bindingType === void 0 ) bindingType = BIND_NONE;

    switch (expr.type) {
    case "ObjectPattern":
      for (var i = 0, list = expr.properties; i < list.length; i += 1) {
        var prop = list[i];

      this.checkLValInnerPattern(prop, bindingType, checkClashes);
      }
      break

    case "ArrayPattern":
      for (var i$1 = 0, list$1 = expr.elements; i$1 < list$1.length; i$1 += 1) {
        var elem = list$1[i$1];

      if (elem) { this.checkLValInnerPattern(elem, bindingType, checkClashes); }
      }
      break

    default:
      this.checkLValSimple(expr, bindingType, checkClashes);
    }
  };

  pp$7.checkLValInnerPattern = function(expr, bindingType, checkClashes) {
    if ( bindingType === void 0 ) bindingType = BIND_NONE;

    switch (expr.type) {
    case "Property":
      // AssignmentProperty has type === "Property"
      this.checkLValInnerPattern(expr.value, bindingType, checkClashes);
      break

    case "AssignmentPattern":
      this.checkLValPattern(expr.left, bindingType, checkClashes);
      break

    case "RestElement":
      this.checkLValPattern(expr.argument, bindingType, checkClashes);
      break

    default:
      this.checkLValPattern(expr, bindingType, checkClashes);
    }
  };

  // The algorithm used to determine whether a regexp can appear at a
  // given point in the program is loosely based on sweet.js' approach.
  // See https://github.com/mozilla/sweet.js/wiki/design


  var TokContext = function TokContext(token, isExpr, preserveSpace, override, generator) {
    this.token = token;
    this.isExpr = !!isExpr;
    this.preserveSpace = !!preserveSpace;
    this.override = override;
    this.generator = !!generator;
  };

  var types = {
    b_stat: new TokContext("{", false),
    b_expr: new TokContext("{", true),
    b_tmpl: new TokContext("${", false),
    p_stat: new TokContext("(", false),
    p_expr: new TokContext("(", true),
    q_tmpl: new TokContext("`", true, true, function (p) { return p.tryReadTemplateToken(); }),
    f_stat: new TokContext("function", false),
    f_expr: new TokContext("function", true),
    f_expr_gen: new TokContext("function", true, false, null, true),
    f_gen: new TokContext("function", false, false, null, true)
  };

  var pp$6 = Parser.prototype;

  pp$6.initialContext = function() {
    return [types.b_stat]
  };

  pp$6.curContext = function() {
    return this.context[this.context.length - 1]
  };

  pp$6.braceIsBlock = function(prevType) {
    var parent = this.curContext();
    if (parent === types.f_expr || parent === types.f_stat)
      { return true }
    if (prevType === types$1.colon && (parent === types.b_stat || parent === types.b_expr))
      { return !parent.isExpr }

    // The check for `tt.name && exprAllowed` detects whether we are
    // after a `yield` or `of` construct. See the `updateContext` for
    // `tt.name`.
    if (prevType === types$1._return || prevType === types$1.name && this.exprAllowed)
      { return lineBreak.test(this.input.slice(this.lastTokEnd, this.start)) }
    if (prevType === types$1._else || prevType === types$1.semi || prevType === types$1.eof || prevType === types$1.parenR || prevType === types$1.arrow)
      { return true }
    if (prevType === types$1.braceL)
      { return parent === types.b_stat }
    if (prevType === types$1._var || prevType === types$1._const || prevType === types$1.name)
      { return false }
    return !this.exprAllowed
  };

  pp$6.inGeneratorContext = function() {
    for (var i = this.context.length - 1; i >= 1; i--) {
      var context = this.context[i];
      if (context.token === "function")
        { return context.generator }
    }
    return false
  };

  pp$6.updateContext = function(prevType) {
    var update, type = this.type;
    if (type.keyword && prevType === types$1.dot)
      { this.exprAllowed = false; }
    else if (update = type.updateContext)
      { update.call(this, prevType); }
    else
      { this.exprAllowed = type.beforeExpr; }
  };

  // Used to handle edge cases when token context could not be inferred correctly during tokenization phase

  pp$6.overrideContext = function(tokenCtx) {
    if (this.curContext() !== tokenCtx) {
      this.context[this.context.length - 1] = tokenCtx;
    }
  };

  // Token-specific context update code

  types$1.parenR.updateContext = types$1.braceR.updateContext = function() {
    if (this.context.length === 1) {
      this.exprAllowed = true;
      return
    }
    var out = this.context.pop();
    if (out === types.b_stat && this.curContext().token === "function") {
      out = this.context.pop();
    }
    this.exprAllowed = !out.isExpr;
  };

  types$1.braceL.updateContext = function(prevType) {
    this.context.push(this.braceIsBlock(prevType) ? types.b_stat : types.b_expr);
    this.exprAllowed = true;
  };

  types$1.dollarBraceL.updateContext = function() {
    this.context.push(types.b_tmpl);
    this.exprAllowed = true;
  };

  types$1.parenL.updateContext = function(prevType) {
    var statementParens = prevType === types$1._if || prevType === types$1._for || prevType === types$1._with || prevType === types$1._while;
    this.context.push(statementParens ? types.p_stat : types.p_expr);
    this.exprAllowed = true;
  };

  types$1.incDec.updateContext = function() {
    // tokExprAllowed stays unchanged
  };

  types$1._function.updateContext = types$1._class.updateContext = function(prevType) {
    if (prevType.beforeExpr && prevType !== types$1._else &&
        !(prevType === types$1.semi && this.curContext() !== types.p_stat) &&
        !(prevType === types$1._return && lineBreak.test(this.input.slice(this.lastTokEnd, this.start))) &&
        !((prevType === types$1.colon || prevType === types$1.braceL) && this.curContext() === types.b_stat))
      { this.context.push(types.f_expr); }
    else
      { this.context.push(types.f_stat); }
    this.exprAllowed = false;
  };

  types$1.colon.updateContext = function() {
    if (this.curContext().token === "function") { this.context.pop(); }
    this.exprAllowed = true;
  };

  types$1.backQuote.updateContext = function() {
    if (this.curContext() === types.q_tmpl)
      { this.context.pop(); }
    else
      { this.context.push(types.q_tmpl); }
    this.exprAllowed = false;
  };

  types$1.star.updateContext = function(prevType) {
    if (prevType === types$1._function) {
      var index = this.context.length - 1;
      if (this.context[index] === types.f_expr)
        { this.context[index] = types.f_expr_gen; }
      else
        { this.context[index] = types.f_gen; }
    }
    this.exprAllowed = true;
  };

  types$1.name.updateContext = function(prevType) {
    var allowed = false;
    if (this.options.ecmaVersion >= 6 && prevType !== types$1.dot) {
      if (this.value === "of" && !this.exprAllowed ||
          this.value === "yield" && this.inGeneratorContext())
        { allowed = true; }
    }
    this.exprAllowed = allowed;
  };

  // A recursive descent parser operates by defining functions for all
  // syntactic elements, and recursively calling those, each function
  // advancing the input stream and returning an AST node. Precedence
  // of constructs (for example, the fact that `!x[1]` means `!(x[1])`
  // instead of `(!x)[1]` is handled by the fact that the parser
  // function that parses unary prefix operators is called first, and
  // in turn calls the function that parses `[]` subscripts — that
  // way, it'll receive the node for `x[1]` already parsed, and wraps
  // *that* in the unary operator node.
  //
  // Acorn uses an [operator precedence parser][opp] to handle binary
  // operator precedence, because it is much more compact than using
  // the technique outlined above, which uses different, nesting
  // functions to specify precedence, for all of the ten binary
  // precedence levels that JavaScript defines.
  //
  // [opp]: http://en.wikipedia.org/wiki/Operator-precedence_parser


  var pp$5 = Parser.prototype;

  // Check if property name clashes with already added.
  // Object/class getters and setters are not allowed to clash —
  // either with each other or with an init property — and in
  // strict mode, init properties are also not allowed to be repeated.

  pp$5.checkPropClash = function(prop, propHash, refDestructuringErrors) {
    if (this.options.ecmaVersion >= 9 && prop.type === "SpreadElement")
      { return }
    if (this.options.ecmaVersion >= 6 && (prop.computed || prop.method || prop.shorthand))
      { return }
    var key = prop.key;
    var name;
    switch (key.type) {
    case "Identifier": name = key.name; break
    case "Literal": name = String(key.value); break
    default: return
    }
    var kind = prop.kind;
    if (this.options.ecmaVersion >= 6) {
      if (name === "__proto__" && kind === "init") {
        if (propHash.proto) {
          if (refDestructuringErrors) {
            if (refDestructuringErrors.doubleProto < 0) {
              refDestructuringErrors.doubleProto = key.start;
            }
          } else {
            this.raiseRecoverable(key.start, "Redefinition of __proto__ property");
          }
        }
        propHash.proto = true;
      }
      return
    }
    name = "$" + name;
    var other = propHash[name];
    if (other) {
      var redefinition;
      if (kind === "init") {
        redefinition = this.strict && other.init || other.get || other.set;
      } else {
        redefinition = other.init || other[kind];
      }
      if (redefinition)
        { this.raiseRecoverable(key.start, "Redefinition of property"); }
    } else {
      other = propHash[name] = {
        init: false,
        get: false,
        set: false
      };
    }
    other[kind] = true;
  };

  // ### Expression parsing

  // These nest, from the most general expression type at the top to
  // 'atomic', nondivisible expression types at the bottom. Most of
  // the functions will simply let the function(s) below them parse,
  // and, *if* the syntactic construct they handle is present, wrap
  // the AST node that the inner parser gave them in another node.

  // Parse a full expression. The optional arguments are used to
  // forbid the `in` operator (in for loops initalization expressions)
  // and provide reference for storing '=' operator inside shorthand
  // property assignment in contexts where both object expression
  // and object pattern might appear (so it's possible to raise
  // delayed syntax error at correct position).

  pp$5.parseExpression = function(forInit, refDestructuringErrors) {
    var startPos = this.start, startLoc = this.startLoc;
    var expr = this.parseMaybeAssign(forInit, refDestructuringErrors);
    if (this.type === types$1.comma) {
      var node = this.startNodeAt(startPos, startLoc);
      node.expressions = [expr];
      while (this.eat(types$1.comma)) { node.expressions.push(this.parseMaybeAssign(forInit, refDestructuringErrors)); }
      return this.finishNode(node, "SequenceExpression")
    }
    return expr
  };

  // Parse an assignment expression. This includes applications of
  // operators like `+=`.

  pp$5.parseMaybeAssign = function(forInit, refDestructuringErrors, afterLeftParse) {
    if (this.isContextual("yield")) {
      if (this.inGenerator) { return this.parseYield(forInit) }
      // The tokenizer will assume an expression is allowed after
      // `yield`, but this isn't that kind of yield
      else { this.exprAllowed = false; }
    }

    var ownDestructuringErrors = false, oldParenAssign = -1, oldTrailingComma = -1, oldDoubleProto = -1;
    if (refDestructuringErrors) {
      oldParenAssign = refDestructuringErrors.parenthesizedAssign;
      oldTrailingComma = refDestructuringErrors.trailingComma;
      oldDoubleProto = refDestructuringErrors.doubleProto;
      refDestructuringErrors.parenthesizedAssign = refDestructuringErrors.trailingComma = -1;
    } else {
      refDestructuringErrors = new DestructuringErrors;
      ownDestructuringErrors = true;
    }

    var startPos = this.start, startLoc = this.startLoc;
    if (this.type === types$1.parenL || this.type === types$1.name) {
      this.potentialArrowAt = this.start;
      this.potentialArrowInForAwait = forInit === "await";
    }
    var left = this.parseMaybeConditional(forInit, refDestructuringErrors);
    if (afterLeftParse) { left = afterLeftParse.call(this, left, startPos, startLoc); }
    if (this.type.isAssign) {
      var node = this.startNodeAt(startPos, startLoc);
      node.operator = this.value;
      if (this.type === types$1.eq)
        { left = this.toAssignable(left, false, refDestructuringErrors); }
      if (!ownDestructuringErrors) {
        refDestructuringErrors.parenthesizedAssign = refDestructuringErrors.trailingComma = refDestructuringErrors.doubleProto = -1;
      }
      if (refDestructuringErrors.shorthandAssign >= left.start)
        { refDestructuringErrors.shorthandAssign = -1; } // reset because shorthand default was used correctly
      if (this.type === types$1.eq)
        { this.checkLValPattern(left); }
      else
        { this.checkLValSimple(left); }
      node.left = left;
      this.next();
      node.right = this.parseMaybeAssign(forInit);
      if (oldDoubleProto > -1) { refDestructuringErrors.doubleProto = oldDoubleProto; }
      return this.finishNode(node, "AssignmentExpression")
    } else {
      if (ownDestructuringErrors) { this.checkExpressionErrors(refDestructuringErrors, true); }
    }
    if (oldParenAssign > -1) { refDestructuringErrors.parenthesizedAssign = oldParenAssign; }
    if (oldTrailingComma > -1) { refDestructuringErrors.trailingComma = oldTrailingComma; }
    return left
  };

  // Parse a ternary conditional (`?:`) operator.

  pp$5.parseMaybeConditional = function(forInit, refDestructuringErrors) {
    var startPos = this.start, startLoc = this.startLoc;
    var expr = this.parseExprOps(forInit, refDestructuringErrors);
    if (this.checkExpressionErrors(refDestructuringErrors)) { return expr }
    if (this.eat(types$1.question)) {
      var node = this.startNodeAt(startPos, startLoc);
      node.test = expr;
      node.consequent = this.parseMaybeAssign();
      this.expect(types$1.colon);
      node.alternate = this.parseMaybeAssign(forInit);
      return this.finishNode(node, "ConditionalExpression")
    }
    return expr
  };

  // Start the precedence parser.

  pp$5.parseExprOps = function(forInit, refDestructuringErrors) {
    var startPos = this.start, startLoc = this.startLoc;
    var expr = this.parseMaybeUnary(refDestructuringErrors, false, false, forInit);
    if (this.checkExpressionErrors(refDestructuringErrors)) { return expr }
    return expr.start === startPos && expr.type === "ArrowFunctionExpression" ? expr : this.parseExprOp(expr, startPos, startLoc, -1, forInit)
  };

  // Parse binary operators with the operator precedence parsing
  // algorithm. `left` is the left-hand side of the operator.
  // `minPrec` provides context that allows the function to stop and
  // defer further parser to one of its callers when it encounters an
  // operator that has a lower precedence than the set it is parsing.

  pp$5.parseExprOp = function(left, leftStartPos, leftStartLoc, minPrec, forInit) {
    var prec = this.type.binop;
    if (prec != null && (!forInit || this.type !== types$1._in)) {
      if (prec > minPrec) {
        var logical = this.type === types$1.logicalOR || this.type === types$1.logicalAND;
        var coalesce = this.type === types$1.coalesce;
        if (coalesce) {
          // Handle the precedence of `tt.coalesce` as equal to the range of logical expressions.
          // In other words, `node.right` shouldn't contain logical expressions in order to check the mixed error.
          prec = types$1.logicalAND.binop;
        }
        var op = this.value;
        this.next();
        var startPos = this.start, startLoc = this.startLoc;
        var right = this.parseExprOp(this.parseMaybeUnary(null, false, false, forInit), startPos, startLoc, prec, forInit);
        var node = this.buildBinary(leftStartPos, leftStartLoc, left, right, op, logical || coalesce);
        if ((logical && this.type === types$1.coalesce) || (coalesce && (this.type === types$1.logicalOR || this.type === types$1.logicalAND))) {
          this.raiseRecoverable(this.start, "Logical expressions and coalesce expressions cannot be mixed. Wrap either by parentheses");
        }
        return this.parseExprOp(node, leftStartPos, leftStartLoc, minPrec, forInit)
      }
    }
    return left
  };

  pp$5.buildBinary = function(startPos, startLoc, left, right, op, logical) {
    if (right.type === "PrivateIdentifier") { this.raise(right.start, "Private identifier can only be left side of binary expression"); }
    var node = this.startNodeAt(startPos, startLoc);
    node.left = left;
    node.operator = op;
    node.right = right;
    return this.finishNode(node, logical ? "LogicalExpression" : "BinaryExpression")
  };

  // Parse unary operators, both prefix and postfix.

  pp$5.parseMaybeUnary = function(refDestructuringErrors, sawUnary, incDec, forInit) {
    var startPos = this.start, startLoc = this.startLoc, expr;
    if (this.isContextual("await") && this.canAwait) {
      expr = this.parseAwait(forInit);
      sawUnary = true;
    } else if (this.type.prefix) {
      var node = this.startNode(), update = this.type === types$1.incDec;
      node.operator = this.value;
      node.prefix = true;
      this.next();
      node.argument = this.parseMaybeUnary(null, true, update, forInit);
      this.checkExpressionErrors(refDestructuringErrors, true);
      if (update) { this.checkLValSimple(node.argument); }
      else if (this.strict && node.operator === "delete" && isLocalVariableAccess(node.argument))
        { this.raiseRecoverable(node.start, "Deleting local variable in strict mode"); }
      else if (node.operator === "delete" && isPrivateFieldAccess(node.argument))
        { this.raiseRecoverable(node.start, "Private fields can not be deleted"); }
      else { sawUnary = true; }
      expr = this.finishNode(node, update ? "UpdateExpression" : "UnaryExpression");
    } else if (!sawUnary && this.type === types$1.privateId) {
      if ((forInit || this.privateNameStack.length === 0) && this.options.checkPrivateFields) { this.unexpected(); }
      expr = this.parsePrivateIdent();
      // only could be private fields in 'in', such as #x in obj
      if (this.type !== types$1._in) { this.unexpected(); }
    } else {
      expr = this.parseExprSubscripts(refDestructuringErrors, forInit);
      if (this.checkExpressionErrors(refDestructuringErrors)) { return expr }
      while (this.type.postfix && !this.canInsertSemicolon()) {
        var node$1 = this.startNodeAt(startPos, startLoc);
        node$1.operator = this.value;
        node$1.prefix = false;
        node$1.argument = expr;
        this.checkLValSimple(expr);
        this.next();
        expr = this.finishNode(node$1, "UpdateExpression");
      }
    }

    if (!incDec && this.eat(types$1.starstar)) {
      if (sawUnary)
        { this.unexpected(this.lastTokStart); }
      else
        { return this.buildBinary(startPos, startLoc, expr, this.parseMaybeUnary(null, false, false, forInit), "**", false) }
    } else {
      return expr
    }
  };

  function isLocalVariableAccess(node) {
    return (
      node.type === "Identifier" ||
      node.type === "ParenthesizedExpression" && isLocalVariableAccess(node.expression)
    )
  }

  function isPrivateFieldAccess(node) {
    return (
      node.type === "MemberExpression" && node.property.type === "PrivateIdentifier" ||
      node.type === "ChainExpression" && isPrivateFieldAccess(node.expression) ||
      node.type === "ParenthesizedExpression" && isPrivateFieldAccess(node.expression)
    )
  }

  // Parse call, dot, and `[]`-subscript expressions.

  pp$5.parseExprSubscripts = function(refDestructuringErrors, forInit) {
    var startPos = this.start, startLoc = this.startLoc;
    var expr = this.parseExprAtom(refDestructuringErrors, forInit);
    if (expr.type === "ArrowFunctionExpression" && this.input.slice(this.lastTokStart, this.lastTokEnd) !== ")")
      { return expr }
    var result = this.parseSubscripts(expr, startPos, startLoc, false, forInit);
    if (refDestructuringErrors && result.type === "MemberExpression") {
      if (refDestructuringErrors.parenthesizedAssign >= result.start) { refDestructuringErrors.parenthesizedAssign = -1; }
      if (refDestructuringErrors.parenthesizedBind >= result.start) { refDestructuringErrors.parenthesizedBind = -1; }
      if (refDestructuringErrors.trailingComma >= result.start) { refDestructuringErrors.trailingComma = -1; }
    }
    return result
  };

  pp$5.parseSubscripts = function(base, startPos, startLoc, noCalls, forInit) {
    var maybeAsyncArrow = this.options.ecmaVersion >= 8 && base.type === "Identifier" && base.name === "async" &&
        this.lastTokEnd === base.end && !this.canInsertSemicolon() && base.end - base.start === 5 &&
        this.potentialArrowAt === base.start;
    var optionalChained = false;

    while (true) {
      var element = this.parseSubscript(base, startPos, startLoc, noCalls, maybeAsyncArrow, optionalChained, forInit);

      if (element.optional) { optionalChained = true; }
      if (element === base || element.type === "ArrowFunctionExpression") {
        if (optionalChained) {
          var chainNode = this.startNodeAt(startPos, startLoc);
          chainNode.expression = element;
          element = this.finishNode(chainNode, "ChainExpression");
        }
        return element
      }

      base = element;
    }
  };

  pp$5.shouldParseAsyncArrow = function() {
    return !this.canInsertSemicolon() && this.eat(types$1.arrow)
  };

  pp$5.parseSubscriptAsyncArrow = function(startPos, startLoc, exprList, forInit) {
    return this.parseArrowExpression(this.startNodeAt(startPos, startLoc), exprList, true, forInit)
  };

  pp$5.parseSubscript = function(base, startPos, startLoc, noCalls, maybeAsyncArrow, optionalChained, forInit) {
    var optionalSupported = this.options.ecmaVersion >= 11;
    var optional = optionalSupported && this.eat(types$1.questionDot);
    if (noCalls && optional) { this.raise(this.lastTokStart, "Optional chaining cannot appear in the callee of new expressions"); }

    var computed = this.eat(types$1.bracketL);
    if (computed || (optional && this.type !== types$1.parenL && this.type !== types$1.backQuote) || this.eat(types$1.dot)) {
      var node = this.startNodeAt(startPos, startLoc);
      node.object = base;
      if (computed) {
        node.property = this.parseExpression();
        this.expect(types$1.bracketR);
      } else if (this.type === types$1.privateId && base.type !== "Super") {
        node.property = this.parsePrivateIdent();
      } else {
        node.property = this.parseIdent(this.options.allowReserved !== "never");
      }
      node.computed = !!computed;
      if (optionalSupported) {
        node.optional = optional;
      }
      base = this.finishNode(node, "MemberExpression");
    } else if (!noCalls && this.eat(types$1.parenL)) {
      var refDestructuringErrors = new DestructuringErrors, oldYieldPos = this.yieldPos, oldAwaitPos = this.awaitPos, oldAwaitIdentPos = this.awaitIdentPos;
      this.yieldPos = 0;
      this.awaitPos = 0;
      this.awaitIdentPos = 0;
      var exprList = this.parseExprList(types$1.parenR, this.options.ecmaVersion >= 8, false, refDestructuringErrors);
      if (maybeAsyncArrow && !optional && this.shouldParseAsyncArrow()) {
        this.checkPatternErrors(refDestructuringErrors, false);
        this.checkYieldAwaitInDefaultParams();
        if (this.awaitIdentPos > 0)
          { this.raise(this.awaitIdentPos, "Cannot use 'await' as identifier inside an async function"); }
        this.yieldPos = oldYieldPos;
        this.awaitPos = oldAwaitPos;
        this.awaitIdentPos = oldAwaitIdentPos;
        return this.parseSubscriptAsyncArrow(startPos, startLoc, exprList, forInit)
      }
      this.checkExpressionErrors(refDestructuringErrors, true);
      this.yieldPos = oldYieldPos || this.yieldPos;
      this.awaitPos = oldAwaitPos || this.awaitPos;
      this.awaitIdentPos = oldAwaitIdentPos || this.awaitIdentPos;
      var node$1 = this.startNodeAt(startPos, startLoc);
      node$1.callee = base;
      node$1.arguments = exprList;
      if (optionalSupported) {
        node$1.optional = optional;
      }
      base = this.finishNode(node$1, "CallExpression");
    } else if (this.type === types$1.backQuote) {
      if (optional || optionalChained) {
        this.raise(this.start, "Optional chaining cannot appear in the tag of tagged template expressions");
      }
      var node$2 = this.startNodeAt(startPos, startLoc);
      node$2.tag = base;
      node$2.quasi = this.parseTemplate({isTagged: true});
      base = this.finishNode(node$2, "TaggedTemplateExpression");
    }
    return base
  };

  // Parse an atomic expression — either a single token that is an
  // expression, an expression started by a keyword like `function` or
  // `new`, or an expression wrapped in punctuation like `()`, `[]`,
  // or `{}`.

  pp$5.parseExprAtom = function(refDestructuringErrors, forInit, forNew) {
    // If a division operator appears in an expression position, the
    // tokenizer got confused, and we force it to read a regexp instead.
    if (this.type === types$1.slash) { this.readRegexp(); }

    var node, canBeArrow = this.potentialArrowAt === this.start;
    switch (this.type) {
    case types$1._super:
      if (!this.allowSuper)
        { this.raise(this.start, "'super' keyword outside a method"); }
      node = this.startNode();
      this.next();
      if (this.type === types$1.parenL && !this.allowDirectSuper)
        { this.raise(node.start, "super() call outside constructor of a subclass"); }
      // The `super` keyword can appear at below:
      // SuperProperty:
      //     super [ Expression ]
      //     super . IdentifierName
      // SuperCall:
      //     super ( Arguments )
      if (this.type !== types$1.dot && this.type !== types$1.bracketL && this.type !== types$1.parenL)
        { this.unexpected(); }
      return this.finishNode(node, "Super")

    case types$1._this:
      node = this.startNode();
      this.next();
      return this.finishNode(node, "ThisExpression")

    case types$1.name:
      var startPos = this.start, startLoc = this.startLoc, containsEsc = this.containsEsc;
      var id = this.parseIdent(false);
      if (this.options.ecmaVersion >= 8 && !containsEsc && id.name === "async" && !this.canInsertSemicolon() && this.eat(types$1._function)) {
        this.overrideContext(types.f_expr);
        return this.parseFunction(this.startNodeAt(startPos, startLoc), 0, false, true, forInit)
      }
      if (canBeArrow && !this.canInsertSemicolon()) {
        if (this.eat(types$1.arrow))
          { return this.parseArrowExpression(this.startNodeAt(startPos, startLoc), [id], false, forInit) }
        if (this.options.ecmaVersion >= 8 && id.name === "async" && this.type === types$1.name && !containsEsc &&
            (!this.potentialArrowInForAwait || this.value !== "of" || this.containsEsc)) {
          id = this.parseIdent(false);
          if (this.canInsertSemicolon() || !this.eat(types$1.arrow))
            { this.unexpected(); }
          return this.parseArrowExpression(this.startNodeAt(startPos, startLoc), [id], true, forInit)
        }
      }
      return id

    case types$1.regexp:
      var value = this.value;
      node = this.parseLiteral(value.value);
      node.regex = {pattern: value.pattern, flags: value.flags};
      return node

    case types$1.num: case types$1.string:
      return this.parseLiteral(this.value)

    case types$1._null: case types$1._true: case types$1._false:
      node = this.startNode();
      node.value = this.type === types$1._null ? null : this.type === types$1._true;
      node.raw = this.type.keyword;
      this.next();
      return this.finishNode(node, "Literal")

    case types$1.parenL:
      var start = this.start, expr = this.parseParenAndDistinguishExpression(canBeArrow, forInit);
      if (refDestructuringErrors) {
        if (refDestructuringErrors.parenthesizedAssign < 0 && !this.isSimpleAssignTarget(expr))
          { refDestructuringErrors.parenthesizedAssign = start; }
        if (refDestructuringErrors.parenthesizedBind < 0)
          { refDestructuringErrors.parenthesizedBind = start; }
      }
      return expr

    case types$1.bracketL:
      node = this.startNode();
      this.next();
      node.elements = this.parseExprList(types$1.bracketR, true, true, refDestructuringErrors);
      return this.finishNode(node, "ArrayExpression")

    case types$1.braceL:
      this.overrideContext(types.b_expr);
      return this.parseObj(false, refDestructuringErrors)

    case types$1._function:
      node = this.startNode();
      this.next();
      return this.parseFunction(node, 0)

    case types$1._class:
      return this.parseClass(this.startNode(), false)

    case types$1._new:
      return this.parseNew()

    case types$1.backQuote:
      return this.parseTemplate()

    case types$1._import:
      if (this.options.ecmaVersion >= 11) {
        return this.parseExprImport(forNew)
      } else {
        return this.unexpected()
      }

    default:
      return this.parseExprAtomDefault()
    }
  };

  pp$5.parseExprAtomDefault = function() {
    this.unexpected();
  };

  pp$5.parseExprImport = function(forNew) {
    var node = this.startNode();

    // Consume `import` as an identifier for `import.meta`.
    // Because `this.parseIdent(true)` doesn't check escape sequences, it needs the check of `this.containsEsc`.
    if (this.containsEsc) { this.raiseRecoverable(this.start, "Escape sequence in keyword import"); }
    this.next();

    if (this.type === types$1.parenL && !forNew) {
      return this.parseDynamicImport(node)
    } else if (this.type === types$1.dot) {
      var meta = this.startNodeAt(node.start, node.loc && node.loc.start);
      meta.name = "import";
      node.meta = this.finishNode(meta, "Identifier");
      return this.parseImportMeta(node)
    } else {
      this.unexpected();
    }
  };

  pp$5.parseDynamicImport = function(node) {
    this.next(); // skip `(`

    // Parse node.source.
    node.source = this.parseMaybeAssign();

    if (this.options.ecmaVersion >= 16) {
      if (!this.eat(types$1.parenR)) {
        this.expect(types$1.comma);
        if (!this.afterTrailingComma(types$1.parenR)) {
          node.options = this.parseMaybeAssign();
          if (!this.eat(types$1.parenR)) {
            this.expect(types$1.comma);
            if (!this.afterTrailingComma(types$1.parenR)) {
              this.unexpected();
            }
          }
        } else {
          node.options = null;
        }
      } else {
        node.options = null;
      }
    } else {
      // Verify ending.
      if (!this.eat(types$1.parenR)) {
        var errorPos = this.start;
        if (this.eat(types$1.comma) && this.eat(types$1.parenR)) {
          this.raiseRecoverable(errorPos, "Trailing comma is not allowed in import()");
        } else {
          this.unexpected(errorPos);
        }
      }
    }

    return this.finishNode(node, "ImportExpression")
  };

  pp$5.parseImportMeta = function(node) {
    this.next(); // skip `.`

    var containsEsc = this.containsEsc;
    node.property = this.parseIdent(true);

    if (node.property.name !== "meta")
      { this.raiseRecoverable(node.property.start, "The only valid meta property for import is 'import.meta'"); }
    if (containsEsc)
      { this.raiseRecoverable(node.start, "'import.meta' must not contain escaped characters"); }
    if (this.options.sourceType !== "module" && !this.options.allowImportExportEverywhere)
      { this.raiseRecoverable(node.start, "Cannot use 'import.meta' outside a module"); }

    return this.finishNode(node, "MetaProperty")
  };

  pp$5.parseLiteral = function(value) {
    var node = this.startNode();
    node.value = value;
    node.raw = this.input.slice(this.start, this.end);
    if (node.raw.charCodeAt(node.raw.length - 1) === 110) { node.bigint = node.raw.slice(0, -1).replace(/_/g, ""); }
    this.next();
    return this.finishNode(node, "Literal")
  };

  pp$5.parseParenExpression = function() {
    this.expect(types$1.parenL);
    var val = this.parseExpression();
    this.expect(types$1.parenR);
    return val
  };

  pp$5.shouldParseArrow = function(exprList) {
    return !this.canInsertSemicolon()
  };

  pp$5.parseParenAndDistinguishExpression = function(canBeArrow, forInit) {
    var startPos = this.start, startLoc = this.startLoc, val, allowTrailingComma = this.options.ecmaVersion >= 8;
    if (this.options.ecmaVersion >= 6) {
      this.next();

      var innerStartPos = this.start, innerStartLoc = this.startLoc;
      var exprList = [], first = true, lastIsComma = false;
      var refDestructuringErrors = new DestructuringErrors, oldYieldPos = this.yieldPos, oldAwaitPos = this.awaitPos, spreadStart;
      this.yieldPos = 0;
      this.awaitPos = 0;
      // Do not save awaitIdentPos to allow checking awaits nested in parameters
      while (this.type !== types$1.parenR) {
        first ? first = false : this.expect(types$1.comma);
        if (allowTrailingComma && this.afterTrailingComma(types$1.parenR, true)) {
          lastIsComma = true;
          break
        } else if (this.type === types$1.ellipsis) {
          spreadStart = this.start;
          exprList.push(this.parseParenItem(this.parseRestBinding()));
          if (this.type === types$1.comma) {
            this.raiseRecoverable(
              this.start,
              "Comma is not permitted after the rest element"
            );
          }
          break
        } else {
          exprList.push(this.parseMaybeAssign(false, refDestructuringErrors, this.parseParenItem));
        }
      }
      var innerEndPos = this.lastTokEnd, innerEndLoc = this.lastTokEndLoc;
      this.expect(types$1.parenR);

      if (canBeArrow && this.shouldParseArrow(exprList) && this.eat(types$1.arrow)) {
        this.checkPatternErrors(refDestructuringErrors, false);
        this.checkYieldAwaitInDefaultParams();
        this.yieldPos = oldYieldPos;
        this.awaitPos = oldAwaitPos;
        return this.parseParenArrowList(startPos, startLoc, exprList, forInit)
      }

      if (!exprList.length || lastIsComma) { this.unexpected(this.lastTokStart); }
      if (spreadStart) { this.unexpected(spreadStart); }
      this.checkExpressionErrors(refDestructuringErrors, true);
      this.yieldPos = oldYieldPos || this.yieldPos;
      this.awaitPos = oldAwaitPos || this.awaitPos;

      if (exprList.length > 1) {
        val = this.startNodeAt(innerStartPos, innerStartLoc);
        val.expressions = exprList;
        this.finishNodeAt(val, "SequenceExpression", innerEndPos, innerEndLoc);
      } else {
        val = exprList[0];
      }
    } else {
      val = this.parseParenExpression();
    }

    if (this.options.preserveParens) {
      var par = this.startNodeAt(startPos, startLoc);
      par.expression = val;
      return this.finishNode(par, "ParenthesizedExpression")
    } else {
      return val
    }
  };

  pp$5.parseParenItem = function(item) {
    return item
  };

  pp$5.parseParenArrowList = function(startPos, startLoc, exprList, forInit) {
    return this.parseArrowExpression(this.startNodeAt(startPos, startLoc), exprList, false, forInit)
  };

  // New's precedence is slightly tricky. It must allow its argument to
  // be a `[]` or dot subscript expression, but not a call — at least,
  // not without wrapping it in parentheses. Thus, it uses the noCalls
  // argument to parseSubscripts to prevent it from consuming the
  // argument list.

  var empty = [];

  pp$5.parseNew = function() {
    if (this.containsEsc) { this.raiseRecoverable(this.start, "Escape sequence in keyword new"); }
    var node = this.startNode();
    this.next();
    if (this.options.ecmaVersion >= 6 && this.type === types$1.dot) {
      var meta = this.startNodeAt(node.start, node.loc && node.loc.start);
      meta.name = "new";
      node.meta = this.finishNode(meta, "Identifier");
      this.next();
      var containsEsc = this.containsEsc;
      node.property = this.parseIdent(true);
      if (node.property.name !== "target")
        { this.raiseRecoverable(node.property.start, "The only valid meta property for new is 'new.target'"); }
      if (containsEsc)
        { this.raiseRecoverable(node.start, "'new.target' must not contain escaped characters"); }
      if (!this.allowNewDotTarget)
        { this.raiseRecoverable(node.start, "'new.target' can only be used in functions and class static block"); }
      return this.finishNode(node, "MetaProperty")
    }
    var startPos = this.start, startLoc = this.startLoc;
    node.callee = this.parseSubscripts(this.parseExprAtom(null, false, true), startPos, startLoc, true, false);
    if (this.eat(types$1.parenL)) { node.arguments = this.parseExprList(types$1.parenR, this.options.ecmaVersion >= 8, false); }
    else { node.arguments = empty; }
    return this.finishNode(node, "NewExpression")
  };

  // Parse template expression.

  pp$5.parseTemplateElement = function(ref) {
    var isTagged = ref.isTagged;

    var elem = this.startNode();
    if (this.type === types$1.invalidTemplate) {
      if (!isTagged) {
        this.raiseRecoverable(this.start, "Bad escape sequence in untagged template literal");
      }
      elem.value = {
        raw: this.value.replace(/\r\n?/g, "\n"),
        cooked: null
      };
    } else {
      elem.value = {
        raw: this.input.slice(this.start, this.end).replace(/\r\n?/g, "\n"),
        cooked: this.value
      };
    }
    this.next();
    elem.tail = this.type === types$1.backQuote;
    return this.finishNode(elem, "TemplateElement")
  };

  pp$5.parseTemplate = function(ref) {
    if ( ref === void 0 ) ref = {};
    var isTagged = ref.isTagged; if ( isTagged === void 0 ) isTagged = false;

    var node = this.startNode();
    this.next();
    node.expressions = [];
    var curElt = this.parseTemplateElement({isTagged: isTagged});
    node.quasis = [curElt];
    while (!curElt.tail) {
      if (this.type === types$1.eof) { this.raise(this.pos, "Unterminated template literal"); }
      this.expect(types$1.dollarBraceL);
      node.expressions.push(this.parseExpression());
      this.expect(types$1.braceR);
      node.quasis.push(curElt = this.parseTemplateElement({isTagged: isTagged}));
    }
    this.next();
    return this.finishNode(node, "TemplateLiteral")
  };

  pp$5.isAsyncProp = function(prop) {
    return !prop.computed && prop.key.type === "Identifier" && prop.key.name === "async" &&
      (this.type === types$1.name || this.type === types$1.num || this.type === types$1.string || this.type === types$1.bracketL || this.type.keyword || (this.options.ecmaVersion >= 9 && this.type === types$1.star)) &&
      !lineBreak.test(this.input.slice(this.lastTokEnd, this.start))
  };

  // Parse an object literal or binding pattern.

  pp$5.parseObj = function(isPattern, refDestructuringErrors) {
    var node = this.startNode(), first = true, propHash = {};
    node.properties = [];
    this.next();
    while (!this.eat(types$1.braceR)) {
      if (!first) {
        this.expect(types$1.comma);
        if (this.options.ecmaVersion >= 5 && this.afterTrailingComma(types$1.braceR)) { break }
      } else { first = false; }

      var prop = this.parseProperty(isPattern, refDestructuringErrors);
      if (!isPattern) { this.checkPropClash(prop, propHash, refDestructuringErrors); }
      node.properties.push(prop);
    }
    return this.finishNode(node, isPattern ? "ObjectPattern" : "ObjectExpression")
  };

  pp$5.parseProperty = function(isPattern, refDestructuringErrors) {
    var prop = this.startNode(), isGenerator, isAsync, startPos, startLoc;
    if (this.options.ecmaVersion >= 9 && this.eat(types$1.ellipsis)) {
      if (isPattern) {
        prop.argument = this.parseIdent(false);
        if (this.type === types$1.comma) {
          this.raiseRecoverable(this.start, "Comma is not permitted after the rest element");
        }
        return this.finishNode(prop, "RestElement")
      }
      // Parse argument.
      prop.argument = this.parseMaybeAssign(false, refDestructuringErrors);
      // To disallow trailing comma via `this.toAssignable()`.
      if (this.type === types$1.comma && refDestructuringErrors && refDestructuringErrors.trailingComma < 0) {
        refDestructuringErrors.trailingComma = this.start;
      }
      // Finish
      return this.finishNode(prop, "SpreadElement")
    }
    if (this.options.ecmaVersion >= 6) {
      prop.method = false;
      prop.shorthand = false;
      if (isPattern || refDestructuringErrors) {
        startPos = this.start;
        startLoc = this.startLoc;
      }
      if (!isPattern)
        { isGenerator = this.eat(types$1.star); }
    }
    var containsEsc = this.containsEsc;
    this.parsePropertyName(prop);
    if (!isPattern && !containsEsc && this.options.ecmaVersion >= 8 && !isGenerator && this.isAsyncProp(prop)) {
      isAsync = true;
      isGenerator = this.options.ecmaVersion >= 9 && this.eat(types$1.star);
      this.parsePropertyName(prop);
    } else {
      isAsync = false;
    }
    this.parsePropertyValue(prop, isPattern, isGenerator, isAsync, startPos, startLoc, refDestructuringErrors, containsEsc);
    return this.finishNode(prop, "Property")
  };

  pp$5.parseGetterSetter = function(prop) {
    prop.kind = prop.key.name;
    this.parsePropertyName(prop);
    prop.value = this.parseMethod(false);
    var paramCount = prop.kind === "get" ? 0 : 1;
    if (prop.value.params.length !== paramCount) {
      var start = prop.value.start;
      if (prop.kind === "get")
        { this.raiseRecoverable(start, "getter should have no params"); }
      else
        { this.raiseRecoverable(start, "setter should have exactly one param"); }
    } else {
      if (prop.kind === "set" && prop.value.params[0].type === "RestElement")
        { this.raiseRecoverable(prop.value.params[0].start, "Setter cannot use rest params"); }
    }
  };

  pp$5.parsePropertyValue = function(prop, isPattern, isGenerator, isAsync, startPos, startLoc, refDestructuringErrors, containsEsc) {
    if ((isGenerator || isAsync) && this.type === types$1.colon)
      { this.unexpected(); }

    if (this.eat(types$1.colon)) {
      prop.value = isPattern ? this.parseMaybeDefault(this.start, this.startLoc) : this.parseMaybeAssign(false, refDestructuringErrors);
      prop.kind = "init";
    } else if (this.options.ecmaVersion >= 6 && this.type === types$1.parenL) {
      if (isPattern) { this.unexpected(); }
      prop.kind = "init";
      prop.method = true;
      prop.value = this.parseMethod(isGenerator, isAsync);
    } else if (!isPattern && !containsEsc &&
               this.options.ecmaVersion >= 5 && !prop.computed && prop.key.type === "Identifier" &&
               (prop.key.name === "get" || prop.key.name === "set") &&
               (this.type !== types$1.comma && this.type !== types$1.braceR && this.type !== types$1.eq)) {
      if (isGenerator || isAsync) { this.unexpected(); }
      this.parseGetterSetter(prop);
    } else if (this.options.ecmaVersion >= 6 && !prop.computed && prop.key.type === "Identifier") {
      if (isGenerator || isAsync) { this.unexpected(); }
      this.checkUnreserved(prop.key);
      if (prop.key.name === "await" && !this.awaitIdentPos)
        { this.awaitIdentPos = startPos; }
      prop.kind = "init";
      if (isPattern) {
        prop.value = this.parseMaybeDefault(startPos, startLoc, this.copyNode(prop.key));
      } else if (this.type === types$1.eq && refDestructuringErrors) {
        if (refDestructuringErrors.shorthandAssign < 0)
          { refDestructuringErrors.shorthandAssign = this.start; }
        prop.value = this.parseMaybeDefault(startPos, startLoc, this.copyNode(prop.key));
      } else {
        prop.value = this.copyNode(prop.key);
      }
      prop.shorthand = true;
    } else { this.unexpected(); }
  };

  pp$5.parsePropertyName = function(prop) {
    if (this.options.ecmaVersion >= 6) {
      if (this.eat(types$1.bracketL)) {
        prop.computed = true;
        prop.key = this.parseMaybeAssign();
        this.expect(types$1.bracketR);
        return prop.key
      } else {
        prop.computed = false;
      }
    }
    return prop.key = this.type === types$1.num || this.type === types$1.string ? this.parseExprAtom() : this.parseIdent(this.options.allowReserved !== "never")
  };

  // Initialize empty function node.

  pp$5.initFunction = function(node) {
    node.id = null;
    if (this.options.ecmaVersion >= 6) { node.generator = node.expression = false; }
    if (this.options.ecmaVersion >= 8) { node.async = false; }
  };

  // Parse object or class method.

  pp$5.parseMethod = function(isGenerator, isAsync, allowDirectSuper) {
    var node = this.startNode(), oldYieldPos = this.yieldPos, oldAwaitPos = this.awaitPos, oldAwaitIdentPos = this.awaitIdentPos;

    this.initFunction(node);
    if (this.options.ecmaVersion >= 6)
      { node.generator = isGenerator; }
    if (this.options.ecmaVersion >= 8)
      { node.async = !!isAsync; }

    this.yieldPos = 0;
    this.awaitPos = 0;
    this.awaitIdentPos = 0;
    this.enterScope(functionFlags(isAsync, node.generator) | SCOPE_SUPER | (allowDirectSuper ? SCOPE_DIRECT_SUPER : 0));

    this.expect(types$1.parenL);
    node.params = this.parseBindingList(types$1.parenR, false, this.options.ecmaVersion >= 8);
    this.checkYieldAwaitInDefaultParams();
    this.parseFunctionBody(node, false, true, false);

    this.yieldPos = oldYieldPos;
    this.awaitPos = oldAwaitPos;
    this.awaitIdentPos = oldAwaitIdentPos;
    return this.finishNode(node, "FunctionExpression")
  };

  // Parse arrow function expression with given parameters.

  pp$5.parseArrowExpression = function(node, params, isAsync, forInit) {
    var oldYieldPos = this.yieldPos, oldAwaitPos = this.awaitPos, oldAwaitIdentPos = this.awaitIdentPos;

    this.enterScope(functionFlags(isAsync, false) | SCOPE_ARROW);
    this.initFunction(node);
    if (this.options.ecmaVersion >= 8) { node.async = !!isAsync; }

    this.yieldPos = 0;
    this.awaitPos = 0;
    this.awaitIdentPos = 0;

    node.params = this.toAssignableList(params, true);
    this.parseFunctionBody(node, true, false, forInit);

    this.yieldPos = oldYieldPos;
    this.awaitPos = oldAwaitPos;
    this.awaitIdentPos = oldAwaitIdentPos;
    return this.finishNode(node, "ArrowFunctionExpression")
  };

  // Parse function body and check parameters.

  pp$5.parseFunctionBody = function(node, isArrowFunction, isMethod, forInit) {
    var isExpression = isArrowFunction && this.type !== types$1.braceL;
    var oldStrict = this.strict, useStrict = false;

    if (isExpression) {
      node.body = this.parseMaybeAssign(forInit);
      node.expression = true;
      this.checkParams(node, false);
    } else {
      var nonSimple = this.options.ecmaVersion >= 7 && !this.isSimpleParamList(node.params);
      if (!oldStrict || nonSimple) {
        useStrict = this.strictDirective(this.end);
        // If this is a strict mode function, verify that argument names
        // are not repeated, and it does not try to bind the words `eval`
        // or `arguments`.
        if (useStrict && nonSimple)
          { this.raiseRecoverable(node.start, "Illegal 'use strict' directive in function with non-simple parameter list"); }
      }
      // Start a new scope with regard to labels and the `inFunction`
      // flag (restore them to their old value afterwards).
      var oldLabels = this.labels;
      this.labels = [];
      if (useStrict) { this.strict = true; }

      // Add the params to varDeclaredNames to ensure that an error is thrown
      // if a let/const declaration in the function clashes with one of the params.
      this.checkParams(node, !oldStrict && !useStrict && !isArrowFunction && !isMethod && this.isSimpleParamList(node.params));
      // Ensure the function name isn't a forbidden identifier in strict mode, e.g. 'eval'
      if (this.strict && node.id) { this.checkLValSimple(node.id, BIND_OUTSIDE); }
      node.body = this.parseBlock(false, undefined, useStrict && !oldStrict);
      node.expression = false;
      this.adaptDirectivePrologue(node.body.body);
      this.labels = oldLabels;
    }
    this.exitScope();
  };

  pp$5.isSimpleParamList = function(params) {
    for (var i = 0, list = params; i < list.length; i += 1)
      {
      var param = list[i];

      if (param.type !== "Identifier") { return false
    } }
    return true
  };

  // Checks function params for various disallowed patterns such as using "eval"
  // or "arguments" and duplicate parameters.

  pp$5.checkParams = function(node, allowDuplicates) {
    var nameHash = Object.create(null);
    for (var i = 0, list = node.params; i < list.length; i += 1)
      {
      var param = list[i];

      this.checkLValInnerPattern(param, BIND_VAR, allowDuplicates ? null : nameHash);
    }
  };

  // Parses a comma-separated list of expressions, and returns them as
  // an array. `close` is the token type that ends the list, and
  // `allowEmpty` can be turned on to allow subsequent commas with
  // nothing in between them to be parsed as `null` (which is needed
  // for array literals).

  pp$5.parseExprList = function(close, allowTrailingComma, allowEmpty, refDestructuringErrors) {
    var elts = [], first = true;
    while (!this.eat(close)) {
      if (!first) {
        this.expect(types$1.comma);
        if (allowTrailingComma && this.afterTrailingComma(close)) { break }
      } else { first = false; }

      var elt = (void 0);
      if (allowEmpty && this.type === types$1.comma)
        { elt = null; }
      else if (this.type === types$1.ellipsis) {
        elt = this.parseSpread(refDestructuringErrors);
        if (refDestructuringErrors && this.type === types$1.comma && refDestructuringErrors.trailingComma < 0)
          { refDestructuringErrors.trailingComma = this.start; }
      } else {
        elt = this.parseMaybeAssign(false, refDestructuringErrors);
      }
      elts.push(elt);
    }
    return elts
  };

  pp$5.checkUnreserved = function(ref) {
    var start = ref.start;
    var end = ref.end;
    var name = ref.name;

    if (this.inGenerator && name === "yield")
      { this.raiseRecoverable(start, "Cannot use 'yield' as identifier inside a generator"); }
    if (this.inAsync && name === "await")
      { this.raiseRecoverable(start, "Cannot use 'await' as identifier inside an async function"); }
    if (this.currentThisScope().inClassFieldInit && name === "arguments")
      { this.raiseRecoverable(start, "Cannot use 'arguments' in class field initializer"); }
    if (this.inClassStaticBlock && (name === "arguments" || name === "await"))
      { this.raise(start, ("Cannot use " + name + " in class static initialization block")); }
    if (this.keywords.test(name))
      { this.raise(start, ("Unexpected keyword '" + name + "'")); }
    if (this.options.ecmaVersion < 6 &&
      this.input.slice(start, end).indexOf("\\") !== -1) { return }
    var re = this.strict ? this.reservedWordsStrict : this.reservedWords;
    if (re.test(name)) {
      if (!this.inAsync && name === "await")
        { this.raiseRecoverable(start, "Cannot use keyword 'await' outside an async function"); }
      this.raiseRecoverable(start, ("The keyword '" + name + "' is reserved"));
    }
  };

  // Parse the next token as an identifier. If `liberal` is true (used
  // when parsing properties), it will also convert keywords into
  // identifiers.

  pp$5.parseIdent = function(liberal) {
    var node = this.parseIdentNode();
    this.next(!!liberal);
    this.finishNode(node, "Identifier");
    if (!liberal) {
      this.checkUnreserved(node);
      if (node.name === "await" && !this.awaitIdentPos)
        { this.awaitIdentPos = node.start; }
    }
    return node
  };

  pp$5.parseIdentNode = function() {
    var node = this.startNode();
    if (this.type === types$1.name) {
      node.name = this.value;
    } else if (this.type.keyword) {
      node.name = this.type.keyword;

      // To fix https://github.com/acornjs/acorn/issues/575
      // `class` and `function` keywords push new context into this.context.
      // But there is no chance to pop the context if the keyword is consumed as an identifier such as a property name.
      // If the previous token is a dot, this does not apply because the context-managing code already ignored the keyword
      if ((node.name === "class" || node.name === "function") &&
        (this.lastTokEnd !== this.lastTokStart + 1 || this.input.charCodeAt(this.lastTokStart) !== 46)) {
        this.context.pop();
      }
      this.type = types$1.name;
    } else {
      this.unexpected();
    }
    return node
  };

  pp$5.parsePrivateIdent = function() {
    var node = this.startNode();
    if (this.type === types$1.privateId) {
      node.name = this.value;
    } else {
      this.unexpected();
    }
    this.next();
    this.finishNode(node, "PrivateIdentifier");

    // For validating existence
    if (this.options.checkPrivateFields) {
      if (this.privateNameStack.length === 0) {
        this.raise(node.start, ("Private field '#" + (node.name) + "' must be declared in an enclosing class"));
      } else {
        this.privateNameStack[this.privateNameStack.length - 1].used.push(node);
      }
    }

    return node
  };

  // Parses yield expression inside generator.

  pp$5.parseYield = function(forInit) {
    if (!this.yieldPos) { this.yieldPos = this.start; }

    var node = this.startNode();
    this.next();
    if (this.type === types$1.semi || this.canInsertSemicolon() || (this.type !== types$1.star && !this.type.startsExpr)) {
      node.delegate = false;
      node.argument = null;
    } else {
      node.delegate = this.eat(types$1.star);
      node.argument = this.parseMaybeAssign(forInit);
    }
    return this.finishNode(node, "YieldExpression")
  };

  pp$5.parseAwait = function(forInit) {
    if (!this.awaitPos) { this.awaitPos = this.start; }

    var node = this.startNode();
    this.next();
    node.argument = this.parseMaybeUnary(null, true, false, forInit);
    return this.finishNode(node, "AwaitExpression")
  };

  var pp$4 = Parser.prototype;

  // This function is used to raise exceptions on parse errors. It
  // takes an offset integer (into the current `input`) to indicate
  // the location of the error, attaches the position to the end
  // of the error message, and then raises a `SyntaxError` with that
  // message.

  pp$4.raise = function(pos, message) {
    var loc = getLineInfo(this.input, pos);
    message += " (" + loc.line + ":" + loc.column + ")";
    var err = new SyntaxError(message);
    err.pos = pos; err.loc = loc; err.raisedAt = this.pos;
    throw err
  };

  pp$4.raiseRecoverable = pp$4.raise;

  pp$4.curPosition = function() {
    if (this.options.locations) {
      return new Position(this.curLine, this.pos - this.lineStart)
    }
  };

  var pp$3 = Parser.prototype;

  var Scope = function Scope(flags) {
    this.flags = flags;
    // A list of var-declared names in the current lexical scope
    this.var = [];
    // A list of lexically-declared names in the current lexical scope
    this.lexical = [];
    // A list of lexically-declared FunctionDeclaration names in the current lexical scope
    this.functions = [];
    // A switch to disallow the identifier reference 'arguments'
    this.inClassFieldInit = false;
  };

  // The functions in this module keep track of declared variables in the current scope in order to detect duplicate variable names.

  pp$3.enterScope = function(flags) {
    this.scopeStack.push(new Scope(flags));
  };

  pp$3.exitScope = function() {
    this.scopeStack.pop();
  };

  // The spec says:
  // > At the top level of a function, or script, function declarations are
  // > treated like var declarations rather than like lexical declarations.
  pp$3.treatFunctionsAsVarInScope = function(scope) {
    return (scope.flags & SCOPE_FUNCTION) || !this.inModule && (scope.flags & SCOPE_TOP)
  };

  pp$3.declareName = function(name, bindingType, pos) {
    var redeclared = false;
    if (bindingType === BIND_LEXICAL) {
      var scope = this.currentScope();
      redeclared = scope.lexical.indexOf(name) > -1 || scope.functions.indexOf(name) > -1 || scope.var.indexOf(name) > -1;
      scope.lexical.push(name);
      if (this.inModule && (scope.flags & SCOPE_TOP))
        { delete this.undefinedExports[name]; }
    } else if (bindingType === BIND_SIMPLE_CATCH) {
      var scope$1 = this.currentScope();
      scope$1.lexical.push(name);
    } else if (bindingType === BIND_FUNCTION) {
      var scope$2 = this.currentScope();
      if (this.treatFunctionsAsVar)
        { redeclared = scope$2.lexical.indexOf(name) > -1; }
      else
        { redeclared = scope$2.lexical.indexOf(name) > -1 || scope$2.var.indexOf(name) > -1; }
      scope$2.functions.push(name);
    } else {
      for (var i = this.scopeStack.length - 1; i >= 0; --i) {
        var scope$3 = this.scopeStack[i];
        if (scope$3.lexical.indexOf(name) > -1 && !((scope$3.flags & SCOPE_SIMPLE_CATCH) && scope$3.lexical[0] === name) ||
            !this.treatFunctionsAsVarInScope(scope$3) && scope$3.functions.indexOf(name) > -1) {
          redeclared = true;
          break
        }
        scope$3.var.push(name);
        if (this.inModule && (scope$3.flags & SCOPE_TOP))
          { delete this.undefinedExports[name]; }
        if (scope$3.flags & SCOPE_VAR) { break }
      }
    }
    if (redeclared) { this.raiseRecoverable(pos, ("Identifier '" + name + "' has already been declared")); }
  };

  pp$3.checkLocalExport = function(id) {
    // scope.functions must be empty as Module code is always strict.
    if (this.scopeStack[0].lexical.indexOf(id.name) === -1 &&
        this.scopeStack[0].var.indexOf(id.name) === -1) {
      this.undefinedExports[id.name] = id;
    }
  };

  pp$3.currentScope = function() {
    return this.scopeStack[this.scopeStack.length - 1]
  };

  pp$3.currentVarScope = function() {
    for (var i = this.scopeStack.length - 1;; i--) {
      var scope = this.scopeStack[i];
      if (scope.flags & SCOPE_VAR) { return scope }
    }
  };

  // Could be useful for `this`, `new.target`, `super()`, `super.property`, and `super[property]`.
  pp$3.currentThisScope = function() {
    for (var i = this.scopeStack.length - 1;; i--) {
      var scope = this.scopeStack[i];
      if (scope.flags & SCOPE_VAR && !(scope.flags & SCOPE_ARROW)) { return scope }
    }
  };

  var Node = function Node(parser, pos, loc) {
    this.type = "";
    this.start = pos;
    this.end = 0;
    if (parser.options.locations)
      { this.loc = new SourceLocation(parser, loc); }
    if (parser.options.directSourceFile)
      { this.sourceFile = parser.options.directSourceFile; }
    if (parser.options.ranges)
      { this.range = [pos, 0]; }
  };

  // Start an AST node, attaching a start offset.

  var pp$2 = Parser.prototype;

  pp$2.startNode = function() {
    return new Node(this, this.start, this.startLoc)
  };

  pp$2.startNodeAt = function(pos, loc) {
    return new Node(this, pos, loc)
  };

  // Finish an AST node, adding `type` and `end` properties.

  function finishNodeAt(node, type, pos, loc) {
    node.type = type;
    node.end = pos;
    if (this.options.locations)
      { node.loc.end = loc; }
    if (this.options.ranges)
      { node.range[1] = pos; }
    return node
  }

  pp$2.finishNode = function(node, type) {
    return finishNodeAt.call(this, node, type, this.lastTokEnd, this.lastTokEndLoc)
  };

  // Finish node at given position

  pp$2.finishNodeAt = function(node, type, pos, loc) {
    return finishNodeAt.call(this, node, type, pos, loc)
  };

  pp$2.copyNode = function(node) {
    var newNode = new Node(this, node.start, this.startLoc);
    for (var prop in node) { newNode[prop] = node[prop]; }
    return newNode
  };

  // This file was generated by "bin/generate-unicode-script-values.js". Do not modify manually!
  var scriptValuesAddedInUnicode = "Gara Garay Gukh Gurung_Khema Hrkt Katakana_Or_Hiragana Kawi Kirat_Rai Krai Nag_Mundari Nagm Ol_Onal Onao Sunu Sunuwar Todhri Todr Tulu_Tigalari Tutg Unknown Zzzz";

  // This file contains Unicode properties extracted from the ECMAScript specification.
  // The lists are extracted like so:
  // $$('#table-binary-unicode-properties > figure > table > tbody > tr > td:nth-child(1) code').map(el => el.innerText)

  // #table-binary-unicode-properties
  var ecma9BinaryProperties = "ASCII ASCII_Hex_Digit AHex Alphabetic Alpha Any Assigned Bidi_Control Bidi_C Bidi_Mirrored Bidi_M Case_Ignorable CI Cased Changes_When_Casefolded CWCF Changes_When_Casemapped CWCM Changes_When_Lowercased CWL Changes_When_NFKC_Casefolded CWKCF Changes_When_Titlecased CWT Changes_When_Uppercased CWU Dash Default_Ignorable_Code_Point DI Deprecated Dep Diacritic Dia Emoji Emoji_Component Emoji_Modifier Emoji_Modifier_Base Emoji_Presentation Extender Ext Grapheme_Base Gr_Base Grapheme_Extend Gr_Ext Hex_Digit Hex IDS_Binary_Operator IDSB IDS_Trinary_Operator IDST ID_Continue IDC ID_Start IDS Ideographic Ideo Join_Control Join_C Logical_Order_Exception LOE Lowercase Lower Math Noncharacter_Code_Point NChar Pattern_Syntax Pat_Syn Pattern_White_Space Pat_WS Quotation_Mark QMark Radical Regional_Indicator RI Sentence_Terminal STerm Soft_Dotted SD Terminal_Punctuation Term Unified_Ideograph UIdeo Uppercase Upper Variation_Selector VS White_Space space XID_Continue XIDC XID_Start XIDS";
  var ecma10BinaryProperties = ecma9BinaryProperties + " Extended_Pictographic";
  var ecma11BinaryProperties = ecma10BinaryProperties;
  var ecma12BinaryProperties = ecma11BinaryProperties + " EBase EComp EMod EPres ExtPict";
  var ecma13BinaryProperties = ecma12BinaryProperties;
  var ecma14BinaryProperties = ecma13BinaryProperties;

  var unicodeBinaryProperties = {
    9: ecma9BinaryProperties,
    10: ecma10BinaryProperties,
    11: ecma11BinaryProperties,
    12: ecma12BinaryProperties,
    13: ecma13BinaryProperties,
    14: ecma14BinaryProperties
  };

  // #table-binary-unicode-properties-of-strings
  var ecma14BinaryPropertiesOfStrings = "Basic_Emoji Emoji_Keycap_Sequence RGI_Emoji_Modifier_Sequence RGI_Emoji_Flag_Sequence RGI_Emoji_Tag_Sequence RGI_Emoji_ZWJ_Sequence RGI_Emoji";

  var unicodeBinaryPropertiesOfStrings = {
    9: "",
    10: "",
    11: "",
    12: "",
    13: "",
    14: ecma14BinaryPropertiesOfStrings
  };

  // #table-unicode-general-category-values
  var unicodeGeneralCategoryValues = "Cased_Letter LC Close_Punctuation Pe Connector_Punctuation Pc Control Cc cntrl Currency_Symbol Sc Dash_Punctuation Pd Decimal_Number Nd digit Enclosing_Mark Me Final_Punctuation Pf Format Cf Initial_Punctuation Pi Letter L Letter_Number Nl Line_Separator Zl Lowercase_Letter Ll Mark M Combining_Mark Math_Symbol Sm Modifier_Letter Lm Modifier_Symbol Sk Nonspacing_Mark Mn Number N Open_Punctuation Ps Other C Other_Letter Lo Other_Number No Other_Punctuation Po Other_Symbol So Paragraph_Separator Zp Private_Use Co Punctuation P punct Separator Z Space_Separator Zs Spacing_Mark Mc Surrogate Cs Symbol S Titlecase_Letter Lt Unassigned Cn Uppercase_Letter Lu";

  // #table-unicode-script-values
  var ecma9ScriptValues = "Adlam Adlm Ahom Anatolian_Hieroglyphs Hluw Arabic Arab Armenian Armn Avestan Avst Balinese Bali Bamum Bamu Bassa_Vah Bass Batak Batk Bengali Beng Bhaiksuki Bhks Bopomofo Bopo Brahmi Brah Braille Brai Buginese Bugi Buhid Buhd Canadian_Aboriginal Cans Carian Cari Caucasian_Albanian Aghb Chakma Cakm Cham Cham Cherokee Cher Common Zyyy Coptic Copt Qaac Cuneiform Xsux Cypriot Cprt Cyrillic Cyrl Deseret Dsrt Devanagari Deva Duployan Dupl Egyptian_Hieroglyphs Egyp Elbasan Elba Ethiopic Ethi Georgian Geor Glagolitic Glag Gothic Goth Grantha Gran Greek Grek Gujarati Gujr Gurmukhi Guru Han Hani Hangul Hang Hanunoo Hano Hatran Hatr Hebrew Hebr Hiragana Hira Imperial_Aramaic Armi Inherited Zinh Qaai Inscriptional_Pahlavi Phli Inscriptional_Parthian Prti Javanese Java Kaithi Kthi Kannada Knda Katakana Kana Kayah_Li Kali Kharoshthi Khar Khmer Khmr Khojki Khoj Khudawadi Sind Lao Laoo Latin Latn Lepcha Lepc Limbu Limb Linear_A Lina Linear_B Linb Lisu Lisu Lycian Lyci Lydian Lydi Mahajani Mahj Malayalam Mlym Mandaic Mand Manichaean Mani Marchen Marc Masaram_Gondi Gonm Meetei_Mayek Mtei Mende_Kikakui Mend Meroitic_Cursive Merc Meroitic_Hieroglyphs Mero Miao Plrd Modi Mongolian Mong Mro Mroo Multani Mult Myanmar Mymr Nabataean Nbat New_Tai_Lue Talu Newa Newa Nko Nkoo Nushu Nshu Ogham Ogam Ol_Chiki Olck Old_Hungarian Hung Old_Italic Ital Old_North_Arabian Narb Old_Permic Perm Old_Persian Xpeo Old_South_Arabian Sarb Old_Turkic Orkh Oriya Orya Osage Osge Osmanya Osma Pahawh_Hmong Hmng Palmyrene Palm Pau_Cin_Hau Pauc Phags_Pa Phag Phoenician Phnx Psalter_Pahlavi Phlp Rejang Rjng Runic Runr Samaritan Samr Saurashtra Saur Sharada Shrd Shavian Shaw Siddham Sidd SignWriting Sgnw Sinhala Sinh Sora_Sompeng Sora Soyombo Soyo Sundanese Sund Syloti_Nagri Sylo Syriac Syrc Tagalog Tglg Tagbanwa Tagb Tai_Le Tale Tai_Tham Lana Tai_Viet Tavt Takri Takr Tamil Taml Tangut Tang Telugu Telu Thaana Thaa Thai Thai Tibetan Tibt Tifinagh Tfng Tirhuta Tirh Ugaritic Ugar Vai Vaii Warang_Citi Wara Yi Yiii Zanabazar_Square Zanb";
  var ecma10ScriptValues = ecma9ScriptValues + " Dogra Dogr Gunjala_Gondi Gong Hanifi_Rohingya Rohg Makasar Maka Medefaidrin Medf Old_Sogdian Sogo Sogdian Sogd";
  var ecma11ScriptValues = ecma10ScriptValues + " Elymaic Elym Nandinagari Nand Nyiakeng_Puachue_Hmong Hmnp Wancho Wcho";
  var ecma12ScriptValues = ecma11ScriptValues + " Chorasmian Chrs Diak Dives_Akuru Khitan_Small_Script Kits Yezi Yezidi";
  var ecma13ScriptValues = ecma12ScriptValues + " Cypro_Minoan Cpmn Old_Uyghur Ougr Tangsa Tnsa Toto Vithkuqi Vith";
  var ecma14ScriptValues = ecma13ScriptValues + " " + scriptValuesAddedInUnicode;

  var unicodeScriptValues = {
    9: ecma9ScriptValues,
    10: ecma10ScriptValues,
    11: ecma11ScriptValues,
    12: ecma12ScriptValues,
    13: ecma13ScriptValues,
    14: ecma14ScriptValues
  };

  var data = {};
  function buildUnicodeData(ecmaVersion) {
    var d = data[ecmaVersion] = {
      binary: wordsRegexp(unicodeBinaryProperties[ecmaVersion] + " " + unicodeGeneralCategoryValues),
      binaryOfStrings: wordsRegexp(unicodeBinaryPropertiesOfStrings[ecmaVersion]),
      nonBinary: {
        General_Category: wordsRegexp(unicodeGeneralCategoryValues),
        Script: wordsRegexp(unicodeScriptValues[ecmaVersion])
      }
    };
    d.nonBinary.Script_Extensions = d.nonBinary.Script;

    d.nonBinary.gc = d.nonBinary.General_Category;
    d.nonBinary.sc = d.nonBinary.Script;
    d.nonBinary.scx = d.nonBinary.Script_Extensions;
  }

  for (var i = 0, list = [9, 10, 11, 12, 13, 14]; i < list.length; i += 1) {
    var ecmaVersion = list[i];

    buildUnicodeData(ecmaVersion);
  }

  var pp$1 = Parser.prototype;

  // Track disjunction structure to determine whether a duplicate
  // capture group name is allowed because it is in a separate branch.
  var BranchID = function BranchID(parent, base) {
    // Parent disjunction branch
    this.parent = parent;
    // Identifies this set of sibling branches
    this.base = base || this;
  };

  BranchID.prototype.separatedFrom = function separatedFrom (alt) {
    // A branch is separate from another branch if they or any of
    // their parents are siblings in a given disjunction
    for (var self = this; self; self = self.parent) {
      for (var other = alt; other; other = other.parent) {
        if (self.base === other.base && self !== other) { return true }
      }
    }
    return false
  };

  BranchID.prototype.sibling = function sibling () {
    return new BranchID(this.parent, this.base)
  };

  var RegExpValidationState = function RegExpValidationState(parser) {
    this.parser = parser;
    this.validFlags = "gim" + (parser.options.ecmaVersion >= 6 ? "uy" : "") + (parser.options.ecmaVersion >= 9 ? "s" : "") + (parser.options.ecmaVersion >= 13 ? "d" : "") + (parser.options.ecmaVersion >= 15 ? "v" : "");
    this.unicodeProperties = data[parser.options.ecmaVersion >= 14 ? 14 : parser.options.ecmaVersion];
    this.source = "";
    this.flags = "";
    this.start = 0;
    this.switchU = false;
    this.switchV = false;
    this.switchN = false;
    this.pos = 0;
    this.lastIntValue = 0;
    this.lastStringValue = "";
    this.lastAssertionIsQuantifiable = false;
    this.numCapturingParens = 0;
    this.maxBackReference = 0;
    this.groupNames = Object.create(null);
    this.backReferenceNames = [];
    this.branchID = null;
  };

  RegExpValidationState.prototype.reset = function reset (start, pattern, flags) {
    var unicodeSets = flags.indexOf("v") !== -1;
    var unicode = flags.indexOf("u") !== -1;
    this.start = start | 0;
    this.source = pattern + "";
    this.flags = flags;
    if (unicodeSets && this.parser.options.ecmaVersion >= 15) {
      this.switchU = true;
      this.switchV = true;
      this.switchN = true;
    } else {
      this.switchU = unicode && this.parser.options.ecmaVersion >= 6;
      this.switchV = false;
      this.switchN = unicode && this.parser.options.ecmaVersion >= 9;
    }
  };

  RegExpValidationState.prototype.raise = function raise (message) {
    this.parser.raiseRecoverable(this.start, ("Invalid regular expression: /" + (this.source) + "/: " + message));
  };

  // If u flag is given, this returns the code point at the index (it combines a surrogate pair).
  // Otherwise, this returns the code unit of the index (can be a part of a surrogate pair).
  RegExpValidationState.prototype.at = function at (i, forceU) {
      if ( forceU === void 0 ) forceU = false;

    var s = this.source;
    var l = s.length;
    if (i >= l) {
      return -1
    }
    var c = s.charCodeAt(i);
    if (!(forceU || this.switchU) || c <= 0xD7FF || c >= 0xE000 || i + 1 >= l) {
      return c
    }
    var next = s.charCodeAt(i + 1);
    return next >= 0xDC00 && next <= 0xDFFF ? (c << 10) + next - 0x35FDC00 : c
  };

  RegExpValidationState.prototype.nextIndex = function nextIndex (i, forceU) {
      if ( forceU === void 0 ) forceU = false;

    var s = this.source;
    var l = s.length;
    if (i >= l) {
      return l
    }
    var c = s.charCodeAt(i), next;
    if (!(forceU || this.switchU) || c <= 0xD7FF || c >= 0xE000 || i + 1 >= l ||
        (next = s.charCodeAt(i + 1)) < 0xDC00 || next > 0xDFFF) {
      return i + 1
    }
    return i + 2
  };

  RegExpValidationState.prototype.current = function current (forceU) {
      if ( forceU === void 0 ) forceU = false;

    return this.at(this.pos, forceU)
  };

  RegExpValidationState.prototype.lookahead = function lookahead (forceU) {
      if ( forceU === void 0 ) forceU = false;

    return this.at(this.nextIndex(this.pos, forceU), forceU)
  };

  RegExpValidationState.prototype.advance = function advance (forceU) {
      if ( forceU === void 0 ) forceU = false;

    this.pos = this.nextIndex(this.pos, forceU);
  };

  RegExpValidationState.prototype.eat = function eat (ch, forceU) {
      if ( forceU === void 0 ) forceU = false;

    if (this.current(forceU) === ch) {
      this.advance(forceU);
      return true
    }
    return false
  };

  RegExpValidationState.prototype.eatChars = function eatChars (chs, forceU) {
      if ( forceU === void 0 ) forceU = false;

    var pos = this.pos;
    for (var i = 0, list = chs; i < list.length; i += 1) {
      var ch = list[i];

        var current = this.at(pos, forceU);
      if (current === -1 || current !== ch) {
        return false
      }
      pos = this.nextIndex(pos, forceU);
    }
    this.pos = pos;
    return true
  };

  /**
   * Validate the flags part of a given RegExpLiteral.
   *
   * @param {RegExpValidationState} state The state to validate RegExp.
   * @returns {void}
   */
  pp$1.validateRegExpFlags = function(state) {
    var validFlags = state.validFlags;
    var flags = state.flags;

    var u = false;
    var v = false;

    for (var i = 0; i < flags.length; i++) {
      var flag = flags.charAt(i);
      if (validFlags.indexOf(flag) === -1) {
        this.raise(state.start, "Invalid regular expression flag");
      }
      if (flags.indexOf(flag, i + 1) > -1) {
        this.raise(state.start, "Duplicate regular expression flag");
      }
      if (flag === "u") { u = true; }
      if (flag === "v") { v = true; }
    }
    if (this.options.ecmaVersion >= 15 && u && v) {
      this.raise(state.start, "Invalid regular expression flag");
    }
  };

  function hasProp(obj) {
    for (var _ in obj) { return true }
    return false
  }

  /**
   * Validate the pattern part of a given RegExpLiteral.
   *
   * @param {RegExpValidationState} state The state to validate RegExp.
   * @returns {void}
   */
  pp$1.validateRegExpPattern = function(state) {
    this.regexp_pattern(state);

    // The goal symbol for the parse is |Pattern[~U, ~N]|. If the result of
    // parsing contains a |GroupName|, reparse with the goal symbol
    // |Pattern[~U, +N]| and use this result instead. Throw a *SyntaxError*
    // exception if _P_ did not conform to the grammar, if any elements of _P_
    // were not matched by the parse, or if any Early Error conditions exist.
    if (!state.switchN && this.options.ecmaVersion >= 9 && hasProp(state.groupNames)) {
      state.switchN = true;
      this.regexp_pattern(state);
    }
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-Pattern
  pp$1.regexp_pattern = function(state) {
    state.pos = 0;
    state.lastIntValue = 0;
    state.lastStringValue = "";
    state.lastAssertionIsQuantifiable = false;
    state.numCapturingParens = 0;
    state.maxBackReference = 0;
    state.groupNames = Object.create(null);
    state.backReferenceNames.length = 0;
    state.branchID = null;

    this.regexp_disjunction(state);

    if (state.pos !== state.source.length) {
      // Make the same messages as V8.
      if (state.eat(0x29 /* ) */)) {
        state.raise("Unmatched ')'");
      }
      if (state.eat(0x5D /* ] */) || state.eat(0x7D /* } */)) {
        state.raise("Lone quantifier brackets");
      }
    }
    if (state.maxBackReference > state.numCapturingParens) {
      state.raise("Invalid escape");
    }
    for (var i = 0, list = state.backReferenceNames; i < list.length; i += 1) {
      var name = list[i];

      if (!state.groupNames[name]) {
        state.raise("Invalid named capture referenced");
      }
    }
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-Disjunction
  pp$1.regexp_disjunction = function(state) {
    var trackDisjunction = this.options.ecmaVersion >= 16;
    if (trackDisjunction) { state.branchID = new BranchID(state.branchID, null); }
    this.regexp_alternative(state);
    while (state.eat(0x7C /* | */)) {
      if (trackDisjunction) { state.branchID = state.branchID.sibling(); }
      this.regexp_alternative(state);
    }
    if (trackDisjunction) { state.branchID = state.branchID.parent; }

    // Make the same message as V8.
    if (this.regexp_eatQuantifier(state, true)) {
      state.raise("Nothing to repeat");
    }
    if (state.eat(0x7B /* { */)) {
      state.raise("Lone quantifier brackets");
    }
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-Alternative
  pp$1.regexp_alternative = function(state) {
    while (state.pos < state.source.length && this.regexp_eatTerm(state)) {}
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-annexB-Term
  pp$1.regexp_eatTerm = function(state) {
    if (this.regexp_eatAssertion(state)) {
      // Handle `QuantifiableAssertion Quantifier` alternative.
      // `state.lastAssertionIsQuantifiable` is true if the last eaten Assertion
      // is a QuantifiableAssertion.
      if (state.lastAssertionIsQuantifiable && this.regexp_eatQuantifier(state)) {
        // Make the same message as V8.
        if (state.switchU) {
          state.raise("Invalid quantifier");
        }
      }
      return true
    }

    if (state.switchU ? this.regexp_eatAtom(state) : this.regexp_eatExtendedAtom(state)) {
      this.regexp_eatQuantifier(state);
      return true
    }

    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-annexB-Assertion
  pp$1.regexp_eatAssertion = function(state) {
    var start = state.pos;
    state.lastAssertionIsQuantifiable = false;

    // ^, $
    if (state.eat(0x5E /* ^ */) || state.eat(0x24 /* $ */)) {
      return true
    }

    // \b \B
    if (state.eat(0x5C /* \ */)) {
      if (state.eat(0x42 /* B */) || state.eat(0x62 /* b */)) {
        return true
      }
      state.pos = start;
    }

    // Lookahead / Lookbehind
    if (state.eat(0x28 /* ( */) && state.eat(0x3F /* ? */)) {
      var lookbehind = false;
      if (this.options.ecmaVersion >= 9) {
        lookbehind = state.eat(0x3C /* < */);
      }
      if (state.eat(0x3D /* = */) || state.eat(0x21 /* ! */)) {
        this.regexp_disjunction(state);
        if (!state.eat(0x29 /* ) */)) {
          state.raise("Unterminated group");
        }
        state.lastAssertionIsQuantifiable = !lookbehind;
        return true
      }
    }

    state.pos = start;
    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-Quantifier
  pp$1.regexp_eatQuantifier = function(state, noError) {
    if ( noError === void 0 ) noError = false;

    if (this.regexp_eatQuantifierPrefix(state, noError)) {
      state.eat(0x3F /* ? */);
      return true
    }
    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-QuantifierPrefix
  pp$1.regexp_eatQuantifierPrefix = function(state, noError) {
    return (
      state.eat(0x2A /* * */) ||
      state.eat(0x2B /* + */) ||
      state.eat(0x3F /* ? */) ||
      this.regexp_eatBracedQuantifier(state, noError)
    )
  };
  pp$1.regexp_eatBracedQuantifier = function(state, noError) {
    var start = state.pos;
    if (state.eat(0x7B /* { */)) {
      var min = 0, max = -1;
      if (this.regexp_eatDecimalDigits(state)) {
        min = state.lastIntValue;
        if (state.eat(0x2C /* , */) && this.regexp_eatDecimalDigits(state)) {
          max = state.lastIntValue;
        }
        if (state.eat(0x7D /* } */)) {
          // SyntaxError in https://www.ecma-international.org/ecma-262/8.0/#sec-term
          if (max !== -1 && max < min && !noError) {
            state.raise("numbers out of order in {} quantifier");
          }
          return true
        }
      }
      if (state.switchU && !noError) {
        state.raise("Incomplete quantifier");
      }
      state.pos = start;
    }
    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-Atom
  pp$1.regexp_eatAtom = function(state) {
    return (
      this.regexp_eatPatternCharacters(state) ||
      state.eat(0x2E /* . */) ||
      this.regexp_eatReverseSolidusAtomEscape(state) ||
      this.regexp_eatCharacterClass(state) ||
      this.regexp_eatUncapturingGroup(state) ||
      this.regexp_eatCapturingGroup(state)
    )
  };
  pp$1.regexp_eatReverseSolidusAtomEscape = function(state) {
    var start = state.pos;
    if (state.eat(0x5C /* \ */)) {
      if (this.regexp_eatAtomEscape(state)) {
        return true
      }
      state.pos = start;
    }
    return false
  };
  pp$1.regexp_eatUncapturingGroup = function(state) {
    var start = state.pos;
    if (state.eat(0x28 /* ( */)) {
      if (state.eat(0x3F /* ? */)) {
        if (this.options.ecmaVersion >= 16) {
          var addModifiers = this.regexp_eatModifiers(state);
          var hasHyphen = state.eat(0x2D /* - */);
          if (addModifiers || hasHyphen) {
            for (var i = 0; i < addModifiers.length; i++) {
              var modifier = addModifiers.charAt(i);
              if (addModifiers.indexOf(modifier, i + 1) > -1) {
                state.raise("Duplicate regular expression modifiers");
              }
            }
            if (hasHyphen) {
              var removeModifiers = this.regexp_eatModifiers(state);
              if (!addModifiers && !removeModifiers && state.current() === 0x3A /* : */) {
                state.raise("Invalid regular expression modifiers");
              }
              for (var i$1 = 0; i$1 < removeModifiers.length; i$1++) {
                var modifier$1 = removeModifiers.charAt(i$1);
                if (
                  removeModifiers.indexOf(modifier$1, i$1 + 1) > -1 ||
                  addModifiers.indexOf(modifier$1) > -1
                ) {
                  state.raise("Duplicate regular expression modifiers");
                }
              }
            }
          }
        }
        if (state.eat(0x3A /* : */)) {
          this.regexp_disjunction(state);
          if (state.eat(0x29 /* ) */)) {
            return true
          }
          state.raise("Unterminated group");
        }
      }
      state.pos = start;
    }
    return false
  };
  pp$1.regexp_eatCapturingGroup = function(state) {
    if (state.eat(0x28 /* ( */)) {
      if (this.options.ecmaVersion >= 9) {
        this.regexp_groupSpecifier(state);
      } else if (state.current() === 0x3F /* ? */) {
        state.raise("Invalid group");
      }
      this.regexp_disjunction(state);
      if (state.eat(0x29 /* ) */)) {
        state.numCapturingParens += 1;
        return true
      }
      state.raise("Unterminated group");
    }
    return false
  };
  // RegularExpressionModifiers ::
  //   [empty]
  //   RegularExpressionModifiers RegularExpressionModifier
  pp$1.regexp_eatModifiers = function(state) {
    var modifiers = "";
    var ch = 0;
    while ((ch = state.current()) !== -1 && isRegularExpressionModifier(ch)) {
      modifiers += codePointToString(ch);
      state.advance();
    }
    return modifiers
  };
  // RegularExpressionModifier :: one of
  //   `i` `m` `s`
  function isRegularExpressionModifier(ch) {
    return ch === 0x69 /* i */ || ch === 0x6d /* m */ || ch === 0x73 /* s */
  }

  // https://www.ecma-international.org/ecma-262/8.0/#prod-annexB-ExtendedAtom
  pp$1.regexp_eatExtendedAtom = function(state) {
    return (
      state.eat(0x2E /* . */) ||
      this.regexp_eatReverseSolidusAtomEscape(state) ||
      this.regexp_eatCharacterClass(state) ||
      this.regexp_eatUncapturingGroup(state) ||
      this.regexp_eatCapturingGroup(state) ||
      this.regexp_eatInvalidBracedQuantifier(state) ||
      this.regexp_eatExtendedPatternCharacter(state)
    )
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-annexB-InvalidBracedQuantifier
  pp$1.regexp_eatInvalidBracedQuantifier = function(state) {
    if (this.regexp_eatBracedQuantifier(state, true)) {
      state.raise("Nothing to repeat");
    }
    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-SyntaxCharacter
  pp$1.regexp_eatSyntaxCharacter = function(state) {
    var ch = state.current();
    if (isSyntaxCharacter(ch)) {
      state.lastIntValue = ch;
      state.advance();
      return true
    }
    return false
  };
  function isSyntaxCharacter(ch) {
    return (
      ch === 0x24 /* $ */ ||
      ch >= 0x28 /* ( */ && ch <= 0x2B /* + */ ||
      ch === 0x2E /* . */ ||
      ch === 0x3F /* ? */ ||
      ch >= 0x5B /* [ */ && ch <= 0x5E /* ^ */ ||
      ch >= 0x7B /* { */ && ch <= 0x7D /* } */
    )
  }

  // https://www.ecma-international.org/ecma-262/8.0/#prod-PatternCharacter
  // But eat eager.
  pp$1.regexp_eatPatternCharacters = function(state) {
    var start = state.pos;
    var ch = 0;
    while ((ch = state.current()) !== -1 && !isSyntaxCharacter(ch)) {
      state.advance();
    }
    return state.pos !== start
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-annexB-ExtendedPatternCharacter
  pp$1.regexp_eatExtendedPatternCharacter = function(state) {
    var ch = state.current();
    if (
      ch !== -1 &&
      ch !== 0x24 /* $ */ &&
      !(ch >= 0x28 /* ( */ && ch <= 0x2B /* + */) &&
      ch !== 0x2E /* . */ &&
      ch !== 0x3F /* ? */ &&
      ch !== 0x5B /* [ */ &&
      ch !== 0x5E /* ^ */ &&
      ch !== 0x7C /* | */
    ) {
      state.advance();
      return true
    }
    return false
  };

  // GroupSpecifier ::
  //   [empty]
  //   `?` GroupName
  pp$1.regexp_groupSpecifier = function(state) {
    if (state.eat(0x3F /* ? */)) {
      if (!this.regexp_eatGroupName(state)) { state.raise("Invalid group"); }
      var trackDisjunction = this.options.ecmaVersion >= 16;
      var known = state.groupNames[state.lastStringValue];
      if (known) {
        if (trackDisjunction) {
          for (var i = 0, list = known; i < list.length; i += 1) {
            var altID = list[i];

            if (!altID.separatedFrom(state.branchID))
              { state.raise("Duplicate capture group name"); }
          }
        } else {
          state.raise("Duplicate capture group name");
        }
      }
      if (trackDisjunction) {
        (known || (state.groupNames[state.lastStringValue] = [])).push(state.branchID);
      } else {
        state.groupNames[state.lastStringValue] = true;
      }
    }
  };

  // GroupName ::
  //   `<` RegExpIdentifierName `>`
  // Note: this updates `state.lastStringValue` property with the eaten name.
  pp$1.regexp_eatGroupName = function(state) {
    state.lastStringValue = "";
    if (state.eat(0x3C /* < */)) {
      if (this.regexp_eatRegExpIdentifierName(state) && state.eat(0x3E /* > */)) {
        return true
      }
      state.raise("Invalid capture group name");
    }
    return false
  };

  // RegExpIdentifierName ::
  //   RegExpIdentifierStart
  //   RegExpIdentifierName RegExpIdentifierPart
  // Note: this updates `state.lastStringValue` property with the eaten name.
  pp$1.regexp_eatRegExpIdentifierName = function(state) {
    state.lastStringValue = "";
    if (this.regexp_eatRegExpIdentifierStart(state)) {
      state.lastStringValue += codePointToString(state.lastIntValue);
      while (this.regexp_eatRegExpIdentifierPart(state)) {
        state.lastStringValue += codePointToString(state.lastIntValue);
      }
      return true
    }
    return false
  };

  // RegExpIdentifierStart ::
  //   UnicodeIDStart
  //   `$`
  //   `_`
  //   `\` RegExpUnicodeEscapeSequence[+U]
  pp$1.regexp_eatRegExpIdentifierStart = function(state) {
    var start = state.pos;
    var forceU = this.options.ecmaVersion >= 11;
    var ch = state.current(forceU);
    state.advance(forceU);

    if (ch === 0x5C /* \ */ && this.regexp_eatRegExpUnicodeEscapeSequence(state, forceU)) {
      ch = state.lastIntValue;
    }
    if (isRegExpIdentifierStart(ch)) {
      state.lastIntValue = ch;
      return true
    }

    state.pos = start;
    return false
  };
  function isRegExpIdentifierStart(ch) {
    return isIdentifierStart(ch, true) || ch === 0x24 /* $ */ || ch === 0x5F /* _ */
  }

  // RegExpIdentifierPart ::
  //   UnicodeIDContinue
  //   `$`
  //   `_`
  //   `\` RegExpUnicodeEscapeSequence[+U]
  //   <ZWNJ>
  //   <ZWJ>
  pp$1.regexp_eatRegExpIdentifierPart = function(state) {
    var start = state.pos;
    var forceU = this.options.ecmaVersion >= 11;
    var ch = state.current(forceU);
    state.advance(forceU);

    if (ch === 0x5C /* \ */ && this.regexp_eatRegExpUnicodeEscapeSequence(state, forceU)) {
      ch = state.lastIntValue;
    }
    if (isRegExpIdentifierPart(ch)) {
      state.lastIntValue = ch;
      return true
    }

    state.pos = start;
    return false
  };
  function isRegExpIdentifierPart(ch) {
    return isIdentifierChar(ch, true) || ch === 0x24 /* $ */ || ch === 0x5F /* _ */ || ch === 0x200C /* <ZWNJ> */ || ch === 0x200D /* <ZWJ> */
  }

  // https://www.ecma-international.org/ecma-262/8.0/#prod-annexB-AtomEscape
  pp$1.regexp_eatAtomEscape = function(state) {
    if (
      this.regexp_eatBackReference(state) ||
      this.regexp_eatCharacterClassEscape(state) ||
      this.regexp_eatCharacterEscape(state) ||
      (state.switchN && this.regexp_eatKGroupName(state))
    ) {
      return true
    }
    if (state.switchU) {
      // Make the same message as V8.
      if (state.current() === 0x63 /* c */) {
        state.raise("Invalid unicode escape");
      }
      state.raise("Invalid escape");
    }
    return false
  };
  pp$1.regexp_eatBackReference = function(state) {
    var start = state.pos;
    if (this.regexp_eatDecimalEscape(state)) {
      var n = state.lastIntValue;
      if (state.switchU) {
        // For SyntaxError in https://www.ecma-international.org/ecma-262/8.0/#sec-atomescape
        if (n > state.maxBackReference) {
          state.maxBackReference = n;
        }
        return true
      }
      if (n <= state.numCapturingParens) {
        return true
      }
      state.pos = start;
    }
    return false
  };
  pp$1.regexp_eatKGroupName = function(state) {
    if (state.eat(0x6B /* k */)) {
      if (this.regexp_eatGroupName(state)) {
        state.backReferenceNames.push(state.lastStringValue);
        return true
      }
      state.raise("Invalid named reference");
    }
    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-annexB-CharacterEscape
  pp$1.regexp_eatCharacterEscape = function(state) {
    return (
      this.regexp_eatControlEscape(state) ||
      this.regexp_eatCControlLetter(state) ||
      this.regexp_eatZero(state) ||
      this.regexp_eatHexEscapeSequence(state) ||
      this.regexp_eatRegExpUnicodeEscapeSequence(state, false) ||
      (!state.switchU && this.regexp_eatLegacyOctalEscapeSequence(state)) ||
      this.regexp_eatIdentityEscape(state)
    )
  };
  pp$1.regexp_eatCControlLetter = function(state) {
    var start = state.pos;
    if (state.eat(0x63 /* c */)) {
      if (this.regexp_eatControlLetter(state)) {
        return true
      }
      state.pos = start;
    }
    return false
  };
  pp$1.regexp_eatZero = function(state) {
    if (state.current() === 0x30 /* 0 */ && !isDecimalDigit(state.lookahead())) {
      state.lastIntValue = 0;
      state.advance();
      return true
    }
    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-ControlEscape
  pp$1.regexp_eatControlEscape = function(state) {
    var ch = state.current();
    if (ch === 0x74 /* t */) {
      state.lastIntValue = 0x09; /* \t */
      state.advance();
      return true
    }
    if (ch === 0x6E /* n */) {
      state.lastIntValue = 0x0A; /* \n */
      state.advance();
      return true
    }
    if (ch === 0x76 /* v */) {
      state.lastIntValue = 0x0B; /* \v */
      state.advance();
      return true
    }
    if (ch === 0x66 /* f */) {
      state.lastIntValue = 0x0C; /* \f */
      state.advance();
      return true
    }
    if (ch === 0x72 /* r */) {
      state.lastIntValue = 0x0D; /* \r */
      state.advance();
      return true
    }
    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-ControlLetter
  pp$1.regexp_eatControlLetter = function(state) {
    var ch = state.current();
    if (isControlLetter(ch)) {
      state.lastIntValue = ch % 0x20;
      state.advance();
      return true
    }
    return false
  };
  function isControlLetter(ch) {
    return (
      (ch >= 0x41 /* A */ && ch <= 0x5A /* Z */) ||
      (ch >= 0x61 /* a */ && ch <= 0x7A /* z */)
    )
  }

  // https://www.ecma-international.org/ecma-262/8.0/#prod-RegExpUnicodeEscapeSequence
  pp$1.regexp_eatRegExpUnicodeEscapeSequence = function(state, forceU) {
    if ( forceU === void 0 ) forceU = false;

    var start = state.pos;
    var switchU = forceU || state.switchU;

    if (state.eat(0x75 /* u */)) {
      if (this.regexp_eatFixedHexDigits(state, 4)) {
        var lead = state.lastIntValue;
        if (switchU && lead >= 0xD800 && lead <= 0xDBFF) {
          var leadSurrogateEnd = state.pos;
          if (state.eat(0x5C /* \ */) && state.eat(0x75 /* u */) && this.regexp_eatFixedHexDigits(state, 4)) {
            var trail = state.lastIntValue;
            if (trail >= 0xDC00 && trail <= 0xDFFF) {
              state.lastIntValue = (lead - 0xD800) * 0x400 + (trail - 0xDC00) + 0x10000;
              return true
            }
          }
          state.pos = leadSurrogateEnd;
          state.lastIntValue = lead;
        }
        return true
      }
      if (
        switchU &&
        state.eat(0x7B /* { */) &&
        this.regexp_eatHexDigits(state) &&
        state.eat(0x7D /* } */) &&
        isValidUnicode(state.lastIntValue)
      ) {
        return true
      }
      if (switchU) {
        state.raise("Invalid unicode escape");
      }
      state.pos = start;
    }

    return false
  };
  function isValidUnicode(ch) {
    return ch >= 0 && ch <= 0x10FFFF
  }

  // https://www.ecma-international.org/ecma-262/8.0/#prod-annexB-IdentityEscape
  pp$1.regexp_eatIdentityEscape = function(state) {
    if (state.switchU) {
      if (this.regexp_eatSyntaxCharacter(state)) {
        return true
      }
      if (state.eat(0x2F /* / */)) {
        state.lastIntValue = 0x2F; /* / */
        return true
      }
      return false
    }

    var ch = state.current();
    if (ch !== 0x63 /* c */ && (!state.switchN || ch !== 0x6B /* k */)) {
      state.lastIntValue = ch;
      state.advance();
      return true
    }

    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-DecimalEscape
  pp$1.regexp_eatDecimalEscape = function(state) {
    state.lastIntValue = 0;
    var ch = state.current();
    if (ch >= 0x31 /* 1 */ && ch <= 0x39 /* 9 */) {
      do {
        state.lastIntValue = 10 * state.lastIntValue + (ch - 0x30 /* 0 */);
        state.advance();
      } while ((ch = state.current()) >= 0x30 /* 0 */ && ch <= 0x39 /* 9 */)
      return true
    }
    return false
  };

  // Return values used by character set parsing methods, needed to
  // forbid negation of sets that can match strings.
  var CharSetNone = 0; // Nothing parsed
  var CharSetOk = 1; // Construct parsed, cannot contain strings
  var CharSetString = 2; // Construct parsed, can contain strings

  // https://www.ecma-international.org/ecma-262/8.0/#prod-CharacterClassEscape
  pp$1.regexp_eatCharacterClassEscape = function(state) {
    var ch = state.current();

    if (isCharacterClassEscape(ch)) {
      state.lastIntValue = -1;
      state.advance();
      return CharSetOk
    }

    var negate = false;
    if (
      state.switchU &&
      this.options.ecmaVersion >= 9 &&
      ((negate = ch === 0x50 /* P */) || ch === 0x70 /* p */)
    ) {
      state.lastIntValue = -1;
      state.advance();
      var result;
      if (
        state.eat(0x7B /* { */) &&
        (result = this.regexp_eatUnicodePropertyValueExpression(state)) &&
        state.eat(0x7D /* } */)
      ) {
        if (negate && result === CharSetString) { state.raise("Invalid property name"); }
        return result
      }
      state.raise("Invalid property name");
    }

    return CharSetNone
  };

  function isCharacterClassEscape(ch) {
    return (
      ch === 0x64 /* d */ ||
      ch === 0x44 /* D */ ||
      ch === 0x73 /* s */ ||
      ch === 0x53 /* S */ ||
      ch === 0x77 /* w */ ||
      ch === 0x57 /* W */
    )
  }

  // UnicodePropertyValueExpression ::
  //   UnicodePropertyName `=` UnicodePropertyValue
  //   LoneUnicodePropertyNameOrValue
  pp$1.regexp_eatUnicodePropertyValueExpression = function(state) {
    var start = state.pos;

    // UnicodePropertyName `=` UnicodePropertyValue
    if (this.regexp_eatUnicodePropertyName(state) && state.eat(0x3D /* = */)) {
      var name = state.lastStringValue;
      if (this.regexp_eatUnicodePropertyValue(state)) {
        var value = state.lastStringValue;
        this.regexp_validateUnicodePropertyNameAndValue(state, name, value);
        return CharSetOk
      }
    }
    state.pos = start;

    // LoneUnicodePropertyNameOrValue
    if (this.regexp_eatLoneUnicodePropertyNameOrValue(state)) {
      var nameOrValue = state.lastStringValue;
      return this.regexp_validateUnicodePropertyNameOrValue(state, nameOrValue)
    }
    return CharSetNone
  };

  pp$1.regexp_validateUnicodePropertyNameAndValue = function(state, name, value) {
    if (!hasOwn(state.unicodeProperties.nonBinary, name))
      { state.raise("Invalid property name"); }
    if (!state.unicodeProperties.nonBinary[name].test(value))
      { state.raise("Invalid property value"); }
  };

  pp$1.regexp_validateUnicodePropertyNameOrValue = function(state, nameOrValue) {
    if (state.unicodeProperties.binary.test(nameOrValue)) { return CharSetOk }
    if (state.switchV && state.unicodeProperties.binaryOfStrings.test(nameOrValue)) { return CharSetString }
    state.raise("Invalid property name");
  };

  // UnicodePropertyName ::
  //   UnicodePropertyNameCharacters
  pp$1.regexp_eatUnicodePropertyName = function(state) {
    var ch = 0;
    state.lastStringValue = "";
    while (isUnicodePropertyNameCharacter(ch = state.current())) {
      state.lastStringValue += codePointToString(ch);
      state.advance();
    }
    return state.lastStringValue !== ""
  };

  function isUnicodePropertyNameCharacter(ch) {
    return isControlLetter(ch) || ch === 0x5F /* _ */
  }

  // UnicodePropertyValue ::
  //   UnicodePropertyValueCharacters
  pp$1.regexp_eatUnicodePropertyValue = function(state) {
    var ch = 0;
    state.lastStringValue = "";
    while (isUnicodePropertyValueCharacter(ch = state.current())) {
      state.lastStringValue += codePointToString(ch);
      state.advance();
    }
    return state.lastStringValue !== ""
  };
  function isUnicodePropertyValueCharacter(ch) {
    return isUnicodePropertyNameCharacter(ch) || isDecimalDigit(ch)
  }

  // LoneUnicodePropertyNameOrValue ::
  //   UnicodePropertyValueCharacters
  pp$1.regexp_eatLoneUnicodePropertyNameOrValue = function(state) {
    return this.regexp_eatUnicodePropertyValue(state)
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-CharacterClass
  pp$1.regexp_eatCharacterClass = function(state) {
    if (state.eat(0x5B /* [ */)) {
      var negate = state.eat(0x5E /* ^ */);
      var result = this.regexp_classContents(state);
      if (!state.eat(0x5D /* ] */))
        { state.raise("Unterminated character class"); }
      if (negate && result === CharSetString)
        { state.raise("Negated character class may contain strings"); }
      return true
    }
    return false
  };

  // https://tc39.es/ecma262/#prod-ClassContents
  // https://www.ecma-international.org/ecma-262/8.0/#prod-ClassRanges
  pp$1.regexp_classContents = function(state) {
    if (state.current() === 0x5D /* ] */) { return CharSetOk }
    if (state.switchV) { return this.regexp_classSetExpression(state) }
    this.regexp_nonEmptyClassRanges(state);
    return CharSetOk
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-NonemptyClassRanges
  // https://www.ecma-international.org/ecma-262/8.0/#prod-NonemptyClassRangesNoDash
  pp$1.regexp_nonEmptyClassRanges = function(state) {
    while (this.regexp_eatClassAtom(state)) {
      var left = state.lastIntValue;
      if (state.eat(0x2D /* - */) && this.regexp_eatClassAtom(state)) {
        var right = state.lastIntValue;
        if (state.switchU && (left === -1 || right === -1)) {
          state.raise("Invalid character class");
        }
        if (left !== -1 && right !== -1 && left > right) {
          state.raise("Range out of order in character class");
        }
      }
    }
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-ClassAtom
  // https://www.ecma-international.org/ecma-262/8.0/#prod-ClassAtomNoDash
  pp$1.regexp_eatClassAtom = function(state) {
    var start = state.pos;

    if (state.eat(0x5C /* \ */)) {
      if (this.regexp_eatClassEscape(state)) {
        return true
      }
      if (state.switchU) {
        // Make the same message as V8.
        var ch$1 = state.current();
        if (ch$1 === 0x63 /* c */ || isOctalDigit(ch$1)) {
          state.raise("Invalid class escape");
        }
        state.raise("Invalid escape");
      }
      state.pos = start;
    }

    var ch = state.current();
    if (ch !== 0x5D /* ] */) {
      state.lastIntValue = ch;
      state.advance();
      return true
    }

    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-annexB-ClassEscape
  pp$1.regexp_eatClassEscape = function(state) {
    var start = state.pos;

    if (state.eat(0x62 /* b */)) {
      state.lastIntValue = 0x08; /* <BS> */
      return true
    }

    if (state.switchU && state.eat(0x2D /* - */)) {
      state.lastIntValue = 0x2D; /* - */
      return true
    }

    if (!state.switchU && state.eat(0x63 /* c */)) {
      if (this.regexp_eatClassControlLetter(state)) {
        return true
      }
      state.pos = start;
    }

    return (
      this.regexp_eatCharacterClassEscape(state) ||
      this.regexp_eatCharacterEscape(state)
    )
  };

  // https://tc39.es/ecma262/#prod-ClassSetExpression
  // https://tc39.es/ecma262/#prod-ClassUnion
  // https://tc39.es/ecma262/#prod-ClassIntersection
  // https://tc39.es/ecma262/#prod-ClassSubtraction
  pp$1.regexp_classSetExpression = function(state) {
    var result = CharSetOk, subResult;
    if (this.regexp_eatClassSetRange(state)) ; else if (subResult = this.regexp_eatClassSetOperand(state)) {
      if (subResult === CharSetString) { result = CharSetString; }
      // https://tc39.es/ecma262/#prod-ClassIntersection
      var start = state.pos;
      while (state.eatChars([0x26, 0x26] /* && */)) {
        if (
          state.current() !== 0x26 /* & */ &&
          (subResult = this.regexp_eatClassSetOperand(state))
        ) {
          if (subResult !== CharSetString) { result = CharSetOk; }
          continue
        }
        state.raise("Invalid character in character class");
      }
      if (start !== state.pos) { return result }
      // https://tc39.es/ecma262/#prod-ClassSubtraction
      while (state.eatChars([0x2D, 0x2D] /* -- */)) {
        if (this.regexp_eatClassSetOperand(state)) { continue }
        state.raise("Invalid character in character class");
      }
      if (start !== state.pos) { return result }
    } else {
      state.raise("Invalid character in character class");
    }
    // https://tc39.es/ecma262/#prod-ClassUnion
    for (;;) {
      if (this.regexp_eatClassSetRange(state)) { continue }
      subResult = this.regexp_eatClassSetOperand(state);
      if (!subResult) { return result }
      if (subResult === CharSetString) { result = CharSetString; }
    }
  };

  // https://tc39.es/ecma262/#prod-ClassSetRange
  pp$1.regexp_eatClassSetRange = function(state) {
    var start = state.pos;
    if (this.regexp_eatClassSetCharacter(state)) {
      var left = state.lastIntValue;
      if (state.eat(0x2D /* - */) && this.regexp_eatClassSetCharacter(state)) {
        var right = state.lastIntValue;
        if (left !== -1 && right !== -1 && left > right) {
          state.raise("Range out of order in character class");
        }
        return true
      }
      state.pos = start;
    }
    return false
  };

  // https://tc39.es/ecma262/#prod-ClassSetOperand
  pp$1.regexp_eatClassSetOperand = function(state) {
    if (this.regexp_eatClassSetCharacter(state)) { return CharSetOk }
    return this.regexp_eatClassStringDisjunction(state) || this.regexp_eatNestedClass(state)
  };

  // https://tc39.es/ecma262/#prod-NestedClass
  pp$1.regexp_eatNestedClass = function(state) {
    var start = state.pos;
    if (state.eat(0x5B /* [ */)) {
      var negate = state.eat(0x5E /* ^ */);
      var result = this.regexp_classContents(state);
      if (state.eat(0x5D /* ] */)) {
        if (negate && result === CharSetString) {
          state.raise("Negated character class may contain strings");
        }
        return result
      }
      state.pos = start;
    }
    if (state.eat(0x5C /* \ */)) {
      var result$1 = this.regexp_eatCharacterClassEscape(state);
      if (result$1) {
        return result$1
      }
      state.pos = start;
    }
    return null
  };

  // https://tc39.es/ecma262/#prod-ClassStringDisjunction
  pp$1.regexp_eatClassStringDisjunction = function(state) {
    var start = state.pos;
    if (state.eatChars([0x5C, 0x71] /* \q */)) {
      if (state.eat(0x7B /* { */)) {
        var result = this.regexp_classStringDisjunctionContents(state);
        if (state.eat(0x7D /* } */)) {
          return result
        }
      } else {
        // Make the same message as V8.
        state.raise("Invalid escape");
      }
      state.pos = start;
    }
    return null
  };

  // https://tc39.es/ecma262/#prod-ClassStringDisjunctionContents
  pp$1.regexp_classStringDisjunctionContents = function(state) {
    var result = this.regexp_classString(state);
    while (state.eat(0x7C /* | */)) {
      if (this.regexp_classString(state) === CharSetString) { result = CharSetString; }
    }
    return result
  };

  // https://tc39.es/ecma262/#prod-ClassString
  // https://tc39.es/ecma262/#prod-NonEmptyClassString
  pp$1.regexp_classString = function(state) {
    var count = 0;
    while (this.regexp_eatClassSetCharacter(state)) { count++; }
    return count === 1 ? CharSetOk : CharSetString
  };

  // https://tc39.es/ecma262/#prod-ClassSetCharacter
  pp$1.regexp_eatClassSetCharacter = function(state) {
    var start = state.pos;
    if (state.eat(0x5C /* \ */)) {
      if (
        this.regexp_eatCharacterEscape(state) ||
        this.regexp_eatClassSetReservedPunctuator(state)
      ) {
        return true
      }
      if (state.eat(0x62 /* b */)) {
        state.lastIntValue = 0x08; /* <BS> */
        return true
      }
      state.pos = start;
      return false
    }
    var ch = state.current();
    if (ch < 0 || ch === state.lookahead() && isClassSetReservedDoublePunctuatorCharacter(ch)) { return false }
    if (isClassSetSyntaxCharacter(ch)) { return false }
    state.advance();
    state.lastIntValue = ch;
    return true
  };

  // https://tc39.es/ecma262/#prod-ClassSetReservedDoublePunctuator
  function isClassSetReservedDoublePunctuatorCharacter(ch) {
    return (
      ch === 0x21 /* ! */ ||
      ch >= 0x23 /* # */ && ch <= 0x26 /* & */ ||
      ch >= 0x2A /* * */ && ch <= 0x2C /* , */ ||
      ch === 0x2E /* . */ ||
      ch >= 0x3A /* : */ && ch <= 0x40 /* @ */ ||
      ch === 0x5E /* ^ */ ||
      ch === 0x60 /* ` */ ||
      ch === 0x7E /* ~ */
    )
  }

  // https://tc39.es/ecma262/#prod-ClassSetSyntaxCharacter
  function isClassSetSyntaxCharacter(ch) {
    return (
      ch === 0x28 /* ( */ ||
      ch === 0x29 /* ) */ ||
      ch === 0x2D /* - */ ||
      ch === 0x2F /* / */ ||
      ch >= 0x5B /* [ */ && ch <= 0x5D /* ] */ ||
      ch >= 0x7B /* { */ && ch <= 0x7D /* } */
    )
  }

  // https://tc39.es/ecma262/#prod-ClassSetReservedPunctuator
  pp$1.regexp_eatClassSetReservedPunctuator = function(state) {
    var ch = state.current();
    if (isClassSetReservedPunctuator(ch)) {
      state.lastIntValue = ch;
      state.advance();
      return true
    }
    return false
  };

  // https://tc39.es/ecma262/#prod-ClassSetReservedPunctuator
  function isClassSetReservedPunctuator(ch) {
    return (
      ch === 0x21 /* ! */ ||
      ch === 0x23 /* # */ ||
      ch === 0x25 /* % */ ||
      ch === 0x26 /* & */ ||
      ch === 0x2C /* , */ ||
      ch === 0x2D /* - */ ||
      ch >= 0x3A /* : */ && ch <= 0x3E /* > */ ||
      ch === 0x40 /* @ */ ||
      ch === 0x60 /* ` */ ||
      ch === 0x7E /* ~ */
    )
  }

  // https://www.ecma-international.org/ecma-262/8.0/#prod-annexB-ClassControlLetter
  pp$1.regexp_eatClassControlLetter = function(state) {
    var ch = state.current();
    if (isDecimalDigit(ch) || ch === 0x5F /* _ */) {
      state.lastIntValue = ch % 0x20;
      state.advance();
      return true
    }
    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-HexEscapeSequence
  pp$1.regexp_eatHexEscapeSequence = function(state) {
    var start = state.pos;
    if (state.eat(0x78 /* x */)) {
      if (this.regexp_eatFixedHexDigits(state, 2)) {
        return true
      }
      if (state.switchU) {
        state.raise("Invalid escape");
      }
      state.pos = start;
    }
    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-DecimalDigits
  pp$1.regexp_eatDecimalDigits = function(state) {
    var start = state.pos;
    var ch = 0;
    state.lastIntValue = 0;
    while (isDecimalDigit(ch = state.current())) {
      state.lastIntValue = 10 * state.lastIntValue + (ch - 0x30 /* 0 */);
      state.advance();
    }
    return state.pos !== start
  };
  function isDecimalDigit(ch) {
    return ch >= 0x30 /* 0 */ && ch <= 0x39 /* 9 */
  }

  // https://www.ecma-international.org/ecma-262/8.0/#prod-HexDigits
  pp$1.regexp_eatHexDigits = function(state) {
    var start = state.pos;
    var ch = 0;
    state.lastIntValue = 0;
    while (isHexDigit(ch = state.current())) {
      state.lastIntValue = 16 * state.lastIntValue + hexToInt(ch);
      state.advance();
    }
    return state.pos !== start
  };
  function isHexDigit(ch) {
    return (
      (ch >= 0x30 /* 0 */ && ch <= 0x39 /* 9 */) ||
      (ch >= 0x41 /* A */ && ch <= 0x46 /* F */) ||
      (ch >= 0x61 /* a */ && ch <= 0x66 /* f */)
    )
  }
  function hexToInt(ch) {
    if (ch >= 0x41 /* A */ && ch <= 0x46 /* F */) {
      return 10 + (ch - 0x41 /* A */)
    }
    if (ch >= 0x61 /* a */ && ch <= 0x66 /* f */) {
      return 10 + (ch - 0x61 /* a */)
    }
    return ch - 0x30 /* 0 */
  }

  // https://www.ecma-international.org/ecma-262/8.0/#prod-annexB-LegacyOctalEscapeSequence
  // Allows only 0-377(octal) i.e. 0-255(decimal).
  pp$1.regexp_eatLegacyOctalEscapeSequence = function(state) {
    if (this.regexp_eatOctalDigit(state)) {
      var n1 = state.lastIntValue;
      if (this.regexp_eatOctalDigit(state)) {
        var n2 = state.lastIntValue;
        if (n1 <= 3 && this.regexp_eatOctalDigit(state)) {
          state.lastIntValue = n1 * 64 + n2 * 8 + state.lastIntValue;
        } else {
          state.lastIntValue = n1 * 8 + n2;
        }
      } else {
        state.lastIntValue = n1;
      }
      return true
    }
    return false
  };

  // https://www.ecma-international.org/ecma-262/8.0/#prod-OctalDigit
  pp$1.regexp_eatOctalDigit = function(state) {
    var ch = state.current();
    if (isOctalDigit(ch)) {
      state.lastIntValue = ch - 0x30; /* 0 */
      state.advance();
      return true
    }
    state.lastIntValue = 0;
    return false
  };
  function isOctalDigit(ch) {
    return ch >= 0x30 /* 0 */ && ch <= 0x37 /* 7 */
  }

  // https://www.ecma-international.org/ecma-262/8.0/#prod-Hex4Digits
  // https://www.ecma-international.org/ecma-262/8.0/#prod-HexDigit
  // And HexDigit HexDigit in https://www.ecma-international.org/ecma-262/8.0/#prod-HexEscapeSequence
  pp$1.regexp_eatFixedHexDigits = function(state, length) {
    var start = state.pos;
    state.lastIntValue = 0;
    for (var i = 0; i < length; ++i) {
      var ch = state.current();
      if (!isHexDigit(ch)) {
        state.pos = start;
        return false
      }
      state.lastIntValue = 16 * state.lastIntValue + hexToInt(ch);
      state.advance();
    }
    return true
  };

  // Object type used to represent tokens. Note that normally, tokens
  // simply exist as properties on the parser object. This is only
  // used for the onToken callback and the external tokenizer.

  var Token = function Token(p) {
    this.type = p.type;
    this.value = p.value;
    this.start = p.start;
    this.end = p.end;
    if (p.options.locations)
      { this.loc = new SourceLocation(p, p.startLoc, p.endLoc); }
    if (p.options.ranges)
      { this.range = [p.start, p.end]; }
  };

  // ## Tokenizer

  var pp = Parser.prototype;

  // Move to the next token

  pp.next = function(ignoreEscapeSequenceInKeyword) {
    if (!ignoreEscapeSequenceInKeyword && this.type.keyword && this.containsEsc)
      { this.raiseRecoverable(this.start, "Escape sequence in keyword " + this.type.keyword); }
    if (this.options.onToken)
      { this.options.onToken(new Token(this)); }

    this.lastTokEnd = this.end;
    this.lastTokStart = this.start;
    this.lastTokEndLoc = this.endLoc;
    this.lastTokStartLoc = this.startLoc;
    this.nextToken();
  };

  pp.getToken = function() {
    this.next();
    return new Token(this)
  };

  // If we're in an ES6 environment, make parsers iterable
  if (typeof Symbol !== "undefined")
    { pp[Symbol.iterator] = function() {
      var this$1$1 = this;

      return {
        next: function () {
          var token = this$1$1.getToken();
          return {
            done: token.type === types$1.eof,
            value: token
          }
        }
      }
    }; }

  // Toggle strict mode. Re-reads the next number or string to please
  // pedantic tests (`"use strict"; 010;` should fail).

  // Read a single token, updating the parser object's token-related
  // properties.

  pp.nextToken = function() {
    var curContext = this.curContext();
    if (!curContext || !curContext.preserveSpace) { this.skipSpace(); }

    this.start = this.pos;
    if (this.options.locations) { this.startLoc = this.curPosition(); }
    if (this.pos >= this.input.length) { return this.finishToken(types$1.eof) }

    if (curContext.override) { return curContext.override(this) }
    else { this.readToken(this.fullCharCodeAtPos()); }
  };

  pp.readToken = function(code) {
    // Identifier or keyword. '\uXXXX' sequences are allowed in
    // identifiers, so '\' also dispatches to that.
    if (isIdentifierStart(code, this.options.ecmaVersion >= 6) || code === 92 /* '\' */)
      { return this.readWord() }

    return this.getTokenFromCode(code)
  };

  pp.fullCharCodeAtPos = function() {
    var code = this.input.charCodeAt(this.pos);
    if (code <= 0xd7ff || code >= 0xdc00) { return code }
    var next = this.input.charCodeAt(this.pos + 1);
    return next <= 0xdbff || next >= 0xe000 ? code : (code << 10) + next - 0x35fdc00
  };

  pp.skipBlockComment = function() {
    var startLoc = this.options.onComment && this.curPosition();
    var start = this.pos, end = this.input.indexOf("*/", this.pos += 2);
    if (end === -1) { this.raise(this.pos - 2, "Unterminated comment"); }
    this.pos = end + 2;
    if (this.options.locations) {
      for (var nextBreak = (void 0), pos = start; (nextBreak = nextLineBreak(this.input, pos, this.pos)) > -1;) {
        ++this.curLine;
        pos = this.lineStart = nextBreak;
      }
    }
    if (this.options.onComment)
      { this.options.onComment(true, this.input.slice(start + 2, end), start, this.pos,
                             startLoc, this.curPosition()); }
  };

  pp.skipLineComment = function(startSkip) {
    var start = this.pos;
    var startLoc = this.options.onComment && this.curPosition();
    var ch = this.input.charCodeAt(this.pos += startSkip);
    while (this.pos < this.input.length && !isNewLine(ch)) {
      ch = this.input.charCodeAt(++this.pos);
    }
    if (this.options.onComment)
      { this.options.onComment(false, this.input.slice(start + startSkip, this.pos), start, this.pos,
                             startLoc, this.curPosition()); }
  };

  // Called at the start of the parse and after every token. Skips
  // whitespace and comments, and.

  pp.skipSpace = function() {
    loop: while (this.pos < this.input.length) {
      var ch = this.input.charCodeAt(this.pos);
      switch (ch) {
      case 32: case 160: // ' '
        ++this.pos;
        break
      case 13:
        if (this.input.charCodeAt(this.pos + 1) === 10) {
          ++this.pos;
        }
      case 10: case 8232: case 8233:
        ++this.pos;
        if (this.options.locations) {
          ++this.curLine;
          this.lineStart = this.pos;
        }
        break
      case 47: // '/'
        switch (this.input.charCodeAt(this.pos + 1)) {
        case 42: // '*'
          this.skipBlockComment();
          break
        case 47:
          this.skipLineComment(2);
          break
        default:
          break loop
        }
        break
      default:
        if (ch > 8 && ch < 14 || ch >= 5760 && nonASCIIwhitespace.test(String.fromCharCode(ch))) {
          ++this.pos;
        } else {
          break loop
        }
      }
    }
  };

  // Called at the end of every token. Sets `end`, `val`, and
  // maintains `context` and `exprAllowed`, and skips the space after
  // the token, so that the next one's `start` will point at the
  // right position.

  pp.finishToken = function(type, val) {
    this.end = this.pos;
    if (this.options.locations) { this.endLoc = this.curPosition(); }
    var prevType = this.type;
    this.type = type;
    this.value = val;

    this.updateContext(prevType);
  };

  // ### Token reading

  // This is the function that is called to fetch the next token. It
  // is somewhat obscure, because it works in character codes rather
  // than characters, and because operator parsing has been inlined
  // into it.
  //
  // All in the name of speed.
  //
  pp.readToken_dot = function() {
    var next = this.input.charCodeAt(this.pos + 1);
    if (next >= 48 && next <= 57) { return this.readNumber(true) }
    var next2 = this.input.charCodeAt(this.pos + 2);
    if (this.options.ecmaVersion >= 6 && next === 46 && next2 === 46) { // 46 = dot '.'
      this.pos += 3;
      return this.finishToken(types$1.ellipsis)
    } else {
      ++this.pos;
      return this.finishToken(types$1.dot)
    }
  };

  pp.readToken_slash = function() { // '/'
    var next = this.input.charCodeAt(this.pos + 1);
    if (this.exprAllowed) { ++this.pos; return this.readRegexp() }
    if (next === 61) { return this.finishOp(types$1.assign, 2) }
    return this.finishOp(types$1.slash, 1)
  };

  pp.readToken_mult_modulo_exp = function(code) { // '%*'
    var next = this.input.charCodeAt(this.pos + 1);
    var size = 1;
    var tokentype = code === 42 ? types$1.star : types$1.modulo;

    // exponentiation operator ** and **=
    if (this.options.ecmaVersion >= 7 && code === 42 && next === 42) {
      ++size;
      tokentype = types$1.starstar;
      next = this.input.charCodeAt(this.pos + 2);
    }

    if (next === 61) { return this.finishOp(types$1.assign, size + 1) }
    return this.finishOp(tokentype, size)
  };

  pp.readToken_pipe_amp = function(code) { // '|&'
    var next = this.input.charCodeAt(this.pos + 1);
    if (next === code) {
      if (this.options.ecmaVersion >= 12) {
        var next2 = this.input.charCodeAt(this.pos + 2);
        if (next2 === 61) { return this.finishOp(types$1.assign, 3) }
      }
      return this.finishOp(code === 124 ? types$1.logicalOR : types$1.logicalAND, 2)
    }
    if (next === 61) { return this.finishOp(types$1.assign, 2) }
    return this.finishOp(code === 124 ? types$1.bitwiseOR : types$1.bitwiseAND, 1)
  };

  pp.readToken_caret = function() { // '^'
    var next = this.input.charCodeAt(this.pos + 1);
    if (next === 61) { return this.finishOp(types$1.assign, 2) }
    return this.finishOp(types$1.bitwiseXOR, 1)
  };

  pp.readToken_plus_min = function(code) { // '+-'
    var next = this.input.charCodeAt(this.pos + 1);
    if (next === code) {
      if (next === 45 && !this.inModule && this.input.charCodeAt(this.pos + 2) === 62 &&
          (this.lastTokEnd === 0 || lineBreak.test(this.input.slice(this.lastTokEnd, this.pos)))) {
        // A `-->` line comment
        this.skipLineComment(3);
        this.skipSpace();
        return this.nextToken()
      }
      return this.finishOp(types$1.incDec, 2)
    }
    if (next === 61) { return this.finishOp(types$1.assign, 2) }
    return this.finishOp(types$1.plusMin, 1)
  };

  pp.readToken_lt_gt = function(code) { // '<>'
    var next = this.input.charCodeAt(this.pos + 1);
    var size = 1;
    if (next === code) {
      size = code === 62 && this.input.charCodeAt(this.pos + 2) === 62 ? 3 : 2;
      if (this.input.charCodeAt(this.pos + size) === 61) { return this.finishOp(types$1.assign, size + 1) }
      return this.finishOp(types$1.bitShift, size)
    }
    if (next === 33 && code === 60 && !this.inModule && this.input.charCodeAt(this.pos + 2) === 45 &&
        this.input.charCodeAt(this.pos + 3) === 45) {
      // `<!--`, an XML-style comment that should be interpreted as a line comment
      this.skipLineComment(4);
      this.skipSpace();
      return this.nextToken()
    }
    if (next === 61) { size = 2; }
    return this.finishOp(types$1.relational, size)
  };

  pp.readToken_eq_excl = function(code) { // '=!'
    var next = this.input.charCodeAt(this.pos + 1);
    if (next === 61) { return this.finishOp(types$1.equality, this.input.charCodeAt(this.pos + 2) === 61 ? 3 : 2) }
    if (code === 61 && next === 62 && this.options.ecmaVersion >= 6) { // '=>'
      this.pos += 2;
      return this.finishToken(types$1.arrow)
    }
    return this.finishOp(code === 61 ? types$1.eq : types$1.prefix, 1)
  };

  pp.readToken_question = function() { // '?'
    var ecmaVersion = this.options.ecmaVersion;
    if (ecmaVersion >= 11) {
      var next = this.input.charCodeAt(this.pos + 1);
      if (next === 46) {
        var next2 = this.input.charCodeAt(this.pos + 2);
        if (next2 < 48 || next2 > 57) { return this.finishOp(types$1.questionDot, 2) }
      }
      if (next === 63) {
        if (ecmaVersion >= 12) {
          var next2$1 = this.input.charCodeAt(this.pos + 2);
          if (next2$1 === 61) { return this.finishOp(types$1.assign, 3) }
        }
        return this.finishOp(types$1.coalesce, 2)
      }
    }
    return this.finishOp(types$1.question, 1)
  };

  pp.readToken_numberSign = function() { // '#'
    var ecmaVersion = this.options.ecmaVersion;
    var code = 35; // '#'
    if (ecmaVersion >= 13) {
      ++this.pos;
      code = this.fullCharCodeAtPos();
      if (isIdentifierStart(code, true) || code === 92 /* '\' */) {
        return this.finishToken(types$1.privateId, this.readWord1())
      }
    }

    this.raise(this.pos, "Unexpected character '" + codePointToString(code) + "'");
  };

  pp.getTokenFromCode = function(code) {
    switch (code) {
    // The interpretation of a dot depends on whether it is followed
    // by a digit or another two dots.
    case 46: // '.'
      return this.readToken_dot()

    // Punctuation tokens.
    case 40: ++this.pos; return this.finishToken(types$1.parenL)
    case 41: ++this.pos; return this.finishToken(types$1.parenR)
    case 59: ++this.pos; return this.finishToken(types$1.semi)
    case 44: ++this.pos; return this.finishToken(types$1.comma)
    case 91: ++this.pos; return this.finishToken(types$1.bracketL)
    case 93: ++this.pos; return this.finishToken(types$1.bracketR)
    case 123: ++this.pos; return this.finishToken(types$1.braceL)
    case 125: ++this.pos; return this.finishToken(types$1.braceR)
    case 58: ++this.pos; return this.finishToken(types$1.colon)

    case 96: // '`'
      if (this.options.ecmaVersion < 6) { break }
      ++this.pos;
      return this.finishToken(types$1.backQuote)

    case 48: // '0'
      var next = this.input.charCodeAt(this.pos + 1);
      if (next === 120 || next === 88) { return this.readRadixNumber(16) } // '0x', '0X' - hex number
      if (this.options.ecmaVersion >= 6) {
        if (next === 111 || next === 79) { return this.readRadixNumber(8) } // '0o', '0O' - octal number
        if (next === 98 || next === 66) { return this.readRadixNumber(2) } // '0b', '0B' - binary number
      }

    // Anything else beginning with a digit is an integer, octal
    // number, or float.
    case 49: case 50: case 51: case 52: case 53: case 54: case 55: case 56: case 57: // 1-9
      return this.readNumber(false)

    // Quotes produce strings.
    case 34: case 39: // '"', "'"
      return this.readString(code)

    // Operators are parsed inline in tiny state machines. '=' (61) is
    // often referred to. `finishOp` simply skips the amount of
    // characters it is given as second argument, and returns a token
    // of the type given by its first argument.
    case 47: // '/'
      return this.readToken_slash()

    case 37: case 42: // '%*'
      return this.readToken_mult_modulo_exp(code)

    case 124: case 38: // '|&'
      return this.readToken_pipe_amp(code)

    case 94: // '^'
      return this.readToken_caret()

    case 43: case 45: // '+-'
      return this.readToken_plus_min(code)

    case 60: case 62: // '<>'
      return this.readToken_lt_gt(code)

    case 61: case 33: // '=!'
      return this.readToken_eq_excl(code)

    case 63: // '?'
      return this.readToken_question()

    case 126: // '~'
      return this.finishOp(types$1.prefix, 1)

    case 35: // '#'
      return this.readToken_numberSign()
    }

    this.raise(this.pos, "Unexpected character '" + codePointToString(code) + "'");
  };

  pp.finishOp = function(type, size) {
    var str = this.input.slice(this.pos, this.pos + size);
    this.pos += size;
    return this.finishToken(type, str)
  };

  pp.readRegexp = function() {
    var escaped, inClass, start = this.pos;
    for (;;) {
      if (this.pos >= this.input.length) { this.raise(start, "Unterminated regular expression"); }
      var ch = this.input.charAt(this.pos);
      if (lineBreak.test(ch)) { this.raise(start, "Unterminated regular expression"); }
      if (!escaped) {
        if (ch === "[") { inClass = true; }
        else if (ch === "]" && inClass) { inClass = false; }
        else if (ch === "/" && !inClass) { break }
        escaped = ch === "\\";
      } else { escaped = false; }
      ++this.pos;
    }
    var pattern = this.input.slice(start, this.pos);
    ++this.pos;
    var flagsStart = this.pos;
    var flags = this.readWord1();
    if (this.containsEsc) { this.unexpected(flagsStart); }

    // Validate pattern
    var state = this.regexpState || (this.regexpState = new RegExpValidationState(this));
    state.reset(start, pattern, flags);
    this.validateRegExpFlags(state);
    this.validateRegExpPattern(state);

    // Create Literal#value property value.
    var value = null;
    try {
      value = new RegExp(pattern, flags);
    } catch (e) {
      // ESTree requires null if it failed to instantiate RegExp object.
      // https://github.com/estree/estree/blob/a27003adf4fd7bfad44de9cef372a2eacd527b1c/es5.md#regexpliteral
    }

    return this.finishToken(types$1.regexp, {pattern: pattern, flags: flags, value: value})
  };

  // Read an integer in the given radix. Return null if zero digits
  // were read, the integer value otherwise. When `len` is given, this
  // will return `null` unless the integer has exactly `len` digits.

  pp.readInt = function(radix, len, maybeLegacyOctalNumericLiteral) {
    // `len` is used for character escape sequences. In that case, disallow separators.
    var allowSeparators = this.options.ecmaVersion >= 12 && len === undefined;

    // `maybeLegacyOctalNumericLiteral` is true if it doesn't have prefix (0x,0o,0b)
    // and isn't fraction part nor exponent part. In that case, if the first digit
    // is zero then disallow separators.
    var isLegacyOctalNumericLiteral = maybeLegacyOctalNumericLiteral && this.input.charCodeAt(this.pos) === 48;

    var start = this.pos, total = 0, lastCode = 0;
    for (var i = 0, e = len == null ? Infinity : len; i < e; ++i, ++this.pos) {
      var code = this.input.charCodeAt(this.pos), val = (void 0);

      if (allowSeparators && code === 95) {
        if (isLegacyOctalNumericLiteral) { this.raiseRecoverable(this.pos, "Numeric separator is not allowed in legacy octal numeric literals"); }
        if (lastCode === 95) { this.raiseRecoverable(this.pos, "Numeric separator must be exactly one underscore"); }
        if (i === 0) { this.raiseRecoverable(this.pos, "Numeric separator is not allowed at the first of digits"); }
        lastCode = code;
        continue
      }

      if (code >= 97) { val = code - 97 + 10; } // a
      else if (code >= 65) { val = code - 65 + 10; } // A
      else if (code >= 48 && code <= 57) { val = code - 48; } // 0-9
      else { val = Infinity; }
      if (val >= radix) { break }
      lastCode = code;
      total = total * radix + val;
    }

    if (allowSeparators && lastCode === 95) { this.raiseRecoverable(this.pos - 1, "Numeric separator is not allowed at the last of digits"); }
    if (this.pos === start || len != null && this.pos - start !== len) { return null }

    return total
  };

  function stringToNumber(str, isLegacyOctalNumericLiteral) {
    if (isLegacyOctalNumericLiteral) {
      return parseInt(str, 8)
    }

    // `parseFloat(value)` stops parsing at the first numeric separator then returns a wrong value.
    return parseFloat(str.replace(/_/g, ""))
  }

  function stringToBigInt(str) {
    if (typeof BigInt !== "function") {
      return null
    }

    // `BigInt(value)` throws syntax error if the string contains numeric separators.
    return BigInt(str.replace(/_/g, ""))
  }

  pp.readRadixNumber = function(radix) {
    var start = this.pos;
    this.pos += 2; // 0x
    var val = this.readInt(radix);
    if (val == null) { this.raise(this.start + 2, "Expected number in radix " + radix); }
    if (this.options.ecmaVersion >= 11 && this.input.charCodeAt(this.pos) === 110) {
      val = stringToBigInt(this.input.slice(start, this.pos));
      ++this.pos;
    } else if (isIdentifierStart(this.fullCharCodeAtPos())) { this.raise(this.pos, "Identifier directly after number"); }
    return this.finishToken(types$1.num, val)
  };

  // Read an integer, octal integer, or floating-point number.

  pp.readNumber = function(startsWithDot) {
    var start = this.pos;
    if (!startsWithDot && this.readInt(10, undefined, true) === null) { this.raise(start, "Invalid number"); }
    var octal = this.pos - start >= 2 && this.input.charCodeAt(start) === 48;
    if (octal && this.strict) { this.raise(start, "Invalid number"); }
    var next = this.input.charCodeAt(this.pos);
    if (!octal && !startsWithDot && this.options.ecmaVersion >= 11 && next === 110) {
      var val$1 = stringToBigInt(this.input.slice(start, this.pos));
      ++this.pos;
      if (isIdentifierStart(this.fullCharCodeAtPos())) { this.raise(this.pos, "Identifier directly after number"); }
      return this.finishToken(types$1.num, val$1)
    }
    if (octal && /[89]/.test(this.input.slice(start, this.pos))) { octal = false; }
    if (next === 46 && !octal) { // '.'
      ++this.pos;
      this.readInt(10);
      next = this.input.charCodeAt(this.pos);
    }
    if ((next === 69 || next === 101) && !octal) { // 'eE'
      next = this.input.charCodeAt(++this.pos);
      if (next === 43 || next === 45) { ++this.pos; } // '+-'
      if (this.readInt(10) === null) { this.raise(start, "Invalid number"); }
    }
    if (isIdentifierStart(this.fullCharCodeAtPos())) { this.raise(this.pos, "Identifier directly after number"); }

    var val = stringToNumber(this.input.slice(start, this.pos), octal);
    return this.finishToken(types$1.num, val)
  };

  // Read a string value, interpreting backslash-escapes.

  pp.readCodePoint = function() {
    var ch = this.input.charCodeAt(this.pos), code;

    if (ch === 123) { // '{'
      if (this.options.ecmaVersion < 6) { this.unexpected(); }
      var codePos = ++this.pos;
      code = this.readHexChar(this.input.indexOf("}", this.pos) - this.pos);
      ++this.pos;
      if (code > 0x10FFFF) { this.invalidStringToken(codePos, "Code point out of bounds"); }
    } else {
      code = this.readHexChar(4);
    }
    return code
  };

  pp.readString = function(quote) {
    var out = "", chunkStart = ++this.pos;
    for (;;) {
      if (this.pos >= this.input.length) { this.raise(this.start, "Unterminated string constant"); }
      var ch = this.input.charCodeAt(this.pos);
      if (ch === quote) { break }
      if (ch === 92) { // '\'
        out += this.input.slice(chunkStart, this.pos);
        out += this.readEscapedChar(false);
        chunkStart = this.pos;
      } else if (ch === 0x2028 || ch === 0x2029) {
        if (this.options.ecmaVersion < 10) { this.raise(this.start, "Unterminated string constant"); }
        ++this.pos;
        if (this.options.locations) {
          this.curLine++;
          this.lineStart = this.pos;
        }
      } else {
        if (isNewLine(ch)) { this.raise(this.start, "Unterminated string constant"); }
        ++this.pos;
      }
    }
    out += this.input.slice(chunkStart, this.pos++);
    return this.finishToken(types$1.string, out)
  };

  // Reads template string tokens.

  var INVALID_TEMPLATE_ESCAPE_ERROR = {};

  pp.tryReadTemplateToken = function() {
    this.inTemplateElement = true;
    try {
      this.readTmplToken();
    } catch (err) {
      if (err === INVALID_TEMPLATE_ESCAPE_ERROR) {
        this.readInvalidTemplateToken();
      } else {
        throw err
      }
    }

    this.inTemplateElement = false;
  };

  pp.invalidStringToken = function(position, message) {
    if (this.inTemplateElement && this.options.ecmaVersion >= 9) {
      throw INVALID_TEMPLATE_ESCAPE_ERROR
    } else {
      this.raise(position, message);
    }
  };

  pp.readTmplToken = function() {
    var out = "", chunkStart = this.pos;
    for (;;) {
      if (this.pos >= this.input.length) { this.raise(this.start, "Unterminated template"); }
      var ch = this.input.charCodeAt(this.pos);
      if (ch === 96 || ch === 36 && this.input.charCodeAt(this.pos + 1) === 123) { // '`', '${'
        if (this.pos === this.start && (this.type === types$1.template || this.type === types$1.invalidTemplate)) {
          if (ch === 36) {
            this.pos += 2;
            return this.finishToken(types$1.dollarBraceL)
          } else {
            ++this.pos;
            return this.finishToken(types$1.backQuote)
          }
        }
        out += this.input.slice(chunkStart, this.pos);
        return this.finishToken(types$1.template, out)
      }
      if (ch === 92) { // '\'
        out += this.input.slice(chunkStart, this.pos);
        out += this.readEscapedChar(true);
        chunkStart = this.pos;
      } else if (isNewLine(ch)) {
        out += this.input.slice(chunkStart, this.pos);
        ++this.pos;
        switch (ch) {
        case 13:
          if (this.input.charCodeAt(this.pos) === 10) { ++this.pos; }
        case 10:
          out += "\n";
          break
        default:
          out += String.fromCharCode(ch);
          break
        }
        if (this.options.locations) {
          ++this.curLine;
          this.lineStart = this.pos;
        }
        chunkStart = this.pos;
      } else {
        ++this.pos;
      }
    }
  };

  // Reads a template token to search for the end, without validating any escape sequences
  pp.readInvalidTemplateToken = function() {
    for (; this.pos < this.input.length; this.pos++) {
      switch (this.input[this.pos]) {
      case "\\":
        ++this.pos;
        break

      case "$":
        if (this.input[this.pos + 1] !== "{") { break }
        // fall through
      case "`":
        return this.finishToken(types$1.invalidTemplate, this.input.slice(this.start, this.pos))

      case "\r":
        if (this.input[this.pos + 1] === "\n") { ++this.pos; }
        // fall through
      case "\n": case "\u2028": case "\u2029":
        ++this.curLine;
        this.lineStart = this.pos + 1;
        break
      }
    }
    this.raise(this.start, "Unterminated template");
  };

  // Used to read escaped characters

  pp.readEscapedChar = function(inTemplate) {
    var ch = this.input.charCodeAt(++this.pos);
    ++this.pos;
    switch (ch) {
    case 110: return "\n" // 'n' -> '\n'
    case 114: return "\r" // 'r' -> '\r'
    case 120: return String.fromCharCode(this.readHexChar(2)) // 'x'
    case 117: return codePointToString(this.readCodePoint()) // 'u'
    case 116: return "\t" // 't' -> '\t'
    case 98: return "\b" // 'b' -> '\b'
    case 118: return "\u000b" // 'v' -> '\u000b'
    case 102: return "\f" // 'f' -> '\f'
    case 13: if (this.input.charCodeAt(this.pos) === 10) { ++this.pos; } // '\r\n'
    case 10: // ' \n'
      if (this.options.locations) { this.lineStart = this.pos; ++this.curLine; }
      return ""
    case 56:
    case 57:
      if (this.strict) {
        this.invalidStringToken(
          this.pos - 1,
          "Invalid escape sequence"
        );
      }
      if (inTemplate) {
        var codePos = this.pos - 1;

        this.invalidStringToken(
          codePos,
          "Invalid escape sequence in template string"
        );
      }
    default:
      if (ch >= 48 && ch <= 55) {
        var octalStr = this.input.substr(this.pos - 1, 3).match(/^[0-7]+/)[0];
        var octal = parseInt(octalStr, 8);
        if (octal > 255) {
          octalStr = octalStr.slice(0, -1);
          octal = parseInt(octalStr, 8);
        }
        this.pos += octalStr.length - 1;
        ch = this.input.charCodeAt(this.pos);
        if ((octalStr !== "0" || ch === 56 || ch === 57) && (this.strict || inTemplate)) {
          this.invalidStringToken(
            this.pos - 1 - octalStr.length,
            inTemplate
              ? "Octal literal in template string"
              : "Octal literal in strict mode"
          );
        }
        return String.fromCharCode(octal)
      }
      if (isNewLine(ch)) {
        // Unicode new line characters after \ get removed from output in both
        // template literals and strings
        if (this.options.locations) { this.lineStart = this.pos; ++this.curLine; }
        return ""
      }
      return String.fromCharCode(ch)
    }
  };

  // Used to read character escape sequences ('\x', '\u', '\U').

  pp.readHexChar = function(len) {
    var codePos = this.pos;
    var n = this.readInt(16, len);
    if (n === null) { this.invalidStringToken(codePos, "Bad character escape sequence"); }
    return n
  };

  // Read an identifier, and return it as a string. Sets `this.containsEsc`
  // to whether the word contained a '\u' escape.
  //
  // Incrementally adds only escaped chars, adding other chunks as-is
  // as a micro-optimization.

  pp.readWord1 = function() {
    this.containsEsc = false;
    var word = "", first = true, chunkStart = this.pos;
    var astral = this.options.ecmaVersion >= 6;
    while (this.pos < this.input.length) {
      var ch = this.fullCharCodeAtPos();
      if (isIdentifierChar(ch, astral)) {
        this.pos += ch <= 0xffff ? 1 : 2;
      } else if (ch === 92) { // "\"
        this.containsEsc = true;
        word += this.input.slice(chunkStart, this.pos);
        var escStart = this.pos;
        if (this.input.charCodeAt(++this.pos) !== 117) // "u"
          { this.invalidStringToken(this.pos, "Expecting Unicode escape sequence \\uXXXX"); }
        ++this.pos;
        var esc = this.readCodePoint();
        if (!(first ? isIdentifierStart : isIdentifierChar)(esc, astral))
          { this.invalidStringToken(escStart, "Invalid Unicode escape"); }
        word += codePointToString(esc);
        chunkStart = this.pos;
      } else {
        break
      }
      first = false;
    }
    return word + this.input.slice(chunkStart, this.pos)
  };

  // Read an identifier or keyword token. Will check for reserved
  // words when necessary.

  pp.readWord = function() {
    var word = this.readWord1();
    var type = types$1.name;
    if (this.keywords.test(word)) {
      type = keywords[word];
    }
    return this.finishToken(type, word)
  };

  // Acorn is a tiny, fast JavaScript parser written in JavaScript.
  //
  // Acorn was written by Marijn Haverbeke, Ingvar Stepanyan, and
  // various contributors and released under an MIT license.
  //
  // Git repositories for Acorn are available at
  //
  //     http://marijnhaverbeke.nl/git/acorn
  //     https://github.com/acornjs/acorn.git
  //
  // Please use the [github bug tracker][ghbt] to report issues.
  //
  // [ghbt]: https://github.com/acornjs/acorn/issues
  //
  // [walk]: util/walk.js


  var version = "8.14.0";

  Parser.acorn = {
    Parser: Parser,
    version: version,
    defaultOptions: defaultOptions,
    Position: Position,
    SourceLocation: SourceLocation,
    getLineInfo: getLineInfo,
    Node: Node,
    TokenType: TokenType,
    tokTypes: types$1,
    keywordTypes: keywords,
    TokContext: TokContext,
    tokContexts: types,
    isIdentifierChar: isIdentifierChar,
    isIdentifierStart: isIdentifierStart,
    Token: Token,
    isNewLine: isNewLine,
    lineBreak: lineBreak,
    lineBreakG: lineBreakG,
    nonASCIIwhitespace: nonASCIIwhitespace
  };

  // The main exported interface (under `self.acorn` when in the
  // browser) is a `parse` function that takes a code string and returns
  // an abstract syntax tree as specified by the [ESTree spec][estree].
  //
  // [estree]: https://github.com/estree/estree

  function parse(input, options) {
    return Parser.parse(input, options)
  }

  // This function tries to parse a single expression at a given
  // offset in a string. Useful for parsing mixed-language formats
  // that embed JavaScript expressions.

  function parseExpressionAt(input, pos, options) {
    return Parser.parseExpressionAt(input, pos, options)
  }

  // Acorn is organized as a tokenizer and a recursive-descent parser.
  // The `tokenizer` export provides an interface to the tokenizer.

  function tokenizer(input, options) {
    return Parser.tokenizer(input, options)
  }

  exports.Node = Node;
  exports.Parser = Parser;
  exports.Position = Position;
  exports.SourceLocation = SourceLocation;
  exports.TokContext = TokContext;
  exports.Token = Token;
  exports.TokenType = TokenType;
  exports.defaultOptions = defaultOptions;
  exports.getLineInfo = getLineInfo;
  exports.isIdentifierChar = isIdentifierChar;
  exports.isIdentifierStart = isIdentifierStart;
  exports.isNewLine = isNewLine;
  exports.keywordTypes = keywords;
  exports.lineBreak = lineBreak;
  exports.lineBreakG = lineBreakG;
  exports.nonASCIIwhitespace = nonASCIIwhitespace;
  exports.parse = parse;
  exports.parseExpressionAt = parseExpressionAt;
  exports.tokContexts = types;
  exports.tokTypes = types$1;
  exports.tokenizer = tokenizer;
  exports.version = version;

}));
"""


if __name__ == "__main__":
    sys.exit(main())
