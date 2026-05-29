"""Chain — YAML workflow runner for North9 tools."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


@dataclass
class Step:
    id: str
    tool: str
    args: dict[str, Any]
    on_error: str = "stop"   # "stop" | "continue" | "skip"
    description: str = ""


@dataclass
class StepResult:
    step: Step
    output: str
    success: bool
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.step.id,
            "tool": self.step.tool,
            "success": self.success,
            "output": self.output,
            "error": self.error,
        }


@dataclass
class WorkflowResult:
    name: str
    results: list[StepResult]
    completed: bool  # True if all steps ran (no stop-on-error)

    @property
    def succeeded(self) -> list[StepResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[StepResult]:
        return [r for r in self.results if not r.success]

    def format_report(self) -> str:
        lines = [f"Chain — {self.name}", "=" * 50]
        for r in self.results:
            icon = "✓" if r.success else "✗"
            lines.append(f"  {icon} [{r.step.id}] {r.step.tool}")
            if r.error:
                lines.append(f"      Error: {r.error}")
            elif r.output:
                preview = r.output[:100].replace("\n", " ")
                lines.append(f"      → {preview}{'…' if len(r.output) > 100 else ''}")
        lines.append("")
        lines.append(f"  {len(self.succeeded)}/{len(self.results)} steps succeeded")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "completed": self.completed,
            "succeeded": len(self.succeeded),
            "failed": len(self.failed),
            "results": [r.to_dict() for r in self.results],
        }


def _interpolate(value: Any, context: dict[str, StepResult]) -> Any:
    """Replace {{ step_id.output }} references with actual values."""
    if isinstance(value, str):
        def replace(m: re.Match) -> str:
            expr = m.group(1).strip()
            parts = expr.split(".")
            if len(parts) == 2:
                step_id, attr = parts
                if step_id in context and hasattr(context[step_id], attr):
                    return str(getattr(context[step_id], attr))
            return m.group(0)  # leave unchanged if not found
        return re.sub(r"\{\{\s*([^}]+)\s*\}\}", replace, value)
    elif isinstance(value, dict):
        return {k: _interpolate(v, context) for k, v in value.items()}
    elif isinstance(value, list):
        return [_interpolate(v, context) for v in value]
    return value


class Workflow:
    """A sequence of tool calls with data passing between steps."""

    def __init__(self, name: str, steps: list[Step]):
        self.name = name
        self.steps = steps

    @classmethod
    def from_yaml(cls, path: str | Path) -> Workflow:
        if not _YAML_AVAILABLE:
            raise ImportError("PyYAML required: pip install pyyaml")
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> Workflow:
        steps = []
        for s in data.get("steps", []):
            step_id = s.get("id", f"step_{len(steps)}")
            steps.append(Step(
                id=step_id,
                tool=s["tool"],
                args=s.get("args", {}),
                on_error=s.get("on_error", "stop"),
                description=s.get("description", ""),
            ))
        return cls(name=data.get("name", "workflow"), steps=steps)

    def run(self, executor: ToolExecutor) -> WorkflowResult:
        """Run all steps. executor.call(tool_name, **kwargs) -> str."""
        results: list[StepResult] = []
        context: dict[str, StepResult] = {}
        completed = True

        for step in self.steps:
            interpolated_args = _interpolate(step.args, context)
            try:
                output = executor.call(step.tool, **interpolated_args)
                result = StepResult(step=step, output=str(output), success=True)
            except Exception as e:
                result = StepResult(step=step, output="", success=False, error=str(e))

            results.append(result)
            context[step.id] = result

            if not result.success and step.on_error == "stop":
                completed = False
                break

        return WorkflowResult(name=self.name, results=results, completed=completed)


class ToolExecutor:
    """Calls tools by name. Registry maps tool_name -> callable."""

    def __init__(self, registry: dict[str, Any] | None = None):
        self._registry: dict[str, Any] = registry or {}

    def register(self, name: str, fn: Any) -> None:
        self._registry[name] = fn

    def call(self, tool_name: str, **kwargs: Any) -> str:
        if tool_name not in self._registry:
            raise ValueError(f"Tool '{tool_name}' not registered. Available: {list(self._registry)}")
        return self._registry[tool_name](**kwargs)
