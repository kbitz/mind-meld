### [review:files=src/mind_meld/skills/retro_fleet/aggregator.py|tests/test_retro_fleet_aggregator.py] Test gap: Pin order and deduplication of extrapolated model mappings
- **ID:** wi-c1ecff
- **Test gap:** No demonstrated defect. Filed under `test-gaps=record`.
- **Description:** Current fixtures check mapping membership, sanitization and the omission count. They leave ordering and post-sanitization deduplication implicit.
- **Hypothesis (untested):** Reverse the fixture order, use two aliases with the same sanitized label and target, then check the visible sequence and omission count.
- **Upstream severity:** INFORMATIONAL
- **Found in:** `src/mind_meld/skills/retro_fleet/aggregator.py` at `_format_extrapolated_model_ids`.
- **Evidence:** The coverage audit read the renderer and fixture at `022c277`. It found no failing behavior.
- **Context:** Track 169B review-and-prep, branch `mmm/f3d5248e`, 2026-11-27. Revisit when a track changes extrapolation disclosure or its tests.
- **Effort:** ?
