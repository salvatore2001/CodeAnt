import json
import sys

import pytest

from codeant import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from codeant.proof import ProofContract
from codeant.task_state import STOP_REASON_PROOF_FAILED, STOP_REASON_PROOF_UNVERIFIED


def python_check(source):
    return f'{sys.executable} -c "{source}"'


def build_agent(tmp_path, outputs, contract=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    if contract is not None:
        config_path = tmp_path / ".codeant" / "proof.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(contract), encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".codeant" / "sessions"),
        approval_policy=kwargs.pop("approval_policy", "auto"),
        **kwargs,
    )


def test_antproof_verifies_final_and_persists_evidence(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="patch_file" path="README.md"><old_text>demo</old_text><new_text>verified</new_text></tool>',
            "<final>Updated the README.</final>",
        ],
        contract={
            "schema_version": 1,
            "mode": "strict",
            "expects_changes": True,
            "allowed_change_paths": ["README.md"],
            "checks": [
                {
                    "id": "readme",
                    "command": python_check("from pathlib import Path; assert Path('README.md').read_text().startswith('verified')"),
                    "required": True,
                }
            ],
        },
        proof_mode="strict",
    )

    answer = agent.ask("Update README.md")

    assert "AntProof: VERIFIED" in answer
    assert agent.current_task_state.status == "completed"
    assert agent.current_task_state.proof_status == "verified"
    proof = agent.run_store.load_proof(agent.current_task_state.run_id)
    assert proof["latest"]["status"] == "verified"
    assert proof["latest"]["changed_paths"] == ["README.md"]
    assert proof["latest"]["checks"][0]["status"] == "passed"
    assert proof["contract_digest"].startswith("sha256:")


def test_antproof_failed_final_returns_evidence_to_model_and_repairs(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Done without changing anything.</final>",
            '<tool name="patch_file" path="README.md"><old_text>demo</old_text><new_text>repaired</new_text></tool>',
            "<final>Repaired the file.</final>",
        ],
        contract={
            "schema_version": 1,
            "expects_changes": True,
            "allowed_change_paths": ["README.md"],
            "checks": [
                {
                    "id": "readme",
                    "command": python_check("from pathlib import Path; assert 'repaired' in Path('README.md').read_text()"),
                }
            ],
        },
        proof_mode="strict",
        proof_repair_rounds=2,
    )

    answer = agent.ask("Repair README.md")

    assert "AntProof: VERIFIED" in answer
    proof = agent.run_store.load_proof(agent.current_task_state.run_id)
    assert [attempt["status"] for attempt in proof["attempts"]] == ["failed", "verified"]
    proof_history = [item for item in agent.session["history"] if item.get("name") == "antproof"]
    assert proof_history
    assert "expected workspace changes" in proof_history[0]["content"]
    assert "AntProof contract:" in agent.model_client.prompts[-1]


def test_strict_mode_does_not_complete_when_required_check_fails(tmp_path):
    agent = build_agent(
        tmp_path,
        ["<final>This is not actually verified.</final>"],
        contract={
            "schema_version": 1,
            "checks": [
                {
                    "id": "always_fails",
                    "command": python_check("raise SystemExit(7)"),
                }
            ],
        },
        proof_mode="strict",
        proof_repair_rounds=0,
    )

    answer = agent.ask("Finish the task")

    assert "AntProof: FAILED" in answer
    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == STOP_REASON_PROOF_FAILED
    assert agent.current_task_state.proof_status == "failed"


def test_strict_mode_marks_denied_verifier_unverified(tmp_path):
    agent = build_agent(
        tmp_path,
        ["<final>Cannot run the verifier.</final>"],
        contract={
            "schema_version": 1,
            "checks": [{"id": "denied", "command": python_check("print('ok')")}],
        },
        proof_mode="strict",
        proof_repair_rounds=0,
        approval_policy="never",
    )

    answer = agent.ask("Verify this task")

    assert "AntProof: UNVERIFIED" in answer
    assert agent.current_task_state.status == "stopped"
    assert agent.current_task_state.stop_reason == STOP_REASON_PROOF_UNVERIFIED


def test_auto_mode_without_contract_keeps_legacy_answer(tmp_path):
    agent = build_agent(tmp_path, ["<final>Read-only answer.</final>"], proof_mode="auto")

    answer = agent.ask("Explain the README")

    assert answer == "Read-only answer."
    assert agent.current_task_state.status == "completed"
    assert agent.current_task_state.proof_status == "not_required"
    assert agent.run_store.proof_path(agent.current_task_state).is_file()


def test_strict_mode_requires_a_contract(tmp_path):
    agent = build_agent(
        tmp_path,
        ["<final>No contract.</final>"],
        proof_mode="strict",
        proof_repair_rounds=0,
    )

    answer = agent.ask("Change the project")

    assert "AntProof: UNVERIFIED" in answer
    assert "strict mode requires a proof contract" in answer
    assert agent.current_task_state.stop_reason == STOP_REASON_PROOF_UNVERIFIED


def test_contract_rejects_duplicate_check_ids(tmp_path):
    config_path = tmp_path / "proof.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "checks": [
                    {"id": "same", "command": "one"},
                    {"id": "same", "command": "two"},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate proof check id"):
        ProofContract.load(tmp_path, proof_path=config_path)


def test_cli_verify_commands_override_configured_checks(tmp_path):
    config_path = tmp_path / "proof.json"
    config_path.write_text(
        json.dumps({"schema_version": 1, "checks": [{"id": "repo", "command": "repo-check"}]}),
        encoding="utf-8",
    )

    contract = ProofContract.load(
        tmp_path,
        proof_path=config_path,
        verify_commands=["first", "second"],
    )

    assert contract.source == "cli"
    assert [check.check_id for check in contract.checks] == ["cli_1", "cli_2"]
    assert [check.command for check in contract.checks] == ["first", "second"]


def test_reset_clears_antproof_runtime_state(tmp_path):
    agent = build_agent(
        tmp_path,
        ["<final>Read-only answer.</final>"],
        contract={"schema_version": 1, "checks": [{"id": "ok", "command": python_check("print('ok')")}]},
        proof_mode="strict",
        proof_repair_rounds=0,
    )

    agent.ask("Run the proof")
    assert agent.last_proof_result is not None
    assert agent.proof_records

    agent.reset()

    assert agent.current_task_state is None
    assert agent.current_run_dir is None
    assert agent.proof_baseline == {}
    assert agent.proof_records == []
    assert agent.last_proof_result is None
    assert agent.render_proof_text() == ""

def test_off_mode_does_not_write_proof_artifact(tmp_path):
    agent = build_agent(
        tmp_path,
        ["<final>Legacy answer.</final>"],
        contract={"schema_version": 1, "checks": [{"id": "ignored", "command": python_check("raise SystemExit(1)")}]},
        proof_mode="off",
    )

    assert agent.ask("Keep legacy behavior") == "Legacy answer."
    assert not agent.run_store.proof_path(agent.current_task_state).exists()