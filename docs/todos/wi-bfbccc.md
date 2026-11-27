### [review:files=src/mind_meld/core.py|src/mind_meld/pullflow.py] Test gap: Keep elapsed time through changing Syncthing counts
- **ID:** wi-bfbccc
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** The timer and delivery polling tests run separately. They do not cover the elapsed display when pending counts change during a terminal pull.
- **Hypothesis (untested):** Drive changing counts through the delivery wait with a synthetic fleet and a test clock, then assert elapsed time continues across the displayed counts.
- **Upstream severity:** INFORMATIONAL
- **Found in:** `tests/test_pullflow.py::test_syncthing_delivery_wait_polls_until_clear_and_applies_arrived_file`
- **Evidence:** Coverage review traced the polling loop to `hold_phase_timer` and found passing tests for each part. No failure was reproduced.
- **Context:** Filed by /review-and-prep on [PR #395](https://github.com/kbitz/mind-meld/pull/395) at `b0ac8bac` (2026-11-27). Revisit when delivery polling or elapsed timing changes.
- **Effort:** ? (user triages in /roadmap)
