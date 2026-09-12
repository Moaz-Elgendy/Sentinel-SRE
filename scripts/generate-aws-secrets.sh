#!/usr/bin/env bash
#
# Generate the gitignored secret env files the AWS overlay needs.
#
# Run this ONCE per environment, on the EC2 node (over SSM), before the
# first deploy:
#
#   sudo /opt/sentinel-sre/scripts/generate-aws-secrets.sh
#
# It writes k8s/overlays/aws/secrets/*.env with freshly generated random
# values. Those files are gitignored and must never be committed — the
# .example templates next to them are the tracked versions.
#
# Why generated here rather than passed in from CI:
#   Every value except Sentinel's optional third-party credentials is
#   internal to the cluster (database passwords, the JWT signing key, the
#   chaos admin token). Nothing outside the cluster needs to know them, so
#   the fewer places they exist the better — generating them on the node
#   means they never travel through GitHub Actions logs, SSM command
#   parameters, or a Terraform state file.
#
#   Sentinel's OpenAI/GitHub/Slack credentials are the exception: those come
#   from outside and have to be pasted in. This script leaves them blank and
#   tells you where to put them.
#
# Re-running it: it will NOT overwrite existing files by default, because
# regenerating a database password without also resetting the password
# inside PostgreSQL leaves the two out of sync and the service unable to
# connect. Pass --force only if you understand that, and see the note at the
# bottom for what else has to happen.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SECRETS_DIR="${REPO_ROOT}/k8s/overlays/aws/secrets"

FORCE=false
if [ "${1:-}" = "--force" ]; then
  FORCE=true
fi

if [ ! -d "${SECRETS_DIR}" ]; then
  echo "ERROR: ${SECRETS_DIR} does not exist. Is this the repository root?" >&2
  exit 1
fi

# openssl is present on Ubuntu by default; fall back to /dev/urandom rather
# than failing, so this also works on a minimal image.
gen() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

write_file() {
  local path="$1"
  shift
  if [ -f "${path}" ] && [ "${FORCE}" != true ]; then
    echo "  skip   $(basename "${path}") (exists; --force to overwrite)"
    return 0
  fi
  printf '%s\n' "$@" > "${path}"
  chmod 0600 "${path}"
  echo "  wrote  $(basename "${path}")"
}

echo "Generating secrets in ${SECRETS_DIR}"

# One password per database. Deliberately different from each other: a
# credential leak from one service should not grant access to the other's
# data.
CITIZEN_DB_PASSWORD="$(gen)"
NOTIFICATION_DB_PASSWORD="$(gen)"
JWT_SECRET="$(gen)"
GRAFANA_PASSWORD="$(gen)"

# One shared chaos token across both services. They could differ, but
# Sentinel needs whichever value each service uses in order to reset faults
# on it, and a single token means one value to configure rather than two
# that can silently drift apart.
CHAOS_ADMIN_TOKEN="$(gen)"

write_file "${SECRETS_DIR}/citizen-postgres.env" \
  "POSTGRES_USER=app_user" \
  "POSTGRES_PASSWORD=${CITIZEN_DB_PASSWORD}" \
  "POSTGRES_DB=citizen_portal"

write_file "${SECRETS_DIR}/notification-postgres.env" \
  "POSTGRES_USER=app_user" \
  "POSTGRES_PASSWORD=${NOTIFICATION_DB_PASSWORD}" \
  "POSTGRES_DB=notification_service"

# DATABASE_PASSWORD must match the corresponding POSTGRES_PASSWORD above.
# Generating both in one run is the only reason they are guaranteed to
# agree — which is why re-running this for only one file is a bad idea.
write_file "${SECRETS_DIR}/citizen-service.env" \
  "DATABASE_USER=app_user" \
  "DATABASE_PASSWORD=${CITIZEN_DB_PASSWORD}" \
  "JWT_SECRET=${JWT_SECRET}" \
  "CHAOS_ADMIN_TOKEN=${CHAOS_ADMIN_TOKEN}"

write_file "${SECRETS_DIR}/notification-service.env" \
  "DATABASE_USER=app_user" \
  "DATABASE_PASSWORD=${NOTIFICATION_DB_PASSWORD}" \
  "CHAOS_ADMIN_TOKEN=${CHAOS_ADMIN_TOKEN}"

write_file "${SECRETS_DIR}/grafana.env" \
  "GF_SECURITY_ADMIN_USER=admin" \
  "GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD}"

# Sentinel: the chaos token is generated (it is internal), the three
# third-party credentials are left blank for a human to fill in. Sentinel
# starts and runs the full lifecycle with all three empty.
write_file "${SECRETS_DIR}/sentinel.env" \
  "OPENAI_API_KEY=" \
  "GITHUB_TOKEN=" \
  "GITHUB_REPOSITORY=" \
  "SLACK_WEBHOOK_URL=" \
  "CHAOS_ADMIN_TOKEN=${CHAOS_ADMIN_TOKEN}"

cat <<EOF

Done.

Values you may want to note now (they are not printed again):

  Grafana admin password : ${GRAFANA_PASSWORD}
  Chaos admin token      : ${CHAOS_ADMIN_TOKEN}

Grafana has no public route — reach it with:
  sudo kubectl -n citizen-portal port-forward svc/grafana 3000:3000

The chaos token is what scripts/incident-scenarios.sh needs:
  export CHAOS_ADMIN_TOKEN=${CHAOS_ADMIN_TOKEN}

Optional, to enable Sentinel's integrations, edit:
  ${SECRETS_DIR}/sentinel.env
    OPENAI_API_KEY      -> LLM-enriched root cause analysis
    GITHUB_TOKEN        -> automatic incident issues
    GITHUB_REPOSITORY   -> owner/repo for those issues
    SLACK_WEBHOOK_URL   -> incident notifications
  Sentinel works without all of these; it runs rule-based and logs that it
  did so.

Next: scripts/deploy-aws.sh <image-tag>

--------------------------------------------------------------------------
If you ever re-run this with --force, be aware:

  PostgreSQL only reads POSTGRES_PASSWORD when it initialises an EMPTY data
  directory. The PVCs already hold initialised databases, so a new password
  in the Secret will NOT change the password inside PostgreSQL — the
  services will simply fail to authenticate, which presents as
  crash-looping app pods next to perfectly healthy database pods.

  To actually rotate a database password you must also change it in the
  running database:

    sudo kubectl -n citizen-portal exec deploy/citizen-postgres -- \\
      psql -U app_user -d citizen_portal \\
      -c "ALTER USER app_user WITH PASSWORD '<new-password>';"

  Rotating JWT_SECRET is safe but logs every user out.
  Rotating CHAOS_ADMIN_TOKEN is safe; update sentinel.env to match.
--------------------------------------------------------------------------
EOF
