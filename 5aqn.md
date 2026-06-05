# tick.worktree stores an absolute path; moving/renaming the repo needs 'git worktree repair <new>/.tick' + resetting tick.worktree config. Store it relative to repo root or resolve dynamically.
kind: debt
tags: portability
created: 2026-06-05T21:28Z
closed: 2026-06-05T21:58Z

- 2026-06-05T21:58Z Fixed: tick.worktree is now stored relative to the repo root and resolved against the parent of --git-common-dir, so a repo move/rename no longer strands the path. resolve() also self-heals git's linked-worktree pointer via 'git worktree repair' when it detects a broken gitdir link, and migrates legacy absolute config values to relative on first resolve after a move. Covered by test_store_relocatable_after_repo_move and test_legacy_absolute_config_migrates_on_resolve; SPEC 3.2 + 13 updated.
