"""
Sentinel configuration.

Same pattern as citizen-service: pydantic-settings, everything from the
environment, no secrets in code. Field names map to UPPER_SNAKE env vars
automatically (``prometheus_url`` <- ``PROMETHEUS_URL``).

Two deliberate design notes:

1. Every value has a default that is correct for the in-cluster deployment in
   namespace `citizen-portal`. That means Sentinel starts and does something
   useful with an empty env, which matters because a misconfigured
   *observability* service that refuses to boot is worse than one that boots
   with sane defaults and says so in a log line.

2. `dry_run` defaults to **False**. Sentinel is autonomous by design — it
   restarts, rolls back and scales without a human approval gate. That is the
   point of the project. The safety net is NOT "a human clicks yes"; it is the
   Policy Engine (allow-lists + confidence thresholds + action caps +
   cooldowns) in app/lifecycle/policy.py. If you want a human in the loop, set
   DRY_RUN=true and read the incident record.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- Identity -------------------------------------------------------
    service_name: str = "sentinel-ai"
    version: str = "0.1.0"

    # ---- Observability backends -----------------------------------------
    # In-cluster Service DNS. Sentinel only ever *reads* from these.
    prometheus_url: str = "http://prometheus:9090"
    loki_url: str = "http://loki:3100"
    alertmanager_url: str = "http://alertmanager:9093"

    # ---- Target services ------------------------------------------------
    # Used for HTTP health validation (/readyz JSON parsing) and for the
    # chaos reset action. Ports are 8000 because both Python services listen
    # on 8000 inside the cluster.
    citizen_service_url: str = "http://citizen-service:8000"
    notification_service_url: str = "http://notification-service:8000"
    frontend_url: str = "http://frontend:80"

    # Shared secret for POST /api/chaos/reset. Note the app services return
    # **404** (not 401) on a wrong/missing token, so "404" from a chaos call
    # means "bad token or chaos disabled", never "endpoint missing".
    chaos_admin_token: str = ""

    # ---- LLM (optional) --------------------------------------------------
    # Empty key => rule-based RCA only. Everything still works; we log it and
    # record `llm_used=false` on the incident so post-hoc analysis is honest.
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 20.0

    # ---- Documentation sinks (both optional, both no-op when unset) ------
    github_token: str = ""
    github_repository: str = ""  # "owner/repo"
    slack_webhook_url: str = ""

    # ---- Policy: allow-lists --------------------------------------------
    # Comma-separated. Anything not on these lists cannot be touched, ever.
    allowed_namespaces: str = "citizen-portal"
    allowed_deployments: str = "citizen-service,notification-service,frontend"

    # Hard deny-list, enforced *in addition* to the allow-list so that a
    # sloppy ALLOWED_DEPLOYMENTS env value cannot make the databases
    # remediable. Postgres is stateful and a restart/rollback/scale of it is
    # a data-loss risk that no confidence score justifies, so a Postgres
    # incident always escalates to a human. This list is NOT env-configurable
    # on purpose.
    denied_deployments_frozen: tuple[str, ...] = (
        "citizen-postgres",
        "notification-postgres",
    )
    denied_namespaces_frozen: tuple[str, ...] = (
        "kube-system",
        "kube-public",
        "kube-node-lease",
    )

    # ---- Policy: confidence thresholds ----------------------------------
    # Rollback is the most disruptive action (it changes what code is
    # running), so it needs near-certainty. The others are recoverable.
    confidence_threshold_rollback: float = 0.95
    confidence_threshold_restart: float = 0.90
    confidence_threshold_scale: float = 0.90
    confidence_threshold_chaos_reset: float = 0.90

    # ---- Policy: bounds and rate limits ---------------------------------
    min_replicas: int = 1  # never, ever 0 — that is an outage, not a fix
    max_replicas: int = 3  # single-node K3s; more than this just pends
    max_actions_per_incident: int = 3
    action_cooldown_seconds: int = 120
    deployment_correlation_window_minutes: int = 30

    # ---- Validation ------------------------------------------------------
    validation_settle_seconds: int = 20
    validation_timeout_seconds: int = 180
    validation_poll_interval_seconds: int = 10

    # Recovery thresholds. error_rate is a ratio (0.05 == 5% of requests
    # returning 5xx), latency is seconds at p95.
    validation_max_error_rate: float = 0.05
    validation_max_p95_latency_seconds: float = 1.5
    validation_max_cpu_cores: float = 0.9  # from process_cpu_seconds_total rate
    validation_max_memory_bytes: float = 700_000_000.0

    # ---- Execution mode --------------------------------------------------
    dry_run: bool = False

    # ---- Persistence -----------------------------------------------------
    sentinel_db_path: str = "/data/sentinel.db"

    # ---- Detection -------------------------------------------------------
    # How long an incident stays "open for dedup" after its last update. A
    # repeat firing inside this window joins the existing incident instead of
    # opening a new one, so Alertmanager's repeat_interval does not produce a
    # storm of duplicate incidents.
    incident_dedup_window_seconds: int = 3600

    # ---- Derived helpers -------------------------------------------------
    @property
    def allowed_namespaces_list(self) -> list[str]:
        return [n.strip() for n in self.allowed_namespaces.split(",") if n.strip()]

    @property
    def allowed_deployments_list(self) -> list[str]:
        return [d.strip() for d in self.allowed_deployments.split(",") if d.strip()]

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key.strip())

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_token.strip() and self.github_repository.strip())

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_webhook_url.strip())

    def base_url_for(self, target: str) -> str | None:
        """Map a deployment name to its in-cluster base URL.

        Returns None for targets we have no HTTP surface for — the validation
        phase treats that as "HTTP health check unavailable" rather than
        "unhealthy", and the rollback policy treats it as "recovery
        validation not available" and refuses to roll back.
        """
        return {
            "citizen-service": self.citizen_service_url,
            "notification-service": self.notification_service_url,
            "frontend": self.frontend_url,
        }.get(target)


settings = Settings()
