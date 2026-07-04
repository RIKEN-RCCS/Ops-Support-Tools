# Runbook Gateway

Apptainer-based CLI gateway for real-machine operators and AI agents.

The gateway lets authorized local users claim reviewed **real-machine runbook tasks**, inspect the runbook documents, keep a lease alive, submit execution results, and release work. It talks only to the narrow Knowledge API handoff endpoints. It is not used for parent investigation cases, DB/RAG/web research tasks, or policy-decision tasks.

## Security Model

Use an external write token file. Do not bake the token into the SIF.

```text
group: support-ai-runbook

/opt/support-ai/runbook-gateway.sif
  owner: root
  group: support-ai-runbook
  mode: 750

/etc/support-ai/knowledge_write_token
  owner: root
  group: support-ai-runbook
  mode: 640

/usr/local/bin/runbook-gateway
  owner: root
  group: support-ai-runbook
  mode: 750
```

Users outside `support-ai-runbook` cannot execute the wrapper or read the token. Users inside the group can use the gateway and should be treated as trusted runbook operators.

## Build

Run this from the repository root:

```bash
apptainer build runbook-gateway.sif apps/runbook-gateway/Apptainer.def
```

Install:

```bash
install -o root -g support-ai-runbook -m 750 runbook-gateway.sif /opt/support-ai/runbook-gateway.sif
install -o root -g support-ai-runbook -m 750 apps/runbook-gateway/runbook-gateway-wrapper.sh /usr/local/bin/runbook-gateway
install -o root -g support-ai-runbook -m 640 knowledge_write_token /etc/support-ai/knowledge_write_token
```

`knowledge_write_token` must match `KNOWLEDGE_API_WRITE_TOKEN_FILE` configured on the central Knowledge API.

## Commands

Claim the oldest suitable `review_passed` real-machine task run:

```bash
runbook-gateway claim
```

Claim by target:

```bash
runbook-gateway claim \
  --machine RIKYU \
  --task-type real_machine \
  --capability read_only \
  --document-kind runbook-plan \
  --document-tag cuda
```

For specialized agents or people, claim filters can narrow the queue by `--parent-run-id`, `--task-type`, `--executor-mode`, and `--capability`. This keeps read-only agents, compile-capable agents, and human-with-AI work separated while using the same Knowledge API lease mechanism.

Show a claimed run and attached documents:

```bash
runbook-gateway show --run-id RUN_ID --include-body
```

Extend the lease:

```bash
runbook-gateway heartbeat \
  --run-id RUN_ID \
  --claim-token CLAIM_TOKEN
```

Submit execution results:

```bash
runbook-gateway submit \
  --run-id RUN_ID \
  --claim-token CLAIM_TOKEN \
  --node c000 \
  --cuda-version "CUDA module/version observed" \
  --compiler-version "GCC version observed" \
  --mpi-version "MPI/HPC-X version observed" \
  --reproducibility single_observation \
  --reuse-scope "RIKYU module selection for CUDA/GCC/MPI" \
  --staleness-triggers "driver, CUDA, GCC, MPI, module tree, or maintenance update" \
  --findings-file findings.md \
  --issue-on-run-file issue_on_run.md \
  --summary-file summary.md \
  --answer-draft-file answer_draft.md \
  --next-status result_registered
```

Use the provenance flags whenever a result may become reusable knowledge. Reusable does not mean permanently reproducible: include the machine, node, versions, modules, commands, job conditions, and staleness triggers so later agents can decide whether to reuse the finding, refresh it on the machine, or ask a human for policy confirmation.

Release without completing:

```bash
runbook-gateway release \
  --run-id RUN_ID \
  --claim-token CLAIM_TOKEN \
  --next-status review_passed
```

## Required Knowledge API Endpoints

The supercomputer side only needs these central endpoints:

```text
POST /api/runs/claim
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/documents?include_body=1
POST /api/runs/{run_id}/claim/heartbeat
POST /api/runs/{run_id}/claim/release
POST /api/runs/{run_id}/execution-result
```

Keep `/knowledge/`, `/api/search`, `/api/documents`, `/api/document-handoffs`, `/api/runs` list, and generic document creation closed to the supercomputer side unless there is a separate operational need.
