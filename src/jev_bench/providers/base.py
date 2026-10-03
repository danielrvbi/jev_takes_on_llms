"""Common transport boundary and measurement result."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from pydantic import BaseModel


@dataclass
class ProviderResult:
    output: BaseModel | None = None
    values: dict[str, Any] = field(default_factory=dict)
    raw_response: Any = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str = ""


@dataclass(frozen=True)
class Request:
    state: str
    questions: dict
    schema: type[BaseModel]
    system_prompt: str


@dataclass(frozen=True)
class InvocationContext:
    audit_directory: Path
    purpose: str = "measurement"
    prefix_experiment: bool = False


class Provider(Protocol):
    def invoke(self, request: Request, context: InvocationContext) -> ProviderResult: ...
