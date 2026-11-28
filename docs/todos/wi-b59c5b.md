### [review:files=src/mind_meld/cli.py] Test gap: Check rewritten event row deltas without fresh host growth
- **ID:** wi-b59c5b
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Exercise the rewritten-file branch when a verified earlier revision already contains a catch-up or commit row and the attended host comparison yields no growth.
- **Hypothesis (untested):** Use a rewritten day with one preserved row and one new queued row. Verify the receipt follows the new row and stays quiet for a rewrite with only old rows.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/cli.py, `_published_usage_changed`
- **Evidence:** The same-day integration scenario exercises previous-blob recovery. Its fresh host row can decide the receipt before the rewritten-row comparison.
- **Context:** Track 171A review at `d1d0dd4` (2026-11-27). Revisit when a track next changes the published event comparison.
- **Effort:** ? (user triages in /roadmap)
