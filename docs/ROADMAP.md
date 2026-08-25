# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups and Tracks are regenerated whenever the roadmap is refreshed.

Standing constraints — these can refuse a Track, not merely shape how one is written:

- **mm maintains a `retro-fleet` skill link only for hosts that do not discover `~/.claude/skills`.** Verified 2026-08-24 against Grok 1.0.5 with `grok inspect --json`. A proposal to add an agent row must first show the host does not already find the directory. This criterion killed Track 27A.
- **A card's premise is checked against HEAD at drain time, not carried forward from when it was filed.** Six Tracks have now run on falsified premises. If the premise is false, discharge or kill it — do not emit the task.
- **A command that only exists to undo an automatic action is refused until the automatic action is shown to be correct.** v0.12.44 killed `mm uninstall-skills` this way: a revoke command, a `[skills] revoked` denylist, and a third policy axis were all downstream of one defect — the installer recreated a link the user deleted. Fixing the installer made all three unnecessary. Before filing an inverse, check whether the forward action should have happened at all.
- **Release-bearing Tracks serialize.** `pyproject.toml` is deliberately absent from `docs/shared-infra.txt`; two Tracks claiming one version force-advance `latest` to an untagged commit. See that file for the full argument.

---

## In Progress

_Nothing in flight._

---

## Current Plan

_tombstone: 27_

_Empty. Group 28 shipped as v0.12.44 and closed the durable-skill-link work; nothing is queued._

### Execution Map

No unshipped Groups.

**Total: 0 phases . 0 groups . 0 tracks remaining.**

---

## Future

Deferred: docs/roadmap-future.md (57 items)

## Shipped

History: docs/roadmap-shipped.md
