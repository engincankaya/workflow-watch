"""Small invented Claude Code project folders for the tests.

The layout follows what Claude Code 2.1.325 was seen to write; see README.md.
No real transcript is copied here.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def now_ms():
    return int(time.time() * 1000)


def write_jsonl(path, records, tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        handle.write(tail)


def append_jsonl(path, records, raw=""):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(raw)
        for record in records:
            handle.write(json.dumps(record) + "\n")


def set_mtime(path, ms):
    os.utime(path, (ms / 1000, ms / 1000))


class ProjectBuilder:
    def __init__(self, root, slug="-work-demo", cwd="/work/demo"):
        self.root = Path(root)
        self.project = self.root / slug
        self.project.mkdir(parents=True, exist_ok=True)
        self.cwd = cwd

    # -- main session transcript ------------------------------------------

    def session(self, session_id, entries):
        path = self.project / (session_id + ".jsonl")
        write_jsonl(path, entries)
        (self.project / session_id).mkdir(exist_ok=True)
        return path

    def launch_entry(self, tool_use_id, when, tool_input):
        return {"type": "assistant", "timestamp": iso(when), "cwd": self.cwd, "sessionId": "s",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": tool_use_id, "name": "Workflow", "input": tool_input}]}}

    def launch_result_entry(self, tool_use_id, when, run_id, task_id, name, summary="A demo run"):
        return {"type": "user", "timestamp": iso(when), "cwd": self.cwd,
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id,
                     "content": "Workflow launched in background. Task ID: " + task_id}]},
                "toolUseResult": {"status": "async_launched", "taskId": task_id,
                                  "taskType": "local_workflow", "workflowName": name,
                                  "runId": run_id, "summary": summary}}

    def notification_entry(self, when, task_id, status, summary):
        content = ("<task-notification>\n<task-id>%s</task-id>\n<status>%s</status>\n"
                   "<summary>%s</summary>\n</task-notification>" % (task_id, status, summary))
        return {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(when),
                "content": content}

    # -- runs ---------------------------------------------------------------

    def run_dir(self, session_id, run_id):
        path = self.project / session_id / "subagents" / "workflows" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def journal(self, session_id, run_id, records, tail=""):
        path = self.run_dir(session_id, run_id) / "journal.jsonl"
        write_jsonl(path, records, tail)
        return path

    def agent(self, session_id, run_id, agent_id, label, phase, entries, meta=True):
        run_dir = self.run_dir(session_id, run_id)
        transcript = run_dir / ("agent-%s.jsonl" % agent_id)
        write_jsonl(transcript, entries)
        if meta:
            (run_dir / ("agent-%s.meta.json" % agent_id)).write_text(json.dumps({
                "agentType": "workflow-subagent", "isBuiltIn": True, "description": label,
                "workflowPhase": phase, "spawnDepth": 1, "model": "opus"}))
        return transcript

    def record(self, session_id, run_id, data):
        path = self.project / session_id / "workflows" / (run_id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path


def started(agent_id, label, phase, key=None):
    return {"type": "started", "key": key or "v2:" + agent_id, "agentId": agent_id,
            "label": label, "phase": phase}


def result(agent_id, value, key=None):
    return {"type": "result", "key": key or "v2:" + agent_id, "agentId": agent_id,
            "result": value, "toolsUsed": ["Bash", "StructuredOutput"]}


def transcript(start_ms, steps=2, prompt="Do the task.", tool="Bash", finish_text=None):
    """An agent transcript: the prompt, then tool calls with results."""
    entries = [
        {"type": "user", "timestamp": iso(start_ms), "cwd": "/work/demo", "agentId": "x",
         "message": {"role": "user", "content": "[Workflow harness — computed task] " + prompt}},
    ]
    t = start_ms
    for i in range(steps):
        t += 1000
        entries.append({"type": "assistant", "timestamp": iso(t), "cwd": "/work/demo",
                        "message": {"id": "msg_%d" % i, "role": "assistant", "model": "claude-test-1",
                                    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                                              "cache_read_input_tokens": 1000, "output_tokens": 50},
                                    "content": [{"type": "tool_use", "id": "tu_%d" % i, "name": tool,
                                                 "input": {"command": "make test %d" % i,
                                                           "description": "Run test step %d" % i}}]}})
        t += 1000
        entries.append({"type": "user", "timestamp": iso(t),
                        "message": {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": "tu_%d" % i,
                             "content": "ok %d" % i, "is_error": False}]}})
    if finish_text:
        t += 1000
        entries.append({"type": "assistant", "timestamp": iso(t),
                        "message": {"id": "msg_end", "role": "assistant", "model": "claude-test-1",
                                    "content": [{"type": "text", "text": finish_text}]}})
    return entries


def make_png(width=40, height=24, rgb=(40, 120, 220)):
    """A small, valid PNG of one colour."""
    import struct
    import zlib

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def image_read_transcript(start_ms, images):
    """An agent that reads image files. ``images`` is a list of
    (file path, media type, bytes)."""
    import base64
    entries = [{"type": "user", "timestamp": iso(start_ms), "cwd": "/work/demo",
                "message": {"role": "user", "content": "Look at the screenshots."}}]
    t = start_ms
    for i, (path, media, data) in enumerate(images):
        t += 1000
        entries.append({"type": "assistant", "timestamp": iso(t), "message": {
            "id": "msg_img_%d" % i, "role": "assistant", "model": "claude-test-1",
            "content": [{"type": "tool_use", "id": "tu_img_%d" % i, "name": "Read", "input": {"file_path": path}}]}})
        t += 500
        entries.append({"type": "user", "timestamp": iso(t), "toolUseResult": {"type": "image"}, "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_img_%d" % i, "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media,
                                             "data": base64.b64encode(data).decode("ascii")}}]}]}})
    return entries
