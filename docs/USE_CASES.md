# Everyday use cases

FactLane is useful when a small fact should survive a session **without becoming hidden authority**.

It does not replace your assistant. It sits behind a compatible AI-agent host as a local governed memory layer. The technical setup is deliberate; everyday use can remain simple.

## Remember my preferences between sessions

You might tell an assistant:

> Keep answers concise, explain in Arabic, and preserve English technical terms.

That preference can be stored as a bounded fact instead of replaying the old conversation every time.

If you later say:

> Go deep on this one.

your current instruction wins. Memory helps; it does not overrule the active task.

## Remember important project facts

Useful durable facts include:

- a product goal;
- an architectural decision;
- an ownership boundary;
- a repository or project scope;
- a recurring workflow rule;
- a release or environment fact that is stored with an appropriate freshness policy.

Facts that can change need stronger freshness discipline than stable preferences. A remembered “current release” or “production target” is only useful while its freshness and source still support it.

## Remember recurring workflow rules

FactLane can carry durable workflow facts such as:

> Production changes require Owner approval.

or:

> Run local qualification before preparing a release review.

The fact does not grant the permission it describes. Runtime authority still comes from the trusted launcher and current instructions.

## Reuse governed facts across multiple agent hosts

More than one compatible local agent host can use the same governed FactLane store when the operator configures them against that environment.

This shares **bounded facts**, not full conversation transcripts. Hosts can have different launcher profiles, identities, and permissions even when they see the same public MCP tools.

## Let an agent propose memory without trusting it immediately

A normal delegated agent can be allowed to contribute a **Candidate** after explicit content consent:

> This looks worth remembering.

That is deliberately different from:

> This is verified Current truth.

Promotion requires a separate trusted verification operation with identity, revision, and lifecycle checks. The contributing agent cannot self-promote by asserting a privileged identity in the request.

## Retire facts that are no longer Current

A trusted Owner can govern lifecycle after a fact has been stored:

- `REVOKE` an eligible Current fact that should no longer count as Current;
- `REJECT` a Candidate that should not be admitted;
- resolve a contradiction group after reviewing competing records;
- reverify or replace when the underlying fact changes;
- manually recheck `on_change` freshness with new bounded evidence;
- readmit an eligible revoked logical memory only through a new governed verification path.

These are logical lifecycle operations. They are not a claim of physical erasure and they do not replace sensitive-memory Recovery.

## Keep memory inside the right boundary

FactLane does not use one giant memory bucket. A fact belongs to one exact public scope:

- `GLOBAL_USER`;
- `PROJECT`;
- `WORKFLOW`;
- `TOOL_ENVIRONMENT`;
- `CROSS_PROJECT_WORKFLOW`.

A project fact does not silently fan out into another project merely because the same assistant is connected. `CROSS_PROJECT_WORKFLOW` is an explicit identity-free scope for deliberately cross-project workflow facts, not a command to search every project.

## Use the Owner control plane when review matters

The local Owner CLI and loopback Owner WebUI make the governance layer visible instead of hiding it behind raw database access. They provide bounded views for Current, Candidates, conflicts, history, health, and retention plus the governed lifecycle operations above.

The Owner WebUI is a local operator surface, not an HTTP MCP server and not a sixth public tool.

## Next: check fit before setup

If these examples match your problem, use the [FAQ](FAQ.md), then [Core concepts](CORE_CONCEPTS.md). When you are ready to connect a host, follow the [Quick Start](QUICKSTART.md).
