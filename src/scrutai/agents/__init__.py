from .base import Specialist
from .correctness import CorrectnessAgent
from .security import SecurityAgent
from .tests import TestCoverageAgent

# Registry the orchestrator routes against. Add CrewAI/ADK-backed specialists
# here later behind the same Specialist interface.
REGISTRY: dict[str, type[Specialist]] = {
    "security": SecurityAgent,
    "correctness": CorrectnessAgent,
    "tests": TestCoverageAgent,
}

__all__ = ["Specialist", "REGISTRY", "SecurityAgent", "CorrectnessAgent", "TestCoverageAgent"]
