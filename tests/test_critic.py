"""The critic: decisions, the debate loop, and termination."""

from __future__ import annotations

import json

from scrutai import review_diff
from scrutai.config import ScrutaiConfig
from scrutai.critic import build_prompt, critique, judge
from scrutai.llm import MockLLMClient
from scrutai.mock import strip_code
from scrutai.models import ChangedFile, DiffContext, Finding, Severity


def _diff(patch: str, path: str = "app.py") -> DiffContext:
    return DiffContext(files=[ChangedFile(path=path, patch=patch)])


def _finding(**kw: object) -> Finding:
    base: dict[str, object] = {
        "agent": "correctness",
        "title": "t",
        "body": "b",
        "file": "app.py",
        "line": 1,
        "category": "broad_except",
        "severity": Severity.HIGH,
        "confidence": 0.7,
        "evidence": ["L1: x"],
    }
    base.update(kw)
    return Finding.model_validate(base)


class Critic:
    """A critic that always gives the same verdict, counting its calls."""

    tokens_used = 0
    cost_usd = 0.0

    def __init__(self, verdict: dict[str, object] | str) -> None:
        self.reply = verdict if isinstance(verdict, str) else json.dumps(verdict)
        self.calls = 0

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        if "MODE: defend" in prompt:
            return json.dumps({"defense": "still sure", "evidence": ["re-read"]})
        if prompt.startswith("ROLE: correctness"):
            finding = {"title": "x", "line": 3, "category": "broad_except", "evidence": ["L3"]}
            return json.dumps({"findings": [finding]})
        self.calls += 1
        return self.reply


DIFF = _diff("+try:\n+    go()\n+except Exception:\n+    pass\n")


def test_kill_marks_dead() -> None:
    f = judge(
        Critic({"decision": "kill", "confidence": 0.9, "note": "nope"}),
        ScrutaiConfig(),
        _finding(),
        DIFF,
        1,
    )
    assert not f.alive and f.critic_note == "nope"


def test_downgrade_only_lowers_severity() -> None:
    cfg = ScrutaiConfig()
    down = judge(
        Critic({"decision": "downgrade", "confidence": 0.8, "severity": "low"}),
        cfg,
        _finding(),
        DIFF,
        1,
    )
    assert down.severity == Severity.LOW and down.alive
    up = judge(
        Critic({"decision": "downgrade", "confidence": 0.8, "severity": "critical"}),
        cfg,
        _finding(),
        DIFF,
        1,
    )
    assert up.severity == Severity.HIGH


def test_unjudged_finding_is_withheld() -> None:
    f = judge(Critic("¯\\_(ツ)_/¯"), ScrutaiConfig(), _finding(confidence=0.99), DIFF, 1)
    assert not f.alive and f.unjudged
    assert "withheld" in (f.critic_note or "")


def test_judge_does_not_mutate_input() -> None:
    original = _finding()
    judge(Critic({"decision": "kill", "confidence": 0.1}), ScrutaiConfig(), original, DIFF, 1)
    assert original.alive and original.history == []


def test_later_rounds_only_rejudge_contested() -> None:
    critic = Critic({"decision": "uphold", "confidence": 0.9})
    settled, open_ = _finding(), _finding(line=3, contested=True)
    critique(critic, ScrutaiConfig(), [settled, open_], DIFF, round_no=2)
    assert critic.calls == 1


def test_prompt_flags_lines_outside_the_diff() -> None:
    prompt = build_prompt(_finding(line=40), DIFF, 1, 2)
    assert "CITED CODE: (line 40 is not added by this diff)" in prompt
    prompt = build_prompt(_finding(line=3), DIFF, 1, 2)
    assert "CITED CODE: except Exception:" in prompt and "+L3: except Exception:" in prompt


def test_endless_challenges_stop_at_max_rounds() -> None:
    critic = Critic({"decision": "challenge", "confidence": 0.5, "question": "really?"})
    for rounds in (1, 2, 3):
        critic.calls = 0
        result = review_diff(
            DIFF, ScrutaiConfig(enabled_agents=["correctness"], max_critic_rounds=rounds), critic
        )
        assert result.rounds == rounds
        assert critic.calls == rounds  # one judgement per round, then it stops
        assert result.findings == []  # never settled above threshold -> dropped
        assert result.dropped[0].contested


def test_specialist_can_withdraw() -> None:
    class Withdrawer(Critic):
        def complete(self, *, model: str, system: str, prompt: str) -> str:
            if "MODE: defend" in prompt:
                return json.dumps({"withdraw": True, "reason": "misread it"})
            if prompt.startswith("ROLE: correctness"):
                return json.dumps(
                    {
                        "findings": [
                            {
                                "title": "x",
                                "line": 3,
                                "category": "broad_except",
                                "evidence": ["L3"],
                            }
                        ]
                    }
                )
            self.calls += 1
            return self.reply

    critic = Withdrawer({"decision": "challenge", "confidence": 0.5, "question": "sure?"})
    result = review_diff(DIFF, ScrutaiConfig(enabled_agents=["correctness"]), critic)
    (dropped,) = result.dropped
    assert dropped.critic_note == "withdrawn by correctness: misread it"
    assert critic.calls == 1  # no second critic pass for a withdrawn finding


def test_mock_debate_upholds_grounded_findings_in_two_rounds() -> None:
    result = review_diff(DIFF, ScrutaiConfig(enabled_agents=["correctness"]), MockLLMClient())
    (f,) = result.findings
    assert result.rounds == 2
    assert [h.split(":")[0] for h in f.history] == ["round 1", "defense", "round 2"]


def test_mock_critic_kills_planted_false_positives() -> None:
    patch = (
        "+# never call os.system(user) here\n"
        '+msg = "do not eval(x)"\n'
        '+os.system("ls -la")\n'
        '+DB_PASSWORD = "changeme"\n'
        "+os.system(user_cmd)\n"
    )
    result = review_diff(_diff(patch), ScrutaiConfig(enabled_agents=["security"]), MockLLMClient())
    assert [(f.category, f.line) for f in result.findings] == [("injection", 5)]
    assert len(result.dropped) == 4


def test_test_fixture_secret_is_killed() -> None:
    patch = '+API_KEY = "sk-live-a8f7d6e5c4"\n'
    result = review_diff(_diff(patch, "tests/test_api.py"), ScrutaiConfig(), MockLLMClient())
    assert result.findings == []


def test_strip_code() -> None:
    assert strip_code('x = "a # b"  # real comment') == 'x = ""  '
    assert strip_code('x = "a # b"', keep_strings=True) == 'x = "a # b"'
    assert strip_code("call(x) // js comment") == "call(x) "
    assert strip_code(r'"esc\"aped" + y') == '"" + y'


def test_secret_value_is_the_matched_one_not_the_first_string() -> None:
    patch = '+conn = connect(host="db", password="Pr0d-Db!2024")\n'
    result = review_diff(_diff(patch), ScrutaiConfig(enabled_agents=["security"]), MockLLMClient())
    assert [f.category for f in result.findings] == ["hardcoded_secret"]


# ---- rule-backed findings: a kill needs counter-evidence (issue #12) ----------

KILL = {"decision": "kill", "confidence": 0.2, "note": "looks fine to me"}
RULE = "scrutai.injection.os-system"


def test_prompt_tells_the_critic_a_rule_fired() -> None:
    prompt = build_prompt(_finding(sast_rule=RULE), DIFF, 1, 2)
    assert f"SAST: Semgrep rule {RULE} fired on the cited line" in prompt
    assert "SAST:" not in build_prompt(_finding(), DIFF, 1, 2)


def test_kill_without_counter_evidence_becomes_a_challenge() -> None:
    f = judge(Critic(KILL), ScrutaiConfig(), _finding(sast_rule=RULE), DIFF, 1)
    assert f.alive and f.contested and RULE in (f.challenge or "")
    assert f.history[-1].startswith("round 1: challenge")
    assert "kill overruled" in (f.critic_note or "")


def test_kill_without_counter_evidence_in_the_last_round_does_not_stand() -> None:
    cfg = ScrutaiConfig()
    f = judge(Critic(KILL), cfg, _finding(sast_rule=RULE), DIFF, cfg.max_critic_rounds)
    assert f.alive and not f.contested and f.confidence == cfg.min_confidence
    assert f.history[-1].startswith("round 2: uphold")


def test_kill_with_counter_evidence_stands() -> None:
    verdict = {**KILL, "counter_evidence": 'L1: os.system("ls")  # constant'}
    f = judge(Critic(verdict), ScrutaiConfig(), _finding(sast_rule=RULE), DIFF, 1)
    assert not f.alive and "Counter-evidence: L1: os.system" in (f.critic_note or "")


def test_dedupe_keeps_the_rule_whichever_duplicate_wins() -> None:
    from scrutai.critic import dedupe

    backed = _finding(agent="security", confidence=0.5, sast_rule=RULE)
    stronger = _finding(agent="correctness", confidence=0.9)
    for order in ([backed, stronger], [stronger, backed]):
        (kept,) = dedupe(order)
        assert kept.agent == "correctness" and kept.sast_rule == RULE
