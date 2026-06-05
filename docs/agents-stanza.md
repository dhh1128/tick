# AGENTS.md / CLAUDE.md stanza for repos that use tick

Paste the following into a target repo's `AGENTS.md` or `CLAUDE.md` so coding
agents use the local ledger instead of an external tracker.

---

## Task tracking: `tick`

This repo tracks tasks, tech debt, and ideas in a local [`tick`](https://github.com/dhh1128/tick)
ledger (an orphan `tick` branch; the `tick` CLI is the interface). Reads are plain
files — do **not** use an external API for task tracking.

- **A tick mark is `!` + a digit-first 4-char id**, e.g. `!4mz3`. It pins a tick
  to a code location.
- **Before editing a file**, grep it for marks and read what they reference:
  `rg '![2-7][a-z2-7]{3}' <file>` then `tick show <id>`. A mark means recorded
  context exists for that spot — read it first.
- **Search** existing ticks with `tick grep <text>`; **list** with `tick ls`.
- **Capture** new work with `tick add "<title>"` and place the printed `!<id>`
  mark at the relevant code spot.
- When your change **resolves** a tick, run `tick off <id>` and **delete the
  mark(s)** it reports still in the code.

---
