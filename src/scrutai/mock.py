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
from dataclasses import dataclass
from typing import Any


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
]

RULES_BY_CATEGORY = {r.category: r for r in RULES}

_FILE = re.compile(r"^=== FILE (\S+) \[([^\]]*)\]")
_LINE = re.compile(r"^L(\d+): (.*)$")
_DEF = re.compile(r"^\s*(?:async\s+)?(?:def|function)\s+([A-Za-z]\w*)\s*\(")


@dataclass
class SrcLine:
    file: str
    kind: str
    line: int
    text: str


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
            out.append(SrcLine(cur[0], cur[1], int(lm.group(1)), lm.group(2)))
    return out


def _observations(prompt: str) -> list[str]:
    return [chunk for chunk in prompt.split("OBSERVATION:\n")[1:]]


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

    @property
    def tokens_used(self) -> int:
        return self._tokens

    @property
    def cost_usd(self) -> float:
        return 0.0

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        reply = self._reply(system, prompt)
        self._tokens += (len(system) + len(prompt) + len(reply)) // 4
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
        hits = [
            (rule, src)
            for src in lines
            for rule in RULES
            if rule.agent == agent and rule.pattern.search(src.text)
        ]
        observations = _observations(prompt)
        probed = [(rule, src) for rule, src in hits if rule.probe]
        # ReAct: a careful security reviewer looks for other call sites first.
        if agent == "security" and probed and not observations and "--- FINAL" not in prompt:
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
        findings = [_finding(rule, src, extra, 0.7) for rule, src in hits]
        return {"thought": f"{len(findings)} issue(s) on added lines.", "findings": findings}

    def _tests(self, prompt: str) -> dict[str, Any]:
        lines = parse_files(prompt)
        defs = [
            (m.group(1), src)
            for src in lines
            if src.kind == "code" and (m := _DEF.match(src.text)) and not m.group(1).startswith("_")
        ]
        if not defs:
            return {"findings": []}
        observations = _observations(prompt)
        if not observations and "--- FINAL" not in prompt:
            names = "|".join(sorted({name for name, _ in defs}))
            return {
                "thought": "Look for existing tests of the new functions.",
                "action": {"tool": "grep", "args": {"pattern": rf"\b({names})\b", "regex": True}},
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
            return {"decision": "kill", "confidence": conf, "note": note}

        if cited.startswith("("):
            return kill(0.1, f"Out of scope: {cited.strip('()')}.")
        if evidence.strip() in ("", "none"):
            return kill(0.2, "No concrete line or tool result cited.")

        rule = RULES_BY_CATEGORY.get(category)
        if rule is not None:
            # Secrets and SQL live *in* strings; everything else must survive
            # with string contents blanked out.
            keep_strings = category in ("hardcoded_secret", "sql_injection")
            code = strip_code(cited, keep_strings=keep_strings)
            if not rule.pattern.search(code):
                return kill(0.15, "Pattern only appears in a comment or string literal.")
        if category == "hardcoded_secret":
            m = re.search(r"[:=]\s*([\"'])(.*?)\1", cited)
            value = m.group(2) if m else ""
            if in_test_file or _placeholder(value):
                return kill(0.2, "Placeholder or test-fixture value, not a real credential.")
        if category == "injection" and _CONSTANT_SINK.search(cited):
            return kill(0.3, "The command is a constant literal; no untrusted input reaches it.")
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
_PLACEHOLDER = re.compile(
    r"^(x+|\*+|<.*>|\$\{.*\}|\{\{.*\}\}|your[-_ ].*|.*(example|dummy|placeholder|"
    r"changeme|fake|sample|redacted).*|test.*)$",
    re.I,
)


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
