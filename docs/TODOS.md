# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

## Unprocessed

- **ANSI-escape sanitization on peer-supplied paths** — `console.print(f"...{rel_path}")` at multiple sites (cli.py:917, 962, 1082, 1096; Track 5B will add more in `_print_pull_summary`) renders ANSI escape sequences without escaping when `rel_path` originates from a peer's manifest. A compromised peer could plant escape chars in a synced filename to confuse terminal output. Low likelihood (requires peer compromise), low impact (visual confusion, no code execution). Fix: `from rich.markup import escape` and wrap each `rel_path` print site. ~1 LOC per call site, ~6 sites. (XS) [plan-ceo-review]

