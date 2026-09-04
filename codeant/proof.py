"""Evidence-based completion checks for CodeAnt.

AntProof deliberately stays small and dependency-free.  A proof contract is
loaded once at the beginning of a run, then executed after the model submits a
final answer.  The contract is immutable for that run so a model cannot make
the verifier weaker by editing its configuration.
"""

from __future__ import annotations

import fnmatch
import os
import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


PROOF_MODES = ("off", "auto", "strict")
PROOF_STATUS_PENDING = "pending"
PROOF_STATUS_NOT_REQUIRED = "not_required"
PROOF_STATUS_VERIFIED = "verified"
PROOF_STATUS_UNVERIFIED = "unverified"
PROOF_STATUS_FAILED = "failed"


def _clip(value, limit=4000):
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _digest(payload):
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ProofCheck:
    check_id: str
    command: str
    timeout: int = 120
    required: bool = True

    def to_dict(self):
        return {
            "id": self.check_id,
            "command": self.command,
            "timeout": self.timeout,
            "required": self.required,
        }


@dataclass(frozen=True)
class ProofContract:
    schema_version: int = 1
    mode: str = "auto"
    expects_changes: bool = False
    allowed_change_paths: tuple[str, ...] = ()
    checks: tuple[ProofCheck, ...] = ()
    source: str = "none"

    @property
    def configured(self):
        return bool(self.checks or self.expects_changes or self.allowed_change_paths)

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "expects_changes": self.expects_changes,
            "allowed_change_paths": list(self.allowed_change_paths),
            "checks": [check.to_dict() for check in self.checks],
            "source": self.source,
        }

    @property
    def digest(self):
        payload = self.to_dict()
        payload.pop("source", None)
        return _digest(payload)

    @classmethod
    def empty(cls, mode="auto"):
        return cls(mode=mode)

    @classmethod
    def load(cls, root, mode=None, proof_path=None, verify_commands=None):
        requested_mode = None if mode is None else str(mode).lower()
        if requested_mode is not None and requested_mode not in PROOF_MODES:
            raise ValueError(f"proof mode must be one of: {', '.join(PROOF_MODES)}")
        if requested_mode == "off":
            return cls.empty(mode="off")

        commands = [str(item).strip() for item in (verify_commands or ()) if str(item).strip()]
        if proof_path:
            path = Path(proof_path)
            if not path.is_absolute():
                path = Path(root) / path
        else:
            path = Path(root) / ".codeant" / "proof.json"
        payload = None
        source = "none"
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"cannot load proof contract {path}: {exc}") from exc
            source = str(path)
            if not isinstance(payload, dict):
                raise ValueError("proof contract must be a JSON object")
        else:
            payload = {}

        effective_mode = requested_mode or str(payload.get("mode", "auto")).lower()
        if effective_mode not in PROOF_MODES:
            raise ValueError(f"proof mode must be one of: {', '.join(PROOF_MODES)}")
        if effective_mode == "off":
            return cls.empty(mode="off")

        if commands:
            payload["checks"] = [
                {"id": f"cli_{index}", "command": command, "required": True}
                for index, command in enumerate(commands, start=1)
            ]
            source = "cli"

        schema_version = int(payload.get("schema_version", 1))
        if schema_version != 1:
            raise ValueError(f"unsupported proof schema_version: {schema_version}")

        raw_checks = payload.get("checks", [])
        if not isinstance(raw_checks, list):
            raise ValueError("proof checks must be a list")
        checks = []
        seen_ids = set()
        for index, raw_check in enumerate(raw_checks, start=1):
            if isinstance(raw_check, str):
                raw_check = {"id": f"check_{index}", "command": raw_check}
            if not isinstance(raw_check, dict):
                raise ValueError(f"proof check {index} must be an object")
            check_id = str(raw_check.get("id", f"check_{index}")).strip()
            command = str(raw_check.get("command", "")).strip()
            if not check_id or not command:
                raise ValueError(f"proof check {index} requires id and command")
            if check_id in seen_ids:
                raise ValueError(f"duplicate proof check id: {check_id}")
            seen_ids.add(check_id)
            timeout = int(raw_check.get("timeout", 120))
            if timeout < 1 or timeout > 120:
                raise ValueError(f"proof check {check_id} timeout must be in [1, 120]")
            checks.append(
                ProofCheck(
                    check_id=check_id,
                    command=command,
                    timeout=timeout,
                    required=bool(raw_check.get("required", True)),
                )
            )

        paths = payload.get("allowed_change_paths", [])
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list):
            raise ValueError("allowed_change_paths must be a list")
        allowed_paths = tuple(str(item).replace("\\", "/").strip() for item in paths if str(item).strip())
        return cls(
            schema_version=schema_version,
            mode=effective_mode,
            expects_changes=bool(payload.get("expects_changes", False)),
            allowed_change_paths=allowed_paths,
            checks=tuple(checks),
            source=source,
        )

@dataclass
class ProofResult:
    status: str
    contract: ProofContract
    changed_paths: list[str] = field(default_factory=list)
    unauthorized_paths: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    failure_reasons: list[str] = field(default_factory=list)
    verified_at: str = ""
    duration_ms: int = 0

    def to_dict(self):
        return {
            "status": self.status,
            "contract": self.contract.to_dict(),
            "contract_digest": self.contract.digest,
            "changed_paths": list(self.changed_paths),
            "unauthorized_paths": list(self.unauthorized_paths),
            "checks": list(self.checks),
            "failure_reasons": list(self.failure_reasons),
            "verified_at": self.verified_at,
            "duration_ms": self.duration_ms,
        }

    @property
    def passed(self):
        return self.status in {PROOF_STATUS_VERIFIED, PROOF_STATUS_NOT_REQUIRED}

    def feedback(self):
        if self.status == PROOF_STATUS_VERIFIED:
            return "AntProof verification passed. You may provide the final answer."
        if self.status == PROOF_STATUS_NOT_REQUIRED:
            return "AntProof has no configured checks for this task."
        lines = [f"AntProof verification {self.status}."]
        for reason in self.failure_reasons:
            lines.append(f"- {reason}")
        for check in self.checks:
            if check.get("status") not in {"passed", "not_run"}:
                lines.append(
                    f"- {check.get('id')}: exit_code={check.get('exit_code')} "
                    f"{check.get('stderr') or check.get('stdout') or check.get('status', '')}"
                )
        if self.status == PROOF_STATUS_FAILED:
            lines.append("Fix the reported issue, then submit a new <final> answer.")
        return "\n".join(lines)


class AntProofEngine:
    def __init__(self, contract, root, snapshot_fn, redact_fn, approve_fn, shell_env_fn):
        self.contract = contract
        self.root = Path(root)
        self.snapshot_fn = snapshot_fn
        self.redact_fn = redact_fn
        self.approve_fn = approve_fn
        self.shell_env_fn = shell_env_fn

    @staticmethod
    def _path_allowed(path, patterns):
        if not patterns:
            return True
        normalized = str(path).replace("\\", "/")
        return any(
            fnmatch.fnmatch(normalized, pattern)
            or PurePosixPath(normalized).match(pattern)
            for pattern in patterns
        )

    def run(self, baseline, now=None):
        started = time.monotonic()
        now = now or ""
        if self.contract.mode == "off":
            return ProofResult(
                status=PROOF_STATUS_NOT_REQUIRED,
                contract=self.contract,
                verified_at=now,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        if not self.contract.configured:
            status = PROOF_STATUS_UNVERIFIED if self.contract.mode == "strict" else PROOF_STATUS_NOT_REQUIRED
            reasons = ["strict mode requires a proof contract"] if status == PROOF_STATUS_UNVERIFIED else []
            return ProofResult(
                status=status,
                contract=self.contract,
                failure_reasons=reasons,
                verified_at=now,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        current = self.snapshot_fn()
        changed_paths = sorted(set(baseline) | set(current))
        changed_paths = [path for path in changed_paths if baseline.get(path) != current.get(path)]
        unauthorized = [
            path for path in changed_paths
            if not self._path_allowed(path, self.contract.allowed_change_paths)
        ]
        reasons = []
        if self.contract.expects_changes and not changed_paths:
            reasons.append("expected workspace changes, but no files changed")
        if unauthorized:
            reasons.append("changed paths outside allowlist: " + ", ".join(unauthorized))

        checks = []
        for check in self.contract.checks:
            check_result = self._run_check(check)
            checks.append(check_result)
            if check.required and check_result["status"] != "passed":
                reasons.append(f"required check failed: {check.check_id}")

        if any(check.get("required") and check.get("status") == "denied" for check in checks):
            status = PROOF_STATUS_UNVERIFIED
        elif reasons:
            status = PROOF_STATUS_FAILED
        else:
            status = PROOF_STATUS_VERIFIED
        return ProofResult(
            status=status,
            contract=self.contract,
            changed_paths=changed_paths,
            unauthorized_paths=unauthorized,
            checks=checks,
            failure_reasons=reasons,
            verified_at=now,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _run_check(self, check):
        args = {"command": check.command, "timeout": check.timeout}
        if not self.approve_fn("run_shell", args):
            return {
                "id": check.check_id,
                "command": check.command,
                "timeout": check.timeout,
                "required": check.required,
                "status": "denied",
                "exit_code": None,
                "stdout": "",
                "stderr": "verification approval denied",
                "duration_ms": 0,
                "workspace_changes": [],
            }
        started = time.monotonic()
        before = self.snapshot_fn()
        run_kwargs = {}
        if os.name == "nt":
            configured = os.environ.get("ComSpec") or os.environ.get("COMSPEC")
            system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
            run_kwargs["executable"] = configured or system_root + r"\System32\cmd.exe"
        try:
            completed = subprocess.run(
                check.command,
                cwd=self.root,
                shell=True,
                capture_output=True,
                text=True,
                timeout=check.timeout,
                env=self.shell_env_fn(),
                **run_kwargs,
            )
            after = self.snapshot_fn()
            changed, _ = self._changed(before, after)
            exit_code = completed.returncode
            status = "passed" if exit_code == 0 and not changed else "failed"
            stderr = completed.stderr.strip()
            if changed:
                stderr = (stderr + "\n" if stderr else "") + "verification command modified workspace: " + ", ".join(changed)
            return {
                "id": check.check_id,
                "command": check.command,
                "timeout": check.timeout,
                "required": check.required,
                "status": status,
                "exit_code": exit_code,
                "stdout": _clip(self.redact_fn(completed.stdout.strip())),
                "stderr": _clip(self.redact_fn(stderr)),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "workspace_changes": changed,
            }
        except subprocess.TimeoutExpired as exc:
            after = self.snapshot_fn()
            changed, _ = self._changed(before, after)
            stderr = f"verification timed out after {check.timeout}s"
            if changed:
                stderr += "; verification command modified workspace: " + ", ".join(changed)
            return {
                "id": check.check_id,
                "command": check.command,
                "timeout": check.timeout,
                "required": check.required,
                "status": "failed",
                "exit_code": None,
                "stdout": _clip(self.redact_fn(getattr(exc, "stdout", "") or "")),
                "stderr": _clip(self.redact_fn(stderr)),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "workspace_changes": changed,
            }
        except OSError as exc:
            after = self.snapshot_fn()
            changed, _ = self._changed(before, after)
            stderr = f"verification could not start: {exc}"
            if changed:
                stderr += "; verification command modified workspace: " + ", ".join(changed)
            return {
                "id": check.check_id,
                "command": check.command,
                "timeout": check.timeout,
                "required": check.required,
                "status": "failed",
                "exit_code": None,
                "stdout": "",
                "stderr": _clip(self.redact_fn(stderr)),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "workspace_changes": changed,
            }
    @staticmethod
    def _changed(before, after):
        paths = sorted(set(before) | set(after))
        changed = [path for path in paths if before.get(path) != after.get(path)]
        return changed, [path for path in changed]
