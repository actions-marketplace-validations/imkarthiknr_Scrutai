"""Load and validate `.scrutai.yml`.

Every knob a user is allowed to turn lives here, with sane defaults so the tool
runs with no config file at all.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .models import Severity


class AgentModels(BaseModel):
    # A cheap model routes; a strong model runs the critic. Per-role models keep
    # cost down without sacrificing the judgement that matters.
    router: str = "gpt-4o-mini"
    specialist: str = "gpt-4o-mini"
    critic: str = "claude-3-5-sonnet-latest"


class ScrutaiConfig(BaseModel):
    enabled_agents: list[str] = Field(default_factory=lambda: ["security", "correctness", "tests"])
    # Only findings at or above this severity are posted.
    min_severity: Severity = Severity.LOW
    # Only findings the critic scores at/above this survive.
    min_confidence: float = 0.6
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude: list[str] = Field(default_factory=lambda: ["**/vendor/**", "**/*.lock", "**/dist/**"])
    models: AgentModels = Field(default_factory=AgentModels)
    max_critic_rounds: int = 2
    token_budget: int = 200_000
    # Severity at which the CLI/Action exits non-zero (fails CI).
    fail_on: Severity = Severity.HIGH
    # "mock" runs the whole pipeline offline with canned findings; "live" calls
    # a real provider via LiteLLM.
    llm_mode: str = "mock"

    @classmethod
    def load(cls, path: str | Path = ".scrutai.yml") -> ScrutaiConfig:
        p = Path(path)
        if not p.exists():
            return cls()
        data = yaml.safe_load(p.read_text()) or {}
        return cls.model_validate(data)
