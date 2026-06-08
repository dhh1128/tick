# AGENTS.md / CLAUDE.md stanza for repos that use tick

`tick init` now appends this stanza to the target repo's `AGENTS.md` automatically
(idempotently — re-running `init` won't duplicate it; an existing `AGENTS.md` is
preserved and the stanza appended). The canonical text lives in code as
`store._TICK_STANZA`. Use the copy below if you instead want to paste it into a
`CLAUDE.md`, or into an `AGENTS.md` that predates a `tick`-injected one.

Note: the example id below is shown **without** its `~` sigil on purpose. The stanza
lands in a tracked file that is itself scanned for marks, so a literal example mark
would be flagged as an orphan (and would mislead an agent's own `rg`).

---

## Task tracking: `tick`

This repo tracks tasks, tech debt, and ideas in a local [`tick`](https://github.com/dhh1128/tick)
ledger (an orphan `tick` branch; the `tick` CLI is the interface). Reads are plain
files — do **not** use an external API for task tracking.

- **A tick mark is the sigil `~` immediately followed by a digit-first 4-char
  base32 id** (the id part looks like `4mz3`, so the full mark is that id with a
  leading `~`). It pins a tick to a code location.
- **Before editing a file**, grep it for marks and read what they reference:
  `rg '~[2-7][a-z2-7]{3}\b' <file>` then `tick show <id>`. A mark means recorded
  context exists for that spot — read it first.
- **Search** existing ticks with `tick grep <text>`; **list** with `tick ls`.
- **Capture** new work with `tick add "<title>"` and place the printed mark
  (`~` + the new id) at the relevant code spot.
- When your change **resolves** a tick, run `tick off <id>` and **delete the
  mark(s)** it reports still in the code.

---
