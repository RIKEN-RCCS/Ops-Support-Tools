# Knowledge API

SQLite-backed encrypted storage for support AI findings, investigation cases, real-machine runbooks, answer drafts, and document handoffs.

## Terminology

- **Investigation case**: the parent case run for a Zendesk ticket or support question. It has `task_type=investigation_case` and coordinates routing, knowledge research, real-machine tasks, policy decisions, and answer review.
- **Investigation task**: a task run or typed request that advances one part of the case. Examples are `knowledge_research`, `real_machine_scope`, `real_machine`, `policy_decision`, and answer synthesis/review work.
- **Real-machine scope**: a broad real-machine investigation request from the router. It must be split into small `real_machine` tasks before runbook planning.
- **Runbook**: only the executable/checkable procedure for a `real_machine` task. DB search, RAG search, web search, and policy decisions are investigation tasks, not runbooks.
- **Document**: the durable record attached to a case or task. Documents carry requests, plans, findings, reviews, answers, and handoffs.

## API

```text
GET  /healthz
GET  /
GET  /documents
GET  /documents/{id}/view
GET  /runs
GET  /runs/{id}/view
GET  /handoffs
GET  /handoffs/{id}/view
POST /api/documents
GET  /api/documents/{id}
GET  /api/search?q=...
POST /api/document-handoffs
GET  /api/document-handoffs?status=requested
GET  /api/document-handoffs/{id}
PATCH /api/document-handoffs/{id}
POST /api/runs
GET  /api/runs?status=requested
GET  /api/runs/runnable
POST /api/runs/worker-claim
POST /api/runs/claim
GET  /api/runs/{id}
PATCH /api/runs/{id}
POST /api/runs/{id}/documents
GET  /api/runs/{id}/documents
POST /api/runs/{id}/execution-result
POST /api/runs/{id}/claim/heartbeat
POST /api/runs/{id}/claim/release
```

## Run Payload And Task Metadata

Runs are state machines for investigation cases and investigation tasks. A parent investigation case must have `task_type=investigation_case` and no `parent_run_id`; child task runs must set `parent_run_id` and use a smaller `task_type` such as `real_machine_scope` or `real_machine`.

```json
{
  "ticket_id": 14,
  "parent_run_id": "parent-run-id",
  "task_type": "real_machine",
  "task_priority": "normal",
  "required_capabilities": ["read_only", "compile"],
  "executor_mode": "human_with_ai",
  "risk_level": "medium",
  "approval_required": true,
  "environment": "RIKYU",
  "machine": "RIKYU",
  "status": "requested",
  "summary": "Verify CUDA-aware MPI compile path.",
  "runbook": "# Real-Machine Investigation Request\n\n..."
}
```

`task_type` and `status` are required for new runs. Unknown task types and unknown statuses are rejected. This repository is still in construction, so the API intentionally fails fast instead of creating ambiguous legacy runs.

`GET /api/runs/runnable` groups runs into `ai_worker_claimable`, `real_machine_claimable`, `human_required`, and in-progress buckets. Each item includes a `dependency` object with `blocked_by`, `unblocks_when`, `runnable_by`, and child task status summaries, so a worker or human can see why a case is waiting and what completion will unblock it. Use it to see what workers or agents can safely process next. `GET /api/runs`, `POST /api/runs/worker-claim`, and `POST /api/runs/claim` accept `parent_run_id` and `task_type` filters. `worker-claim` is for internal AI workers and atomically moves a run from one of the requested statuses to an in-progress status such as `planning`, `routing`, `knowledge_researching`, `splitting`, `risk_reviewing`, or `answer_synthesizing`.

Worker claim is registry-guarded by the API. The `worker` name must exist in the server-side worker map, requested `statuses` must be allowed for that worker, `claim_status` must match the worker's in-progress state, and any configured `task_type` restriction is enforced. For example, the knowledge research worker can claim only `task_type=knowledge_research`; if it forgets that filter, the API applies it. This prevents a worker bug from claiming a parent investigation case or another worker's task. Add new workers to the worker map before deploying them.

The worker map is the ownership table for autonomous processing. Define a worker's `task_type`, input status, and in-progress status there first, then deploy the worker. A worker instance may send `worker@hostname`; the API validates the base worker name and records the full instance name for lease visibility.

Execution claim is separate: `POST /api/runs/claim` also accepts `executor_mode` and `capability`, so real-machine agents can claim only tasks matching their allowed execution mode and skill.

## Document Payload

```json
{
  "ticket_id": 8,
  "kind": "investigation",
  "title": "QOSMaxJobs investigation",
  "summary": "Checked scheduler limits and prepared a workaround.",
  "body_md": "# Investigation\n\n...",
  "tags": ["scheduler", "qos"],
  "source": "zendesk-support-ai",
  "environment": "production",
  "machine": "target-host-01"
}
```

Documents are stored in `/data/db.sqlite`. Markdown bodies, document summaries, runbook text, run issue notes, run summaries, and handoff notes are encrypted inside SQLite using `KNOWLEDGE_FIELD_KEY_FILE`.

`ticket_id` is optional. Use it when a record comes from a Zendesk ticket. Use `environment` and `machine` to mark where the finding applies, even for records that are not tied to Zendesk.

Plaintext body search is intentionally disabled. `/api/search` searches only non-body metadata such as title, kind, source, environment, machine, and tags.

Timestamps are stored as Unix epoch seconds in SQLite. The web UI renders them with the service container's local timezone. Set `OPS_SUPPORT_TOOLS_TZ` when starting compose, for example `OPS_SUPPORT_TOOLS_TZ=Asia/Tokyo` on a Japan-based server or `OPS_SUPPORT_TOOLS_TZ=America/New_York` on a US East server. The same value is passed to the support AI workers so logs and web UI timestamps stay aligned. If unset, compose defaults to `UTC`.

Production Docker uses:

```text
apps/knowledge-api/secrets/knowledge_field_key
apps/knowledge-api/secrets/knowledge_api_write_token
```

Losing this key makes encrypted fields unrecoverable.

`knowledge_api_write_token` protects the narrow runbook handoff write endpoints. Configure it through `KNOWLEDGE_API_WRITE_TOKEN_FILE`; clients send it as `Authorization: Bearer ...`. Do not expose this token in documents, Zendesk comments, logs, or screenshots.

When the service runs as a non-root container user, the token file must be readable by that user through the mounted secret. The production compose setup runs `knowledge-api` with group `1000`, so keep the token file at `0640` or another equivalent group-readable mode. Check only file metadata with `stat`; do not print the token contents.

## Web Browse

The same service also provides a small browser UI for human review:

```text
http://127.0.0.1:18180/
```

The UI decrypts fields through the application process. Direct SQLite inspection still shows encrypted ciphertext for bodies, summaries, runbooks, and handoff notes.

## Move / Restore

For server moves, keep the DB and key together:

```text
apps/knowledge-api/data/
apps/knowledge-api/secrets/knowledge_field_key
```

Copying only the SQLite DB is not enough. Without `knowledge_field_key`, encrypted fields cannot be recovered.

## Document Handoff

Use document handoffs when a document does not belong to a run, or when the next worker only needs a queued document to review, summarize, translate, attach to Zendesk, or pass to an operator.

Create a handoff:

```json
{
  "ticket_id": 8,
  "kind": "answer-draft",
  "title": "Draft reply for scheduler question",
  "summary": "Draft reply prepared from previous findings.",
  "body_md": "# Draft Reply\n\n...",
  "tags": ["draft", "scheduler"],
  "source": "zendesk-support-ai",
  "environment": "production",
  "machine": "target-host-01",
  "handoff": {
    "channel": "operator-review",
    "recipient": "support-agent",
    "status": "requested",
    "note": "Please check the wording before posting."
  }
}
```

Fetch pending handoffs:

```text
GET /api/document-handoffs?status=requested&channel=operator-review
GET /api/document-handoffs?environment=production&machine=target-host-01
```

Update a handoff after processing:

```json
{
  "status": "done",
  "note": "Reviewed and ready to post."
}
```

Useful channels are `operator-review`, `real-machine-agent`, `zendesk-draft`, `knowledge-curation`, and `handoff-note`.

## Knowledge Research

`knowledge-research-worker` handles `knowledge-research-request` documents attached to investigation runs. It searches this encrypted Knowledge API first through `/api/search`, fetches candidate documents through `/api/documents/{id}`, and writes a `knowledge-research-result` document back to the same run. Optional RAG and web-search services can be configured on the support AI side; if they are not configured, the result records that explicitly.

The worker does not create a plaintext index of encrypted document bodies. Body text is decrypted only through the API process for the selected candidate documents, then summarized into the encrypted result document.

## Run Handoff

Triage AI can create an investigation case, and later AI agents or human operators can process child investigation tasks. Real-machine tasks expose a runbook; DB, RAG, web, and policy work use typed request/result documents instead.

Create a run:

```json
{
  "ticket_id": 8,
  "status": "requested",
  "runbook": "# Runbook\n\n1. Check the user code.\n2. Reproduce on the target system.\n3. Record findings and an answer draft.",
  "summary": "Investigate a scheduler limit question.",
  "environment": "production",
  "machine": "target-host-01"
}
```

Attach an output document to the run:

```json
{
  "role": "findings",
  "ticket_id": 8,
  "kind": "run-findings",
  "title": "Scheduler limit findings",
  "summary": "The job limit is enforced by the selected QOS.",
  "body_md": "# Findings\n\n## Issue On Run\n\n...\n\n## Summary\n\n...\n\n## Answer Draft\n\n...",
  "tags": ["scheduler", "runbook"],
  "source": "real-machine-agent",
  "environment": "production",
  "machine": "target-host-01"
}
```

Documents attached to a run inherit the run's `environment` and `machine` when those fields are omitted from the document payload.

Suggested document roles are `findings`, `issue_on_run`, `summary`, `answer_draft`, and `operator_note`.

Investigation routing uses the parent `investigation_case` run with `status=routing_requested` as the request. Additional documents are context, not a required routing-request wrapper:

- `investigation-router-plan`: the router's split into DB search, real-machine investigation, and policy decision requests.
- `answer-question-evaluation`: answer coverage gaps that may send a case back to `routing_requested`.
- `case-decision`: why new context was attached to an existing case or why a new case/task was opened.
- `knowledge-research-request`: existing Knowledge/DB, past findings, known issues, operation docs, and policy records to check first.
- `real-machine-investigation-request`: evidence that requires the target environment; may be read-only, compile/write, job execution, user-data access, or privileged depending on capabilities.
- `real-machine-investigation-source`: child-run execution contract with required capabilities, executor mode, risk level, approval requirement, freshness requirement, and staleness risks.
- `policy-decision-request`: human operation/support policy decisions that DB and machines cannot decide.

Knowledge research is DB-first but not DB-blind. A useful record should preserve timestamp, environment, machine or node type, driver/runtime/module versions, commands, inputs, work directory, job conditions, observed facts, inferred conclusions, unverified points, and staleness risks. Records without that context may still be useful hints, but they should not be treated as timeless truth.

Register execution results as a bundle:

```json
{
  "source": "real-machine-agent",
  "findings": "Confirmed facts, commands inspected, and evidence.",
  "issue_on_run": "Remaining blockers or problems during execution.",
  "summary": "Short handoff summary for the next reader.",
  "answer_draft": "Draft text that may be returned to Zendesk after review.",
  "answer_draft_policy": "hold",
  "runbook_document_id": "runbook-plan-document-id",
  "runbook_title": "Runbook title shown to operators",
  "claim_token": "token printed by claim_run.py when status=executing",
  "next_status": "result_registered",
  "create_zendesk_handoff": false
}
```

`POST /api/runs/{id}/execution-result` creates separate encrypted documents with roles/kinds `findings`, `issue_on_run`, `summary`, and `answer_draft` for the non-empty fields. It also updates the run-level encrypted `summary` and `issue_on_run` fields when those values are supplied. Include `runbook_document_id` and `runbook_title` when the results came from a specific `runbook-plan`; the web UI records these automatically from the latest plan.

Execution results may also include provenance and freshness metadata:

- `observed_at`, `node`, `os_version`, `driver_version`, `cuda_version`, `compiler_version`, `mpi_version`
- `modules`, `commands`, `workdir`, `job_conditions`
- `reproducibility`: `unknown`, `single_observation`, `reproduced`, `documented_policy`, or `historical`
- `reuse_scope`, `stale_after`, `staleness_triggers`

These fields are especially important for `findings`. A finding can be highly reusable without being universally reproducible: it may only apply to the recorded machine, module set, driver, CUDA, compiler, MPI library, node type, job condition, or operation date. If the recorded context is missing, old, or affected by a listed staleness trigger, downstream agents should route the question to DB-first investigation and then request fresh machine checks or human policy confirmation as needed.

Valid `answer_draft_policy` values are `hold`, `internal_note`, and `public_reply_draft`. Valid `next_status` values are `result_registered`, `answer_review`, `policy_review`, `human_review`, `review_passed`, `task_done`, `closed`, `execution_failed`, and `no_change`. Use `result_registered` after normal runbook execution so answer synthesis can pick up the result. Use `task_done` for child tasks whose documents are complete and can be consumed by the parent case without human answer review. If `create_zendesk_handoff=true` and `answer_draft` is present, the API creates a `zendesk-draft` handoff for later review. It does not post to Zendesk.

The run detail web page has the same registration form under **Register Execution Result**. Use it when a human operator has run the checks manually and wants to return findings, issues, a summary, and an answer draft to Knowledge without calling the API directly. The **Execution Results** panel shows which runbook produced each result document.

## Runbook Execution Procedure

Use this procedure when a human operator or real-machine AI receives a reviewed real-machine runbook task.

1. Claim the target task run before starting work. This prevents multiple operators or agents from executing the same real-machine runbook at the same time.
2. Open the target run detail page and confirm `environment`, `machine`, status, latest runbook plan, chief review, and stop conditions. The runbook plan shown under **Runbook Under Review** is the execution target unless an operator explicitly selects a different runbook document.
3. Keep the lease alive while working. If the lease expires, another operator or agent may reclaim the run.
4. Execute only the commands that are explicitly allowed by the runbook. Treat module changes, job submission, installation, file edits, service restarts, user data access, and destructive commands as out of scope unless the runbook and an operator approval both allow them.
5. Stop immediately if the target machine is ambiguous, a command would exceed the stated scope, credentials or secrets would be exposed, or the result contradicts the runbook assumptions.
6. Record evidence as short factual notes. Prefer command purpose and summarized output over large raw logs. Do not paste secrets, tokens, private user data, or unnecessary full command output.
7. Register results from the run detail page or `POST /api/runs/{id}/execution-result`. Make sure the result registration points to the runbook document that was actually executed, so later reviewers can see which plan produced each finding. Claimed `executing` runs require the matching `claim_token` when registering results.
8. Record provenance and freshness context for reusable findings: when, where, which module/version/job condition, how reproducible, and what future change would make the result stale. This prevents old-but-plausible evidence from being treated as current truth.

### Claim / Lease

Claim a `review_passed` run:

```bash
python3 apps/knowledge-api/claim_run.py claim \
  --api http://127.0.0.1:18180 \
  --claimant "$USER" \
  --lease-seconds 1800
```

Claim a specific run:

```bash
python3 apps/knowledge-api/claim_run.py claim \
  --api http://127.0.0.1:18180 \
  --claimant "$USER" \
  --run-id RUN_ID
```

Claim by target metadata:

```bash
python3 apps/knowledge-api/claim_run.py claim \
  --api http://127.0.0.1:18180 \
  --claimant cuda-mpi-agent \
  --machine RIKYU \
  --document-kind runbook-plan \
  --document-tag cuda \
  --lease-seconds 1800
```

Target filters are metadata-only: `ticket_id`, `environment`, `machine`, attached `document_kind`, `document_title_contains`, `document_source`, and `document_tag`. Encrypted document bodies are not searched. Use document tags such as `cuda`, `mpi`, `compiler`, `scheduler`, or machine-specific tags when creating real-machine `runbook-plan` documents so specialized agents can claim suitable work.

The command prints a `claim_token`. Keep it for heartbeat, release, and execution-result registration. Do not put it in documents or Zendesk comments.

Extend the lease:

```bash
python3 apps/knowledge-api/claim_run.py heartbeat \
  --api http://127.0.0.1:18180 \
  --run-id RUN_ID \
  --claim-token CLAIM_TOKEN \
  --lease-seconds 1800
```

Release without completing:

```bash
python3 apps/knowledge-api/claim_run.py release \
  --api http://127.0.0.1:18180 \
  --run-id RUN_ID \
  --claim-token CLAIM_TOKEN \
  --next-status review_passed
```

After registering execution results, use `result_registered`, `closed`, or `execution_failed` as appropriate. The execution-result API clears the claim when a claimed run is moved out of `executing`.

Direct API claim request:

```json
{
  "claimant": "operator-name",
  "status": "review_passed",
  "environment": "production",
  "machine": "RIKYU",
  "document_kind": "runbook-plan",
  "document_tag": "cuda",
  "lease_seconds": 1800
}
```

Execution result fields:

| Field | Purpose |
|---|---|
| `findings` | Confirmed facts, read-only checks performed, summarized evidence, and what was not checked |
| `issue_on_run` | Problems during execution, blocked steps, scope violations avoided, ambiguity, or `none` if no issue occurred |
| `summary` | Short handoff summary for the next support person or AI |
| `answer_draft` | Draft text for Zendesk or an internal note; keep `answer_draft_policy=hold` unless it is ready for review |

Recommended status after registration is `result_registered` when answer synthesis should review the findings and draft. Use `task_done` for a child task that is complete as evidence but does not itself need answer synthesis. Use `answer_review` only when a human should review an already synthesized answer, and `closed` only when the run is complete and no follow-up action remains. Creating a `zendesk-draft` handoff queues a draft for later review; it still does not post to Zendesk.

`answer_draft` registered by a real-machine gateway is an execution result draft, not necessarily the final reply candidate. The support-side `answer-synthesis-worker` can attach an `answer-quality-review` document and a newer `answer_draft` with role `answer_draft_synthesized`; operator review should prefer that synthesized draft when present, while still checking it against `findings` and `issue_on_run`.

The same worker can also attach `answer-question-evaluation`, which compares the latest answer draft with the original Zendesk question and highlights covered points, unanswered points, unsupported claims, overstatements, and the recommended operator action.

## Narrow Gateway Surface

Real-machine gateways only need these endpoints:

```text
POST /api/runs/claim
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/documents?include_body=1
POST /api/runs/{run_id}/claim/heartbeat
POST /api/runs/{run_id}/claim/release
POST /api/runs/{run_id}/execution-result
```

Require the write token for the four POST endpoints. Keep the management web UI under `/knowledge/` and broad APIs such as `/api/search`, `/api/documents`, `/api/document-handoffs`, `/api/runs` list, generic run document creation, and internal `POST /api/runs/worker-claim` out of the real-machine gateway allowlist unless there is a separate operational reason.
