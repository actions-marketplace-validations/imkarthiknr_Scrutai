"""Load and validate `.scrutai.yml`.

Every knob a user is allowed to turn lives here, with sane defaults so the tool
runs with no config file at all.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from .models import Severity


class AgentModels(BaseModel):
    # A cheap model routes; a strong model runs the critic. Per-role models keep
    # cost down without sacrificing the judgement that matters.
    router: str = "gpt-4o-mini"
    specialist: str = "gpt-4o-mini"
    critic: str = "claude-3-5-sonnet-latest"


class ScrutaiConfig(BaseModel):
    enabled_agents: list[str] = Field(
        default_factory=lambda: ["security", "correctness", "tests", "performance", "style"]
    )
    # Only findings at or above this severity are posted.
    min_severity: Severity = Severity.LOW
    # Only findings the critic scores at/above this survive.
    min_confidence: float = 0.6
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude: list[str] = Field(default_factory=lambda: ["**/vendor/**", "**/*.lock", "**/dist/**"])
    models: AgentModels = Field(default_factory=AgentModels)
    max_critic_rounds: int = 2
    # ReAct budget per specialist: tool calls + the final answer.
    max_agent_steps: int = 4
    # Parallel critic/defense calls (specialists always fan out concurrently).
    concurrency: int = 4
    token_budget: int = 200_000
    # Severity at which the CLI/Action exits non-zero (fails CI).
    fail_on: Severity = Severity.HIGH
    # "mock" runs the whole pipeline offline with canned findings; "live" calls
    # a real provider via LiteLLM.
    llm_mode: str = "mock"
    # "heuristic" (free, deterministic) or "llm" (the router model may narrow
    # the heuristic selection further; it can never add agents).
    routing: str = "heuristic"
    # Semgrep: "auto" runs it when installed, "off" never, "required" errors if
    # missing. semgrep_config is "bundled" (offline ruleset) or any --config value.
    semgrep: str = "auto"
    semgrep_config: str = "bundled"

    @field_validator("enabled_agents")
    @classmethod
    def _known_agents(cls, names: list[str]) -> list[str]:
        from .agents import REGISTRY  # local: agents import this module

        unknown = [n for n in names if n not in REGISTRY]
        if unknown:
            raise ValueError(f"unknown agent(s) {unknown}; available: {sorted(REGISTRY)}")
        return names

    @field_validator("llm_mode")
    @classmethod
    def _known_mode(cls, mode: str) -> str:
        if mode not in ("mock", "live"):
            raise ValueError("llm_mode must be 'mock' or 'live'")
        return mode

    @field_validator("semgrep")
    @classmethod
    def _known_semgrep(cls, mode: str) -> str:
        if mode not in ("auto", "off", "required"):
            raise ValueError("semgrep must be 'auto', 'off' or 'required'")
        return mode

    @field_validator("routing")
    @classmethod
    def _known_routing(cls, routing: str) -> str:
        if routing not in ("heuristic", "llm"):
            raise ValueError("routing must be 'heuristic' or 'llm'")
        return routing

    @classmethod
    def load(cls, path: str | Path = ".scrutai.yml") -> ScrutaiConfig:
        p = Path(path)
        if not p.exists():
            return cls()
        data = yaml.safe_load(p.read_text()) or {}
        return cls.model_validate(data)
