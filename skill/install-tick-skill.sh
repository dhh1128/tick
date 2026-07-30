#!/usr/bin/env bash
#
# install-tick-skill.sh
#
# Installs the `tick` Claude Code skill from this clone of the tick repo by symlinking
# ~/.claude/skills/tick -> this folder. Claude Code discovers personal skills at
# ~/.claude/skills/<name>/SKILL.md; the link is named `tick` even though the folder is
# `skill/`, because the skill's name comes from the directory Claude Code sees.
#
# Symlinking (rather than the `cp` in README.md) means the installed skill tracks this
# clone, so a `git pull` here updates the skill with no reinstall.
#
# This only wires up the SKILL.md. The `tick` CLI itself is a separate install
# (see README.md) — the skill is useless without it, so a missing `tick` on PATH is
# reported as a warning below.
#
# Safe to re-run (idempotent). No sudo. Only touches ~/.claude/skills/ and, if a real
# directory is already in the way, a single <link>.bak backup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$SCRIPT_DIR/SKILL.md" ]; then
  echo "ERROR: expected SKILL.md at $SCRIPT_DIR — run this from the skill/ folder of your tick clone." >&2
  exit 1
fi

mkdir -p "$HOME/.claude/skills"
LINK="$HOME/.claude/skills/tick"

if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
  echo "WARNING: $LINK exists and is not a symlink; backing it up to $LINK.bak"
  mv -f "$LINK" "$LINK.bak"
elif [ -L "$LINK" ]; then
  OLD="$(readlink "$LINK")"
  [ "$OLD" != "$SCRIPT_DIR" ] && echo "Note: replacing existing symlink (was -> $OLD)"
fi

ln -sfn "$SCRIPT_DIR" "$LINK"

RESOLVED="$(readlink -f "$LINK")"
if [ "$RESOLVED" != "$SCRIPT_DIR" ]; then
  echo "ERROR: symlink verification failed: $LINK -> $RESOLVED (expected $SCRIPT_DIR)" >&2
  exit 1
fi
echo "Symlink OK: $LINK -> $RESOLVED"

if ! command -v tick >/dev/null 2>&1; then
  echo
  echo "WARNING: the 'tick' CLI is not on your PATH."
  echo "  The skill is installed, but it drives the CLI — install it per README.md."
fi

cat <<'EOF'

============================================================
  tick skill installed.

  Claude will now use the repo-local tick ledger when you say
  "add a tick", "what's next here", "tick off X", or when it
  hits a ~XXXX tick mark in source.

  Per-repo opt-in for agents: paste docs/agents-stanza.md into
  that repo's AGENTS.md / CLAUDE.md.
============================================================
EOF
