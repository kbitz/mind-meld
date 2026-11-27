### [review:files=src/mind_meld/cli.py|src/mind_meld/pullflow.py] Test gap: Clear terminal progress for init join placement notice
- **ID:** wi-ca1a22
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** The existing init join test covers the placement notice in nonterminal output. It does not exercise the pause around the notice during a live spinner.
- **Hypothesis (untested):** Run the join fixture with terminal progress active and assert one intact placement notice appears between the cleared frame and the join outcome.
- **Upstream severity:** INFORMATIONAL
- **Found in:** `tests/test_projectid.py::test_a_join_adds_the_not_placed_line_once`
- **Evidence:** Coverage review traced the terminal pause to the placement print and verified the nonterminal assertion. No failure was reproduced.
- **Context:** Filed by /review-and-prep on [PR #395](https://github.com/kbitz/mind-meld/pull/395) at `b0ac8bac` (2026-11-27). Revisit when the join tail or placement notice changes.
- **Effort:** ? (user triages in /roadmap)
