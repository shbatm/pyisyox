#!/bin/bash
set -euo pipefail

cd /workspaces/pyisyox

# The Dockerfile already pip-installed runtime + dev requirements
# system-wide, so the workspace just needs the editable package +
# pre-commit hooks wired in.

pip3 install -e .

# Pre-commit hooks call script/run-in-env.sh, which prefers a project
# .venv. Create one that inherits the system site-packages (the
# Dockerfile already installed the runtime + dev deps there) so
# worktrees don't have to bootstrap a fresh venv before commit
# (matches the "Worktree gotcha" note in CLAUDE.md).
if [ ! -e .venv ]; then
  python3 -m venv --system-site-packages .venv
fi

pre-commit install --install-hooks

# Convenience: copy __main__ into example/ so users can poke at the
# CLI without rooting around in the package.
mkdir -p example
cp -f pyisyox/__main__.py example/example_connection.py
