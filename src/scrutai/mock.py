"""The offline "model" behind `llm_mode: mock`.

A deterministic stand-in for a real LLM that speaks the exact same protocol
(ReAct actions, findings JSON, critic verdicts) by reading the structured
prompts Scrutai sends. It lets the whole graph run end-to-end with no API key,
and it is what every test and the default `scrutai eval` run against.

It is deliberately built like a *noisy* reviewer: specialists match their rules
against raw source lines, so a sink mentioned in a comment or a string still
gets flagged. That noise is the point; it is what the critic exists to remove,
and the eval harness measures how much it removes.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any

from .tools.semgrep import category_for


@dataclass(frozen=True)
class Rule:
    agent: str
    category: str
    pattern: re.Pattern[str]
    severity: str
    title: str
    body: str
    #: Fixed string the security agent greps the repo for before reporting.
    probe: str = ""
    #: Only fires on lines nested inside a for/while loop.
    in_loop: bool = False


def _r(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


RULES: list[Rule] = [
    # --- security ---
    Rule(
        "security",
        "injection",
        _r(
            r"\bos\.(system|popen)\(|subprocess\.\w+\([^)]*shell\s*=\s*True|(?<![\w.])(eval|exec)\("
        ),
        "high",
        "Possible command/eval injection",
        "Input reaches a dynamic execution sink (shell or eval/exec).",
        probe="(",
    ),
    Rule(
        "security",
        "sql_injection",
        _r(r"\.execute\(\s*(f[\"']|[\"'][^\"']*[\"']\s*(%|\+|\.format))"),
        "high",
        "SQL built with string formatting",
        "Query text is assembled from variables; use bound parameters instead.",
        probe=".execute(",
    ),
    Rule(
        "security",
        "hardcoded_secret",
        _r(
            r"\b\w*(password|passwd|secret|api_?key|token|private_?key)\w*\s*[:=]\s*[\"'][^\"']+[\"']",
            re.I,
        ),
        "high",
        "Hardcoded credential",
        "A credential literal is committed to source; load it from the environment.",
    ),
    Rule(
        "security",
        "unsafe_deserialization",
        _r(r"\bpickle\.loads?\(|\byaml\.load\((?![^)]*SafeLoader)|\bmarshal\.loads?\("),
        "high",
        "Unsafe deserialization",
        "Deserializing untrusted bytes with pickle/yaml.load/marshal can execute code.",
        probe="load",
    ),
    Rule(
        "security",
        "weak_crypto",
        _r(r"\bhashlib\.(md5|sha1)\(|\bmd5\("),
        "medium",
        "Weak hash algorithm",
        "MD5/SHA1 are broken for security purposes; use SHA-256 or a password KDF.",
    ),
    Rule(
        "security",
        "tls_verify_disabled",
        _r(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false"),
        "medium",
        "TLS certificate verification disabled",
        "Disabling verification allows man-in-the-middle attacks.",
    ),
    # --- correctness ---
    Rule(
        "correctness",
        "broad_except",
        _r(r"^\s*except\s*:|^\s*except\s+(Base)?Exception\b"),
        "medium",
        "Overly broad exception handler",
        "A bare/broad except swallows unrelated errors and hides bugs.",
    ),
    Rule(
        "correctness",
        "mutable_default",
        _r(r"\bdef\s+\w+\(.*=\s*(\[\]|\{\}|set\(\)|dict\(\)|list\(\))"),
        "medium",
        "Mutable default argument",
        "The default object is shared across calls; use None and create it inside.",
    ),
    Rule(
        "correctness",
        "none_comparison",
        _r(r"[!=]=\s*None\b"),
        "low",
        "Comparison to None with ==/!=",
        "Use `is None` / `is not None`; == can be overridden.",
    ),
    Rule(
        "correctness",
        "identity_literal",
        _r(r"\bis\s+(not\s+)?([\"']|\d)"),
        "medium",
        "Identity comparison with a literal",
        "`is` compares identity, not value; it only works by interning accident.",
    ),
    # --- performance ---
    Rule(
        "performance",
        "n_plus_one",
        _r(
            r"\.(execute|query|fetchone|fetchall)\(|\brequests\.(get|post|put|delete)\(|"
            r"\b(session|client|http)\.(get|post)\(|\.objects\.(get|filter)\(|\bfetch\("
        ),
        "medium",
        "Query or network call inside a loop",
        "This runs once per iteration (N+1); batch it or hoist it out of the loop.",
        in_loop=True,
    ),
    Rule(
        "performance",
        "regex_in_loop",
        _r(r"\bre\.compile\("),
        "low",
        "Regex compiled inside a loop",
        "Compile once outside the loop and reuse the pattern.",
        in_loop=True,
    ),
    Rule(
        "performance",
        "string_concat_in_loop",
        _r(r"\b\w+\s*\+=\s*(f?[\"']|str\()"),
        "low",
        "String built with += in a loop",
        "Repeated concatenation is quadratic; collect parts and str.join them.",
        in_loop=True,
    ),
    Rule(
        "performance",
        "sort_for_min_max",
        _r(r"sorted\([^)]*\)\[(0|-1)\]"),
        "low",
        "Sorting to take the min/max",
        "sorted(...)[0] is O(n log n); min()/max() is O(n).",
    ),
    # --- style ---
    Rule(
        "style",
        "debug_leftover",
        _r(
            r"^\s*(print\(|breakpoint\(\)|pdb\.set_trace\(\)|import pdb\b|console\.log\(|debugger;)"
        ),
        "low",
        "Debug leftover",
        "Debug output/breakpoints should not ship in library code; use logging.",
    ),
    Rule(
        "style",
        "wildcard_import",
        _r(r"^\s*from\s+\S+\s+import\s+\*"),
        "low",
        "Wildcard import",
        "`import *` hides where names come from and can shadow builtins.",
    ),
    Rule(
        "style",
        "untracked_todo",
        _r(r"(#|//)\s*(TODO|FIXME|XXX)\b"),
        "info",
        "TODO without a ticket",
        "Link a ticket so this does not rot.",
    ),
]

RULES_BY_CATEGORY = {r.category: r for r in RULES}
_LOOP = re.compile(r"^\s*(?:async\s+)?(for|while)\b")
_SCOPE = re.compile(r"^\s*(?:async\s+)?(def|class|function)\b")


def enclosing_loop(lines: list[SrcLine], idx: int) -> SrcLine | None:
    """The nearest for/while header that `lines[idx]` is nested under, if any."""
    target = lines[idx]
    indent = len(target.text) - len(target.text.lstrip())
    for j in range(idx - 1, -1, -1):
        cand = lines[j]
        if cand.file != target.file:
            break
        if not cand.text.strip():
            continue
        cand_indent = len(cand.text) - len(cand.text.lstrip())
        if cand_indent >= indent:
            continue
        if _LOOP.match(cand.text):
            return cand
        if _SCOPE.match(cand.text):
            return None
        indent = cand_indent
    return None


_FILE = re.compile(r"^=== FILE (\S+) \[([^\]]*)\]")
_LINE = re.compile(r"^L(\d+): (.*)$")
_DEF = re.compile(r"^\s*(?:async\s+)?(?:def|function)\s+([A-Za-z]\w*)\s*\(")


@dataclass
class SrcLine:
    file: str
    kind: str
    line: int
    text: str
    #: Inside a triple-quoted string (docstring, embedded source, fixture).
    in_string: bool = False


def parse_files(prompt: str) -> list[SrcLine]:
    """Recover (file, kind, line, code) from the numbered diff in a prompt."""
    out: list[SrcLine] = []
    cur: tuple[str, str] | None = None
    for raw in prompt.splitlines():
        m = _FILE.match(raw)
        if m:
            attrs = [a.strip() for a in m.group(2).split(",")]
            cur = (m.group(1), attrs[1] if len(attrs) > 1 else "code")
            continue
        if raw.startswith(("--- STEP", "--- FINAL", "SEED OBSERVATION")):
            cur = None
            continue
        lm = _LINE.match(raw)
        if cur and lm:
            text = lm.group(2)
            prev = out[-1] if out and out[-1].file == cur[0] else None
            # Approximate: carry triple-quote state across consecutive lines.
            inside = bool(prev and prev.in_string) != bool(prev and _opens(prev.text))
            out.append(SrcLine(cur[0], cur[1], int(lm.group(1)), text, inside))
    return out


def _opens(text: str) -> bool:
    """True if the line toggles triple-quoted-string state."""
    return (text.count('"""') + text.count("'''")) % 2 == 1


def _observations(prompt: str) -> list[str]:
    # Protocol markers only count at line start: reviewed code is always
    # prefixed with `L<n>: `, so a diff that *contains* the text
    # "OBSERVATION:" or "--- FINAL" (Scrutai reviewing itself) can't spoof them.
    return re.split(r"^OBSERVATION:\n", prompt, flags=re.M)[1:]


def _final(prompt: str) -> bool:
    return re.search(r"^--- FINAL", prompt, re.M) is not None


def _finding(rule: Rule, src: SrcLine, extra: list[str], confidence: float) -> dict[str, Any]:
    return {
        "title": rule.title,
        "body": rule.body,
        "file": src.file,
        "line": src.line,
        "category": rule.category,
        "severity": rule.severity,
        "confidence": confidence,
        "evidence": [f"L{src.line}: {src.text.strip()}", *extra],
    }


class MockLLMClient:
    """Deterministic offline client speaking Scrutai's prompt protocol."""

    def __init__(self) -> None:
        self._tokens = 0
        self._lock = threading.Lock()
        self._last = threading.local()

    @property
    def tokens_used(self) -> int:
        return self._tokens

    @property
    def cost_usd(self) -> float:
        return 0.0

    @property
    def last_call_tokens(self) -> int:
        """Tokens of this thread's most recent call (exact under concurrency)."""
        return int(getattr(self._last, "tokens", 0))

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        reply = self._reply(system, prompt)
        with self._lock:  # specialists and critic calls run on several threads
            tokens = (len(system) + len(prompt) + len(reply)) // 4
            self._tokens += tokens
        self._last.tokens = tokens
        return reply

    # ---- dispatch ------------------------------------------------------------

    def _reply(self, system: str, prompt: str) -> str:
        role = prompt.split("\n", 1)[0].removeprefix("ROLE:").strip()
        if role == "router":
            m = re.search(r"^CANDIDATES: (.*)$", prompt, re.M)
            return json.dumps({"agents": m.group(1).split(", ") if m else []})
        if role == "critic" or "you are the critic" in system.lower():
            return json.dumps(self._critic(prompt))
        if "\nMODE: defend" in prompt:
            return json.dumps(self._defend(prompt))
        if role == "tests":
            return json.dumps(self._tests(prompt))
        if role in {r.agent for r in RULES}:
            return json.dumps(self._rules(role, prompt))
        return json.dumps({"findings": []})

    # ---- specialists ---------------------------------------------------------

    def _rules(self, agent: str, prompt: str) -> dict[str, Any]:
        lines = parse_files(prompt)
        hits: list[tuple[Rule, SrcLine]] = []
        loops: dict[int, SrcLine] = {}
        for idx, src in enumerate(lines):
            for rule in RULES:
                if rule.agent != agent or src.in_string or not rule.pattern.search(src.text):
                    continue
                if rule.in_loop:
                    loop = enclosing_loop(lines, idx)
                    if loop is None:
                        continue
                    loops[len(hits)] = loop
                hits.append((rule, src))
        observations = _observations(prompt)
        probed = [(rule, src) for rule, src in hits if rule.probe]
        # ReAct: a careful security reviewer looks for other call sites first.
        if agent == "security" and probed and not observations and not _final(prompt):
            rule, src = probed[0]
            m = rule.pattern.search(src.text)
            sink = m.group(0) if m else rule.probe
            return {
                "thought": "Check how widely this sink is used before reporting.",
                "action": {"tool": "grep", "args": {"pattern": sink.strip()}},
            }
        extra: list[str] = []
        if observations:
            n = sum(1 for ln in observations[-1].splitlines() if ":" in ln and ln[:1] != "(")
            extra.append(f"grep: {n} call site(s) of this sink in the repo")
        findings = [
            *self._sast(prompt, lines, {(src.file, src.line, rule.category) for rule, src in hits}),
            *[
                _finding(
                    rule,
                    src,
                    [
                        *extra,
                        *(
                            [f"inside loop at L{lp.line}: {lp.text.strip()}"]
                            if (lp := loops.get(i))
                            else []
                        ),
                    ],
                    0.7,
                )
                for i, (rule, src) in enumerate(hits)
            ],
        ]
        for hit in _SEMGREP_HIT.finditer(_block(prompt, "SEMGREP:")):
            for f in findings:
                if (f["file"], f["line"]) == (hit.group(1), int(hit.group(2))):
                    f["evidence"].append(hit.group(0).strip())
        return {"thought": f"{len(findings)} issue(s) on added lines.", "findings": findings}

    def _sast(
        self, prompt: str, lines: list[SrcLine], seen: set[tuple[str, int, str]]
    ) -> list[dict[str, Any]]:
        """Findings for Semgrep hits the regex rules did not already cover."""
        out: list[dict[str, Any]] = []
        for path, line, rule_id, message in _SEMGREP_HIT.findall(_block(prompt, "SEMGREP:")):
            category = category_for(rule_id)
            if (path, int(line), category) in seen:
                continue
            src = next((s for s in lines if s.file == path and s.line == int(line)), None)
            out.append(
                {
                    "title": f"Semgrep: {message}",
                    "body": f"Rule {rule_id} fired on this line.",
                    "file": path,
                    "line": int(line),
                    "category": category,
                    "severity": "high",
                    "confidence": 0.8,
                    "evidence": [f"L{line}: {src.text.strip() if src else ''}".rstrip()],
                }
            )
        return out

    def _tests(self, prompt: str) -> dict[str, Any]:
        lines = parse_files(prompt)
        defs = [
            (m.group(1), src)
            for src in lines
            if src.kind == "code"
            and not src.in_string
            and (m := _DEF.match(src.text))
            and not m.group(1).startswith("_")
            and len(src.text) - len(src.text.lstrip()) < 8  # nested helpers aren't API
        ]
        if not defs:
            return {"findings": []}
        observations = _observations(prompt)
        if not observations and not _final(prompt):
            names = "|".join(sorted({name for name, _ in defs}))
            return {
                "thought": "Look for existing tests of the new functions.",
                "action": {
                    "tool": "grep",
                    "args": {"pattern": rf"\b({names})\b", "regex": True, "glob": "*test*"},
                },
            }
        test_text = "\n".join(src.text for src in lines if src.kind == "test")
        repo_hits = [
            ln
            for ln in (observations[-1] if observations else "").splitlines()
            if "test" in ln.split(":", 1)[0].lower()
        ]
        findings = []
        for name, src in defs:
            covered = re.search(rf"\b{name}\b", test_text) or any(
                re.search(rf"\b{name}\b", h) for h in repo_hits
            )
            if covered:
                continue
            findings.append(
                {
                    "title": f"New function `{name}` has no tests",
                    "body": "No test in the repo or in this diff references it.",
                    "file": src.file,
                    "line": src.line,
                    "category": "missing_tests",
                    "severity": "low",
                    "confidence": 0.65,
                    "evidence": [
                        f"L{src.line}: {src.text.strip()}",
                        f"grep: no test references {name}",
                    ],
                }
            )
        return {"findings": findings}

    # ---- debate: specialist defends a challenged finding -----------------------

    def _defend(self, prompt: str) -> dict[str, Any]:
        fields = _fields(prompt)
        path, category = fields.get("FILE", ""), fields.get("CATEGORY", "")
        try:
            line = int(fields.get("LINE", ""))
        except ValueError:
            return {"withdraw": True, "reason": "no line to defend"}
        if not _observations(prompt):
            return {
                "thought": "Re-read the cited code before answering the critic.",
                "action": {
                    "tool": "read_file",
                    "args": {"path": path, "start": max(line - 2, 1), "end": line + 2},
                },
            }
        src = next((s for s in parse_files(prompt) if s.file == path and s.line == line), None)
        rule = RULES_BY_CATEGORY.get(category)
        if src is None or (rule is not None and not rule.pattern.search(src.text)):
            return {"withdraw": True, "reason": "could not re-confirm the cited line"}
        return {
            "defense": f"The diff adds L{line} `{src.text.strip()}`, which matches {category}.",
            "evidence": [f"re-read L{line}: {src.text.strip()}"],
        }

    # ---- critic ----------------------------------------------------------------

    def _critic(self, prompt: str) -> dict[str, Any]:
        fields = _fields(prompt)
        category = fields.get("CATEGORY", "general")
        cited = fields.get("CITED CODE", "")
        evidence = _block(prompt, "EVIDENCE:")
        round_no, max_rounds = _round(fields.get("ROUND", "1 of 1"))
        in_test_file = "(kind: test)" in fields.get("FILE", "")

        def kill(conf: float, note: str) -> dict[str, Any]:
            return {
                "decision": "kill",
                "confidence": conf,
                "note": note,
                "counter_evidence": f"L{fields.get('LINE')}: {cited}",
            }

        if cited.startswith("("):
            return kill(0.1, f"Out of scope: {cited.strip('()')}.")
        if evidence.strip() in ("", "none"):
            return kill(0.2, "No concrete line or tool result cited.")

        line_ref = f"{fields.get('FILE', '').split(' (kind')[0]}:{fields.get('LINE')}: ["
        if line_ref in evidence:
            return {"decision": "uphold", "confidence": 0.9, "note": "A Semgrep rule fired here."}

        rule = RULES_BY_CATEGORY.get(category)
        if rule is not None:
            # Secrets and SQL live *in* strings; everything else must survive
            # with string contents blanked out.
            keep_strings = category in ("hardcoded_secret", "sql_injection")
            code = cited if category == "untracked_todo" else strip_code(cited, keep_strings)
            if not rule.pattern.search(code):
                return kill(0.15, "Pattern only appears in a comment or string literal.")
        if category == "hardcoded_secret":
            # Judge the value the secret rule matched, not the first string on
            # the line (`connect(host="db", password="...")`).
            hit = RULES_BY_CATEGORY["hardcoded_secret"].pattern.search(cited)
            m = re.search(r"[:=]\s*([\"'])(.*?)\1\s*$", hit.group(0)) if hit else None
            value = m.group(2) if m else ""
            if in_test_file or _placeholder(value):
                return kill(0.2, "Placeholder or test-fixture value, not a real credential.")
        if category == "injection" and _CONSTANT_SINK.search(cited):
            return kill(0.3, "The command is a constant literal; no untrusted input reaches it.")
        if category == "broad_except" and _reraises(prompt, fields.get("LINE", "")):
            return kill(0.2, "The handler re-raises; nothing is swallowed.")
        if category == "debug_leftover" and _SCRIPT_PATH.search(fields.get("FILE", "")):
            return kill(0.2, "This is a CLI/script; printing is its output channel.")
        if category == "untracked_todo" and _TICKET.search(cited):
            return kill(0.1, "The TODO already references a ticket.")
        if (
            rule is not None
            and rule.in_loop
            and not _LOOP_IN_LISTING.search(
                _block(prompt, "CONTEXT:") + _block(prompt, "EVIDENCE:")
            )
        ):
            return kill(0.25, "No enclosing loop is visible; the call runs once.")
        if category == "weak_crypto" and "usedforsecurity=False" in cited:
            return kill(0.2, "Explicitly marked usedforsecurity=False.")

        grounded = "- tool:" in evidence
        defended = "SPECIALIST DEFENSE:" in prompt
        if not grounded and not defended:
            return {
                "decision": "challenge",
                "confidence": 0.5,
                "note": "Plausible, but no tool observation backs it.",
                "question": f"Re-read line {fields.get('LINE')} with a tool: does it really "
                f"exhibit {category}?",
            }
        if round_no > max_rounds:  # defensive; the graph never gets here
            return kill(0.3, "Debate did not converge.")
        if in_test_file and rule is not None and rule.severity in ("high", "critical"):
            return {
                "decision": "downgrade",
                "confidence": 0.7,
                "severity": "low",
                "note": "Real pattern, but only in test code.",
            }
        conf = 0.8 if defended and not grounded else 0.85
        return {
            "decision": "uphold",
            "confidence": conf,
            "note": "Cited line and evidence hold up.",
        }


_CONSTANT_SINK = re.compile(
    r"\b(os\.system|os\.popen|eval|exec)\(\s*([\"'])[^\"']*\2\s*\)"
    r"|subprocess\.\w+\(\s*([\"'])[^\"']*\3\s*,"
)
# A loop header inside a numbered listing ("+L3:     for x in y:") or evidence.
_LOOP_IN_LISTING = re.compile(r"L\d+:\s+(?:async\s+)?(for|while)\b")
_SEMGREP_HIT = re.compile(r"^(\S+?):(\d+): \[([^\]]+)\] \(\w+\) (.*)$", re.M)
_SCRIPT_PATH = re.compile(r"(^|/)(cli|__main__|manage)\.py\b|(^|/)(scripts?|bin)/")
_TICKET = re.compile(r"#\d+|\b[A-Z][A-Z0-9]+-\d+\b|https?://")
_PLACEHOLDER = re.compile(
    r"^(x+|\*+|<.*>|\$\{.*\}|\{\{.*\}\}|your[-_ ].*|.*(example|dummy|placeholder|"
    r"changeme|fake|sample|redacted).*|test.*)$",
    re.I,
)


def _reraises(prompt: str, line: str) -> bool:
    """Does a `raise` follow within 3 lines of the cited except clause?"""
    try:
        n = int(line)
    except ValueError:
        return False
    for m in re.finditer(r"^[+ ]L(\d+): (.*)$", _block(prompt, "CONTEXT:"), re.M):
        if n < int(m.group(1)) <= n + 3 and re.search(r"\braise\b", m.group(2)):
            return True
    return False


def _placeholder(value: str) -> bool:
    return len(value) < 6 or bool(_PLACEHOLDER.match(value))


def _fields(prompt: str) -> dict[str, str]:
    """Single-line `KEY: value` fields from a prompt (first occurrence wins)."""
    out: dict[str, str] = {}
    for key, value in re.findall(r"^([A-Z][A-Z ]+): (.*)$", prompt, re.M):
        out.setdefault(key, value)
    return out


def _block(prompt: str, header: str) -> str:
    """The lines following `header` up to the next ALLCAPS header."""
    if header not in prompt:
        return ""
    body = prompt.split(header, 1)[1]
    m = re.search(r"^[A-Z][A-Z ]+:", body, re.M)
    return body[: m.start()] if m else body


def _round(text: str) -> tuple[int, int]:
    m = re.match(r"(\d+) of (\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else (1, 1)


def strip_code(line: str, keep_strings: bool = False) -> str:
    """Drop comments (and, unless keep_strings, string contents) from one line."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                if keep_strings:
                    out.append(line[i : i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
                out.append(ch)
            elif keep_strings:
                out.append(ch)
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" or line.startswith("//", i):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)
