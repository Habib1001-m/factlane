# FactLane Workflow Template

> FactLane can operate inside different workflows. This is one practical model for managing repository changes, agents, and evidence; adapt it to the nature of the work, available tools, and security requirements.

## Example change flow

```text
fresh/synchronized repository
-> task branch
-> isolated/local work
-> bounded evidence
-> tests
-> push task branch
-> pull request
-> CI
-> review
-> merge
-> taskboard reconciliation
```

Use placeholders such as `$PROJECT_ROOT`, `$OWNER_HOME`, `$AGENT_HOME`, `<task-branch>`, and `<evidence-dir>` in portable procedures. Replace them with local values only in private execution notes.

## Portable project truth vs. local execution state

**Tracked portable project truth** is reusable, secret-free, path-neutral material that another FactLane user can understand and use: source, tests, sanitized operational guidance, and decisions that belong to the project.

**Ignored machine-local execution state** includes raw evidence, private archives, temporary output, machine-specific state, secrets or credentials, absolute private workstation paths, local databases, and sensitive private logs. Keep that material under a private local boundary such as `.factlane-local/` or another narrowly reviewed ignore rule.

An execution file is not automatically an ignored file. If its content is reusable, portable, sanitized, and useful to another user, track it instead of hiding it. If it is private or machine-specific, keep it local and ignored.

## Adaptation notes

This template is a starting point, not a required operating model. Users may change the branch naming, evidence format, review roles, CI gates, taskboard practice, and merge controls to fit their repository and risk model. Preserve the separation between portable project truth and private execution state, and document any local exceptions where future contributors will see them.
