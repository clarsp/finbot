# Finbot agent guidance

Read README.md, then only the docs/reference pages relevant to the task.
This is the existing Python/Streamlit application; preserve its architecture.

Routine AI analysis excludes data/, audit_logs/, logs/, .git/, .venv/,
__pycache__/, .pytest_cache/, certs/, *.db*, *.tar.gz, *.log and environment
credential files. Do not read, extract, index, summarize or recursively search
these contents unless the user explicitly requests a task requiring them.
Use scoped searches in application source, brokers/, scripts/, tests/ and
docs/reference/. Never print credentials. These are agent instructions, not
filesystem access controls. Archives are historical, not current guidance.

Active runtime: one Docker Compose container; /data/finbot.db is inside external
volume finbot_migrated_20261022. Do not launch another trading worker or open its
live SQLite files from macOS. Keep broker ownership and admin-reference roles.

Tests: .venv/bin/python -m unittest discover -s tests
Preserve existing uncommitted work. Read docs/reference/CONTEXT.md for a short
handoff when continuing prior work.
