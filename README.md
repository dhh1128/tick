# tick

An extremely lightweight, **all-local** task / knowledge ledger for a single
codebase. Track "item X needs doing, here are notes about why/how, now it's
crossed off" — and discover that knowledge **from the code itself** with `grep`,
not from a ticketing API.

`tick` is for the high-churn working memory of one repo. It deliberately does
*not* try to be Jira: no statuses, workflows, templates, assignees, priorities,
or cross-repo views. See [`SPEC.md`](SPEC.md) for the full design and rationale.

## Why

1. Knowledge lives **with the code**, greppable, not behind an API.
2. **All-local** — reads are filesystem reads; no network, no cost.
3. Kept out of the project's public namespace (clutter-avoidance).
4. **Tiny** surface.
5. Travels with branches, worktrees, and machines — one global backlog, never
   forked per-branch.
6. **No PR overhead** to add a note: mutations land on a separate `tick` branch,
   so the code branch's pre-push hook and CI never fire, and each verb is one
   sub-second local commit.

## Install

Single-file zipapp (zero runtime deps; needs Python 3.11+ on the box):

```sh
curl -fsSL https://github.com/dhh1128/tick/releases/latest/download/tick \
  -o ~/.local/bin/tick && chmod +x ~/.local/bin/tick
```

(`~/.local/bin` is the XDG-standard user bin directory; make sure it's on your
`PATH`.) Or from source:

```sh
git clone https://github.com/dhh1128/tick && cd tick
python3 build.py && install -m 0755 dist/tick ~/.local/bin/tick   # or: pipx install .
```

Once installed, **`tick update`** self-updates to the latest release (it verifies
a published sha256 before replacing the binary). `tick ls` also prints a one-line
nudge, at most once a day, when a newer version exists — silence it with
`TICK_NO_UPDATE_CHECK=1` or `tick --no-update-check`. The check is offline-safe
(any network failure is ignored) and never runs on the write path.

## Quickstart

```sh
cd your-repo
tick init                         # orphan `tick` branch + .tick/ worktree; no commit on your branch
#                                 # add --agents to also drop the tick stanza into AGENTS.md (opt-in)
tick add "Speed up the lexer" --kind debt --tag parser
#  -> ~4mz3  Speed up the lexer
#     paste the mark  ~4mz3  wherever this work lives in the code
```

Drop the mark in a comment at the relevant spot:

```python
def tokenize(src):  # ~4mz3 quadratic on long inputs
    ...
```

…or let tick inject it for you (comment style inferred from the extension):

```sh
tick mark 4mz3 src/lexer.py:12     # appends `# ~4mz3` to that line
```

Then:

```sh
tick ls                 # open ticks
tick note 4mz3 "it's the re-scan in the inner loop"
tick refs 4mz3          # every code site referencing this tick
tick off 4mz3           # close it (warns if the mark is still in the code)
```

### The mark

A **tick mark** is `~` + a 4-char id whose first char is a digit, e.g. `~4mz3`.
The digit-first rule means it can never collide with a unary operator applied to
an identifier (`~mask`, `!data`, …), so it's uniquely greppable:

```sh
rg '~[2-7][a-z2-7]{3}\b'   # find every tick mark in the code
```

The sigil is `~`, not `!`: a leading `!` triggers bash history expansion, so a
copy-pasted `tick show !4mz3` would die with `event not found` before `tick`
ever runs. `~4mz3` is safe to type unquoted.

The id is the durable join key — `tick` files never cite line numbers, so they
don't rot. Find a tick's code from its id (`tick refs <id>`); find a tick from a
mark (`tick show <id>`).

## Commands

| Command | What it does |
| --- | --- |
| `tick init [--remote NAME] [--agents] [--install-guard] [--force-host]` | Set up the ledger (orphan branch, `.tick/` worktree, config); ignores `.tick` via `.git/info/exclude` with **no commit on your branch**. Without `--remote` the ledger is **detached** — local to this clone, never pushed. `--agents` also adds the tick stanza to `AGENTS.md` (a guarded docs commit; opt-in). |
| `tick migrate-ignore` | Move a pre-1.2 committed `/.tick` `.gitignore` line to `.git/info/exclude`. |
| `tick add "<title>" [--kind todo\|debt\|idea] [--tag T]…` | Add a tick; prints the mark. |
| `tick mark <id> <file:line>` | Inject the mark as a trailing comment at `file:line` (no commit). |
| `tick note <id> "<text>"` | Append a dated note. |
| `tick edit <id>` | Open the tick in `$EDITOR` to correct/rewrite. |
| `tick off <id>` / `tick reopen <id>` | Close / reopen. |
| `tick ls [--all] [--closed] [--kind K] [--tag T]` | List (open by default). |
| `tick show <id>` | Print a tick. |
| `tick grep <text>` | Search tick titles/bodies. |
| `tick refs <id>` | Find the tick's mark sites in the code. |
| `tick orphans` | Lint: marks with no tick, open ticks with no mark. |
| `tick sync` | `pull --rebase` then push the `tick` branch (mutations already auto-push in the background; `sync` pulls others' changes and flushes any deferred backlog). |
| `tick link` | Add a `.tick` symlink in an additional worktree. |
| `tick update [--check]` | Self-update to the latest release (verifies a sha256 before replacing the binary); `--check` only reports. |
| `tick --version` | Print the installed version. |

## How it stores things

The ledger is an **orphan branch `tick`** (one Markdown file per tick, `<id>.md`),
checked out once as a persistent worktree at `<repo-root>/.tick/` and ignored on
every branch via a `/.tick` entry in the repo-local, untracked
`.git/info/exclude` — so `tick init` makes **no commit** on your code branch. The multiple
worktrees of a repo share one object store, so a tick added in one worktree is
instantly visible in the others; an exclusive `flock` makes concurrent writes
safe. See [`SPEC.md`](SPEC.md) §3–4.

## Backup is opt-in

A fresh ledger is **detached**: it lives in this clone and is never pushed
anywhere. `tick init` says so once, and nothing nags about it afterwards — a
private working ledger doesn't belong on a shared remote just because the repo
has an `origin`.

```sh
tick init                      # detached (records tick.remote = none)
tick init --remote origin      # opt in: push the ledger to origin
tick init --remote none        # opt back out, at any time
```

Once a remote is attached, each mutation fires a **best-effort background push**
of the `tick` branch to it (an ignorable branch alongside your code), so the
ledger backs itself up with no manual step — offline just defers to the next
mutation or `tick sync`. Turn the automatic part off with `git config
tick.autopush false`. `tick sync` is the explicit pull-and-flush (e.g. to pull
another machine's changes); on a detached ledger it declines and tells you how to
attach a remote. The one case where `tick init` attaches a remote without being
asked is when that remote already publishes a `tick` branch — a colleague's
ledger, which this clone joins rather than forking.

Because that push is fire-and-forget, it is still running when the command
returns, so `tick ls` stays quiet about a backlog younger than 30 seconds —
that's a push in flight, not a failure. Past the window it says how many commits
are unbacked, to which remote, and how old the oldest is; it never guesses *why*,
since the check never touches the network. A ledger that has **never** reached
its remote gets its own message. So does one from before 1.3.0 whose
`tick.remote` was never set: nobody decided there, so the hint names both exits —
attach a remote, or `git config tick.remote none` to declare it local and silence
the hint for good.

## Agents

Reads are plain files, so an AI agent uses its native read/grep — no MCP, no
network. Install the Claude skill so agents use the ledger:

```sh
mkdir -p ~/.claude/skills/tick && cp skill/SKILL.md ~/.claude/skills/tick/
```

To teach agents about the ledger in a specific repo, add the stanza to its
`AGENTS.md` / `CLAUDE.md`. This is **opt-in**: run `tick init --agents` (it
appends the stanza and commits that one docs change on your current branch), or
paste [`docs/agents-stanza.md`](docs/agents-stanza.md) in by hand. A plain
`tick init` never touches `AGENTS.md`.

## Development

```sh
python3 -m pytest -q     # tests (pure core + temp-repo integration + e2e)
python3 build.py         # build the zipapp -> dist/tick
```

Built test-first; `tick.core` is pure and fully unit-tested, `tick.store` is the
side-effecting layer integration-tested against a temporary git repo.

## Releasing

`scripts/release.py` cuts a release. It bumps the version in `pyproject.toml`,
runs the guardrails (clean tree, on `main`, in sync with `origin`, tests pass),
commits with a DCO sign-off, and pushes `main` plus an annotated `vX.Y.Z` tag.
The tag push triggers the [`release`](.github/workflows/release.yml) workflow,
which builds `dist/tick` and attaches it to a GitHub release — that's what makes
the `releases/latest/download/tick` install URL above resolve.

```sh
python3 scripts/release.py                    # patch bump, default message
python3 scripts/release.py --minor -m "..."   # minor / --major / --patch
python3 scripts/release.py --set 1.0.0 -m "..."  # explicit version (e.g. first release)
```

## License

MIT.
