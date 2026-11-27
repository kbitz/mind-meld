### [review:files=src/mind_meld/cli.py|src/mind_meld/initflow.py] Test gap: Clear terminal progress for init join memory setup
- **ID:** wi-fbd327
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Current tests exercise join memory setup and its output without a terminal. They do not assert that the output clears the live frame first.
- **Hypothesis (untested):** Run the join memory setup fixture with terminal progress active and assert the pointer and setup messages remain intact before the join outcome.
- **Upstream severity:** INFORMATIONAL
- **Found in:** `tests/test_initflow.py::TestInitMemorySetup160A::test_join_pulls_codex_instructions_before_appending_and_refreshes_once`
- **Evidence:** Coverage review traced the pause around the setup callback and verified the existing join test. No failure was reproduced.
- **Context:** Filed by /review-and-prep on [PR #395](https://github.com/kbitz/mind-meld/pull/395) at `b0ac8bac` (2026-11-27). Revisit when the join callback or memory setup output changes.
- **Effort:** ? (user triages in /roadmap)
