# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

Single source of truth — there is no root-level `TODOS.md`. The two files were
reconciled on 2026-08-14; the root file's live inbox won and moved here, and the
`## Inbox` heading was renamed to `## Unprocessed` (what `/roadmap` drains).

## Item format (load-bearing — `/roadmap` cannot see items written any other way)

Every item is an **H3 heading** carrying a bracketed source tag, optionally
followed by attribute bullets and free-form prose:

```markdown
### [full-review:severity=critical] Short title, not a paragraph
- **Description:** reviewer's framing of the issue (full-review, review)
- **Symptom:** what was observed (pair-review, investigate)
- **Repro:** numbered steps to re-verify before fixing
- **Why:** problem statement (manual, ship — you authored it and stand behind it)
- **Hypothesis (untested):** a direction to investigate, not a fix to apply
- **Effort:** S | M | L
- **Priority:** P1 | P2 | P3
- **Context:** provenance, branch, prior decisions

Free-form prose, measurements, snippets — preserved verbatim, not parsed.
```

`<source>` is one of `pair-review`, `full-review`, `review`, `review-apparatus`,
`test-plan`, `investigate`, `ship`, `manual`, `discovered`, `plan-ceo-review`,
`plan-eng-review`. Keys are lowercase `[a-z-]+`; values may not contain `[`, `]`,
`,` or `;` (pipe-separate file lists: `files=a.py|b.py`). A missing tag routes as
`[manual]`. Full grammar: `gstack-extend/docs/source-tag-contract.md`.

**Why this is load-bearing.** `bin/roadmap-audit` counts items by matching `###`
headings inside `## Unprocessed`. A flat `- **Bold title.** …` bullet is
invisible to it — the section reports `ITEMS: 0` and `/roadmap` skips the drain
entirely. That is not hypothetical: **38 items filed as bullets between
2026-08-30 and 2026-09-01 were reported as `ITEMS: 0` for three days** and were
only drained because a human noticed the roadmap looked stale. If you append
here by hand, use the H3 form.


## Unprocessed

### [manual] Bound the non-interactive keyring read with a timeout backstop
- **Why:** Track 73A's lock-state check covers gnome-keyring's locked and missing default collection and locked items. It cannot see prompt paths that do not use the Secret Service `Unlock` API, such as KeePassXC's "confirm access" dialog or a stalled D-Bus activation. A hook would still wait on those.
- **Hypothesis (untested):** Run the auto commands' keyring read under a bounded wait (for example a daemon thread joined with a few-second timeout) and record `keyring-locked` with reason `timeout` when it expires. Backend-agnostic, but a thread enters the hook path, the dialog still opens on screen, and the abandoned thread holds a D-Bus connection until process exit.
- **Effort:** S
- **Priority:** P3
- **Context:** Supported desktops (docs/SPEC.md) are Keychain and an unlocked Secret Service collection on Arch-based Linux (Omarchy ships gnome-keyring). KeePassXC is outside the supported set. Start in `crypto.get_passphrase(non_interactive=True)`. Depends on Track 73A. Revisit trigger: an autopull or autopush stall observed on a supported desktop that the 73A lock-state check does not explain. Source: /autoplan Track 73A, CEO E1 (2026-10-16).

### [manual] Omarchy qualification checkpoint before Track 80A (for /roadmap)
- **Why:** A small native run on the owner's Omarchy machine (pipx install, actual keyring backend selected, login and reboot behavior, a hook invocation, manual recovery) before 80A builds the Linux passphrase flow.
- **Effort:** S
- **Priority:** P2
- **Context:** Source: /autoplan Track 73A, Codex CEO voice C1 (2026-10-16); not a 73A merge gate. Trigger: a Linux machine joins the owner's desk.

### [manual] Proposed owner acceptance for Track 73A at the 2.0 soak — PENDING owner confirmation
- **Why:** Live owner acceptance that Linux hooks skip (not hang) on a locked Secret Service collection and recover after unlock. Filed as **PROPOSED, pending owner confirmation** — ac1 was not answered at the /autoplan gate (2026-10-16), so this is NOT a required acceptance entry. A new acceptance entry counts only after the owner confirms it directly. Do not mark REQUIRED without that confirmation.
- **Effort:** S
- **Priority:** P2
- **Context:** On an install made by 2.0's `init --new-fleet` or `join`: lock the Secret Service collection, run `mm autopull`, record the stderr line, `mm status`, `mm diag --json` and timestamps; unlock, rerun, record success and the recovery time (target under 2 minutes); record which unlock path worked, whether the keyring has a password, and whether full-disk encryption is on. Draft script below (verify each command on the machine first; it deletes nothing).

```bash
#!/usr/bin/env bash
# Track 73A owner acceptance: Linux only, deletes nothing.
set -euo pipefail
[ "$(uname -s)" = Linux ] || { echo "refusing: Linux only" >&2; exit 1; }
command -v mm >/dev/null && command -v busctl >/dev/null
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
echo "start $(ts)"; mm diag --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["keyring"])'
echo "keyring has a password? (y/n)"; read -r KR_PW; echo "full-disk encryption on? (y/n)"; read -r FDE
busctl --user call org.freedesktop.secrets /org/freedesktop/secrets \
  org.freedesktop.Secret.Service Lock ao 1 /org/freedesktop/secrets/aliases/default
echo "locked $(ts)"; mm autopull || true; mm status | grep -i "auto-pull" || true
mm diag --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["keyring"])'
echo "Unlock the keyring now (desktop keyring manager, or: secret-tool lookup service mind-meld username passphrase >/dev/null). Press Enter when done."; read -r _
echo "unlocked $(ts)"; mm autopull; echo "recovered $(ts)"
mm diag --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["keyring"])'
echo "record: keyring_password=$KR_PW fde=$FDE"
```

### [manual] Owner for store-v2 section 11 prose outside pyproject.toml (for /roadmap)
- **Why:** store-v2 §11's remaining platform prose has no owning Track: README prerequisites (`README.md` "macOS with iCloud Drive"), init help, "other Mac" strings, the root SPEC overview, and AGENTS.md principles. For /roadmap to assign (likely 84A).
- **Effort:** S
- **Priority:** P3
- **Context:** Source: /autoplan Track 73A deferred items (2026-10-16). 73A updated only the CI line and keyring-related prose.

### [manual] Attended commands that agents run can block on a locked Linux keyring
- **Why:** `mm status` (which AGENTS.md tells agents to run) reads the passphrase interactively before rendering; on a locked Secret Service collection it raises a desktop prompt or fails at getpass on a non-TTY. Consider a non-interactive-first read when stdin is not a TTY.
- **Effort:** S
- **Priority:** P3
- **Context:** Trigger: an agent-run attended command observed blocking on the soak machine. Source: /autoplan Track 73A Eng M4 (2026-10-16).

### [manual] Track 80A owns the 2.0 default storage path ~/Sync/mind-meld (for /roadmap, UC1)
- **Why:** Gate answer UC1 accepted (2026-10-16): the `~/Sync/mind-meld` 2.0 default path moved out of Track 73A to Track 80A. First reader is `mm init --new-fleet`. 73A deliberately did not touch `config.DEFAULT_STORAGE_PATH`. For /roadmap to add to the 80A card.
- **Effort:** S
- **Priority:** P2

### [manual] Track 84A owns pyproject.toml description, keywords and classifiers (for /roadmap, UC2)
- **Why:** Gate answer UC2 accepted (2026-10-16): `pyproject.toml` description, keywords and classifiers moved out of Track 73A to Track 84A, which must list `pyproject.toml` in its touches. 73A only added the `secretstorage` dependency.
- **Effort:** S
- **Priority:** P3

### [manual] Audit Python 3.14 Path.exists / is_dir / is_file callers for swallowed errors
- **Why:** On Python 3.14, `Path.exists()`, `is_dir()` and `is_file()` call `os.path.*` and return False for any `OSError`; on 3.13 they raise `PermissionError`/`EIO`. Any strict path that relied on the raise now reads an unreadable directory as absent, and with truth-based manifests that could publish deletions. Linux CI runs 3.14 and pipx installs already resolve 3.14 on Macs that have it.
- **Hypothesis (untested):** The strict manifest walkers already use explicit `lstat`/`stat` mode checks (`walk_claude_source` strict branch), and `events.latest_event_rows` re-stats after `is_dir()`; no unsafe strict caller is known. An AST sweep of the ~100 `src/` call sites, classifying each as strict, preview or best-effort, would confirm it.
- **Effort:** S
- **Priority:** P2
- **Context:** Found by Track 73A's portable-linux job (PR #213): three tests that injected faults through `Path.stat` stopped firing on 3.14 and were moved to `Path.lstat` / `os.stat`. Revisit trigger: before Track 80A's Linux flow, or any strict-path change that calls `exists()`/`is_dir()`/`is_file()`.

### [manual] macOS hook with a locked Keychain and no UI reports no-passphrase, not keyring-locked
- **Why:** keyring's macOS backend maps only `-128` (user denied) to `KeyringLocked`. A locked login keychain in an SSH or launchd session returns `errSecInteractionNotAllowed` (`-25308`), which keyring raises as a plain `KeyringError`; the non-interactive read treats that as no value, so the hook records `no-passphrase` and the README's `mm diag` advice shows nothing Keychain-specific.
- **Hypothesis (untested):** Inspect the swallowed `KeyringError`'s `__cause__` for the macOS status and map `-25308` to `KeyringUnavailableError("locked", "keychain")` with an unlock-the-keychain remedy.
- **Effort:** S
- **Priority:** P3
- **Context:** Track 73A Red Team / adversarial review (PR #213). Not a regression: the old wording also said "keyring empty". Revisit trigger: an owner hook run over SSH or from a launchd agent on a fleet Mac records `no-passphrase` while the passphrase is in the Keychain.

## Drain records

### Roadmap drain — 2026-10-15

16 inbox items: **14 placed, 2 deferred, 0 killed, 0 discharged**. Authored-false rate: 0 / (14 + 0) = 0%. Verification baseline: `573fb12` (v1.9.0). Tracks 71A (v1.7.2, PR #205), 71C (v1.7.3, PR #203), 71D (PR #206), 72A (v1.8.1, PR #208) and 72B (v1.8.0, PR #207) were recorded independently in shipped history, with v1.8.2 (PR #209) and v1.9.0 (Muse, PR #210) under additional release history. Track 71B (PR #204, v1.7.1) is **not** recorded: its receipt says "Track 71B not accepted" and the owner deferred C1 to a follow-up card, so its remainder continues as Track 74A. Groups 71 and 72 are tombstoned. No active pins were declared.

| Inbox item | Disposition |
|---|---|
| [review] Direct test writers can still touch real account state | place → Track 74A (formerly 71B; manifested 2026-10-08 through `_log_unexpected`) |
| 75A publish and presence must replace model pieces and supply the state seam | place → Track 75A (presence writes move from 81A) |
| 76A must replace snapshot, compaction and own-storage reconciliation | place → Track 76A |
| 77A must replace the peer reader behind observable injected entry points | place → Track 77A |
| 78A classification and resolution must honor the shared publication contract | place → Track 78A |
| 80A fleet creation, join and rejoin must replace model registry entries | place → Track 80A |
| 81A retire and delivery status must preserve simulator ownership boundaries | place → Track 81A |
| 2.0.0 acceptance requires no model pieces and explicit gap disposition | place → ROADMAP Phase 4 note on the 2.0.0 cut (acceptance condition, not a Track) |
| 81A must define active membership before retirement under delayed presence | place → Track 74B (design and model), implemented by Track 81A |
| Decide saved-policy restore fork recovery without losing a recoverable edit | place → Track 73C (design and model), implemented by Track 76A |
| Decide current-policy restore recovery after fleet-wide deletion and seq reuse | place → Track 73C, implemented by Track 76A |
| Preserve non-UTF-8 union bytes or obtain an explicit outcome decision | place → Track 74B, implemented by Track 78A |
| Specify recovery of a partial exclusively created own commit | place → Track 73C, implemented by Tracks 75A and 76A |
| [manual] Report the largest synced subtrees in 2.0 status/diag | defer → Future |
| [manual] Re-measure the gstack set at cutover step 1 | place → Track 84A (cutover runbook step 1). Method kept here: walk `~/.gstack/{projects,analytics,retros}` with the walker's global EXCLUDED list and current gstack globs, gzip each file at the default level for sealed size, prune nested checkouts; reader-check any kept tree over about 20 MB sealed before adding a glob. Baselines 2026-10-12 on the measured Mac: a defect-candidates runs tree 101.5 MB raw / 22.5 MB sealed; `kbitz-mind-meld/gate-diff` 24.1 MB raw. |
| [manual] Choose a 2.0 mechanism that ends ad hoc gstack glob batches | defer → Future |

**Leftover plan:** former Track 74A ("Preserve coherent state after config publication failures") is killed by owner-approved scope amendment: its defects were reproduced only with injected flush failures, the card itself says registration self-heals, and the device registry, crypto-init backfill and `mm recover --abandon-manifest` it repairs are 1.x code that Track 83A deletes (spec 1.x freeze). Re-verified at `573fb12`: `_register_and_save` still deletes `devices/<id>.json` on any save exception, and crypto backfill still lets `StorageError` through.

**Shipped-PR backlogs** (filed only in PR bodies and receipt comments): PR #205's eight 1.x items (heal hardening, own-GC in autopush, pull conflict-copy cleanup, crypto-init and corrupt-manifest `mm init` remedies, own-manifest diag status, coverage gaps G1 and G2, root SPEC "bytes freed") are killed under the 1.x freeze. PR #203's D3 backlog: the version byte in the AAD, passphrase normalization and an expected `fleet_id` on unlock go to Track 80A; `.stfolder/` and other markers counted as foreign, and `syncthing_id` grammar and freshness go to Track 82A; mixed part-count families and torn multipart recovery go to Track 73C; the AGENTS.md storage-key rule and the routing leaf list go to Track 84A; the rest (fork names, `mtime` bounds, drive-prefix and case collisions, decode peak, bounded reads, `StoreError` hierarchy, decode-once and simplification advisories, compression side channel, coverage gaps, Python 3.11 timestamp verdict) are killed as unobserved and reversible or inconsequential. PR #206: the `tests/sim/README.md` link is discharged@`42f4f57`; the `.stignore` fixture check goes to Track 82A; native Syncthing conformance is deferred to Future; docstring plan IDs, `_busy`/`ready_at`, scan cost, how-to docs and the `_patch_legacy` Muse paths are killed. PR #208: X3 (scheduled deep run) is deferred; V13 (cross-OS Claude path check) goes to Track 79A; row 46 (isolated `tests/sim` time) goes to Track 75A. PR #207: O6 acceptance is held in Future as REQUIRED / PENDING; root SPEC's gstack default list goes to Track 84A; the `PYTEST_ADDOPTS` leak into the nested simulator runner (seen twice, in pregate diagnostics only) is killed. PR #210's (a)–(h) are killed as unobserved and reversible (no standing Muse blocker, no observed pruning or duplicate envelope id); Linux Muse paths fall under O7 acceptance on the soak.

**Future membership:** 94 → 53. The manifestation gate killed 39 unobserved defect claims, the 1.x freeze killed 6 (`_resolve_interactive_loop`, two-machine test bootstrap, abort transactionality, machine-readable diag conflicts, pull-history failure reasons, `_open_crypto_session`), and "README prerequisites and a timed two-Mac preview/apply walkthrough" is discharged@`2ddaa39` (README prerequisites line; the walkthrough exists; the timing remainder is iCloud onboarding that 2.0 replaces). Five appended: O6 acceptance, native Syncthing conformance, the scheduled deep run, and the two deferred inbox items. "Overdue counterless-FINISHED tracking and an SQLite authorizer" is kept intact: its authorizer half is privacy hardening. Every other bullet is byte-identical. The pointer count was also stale (93 for 94 bullets after PR #210).

**Version:** documentation-only regeneration; no bump.

### Roadmap drain — 2026-10-11

8 inbox items: **4 placed, 4 deferred, 0 killed, 0 discharged**. Authored-false rate: 0 / (4 + 0) = 0%. Verification baseline: `a8afa88` (v1.7.0). This regeneration composed with `/project-spec`: docs/SPEC.md now selects MVP-1 on the 2.0 Syncthing store for macOS and Arch-based Linux, 1.x is frozen at cutover, and the two store designs supply 17 new Tracks. Tracks 70A (v1.7.0, PR #201) and 70B (v1.5.2, PR #199) were recorded independently in shipped history; 70A's two-Mac acceptance (14B) stays REQUIRED / PENDING in Future, not waived. Former Track 71A was re-verified at HEAD and renamed 74A. No active pins were declared.

| Inbox item | Disposition |
|---|---|
| Check Track 71A's invariant documentation routing | place → Track 74A read-first (`docs/invariants/README.md`) |
| Read Conductor's tmpdir/env-override Cursor stores if one is ever observed | defer → Future |
| Switch every reader's permanent-reason remedy to `mm update` | place → Track 71A |
| Per-store failure isolation for malformed or io_error Cursor stores | defer → Future |
| Structural guard: integration tests must never write real `~/.config/mind-meld` state | place → Track 71B (manifested 2026-10-08; still visible in `mm status` on 889e42c0 on 2026-10-11) |
| Overdue counterless-FINISHED tracking and an SQLite authorizer | defer → Future |
| Re-check Cursor ACP (Paseo) capture after each cursor-agent upgrade | defer → Future |
| [plan-devex-review] Link the mm lock-contention error to a README troubleshooting anchor | place → Track 71A (the unknown `plan-devex-review` source tag is drained with it) |

**Future membership against the new target:** 100 → 93. Twelve bullets killed because the 1.x store, mtime-ordered apply and conflict sidecars are retired at 2.0 by the spec decision of 2026-10-11 (Future-clamped peer mtime; `_promote_target_will_sync` exclude miss; mtime-skipped peers; parallel blob fetch; blob-directory peer recovery; devices-write-lock coupling; machine-readable GC outcomes; pull-history `sidecar=` plumbing; conflict GC retention age; stale conflict-copy cleanup on unchanged pulls; historical blob integrity inventory; and "Three-way merge base", absorbed by the 2.1 policies deferral). Two promoted into Tracks: "Deregister/prune retired devices" → Track 81A (`mm devices retire`); "Push `uploaded` rows crowd pull outcomes" → Track 73B (the `history` table). Seven appended: 2.1 policies, 2.2 resolver, 70A's 14B acceptance, and the four deferred inbox items. Every other bullet is byte-identical. "Nothing wires `mm autopush` on the maintainer fleet" was re-observed on device 889e42c0 on 2026-10-11 (no Claude, Codex, launchd or cron hook); its text is unchanged.

**Documents:** root `SPEC.md` keeps the 1.x architecture with an in-place authority notice (tests and code cite its path); `docs/designs/mind-meld-v1.md` moved to `docs/archive/` with a superseded notice; AGENTS.md gained the product-scope bridge. The audit's archive advisories for the live design docs are overridden as before: they cite external host versions or carry live obligations. Documentation-only regeneration needs no version bump.


### Roadmap drain — 2026-10-06

8 inbox items: **5 placed, 3 deferred, 0 killed, 0 discharged**. Authored-false rate: 0 / (5 + 0) = 0%. Verification baseline: `b70f70d`. Tracks 69A/69B discharged independently against approved scope and attributable merged receipts; 69A's pending fallback closes its guide deliverable, while S13 remains REQUIRED / PENDING, 0/3 in Future. Two new Groups contain three Tracks. No active pins declared. All 96 prior Future bullets remain byte-identical; the three deferred inbox entries and required S13 execution follow-up make 100.

| Inbox item | Disposition |
|---|---|
| Replace unsafe lock-removal contention advice | place → Track 70B |
| Clarify broad pinned-install invariant wording | place → Track 70B |
| Read Conductor 0.90.1's SQLite Cursor run store | place → Track 70A |
| Price Cursor models that carry bracketed parameters | defer → Future |
| Init deletes device registration after a published config save fails | place → Track 71A |
| Two handlers catch OSError for helper writes that raise StorageError | place → Track 71A |
| A paused terminal pauses a streaming mm update | defer → Future |
| Teach the mm update progress parser uv's wording | defer → Future |

Current source confirms the five placed defects/gaps. SQLite source demand is the live observation and explicit follow-up decision in PR #192; no remaining SQLite stores were found locally on 2026-10-06, so fresh real-corpus/live qualification remains an implementation gate. Deferred pricing and terminal concurrency preserve the user's decisions; uv progress awaits a real backend transcript/use. Existing Future triggers were reviewed against the index and changes since `e9e56db`; no other promotion or completion was established. The inherited memory evidence remains NO QUALIFIED ROUTE, native 0/48 UNSTARTED/INCONCLUSIVE.

Both former Track IDs remain frozen in independent history, Group 69 is retired, and new work starts at 70. No existing unfinished ID is renamed. The same-file sequence is SQLite reader before durable CLI repair. The archive suggestions refer to host versions, historical baselines or documents with live obligations; none is moved. PROGRESS covers every tagged/changelog version. Documentation-only regeneration needs no version bump.


### Roadmap drain — 2026-10-03

One inbox item: **1 placed, 0 deferred, 0 killed, 0 discharged**. Authored-false rate for the inbox: 0 / (1 + 0) = **0%**. Verification baseline: `a3c5050` (v1.4.0); this regeneration changes documentation only.

| Inbox item | Disposition / destination | Evidence |
|---|---|---|
| [review:severity=informational,files=src/mind_meld/fsutil.py] atomic_write_bytes docstring promises an untouched target after a post-rename failure | place → Track 69B | fsutil.atomic_write_bytes replaces before parent fsync; the existing tests/test_fsutil.py::TestAtomicWriteBytes.test_parent_fsync_failure_is_fatal and Track 68A C2 exercise published bytes followed by an error. The docstring still promises the old target survives every failure. Audit consumers before filing a separate demonstrated caller defect. |

**Shipped reconciliation:** Track 68A, PR #190 / `a3c5050`, is archived under its accepted tests-only no-route scope and the attributable 88-row receipt (87 verified, A39 user-deferred by D8). D7 model limits and Q1–Q18 remain promotion gates. PR #189 / `3642f6f` delivers v1.3.0 self-update with no declared Track ID; its required, unpassed S13 post-release testing is placed in Track 69A. No active pins were declared; Group 68 is retired/reserved. No former live Track is renamed to either new card.

**Future membership:** 94 existing bullets retained byte-for-byte; “Explicit upgrade check” and “Subprocess pipx upgrade” are discharged@3642f6f because mm update forces discovery and update_or_nudge runs pipx. The obsolete “cli.py micro-cleanups” work order is **killed**, not discharged: `_resolve_mm_events_dir` has no current definition anywhere in src/tests, and the remaining “status-enum follow-up” names no target or acceptance; reopen only with a current symbol and concrete benefit. Its former import/_empty_outcomes portions were already assigned to historical Track 18A. Two scoped follow-ups are appended: complete live memory qualification with its existing prerequisites/owners, and D8 cold-reader onboarding timing. **97 → 96**. No transport, parity, bridge or Cursor gate is promoted by the no-route receipt.


### Roadmap drain — 2026-09-30

7 inbox items: **1 placed, 6 deferred, 0 killed, 0 discharged**. Inbox authored-false rate: 0 / (1 + 0) = **0%**. Verification baseline: `c43b777` (v1.2.0), even with `origin/main`. The five memory work packages remain linked to [the formal plan](designs/memory-continuity.md); qualification is executable now, and the remaining four have explicit promotion gates. Forgetting F1-F4, echo prevention E1-E4 and hostile-content checks H1-H4 are required during prototyping and initial implementation, not deferred cleanup.

| Inbox item | Title | Disposition / destination | Evidence or reason |
|---|---|---|---|
| 1 | Qualify Codex memory portability and recall | place → [Track 68A](ROADMAP.md#track-68a-qualify-codex-memory-portability-and-recall) | Codex memories are absent from DEFAULT_SOURCES; native export/import and fresh-session recall remain unqualified. |
| 2 | Deliver Codex memory sync across Macs | defer → [Future](roadmap-future.md#codex-memory-sync) | Promote after Track 68A establishes a supported export/recall contract, retirement and ancestry semantics, and concrete implementation footprints; size the production work from that evidence. |
| 3 | Qualify Claude and Codex memory parity across the fleet | defer → [Future](roadmap-future.md#memory-fleet-parity) | Promote after the Codex transport/recall implementation passes its deterministic gates; requires live two-Mac access and the same scenarios for Claude and Codex. |
| 4 | Share project memories between Claude and Codex | defer → [Future](roadmap-future.md#cross-agent-memory) | Promote after the same-agent fleet parity gate passes; keep both cross-agent directions and the forgetting/echo gates in the initial bridge. |
| 5 | Establish Cursor's memory capability and portability contract | defer → [Future](roadmap-future.md#cursor-memory-contract) | Promote discovery after Track 68A supplies the reusable adapter requirements; implementation follows Codex parity and the qualified shared-view contract. Cursor discovery does not block Claude/Codex sharing. |
| 6 | Price Cursor Grok Fast after a real census | defer → [Future](roadmap-future.md#cursor-fast-pricing) | Keep the real observed Fast-run census trigger. The committed Cursor CONTRACT.md still records fast=true as unobserved; this regeneration establishes no newer census and adds no pricing. |
| 7 | Revisit Cursor auto/composer classification only after observed use | defer → [Future](roadmap-future.md#cursor-auto-classification) | Keep the real observed auto/composer-run trigger and explicit compatibility decision. The committed Cursor CONTRACT.md records neither in the initial corpus; usage capture alone does not authorize customization sync. |

**Former active plan:** 66A's alias removal and 1.0 cut are `discharged@2ddaa39`; the compatibility refusal behavior shipped beyond the old card. Its cache-jitter task is **killed**, not discharged as a fix: the implementation's live probe did not reproduce the premise for reader 0 (zero writes in 39 steady failed passes; the later-reader allowance mechanism is documented but unobserved; `docs/invariants/events-retro.md`, Track 66A probe). Group 66 moves to shipped history.

**Additional shipped work:** `c76f41d` (v1.1.0 unified agent reporting) and `c43b777` (v1.2.0 Cursor usage) both called themselves 67A. Group 67 records both by version and title, without rewriting historical references or pretending two PRs were one Track session. No ID renames; 68A is new.

**Future membership:** 91 existing bullets retained verbatim, 2 discharged, 6 appended: **93 → 97**. “Stable note codes for all 19 `## Notes` strings” and “Retro-card machine/cause diagnostics” are `discharged@c76f41d`: `aggregator._render_health_block` emits coded `MM_HEALTH` issues with machine/cause details and remedies, `retro_fleet/SKILL.md` consumes them, and `tests/test_docs_routing.py` checks the interface. The latter had no more specific residual requirement to carry forward. All other deferred entries retain their existing text and triggers; none is promoted on an unmeasured premise.

**Advisory override:** keep `docs/designs/grok-build-usage-reader.md`, `host-parity.md`, `memory-continuity.md` and `sync-gstack-context.md` in place. The archive heuristic mistakes external host versions or historical baseline references for completed mm plans; these remain active source documents. PROGRESS covers every tagged/changelog version. Documentation only; no version bump.

### Roadmap drain — 2026-09-21

10 inbox items from the Track 63A–65A /autoplan reviews and the PR #181 /ship run, drained under the user's cue "as few Tracks as possible, ideally one, or kill": **2 placed, 4 deferred, 1 killed, 3 discharged**. Authored-false rate: 3 / (2 + 3) = 60%; all three discharges are roadmap-reconcile items the shipping Tracks filed against themselves, not stale observations. Verification baseline: `cdffc34` (v0.14.18). Ground truth closed all three Current Plan Tracks first: 63A (v0.14.16 `77338bd`), 64A (v0.14.17 `a12096c`), 65A (v0.14.18 `cdffc34`); Groups 63–65 are appended to `docs/roadmap-shipped.md`. The regenerated plan is one Group with one Track, and that Track is the v1.0.0 cut.

| Inbox item | Title | Disposition / destination | Evidence or reason |
|---|---|---|---|
| 1 | Standing constraint: publication-evidence precision needs a user-observed wrong status | place → ROADMAP.md standing constraint | A rule that refuses Tracks belongs in the header, not in a Track. |
| 2 | Discharge "Say on the wire whether a host capture was requested" when 65A's T5 ships | discharged@cdffc34 | `attemptlog.py` exists; `cli.py:6236` / `:6264` render the attempt from the local record. The Future bullet is deleted. |
| 3 | Re-card shipped 63A/64A and 65A in one /roadmap run | discharged@cdffc34 | This run. |
| 4 | Autopush read-budget predictor | kill | Its own context rejects the only mechanism it could name (a count-derived budget), and the observation it wants already exists: `mm diag` prints the sweep estimate (`_host_read_sweep_line`) and the standing `deadline` blocker with `last_complete_ms` and `last_deadline_allotted_ms`. |
| 5 | Sealed or incremental lineage tier | defer → docs/roadmap-future.md | Real design, real prerequisite; measured 2.4x / 2x headroom against its own trigger. |
| 6 | Codex `token_usage_record` drift tripwire | defer → docs/roadmap-future.md | Concrete form of the 46B drift trigger; the reader reads every rollout in the census today, and a drift would show on status as `completed, no usage in snapshot`. |
| 7 | Host cache encoding trigger restated and over-cap notice | defer (datum merged into the existing bullet); `too_large` notice killed | Same topic as the 2026-09-05 bullet; the notice is forward defence for a condition 16x away. |
| 8 | Per-reader carry-forward in the fleet aggregator | defer → docs/roadmap-future.md | v0.14.16 chose device-wide replacement on purpose; 65A's `empty_sources` is the data a change would need; trigger stated. |
| 9 | Track 63A's ROADMAP.md card was hand-edited instead of drained by /roadmap | discharged@cdffc34 | This run is the formal re-card; the shipped entry supersedes the hand-edited card. |
| 10 | host_usage.py: last_deadline_allotted_ms jitter can defeat the failed-pass write-skip | place → Track 66A | Premise verified by reading `_carry_read_timing` and `_skip_failed_cache_write` at cdffc34: two independent monotonic reads, whole-map equality. |

**Leftover Future bullets discharged by v0.14.16–v0.14.18** (deleted from `docs/roadmap-future.md`): "Say on the wire whether a host capture was requested" (discharged@cdffc34, above); "Re-read standing host-usage blockers on an interactive no-op push (T3-B)" (discharged@a12096c — every attended push captures, `--capture-usage` removed); "`warm_host_cache_inline`'s docstring cites a stale measurement" (discharged@77338bd — `grep 573 host_usage.py` returns 0 hits; the docstring cites the 2026-09-17 qualification).

**Former active plan:** all three 2026-09-17 IDs shipped under their own numbers; nothing recycled, no renames table. Group 66 is the first free number after shipped history.

| 2026-09-17 ID | Title | 2026-09-21 disposition |
|---|---|---|
| 63A | Keep the warm Codex read inside its budget | shipped as 63A (v0.14.16) |
| 64A | A usage refresh is not a push, and exit 4 says what it skipped | shipped as 64A (v0.14.17); re-scoped at review — `--capture-usage` removed, every attended push captures |
| 65A | Reader-scoped publication evidence on status and diag | shipped as 65A (v0.14.18), with a fifth task (the attended-attempt record) added at the gate |

New Track: 66A (Cut v1.0.0: retire the pre-1.0 conflict alias and converge the failed-read cache write). Future: 93 → 93 (three discharged, three appended, one amended). Shipped history is append-only. Not acted on: the audit's archive finding for `docs/designs/sync-gstack-context.md` (a live design doc cited from CLAUDE.md and AGENTS.md), same call as 2026-09-17. Version recommendation when 66A ships: MAJOR (1.0.0).


### Roadmap drain — 2026-09-17

14 inbox items from the Track 58A–62A /autoplan reviews and the PR #176–#178 /ship runs: **4 placed, 8 deferred, 1 killed, 1 discharged**. Authored-false rate: 1 / (4 + 1) = 20%, and the one discharge is the roadmap reconcile this run performs rather than a stale observation. Verification baseline: `66b5293` (v0.14.15). Ground truth closed five Tracks first: 58A (v0.14.12 `dfc7a09`), 59A and 60A (v0.14.13 `e7eb301`, one PR by user decision), 61A (v0.14.14 `5079b3f`), 62A (v0.14.15 `66b5293`); Groups 58–62 are appended to `docs/roadmap-shipped.md` and Phase 3 (Retro fidelity) is complete.

| Inbox item | Title | Disposition / destination | Evidence or reason |
|---|---|---|---|
| 1 | pull-history rotation is dominated by push `uploaded` rows | defer → docs/roadmap-future.md | `pullhistory._ROTATE_BYTES = 1_000_000`; a logging-contract change needs a policy first. |
| 2 | Preview/apply parity tests for `gc --dry-run` and `recapture --dry-run` | defer → docs/roadmap-future.md | No parity or twin-state test in `tests/test_retention.py` or `tests/test_track_30a.py`; tests only, P3. |
| 3 | Prerequisites and a timed two-Mac preview/apply walkthrough in README | defer → docs/roadmap-future.md | Single-operator audience; time it at the next real onboarding. |
| 4 | Distinguish explicitly requested host captures on the wire | defer → docs/roadmap-future.md | No reader named; the one candidate is status's "Latest attempt: unknown (no attempt receipt)" line. |
| 5 | Host read-budget override | place → Track 63A | Its own instruction was to extend the Future proposal, and that proposal's trigger fired (below). |
| 6 | Audit forensic append callers for silent-write assumptions | defer → docs/roadmap-future.md, audit performed | `flock_append_jsonl` has two callers; of `write_push_event`'s four call sites only `cli.recapture` makes a success claim on a non-strict write. The bullet carries the finding. |
| 7 | Reconcile docs/ROADMAP.md for Track 59A + 60A | discharged@e7eb301 | This run records Groups 59 and 60 in `docs/roadmap-shipped.md`. |
| 8 | Extract a shared `_open_crypto_session` helper | defer → docs/roadmap-future.md | Six sites at HEAD, not the five filed; take it with the next change to one of them. |
| 9 | Minor redundant path/hash re-reads in mm-events + crypto repair | kill | The filer measured the cost as negligible and bounded by design (crypto-init candidates, home-dir ancestor depth), and one of the two re-reads sits beside a deliberate pre-unlink guard. No trigger can fire. |
| 10 | Disambiguate historical device IDs with shared display prefixes | defer → docs/roadmap-future.md | Display only; `_DEVICE_TABLE_LABEL_WIDTH = 8`; trigger is a real prefix collision. |
| 11 | Price Claude fast mode when it is first used | defer → docs/roadmap-future.md | Human trigger as filed; `usage.speed` was `"standard"` on all 22,042 rows that carried it. |
| 12 | Per-reader "completed, no usage" is whole-row-scoped on the mm status/diag read path | place → Track 65A | `local_host_capture_candidate` still emits `"empty": not row.lifetime_by_family`. |
| 13 | Five smaller mm push --capture-usage rough edges from adversarial review | place → Track 64A (items 1, 2 and the reopened third finding) and Track 65A (items 3, 4, 5) | Every sub-claim re-verified at 66b5293; the third finding's harm is `aggregate_git`'s `snap_total` / `snap_zero`, which exclude only recapture-origin rows. |
| 14 | Investigate a claimed TOCTOU on the accepted-manifest digest check | place → Track 65A | `_iter_typed_objs` already computes a single-pass digest that `recorded_row_revision` discards; using it is cheaper than settling reachability. |

**Promoted from Future on a fired trigger.** "Warm Codex read headroom and the healthy-pass cache rewrite" named "a warm pass above 200 ms, or a corpus above ~900 rollouts". Measured 2026-09-17 on a scratch copy of the cache: 1,052 rollouts, 384 ms warm, `deadline` on 3 of 3 attempts at the 250 ms autopush budget, 116 ms left under the 500 ms interactive budget; 186 ms over 752 rollouts on 2026-09-09. Probes are kept in `~/.gstack/projects/kbitz-mind-meld/63a-probes/`. It is Track 63A, first in priority, and not a Hotfix: interactive pushes still publish Codex and autopush is not wired on the measuring Mac.

**Recovered, never filed.** The PR #179 /ship doc-sync noted that SPEC.md's command reference lacks `mm recapture` and `mm push --capture-usage` (`grep -c` returns 0 at 66b5293). It rides Track 64A, which settles the exit codes that entry must state.

**Former active plan:** IDs below refer to the 2026-09-14 plan. All five shipped under their own numbers, so nothing recycled and there is no renames table.

| 2026-09-14 ID | Title | 2026-09-17 disposition |
|---|---|---|
| 58A | Refresh verified rates, re-pin the Grok census, and make model usage easier to read | shipped as 58A (v0.14.12); census pinned to 1.0.30, not the carded 1.0.25 |
| 59A | Create the mm-events root only from init and a verified real push | shipped as 59A (v0.14.13); the refusal task was reversed at the gate — a missing event file is a deletion |
| 60A | Inspection commands observe storage and the upgrade cache without repairing them | shipped as 60A (v0.14.13), inside 59A's PR |
| 61A | Refresh host usage from an attended push and say what the last push published | shipped as 61A (v0.14.14) |
| 62A | Make the other previews write-free | shipped as 62A (v0.14.15), with the multi-peer pull predictor |

New Tracks: 63A (Codex read headroom), 64A (usage-only capture), 65A (reader-scoped publication evidence). Future: 86 → 93 (one promoted, eight appended). Shipped history is append-only. ROADMAP.md's audit-configuration paragraph now says the caps are machine-local (16 / 6 where it was written, 24 / 5 on the Mac that ran this regeneration). Not acted on: the audit's archive finding for `docs/designs/sync-gstack-context.md` (a live design doc cited from AGENTS.md).


### Roadmap drain — 2026-09-14

19 inbox items from the Track 52A–57A /autoplan reviews: **8 placed, 11 deferred, 0 discharged, 0 killed**. Authored-false rate: 0 / (8 + 0) = 0%. Verification baseline: `a7d9bca` (v0.14.11). Ground truth closed six Tracks first: 52A (v0.14.6 `bb39230`), 53A (v0.14.7 `fe21a57`), 54A (v0.14.8 `735ed02`), 55A (v0.14.9 `d85e504`), 56A (v0.14.10 `dbc926e`), 57A (v0.14.11 `a7d9bca`); Groups 52–57 are appended to `docs/roadmap-shipped.md`. The pricing page was re-read on 2026-09-14 before item 6 was placed.

| Inbox item | Title | Disposition / destination | Evidence or reason |
|---|---|---|---|
| 1 | Record the Grok costUsdTicks census against the "Grok publishes its own billed cost" Future item | defer → Future bullet edited in place | The bullet still said "needs its own census first"; the 303-turn census is now recorded on it. |
| 2 | Re-pin the Grok usage census from 1.0.13 to 1.0.25 | place → Track 58A | `GROK_USAGE_CENSUS_HOST_VERSION = "1.0.13"` in host_usage.py; the contract header agrees. |
| 3 | Tripwire when a Codex request reaches OpenAI's 272K long-context tier | defer → docs/roadmap-future.md | Forward defence; `model_context_window` is not read by host_usage.py (0 hits). |
| 4 | First-class attended usage refresh (`mm push --capture-usage`) | place → Track 61A | `_push_core` returns before the events tail on "Nothing to push"; README sends users to `mm recapture 1d`. |
| 5 | Profile and shrink the Codex host-cache round trip | defer → merged into the retitled "Warm Codex read headroom" bullet | Trigger not yet fired (0.18–0.20 s of 0.25 s); `read_codex_usage` commits `locked.data` on every complete pass. |
| 6 | Refresh Anthropic rates: Sonnet 5 $2/$10, Fable 5.1 cache hits 0.025x | place → Track 58A | `sonnet` tier is `_tier(3.0, 15.0)`; `fable` tier derives a $1.00 cache read; the pricing page confirms both claims and adds that Fable 5 / Mythos 5 keep 0.1x. |
| 7 | Cross-machine dedup of host usage by turn key | defer → docs/roadmap-future.md | New wire content plus an accounting schema; needs its own proposal. |
| 8 | `mm status`: say whether the last push published each host reader | place → Track 61A | status prints "grok prior successful scan"; nothing reads a row's `token_sources` back. |
| 9 | Make the other previews write-free (56B) | place → Track 62A | Five commands call `_get_config()` / `_init_crypto_session` with the defaults; pull's conflict migration and excluded rows have no knob. |
| 10 | mm-events bootstrap masks the missing-published-root refusal on the real push (E4) | place → Track 59A | `resolve_sources(..., bootstrap=not dry_run)` precedes `_refuse_unavailable_selected_sources` in `_push_core`; `get_sources` defaults `bootstrap=True`. |
| 11 | Inspection commands repair shared storage (E7) | place → Track 60A | `status` → `_init_crypto_session` default `read_only=False`; `diag` and init's probe → `fetch_crypto_init(backend)` default `repair=True`; deletion precedes `verify_passphrase`. |
| 12 | `mm status` fetches over the network despite "reads cache only" (E3b) | place → Track 60A | `status` calls `upgrade.check_for_upgrade(config)`, which write-throughs via `locked_json_rmw`. |
| 13 | Re-read standing host-usage blockers on an interactive no-op push (T3-B) | defer → docs/roadmap-future.md | Trigger unchanged; Track 61A gives the attended path. |
| 14 | Autopush warm-read headroom and the healthy-pass rewrite on large Codex corpora | defer → merged into the retitled "Warm Codex read headroom" bullet | Same measurement as item 5; one bullet, one trigger. |
| 15 | Add a `[retro] codex_host_usage` knob | defer → docs/roadmap-future.md | No `codex_host_usage` in config.py; no user need demonstrated. |
| 16 | Complete the deferred plain-stderr display audit beyond direct safe_str calls | defer → merged into the plain-stderr bullet | The item's own instruction; the builders it names exist. The bullet's "55A ships" trigger fired 2026-09-09 and it stays deferred as display quality. |
| 17 | Carry a sanitized failure reason on pull-history `failed` rows | defer → docs/roadmap-future.md | `pullhistory.append` has `sidecar=` and no `detail=`. |
| 18 | Decide whether interactive `mm pull` should exit non-zero when files failed to apply | defer → docs/roadmap-future.md | User kept exit 0 at the 53A gate; `pull()` still exits 0 with `Pull incomplete:`. |
| 19 | `mm diag` lists repo-local `GIT_*` variables present in mm's own environment | defer → docs/roadmap-future.md | No `scrubbed_git_env` in cli.py (0 hits); deferred by the 55A /autoplan as E6. |

**Former active plan:** IDs below refer to the 2026-09-06 plan. Six of its seven Tracks shipped; the survivor kept its number and absorbed the provenance refresh by user decision (D1 = A, "5 PRs"), so one PR closes Phase 3. The audit's caps were raised for this repo (`roadmap_max_files_per_track=16`, `roadmap_max_session_weight=6`, via gstack-extend `bin/config`) because release-bearing cards here declare 9–14 files and shipped Tracks routinely land 10x their carded size as one PR (53A: 1,790 insertions on a weight-4 card).

| 2026-09-06 ID | Title | 2026-09-14 disposition / ID |
|---|---|---|
| 52A | Enforce a no-control postcondition in the shared sanitizers | shipped as 52A (v0.14.6) |
| 53A | Contain apply exceptions without losing completed-file bookkeeping | shipped as 53A (v0.14.7) |
| 54A | Report failed Codex capture and remove obsolete reader helpers | shipped as 54A (v0.14.8) |
| 55A | Scrub the git environment for mm's git subprocesses | shipped as 55A (v0.14.9) |
| 56A | Make push dry-run setup honor the no-mutation contract | shipped as 56A (v0.14.10) |
| 57A | Price verified Grok usage | shipped as 57A (v0.14.11) |
| 58A | Make model usage easier to read without changing what totals mean | 58A, retitled "Refresh verified rates, re-pin the Grok census, and make model usage easier to read" (premises re-verified; v0.14.11's Grok `>=` rendering added to the example) |

New Tracks: 59A (mm-events root ownership, E4), 60A (inspection without repair, E7 + E3b), 61A (attended host-usage refresh), 62A (write-free previews, 56B). Future: 79 → 86 (three bullets edited in place, seven appended). Shipped history is append-only. Not acted on: the audit's archive advisory for `docs/designs/sync-gstack-context.md` (a live design doc cited from AGENTS.md).


### Roadmap drain — 2026-09-06

5 inbox items, all `[plan-eng-review]` from the Track 48A–51A /autoplan reviews: **3 placed, 2 deferred, 0 discharged, 0 killed**. Authored-false rate: 0 / (3 + 0) = 0%. Verification baseline: `38222ac` (v0.14.5). Ground truth closed four Tracks first: 48A (v0.14.2 `09d98ab`), 49A (v0.14.3 `0c1a969`), 50A (v0.14.4 `cc22b6c`), 51A (v0.14.5 `38222ac`); Groups 48–51 are appended to `docs/roadmap-shipped.md`.

| Inbox item | Title | Disposition / destination | Evidence or reason |
|---|---|---|---|
| 1 | Contain apply exceptions without losing completed-file bookkeeping | place → Track 53A | `_download_and_apply` still calls `_apply_incoming_file` with no exception boundary at 38222ac; the only `finally` stops the progress bar. |
| 2 | Harden existing Rich and multiline sanitizer consumers against nested escapes | place → Track 52A | `strip_terminal_escapes` is one `re.sub` pass; its docstring and the v0.14.4 CHANGELOG record the follow-up. 12 plain-stderr `safe_str` sites exist (not 3 — corrected 2026-09-06 /ship review); the card names 3 in scope and defers the other 9 to roadmap-future.md. |
| 3 | Decide whether unchanged pulls should retry stale conflict-copy cleanup | defer → docs/roadmap-future.md | Policy choice with a pinned test (`test_conflict_copy.py::TestConflictPublishThenCleanup::test_cleanup_failure_then_unchanged_retry_leaves_extras`), not a defect. |
| 4 | Audit historical blob integrity and design verified targeted re-upload | defer → docs/roadmap-future.md | v0.14.3's per-file pull check is the detector and has not fired; the trigger is a `content check failed:` line on any Mac. Inventory and repair designs are recorded on the bullet. |
| 5 | Make push dry-run setup honor the existing no-mutation contract | place → Track 56A | `push` runs `_maybe_prompt_migration` and the fingerprint persist before `_push_core(dry_run=True)`; `resolve_sources` mkdirs mm-events unconditionally. |

**Former active plan:** IDs below refer to the 2026-09-05 plan. Priority order changed: the two new defects outrank the queued Codex-diagnostics and git-environment cards, so those four recycled. Old IDs are dated lineage in each card's `_Source:` line; nothing was blind-remapped, and the 2026-09-05 records below keep their own numbering.

| 2026-09-05 ID | Title | 2026-09-06 disposition / ID |
|---|---|---|
| 48A | Preserve conflict ownership and failed replacements | shipped as 48A (v0.14.2) |
| 49A | Publish complete, content-consistent snapshots | shipped as 49A (v0.14.3) |
| 50A | Sanitize rejected storage filenames | shipped as 50A (v0.14.4) |
| 51A | Keep mixed timestamp types from aborting pull | shipped as 51A (v0.14.5) |
| 52A | Report failed Codex capture and remove obsolete reader helpers | 54A (premises re-verified) |
| 53A | Scrub the git environment for mm's git subprocesses | 55A (premises re-verified; the `test_track_30a.py` assert moved from `:636` to `:678`) |
| 54A | Price verified Grok usage | 57A |
| 55A | Make model usage easier to read without changing what totals mean | 58A |

New Tracks: 52A (sanitizer postcondition), 53A (pull isolation), 56A (dry-run honesty). Future: 76 → 78; one existing bullet's citation moved from "Track 52A" to the title with dated lineage. `docs/invariants/events-retro.md` gained the 2026-09-06 hop on the unified-reporting ID trail. Shipped history is append-only. Not acted on: the audit's archive advisory for `docs/designs/sync-gstack-context.md` (a live design doc cited from AGENTS.md; v0.14.5 merely edited it).

### Approved roadmap refinements — 2026-09-05

Follow-up to the pre-existing-roadmap assessment at `f10bf34`, applied at the user's request. Current Plan keeps eight Tracks and their IDs. Track 53A remains the reproduced git-environment fix. Track 54A makes its requirement to leave unverifiable model aliases unpriced explicit. Track 55A is narrowed to presentation with preserved accounting boundaries; its title changes from “Report every agent the same way” to “Make model usage easier to read without changing what totals mean,” and Group 55 becomes “Usage presentation.” The Phase 3 end-state follows that scope. No schedule or file-footprint changes.

Six Future entries removed: three completed-goal entries discharged and three proposed designs killed. Two retained entries narrowed; 74 others remain verbatim. Future count: **82 → 76**. No new inbox items and no changes to shipped history.

| Removed Future entry | Disposition | Evidence / decision |
|---|---|---|
| Per-verb or sticky autorun breadcrumbs | discharged@01726ba | Per-verb storage shipped in v0.12.45. The push and pull entries are independent. A sticky-error extension is not implied by that completed request; require a demonstrated same-verb visibility problem and an explicit recovery rule before proposing one. |
| Un-hide and rename `--dump-host-usage` | discharged@69f95b2 | Public help and README expose the flag since v0.12.49. Reject the cosmetic rename/alias rather than keeping a completed discoverability request open. |
| Unify the seven per-agent enumerations | killed | Skill installation, sync consent, active readers, wire compatibility and model families are different domains. Their differing membership is required behavior; a universal registry would couple unrelated policies. Consolidate only a demonstrated duplicate within one domain. |
| Doc-lint: no agent-name triple in README prose | killed | Naming the supported agents is useful documentation. A phrase ban would enforce style rather than factual correctness. Keep factual routing and link assertions in the existing documentation checks. |
| `--dump-host-usage` is the subsystem's only forensic tool and is invisible. | discharged@69f95b2 | The public command is visible and documented. Coverage fields are also exposed by the current inventory dump. The reproduced Codex current-failure diagnosis belongs to Track 52A, not another unhide/rename project. |
| An empty host-usage row can overwrite a populated one under strict latest-wins. | killed | A completed empty observation is legitimate state. The proposed nonempty-wins guard would retain stale data after a genuine empty scan or opt-out. Failed capture must not fabricate a complete zero; fix that producer if reproduced. The completed-empty/failed-omission distinction is explicit in docs/invariants/events-retro.md and test_complete_omitted_then_complete_empty_preserves_wire_history. |

The closed unhide items do not authorize a cosmetic flag rename or alias. The closed per-verb item does not authorize sticky errors. Keep any independently reproduced diagnostic defect scoped to its actual consumer; Track 52A already owns Codex's false-ready diagnosis.

**Retained but narrowed:** host-cache GC now covers only current Codex/Grok caches, has no already-shipped Group 32 prerequisite, and needs measured accumulation beyond normal complete-pass pruning. Grok consent remains unresolved: consider usage-only operation through the existing verb first, preserving explicit customization sources; no commitment to three new CLI surfaces.

**Other deferred work:** measured performance gates and the separate reader-quarantine design remain in place. External skill/configuration work keeps its provenance until ownership transfer is recorded; this change does not modify fleet configuration.


### Roadmap drain — 2026-09-05

28 inbox items: **12 placed, 10 deferred, 6 discharged, 0 killed**. The 11 approved full-review findings are all placed. Authored-false rate for the inbox: 6 / (12 + 6) = **33.3%**; this measures already-shipped observations, not rejected ideas. Verification baseline: `2024ff6` (runtime code unchanged from `8be81ce`).

| Inbox item | Title | Disposition / destination | Evidence or reason |
|---|---|---|---|
| 1 | Intra-file resume for a Grok ledger that exceeds one read budget | defer → docs/roadmap-future.md | The reader still lacks an intra-file checkpoint; the filed >1 GB / three repeated interactive deadline trigger has not been demonstrated. |
| 2 | Host cache encoding trigger (deferred from original Track 46A) | defer → docs/roadmap-future.md | Keep the measured >100 ms or >25 MB gate. The dated filing measured 23.3 ms and 4.11 MB, below both gates. |
| 3 | Per-entry `states` cap on the Codex host cache | defer → docs/roadmap-future.md | No demonstrated pathological entry; preserve totals and cross-file dedup semantics before deciding a cap. |
| 4 | Add the xAI / `grok-4.6-build` price tier | place → Track 54A | Pricing remains absent in resolve_prices; ingestion recovery shipped at 8be81ce. |
| 5 | Ask whether Grok/Codex expose a supported usage surface | defer → docs/roadmap-future.md | Strategic vendor-surface research has no dependency on the immediate correctness repairs. |
| 6 | Reader-agnostic quarantine and drift classification (Track 46B) | defer → docs/roadmap-future.md | Retain the explicit next-drift / unsupported-after-upgrade trigger. The Codex diagnostic defect is placed separately and does not require quarantine. |
| 7 | Stale Track/version literals in code and test comments (15 across 10 files) | defer → docs/roadmap-future.md | Correct remaining historical comments alongside the files being changed; do not create a standalone code PR or carry the old count forward. |
| 8 | `_ensure_inversion_marker` makes every new Mac mis-migrate 100% of its conflict sidecars | discharged@f2624fb → v0.14.0 | Filename birth/era now drives migration; peer mtime remains the peer clock. |
| 9 | README documents the pre-inversion conflict direction, and contradicts itself two paragraphs later | discharged@f2624fb → v0.14.0 | README now says local remains canonical and remote bytes go to the conflict copy. |
| 10 | `_migrate_pre_inversion_conflict` uses `find()` where every sibling parser uses `rindex()` — unbounded `v0-` accretion | discharged@f2624fb → v0.14.0 | _migrate_pre_inversion_conflict now parses the final infix with rindex. |
| 11 | `mm gc --conflicts` can reap a live conflict, prints no paths, and has no preview by default | discharged@f2624fb → v0.14.0 | GC preserves live/recoverable copies, prints deletion paths, and bare --dry-run previews conflicts. Configurable age remains a separate deferred item. |
| 12 | `mm status` and `mm diag` never surface unresolved conflicts | defer → docs/roadmap-future.md | Status coverage shipped; defer the residual machine-readable diag conflict inventory until a consumer needs it. |
| 13 | `pullhistory.append_entry` has a `sidecar=` parameter that no caller ever passes | defer → docs/roadmap-future.md | Forensic filename plumbing remains useful but spans the apply outcome and pull accumulator; it is not needed to prevent the reproduced deletions. |
| 14 | Pull side does not reject conflict-shaped rel_paths from a peer manifest | discharged@f2624fb → v0.14.0 | _filter_excluded_paths rejects conflict-shaped names and .extend-root even with no configured excludes. |
| 15 | Conflict discovery walks trees that `exclude_patterns` removed from sync | discharged@f2624fb → v0.14.0 | The requested surface-asymmetry documentation exists in docs/invariants/conflicts.md; discovery intentionally still finds excluded copies. |
| 16 | Conflict GC retention age should be configurable | defer → docs/roadmap-future.md | Live and recoverable copies are now preserved regardless of age; configurable retention of redundant copies is lower priority. |
| 17 | `synclog.write_sync_log` writes `.mind-meld-log.md` for claude sources only | defer → docs/roadmap-future.md | Generalizing project-log layout is separate from the preservation fixes; status already exposes unresolved conflicts. |
| 18 | Sidecar deduplication deletes another canonical file’s conflict | place → Track 48A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 19 | Failed sidecar replacement removes the previous recoverable copy | place → Track 48A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 20 | Upload can replace an untouched file with another file’s bytes | place → Track 49A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 21 | Rejected manifest filenames bypass terminal sanitization | place → Track 50A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 22 | Unreadable existing files become deletion tombstones | place → Track 49A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 23 | Mixed JSONL timestamp types abort the remaining pull batch | place → Track 51A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 24 | Codex diagnostics report ready after an unsupported read | place → Track 52A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 25 | Delete the unused pre-refactor push-cursor wrapper | place → Track 53A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 26 | Delete the unreachable second recapture writer | place → Track 53A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 27 | Remove the orphan host model-id validator | place → Track 52A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |
| 28 | Delete the unused retro event iterator | place → Track 55A | Approved review reproduction at 8be81ce / rechecked at 2024ff6. |

**Full-review code provenance:** ROADMAP.md's `_Source:` lines cite the review's own R/C/H codes, which exist only in the review transcript — and Conductor workspaces are ephemeral. The code groups resolve against rows 18-28 above by destination Track: **R2/R3** → 48A (rows 18-19), **R1/C2** → 49A (rows 20, 22), **C1** → 50A (row 21), **R4** → 51A (row 23), **C3/H3** → 52A (rows 24, 27), **H1/H2** → 53A (rows 25-26), **H4** → 55A (row 28). Letter-to-row assignment *within* a pair was not recorded; the Track cards carry the reproduction detail, so nothing depends on it.

**Former active plan:** IDs below refer to the 2026-09-03 plan; they are historical, not current routing keys.

| Old task | Title | Disposition | Evidence or reason |
|---|---|---|---|
| 45A.1 | Reproduce under filesystem instrumentation | kill | Not discharged: no cause was named and `docs/invariants/conflicts.md` records none. Dropped by explicit decision on 2026-09-03 on three supports, only one of which is unconditional. (a) `mm pull` re-materializes a vanished sidecar because the pull diff hashes the live local file — **but only while the mtime-skip gate stays shut**: `cli.py:1871` returns `"skipped"` once the local file is newer than remote, so after the user edits their canonical the conflict path never runs again and nothing is re-materialized. Do not read this as self-healing. (b) 47A plus existing config excludes remove 22 of the 25 affected paths from sync — scope reduction, not repair; 3 remain. (c) The `mm status` conflicts line (`cli.py:4523`) makes any recurrence visible — this one holds unconditionally and is what the kill actually rests on. Honest residual: f2624fb's clock fix does NOT explain the 2026-09-01 event (those peer files were ~12 days old and never crossed the 30-day GC bar); cause unknown, may not be mm. Re-open only on a fresh recurrence observed through `mm status`. |
| 45A.2 | Fix it if it is mm | discharge | f2624fb fixed the demonstrated migration/GC defects; independent new review failures are placed in 48A. |
| 45A.3 | Immediate post-write existence warning | kill | A stat immediately after writing neither prevents the reproduced replacement loss nor proves a future deletion; fix the actual mechanisms in 48A. |
| 46A.1 | Per-state increment encoding and Grok resume | defer | Split into existing inbox cache-encoding and intra-file-resume Future entries; the dated trigger measurement does not justify active work. |
| 46A.2 | Bound a single entry | defer | Existing inbox states-cap entry preserves the totals-versus-dedup contract. |
| 47A.1 | Marker-aware directory skip | discharge | f2624fb shipped normalized marker prefixes, include-root preservation and tombstone suppression. |
| 47A.2 | Exclude pair-review state only | discharge | f2624fb excludes session.yaml while preserving prose artifacts. |
| 48A.1 | Scrub git subprocess environments | place | 53A; both history and remote subprocesses still inherit repository-redirection variables. |
| 49A.1 | Hoist the resume protocol | defer | New Future entry requires a measured shared filesystem seam; no renderer depends on it. |
| 49A.2 | Collapse readers into one per-turn adapter | kill | The common tuple discards cumulative Codex state required for cross-file accounting; Grok and Claude have different identities. |
| 49A.3a | Remove proven obsolete local reader helpers | place | 52A removes the test-only Terminal path alongside full-review H3; preserve production TurnState accounting. |
| 49A.3b | Share leaf coercions and add a boundary guard | defer | Retained with the measured resume seam; avoid policing an abstraction that has not earned its place. |
| 49A.3c | Bound interned model detail | defer | New Future entry preserves day totals and measures cardinality first; distinct from the states cap. |
| 50A.1 | One block, every agent | place | 55A; retains coverage, legacy-name tolerance and the verified pricing dependency on 54A. |
| 50A.2 | Everything else aggregates across models | place | 55A; part of the same renderer outcome, with no new summary-card row. |

**Future membership:** 70 existing deferred bullets retained verbatim; pricing promoted to 54A; the three entries below removed from the queue. Twelve deferred entries added (10 from the inbox, two scoped remnants of the old walker card), leaving 82. Refusals remain policy; removing their queue entries does not authorize them.

- **No tooling migration hidden inside a workspace fix:** keep the existing bin/check interface; do not infer a uv/Hatch/tox migration from hatchling being the build backend. Original refusal: [manual], 2026-09-01.
- **No collector-dependent similarity classifier/silent merge:** the v0.12.51 analysis cancelled this auto-resolver, and [the conflict invariants](invariants/conflicts.md#collector-removal-and-auto-resolver-cancellation) forbid resurrecting the collector. It is not waiting for a dataset.
- **No Codex/Grok sessions-snapshot:** local discovery is not permission to publish encoded cwd or transcripts. Claude's sessions snapshot stays Claude-only; reconsider only if a host supplies a metadata-only index. Original refusal: host-parity [manual], 2026-08-17. The roadmap's standing wire-privacy constraint remains in force.

**ID lineage:**

| Previous ID (2026-09-03 plan) | Disposition / new ID (2026-09-05 plan) |
|---|---|
| 45A: sidecar forensics | Shipped work recorded as 45A; new confirmed replacement defects → 48A; immediate-stat mitigation killed |
| 46A: cache encoding | Re-scoped shipped Grok repair recorded as 46A; original encoding/resume/cap work → Future |
| 47A: sync surface | Shipped as 47A |
| 48A: git environment | 53A |
| 49A: one walker, two adapters | Blanket adapter recipe killed; measured filesystem sharing/model bounds → Future; obsolete helpers → 52A |
| 50A: unified reporting | 55A |


Drained 2026-09-02 by Track 37A implementation: 5 discharged (release.yml guard,
width-coupled tests, xdist, CI isolation, bin/check — the six-Track split was
killed), 4 placed (36B amendments, unowned OpenCode files, 44A CLI verbs, 44A
retirement notice), 4 deferred (see docs/roadmap-future.md).


`/roadmap` drain, 38 items on 2026-09-01 (first drain since 2026-08-25; the
2026-08-30 Track 34A batch and the 2026-09-01 Track 35A batch had both gone
un-drained because the audit's `## UNPROCESSED` parser counts `[source:key=val]`
tags and this repo files `_Source: ..._` italics — it had been reporting
`ITEMS: 0` against 38 live bullets):

- **15 placed or applied.** Three new Tracks: 36A (remove OpenCode), 37A
  (workspace bootstrap), 42A (git-environment scrub, filed as S2 on 2026-08-25).
  Card amendments applied: `read-first: 34A` onto Track 38A, the struck
  "OpenCode must keep reporting an honest empty" instruction on Track 41A, the
  corrected `host_usage.py` line count on Track 40A (1,730 filed -> 2,617
  measured), and the deleted sidecar-forensics -> walker-substrate edge. Two new
  standing constraints: prove the counter schema of every reader you consume,
  and delete an unused feature rather than repairing it.
- **12 discharged** (already true at HEAD; the authored-false rate for this run
  is 12/27 = 44%). `git_capture` is read by the aggregator, twice-filed as E6.
  Groups 32 and 33 are already in shipped history, twice-filed. The Track 34A
  `blocked-by: 33A` edge no longer exists to re-derive, twice-filed. The counter-
  schema double-count, the `_tier()` pricing trap, the `PRICING_LAST_UPDATED`
  split, the today's-rates disclosure and both Track 35A card corrections all
  shipped in v0.12.52. Recapture rows already excluded from the zero-capture
  note. Both 2026-08-30 standing-constraint candidates were already in
  ROADMAP.md. Host per-model materialization is already written into Track 40A
  task 3.
- **9 deferred** to `docs/roadmap-future.md` with full context: the merged
  `~/.claude/projects` growth pair, `WALK_TIME_BUDGET_AUTOPUSH_MS`, N6
  subdirectory recovery, `mm diag` path reflow, the held xAI rate table, Grok's
  `costUsdTicks`, and stable `## Notes` codes. Three existing bullets were edited
  in place rather than duplicated (`_iter_jsonl` bounding, `--demo`,
  store-vs-binary skew).
- **2 killed.** "The retro aggregator reads synced event lines with no size
  bound" as filed named `aggregator._iter_event_objects`, a symbol that has
  **never existed** in this repo (`grep` 0 hits, `git log -S` 0 commits); the
  defect is real under the correct symbol `_iter_jsonl` and was merged into the
  existing Future bullet. "Grok is invisible for TWO independent reasons" was a
  coordination fact rather than work; one of its two halves was dissolved by
  removing OpenCode and the other is Track 38A, which names it.

Two items were resolved by user decision rather than by analysis: **mm supports
Claude Code, Codex and Grok Build; OpenCode is dropped** (2026-09-01). That
replaced the drafted OpenCode `$.id` fix with a removal, and dissolved one of the
two blockers on Track 35A's held xAI rates.

Track 28A `/autoplan` drain, 1 item on 2026-08-25:

- 1 discharged: "Retire the 0.12.42 policy-transition machinery" shipped inside
  Track 28A (v0.12.44). Evidence: `grep -rn "maybe_emit_policy_transition|
  declined_owned_link_rows|policy_transition_text|_POLICY_TRANSITION_MARKER|
  _join_display_names" src/ tests/` returns 0. Two corrections to the item as
  filed: its symbol list was incomplete (`_join_display_names` and 5 stale
  `__all__` entries were also dead), and its instruction to delete the README
  troubleshooting entry was **overruled** — `mm devices` shows 2 of 3 fleet
  machines on 0.12.13 and 0.12.34.1, neither of which ever ran a version that
  could emit the notice, so the README entry is the only explanation they will
  reach. Its stated rationale ("28A gives users a supported way to decline")
  was also wrong: 28A shipped no such command. The retirement was right for a
  different reason.
- 0 placed. 0 deferred. 0 killed.

Regen drain, 2026-08-25 — nothing from the inbox, which was already empty. Recorded
because the run's whole yield came from reconciling against git rather than from
filed items:

- 2 Tracks closed from ground truth: 25B shipped as v0.12.41 and 25C as v0.12.42,
  both still listed unshipped at HEAD three releases later. Group 25 → Shipped.
- 1 Group minted for unplanned shipped work: v0.12.43's Grok skill-discovery probe
  became Group 26, so the 27A kill has a visible cause.
- 1 Track killed: 27A (Grok row). v0.12.43 shipped the opposite conclusion plus a
  written exit criterion that refuses the row. Group 27 tombstoned.
- 1 item promoted from `docs/roadmap-future.md`: "Regenerate the roadmap AFTER a
  Track lands" → Track 28B. Sixth occurrence; the deferral reason ("a process
  convention, not a Track") is refuted by this repo's own PROGRESS-row history,
  where a convention line failed twice and a pytest fixed it.
- 2 of 3 leftover task premises on the old 26A had rotted and were rewritten with
  this-turn evidence rather than re-emitted. 0 discharged.

Track 25B `/autoplan` drain, 5 items on 2026-08-22:

- 1 placed: `mm uninstall-skills` became **Track 26A** (new Group 26, between
  Install consent and the Grok row). Placed rather than deferred because the
  installer's `absent target -> symlink -> installed` branch re-creates a
  manually deleted link on the next interactive push, so there is currently no
  supported way to decline the skill — and shipping the Grok row first would
  orphan a fourth link on every uninstall.
- 4 deferred to `docs/roadmap-future.md`: the `mm skill-run --protocol N`
  handshake, the `mm status` store-vs-binary skew nag, the README agent-name
  doc-lint, and the process fix for regenerating the roadmap after a Track
  lands rather than only before.
- 0 killed. 0 discharged.

Drain record, 7 items from the 2026-08-18 Track 23A pass:

- 1 placed: the `## Trends vs last retro` bug became Track 24B. Its revised
  deterministic prior-period design is in flight in PR #138; it removes the
  save/compare circularity and machine-local snapshot baseline.
- 1 discharged: the `mm status` agent-coverage row was absorbed into Track 25A
  (2026-08-21 regen: the one-line nag now lives on Track 24A with the store).
- 5 deferred to `docs/roadmap-future.md`: demo/fixture path,
  `--dump-host-usage` rename, bare-integer retro window, retired-device pruning,
  and reset-aware snapshot deltas.
- 0 killed.

Track 24B drain, 3 items on 2026-08-22:

- 2 deferred to `docs/roadmap-future.md`: machine-readable retro export and a
  bounded binary `_iter_jsonl` reader.
- 1 killed: `--no-trends` is an explicit non-goal; empty current windows already
  suppress the section, and otherwise the trend table is intentional output.

Host-parity inbox drained 2026-08-17: Grok allowlist shipped in Track 22B;
Codex/Grok sessions-snapshot refuse → Future. The Grok skill-link item routed to
Track 23B, which was dissolved on 2026-08-20 after failing its `/autoplan`
premise gate; 2026-08-21 regen places it as Track 27A behind Groups 24-26
(Approach B deleted Group 29).

Track 25A `/autoplan` drain, 1 item on 2026-08-22:

- 1 deferred to `docs/roadmap-future.md`: unify the seven per-agent enumerations
  across five modules. The `/autoplan` run also falsified Track 25A's premise
  (pytest never writes the real `~/.grok/skills`; the defect is silent `zip()`
  truncation), so 25A was retitled and re-scoped, Group 24 moved to Shipped, and
  the packer re-roomed the old 26A with 25A as Track 25B.
- 0 placed from the inbox: `## Unprocessed` was already empty.

_Last updated 2026-10-06 by /roadmap: eight inbox items drained; approved 69A/69B deliverables archived; S13 remains required and unpassed. Prior drain records are historical._
