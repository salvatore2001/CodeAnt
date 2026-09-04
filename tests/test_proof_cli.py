import json

from codeant import build_agent, build_arg_parser, build_welcome


def test_cli_accepts_antproof_options():
    args = build_arg_parser().parse_args(
        [
            "--proof",
            "strict",
            "--proof-config",
            "custom-proof.json",
            "--verify-command",
            "python -m pytest -q",
            "--verify-command",
            "python -m ruff check .",
            "--proof-repair-rounds",
            "1",
        ]
    )

    assert args.proof == "strict"
    assert args.proof_config == "custom-proof.json"
    assert args.verify_commands == ["python -m pytest -q", "python -m ruff check ."]
    assert args.proof_repair_rounds == 1


def test_repo_contract_mode_is_effective_when_cli_does_not_override(tmp_path):
    config = tmp_path / ".codeant" / "proof.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps({"schema_version": 1, "mode": "strict", "expects_changes": True}),
        encoding="utf-8",
    )
    args = build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "ollama"])

    agent = build_agent(args)

    assert args.proof is None
    assert agent.proof_mode == "strict"
    welcome = build_welcome(agent, model="fake", host="local")
    assert "PROOF" in welcome
    assert "strict" in welcome
