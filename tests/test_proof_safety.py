import json
import sys

from codeant import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext


def python_check(source):
    return f'{sys.executable} -c "{source}"'


def build_agent(tmp_path, outputs, contract, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    config = tmp_path / ".codeant" / "proof.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(contract), encoding="utf-8")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".codeant" / "sessions"),
        approval_policy=kwargs.pop("approval_policy", "auto"),
        proof_mode="strict",
        proof_repair_rounds=0,
        **kwargs,
    )


def test_antproof_rejects_changes_outside_contract_allowlist(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="unexpected.txt"><content>surprise</content></tool>',
            "<final>Done.</final>",
        ],
        {
            "schema_version": 1,
            "expects_changes": True,
            "allowed_change_paths": ["README.md"],
        },
    )

    answer = agent.ask("Only change README.md")

    assert "AntProof: FAILED" in answer
    assert "outside allowlist" in answer
    assert agent.current_task_state.status == "failed"


def test_antproof_fails_when_verifier_mutates_workspace(tmp_path):
    agent = build_agent(
        tmp_path,
        ["<final>Run the verifier.</final>"],
        {
            "schema_version": 1,
            "checks": [
                {
                    "id": "impure",
                    "command": python_check("from pathlib import Path; Path('generated.txt').write_text('side effect')"),
                }
            ],
        },
    )

    answer = agent.ask("Verify without changing the workspace")

    assert "AntProof: FAILED" in answer
    proof = agent.run_store.load_proof(agent.current_task_state)
    check = proof["latest"]["checks"][0]
    assert check["status"] == "failed"
    assert check["workspace_changes"] == ["generated.txt"]
    assert "verification command modified workspace" in check["stderr"]


def test_antproof_redacts_secrets_from_evidence(tmp_path, monkeypatch):
    secret = "proof-secret-value-123"
    monkeypatch.setenv("CODEANT_TEST_SECRET", secret)
    agent = build_agent(
        tmp_path,
        ["<final>Verified.</final>"],
        {
            "schema_version": 1,
            "checks": [{"id": "redaction", "command": f"echo {secret}"}],
        },
        secret_env_names=["CODEANT_TEST_SECRET"],
    )

    agent.ask("Verify redaction")

    raw_evidence = agent.run_store.proof_path(agent.current_task_state).read_text(encoding="utf-8")
    assert secret not in raw_evidence
    assert "<redacted>" in raw_evidence


def test_optional_denied_check_does_not_block_proof(tmp_path):
    agent = build_agent(
        tmp_path,
        ["<final>Verified.</final>"],
        {
            "schema_version": 1,
            "checks": [{"id": "optional", "command": python_check("print('ok')"), "required": False}],
        },
        approval_policy="never",
    )

    answer = agent.ask("Run optional verification")

    assert "AntProof: VERIFIED" in answer
    assert agent.current_task_state.status == "completed"
    assert agent.run_store.load_proof(agent.current_task_state)["latest"]["checks"][0]["status"] == "denied"