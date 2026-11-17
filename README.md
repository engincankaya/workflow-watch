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
- **Flow**: drawn from left to right like n8n (a button switches to top to
  bottom; the page remembers the choice), from the script itself, before any
  agent starts. It shows
  `agent()` calls, `phase()` markers, loops, `if` branches with their
  conditions, `break`/`return`, `parallel()`, `pipeline()` and `.map()`
  fan-outs, helper functions and child `workflow()` calls. It knows nothing
  of any one workflow: it reads the script's own structure.
  - Each started item of a fan-out gets its own column, up to 24 items; a
    fan-out inside a column opens into columns of its own. The columns follow
    the item each label names (`review:${file}` and `check:${file}:${kind}` share
    the column of their file), and the same item in several rounds of a loop
    stays one column.
  - A loop shows its round: from the loop counter when a label carries it,
    else from an agent that runs once per round, else how many agents started.
  - Run agents are placed by their label templates and phases; an agent whose
    label says nothing is placed by the literal text of its prompt; a result
    replayed from cache gets its label from the earlier run of the session.
    The agents of a child `workflow()` fill its box. An agent that cannot be
    placed is listed under the flow.
- **Agent panel** (click a node or a row): what the agent does now (its last
  tool call and command, how long ago), its last message, what it returned
  (each field name as a small heading with its value below it; Markdown text
  shown as Markdown), its prompt, and all its steps in order.
  The newest step is on top. Thinking blocks are hidden unless you turn them
  on. The panel updates in place, so what you read does not move when new
  steps arrive.
- **A step, opened**: each tool in its own form, with syntax colours, never as
  raw JSON. Bash is a terminal (the command, the exit code, the output with
  failing and passing lines marked). Edit is a diff of the lines it changed.
  Write and Read show the file with line numbers. Grep and Glob show what they
  found, WebFetch and WebSearch the question and the answer, StructuredOutput
  the returned value as nested lists. Other tools show their fields as a form.
  The full call is read from the transcript when you open the step.

A Workflow call that waits for your approval is listed as "awaiting launch",
and its flow is drawn from the script before the run starts. A workflow
started by name reads its script from `.claude/workflows/` of the project
folder or one of its parents, the personal `workflows/` folder next to
`projects/`, or a plugin's `workflows/` folder. A bundled workflow such as
`/deep-research` has no script file; its flow is drawn from its agents.

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
| `.../<runId>/agent-<agentId>.jsonl` | the agent transcript, appended live: `timestamp`, `cwd`, `gitBranch`, `message.content` blocks (`text`, `thinking`, `tool_use`, `tool_result`), `message.usage`. The first user entry that holds "computed task" is the prompt: a paragraph ending in "follows:" and then the task, every line indented |
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

Seen with a real probe run (Claude Code 2.1.325, 2026-11-17):

- An agent without a `label` is named by the first 60 characters of its
  prompt (the journal's `label` and the meta file's `description`).
- A child `workflow()` runs inside the same run: its agents go to the same
  journal with the phase `▸ <child name>`, and the record's `logs` hold
  `▸ running dynamic workflow <name>` and `▸ <name> done`.
- Resuming a run keeps its run id and folder. An unchanged call writes
  nothing to the journal; agents that run again are appended. The record is
  written again at the end; replayed agents are marked `"cached": true`,
  without duration, tokens or tool calls, and the totals count only what ran
  again. Until the new record is written, the earlier one stays on disk: the
  page shows the run as running when the session has a later launch of the
  same run id.

Not verified yet: how an agent that is retried after a stall shows up in
these files, and the layout on Windows and Linux.

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
- `tests/test_scenarios.py`: eleven invented workflow shapes (fan-out with
  verification, pipeline, loop until dry with nested votes, judge panel,
  no labels, helpers with switch/try/do-while, 60-item fan-out, fan-out in a
  fan-out, fan-out in a loop, rounds from a helper-built label) and run
  states (failed agents, cached results of a resumed run, a cut-off run, a
  saved workflow started by name, a run watched while it runs, several
  projects). Each runs in `tools/simulate_workflow.js`, and
  `tests/scenario_check.js` checks with the page's own code that every agent
  is drawn once, at the `agent()` call that started it (needs Node.js).
- `tools/simulate_workflow.js`: runs any workflow script against a fake
  runtime and writes the files Claude Code writes, live; for example
  `node tools/simulate_workflow.js --root /tmp/ww-demo --script my-flow.js --step-ms 800`
  and then `python3 workflow_watch.py --root /tmp/ww-demo`.
- `tools/simulate_run.py`: an invented live run, for trying the page.
- `tools/vendor_acorn.py`: replaces the embedded acorn parser.
