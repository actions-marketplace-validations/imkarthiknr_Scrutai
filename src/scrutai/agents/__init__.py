from .base import Specialist
from .correctness import CorrectnessAgent
from .performance import PerformanceAgent
from .security import SecurityAgent
from .style import StyleAgent
from .tests import TestCoverageAgent

# Registry the orchestrator routes against. Add CrewAI/ADK-backed specialists
# here later behind the same Specialist interface.
REGISTRY: dict[str, type[Specialist]] = {
    "security": SecurityAgent,
    "correctness": CorrectnessAgent,
    "tests": TestCoverageAgent,
    "performance": PerformanceAgent,
    "style": StyleAgent,
}

__all__ = [
    "REGISTRY",
    "CorrectnessAgent",
    "PerformanceAgent",
    "SecurityAgent",
    "Specialist",
    "StyleAgent",
    "TestCoverageAgent",
]
