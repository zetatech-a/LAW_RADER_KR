#!/bin/bash
# Prepare a Claude Code cloud session: install dependencies so tests run immediately.
# Runs from the SessionStart hook in .claude/settings.json. Does nothing outside the cloud.
if [ "$CLAUDE_CODE_REMOTE" != "true" ]; then
  exit 0
fi
cd "$CLAUDE_PROJECT_DIR" || exit 1

# pip skips requirements that are already satisfied, so resumes stay fast.
pip install -q -r requirements.txt pytest >/dev/null 2>&1 || echo "cloud_session_setup: dependency install failed; run 'pip install -q -r requirements.txt pytest' manually" >&2
exit 0
