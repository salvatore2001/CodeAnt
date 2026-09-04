import json

import pytest

from codeant import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from codeant.planning import PlanEvent, PlanSpec, PlanStep, PlanningGraph
from codeant.task_state import STOP_REASON_PLAN_FAILED, TaskState


def build_agent(tmp_path, outputs, plan_mode="auto"):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".codeant" / "sessions"),
        approval_policy="auto",
        proof_mode="off",
        plan_mode=plan_mode,
        plan_max_tokens=500,
        max_steps=5,
    )


def test_plan_spec_rejects_cycles_and_unknown_dependencies():
    with pytest.raises(ValueError, match="unknown steps"):
        PlanSpec(
            plan_id="p1",
            goal="demo",
            steps=[PlanStep(id="a", objective="a", depends_on=["missing"])],
        )

    with pytest.raises(ValueError, match="acyclic"):
        PlanSpec(
            plan_id="p2",
            goal="demo",
            steps=[
                PlanStep(id="a", objective="a", depends_on=["b"]),
                PlanStep(id="b", objective="b", depends_on=["a"]),
            ],
        )


def test_planning_graph_advances_dependencies_and_blocks_unknown_step():
    plan = PlanSpec(
        plan_id="p1",
        goal="demo",
        steps=[
            PlanStep(id="inspect", objective="inspect"),
            PlanStep(id="change", objective="change", depends_on=["inspect"]),
        ],
    )
    graph = PlanningGraph(plan)

    assert graph.advance().current_step == "inspect"
    assert graph.mark_step_done("inspect").current_step == "change"
    assert graph.mark_step_done("change").phase.value == "completed"

    blocked = graph.advance(PlanEvent(kind="step_done", step_id="missing"))
    assert blocked.phase.value == "blocked"
    assert "unknown plan step" in blocked.blocked_reason


def test_auto_planning_persists_plan_and_injects_current_step(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            json.dumps(
                {
                    "goal": "update README",
                    "steps": [
                        {"id": "inspect", "objective": "inspect README"},
                        {"id": "change", "objective": "change README", "depends_on": ["inspect"]},
                    ],
                }
            ),
            '<plan_step_done id="inspect">README inspected.</plan_step_done>',
            '<plan_step_done id="change">README changed.</plan_step_done>',
            "<final>Done.</final>",
        ],
    )

    answer = agent.ask("Update README")

    assert "Plan: COMPLETED" in answer
    assert agent.current_task_state.plan_phase == "completed"
    assert agent.current_task_state.completed_steps == ["inspect", "change"]
    plan = agent.run_store.load_plan(agent.current_task_state)
    assert plan["plan"]["goal"] == "update README"
    assert plan["state"]["current_step"] == ""
    assert "Structured task plan:" in agent.model_client.prompts[1]
    assert "Current step: inspect" in agent.model_client.prompts[1]


def test_auto_planning_falls_back_when_model_returns_invalid_json(tmp_path):
    agent = build_agent(tmp_path, ["not json", "<final>Done.</final>"], plan_mode="auto")

    answer = agent.ask("Explain README")

    assert "Plan: COMPLETED" in answer
    assert "planner response does not contain a JSON object" in answer
    assert agent.current_task_state.status == "completed"
    assert agent.run_store.load_plan(agent.current_task_state)["source"] == "fallback"


def test_strict_planning_failure_does_not_complete_task(tmp_path):
    agent = build_agent(tmp_path, ["invalid", "<final>Done.</final>"], plan_mode="strict")

    answer = agent.ask("Perform the task")

    assert "Plan: FAILED" in answer
    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == STOP_REASON_PLAN_FAILED

def test_planning_graph_blocks_out_of_order_step():
    plan = PlanSpec(
        plan_id="p-order",
        goal="demo",
        steps=[
            PlanStep(id="first", objective="first"),
            PlanStep(id="second", objective="second", depends_on=["first"]),
        ],
    )
    graph = PlanningGraph(plan)
    graph.advance()

    blocked = graph.mark_step_done("second")

    assert blocked.phase.value == "blocked"
    assert "current step is first" in blocked.blocked_reason


def test_plan_gate_rejects_disallowed_tool_and_path(tmp_path):
    agent = build_agent(tmp_path, [], plan_mode="auto")
    agent.plan = PlanSpec(
        plan_id="p-gate",
        goal="edit source",
        steps=[
            PlanStep(
                id="edit",
                objective="edit source",
                allowed_tools=["patch_file"],
                allowed_paths=["src/**"],
            )
        ],
    )
    agent.plan_graph = PlanningGraph(agent.plan)
    agent.plan_state = agent.plan_graph.advance()

    result = agent.run_tool("write_file", {"path": "src/main.py", "content": "pass\n"})
    assert "plan gate denied" in result
    assert not (tmp_path / "src" / "main.py").exists()

    result = agent.run_tool(
        "patch_file",
        {"path": "README.md", "old_text": "demo", "new_text": "changed"},
    )
    assert "outside plan step 'edit' allowed_paths" in result
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "demo\n"


def test_step_verification_evidence_is_required_before_completion(tmp_path):
    plan_payload = {
        "goal": "verify README",
        "steps": [
            {
                "id": "verify",
                "objective": "verify README",
                "allowed_tools": ["read_file"],
                "verification_commands": ["python -c \"import sys; sys.exit(0)\""],
            }
        ],
    }
    agent = build_agent(
        tmp_path,
        [json.dumps(plan_payload), '<plan_step_done id="verify">check passed</plan_step_done>', "<final>Done.</final>"],
    )

    answer = agent.ask("Verify README")

    assert "Plan: COMPLETED" in answer
    artifact = agent.run_store.load_plan(agent.current_task_state)
    assert artifact["state"]["completed_steps"] == ["verify"]
    assert artifact["state"]["step_verifications"]["verify"][0]["status"] == "passed"


def test_failed_verification_triggers_automatic_replan(tmp_path):
    first_plan = {
        "goal": "repair a failing check",
        "steps": [{"id": "verify", "objective": "run the failing check", "verification_commands": ["python -c \"import sys; sys.exit(1)\""]}],
    }
    second_plan = {
        "goal": "repair a failing check",
        "steps": [{"id": "verify_fixed", "objective": "run the fixed check"}],
    }
    agent = build_agent(
        tmp_path,
        [json.dumps(first_plan),
         '<plan_step_done id="verify">first attempt</plan_step_done>',
         '<plan_step_done id="verify">second attempt</plan_step_done>',
         json.dumps(second_plan),
         '<plan_step_done id="verify_fixed">fixed</plan_step_done>',
         '<final>Done.</final>'],
    )
    agent.plan_repair_rounds = 2
    agent.plan_max_replans = 1

    answer = agent.ask("Repair the failing check")

    assert "Plan: COMPLETED" in answer
    assert agent.plan_replan_count == 1
    assert agent.plan_replan_history[0]["status"] == "completed"
    assert agent.current_task_state.plan_id == agent.plan.plan_id
    assert agent.plan.plan_id != ""


def test_plan_checkpoint_resume_restores_unfinished_plan(tmp_path):
    plan = {
        "goal": "finish two steps",
        "steps": [
            {"id": "first", "objective": "finish first", "allowed_tools": ["read_file"]},
            {"id": "second", "objective": "finish second", "depends_on": ["first"], "allowed_tools": ["read_file"]},
        ],
    }
    first = build_agent(tmp_path, [json.dumps(plan)])
    first.current_task_state = TaskState.create(
        run_id=first.new_run_id(), task_id=first.new_task_id(), user_request="Finish two steps"
    )
    first.current_run_dir = first.run_store.start_run(first.current_task_state)
    first.begin_proof()
    first.begin_plan("Finish two steps")
    first._advance_plan(PlanEvent(kind="step_done", step_id="first", summary="first done"))
    first.create_checkpoint(first.current_task_state, "Finish two steps", trigger="test_resume")
    checkpoint = first.current_checkpoint()
    assert checkpoint["plan_phase"] == "executing"
    assert checkpoint["current_step"] == "second"
    assert checkpoint["plan_artifact"]

    second = MiniAgent.from_session(
        model_client=FakeModelClient([
            '<plan_step_done id="second">second done</plan_step_done>',
            '<final>Done.</final>',
        ]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=first.session_store,
        session_id=first.session["id"],
        approval_policy="auto",
        proof_mode="off",
        plan_mode="auto",
        plan_max_tokens=500,
        max_steps=5,
    )
    answer = second.ask("Finish two steps")

    assert "Plan: COMPLETED" in answer
    assert second.plan_source == "checkpoint"
    assert second.plan_state.completed_steps == ["first", "second"]
    assert len(second.model_client.prompts) == 2
