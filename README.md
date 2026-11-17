# workflow-watch

A local, live viewer for Claude Code **Workflow** runs (the multi-agent
scripts started by the `Workflow` tool). It lists every run of every project
on this computer and draws each run as a flow graph of its agents. A working
agent has a green frame. Click a node to see what that agent is doing now, its
steps, and what it returned. The page updates by itself every two seconds.

You need it most in the VS Code extension, where `/workflows` is not
available. It also shows runs that another Claude Code process started.

Node states: running has a green frame · done has ✔ · quiet for a long time
is amber · returned nothing is red · ended without a result is grey · not
started yet is dashed.

## Start it

```sh
python3 workflow_watch.py
```

This opens `http://127.0.0.1:7766/`. If that port is in use, the next free one
is used.

| Option | Meaning |
| --- | --- |
| `--port N` | use this port on 127.0.0.1 |
| `--root DIR` | the Claude projects folder (default `$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`) |
| `--no-browser` | do not open a browser |
| `--stale-minutes M` | a run without a record whose files did not change for M minutes is shown as "probably stopped" (default 45) |
| `--scan-days D` | read main-session transcripts changed in the last D days (default 3). Older finished runs still show from their records |

To see it inside VS Code, run "Simple Browser: Show" from the command palette
and enter the address.

To see the live view without a real run, run the simulator. It writes an
invented run into the folder you name, never under `~/.claude`:

```sh
python3 tools/simulate_run.py /tmp/ww-demo &
python3 workflow_watch.py --root /tmp/ww-demo
```

## Use it on another computer

Copy `workflow_watch.py`. It needs Python 3.9 or later and nothing else. It
needs no package, no build step and no internet. It works on macOS, Linux and
Windows. On Windows, start it with `py workflow_watch.py`.

## What the page shows

- **Run list**: every run, newest first. For each run: status, project, start
  time, duration, agents done, tokens and last activity. You can filter by
  project, by name, and show running runs only.
- **Run view**: the header (name, description, project, session, run and task
  ids, times, totals, per-phase counts), then the flow, the agents grouped by
  phase, the `log()` lines, the value the script returned, the input (`args`)
  and the script source.
- **Flow**: drawn from the script itself, before any agent starts. It shows
  `agent()` calls, `phase()` markers, loops, `if` branches with their
  conditions, `break`/`return`, `parallel()`, `pipeline()` and `.map()`
  fan-outs, helper functions and child `workflow()` calls. Each started item
  of a fan-out gets its own column, up to 24 items. The stages of a pipeline
  are paired by the item value in their labels. Run agents are matched to the
  nodes by their label templates (`review:${stage}:r${r}`) and phases. An
  agent that matches no node is listed under the flow.
- **Agent panel** (click a node or a row): what the agent does now (its last
  tool call and command, how long ago), its last message, what it returned
  (shown as readable nested lists), its prompt, and all its steps in order.
  Thinking blocks are hidden unless you turn them on. The panel updates in
  place, so what you read does not move when new steps arrive.
- **A step, opened**: each tool in its own form, with syntax colours, never as
  raw JSON. Bash is a terminal (the command, the exit code, the output with
  failing and passing lines marked). Edit is a diff of the lines it changed.
  Write and Read show the file with line numbers. Grep and Glob show what they
  found, WebFetch and WebSearch the question and the answer, StructuredOutput
  the returned value as nested lists. Other tools show their fields as a form.
  The full call is read from the transcript when you open the step.

A Workflow call that waits for your approval is listed as "awaiting launch",
and its flow is drawn from the script before the run starts.

## How it gets the data

It only reads. It never writes under the Claude folder. The server listens on
127.0.0.1 only and refuses requests for any other host name (DNS rebinding).
The page builds all text with `textContent` under a strict Content Security
Policy, because transcripts are untrusted text. Each file is read
incrementally: only the bytes added since the last read are read.

The page draws the flow with the acorn JavaScript parser (MIT licence), which
is embedded at the end of `workflow_watch.py`. To update it, see
`tools/vendor_acorn.py`.

## What it depends on in Claude Code

Claude Code writes the files below for its own use. They are **not a
documented interface**. They were observed with Claude Code 2.1.325 (VS Code
extension, macOS), on 2026-11-17. `<slug>` is a project folder under the root,
and `<session>` is a session id.

| File | Used for |
| --- | --- |
| `<slug>/<session>.jsonl` | `assistant` entries with a `tool_use` named `Workflow` (its `input.script`, `scriptPath`, `args`). `user` entries whose `toolUseResult` has `runId`, `taskId`, `workflowName`, `summary`. `<task-notification>` texts (`task-id`, `status`, `summary`) in `queue-operation` entries. `cwd` |
| `<slug>/<session>/workflows/<runId>.json` | the run record, **written only when the run ends**: `status`, `startTime`, `durationMs`, `script`, `args`, `result`, `logs`, `phases`, `workflowProgress`, `totalTokens`, `totalToolCalls` |
| `<slug>/<session>/subagents/workflows/<runId>/journal.jsonl` | `{"type":"started","agentId","label","phase","key"}` and `{"type":"result","agentId","result"}` lines, appended live, without timestamps |
| `.../<runId>/agent-<agentId>.jsonl` | the agent transcript, appended live: `timestamp`, `cwd`, `gitBranch`, `message.content` blocks (`text`, `thinking`, `tool_use`, `tool_result`), `message.usage` |
| `.../<runId>/agent-<agentId>.meta.json` | `description` (the label), `workflowPhase`, `model` |

Facts the status depends on:

- A run with a record has finished.
- A run without a record is running while its files change. After
  `--stale-minutes` of silence it is shown as "probably stopped". A long tool
  call writes nothing for its whole length, so a quiet agent is marked
  "quiet" only after the same limit.
- After a session restart, Claude Code sends a "stopped" notification even
  for a run whose agents go on working (this was seen on 2026-11-17). When the
  run's files change more than a minute after such a notification, the page
  shows the run as running and says why.
- Token numbers of finished runs come from the record. For running runs they
  are estimates from the transcripts (marked `~`).

Not verified yet: how an agent that is retried after a stall, or a child
`workflow()` run, shows up in these files; and the layout on Windows and Linux.

## When a Claude Code update breaks it

1. Run the tests: `python3 -m unittest discover -s tests` (the graph tests need
   Node.js and are skipped without it).
2. Look at a real run folder and compare it with the table above: the file
   names, the journal line `type` values, and the keys of the record and of the
   `toolUseResult`.
3. The parsing is in `SessionIndex`, `Run._take_event`, `AgentTranscript` and
   `Store._status` in `workflow_watch.py`. Unknown line types are skipped and
   reported as a note on the run page, so the note tells you what changed.
4. If the flow is drawn wrongly, the script parsing is in `GRAPH_JS`
   (`buildGraph`, `matchAgents`, `expandFanouts`).

## Development

- `tests/test_workflow_watch.py`: reader, status and HTTP tests on small
  invented run folders.
- `tests/test_graph.py` with `tests/graph_cases.js` and
  `tests/highlight_cases.js`: the flow builder on several script shapes, and
  the syntax highlighter and line diff (need Node.js).
- `tools/simulate_run.py`: an invented live run, for trying the page.
- `tools/vendor_acorn.py`: replaces the embedded acorn parser.
