# tick — Specification (draft for review)

**Status:** DRAFT — awaiting Daniel's approval before implementation.
**Owner / hosting:** `github.com/dhh1128/tick` — Daniel's **personal** account, **not** the `provenant-dev` org.
**Language:** Python 3.11+, **zero runtime dependencies**. Built **test-first (TDD)** with `pytest`.
**Distribution:** single-file **zipapp**, curl-installable to `~/.local/bin/tick` (see §9).

---

## 1. Purpose

`tick` is an extremely lightweight, all-local task-and-knowledge ledger for **one codebase**. It tracks
small units of work — "item X needs doing, here are accreting notes about why/how, now it's crossed
off" — and makes that knowledge **discoverable from the code itself** via greppable marks, instead of
externalizing it to a ticketing system.

It exists to satisfy six pressures that Jira / GitHub Issues handle badly:

1. **Knowledge lives with the code**, discoverable by `grep`, not behind an API.
2. **All-local**: reads are filesystem reads — no network, no API calls, no cost.
3. **Private-ish by default**: kept out of the project's public namespace so it doesn't clutter the
   shared pool of ideas (clutter-avoidance, not hard secrecy).
4. **Extremely lightweight**: no statuses, workflows, templates, assignees, or priorities.
5. **Travels with branches, worktrees, and machines** — but is **global** (one backlog), never forked
   per-branch.
6. **No PR overhead** to mutate: adding a note must not trigger the working branch's pre-push test
   hook or CI, and must collapse to a single command.

### Non-goals (YAGNI)

- No per-branch forking of the backlog (explicitly rejected — forking knowledge is an antipattern here).
- No cross-repo / product / portfolio view.
- No statuses/workflows/templates/assignees/priorities (optional `kind` + `tags` only).
- No MCP server, no web UI, no daemon. (An MCP server would re-introduce the API indirection pressure 1
  is escaping.)

### Relationship to existing tools (Canon vs. Ledger)

There are two tiers of project knowledge and `tick` is only the first:

- **Tier 1 — Ledger (`tick`, NEW):** high-churn working memory. Low ceremony, mutated constantly.
- **Tier 2 — Canon (unchanged):** `this.i` (intent layer) and `reviews/` (audit artifacts). These are
  *meant* to be versioned with the code and to go through normal PRs. **`tick` does not touch them.**

The bridge: when a tick turns out to be a real design decision, `tick off` is the natural moment to
**graduate** it into a `this.i` node. The ledger is the workshop; Canon is the showroom.

---

## 2. Vocabulary

- **a tick** — one ledger item (a to-do / debt / idea about the code).
- **tick mark** — the in-source reference `!<id>` (e.g. `!2k3m`) that pins a tick to a code location.
- **id** — a 4-character base32 identifier whose **first character is a digit** (see §5).
- **tick off** — to complete a tick.

---

## 3. Storage architecture

### 3.1 The orphan branch

The ledger for a target repo lives on an **orphan branch named `tick`** in that same repo (unrelated
history; holds only ledger files). Same-repo (not a separate repo) is deliberate: the multiple working
trees of one repo **share one object store**, so a commit to `tick` from one worktree is **instantly
visible** to the others with no push/pull. This is what makes concurrent multi-worktree editing cheap.

### 3.2 The persistent worktree + discovery

The `tick` branch is checked out **once** as a persistent worktree, located **inside the repo at
`<repo-root>/.tick/`** (decision: in-repo, not a sibling — a sibling under `~/code` would risk
`gitbulk` / `origin-*` convention false-positives, and nesting matches the existing
`.claude/worktrees/<name>` pattern). `.tick/` is ignored on the code branches via the tracked
`.gitignore` (see §3.3).

Discovery is **config-based, not cwd-based**, so the tool works from any worktree:

- `tick init` records the store path in shared git config **relative to the repo root**:
  `git config tick.worktree .tick`. (An out-of-repo `--store <path>` is recorded absolute, since it has
  no relative anchor; such a store is not relocatable.)
- Every command resolves the store by joining `git rev-parse --git-common-dir` (whose parent is the
  primary checkout) with `tick.worktree`. Because git recomputes `--git-common-dir` from the live
  location, the join always lands on the current path — so **moving or renaming the repo does not strand
  the ledger** (no config reset needed). A legacy *absolute* `tick.worktree` is honored if it still
  exists, and otherwise recovered by basename under the moved root and rewritten relative on resolve.
- The linked `.tick/` worktree also carries git's own absolute admin pointers, which a move breaks
  independently of config. On every resolve, tick checks the pointer and, if broken, self-heals it with
  `git worktree repair` — so a move needs **no** manual `git worktree repair` either. (A valid pointer
  costs one small file read; repair only fires when actually broken.)

The real `.tick/` worktree lives inside the **primary** checkout; additional worktrees reach it through
a symlink (§3.3). Caveat: the primary checkout is therefore load-bearing for those symlinks — fine for a
stable primary clone.

### 3.3 Ignoring, symlinks, and grep

- **One tracked `.gitignore` entry, `/.tick`, committed once** at `tick init`. This is the *only*
  change ever made to a code branch, and it matches both the real `.tick/` dir in the primary checkout
  and the `.tick` symlink in any other worktree (`.gitignore` is tracked, so it's present on every
  branch). We use the tracked `.gitignore` (not `.git/info/exclude`) precisely because one entry then
  covers all worktrees uniformly.
- **Additional worktrees:** `tick link` drops a gitignored `.tick` symlink → the primary's `.tick/`,
  for humans and `tick grep`. The tool itself never needs the symlink (it resolves the store from
  config).
- **Grep reality (corrected from an earlier draft):** because `.tick/` is gitignored, `rg`/`grep`
  **skip it by default**. That's fine — the *marks* are what you grep for, and they live in source
  regardless of where the store sits:
  - **Marks** → `rg '![2-7][a-z2-7]{3}'` over the code, always works.
  - **Tick bodies** → `tick grep <text>` (the tool greps the store wherever it is); raw
    `rg --no-ignore .tick/` also works but isn't the path.
  So store location is essentially grep-neutral; it's chosen for invisibility, not greppability.

### 3.4 Backup / multi-machine (push target)

The `tick` branch is pushed to the **same remote as the code** (decision: simplest, zero extra setup),
appearing as an ignorable branch named `tick`. `tick init` records `tick.remote` (default the existing
`origin`) and `tick.branch` (`tick`). Backup is **automatic**: after each mutation tick fires a
best-effort background `git push` of the ledger branch (`tick.autopush`, default on — set off with
`git config tick.autopush false`). It never blocks or fails the write; offline/rejected pushes just defer
to the next mutation. `tick sync` is the explicit full reconcile — `git pull --rebase` then
`git push <remote> tick` — used to **pull** another machine's changes and to flush any deferred backlog
(surfaced as an "N not yet backed up" hint on `tick ls`).

To keep `sync` from paying the code repo's test tax, `tick init` (with confirmation) installs a guard at
the **top of the repo's `pre-push` hook**: *if every ref being pushed is `refs/heads/tick`, `exit 0`.*
It also documents adding `branches-ignore: [tick]` to the repo's CI. (Both are one-time; per-write
mutations never push — see §4.)

### 3.5 Friction budget

- A mutation is a commit to the **`tick` branch**, never the code branch — so the code branch's
  pre-push hook and CI never fire on it.
- The **only** code-branch commit `tick` ever makes is the one-time `/.tick` `.gitignore` line at init.
- Per-write = an instant local commit (sub-second, offline). Backup push is **automatic but off the
  write path** — a detached best-effort `git push` fires after the commit, so the write returns
  instantly and never waits on (or fails because of) the network. `tick sync` remains for explicit
  pull+flush. Both are made fast by the §3.4 pre-push guard.

---

## 4. Concurrency model

Requirement: three worktrees of one repo, each able to mutate the ledger **safely and concurrently**
without clobbering each other.

1. **One file per tick** (`<id>.md`). Different ticks never touch the same bytes — this removes almost
   all contention by construction. (A single `TASKS.md` is rejected for this reason.)
2. **A commit lock is the safety guarantee.** Every mutating command runs its read-modify-write-commit
   critical section behind `flock` on a dedicated, never-committed lock file in the store
   (`<store>/.tick.lock`, listed in the `tick` branch's own ignore). This serializes local writers, so
   **both quick appends (`tick note`) and full rewrites (`tick edit`) are safe** — there is no
   append-only restriction; you can correct or rewrite any prior note.
3. **Push is off the write path.** Writes commit locally (instant, offline-capable); a best-effort
   background push then backs the commit up without blocking the write (`tick.autopush`). `tick sync`
   does `git pull --rebase` then push; one-file-per-tick keeps rebases conflict-free. Concurrent
   background pushes only ever send supersets of the same history, so the worst case is a benign
   ref-lock loss that the next push or `sync` resolves — never a lost commit (the local commit is
   already durable).
4. **Interactive edits:** `tick edit` opens `$EDITOR`; the lock is held only briefly at write-back +
   commit, not for the whole editor session. Same-tick concurrent edits on one machine are
   last-writer-wins (vanishingly rare for solo use); cross-machine divergence is resolved by `sync`'s
   rebase.

Local cross-worktree visibility needs **no** push/pull (shared object store + one shared store
directory). Push/pull is only for **other machines** and backup.

---

## 5. IDs and tick marks

### 5.1 Id format

- Exactly **4 characters** from base32 lowercase `[a-z2-7]` (RFC-4648 alphabet; matches `this.i`'s id
  charset), with the **first character constrained to a digit `[2-7]`**.
- Space: 6 × 32³ = **196,608** ids — vastly more than the expected scale (dozens live, hundreds over a
  lifetime).
- Generation: random, first char from `[2-7]`, remaining three from `[a-z2-7]`; reject and retry on
  collision with any existing id (open or closed).

### 5.2 The mark and why the first char is a digit

The in-source mark is `!` + id, e.g. `!2k3m`. The **digit-first** rule is what makes the mark uniquely
greppable:

- Naive `![a-z2-7]{4}` collides catastrophically with boolean negation of 4-letter identifiers —
  `!data`, `!user`, `!name`, `!flag`, `!resp` are everywhere in C-family code.
- But an identifier (and a numeric literal) can **never start with a digit**, so `!2k3m` cannot be the
  negation of anything — `!2k3m` simply does not parse as code in any mainstream language.

Canonical grep: `rg '![2-7][a-z2-7]{3}'` — effectively false-positive-free.

### 5.3 Marks are type-agnostic

The mark carries only the id. Whether a tick is a todo, debt, or idea is a field **inside** the file —
one sigil to remember. The `!` lives only in source and in conversation; it is **never** part of a
filename (a leading `!` in a filename is a shell/history-expansion hazard). The file is `<id>.md`.

---

## 6. Tick file format

Dead-minimal Markdown. First line is the title; a few optional header fields; then free-form,
**fully editable** notes that accrete over time.

```markdown
# <title>
kind: todo                 # optional: todo | debt | idea   (default: todo)
tags: parser, perf         # optional, comma-separated
created: 2026-06-05T14:30Z
closed: 2026-06-09T09:05Z   # present iff the tick is done

<free-form body — context, why/how; edit freely>

- 2026-06-06T08:12Z a dated note appended by `tick note`
- 2026-06-07T17:44Z another note (any prior line may be corrected via `tick edit`)
```

- **Timestamps are ISO-8601, UTC, minute precision** (`YYYY-MM-DDThh:mmZ`) for `created`, `closed`, and
  note lines.
- **Open vs. closed** is determined solely by the presence of the `closed:` field. No file moves; the
  file stays at `<store>/<id>.md` for stable resolution from a mark. (`tick ls` filters on `closed:`.)
- `tick note` appends `- <ts> <text>`. `tick edit` opens the whole file for correction/rewrite.

---

## 7. CLI surface

All commands resolve the store from git config (§3.2) and take the commit lock (§4) for any mutation.
Timestamps come from an **injectable UTC clock** (see §9) so tests are deterministic.

| Command | Behavior |
| --- | --- |
| `tick init [--remote <name>] [--store <path>]` | Create the orphan `tick` branch + `.tick/` worktree, record `tick.worktree`/`tick.remote`/`tick.branch`, add the `/.tick` `.gitignore` line (one commit), create the `.tick` symlink, and (with confirmation) install the pre-push guard. Idempotent. |
| `tick add "<title>" [--kind K] [--tag T]...` | Mint an id, write `<id>.md`, commit. **Prints the mark `!<id>`** to paste into code. |
| `tick note <id> "<text>"` | Append a dated note, commit. |
| `tick edit <id>` | Open the tick in `$EDITOR`; on save, commit. For correcting/rewriting anything. |
| `tick off <id>` | Set `closed: <now>`, commit. Then `grep` the code worktree for `!<id>` and **warn**, listing any sites where the mark is still embedded. |
| `tick reopen <id>` | Remove `closed:`, commit. |
| `tick ls [--all] [--closed] [--kind K] [--tag T]` | List ticks (open by default): `!<id>  <kind>  <title>`. |
| `tick show <id>` | Print the tick file. |
| `tick grep <text>` | Search tick bodies/titles in the store; print matching `!<id> <title>` + lines. |
| `tick refs <id>` | `grep` the code worktree for `!<id>`; list `file:line` sites. |
| `tick orphans` | Lint: marks in code with no tick file; **open** ticks with no mark in code. |
| `tick sync` | `git pull --rebase` then `git push` the `tick` branch to its remote. |
| `tick link` | Add the gitignored `.tick` symlink to the current worktree (for extra worktrees). |

Reads (`ls`, `show`, `grep`, `refs`, `orphans`) make **no** network calls.

---

## 8. Agent integration

- **Reads are plain files.** Agents use their native Read/Grep — no MCP, no network (pressures 1 & 2).
- **Target repos get an `AGENTS.md` / `CLAUDE.md` stanza**, roughly:
  > This repo uses `tick` for task tracking. Before editing a file, `rg '![2-7][a-z2-7]{3}' <file>` for
  > tick marks and read each referenced tick with `tick show <id>`. To search existing ticks, use
  > `tick grep <text>`. When your change resolves a tick, `tick off <id>` and delete the mark. To
  > capture new work, `tick add "<title>"`.
- **A thin Claude skill** (`tick`) wraps the CLI so any session can drive add/note/edit/off/ls. It is a
  front-door to the same Python engine — no logic duplicated.

---

## 9. Implementation design (for testability)

A clean seam between **pure logic** and **side effects** is what makes TDD natural:

- **`tick.core` (pure, no I/O):** id generation (injectable RNG), the mark regex + match/extract, tick
  file parse/serialize (round-trip), `ls`/`grep` filtering, orphan-set computation (given marks-found +
  ids-present), open/closed transitions, ISO-8601-UTC-minute timestamp formatting (injectable clock).
  **100% unit-testable with no git, no filesystem, no network.**
- **`tick.store` (I/O adapter):** filesystem read/write of `<id>.md`, the flock critical section, and
  the git operations (`init`, commit, pull/push), each behind small functions. Integration-tested
  against a **temporary real git repo** (`pytest tmp_path` + `subprocess` git).
- **`tick.cli` (argparse):** thin wiring of core + store. End-to-end tested via the temp repo.
- **Clock & RNG are injected** (parameters / small protocols), defaulting to a UTC
  `datetime.now(timezone.utc)` and `secrets`/`random` in production, pinned in tests.

**Packaging / distribution:** a **single-file zipapp** (`python build.py` over the `tick` package via
`zipapp`; shebang `/usr/bin/env python3`), built into `dist/tick` (gitignored — never at the repo root).
Because runtime deps are zero, the zipapp is just our own modules. `scripts/release.py` bumps the
version, tags `vX.Y.Z`, and pushes; the `release` GitHub Actions workflow builds the zipapp and attaches
it to a GitHub release. Installed with one line (repo is public so the asset URL needs no auth):

```
curl -fsSL https://github.com/dhh1128/tick/releases/latest/download/tick -o ~/.local/bin/tick && chmod +x ~/.local/bin/tick
```

(`~/.local/bin` is the XDG-standard user bin directory; requires Python 3.11+ on the target.) Dev
workflow: run from source (`python -m tick`) + `pytest`; dev-only dependency is `pytest`.
`pyproject.toml` defines the package and a `tick` entry point for optional `pip`/`pipx` installs, but the
zipapp is the primary artifact.

---

## 10. Test plan (write these FIRST, red → green)

**Pure core (no git):**
1. id format: 4 chars, charset `[a-z2-7]`, **first char ∈ `[2-7]`**.
2. id generation retries on collision against a supplied set; never returns a dup.
3. mark regex **matches** `!2k3m`, `!7qax`; **rejects** `!data`, `!user`, `!name`, `!flag`, `!=`,
   `!a`, `!2k3` (too short); `!2k3mz` matches only the first 4 (`!2k3m`).
4. extract all marks from a blob of source text (multiple per line, in comments).
5. tick parse/serialize round-trips title + kind + tags + created + closed + body + notes.
6. timestamp formatting: injected clock → `YYYY-MM-DDThh:mmZ` (UTC, minute precision).
7. `tick note` append produces a dated bullet and preserves prior content; a full-body rewrite
   (edit semantics) replaces content as given.
8. open/closed: `off` adds `closed`; `reopen` removes it; `ls` filter selects correctly by
   open/closed/kind/tag; `grep` matches title/body/notes.
9. orphan computation: given {marks in code} and {ids present}, returns (marks-without-tick,
   open-ticks-without-mark) correctly.

**Store / integration (temp git repo):**
10. `init` creates the orphan `tick` branch + `.tick/` worktree, records config, adds the `/.tick`
    `.gitignore` line in exactly one code-branch commit, is idempotent.
11. `add` writes `<id>.md` and produces exactly one commit on `tick`; prints `!<id>`.
12. `note` / `edit` / `off` each produce one commit and the expected file change.
13. the flock critical section serializes two concurrent `add`s (no index corruption, both ticks land,
    distinct ids).
14. `refs` / `orphans` / `grep` find marks/text across the code worktree and store.

**CLI / e2e:** smoke each subcommand through `main()` against the temp repo.

---

## 11. Build order (milestones)

- **M1** — `tick.core` + its unit tests (no git). Red-first per §10.
- **M2** — `tick.store` (flock + git) + integration tests on a temp repo.
- **M3** — `tick.cli` wiring + e2e smoke tests; `pyproject.toml`; zipapp build + curl-install check.
- **M4** — `tick` Claude skill + `AGENTS.md` stanza template.
- **M5** — dogfood: run `tick init` on tick's own repo and track its remaining work as ticks.

---

## 12. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Orphaned marks (`!id` left after close) | `tick off` warns + lists sites; `tick orphans` lints both directions. |
| Line-number rot in tick→code refs | The id is the durable anchor; tick files say "search the mark," never cite line numbers. |
| Multi-machine drift | One-file-per-tick + `tick sync` (pull --rebase); conflicts are rare and per-file. |
| `.tick/` descended into by non-gitignore-respecting tools (`find`, some linters) | Markdown-only content; gitignore-respecting tools skip it; same exposure as your existing `.claude/worktrees/`. |
| Store not discoverable to a new clone | `git config tick.worktree` + the `AGENTS.md` stanza; `tick init` is idempotent and re-establishes the symlink. |
| `tick` branch noticed on the shared remote | Accepted (clutter-avoidance, not secrecy); it's a single ignorable branch. |
| `git worktree --orphan` needs git ≥ 2.42 | Detect version; fall back to the manual orphan-branch dance on older git. |

---

## 13. Decisions log (resolved during design)

- **Name:** `tick`; item = "a tick"; mark = "tick mark" `!<id>`; completion = `tick off`.
- **Global, not per-branch** (forking the backlog rejected).
- **Storage:** in-repo `.tick/` worktree on an orphan `tick` branch; ignored via one tracked
  `/.tick` `.gitignore` line; config-based discovery. (Sibling and `~/.local/share` alternatives
  rejected.)
- **Relocatable store** (resolves `5aqn`)**:** `tick.worktree` is stored *relative* to the repo root and resolved
  against `--git-common-dir`'s parent; git's linked-worktree pointer is self-healed with
  `git worktree repair` on resolve. A repo move/rename therefore needs no manual config reset or
  worktree repair. Legacy absolute config values are migrated to relative on first resolve after a move.
- **Concurrency:** safety from `flock` (not append-only); notes are fully editable.
- **Push target:** same remote as the code, branch `tick` (ignorable); not a separate private remote.
- **Automatic backup (`6pyc`):** mutations fire a best-effort, detached background `git push` of the
  ledger branch (`tick.autopush`, default on), so backup needs no manual `tick sync`. Kept off the write
  path to preserve the instant/offline write; `sync` stays as the explicit pull+flush.
- **Hosting:** `github.com/dhh1128/tick` (personal), not `provenant-dev`.
- **Distribution:** single-file zipapp built to `dist/tick`, published to GitHub Releases by
  `scripts/release.py` + the `release` workflow, curl-installable to `~/.local/bin/tick` (repo public);
  Python 3.11+, zero runtime deps.
- **Timestamps:** ISO-8601, UTC, minute precision.

---

## Appendix — build-process note (not part of the tool)

This spec currently lives in `~/code/pika/`. The intended home is `~/code/tick` on `dhh1128`. The rename
`pika`→`tick` orphans Claude's path-keyed project state (memory + transcript), so it must be done at a
**clean session boundary**: finish in this session, `mv` the dir, start a fresh session in `~/code/tick`,
and re-seed project memory there (the ownership decision + this spec's location). This note exists so the
hosting/rename decision survives outside Claude's memory system.
