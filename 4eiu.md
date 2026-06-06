# sync has no integration test against a real remote
kind: debt
tags: testing
created: 2026-06-05T20:10Z

- 2026-06-06T00:07Z Added test_sync_round_trips_through_a_real_remote (two clones sharing a bare remote: push, then pull --rebase reconciles a second machine's commit, then push; verifies both sides converge) + test_sync_without_remote_errors. Exercises the real ls-remote/pull --rebase/push path. No production change needed.
