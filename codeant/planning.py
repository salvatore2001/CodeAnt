"""Typed task planning and LangGraph orchestration for CodeAnt.

The planner is intentionally separate from the existing model/tool loop.  A
``PlanSpec`` is a small, validated contract; ``PlanningGraph`` advances that
contract through deterministic state transitions.  This lets CodeAnt adopt
structured planning without giving a language model direct control over the
workflow graph.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Callable, Literal

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class PlanMode(str, Enum):
    OFF = "off"
    AUTO = "auto"
    STRICT = "strict"


class PlanPhase(str, Enum):
    NOT_REQUIRED = "not_required"
    PLANNING = "planning"
    READY = "ready"
    EXECUTING = "executing"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=500)
    depends_on: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "low"
    status: Literal["pending", "in_progress", "completed", "blocked"] = "pending"


class PlanSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    plan_id: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=1000)
    assumptions: list[str] = Field(default_factory=list)
    steps: list[PlanStep] = Field(min_length=1, max_length=32)
    proof_checks: list[str] = Field(default_factory=list)
    budget_steps: int = Field(default=6, ge=1, le=100)

    @model_validator(mode="after")
    def validate_dependencies(self):
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("plan step ids must be unique")
        known = set(ids)
        for step in self.steps:
            unknown = sorted(set(step.depends_on) - known)
            if unknown:
                raise ValueError(f"step {step.id} depends on unknown steps: {', '.join(unknown)}")
            if step.id in step.depends_on:
                raise ValueError(f"step {step.id} cannot depend on itself")
        self._validate_acyclic()
        return self

    def _validate_acyclic(self):
        graph = {step.id: set(step.depends_on) for step in self.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str):
            if step_id in visiting:
                raise ValueError("plan step dependencies must be acyclic")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in graph[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in graph:
            visit(step_id)

    @classmethod
    def fallback(cls, goal: str, plan_id: str):
        return cls(
            plan_id=plan_id,
            goal=goal,
            assumptions=["The agent will inspect the repository before making changes."],
            steps=[
                PlanStep(
                    id="inspect_and_execute",
                    objective="Inspect the repository, make the smallest safe change, and verify it.",
                    acceptance_criteria=["The requested task is addressed and evidence is available."],
                    risk="medium",
                )
            ],
        )


class PlanEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["step_done", "step_blocked", "tool_executed", "reset"]
    step_id: str = ""
    summary: str = ""


class PlanState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    plan: PlanSpec
    phase: PlanPhase = PlanPhase.PLANNING
    current_step: str = ""
    completed_steps: list[str] = Field(default_factory=list)
    blocked_reason: str = ""
    events: list[dict[str, Any]] = Field(default_factory=list)
    event: PlanEvent | None = None

    def to_dict(self):
        return self.model_dump(mode="json", exclude={"event"})


def _apply_event(state: PlanState):
    event = state.event
    if event is None:
        return {"phase": PlanPhase.READY}
    updates: dict[str, Any] = {
        "events": [*state.events, event.model_dump(mode="json")],
    }
    if event.kind == "reset":
        updates.update(
            phase=PlanPhase.PLANNING,
            current_step="",
            completed_steps=[],
            blocked_reason="",
        )
    elif event.kind == "step_done" and event.step_id:
        known_ids = {step.id for step in state.plan.steps}
        if event.step_id not in known_ids:
            return {
                **updates,
                "phase": PlanPhase.BLOCKED,
                "current_step": "",
                "blocked_reason": f"unknown plan step: {event.step_id}",
            }
        completed = list(dict.fromkeys([*state.completed_steps, event.step_id]))
        updates.update(completed_steps=completed, blocked_reason="")
    elif event.kind == "step_blocked":
        updates.update(phase=PlanPhase.BLOCKED, blocked_reason=event.summary or "step blocked")
    return updates


def _select_next_step(state: PlanState):
    if state.phase == PlanPhase.BLOCKED:
        return {
            "phase": PlanPhase.BLOCKED,
            "current_step": "",
            "blocked_reason": state.blocked_reason,
        }
    completed = set(state.completed_steps)
    steps = {step.id: step for step in state.plan.steps}
    if completed >= set(steps):
        return {"phase": PlanPhase.COMPLETED, "current_step": ""}
    for step in state.plan.steps:
        if step.id in completed:
            continue
        if set(step.depends_on) <= completed:
            return {"phase": PlanPhase.EXECUTING, "current_step": step.id}
    unresolved = [step.id for step in state.plan.steps if step.id not in completed]
    return {
        "phase": PlanPhase.BLOCKED,
        "current_step": "",
        "blocked_reason": "no executable plan step; unresolved dependencies: " + ", ".join(unresolved),
    }


def _route(state: PlanState):
    if state.phase == PlanPhase.BLOCKED:
        return "blocked"
    if state.phase == PlanPhase.COMPLETED:
        return "completed"
    return "executing"


def build_planning_graph():
    """Build a deterministic LangGraph that advances one planning event.

    The graph does not execute shell or file tools.  CodeAnt remains the only
    owner of side effects; the graph decides which validated plan step should
    be active next.  This boundary keeps the workflow auditable and testable.
    """

    graph = StateGraph(PlanState)
    graph.add_node("apply_event", _apply_event)
    graph.add_node("select_next_step", _select_next_step)
    graph.add_edge(START, "apply_event")
    graph.add_edge("apply_event", "select_next_step")
    graph.add_conditional_edges(
        "select_next_step",
        _route,
        {"executing": END, "completed": END, "blocked": END},
    )
    return graph.compile()


class PlanningGraph:
    """Small stateful wrapper around the compiled LangGraph."""

    def __init__(self, plan: PlanSpec):
        self.graph = build_planning_graph()
        self.state = PlanState(plan=plan)

    def advance(self, event: PlanEvent | None = None):
        self.state = self.graph.invoke(
            self.state.model_copy(update={"event": event}),
        )
        if isinstance(self.state, dict):
            self.state = PlanState.model_validate(self.state)
        return self.state

    def mark_step_done(self, step_id: str, summary=""):
        return self.advance(PlanEvent(kind="step_done", step_id=step_id, summary=summary))

    def mark_step_blocked(self, reason):
        return self.advance(PlanEvent(kind="step_blocked", summary=reason))


def extract_json_object(raw: str):
    """Extract the first JSON object from a model response."""

    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.strip().startswith("```")).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("planner response does not contain a JSON object")
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"planner response is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("planner response must be a JSON object")
    return value


class StructuredPlanner:
    """Generate and validate a PlanSpec using the existing model client."""

    def __init__(self, model_client, max_new_tokens=700, redact_fn: Callable[[str], str] | None = None):
        self.model_client = model_client
        self.max_new_tokens = max_new_tokens
        self.redact_fn = redact_fn or (lambda value: str(value))

    def build_prompt(self, user_message: str, workspace_summary: str):
        return (
            "You are CodeAnt's planning agent. Return ONLY one valid JSON object "
            "matching the PlanSpec schema below; do not use markdown.\n"
            "PlanSpec fields: schema_version=1, plan_id, goal, assumptions[], "
            "steps[], proof_checks[], budget_steps. Each step has id, objective, "
            "depends_on[], allowed_paths[], acceptance_criteria[], risk(low|medium|high).\n"
            "Create a short acyclic plan. Never invent files or claim tests passed.\n\n"
            f"User request:\n{self.redact_fn(user_message)}\n\n"
            f"Workspace summary:\n{self.redact_fn(workspace_summary)}"
        )

    def generate(self, user_message: str, workspace_summary: str, plan_id: str):
        raw = self.model_client.complete(
            self.build_prompt(user_message, workspace_summary),
            self.max_new_tokens,
        )
        payload = extract_json_object(raw)
        payload.setdefault("schema_version", 1)
        payload.setdefault("plan_id", plan_id)
        payload.setdefault("goal", user_message)
        return PlanSpec.model_validate(payload)


__all__ = [
    "PlanEvent",
    "PlanMode",
    "PlanPhase",
    "PlanSpec",
    "PlanState",
    "PlanStep",
    "PlanningGraph",
    "StructuredPlanner",
    "ValidationError",
    "build_planning_graph",
    "extract_json_object",
]
