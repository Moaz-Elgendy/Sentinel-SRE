"""
AWS connector — EC2 + CloudWatch, read-only.

Scope decision (spec section 16 / the follow-up prioritisation message):
implement a clean connector, not a rushed integration that would delay the
primary bad-deployment demo. This client is fully functional (it will
genuinely return EC2 instance state and CloudWatch metric data if given
credentials/instance IDs), but it is NOT YET called from
lifecycle/investigation.py or included in the Evidence Package — see
docs/sentinel-integration.md, "Known limitations", for what wiring it in
would take (one new field on the Evidence Package, one new investigation.py
call guarded by `environment.aws.is_enabled()`, matching the shape every
other collector already follows).

Like every other client in this package: broad `except Exception`, log and
return None/empty rather than raise, because a missing/misconfigured AWS
connector should degrade the incident's evidence quality, not crash the
lifecycle. `boto3` is imported lazily so the rest of Sentinel — including
every test — has no hard dependency on it.

Credentials: never hardcoded. `access_key_id`/`secret_access_key` are
optional constructor arguments; when omitted, boto3 falls through to its own
default credential chain (env vars, shared config file, an instance/IRSA
role), which is the preferred path — see
AWSConnectionConfig's docstring in app/domain/environment.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class AWSClient:
    def __init__(
        self,
        region: str | None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        self.region = region
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key

    @property
    def enabled(self) -> bool:
        return bool(self.region)

    def _session_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"region_name": self.region}
        if self.access_key_id and self.secret_access_key:
            kwargs["aws_access_key_id"] = self.access_key_id
            kwargs["aws_secret_access_key"] = self.secret_access_key
        return kwargs

    # ---- EC2 --------------------------------------------------------------
    def describe_instances(self, instance_ids: list[str]) -> list[dict[str, Any]]:
        """Synchronous on purpose — boto3 has no native async client, and
        callers (once wired in) should run this via `asyncio.to_thread`,
        matching how kubernetes_client.py wraps its own sync SDK calls.
        """
        if not self.enabled or not instance_ids:
            return []
        try:
            import boto3  # noqa: PLC0415
        except ImportError as exc:
            logger.warning("boto3_unavailable", extra={"error_detail": str(exc)[:120]})
            return []
        try:
            ec2 = boto3.client("ec2", **self._session_kwargs())
            resp = ec2.describe_instances(InstanceIds=instance_ids)
        except Exception as exc:  # noqa: BLE001 - botocore raises many types
            logger.warning("ec2_describe_instances_failed", extra={"error_detail": str(exc)[:300]})
            return []
        out: list[dict[str, Any]] = []
        for reservation in resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                out.append(
                    {
                        "instance_id": inst.get("InstanceId"),
                        "state": (inst.get("State") or {}).get("Name"),
                        "instance_type": inst.get("InstanceType"),
                        "availability_zone": (inst.get("Placement") or {}).get(
                            "AvailabilityZone"
                        ),
                        "launch_time": str(inst.get("LaunchTime")) if inst.get("LaunchTime") else None,
                    }
                )
        return out

    # ---- CloudWatch ---------------------------------------------------
    def get_metric_statistics(
        self,
        namespace: str,
        metric_name: str,
        dimensions: list[dict[str, str]],
        start_time: Any,
        end_time: Any,
        period_seconds: int = 60,
        statistics: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Thin wrapper over `get_metric_statistics`. Returns the raw
        datapoints (already-labelled dicts) rather than a summary — the
        Evidence Package normalisation, once this is wired in, should decide
        how to fold this into `metrics`, matching how prometheus.py returns
        raw samples for correlation.py to interpret.
        """
        if not self.enabled:
            return []
        try:
            import boto3  # noqa: PLC0415
        except ImportError as exc:
            logger.warning("boto3_unavailable", extra={"error_detail": str(exc)[:120]})
            return []
        try:
            cw = boto3.client("cloudwatch", **self._session_kwargs())
            resp = cw.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name,
                Dimensions=dimensions,
                StartTime=start_time,
                EndTime=end_time,
                Period=period_seconds,
                Statistics=statistics or ["Average"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cloudwatch_get_metric_statistics_failed",
                extra={"metric_name": metric_name, "error_detail": str(exc)[:300]},
            )
            return []
        return sorted(resp.get("Datapoints", []), key=lambda d: d.get("Timestamp", 0))

    # ---- SSM Run Command -----------------------------------------------
    async def send_shell_command(
        self,
        instance_id: str,
        commands: list[str],
        comment: str,
        timeout_seconds: int = 1800,
    ) -> str:
        """Run an allowlisted shell command on the K3s node through SSM."""
        if not self.enabled:
            raise RuntimeError("AWS region is not configured")

        def _call() -> str:
            import boto3  # noqa: PLC0415

            ssm = boto3.client("ssm", **self._session_kwargs())
            resp = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Comment=comment[:100],
                Parameters={
                    "commands": commands,
                    "executionTimeout": [str(timeout_seconds)],
                },
                TimeoutSeconds=timeout_seconds,
            )
            return resp["Command"]["CommandId"]

        return await asyncio.to_thread(_call)

    async def get_command_invocation(
        self, instance_id: str, command_id: str
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("AWS region is not configured")

        def _call() -> dict[str, Any]:
            import boto3  # noqa: PLC0415

            ssm = boto3.client("ssm", **self._session_kwargs())
            try:
                resp = ssm.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=instance_id,
                )
            except Exception as exc:  # noqa: BLE001
                error = getattr(exc, "response", {}).get("Error", {})
                if error.get("Code") == "InvocationDoesNotExist":
                    return {
                        "command_id": command_id,
                        "instance_id": instance_id,
                        "status": "Pending",
                        "status_details": "Waiting for SSM invocation",
                        "response_code": -1,
                        "stdout": "",
                        "stderr": "",
                        "execution_start": "",
                        "execution_end": "",
                    }
                raise
            return {
                "command_id": command_id,
                "instance_id": instance_id,
                "status": resp.get("Status"),
                "status_details": resp.get("StatusDetails"),
                "response_code": resp.get("ResponseCode"),
                "stdout": resp.get("StandardOutputContent", ""),
                "stderr": resp.get("StandardErrorContent", ""),
                "execution_start": str(resp.get("ExecutionStartDateTime") or ""),
                "execution_end": str(resp.get("ExecutionEndDateTime") or ""),
            }

        return await asyncio.to_thread(_call)
