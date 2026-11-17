/* Run by test_graph.py with Node: node highlight_cases.js <highlight.js>.
   Prints one JSON line: {"passed": n, "failed": [messages]}. */
"use strict";
const H = require(process.argv[2]);
const failed = [];
let passed = 0;
function check(name, cond, info) {
  if (cond) passed++;
  else failed.push(name + (info === undefined ? "" : ": " + JSON.stringify(info)));
}
const cls = (text, lang) => H.tokens(text, lang).filter((t) => t.c).map((t) => t.c + ":" + t.t);
const has = (list, item) => list.indexOf(item) >= 0;

// the tokens always give back the whole text
for (const lang of H.languages) {
  const text = "x = 1 # c\n\"s\" 'q' `t` /* b */ <a b=\"c\"> $V --flag\n\u00e7\u015f";
  check("round trip " + lang, H.tokens(text, lang).map((t) => t.t).join("") === text);
}

// python: a docstring over several lines is one string
const py = H.lines('"""doc\nline two and not\n"""\nfrom x import y\n@app.get("/a")\ndef f(a: int) -> None:\n    return len(a)  # end', "python");
check("python docstring spans lines", py[1].length === 1 && py[1][0].c === "string", py[1]);
check("python keyword after docstring", py[3][0].c === "keyword" && py[3][0].t === "from");
const pyc = cls('@app.get("/a")\ndef f(a: int) -> None:\n    return len(a)  # end', "python");
["deco:@app.get", "keyword:def", "fn:f", "op:->", "literal:None", "builtin:len", "comment:# end"].forEach((t) => check("python " + t, has(pyc, t), pyc));

// bash
const sh = cls('cd /x && PYTHONPATH=. pytest -q tests/a.py | tail -5; echo "done $HOME" # note', "bash");
["fn:cd", "op:&&", "var:PYTHONPATH", "fn:pytest", "attr:-q", "op:|", "fn:tail", "fn:echo", "string:\"done $HOME\"", "comment:# note"].forEach((t) => check("bash " + t, has(sh, t), sh));

// js and json
const js = cls("const x = await fetch(`/a/${b}`); // hi\n/* block\ncomment */ if (x === null) return", "js");
["keyword:const", "keyword:await", "fn:fetch", "string:`/a/${b}`", "comment:// hi", "comment:/* block\ncomment */", "literal:null"].forEach((t) => check("js " + t, has(js, t), js));
const json = cls('{"a": 1, "b": [true, null, "s"]}', "json");
["prop:\"a\"", "number:1", "literal:true", "string:\"s\""].forEach((t) => check("json " + t, has(json, t), json));

// log: only output-like lines are coloured
const log = cls("What happened, so that it is not repeated.\nWe saw an error in the plan.\n5 passed, 1 failed\nAssertionError: x\n12 passed in 3s\nDeprecationWarning: y", "log");
check("log plain prose", !log.some((t) => /What happened|error in the plan/.test(t)), log);
check("log failure line", has(log, "del:5 passed, 1 failed"), log);
check("log exception line", has(log, "del:AssertionError: x"), log);
check("log pass line", has(log, "add:12 passed in 3s"), log);
check("log warning line", has(log, "warn:DeprecationWarning: y"), log);

// diff
const rows = H.diffLines("a\nb\nc\nd", "a\nB\nc\nd\ne");
check("diff kinds", rows.map((r) => r.k).join("") === " -+  +", rows.map((r) => r.k).join(""));
check("diff removed before added", rows[1].k === "-" && rows[2].k === "+");
check("diff equal texts", H.diffLines("x\ny", "x\ny").every((r) => r.k === " "));
check("diff empty old", H.diffLines("", "n").map((r) => r.k).join("") === "-+");

// languages by file name
check("lang py", H.langFor("/a/b/test_x.py") === "python");
check("lang tsx", H.langFor("C:\\src\\App.tsx") === "js");
check("lang dockerfile", H.langFor("Dockerfile") === "bash");
check("lang unknown", H.langFor("README") === null);
check("sniff json", H.sniff('{"a": 1}') === "json");
check("sniff none", H.sniff("just words") === null);

// a very long text is left plain, quickly
const big = "x = 1\n".repeat(100000);
const t0 = Date.now();
const bigTokens = H.tokens(big, "python");
check("long text plain", bigTokens.length === 1);
const mid = "def f(a):\n    return a  # c\n".repeat(5000);
const t1 = Date.now();
H.tokens(mid, "python");
check("100 kB highlights in under 2 s", Date.now() - t1 < 2000, Date.now() - t1);
check("long text fast", Date.now() - t0 < 4000);

console.log(JSON.stringify({ passed, failed }));
