"""Grid — parallel agent execution. Run N tasks concurrently, collect results."""
from __future__ import annotations

import concurrent.futures
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Task:
    id: str
    prompt: str
    model: str = "claude-haiku-4-5-20251001"
    system: str | None = None
    max_tokens: int = 4096
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskResult:
    task: Task
    status: TaskStatus
    output: str
    tokens_used: int
    latency_ms: float
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.task.id,
            "status": self.status.value,
            "output": self.output,
            "tokens_used": self.tokens_used,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
            "metadata": self.task.metadata,
        }


@dataclass
class GridResult:
    results: list[TaskResult]
    total_latency_ms: float
    wall_time_ms: float  # actual elapsed (parallelism win)

    @property
    def succeeded(self) -> list[TaskResult]:
        return [r for r in self.results if r.status == TaskStatus.DONE]

    @property
    def failed(self) -> list[TaskResult]:
        return [r for r in self.results if r.status == TaskStatus.FAILED]

    def outputs(self) -> list[str]:
        """Ordered list of outputs from succeeded tasks."""
        return [r.output for r in self.succeeded]

    def format_summary(self) -> str:
        lines = [
            f"Grid — {len(self.results)} tasks",
            f"  ✓ {len(self.succeeded)} succeeded  ✗ {len(self.failed)} failed",
            f"  Wall time: {round(self.wall_time_ms/1000, 1)}s  (vs {round(self.total_latency_ms/1000, 1)}s serial)",
            f"  Speedup: {round(self.total_latency_ms / max(self.wall_time_ms, 1), 1)}×",
        ]
        if self.failed:
            lines.append("  Failures:")
            for r in self.failed:
                lines.append(f"    [{r.task.id}] {r.error}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "total": len(self.results),
            "succeeded": len(self.succeeded),
            "failed": len(self.failed),
            "wall_time_ms": round(self.wall_time_ms, 1),
            "total_latency_ms": round(self.total_latency_ms, 1),
            "speedup": round(self.total_latency_ms / max(self.wall_time_ms, 1), 1),
            "results": [r.to_dict() for r in self.results],
        }


def _run_one(task: Task, client: Any) -> TaskResult:
    """Run a single task against the Anthropic API."""
    start = time.perf_counter()
    try:
        kwargs: dict[str, Any] = {
            "model": task.model,
            "max_tokens": task.max_tokens,
            "messages": [{"role": "user", "content": task.prompt}],
        }
        if task.system:
            kwargs["system"] = task.system
        response = client.messages.create(**kwargs)
        output = response.content[0].text if response.content else ""
        tokens = response.usage.input_tokens + response.usage.output_tokens
        latency_ms = (time.perf_counter() - start) * 1000
        return TaskResult(
            task=task,
            status=TaskStatus.DONE,
            output=output,
            tokens_used=tokens,
            latency_ms=latency_ms,
        )
    except Exception as e:
        latency_ms = (time.perf_counter() - start) * 1000
        return TaskResult(
            task=task,
            status=TaskStatus.FAILED,
            output="",
            tokens_used=0,
            latency_ms=latency_ms,
            error=str(e),
        )


class Grid:
    """Run multiple AI agent tasks in parallel using a thread pool."""

    def __init__(self, client: Any, max_workers: int = 10):
        self.client = client
        self.max_workers = max_workers

    def run(
        self,
        tasks: list[Task],
        on_result: Callable[[TaskResult], None] | None = None,
    ) -> GridResult:
        """Execute all tasks in parallel. Returns when all complete."""
        wall_start = time.perf_counter()

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_to_task = {
                pool.submit(_run_one, task, self.client): task
                for task in tasks
            }
            results: list[TaskResult] = []
            for future in concurrent.futures.as_completed(future_to_task):
                result = future.result()
                results.append(result)
                if on_result:
                    on_result(result)

        # Sort by original task order
        task_order = {t.id: i for i, t in enumerate(tasks)}
        results.sort(key=lambda r: task_order.get(r.task.id, 0))

        wall_ms = (time.perf_counter() - wall_start) * 1000
        total_ms = sum(r.latency_ms for r in results)

        return GridResult(
            results=results,
            total_latency_ms=total_ms,
            wall_time_ms=wall_ms,
        )

    def map(
        self,
        prompts: list[str],
        model: str = "claude-haiku-4-5-20251001",
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> GridResult:
        """Simple map: run same model+system across N prompts. Returns ordered results."""
        tasks = [
            Task(
                id=str(uuid.uuid4())[:8],
                prompt=p,
                model=model,
                system=system,
                max_tokens=max_tokens,
            )
            for p in prompts
        ]
        return self.run(tasks)


def make_tasks(
    prompts: list[str],
    model: str = "claude-haiku-4-5-20251001",
    system: str | None = None,
    max_tokens: int = 4096,
) -> list[Task]:
    """Helper: turn a list of prompts into Task objects."""
    return [
        Task(
            id=str(uuid.uuid4())[:8],
            prompt=p,
            model=model,
            system=system,
            max_tokens=max_tokens,
        )
        for p in prompts
    ]
