# FactLane host bootstrap

Use this reference when an agent or operator is connecting FactLane to an MCP host and making the
portable `using-factlane` Skill available to that host. It is deliberately host-neutral: inspect
the installed host before changing machine or host configuration, and use only that host's
supported MCP and Skill mechanisms.

## Do not collapse bootstrap states

Report these states separately. One state does not imply the next.

| State | Evidence required |
| --- | --- |
| **runtime installed** | The intended `factlane` executable/environment is present and its identity can be inspected. |
| **MCP configured** | The host configuration points to the intended FactLane executable and arguments. |
| **Skill present** | `SKILL.md` and this `references/host-bootstrap.md` are present in the source tree or installed package data. |
| **Skill registered** | The host's supported Skill mechanism records/imports the Skill. |
| **Skill discoverable** | The host can enumerate or otherwise expose the registered Skill. |
| **Skill loaded** | The current agent/session has direct host evidence that the Skill instructions were loaded. |

Do not claim `registered`, `discoverable`, or `loaded` merely because the files exist on disk.
Installing the Python package does not automatically register the Skill with an MCP host.

## 1. Inspect before changing anything

Read-only inspection comes first:

1. identify the host and its installed version;
2. inspect how that host configures local command-based stdio MCP servers;
3. inspect how that host imports/registers portable Skills;
4. locate the intended FactLane executable and package/source identity;
5. check Python and linked SQLite requirements;
6. check local Ollama reachability and the intended embedding profile;
7. inspect existing FactLane/Skill configuration before proposing edits.

If the agent cannot inspect or change the required machine/host configuration, stop at guidance.
Do not claim installation or registration that was not observed.

Machine/package installation, host configuration, Skill registration, or a launcher-profile change
are environment mutations. Perform them only with the applicable operator/user authority. Consent
to remember a fact is not authorization for any of those mutations.

When inspection finds a missing prerequisite, report the smallest required closure and its effect
before changing anything. Install or modify only the prerequisite the operator authorizes; then
re-run the relevant inspection instead of assuming the closure worked.

## 2. Locate the portable Skill

For a source checkout, the Skill root is:

```text
skills/using-factlane/
├── SKILL.md
└── references/
    └── host-bootstrap.md
```

For a wheel installation, derive the interpreter's data prefix rather than guessing a host path:

```bash
python - <<'PY'
from pathlib import Path
import sysconfig

root = Path(sysconfig.get_path("data")) / "share" / "factlane" / "skills" / "using-factlane"
print(root)
print(root / "SKILL.md")
print(root / "references" / "host-bootstrap.md")
PY
```

Both files must be present before reporting `Skill present`.

## 3. Configure MCP read-only first

Use the intended FactLane executable with a stable non-secret `--host-id`, exact database path,
and explicit embedding profile. Omit `--write-profile` for the first connection.

Do not invent a host-specific configuration schema. Translate the same command/arguments into the
installed host's documented local stdio MCP configuration.

The safe first-run objective is:

1. the host launches FactLane over stdio;
2. the host discovers **exactly five** FactLane tools:
   `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status`;
3. a read-only `memory_status` request succeeds for an exact scope.

Tool visibility is not write permission. A read-only launcher may still advertise `memory_store`
and `memory_update`; governed runtime authorization must block mutation.

## 4. Register the Skill using the host's native mechanism

Use the Skill root containing `SKILL.md` and `references/`. Follow the inspected host's supported
import/register/install procedure. Do not introduce a FactLane host-adapter SDK, wrapper framework,
prompt hook, or private plugin merely to register this Skill.

Apply the recipe that matches the host capability you actually observed:

- **directory import/registration** — give the host the whole `using-factlane/` directory;
- **managed Skill directory** — copy/import the whole directory while preserving
  `references/host-bootstrap.md`; do not copy `SKILL.md` alone;
- **host install command/UI** — select the Skill root and verify where the host says it registered
  the Skill before reporting success.

Do not invent an undocumented destination directory or registration command. If the host only
supports inline prompt text rather than a native Skill mechanism, report native registration as
unsupported/unproven instead of creating a FactLane-specific adapter layer.

After registration, collect direct evidence separately for:

- the registered source/path or host record;
- host discovery/enumeration, when the host exposes it;
- loaded-state evidence for the current session, when the host exposes it.

If a host exposes no reliable loaded-state signal, report `loaded: unproven`; do not infer it from
successful MCP calls.

## 5. Enable Candidate contribution only as a separate authority change

Keep the launcher read-only unless Candidate writes are actually needed and the operator separately
authorizes changing the launcher to `delegated-candidate`.

There are two content-consent paths once Candidate writes are available:

1. **Explicit user request** — “remember/store this” authorizes the bounded content to be proposed
   as a Candidate, subject to the active runtime and privacy boundary.
2. **Agent proposal** — the agent may identify a potentially useful reusable fact, show the
   proposed bounded fact to the user, and ask for explicit authorization before `memory_store`.

Neither path grants verifier authority, changes the launcher profile, or promotes the Candidate to
Current. Promotion remains a separate trusted verifier/CAS operation.

Never treat silence, prior broad approval, or the fact that a tool is visible as write consent.
Never perform autonomous post-turn storage.
Respect the active privacy boundary as well: do not propose or store secrets or sensitive content
that the applicable policy does not permit, even when the fact might be reusable.

## 6. Produce a bootstrap evidence report

Record observed facts, not inferred success. A minimal report should contain:

```text
FactLane source/package identity: <observed>
FactLane executable: <observed absolute path>
Python / linked SQLite: <observed>
Embedding profile / Ollama endpoint: <observed>
MCP configured: yes|no|unproven
Skill present: yes|no
Skill registered: yes|no|unproven
Skill discoverable: yes|no|unproven
Skill loaded: yes|no|unproven
Tool set: <observed names>
Exactly five tools: yes|no
memory_status read-only smoke: pass|fail|not-run
Launcher write profile: read-only|delegated-candidate|trusted-verifier-profile|unproven
Machine/host mutations performed: <list or none>
Remaining blockers: <list or none>
```

Stop and report a blocker if the executable/package identity is ambiguous, the host exposes a tool
set other than exactly five, the read-only smoke fails, the Skill reference set is incomplete, or
required registration/loaded state cannot be proven for a claim you intend to make.
