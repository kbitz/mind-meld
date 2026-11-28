### [review:files=src/mind_meld/cli.py] Test gap: Check missing-day ordering in published usage baselines
- **ID:** wi-f86bd5
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Exercise both sides of the latest verified host and session snapshots when a retained published day is unreadable.
- **Hypothesis (untested):** Use a synthetic manifest with a missing day before and after the known winner. Verify which comparisons remain provable.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/cli.py, `_read_published_usage_baseline`
- **Evidence:** Existing tests cover unread prior row deltas and ordinary host/session selection. The coverage audit found no assertion for both missing-day positions.
- **Context:** Track 171A review at `d1d0dd4` (2026-11-27). Revisit when a track next changes the published event comparison.
- **Effort:** ? (user triages in /roadmap)
