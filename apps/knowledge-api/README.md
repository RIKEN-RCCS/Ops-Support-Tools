# Knowledge API

SQLite-backed encrypted storage for support AI findings, runbooks, answer drafts, and document handoffs.

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
POST /api/runs/claim
GET  /api/runs/{id}
PATCH /api/runs/{id}
POST /api/runs/{id}/documents
GET  /api/runs/{id}/documents
POST /api/runs/{id}/execution-result
POST /api/runs/{id}/claim/heartbeat
POST /api/runs/{id}/claim/release
```

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

## Run Handoff

Triage AI can create a run request, and a later AI agent or human operator can fetch the requested runbook and attach findings, issues, summaries, and answer drafts.

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
  "next_status": "operator_review",
  "create_zendesk_handoff": false
}
```

`POST /api/runs/{id}/execution-result` creates separate encrypted documents with roles/kinds `findings`, `issue_on_run`, `summary`, and `answer_draft` for the non-empty fields. It also updates the run-level encrypted `summary` and `issue_on_run` fields when those values are supplied. Include `runbook_document_id` and `runbook_title` when the results came from a specific `runbook-plan`; the web UI records these automatically from the latest plan.

Valid `answer_draft_policy` values are `hold`, `internal_note`, and `public_reply_draft`. Valid `next_status` values are `operator_review`, `review_passed`, `closed`, and `no_change`. If `create_zendesk_handoff=true` and `answer_draft` is present, the API creates a `zendesk-draft` handoff for later review. It does not post to Zendesk.

The run detail web page has the same registration form under **Register Execution Result**. Use it when a human operator has run the checks manually and wants to return findings, issues, a summary, and an answer draft to Knowledge without calling the API directly. The **Execution Results** panel shows which runbook produced each result document.

## Runbook Execution Procedure

Use this procedure when a human operator or real-machine AI receives a reviewed runbook.

1. Claim the target run before starting work. This prevents multiple operators or agents from executing the same runbook at the same time.
2. Open the target run detail page and confirm `environment`, `machine`, status, latest runbook plan, chief review, and stop conditions. The runbook plan shown under **Runbook Under Review** is the execution target unless an operator explicitly selects a different runbook document.
3. Keep the lease alive while working. If the lease expires, another operator or agent may reclaim the run.
4. Execute only the commands that are explicitly allowed by the runbook. Treat module changes, job submission, installation, file edits, service restarts, user data access, and destructive commands as out of scope unless the runbook and an operator approval both allow them.
5. Stop immediately if the target machine is ambiguous, a command would exceed the stated scope, credentials or secrets would be exposed, or the result contradicts the runbook assumptions.
6. Record evidence as short factual notes. Prefer command purpose and summarized output over large raw logs. Do not paste secrets, tokens, private user data, or unnecessary full command output.
7. Register results from the run detail page or `POST /api/runs/{id}/execution-result`. Make sure the result registration points to the runbook document that was actually executed, so later reviewers can see which plan produced each finding. Claimed `executing` runs require the matching `claim_token` when registering results.

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

Target filters are metadata-only: `ticket_id`, `environment`, `machine`, attached `document_kind`, `document_title_contains`, `document_source`, and `document_tag`. Encrypted document bodies are not searched. Use document tags such as `cuda`, `mpi`, `compiler`, `scheduler`, or machine-specific tags when creating runbook-plan documents so specialized agents can claim suitable work.

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

After registering execution results, use `operator_review`, `closed`, or `execution_failed` as appropriate. The execution-result API clears the claim when a claimed run is moved out of `executing`.

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

Recommended status after registration is `operator_review` when a human should review the findings or answer draft. Use `closed` only when the run is complete and no follow-up action remains. Creating a `zendesk-draft` handoff queues a draft for later review; it still does not post to Zendesk.

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

Require the write token for the four POST endpoints. Keep the management web UI under `/knowledge/` and broad APIs such as `/api/search`, `/api/documents`, `/api/document-handoffs`, `/api/runs` list, and generic run document creation out of the real-machine gateway allowlist unless there is a separate operational reason.
