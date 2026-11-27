### [review:files=src/mind_meld/skills/retro_fleet/aggregator.py|tests/test_retro_fleet_aggregator.py] Test gap: Keep zero-volume aliases out of extrapolation notes
- **ID:** wi-2c3fc3
- **Test gap:** No demonstrated defect. Filed under `test-gaps=record`.
- **Description:** Existing zero-volume checks cover unpriced models and cost availability. They do not exercise a zero-volume fallback alias alongside reported usage.
- **Hypothesis (untested):** Render a positive-volume row containing an unused, fallback-priced alias and assert that only the used aliases appear in the disclosure.
- **Upstream severity:** INFORMATIONAL
- **Found in:** `src/mind_meld/skills/retro_fleet/aggregator.py` at `_extrapolation_notes`.
- **Evidence:** The coverage audit read the volume guard and fixtures at `022c277`. It found no failing behavior.
- **Context:** Track 169B review-and-prep, branch `mmm/f3d5248e`, 2026-11-27. Revisit when a track changes model-volume filtering or disclosure.
- **Effort:** ?
