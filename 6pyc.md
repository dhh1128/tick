# Auto-push ledger after each mutation (tick.autopush) so backup needs no manual 'sync'
kind: todo
tags: sync, friction
created: 2026-06-05T22:50Z
closed: 2026-06-05T22:59Z

- 2026-06-05T22:59Z Implemented: tick.autopush (default on) fires a detached best-effort 'git push' of the tick branch after each mutation (add/note/off/reopen/edit), via store._autopush with start_new_session so it outlives the CLI. Never blocks/fails the write; offline defers. tick ls shows an 'N not yet backed up' hint (store.unpushed_count). Disable with 'git config tick.autopush false'. Covered by 4 new tests; SPEC 3.4/3.5/4 + README updated.
