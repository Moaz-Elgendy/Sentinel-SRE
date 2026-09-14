"""
Customer / Environment model.

This is the seam the whole "external control plane" refactor turns on. Before
this module existed, `core/config.py` WAS the environment: one Prometheus URL,
one Loki URL, one Kubernetes ServiceAccount, global and singular. Every
lifecycle module reached `app.core.config.settings` (directly or via
`SentinelContext`) for connection info.

After this module, an `Environment` is a normal value the lifecycle carries
around — `Incident.environment_id` says which one an incident belongs to, and
`SentinelContext` (see `lifecycle/orchestrator.py:build_context`) is built
FROM one `Environment`, not from global settings. Settings are still where
policy/validation/execution-mode config lives (those are not
customer-specific in this phase), but connection info for Kubernetes,
Prometheus, Loki, GitHub and AWS all live here now.

PHASE 1 SCOPE: exactly one Environment is registered and used — see
`main.py`'s startup bootstrap and `routers/environments.py`. Nothing here
assumes that will stay true; `SQLiteStore.list_environments()` already
returns a list, and every incident already carries `customer_id` /
`environment_id` / `application_id` (see `models/incident.py`), which is the
part that is expensive to retrofit later. Multi-environment dispatch (routing
an inbound Alertmanager webhook to the RIGHT environment, running several
`SentinelContext`s concurrently) is explicitly NOT built yet — see the
"Known limitations" note in the top-level integration doc.

CREDENTIAL HANDLING: every `*ConnectionConfig` below can carry secrets
(kubeconfig, API tokens). `Environment.to_public_dict()` is the ONLY method
that should ever be used to put an Environment in an HTTP response — it
redacts every secret field to a boolean "configured" flag. `to_dict()` (no
"public") includes secrets and must only be used for internal wiring
(building connectors) and the SQLite row, never for a response body.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class KubernetesConnectionConfig(BaseModel):
    """How to reach this environment's Kubernetes/K3s API.

    `mode` mirrors `Settings.kubernetes_mode` (see core/config.py's module
    docstring for what each mode means) but is per-environment here rather
    than process-global, which is the whole point.
    """

    mode: Literal["in_cluster", "kubeconfig", "remote"] = "kubeconfig"
    namespace: str = "citizen-portal"

    # mode == "kubeconfig"
    kubeconfig_b64: str | None = None

    # mode == "remote"
    api_server: str | None = None
    token: str | None = None
    ca_cert_b64: str | None = None
    verify_ssl: bool = True

    def is_configured(self) -> bool:
        if self.mode == "in_cluster":
            return True
        if self.mode == "kubeconfig":
            return bool(self.kubeconfig_b64)
        return bool(self.api_server and self.token)


class PrometheusConnectionConfig(BaseModel):
    url: str
    bearer_token: str | None = None


class LokiConnectionConfig(BaseModel):
    url: str
    bearer_token: str | None = None


class GitHubConnectionConfig(BaseModel):
    token: str | None = None
    repository: str | None = None  # "owner/repo"

    def is_enabled(self) -> bool:
        return bool(self.token and self.repository and "/" in self.repository)


class AWSConnectionConfig(BaseModel):
    """EC2 + CloudWatch only, per the Phase 1 scope decision.

    Empty access_key_id/secret falls through to boto3's default credential
    chain — see Settings.aws_access_key_id's docstring for why that is the
    preferred path.
    """

    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    ec2_instance_ids: list[str] = Field(default_factory=list)

    def is_enabled(self) -> bool:
        return bool(self.region)


class ApplicationProfile(BaseModel):
    """The lightweight application profile from spec section 20.

    Deliberately flat — no dependency graph, no traffic-pattern modelling.
    Just enough for the evidence collector and the API to say what this
    environment is watching.
    """

    id: str
    name: str
    deployments: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    known_failure_modes: list[str] = Field(default_factory=list)


class Environment(BaseModel):
    """One customer's one monitored environment.

    `application` is singular for now (Phase 1: one workload, the citizen
    portal) — `applications: list[ApplicationProfile]` is the natural
    extension when an environment hosts more than one app, deferred until
    there is a second real one to model against.
    """

    id: str = Field(default_factory=lambda: f"env-{uuid.uuid4().hex[:12]}")
    customer_id: str
    name: str
    created_at: float = Field(default_factory=time.time)

    kubernetes: KubernetesConnectionConfig
    prometheus: PrometheusConnectionConfig
    loki: LokiConnectionConfig
    github: GitHubConnectionConfig = Field(default_factory=GitHubConnectionConfig)
    aws: AWSConnectionConfig = Field(default_factory=AWSConnectionConfig)
    application: ApplicationProfile

    @classmethod
    def bootstrap_from_settings(cls, settings_obj: Any) -> "Environment":
        """Build the single Phase-1 demo Environment from env vars.

        Called once at startup (see main.py) only when the store has no
        environment registered yet. This is what lets Sentinel keep booting
        with zero extra configuration — the property the original
        core/config.py docstring called out as deliberate — while the rest of
        the system already treats connection info as environment data, not
        global settings.
        """
        s = settings_obj
        kubernetes = KubernetesConnectionConfig(
            mode=s.kubernetes_mode,
            namespace=s.allowed_namespaces_list[0] if s.allowed_namespaces_list else "citizen-portal",
            kubeconfig_b64=s.kubernetes_kubeconfig_b64 or None,
            api_server=s.kubernetes_api_server or None,
            token=s.kubernetes_token or None,
            ca_cert_b64=s.kubernetes_ca_cert_b64 or None,
            verify_ssl=s.kubernetes_verify_ssl,
        )
        prometheus = PrometheusConnectionConfig(
            url=s.prometheus_url, bearer_token=s.prometheus_bearer_token or None
        )
        loki = LokiConnectionConfig(
            url=s.loki_url, bearer_token=s.loki_bearer_token or None
        )
        github = GitHubConnectionConfig(
            token=s.github_token or None, repository=s.github_repository or None
        )
        aws = AWSConnectionConfig(
            region=s.aws_region or None,
            access_key_id=s.aws_access_key_id or None,
            secret_access_key=s.aws_secret_access_key or None,
            ec2_instance_ids=s.aws_ec2_instance_ids_list,
        )
        application = ApplicationProfile(
            id=s.application_id,
            name=s.environment_name,
            deployments=s.allowed_deployments_list,
            services=s.allowed_deployments_list,
            known_failure_modes=[
                "bad_deployment", "cpu_saturation", "memory_leak",
                "database_unavailable", "crash_loop", "high_latency",
                "http_error_spike", "notification_failure",
            ],
        )
        return cls(
            id=s.environment_id,
            customer_id=s.customer_id,
            name=s.environment_name,
            kubernetes=kubernetes,
            prometheus=prometheus,
            loki=loki,
            github=github,
            aws=aws,
            application=application,
        )

    def to_dict(self) -> dict[str, Any]:
        """Full record, secrets included. Internal use only — see module docstring."""
        return self.model_dump(mode="json")

    def to_public_dict(self) -> dict[str, Any]:
        """Redacted record safe to return from the API. See module docstring."""
        d = self.to_dict()
        k8s = d["kubernetes"]
        k8s["kubeconfig_configured"] = bool(k8s.pop("kubeconfig_b64", None))
        k8s["token_configured"] = bool(k8s.pop("token", None))
        k8s.pop("ca_cert_b64", None)
        d["prometheus"]["bearer_token_configured"] = bool(
            d["prometheus"].pop("bearer_token", None)
        )
        d["loki"]["bearer_token_configured"] = bool(d["loki"].pop("bearer_token", None))
        d["github"]["token_configured"] = bool(d["github"].pop("token", None))
        d["aws"]["access_key_configured"] = bool(d["aws"].pop("access_key_id", None))
        d["aws"].pop("secret_access_key", None)
        return d
