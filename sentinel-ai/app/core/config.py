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

### External control plane (Phase 1)

Sentinel is designed to run OUTSIDE the Kubernetes cluster it watches. These
settings are the bootstrap path only: at startup, if no environment has been
registered yet (via ``POST /environments``), Sentinel constructs exactly one
``Environment`` record from the fields below and registers it as the default
demo environment — see ``app/domain/environment.py`` and
``app/routers/environments.py``. Everything downstream of that (the
Kubernetes/Prometheus/Loki/GitHub connectors, the evidence collector, the
Decision/Policy/Remediation/Validation engines) is environment-scoped, not
settings-scoped, so registering additional environments later does not
require touching this file or any lifecycle module.

``KUBERNETES_MODE`` selects how Sentinel reaches the cluster:

* ``in_cluster`` (default) — the original behaviour: an in-cluster
  ServiceAccount. Only valid when Sentinel is deployed inside the same
  cluster as the workload. Kept as the default so existing deployments are
  unaffected by this change.
* ``kubeconfig`` — a base64-encoded kubeconfig YAML in
  ``KUBERNETES_KUBECONFIG_B64``. This is the normal way to run Sentinel as an
  external control plane against a remote K3s/K8s API server.
* ``remote`` — explicit ``KUBERNETES_API_SERVER`` + ``KUBERNETES_TOKEN`` (a
  long-lived ServiceAccount token, ideally scoped to the same minimal Role
  documented in ``clients/kubernetes_client.py:REQUIRED_RBAC``) +
  ``KUBERNETES_CA_CERT_B64``. Useful when a full kubeconfig is more than the
  remote environment wants to hand over.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- Identity -------------------------------------------------------
    service_name: str = "sentinel-ai"
    version: str = "0.1.0"

    # ---- Multi-tenancy identity (Phase 1: exactly one customer/environment)
    # These seed the single bootstrap Environment record. Not hardcoded as
    # magic strings elsewhere — see app/domain/environment.py.
    customer_id: str = "demo-customer"
    environment_id: str = "demo-env"
    environment_name: str = "citizen-portal-demo"
    application_id: str = "citizen-portal"

    # ---- Kubernetes remote connection (see module docstring) -------------
    kubernetes_mode: str = "in_cluster"  # in_cluster | kubeconfig | remote
    kubernetes_kubeconfig_b64: str = ""
    kubernetes_api_server: str = ""
    kubernetes_token: str = ""
    kubernetes_ca_cert_b64: str = ""
    kubernetes_verify_ssl: bool = True

    # ---- Observability backends -----------------------------------------
    # Reachable remotely. Sentinel only ever *reads* from these. When
    # Sentinel runs outside the cluster, these must resolve from wherever
    # Sentinel is deployed (e.g. the K3s node's public/VPN IP with Traefik
    # routing to the Service), not the in-cluster DNS name.
    prometheus_url: str = "http://prometheus:9090"
    prometheus_bearer_token: str = ""
    loki_url: str = "http://loki:3100"
    loki_bearer_token: str = ""
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

    # ---- LLM / AI reasoning (optional) ------------------------------------
    # `llm_provider` selects the Reasoner implementation (app/reasoning/) —
    # business logic (rca.py) only ever talks to the `Reasoner` interface, so
    # switching provider never touches lifecycle code, and a future
    # `LocalSentinelReasoner` slots in the same way.
    llm_provider: str = "openai"  # openai | gemini

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    gemini_timeout_seconds: float = 20.0

    # Empty key => rule-based RCA only. Everything still works; we log it and
    # record `llm_used=false` on the incident so post-hoc analysis is honest.
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 20.0
    # Empty (default) = talk to real OpenAI. Any OpenAI-API-compatible
    # provider works here without touching rca.py at all — e.g. Groq
    # ("https://api.groq.com/openai/v1" with openai_model="llama-3.3-70b-
    # versatile") or OpenRouter ("https://openrouter.ai/api/v1"). Sentinel
    # only ever uses chat.completions.create() with response_format=
    # json_object, so any provider advertising OpenAI-SDK compatibility and
    # JSON mode for its chosen model is a drop-in swap — confirm both
    # before relying on it, since not every free-tier model supports JSON
    # mode.
    openai_base_url: str = ""

    # ---- Documentation sinks (both optional, both no-op when unset) ------
    github_token: str = ""
    github_repository: str = ""  # "owner/repo"
    slack_webhook_url: str = ""

    # ---- AWS (optional, EC2 + CloudWatch evidence only) -------------------
    # Never hardcode keys. Empty values fall through to boto3's normal
    # credential chain (env vars, shared config file, instance/IRSA role),
    # which is the recommended path — these fields exist for the case where
    # Sentinel runs somewhere that chain does not reach, e.g. a laptop
    # pointed at a customer's AWS account via a scoped IAM user for the demo.
    aws_region: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_ec2_instance_ids: str = ""  # comma-separated; empty = skip EC2 evidence
    chaos_scenario_runner_instance_id: str = ""
    chaos_scenario_runner_workdir: str = "/opt/sentinel-sre"
    chaos_scenario_runner_timeout_seconds: int = 1800

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

    # ---- Sentinel SRE Control Center (GUI) admin auth --------------------
    # This is a SEPARATE identity system from citizen-service's citizen JWT
    # auth (different audience: SRE operators, not citizens) and from
    # CHAOS_ADMIN_TOKEN (a single shared secret for one machine-triggered
    # demo endpoint, not a login system with per-admin audit trail). See
    # app/core/security.py and app/routers/auth.py.
    #
    # `sentinel_jwt_secret` has NO safe default. An empty value at startup
    # means main.py generates a random one for this process lifetime only —
    # every existing GUI session is invalidated on every restart. That is
    # loud and inconvenient on purpose: a hardcoded fallback secret would be
    # the kind of thing that quietly ships to a real deployment. Set this
    # explicitly for anything longer-lived than a local demo.
    sentinel_jwt_secret: str = ""
    sentinel_jwt_algorithm: str = "HS256"
    sentinel_jwt_expire_minutes: int = 8 * 60  # 8h admin session

    # Bootstrap admin, created once at startup if the `admins` table is
    # empty (mirrors Environment.bootstrap_from_settings' "zero extra
    # configuration to start" property). Same reasoning as the JWT secret
    # applies to the password: no safe hardcoded default, so an empty value
    # means main.py generates a random one and logs it once, loudly, instead
    # of shipping a guessable "admin/admin".
    sentinel_admin_username: str = "admin"
    sentinel_admin_password: str = ""

    # Comma-separated origins the Sentinel GUI is served from. No default
    # wildcard: this API now issues bearer tokens, and `allow_origins=["*"]`
    # combined with credentialed requests is exactly the CORS misconfiguration
    # the citizen-portal frontend's own history (see Phases.md) already
    # warns about.
    sentinel_gui_origins: str = "http://localhost:5173,http://localhost:8081"

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
    def sentinel_gui_origins_list(self) -> list[str]:
        return [o.strip() for o in self.sentinel_gui_origins.split(",") if o.strip()]

    @property
    def aws_ec2_instance_ids_list(self) -> list[str]:
        return [i.strip() for i in self.aws_ec2_instance_ids.split(",") if i.strip()]

    @property
    def llm_enabled(self) -> bool:
        """Whether the *configured* provider has what it needs to run.

        Kept as one property (rather than provider-specific ones scattered
        through the codebase) because everything outside app/reasoning/ only
        needs the yes/no answer — see app/reasoning/factory.py for the
        provider-specific check this delegates to.
        """
        if self.llm_provider == "gemini":
            return bool(self.gemini_api_key.strip())
        return bool(self.openai_api_key.strip())

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_token.strip() and self.github_repository.strip())

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_webhook_url.strip())

    @property
    def aws_enabled(self) -> bool:
        # Region is the one thing boto3 cannot always infer; treat it as the
        # signal that AWS evidence collection was deliberately configured.
        return bool(self.aws_region.strip())

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
