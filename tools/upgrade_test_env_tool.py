"""Dedicated protected tool for test-environment frontend upgrades."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile

from tools.protected_ops import (
    append_protected_ops_audit,
    current_upgrade_request_is_authorized,
    load_test_env_upgrade_config,
    password_file_exists,
)
from tools.registry import registry

UPGRADE_TEST_ENV_SCHEMA = {
    "name": "upgrade_test_env_client",
    "description": (
        "Run the protected test-environment client upgrade. Use this only when "
        "the current gateway message is exactly the trigger phrase "
        "'升级测试环境客户端'. Do not use terminal for this flow; authorization "
        "and trigger matching are enforced in code."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def _trim_output(text: str, limit: int = 6000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _error_payload(message: str, *, stage: str = "validation", **extra) -> str:
    payload = {
        "success": False,
        "stage": stage,
        "error": message,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _create_askpass_helper(password_file: str) -> str:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="hermes-ssh-askpass-",
        suffix=".sh",
        delete=False,
    )
    try:
        handle.write("#!/bin/sh\n")
        handle.write(f"IFS= read -r password < {shlex.quote(password_file)} || exit 1\n")
        handle.write("printf '%s\\n' \"$password\"\n")
        handle.close()
        os.chmod(handle.name, 0o700)
    except Exception:
        try:
            handle.close()
        except Exception:
            pass
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    return handle.name


def _build_ssh_invocation(
    host: str,
    ssh_user: str,
    password_file: str,
    remote_command: str,
) -> tuple[list[str], dict[str, str] | None, str | None]:
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("ssh is not installed on the Hermes host.")

    base_argv = [
        ssh,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-o",
        "PasswordAuthentication=yes",
        "-o",
        "PreferredAuthentications=password,keyboard-interactive",
        "-o",
        "KbdInteractiveAuthentication=yes",
        "-o",
        "PubkeyAuthentication=no",
        f"{ssh_user}@{host}",
        remote_command,
    ]

    sshpass = shutil.which("sshpass")
    if sshpass:
        return [
            sshpass,
            "-f",
            password_file,
            *base_argv,
        ], None, None

    setsid = shutil.which("setsid")
    if not setsid:
        raise RuntimeError(
            "Neither sshpass nor setsid is installed on the Hermes host, "
            "so password-based SSH fallback is unavailable."
        )

    askpass_helper = _create_askpass_helper(password_file)
    env = os.environ.copy()
    env.update(
        {
            "SSH_ASKPASS": askpass_helper,
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": env.get("DISPLAY") or "hermes-protected-op:0",
        }
    )
    return [setsid, *base_argv], env, askpass_helper


def _run_remote_script(script_path: str) -> dict:
    cfg = load_test_env_upgrade_config()
    remote_command = f"bash -lc {shlex.quote(script_path)}"
    argv, env, askpass_helper = _build_ssh_invocation(
        host=cfg.host,
        ssh_user=cfg.ssh_user,
        password_file=cfg.password_file,
        remote_command=remote_command,
    )
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=cfg.timeout_seconds,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    finally:
        if askpass_helper:
            try:
                os.unlink(askpass_helper)
            except OSError:
                pass
    return {
        "returncode": completed.returncode,
        "stdout": _trim_output(completed.stdout),
        "stderr": _trim_output(completed.stderr),
    }


def upgrade_test_env_client(task_id: str | None = None) -> str:
    del task_id
    cfg = load_test_env_upgrade_config()

    authorized, reason = current_upgrade_request_is_authorized(cfg)
    if not authorized:
        append_protected_ops_audit(
            operation="upgrade_test_env_client",
            status="denied",
            details={"reason": reason},
        )
        return _error_payload(reason)

    password_ok, password_error = password_file_exists(cfg)
    if not password_ok:
        append_protected_ops_audit(
            operation="upgrade_test_env_client",
            status="failed",
            details={"reason": password_error},
        )
        return _error_payload(password_error)

    try:
        step1 = _run_remote_script(cfg.step1)
    except subprocess.TimeoutExpired:
        message = f"Step 1 timed out after {cfg.timeout_seconds}s."
        append_protected_ops_audit(
            operation="upgrade_test_env_client",
            status="failed",
            details={"stage": "step1", "reason": message},
        )
        return _error_payload(message, stage="step1")
    except Exception as exc:
        message = f"Failed to start step 1: {exc}"
        append_protected_ops_audit(
            operation="upgrade_test_env_client",
            status="failed",
            details={"stage": "step1", "reason": str(exc)},
        )
        return _error_payload(message, stage="step1")

    if step1["returncode"] != 0:
        append_protected_ops_audit(
            operation="upgrade_test_env_client",
            status="failed",
            details={"stage": "step1", **step1},
        )
        return json.dumps(
            {
                "success": False,
                "stage": "step1",
                "error": "Step 1 failed; step 2 was not executed.",
                "step1": step1,
            },
            ensure_ascii=False,
        )

    try:
        step2 = _run_remote_script(cfg.step2)
    except subprocess.TimeoutExpired:
        message = f"Step 2 timed out after {cfg.timeout_seconds}s."
        append_protected_ops_audit(
            operation="upgrade_test_env_client",
            status="failed",
            details={"stage": "step2", "reason": message, "step1": step1},
        )
        return _error_payload(message, stage="step2", step1=step1)
    except Exception as exc:
        message = f"Failed to start step 2: {exc}"
        append_protected_ops_audit(
            operation="upgrade_test_env_client",
            status="failed",
            details={"stage": "step2", "reason": str(exc), "step1": step1},
        )
        return _error_payload(message, stage="step2", step1=step1)

    success = step2["returncode"] == 0
    audit_status = "completed" if success else "failed"
    append_protected_ops_audit(
        operation="upgrade_test_env_client",
        status=audit_status,
        details={"step1": step1, "step2": step2},
    )

    return json.dumps(
        {
            "success": success,
            "stage": "completed" if success else "step2",
            "summary": (
                "Protected test-environment client upgrade completed successfully."
                if success
                else "Step 2 failed after step 1 succeeded."
            ),
            "step1": step1,
            "step2": step2,
        },
        ensure_ascii=False,
    )


registry.register(
    name="upgrade_test_env_client",
    toolset="protected_ops",
    schema=UPGRADE_TEST_ENV_SCHEMA,
    handler=lambda args, **kw: upgrade_test_env_client(task_id=kw.get("task_id")),
    check_fn=lambda: os.name != "nt",
    emoji="🛡️",
    max_result_size_chars=20_000,
)
