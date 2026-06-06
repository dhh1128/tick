# Code scan reads every file unbounded; add a size cap + .gitignore awareness
kind: debt
tags: perf
created: 2026-06-05T20:10Z

- 2026-06-06T00:05Z Fixed in store._iter_code_files: switched os.walk (unbounded, hardcoded skip set) to 'git ls-files --cached --others --exclude-standard' for native .gitignore awareness, plus a 1 MiB MAX_SCAN_BYTES cap and symlink skip. refs/orphans/all_marks_in_code all inherit the bounds. Test: test_code_scan_honors_gitignore_and_size_cap.
