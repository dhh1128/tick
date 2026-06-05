# GitHub release automation so the curl-install URL resolves
kind: todo
tags: release
created: 2026-06-05T20:10Z

- 2026-06-05T23:16Z Done. scripts/release.py (bump/tag/push) + .github/workflows/release.yml (on v* tag: verify tag==version, build dist/tick, gh release create --generate-notes). Build now goes to dist/tick (repo root clean). Repo made public; cut v1.0.0 -> workflow run 27045035371 succeeded -> release published with 'tick' asset. Verified: curl releases/latest/download/tick returns HTTP 200, downloads and runs. README/SPEC updated; ~/bin purged in favor of ~/.local/bin.
