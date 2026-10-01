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

BACKENDS = ("native", "crewai")


def agent_class(name: str, backend: str = "native") -> type[Specialist]:
    """Resolve a specialist name to its class on the requested framework backend."""
    base = REGISTRY[name]
    if backend == "native":
        return base
    if backend == "crewai":
        try:
            from .crewai_backend import crewai_specialist
        except ImportError as exc:
            raise RuntimeError('backend "crewai" needs: pip install "scrutai[crewai]"') from exc
        return crewai_specialist(base)
    raise ValueError(f"unknown backend {backend!r}; expected one of {BACKENDS}")


__all__ = [
    "BACKENDS",
    "agent_class",
    "REGISTRY",
    "CorrectnessAgent",
    "PerformanceAgent",
    "SecurityAgent",
    "Specialist",
    "StyleAgent",
    "TestCoverageAgent",
]
