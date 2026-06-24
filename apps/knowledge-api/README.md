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
GET  /api/runs/{id}
PATCH /api/runs/{id}
POST /api/runs/{id}/documents
GET  /api/runs/{id}/documents
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
```

Losing this key makes encrypted fields unrecoverable.

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
