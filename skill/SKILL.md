---
name: tick
description: Track per-codebase tasks, tech debt, and ideas in the repo's local `tick` ledger (a `tick` CLI over an orphan git branch). Use when the user says "add a tick", "what's on the tick list / what's next", "tick off X", "note on this tick", "any open ticks here", or when you encounter a `!XXXX` tick mark in source. Reads are plain files — never call an external API for this.
---

# tick

`tick` is a lightweight, all-local task/knowledge ledger for the current repo.
State lives in an orphan `tick` branch (one Markdown file per tick); the `tick`
CLI is the front door. This skill is stateless — re-orient by running `tick ls`.

## The mark convention (important)

A **tick mark** is `!` + a 4-char id whose first character is a digit, e.g.
`!4mz3`. It pins a tick to a code location.

- **Before editing a file**, scan it for marks and read the referenced ticks:
  ```sh
  rg '![2-7][a-z2-7]{3}' <file>     # find marks
  tick show <id>                    # read the tick a mark points to
  ```
  A mark means there is known, recorded context about that spot — read it first.
- The id is the durable anchor. To find all code for a tick: `tick refs <id>`.

## Common actions

| User intent | Do |
| --- | --- |
| "what's next / open ticks" | `tick ls` (add `--kind`/`--tag` to filter) |
| "add a tick / track this" | `tick add "<title>" [--kind todo\|debt\|idea] [--tag T]` then tell the user the printed `!<id>` and offer to drop the mark at the relevant code spot |
| drop a mark into code | `tick mark <id> <file:line>` injects `!<id>` as a trailing comment there (no commit) |
| "note that …" on a tick | `tick note <id> "<text>"` |
| correct/expand a tick | `tick edit <id>` (opens `$EDITOR`) |
| "tick off / done" | `tick off <id>`, then **remove the `!<id>` mark** from the code it warns about |
| "search ticks for X" | `tick grep "<x>"` |
| audit | `tick orphans` (marks with no tick; open ticks with no mark) |

## Rules

- When you finish work that resolves a tick, `tick off <id>` **and delete its
  mark(s)** from the source (`tick off` lists where they are).
- When you add a tick that belongs to a code location, add the `!<id>` mark there.
- If `tick ls` errors with "not initialized", the repo hasn't been set up — tell
  the user to run `tick init` (don't run it yourself without asking; it makes a
  commit and a worktree).
- A tick that turns out to be a real design decision should graduate into the
  project's `this.i` / design docs when closed — mention this to the user.
- Never invent ids; only use ids returned by `tick add` or found in the ledger.
