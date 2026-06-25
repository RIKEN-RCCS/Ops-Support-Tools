#!/bin/sh
set -eu

SIF_PATH="${RUNBOOK_GATEWAY_SIF:-/opt/support-ai/runbook-gateway.sif}"
TOKEN_PATH="${RUNBOOK_GATEWAY_TOKEN_PATH:-/etc/support-ai/knowledge_write_token}"
API_URL="${RUNBOOK_GATEWAY_KNOWLEDGE_API_URL:-https://fncx.r-ccs.riken.jp}"
MACHINE="${RUNBOOK_GATEWAY_MACHINE:-}"
ENVIRONMENT="${RUNBOOK_GATEWAY_ENVIRONMENT:-}"

exec apptainer exec \
  --cleanenv \
  --env "RUNBOOK_GATEWAY_KNOWLEDGE_API_URL=${API_URL}" \
  --env "RUNBOOK_GATEWAY_MACHINE=${MACHINE}" \
  --env "RUNBOOK_GATEWAY_ENVIRONMENT=${ENVIRONMENT}" \
  --env "RUNBOOK_GATEWAY_TOKEN_FILE=/run/secrets/knowledge_write_token" \
  --bind "${TOKEN_PATH}:/run/secrets/knowledge_write_token:ro" \
  "${SIF_PATH}" \
  runbook-gateway "$@"
