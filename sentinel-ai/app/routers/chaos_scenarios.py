"""
Operator-triggered incident scenario runner.

This endpoint exists for demos and validation runs: it lets the frontend ask
AWS Systems Manager to run scripts/incident-scenarios.sh on the K3s EC2 node,
without opening SSH or exposing Sentinel's unauthenticated Alertmanager
webhook surface.
"""
from __future__ import annotations

import secrets
import shlex
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.clients.aws_client import AWSClient
from app.core.config import settings

router = APIRouter(prefix="/api/sentinel/chaos-scenarios", tags=["chaos-scenarios"])


SCENARIOS: dict[str, dict[str, Any]] = {
    "db-outage": {
        "title": "Database outage",
        "family": "application fault",
        "description": "Forces citizen-service DB failures and verifies ChaosDatabaseFailure.",
    },
    "http-errors": {
        "title": "HTTP 5xx storm",
        "family": "application fault",
        "description": "Forces citizen-service 5xx responses and drives error-rate alerts.",
    },
    "latency": {
        "title": "High latency",
        "family": "application fault",
        "description": "Injects 1500ms citizen-service latency and generates request traffic.",
    },
    "notification-degradation": {
        "title": "Notification degradation",
        "family": "application fault",
        "description": "Forces notification delivery failures while citizen requests continue.",
    },
    "high-cpu": {
        "title": "High CPU",
        "family": "application fault",
        "description": "Enables the citizen-service CPU burn worker until HighCPUUsage fires.",
    },
    "memory-leak": {
        "title": "Memory leak",
        "family": "application fault",
        "description": "Retains memory in citizen-service until MemoryLeakSuspected fires.",
    },
    "crashloop": {
        "title": "CrashLoopBackOff",
        "family": "platform fault",
        "description": "Patches citizen-service to exit on startup, then rolls it back.",
    },
    "full-outage": {
        "title": "Full outage",
        "family": "platform fault",
        "description": "Scales citizen-service to zero replicas, then restores it.",
    },
    "bad-deployment": {
        "title": "Bad deployment",
        "family": "release fault",
        "description": "Rolls out a bad DATABASE_HOST. By default, leaves the incident open.",
        "dangerous": True,
        "auto_rollback_supported": True,
    },
    "all": {
        "title": "Run safe suite",
        "family": "suite",
        "description": "Runs every repeatable scenario except bad-deployment.",
    },
}


class ScenarioRunRequest(BaseModel):
    namespace: str = Field(default="citizen-portal", min_length=1, max_length=63)
    auto_rollback: bool = False


class ScenarioRunResponse(BaseModel):
    scenario: str
    instance_id: str
    command_id: str
    status_url: str


def _require_operator(x_chaos_token: str | None) -> None:
    configured = settings.chaos_admin_token
    if not configured or not x_chaos_token or not secrets.compare_digest(
        x_chaos_token, configured
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _runner_instance_id() -> str:
    explicit = settings.chaos_scenario_runner_instance_id.strip()
    if explicit:
        return explicit
    ids = settings.aws_ec2_instance_ids_list
    return ids[0] if ids else ""


def _aws() -> AWSClient:
    return AWSClient(
        region=settings.aws_region,
        access_key_id=settings.aws_access_key_id or None,
        secret_access_key=settings.aws_secret_access_key or None,
    )


def _command_for(scenario: str, payload: ScenarioRunRequest) -> str:
    workdir = shlex.quote(settings.chaos_scenario_runner_workdir)
    scenario_arg = shlex.quote(scenario)
    namespace_arg = shlex.quote(payload.namespace)
    rollback_arg = "true" if payload.auto_rollback else "false"
    return "\n".join(
        [
            "set -euo pipefail",
            f"cd {workdir}",
            "if [ -f .env ]; then set -a; . ./.env; set +a; fi",
            "if [ -z \"${CHAOS_ADMIN_TOKEN:-}\" ] && [ -f k8s/overlays/aws/secrets/citizen-service.env ]; then",
            "  CHAOS_ADMIN_TOKEN=$(grep '^CHAOS_ADMIN_TOKEN=' k8s/overlays/aws/secrets/citizen-service.env | head -1 | cut -d= -f2-)",
            "  export CHAOS_ADMIN_TOKEN",
            "fi",
            "chmod +x scripts/incident-scenarios.sh",
            f"./scripts/incident-scenarios.sh {scenario_arg} {namespace_arg} {rollback_arg}",
        ]
    )


@router.get("")
def list_scenarios(x_chaos_token: str | None = Header(default=None)) -> dict[str, Any]:
    _require_operator(x_chaos_token)
    instance_id = _runner_instance_id()
    return {
        "configured": bool(settings.aws_region and instance_id),
        "instance_id": instance_id or None,
        "workdir": settings.chaos_scenario_runner_workdir,
        "scenarios": [
            {"id": key, **value}
            for key, value in SCENARIOS.items()
        ],
    }


@router.post("/{scenario}/runs", response_model=ScenarioRunResponse)
async def run_scenario(
    scenario: str,
    payload: ScenarioRunRequest,
    x_chaos_token: str | None = Header(default=None),
) -> ScenarioRunResponse:
    _require_operator(x_chaos_token)
    if scenario not in SCENARIOS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown scenario"
        )

    instance_id = _runner_instance_id()
    if not settings.aws_region or not instance_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Set AWS_REGION and either CHAOS_SCENARIO_RUNNER_INSTANCE_ID "
                "or AWS_EC2_INSTANCE_IDS so Sentinel can send the SSM command."
            ),
        )

    try:
        command_id = await _aws().send_shell_command(
            instance_id=instance_id,
            commands=[_command_for(scenario, payload)],
            comment=f"Sentinel chaos scenario: {scenario}",
            timeout_seconds=settings.chaos_scenario_runner_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"failed to start SSM command: {str(exc)[:300]}",
        ) from exc
    return ScenarioRunResponse(
        scenario=scenario,
        instance_id=instance_id,
        command_id=command_id,
        status_url=f"/api/sentinel/chaos-scenarios/runs/{command_id}",
    )


@router.get("/runs/{command_id}")
async def get_run(
    command_id: str,
    x_chaos_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_operator(x_chaos_token)
    instance_id = _runner_instance_id()
    if not settings.aws_region or not instance_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="chaos scenario runner is not configured",
        )
    try:
        return await _aws().get_command_invocation(
            instance_id=instance_id, command_id=command_id
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"failed to read SSM command status: {str(exc)[:300]}",
        ) from exc
