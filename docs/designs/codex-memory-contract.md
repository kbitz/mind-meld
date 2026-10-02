# Codex memory source contract

Contract version 1 · Track 68A · measured 2026-10-01 · **NO QUALIFIED ROUTE**.
Qualification owner: Track 68A implementer. Karl owns genuine user enrollment.
This contract covers the exact measured builds below. Protocol evidence from
the tests-only model does not qualify native recall or shipped transport.

<a id="start-here"></a>
## Start here

Use an existing Mind Meld checkout on macOS with Python 3.11+ on PATH. The
existing `bin/check` bootstraps its environment; a scoped run still lints the
whole repository. Replace `/path/to/mind-meld` with your checkout's absolute
path, then run this deterministic recipe:

```bash
"/path/to/mind-meld/bin/check" tests/test_memory_contract.py tests/test_docs_routing.py
```

The measured command receipt and onboarding clock appear in
[Verification](#verification). A green portable run establishes these proofs:

| Series | Observable proof | Limit |
|---|---|---|
| F1–F4 | Accepted retirement excludes roots/descendants after restart, omission and stale arrival; incomplete controls cannot serve | Reference protocol, no native withdrawal proof |
| E1–E4 | Transit preserves origin; paraphrases depend on ancestry or export is blocked before delivery; genuine authorized feedback has a separate route | Synthetic authority and ancestry, no native extraction proof |
| H1–H4 | Bodies cannot author controls; actual serialized agent bytes and escaped, gutter-wrapped terminal output have separate assertions | Framing cannot establish model obedience |

Read the [full design](memory-continuity.md), relevant invariants and the
[Track 68A card](../ROADMAP.md#track-68a-qualify-codex-memory-portability-and-recall)
before changing code. The scoped command above is the Track 68A gate; the
review stage also ran the full `./bin/check` (see Verification), which is not a
release claim. Native experiments first require physical exclusion, supported
credentials with observed cleanup, loaded-context observation, actual isolated
Conductor binding and qualified outside-agent control authority. Their outcomes
are independent of this command.

<a id="capability-ledger"></a>
## Versioned capability ledger

Support axis: **documented**, **observed**, **unsupported**, **unproven**.
Trial axis: **PASS**, **FAIL**, **INCONCLUSIVE**, with a closed reason below.
An unsupported prerequisite leaves dependent trials INCONCLUSIVE; it supplies
no native FAIL or PASS. Evidence class is separately `preflight`, `protocol` or
`live`. An enabled feature, readiness count or correct answer is insufficient.

| Measurement | Exact observation / support | Consequence |
|---|---|---|
| CLI | `codex-cli 0.159.2`; executable SHA-256 `16593cc2f422d5f398a8e40f550ebbaf1245392528957be342c295920a300704` | Exact build only |
| Actual Conductor app-server | Running process ancestry reaches Conductor; executable resolves to the same 0.159.2 binary; isolated `initialize` reports 0.159.2 | Direct app-server probe is not an isolated Conductor integration |
| Conductor | 0.89.2, local macOS; device short ID `889e42c0`; UTC date 2026-10-01 | No second-Mac evidence |
| Other daemon | A separate 0.159.3 app-server daemon exists | Excluded from this cohort; never substitute its schema |
| Memory feature | Observed `memories stable true`; external-agent memory import under development and disabled | Feature labels are candidates, not import qualification |
| Capture/use | Isolated `config/read` accepts `generate_memories=false`, `use_memories=false` independently; production feature read true, overrides absent | On/on, off/on, on/off behavioral effects unproven; production settings unchanged |
| Eligibility | Official defaults six idle hours and 25 percent remaining quota; installed config read returns null for both omitted thresholds | Runtime eligibility/default resolution unproven; no seed or eligible observation window |
| Trigger | Official asynchronous background generation and startup scan settings | Actual extraction/consolidation trigger and delay unobserved |
| Empty initialized stores | `memories_1.sqlite`, `memories_v2_1.sqlite`, `state_5.sqlite`; `memory/status`: zero v2 consolidated threads, readiness false | Initialization does not demonstrate extraction, consolidation or recall |
| Source scope/consent | Measured `threads` DDL contains `project_id`, `cwd`, `memory_mode`; extraction DDL has `thread_id`, source/update/selection timestamps | Joining project/consent across stores and files is unproven; no prose inference |
| IDs/ancestry/retirement | Extraction primary key is thread ID; no ancestry/retirement column in this measured table | No durable export identity selected; other surfaces remain unproven |
| Native same-agent import | Public import documents Claude/Cursor; schema has `externalAgentConfig/import`, `MEMORY` item and arbitrary source selector | Same-agent export/import, populated-store survival and withdrawal unproven; no credentialed import attempted |
| Loaded context | Schema provides thread reads/events and aggregate memory readiness; no delivery observation was qualified | Answer-only results cannot qualify any recall arm |
| Isolated CLI home | Credential-free initialization reported the verified disposable home; account null, auth required; no auth file created | Structural preflight observed, credentialed lifecycle unsupported in current authorization |
| Actual isolated Conductor binding | Bundled skill provides no proven per-workspace memory-home binding; public settings/agent docs returned HTTP 403 | Unproven; stop dependent Conductor trials, preserve their gate |
| User authority | No enrolled, target-bound outside-agent channel exists in this workspace | The model treats any channel name other than its test-user value as non-authority; no live channel was tested; promotion blocked |
| mm consumer | Tests-only `MemoryAdapter`, JSON delivery frame and multiline renderer exist | No real startup carrier, host delivery or user-authority qualification; no automatic fallback selection |

Schema fixture: [tests/fixtures/host_memories/codex/](../../tests/fixtures/host_memories/codex/),
exact CLI/app-server 0.159.2, Conductor 0.89.2. `schema.ddl` SHA-256:
`686274cd5a1e7c0cb4ba34883903071f53711085aeb5566fca09274213eafdd8`.
The measured DDL, merged from the three store files above, includes three
memory tables and the thread table, including
its references to unexported native tables. Tests create owned SQLite stores
from it, insert fixed allowlisted synthetic rows, and read metadata only.
Synthetic `status`/`kind`/IDs are placeholders, not observed native enum values.
No SQLite/WAL/SHM binary, nested Git, private prose, path, email or credential
is a fixture. The fixture/ledger validator rejects unknown fields and values.

<a id="preflight"></a>
## Preflight and isolation recipe

The initial capability preflight used the approved 45–90 minute budget. It
closed its prerequisite investigation within that budget; coding can continue
independently. No eligible learning or recall campaign began. The 72-hour
campaign budget is not falsely reported exhausted.

| Prerequisite | Owner / required evidence | Missing prerequisite cancels |
|---|---|---|
| Actual context observation | Implementer: supported loaded/retrieved context channel, tested without marker leakage | Dependent recall/withdrawal/model-behavior evaluations |
| Physical home and credentials | Implementer; Karl approves any new login: installed inventory, home binding, login write locations and verified cleanup | Credentialed CLI and dependent lifecycle/import trials |
| Actual Conductor binding | Implementer/Conductor supported surface: distinct verified home and observation | Conductor trials only; CLI cannot fill them |
| User authority | Karl and implementer: identity/action/target-bound evidence outside agent tools | Enrollment/retirement/explicit capture and all production promotion |

Raw root handle `root-q1` was checked outside all configured sources, all six
installed default roots, the disabled legacy OpenCode root, storage, Mobile
Documents, Desktop and Documents (both conservatively excluded regardless of
iCloud preference). The checker now also excludes `~/Library/CloudStorage`
(third-party sync providers); that 2026-10-01 receipt predates this and the
other 2026-10-02 hardening and was not rerun. Absent roots remain in the
inventory. The installed
`config.py` provenance SHA at measurement was
`86dc34566c8f3b03c058a4ab8b32d5a22f9308c11ebc3f3b0548cc37fcccc831`.
Configured-source resolution never calls `mm sources`, which hides absent
roots. It reads every supported config shape: explicit `[[sync.sources]]`, a
legacy `[sync].claude_dir` and a defaults-only config. Physical comparisons use deepest existing ancestors, `samefile` and
case- and Unicode-normalization-folded absent suffixes in both containment
directions. A raw root cannot contain a present or future sync child, and can
never be the owned parent itself under any alias. Relative paths, `..` parts,
a root that is not a directory, an owned parent that is a symlink rather than a
real directory, any object on another device than the owned parent (a mount
inside it), any directory that cannot be listed, symlinks, extra hard links,
non-regular files, ambiguous identity, missing inventory, unknown
disabled-source paths, changed roots and bad modes refuse.

mm's own self-update is a drift event for this receipt. Since 1.3.0,
`[upgrade] auto_install` (default on) lets push, pull and the hooks replace the
installed package in place. On 2026-10-02 the installed mm on device
`889e42c0` was 1.3.0, and the provenance SHA above no longer matched it. Set
`auto_install = false` for a campaign, or treat any update as a changed
inventory. Provenance records the installed package version, the installed
`config.py` and the user's `config.toml`; the checker re-reads all three on
every run, so a change to any of them refuses a pinned receipt.

The tests-only read-only checker requires both values shown below. These
environment values authorize a metadata check, not enrollment or retirement.
Setting only one of them, or any other `MM_QUAL_*` value without both, prints
`UNSAFE` instead of skipping, so a half-typed opt-in never passes silently.
`MM_QUAL_ROOT` must name a directory under `~/scratch`, the checker's owned
parent. Replace `/path/to/your-home` with your home directory. Run the check
before creation, create the empty 0700 root, then run it again: its `SAFE`
line prints `receipt=<digest>`. An absent root prints `receipt=unpinned` and
can never be pinned. Prefix every later check (before each launch, raw
operator write and cleanup) with `MM_QUAL_EXPECT=<digest>`; a changed root
identity or inventory then refuses. Keep receipts locally outside sync.

```bash
MM_QUAL_LIVE=read-only MM_QUAL_ROOT="/path/to/your-home/scratch/track68a-qualification" "/path/to/mind-meld/bin/check" --serial tests/test_memory_contract.py -- -k I2_live_root_check -s
```

Expected diagnostic: `SAFE root-q1 receipt=<digest>` or `UNSAFE unsafe-root`,
linked to this section. Default portable checks never read real roots or
launch a native host. The process HOME and existing Keychain/cache guards stay
in place. Synthetic alias tests do not claim a live firmlink or case-alias
observation.

Create only owned 0700 directories and 0600 files under a SAFE root. Write a
minimal credential-free config by hand; never copy production auth/config.
The structural probe used file credential storage and disabled analytics,
capture and use. It launched only initialization and read requests: no thread,
seed, model turn, login, native reset, table patch or fabricated chat.

Native initialization created `installation_id` at 0644 despite umask 077.
The private containing root remained 0700. The process was closed and this
owned file tightened to 0600; that repair does not qualify autonomous native
writer modes. A credentialed arm still needs a supported boundary that holds
the required modes and physical home. Periodic checks do not interpose on
autonomous writes or defeat concurrent same-user path rewiring.

Credential mechanisms documented by OpenAI include file, keyring, auto and
ephemeral storage. This run exercised none with credentials. Future login must
first record actual write locations, refresh behavior, Keychain effects and
owned cleanup verification, with separate explicit approval. No credentialed
recipe is runnable from this contract yet. No production credentials were read
or borrowed. A separate Mac account likewise requires separate approval.

Cleanup requires the pinned receipt, closes the owned process, rechecks
inventory, ownership, object modes and pinned identities, then removes only the
objects that verification pinned,
walking pinned directory descriptors with `O_NOFOLLOW` rather than paths. An
object created after verification is refused, never deleted, and the root
stays. This is a closed-process guard, not a sandbox against a concurrent
same-user writer. The test helper `cleanup_owned` proves refusal with
disposable trees. No committed live cleanup entry exists: the 2026-10-01
cleanup recorded below used uncommitted operator steps, and a later campaign
must add a reviewed opt-in entry, gated like the root check and including the
`auth.json` and closed-process checks, before deleting a real home.

<a id="trial-matrix"></a>
## Frozen live matrix and clocks

R is a harmless actual Mind Meld fact not derivable from repository code, docs,
history or unrelated baseline instructions. C is separate new authorized
feedback. Obtain these from the attended operator after prerequisites pass.
Audit R/C absence in fresh prompts, checkout and instruction carriers without
committing either marker. Learning conversations and resulting native artifacts
may contain them. Memory-absent homes remain unseeded.

After prerequisites qualify, seed R separately in each positive home through
qualified capture-enabled controls, then observe eligible extraction and
consolidation before switching to that arm's capture/use setting. Learn C in
a different conversation under the tested setting. Never copy seed state into
an absent home. Capture-disabled arms still need their independently prepared
R state; a fresh answer cannot stand in for that preparation.

| Mode (each CLI and Conductor) | Capture | Use | New C extraction | Existing eligible R delivery | Positive / absent repeats |
|---|---|---|---|---|---|
| both on | on | on | expected | expected / absent zero | 3 / 3 |
| capture off | off | on | none | expected / absent zero | 3 / 3 |
| use off | on | off | may extract | zero / zero | 3 / 3 |
| both off | off | off | none | zero / zero | 3 / 3 |

The fixture freezes 16 arms, eight distinct positive homes, eight unseeded
absent homes, eight R learning conversations and eight separate C learning
conversations. Absent homes learn neither marker, so new C extraction is
expected only in capture-on positive homes; a ledger row that records seed,
extraction or consolidation for an absent arm is refused. The structural
preflight probe is its own `preflight` row, never one of the 48 recall trials.
Three independent fresh sessions can share consolidated state
within one setting; incompatible settings never share capture state. This is
48 base recall trials. **Executed: 0/48; observed delivery: unavailable**, not
zero delivered in 48 completed sessions. CLI arms carry credential-unsupported;
Conductor arms also require supported binding/observation. All remain
INCONCLUSIVE, with support separately unproven/unsupported as above.

Additional inventory per mode: three independent sessions at each of seven
stages (populated local baseline, supported import, regeneration, retirement,
recall-only re-extraction, restart, path/workspace replacement): 21 per mode,
42 further sessions. Also two receiving-store local-learning conversations,
two imports, two regenerations, two withdrawals, two restarts and two supported
path replacements across the campaign. All are pending prerequisites; native
hostile-body behavior observations share those receiving sessions but retain
separate receipts. Their protocol equivalents do not count as native trials.

Record seed, verified idle/quota eligibility, extraction, consolidation,
delivery and answer separately. Start the 30-minute observation only after
verified eligibility; no forced timestamps or guaranteed scheduler deadline.
Every PASS or FAIL row, absent arms included, needs verified eligibility and
its timestamp, because the observation window opens there. An absent-arm PASS
also records a wrong answer: a correct answer from an unseeded home means R
reached it some other way. A drifted row (`INCONCLUSIVE`, `build-changed`)
records whatever build it drifted to, and a CLI row never depends on the
Conductor build.
Use-on positives need 3/3 observed R delivery; absent, retired and use-off
arms need 0/3 observed delivery with a working observation channel. Check local
and imported facts after populated import/regeneration. Unknown eligibility,
unfinished consolidation and unavailable observation are INCONCLUSIVE. At 72
calendar hours unfinished arms become budget-exhausted, preserving completed
cohorts. Active operator time and scheduler waiting are separate receipts.

Prospective schedule: first 45–90 minutes for prerequisites; then eligible
R preparation in eight homes and separate C learning in the eight tested
settings, with independent T2 work during scheduler waits. Six hours is a
documented eligibility candidate, not a consolidation deadline. Each verified
eligibility timestamp opens only its 30-minute observation window. Three fresh
recalls share qualified consolidated state within each arm; run the 48 base
trials and 42 lifecycle sessions within the 72-hour calendar campaign when
possible. Stop unfinished arms honestly at the boundary. Actual attended
operator time and scheduler waiting for this run: zero; no seeding began.

<a id="protocol"></a>
## Protocol semantics and conformance

The adapter boundary is `project_id`, opaque stable `root_id`, immutable
`revision`, declared ancestry, validated control version and target-bound
authenticated-authority evidence references. The adapter is unit-agnostic: F2
exercises both a fact-level and a whole-project export unit through it, and
retirement acts on whichever unit the source can identify. No native column is
a transport API. The device registry maps a fresh device ID to an existing
durable origin, never shadows an origin ID and is never re-pointed; aliases map
to the same canonical project for records, controls and inventories alike. Retirement carries across both (F1, C1) rather
than minting fresh roots from a device registration or cwd. Production
persistence of that identity mapping still needs qualification (Q17).

Ordinary updates explicitly supersede revisions; concurrent corrections stay
visible together. A revision cannot supersede itself and supersession never
cycles. A revision is immutable: new bytes under an accepted revision are
refused, and a revision history (the hash of the whole row, its root and its
supersession) outlives payload GC, so a GC'd original that resurfaces is still
superseded and still cannot change. An export is what a cold peer can accept
and verify: every exported root declares its whole known lineage across the
revisions exported with it, every ancestor and supersession target it names
travels in the same export, and a superseded revision travels only with a
superseder (so a resurfaced original never re-exports without its
correction). A later revision may omit ancestry while the revision that
declared it is still exported; when publishing would lose a known ancestry or
supersession claim (after payload GC removed the declaring revision, say),
automatic export is blocked persistently. Accepted retirement
permanently excludes every revision and known descendant, before indexing and
again at retrieval, export and agent delivery. Omitted retirement entries,
clocks, regeneration, payload GC and re-enrollment cannot erase it; no TTL or
identifier reuse applies. The tests' single JSON snapshot is a local semantic
transaction using `fsutil.atomic_write_bytes(mode=0o600, fsync=True)`, not a
production wire/storage choice. Payloads, revision history, controls, lineage,
availability, cohort completeness, unresolved controls, last exports and
ancestry blocks persist together; the recall cache is rebuilt after every
publication and on restart, and recall and export hand out copies, never the
cache itself. One writer per store is assumed: a generation counter is checked
before every write and every read path, so a stale instance serves and writes
nothing instead of replacing newer state. That is detection, not locking (see
known model limits). Post-rename failure makes the instance uncertain: it
serves nothing and writes nothing more, so stale memory can never overwrite
newer persisted bytes, until a reopen validates the actual bytes. Reopen
cross-checks every index (records against their keys and revision history,
including root and supersession; staged rows against their keys; exports
against the revision history's hashes; declared ancestry against lineage;
accepted controls against their keys, retirement and authority) and refuses
incoherent state.
The real `atomic_write_bytes` docstring still promises an untouched target on
any failure; C2 shows a parent-fsync failure after rename leaves the new bytes,
and that discrepancy is filed in `docs/TODOS.md` rather than changed here.

Completeness requires a validated cohort inventory certified for that exact
project and all its required controls, for cold and reinstalled receivers and
with a declared cohort member that never pushed. Another project's certificate never unlocks
a view. Partial deliveries from different cohort origins merge until the
inventory completes. Both arrival orders and interruption leave incomplete
views unavailable. It proves the declared cohort, never every offline peer's
latest decision. Control identities are per project, so another project's
control with the same name never completes this one. A control batch is
validated as a whole, in any order: every valid, authorized retirement in it
is kept even when another member is malformed or refused. A validated A control
targeting B, or one without enrolled authority, is refused while valid A stays
live, as is a validated B control delivered on A's channel. An unsupported
control closes its validated scope before any other field of its unknown schema
(its target included) is read; invalid scope or a malformed member closes all
enrolled views. Both stay unresolved control state (`control-unresolved`)
across later updates. A supported control with the same identity resolves
scoped state; whole-host state, whose real scope is unknown, clears only
through the enrolled user's local remedy. The remedy never retires anything,
canonicalizes its scope and refuses to report clearing a control that was not
unresolved. Unresolved controls count against the admission budget, a missing
or malformed control ID as its own identity. A forged record claim (origin, ancestry,
revision, authority reference) is refused without closing unrelated recall;
only an unsupported record schema closes the view. Authority and completeness
are distinct trusted inputs: user-authority evidence enters through a separate
assumed-trusted enrollment channel, never a payload, and its cross-device
transport is unmodeled (Q7).

Ancestry-preserving paraphrases remain dependent. Without qualified ancestry,
persist project or whole-host automatic-export suppression **before** foreign
delivery. It survives restart/schema loss, later acceptance never republishes
the export, and the last accepted export and unrelated sources are preserved.
The adapter's `foreign_delivery` only ever frames the stored bytes of a
record it currently serves, never the caller's row; any other delivery path
must route through it (see known model limits). Qualified explicit new
feedback is a separate capture route: it must name a root this store has never
seen and declare no ancestry. The public receive path refuses feedback rows,
and automatic export never carries them, so feedback leaves only through the
explicit route. Hash equality or semantic similarity cannot establish origin. Absence, unreadability, disabled
selection and empty consolidation author no retirement and publish no
synthetic empty replacement: `export()` returns "nothing publishable" while a
view is closed or blocked. Unknown schema (including a census artifact with
an unexpected key set), lost ancestry and changed output block automatic export
persistently; a transient unreadable or unstable census only closes the view
until a later complete, coherent acceptance. Aliases resolve in every adapter
entry point that takes a project (the local remedy and the explicit export
included), never re-point to another project and never shadow a real one.

Prototype budgets: 2000 total payload/control records, 64 immediate ancestry
references, depth 32, 4096 metadata bytes per record, 10 MiB per census, five
seconds and two attempts. Record count (revision history, every project's
staged rows, incoming rows, and accepted, unresolved and incoming controls) and
bytes (incoming plus every project's staged bytes) are store-wide and enforced
at admission, before any partial delivery is staged or any control applied.
Metadata bytes and ancestry width are enforced per record while staging (valid
controls in the same batch are kept), lineage width and depth when the
candidate is assembled, and the five seconds and two attempts in the census.
The census deadline also interrupts a running query, and more than one job
watermark refuses rather than picking one.
Iterative visited traversal reports deterministic work counts. Budget refusal closes the affected view/export
without truncating retirement or replacing accepted exports. Because
retirements are permanent and the record budget counts every project's records
and controls, a fixed budget gives a store a finite lifetime: transport must
give permanent retirement its own storage class or compaction rule (Q6). These
are adjustable research budgets, not frozen production limits or fleet
performance claims.

Agent delivery is one bounded JSON data object with allowlisted project and
eight-hex origin labels, escaped payload, explicit UTF-8 byte truncation and
no grants. A body cannot close it or supply metadata. Terminal display prints
trusted labels for metadata, escapes every non-printable, combining (including
variation selectors), separator other than the ASCII space and blank-letter
character to visible notation (never strips it, unlike `safe_terminal_str`, so
a human sees every character the agent receives), wraps each line itself on
the rendered width of the whole line and puts a trusted `  | ` gutter on every
visual payload line, and prints literal `Text`. A body line therefore never
renders as a metadata line at the rendering width; that width must be the
terminal's actual display width (see known model limits). ESC/C1, OSC/CSI
bodies, CR/BEL, bidi/format characters, invisible and combining characters
and Rich lookalikes have separate output assertions at widths 40, 80 and 120.
Published recipes are printable ASCII only.
Serialized framing proves structure, not model obedience. Raw hostile bodies
are constructed only in Python tests.

| Deliberate mutant | Conformance case that must fail |
|---|---|
| mtime-wins | `test_F1_offline_resurrection_orders_clocks_restart` |
| hash-only-identity | `test_F2_explicit_correction_concurrent_regeneration_then_retirement` |
| absence-as-retirement | `test_F4_unknown_absence_preserves_export_and_gc_retains_controls` |
| body-authority | `test_H1_body_derived_summary_and_agent_invocation_author_nothing` |
| early-serving | `test_F3_partial_acceptance_cached_view_is_unavailable_after_restart` |
| retirement-on-omission | `test_F1_manifest_omission_registration_and_alias_keep_retirement` |
| block-in-memory | `test_E2_export_block_persists_before_foreign_delivery_and_schema_loss` |
| republish-while-blocked | `test_E2_acceptance_while_blocked_keeps_the_last_published_export` |

The registry/case map is machine-checked. M1 runs each real conformance test
unchanged against the reference, where it passes, and against its mutant,
where it must fail on that group's own hinted assertion. The fault survives
reopen. No xfail or caught arbitrary exception supplies credit. The cases read
the reference model's state directly and use its assumed-trusted channels, so
the transport/bridge follow-ons must re-express them against a public
conformance interface for shipped adapters (Q11); they cannot be re-bound as is.

<a id="reasons"></a>
## Outcome reasons and safe next actions

Each reason is a problem/cause-or-uncertainty/gate/action/owner tuple. None is
an exit-code change or permission for an agent to enroll, log in or retry capture.

| Reason | Problem and established cause / uncertainty | Affected gates | Safe next action | Owner |
|---|---|---|---|---|
| eligibility-unknown | Idle/quota/trigger eligibility not established | lifecycle, recall | Observe eligibility using a supported channel; leave window unopened | implementer |
| no-consolidation | Eligible input has no completed consolidation | recall, import/regeneration | Record scheduler delay; supported attended later observation | implementer |
| unsupported-surface | Required source/consumer/control surface not qualified | dependent host/control gates | Research supported surface; retain no-route leaf | implementer |
| observation-unavailable | Loaded/retrieved context unavailable; answer insufficient | recall, withdrawal, model behavior | Qualify actual observation, keep answer separate | implementer |
| credential-unsupported | Approved isolated login/cleanup mechanism unavailable | credentialed native arms | Karl approves separate setup after locations/effects are established | Karl / implementer |
| unsafe-root | Inventory/identity/containment/ownership/modes ambiguous or unsafe | raw writes, native launch, cleanup | Select and prove another owned root or repair owned modes while closed | implementer |
| build-changed | Build/schema/control/carrier changed during observation | dependent cohort only | Record before/after and targeted requalification | implementer |
| budget-exhausted | 72-hour campaign ended before observation completed | unfinished live arms | Preserve INCONCLUSIVE receipts; separately choose future campaign | Karl / implementer |
| unstable-source | Descriptors or demonstrated source/job revisions disagree | scope, consent, recall census | Two short bounded attempts; qualified join before retry | implementer |
| scan-limit | Count/bytes/lineage/deadline exceeds prototype budget | affected view/export | Preserve accepted evidence; examine supported bounded approach | implementer |
| control-incomplete | Declared cohort controls not coherently accepted | recall, withdrawal | Complete validated inventory before serving; never infer empty controls | transport owner |
| control-unresolved | An unsupported, unscoped or malformed control is unresolved control state | the affected view, or every view for invalid scope | Deliver a supported control with the same identity, or have the enrolled user clear it through the local remedy; never retire a root just to clear it | Karl / transport owner |
| publication-unavailable | Write/durability failure may occur after rename | accepted view and cached delivery | Close view, reopen actual coherent bytes and validate controls | implementer / transport owner |

Reason codes are closed machine values; this table is their remedy, and the
suite checks that every code has a row. The only printed diagnostic, the
opt-in root check, links to [preflight](#preflight); F/E/H assertion messages
link to existing design anchors. Ledger values are closed: versions, opaque
handles, known owners, synthetic source/scope references, settings, lifecycle
fields, counts and tri-state outcome. Lifecycle chronology follows the stage
order, so a receipt survives canonical JSON serialization. A PASS or FAIL row
must carry the exact measured builds, observed support, a known source, a live
evidence handle and exactly one observed session; a positive arm also needs
seed, eligibility, extraction and consolidation timestamps, and delivery must
fall inside the 30-minute window after eligibility. The excluded 0.159.3 daemon
or a mixed cohort never promotes. Null observations
explicitly mean unobserved. Unknown fields, empty/null maps, duplicate or
unplanned trial IDs, invalid counts and unsafe values reject.

<a id="decision"></a>
## Integration decision and promotion blockers

Prefer native only with proven export, import, scope, consent, withdrawal,
ancestry, actual CLI/Conductor delivery, isolation, control completeness and
user authority, plus a complete 48-trial native cohort and a lifecycle in
which the retired root is observed delivered after import and absent at every
stage after retirement. Otherwise independently evaluate mm delivery,
withdrawal, controls, ancestry-block behavior and explicit capture against the
mm route's own delivery cohort and withdrawal lifecycle; native evidence never
fills it.
This run qualifies neither: **NO QUALIFIED ROUTE**. No failed native gate is
waived by the reference model.

Receiving consumer today: only the disposable tests-only `MemoryAdapter`.
Candidate mm footprint: leaf export/recall consumers, narrow CLI/status/diag
hooks, persistent project/origin identity and permanent retirement storage,
plus a static startup retrieval instruction through the user-approved shared
agent-config carrier. They are follow-on designs, not new modules/commands here.
The carrier must contain no retrieved text, generated views, credentials,
enrollment, project IDs or machine-local paths; absence returns an honest
unavailable result that cannot grant agent enrollment. Real CLI/Conductor
delivery and authority still require observed qualification.

No released mm version currently honors this proposed retirement protocol.
Minimum eligible peer version is **undetermined**, not 1.3.0 by implication.
The transport owner must classify compatibility, reserve a newer-format refusal
gate if needed, and bound enrollment including registered peers that never
pushed; `last_seen_version` alone cannot establish their participation.
Membership under a fleet AES key is not authenticated user authority.

Every change to CLI/app-server/Conductor build, DDL/schema, capture/use controls,
import API, consolidation, delivery carrier or ancestry behavior invalidates
only dependent cohorts until targeted requalification. The CLI and the
Conductor app-server resolve to one binary, so drift in either invalidates
both modes; only Conductor rows depend on the Conductor build. Compare before/after
each trial and retain independent evidence. No wall-clock CI expiry or broader
version range is inferred. Startup text pinning, ciphertext-only transport,
source consent/preview/unattended failure breadcrumbs, MEMORY.md merger
exclusion and raw Claude sync coexistence remain mandatory follow-on gates.

<a id="known-limits"></a>
## Known model limits

The 2026-10-02 review recorded these as obligations for the follow-ons rather
than fixing them in the tests-only model. Each one stays a promotion blocker
until the named owner resolves it.

| Limit | Consequence today | Owner |
|---|---|---|
| Root identity is `project:root`; the first origin to arrive owns it, and any cohort origin can claim a fresh root | A peer's same-named unit or a squatted root ID is refused instead of coexisting; the design scopes identity by project, originating device and native source ID | transport identity owner (Q8, Q17) |
| Ledger rows carry no route, session, home or per-trial evidence identity | Separate native and mm cohorts, three independent sessions and distinct homes cannot be verified from receipts; the caller keeps cohorts apart by position | qualification implementer |
| Only `foreign_delivery` applies the served-record check and the pre-delivery ancestry block; `agent_frame` is a free function and `recall` alone persists no block | A startup carrier that pairs recall with framing bypasses the block; every real delivery path must route through the checked entry point | mm route / shared agent-config owner (Q14, Q15) |
| macOS ACL grants are not inspected (`S_IMODE` ignores them); an inventory root spelled through a firmlink and still absent compares lexically | A 0600 file readable through an ACL passes; an absent root under `/System/Volumes/Data` is caught only after it exists, by the next check | qualification implementer |
| No committed live cleanup entry | The 2026-10-01 cleanup used uncommitted steps; a later campaign needs a reviewed, opted-in entry before deleting a real home | qualification implementer |
| Any partial delivery starts an acceptance transaction and closes the view until it completes; a rejected control batch leaves an already-live view live | A peer can hold a view closed by repeatedly sending incomplete deliveries; recorded with the unsupported-control denial question | Karl / authority design (Q7) |
| Agent channels are modeled as a non-user authority value | H1 shows channel names author nothing, not that a real agent cannot reach the user channel | Karl / authority design (Q7) |
| An export block is never lifted, and block states have no reason code of their own | A project blocked by lost ancestry, a GC'd supersession target or an unqualified delivery publishes nothing automatically for good; operators see only `export()` returning nothing | transport (Q5, Q9) |
| The revision hash covers the whole row, observation fields (`observed_date`, `evidence_ref`, `metadata_ref`) included | Re-observing the same revision with a new observation date is refused as new bytes; a production adapter must hash content and identity apart from observation metadata | transport |
| The ledger never checks `expected_capture`: rows carry no capture observation | A trial cannot show that C was (or was not) extracted; the capture gate stays a separate live receipt | qualification implementer |
| Whole-project regeneration is untested: F2 retires a whole-project unit but never regenerates one | A regenerated whole-project export under a new root would mint fresh identity; the design's unit mapping must be qualified per source | qualification implementer (Q1) |
| Payload GC publishes an empty export while the view stays live | Between GC and the next acceptance, peers see an empty export, not "nothing publishable" | transport (Q6) |
| The generation check is detection, not locking | Two writers that both read the same generation can race between the check and the rename; one writer per store remains an assumption | transport (Q4) |
| `schema.ddl` merges the tables of the three measured store files into one fixture, though its pinned header comment says "store" | The census exercises an owned join, not a single native store's shape; the comment stays because the fixture hash is pinned evidence | qualification implementer (Q12) |
| Terminal wrapping trusts the width it is given | Rendering at a width larger than the terminal's real width lets the terminal re-wrap a payload line without the gutter | receiving consumer (Q15) |

<a id="open-questions"></a>
## Q1–Q18 ownership and remaining evidence

All original questions remain in [the binding design](memory-continuity.md#decisions-to-resolve-before-implementation)
and [its further questions](memory-continuity.md#further-questions-retained-for-qualification-and-later-packages).
This table assigns work, without claiming a prototype settled a native gate.

| Question | Finding / precise remaining obligation | Owner |
|---|---|---|
| Q1 | Native export/import/regeneration/withdrawal unproven | qualification implementer |
| Q2 | Measured scope/consent columns; consistent eligible project join unproven | qualification implementer |
| Q3 | Neither native nor real mm consumer qualifies | qualification implementer |
| Q4 | Atomic semantic snapshot/recovery built; encrypted production encoding and installation undecided | transport |
| Q5 | Both ancestry modes exercised synthetically; native ancestry and explicit capture route unproven | qualification / transport |
| Q6 | Model retains retirement on omission/GC and gates cold cohort; permanent storage class or compaction (a fixed budget plus permanent retirement gives a finite lifetime), completeness/checkpoint and encrypted encoding still required | transport |
| Q7 | No live outside-agent authority; the model takes user-authority evidence and the unresolved-control remedy through assumed-trusted local channels whose cross-device transport is unmodeled; successor/restoration, compromised-peer denial (unsupported controls and incomplete deliveries can hold views closed) and author authentication unresolved | Karl / authority design / transport |
| Q8 | Minimal stable project identity must precede transport; Claude association must pass parity before bridge | transport / parity |
| Q9 | Pre-delivery project/host blocks persist in model; qualified capture fallback must be sized/implemented before B-to-A acceptance can pass | transport |
| Q10 | Frozen three-repeat matrix, answer/delivery separation; F1 and F2 run older/equal/newer/far-future clocks; actual observation and 48 trials unavailable | qualification implementer |
| Q11 | Eight mutants each fail their mapped real F, E or H conformance test (F1–F4, E2, H1); E1, E3, E4 and H2–H4 have no bound mutant yet. The cases read reference-model state and assumed channels, so shipped adapters need a public conformance interface and re-expressed cases | transport / bridge |
| Q12 | Measured DDL, synthetic value allowlist and exact-build pins built | qualification implementer; future census on drift |
| Q13 | Opt-out/isolation/unsupported/empty-source gates remain required; encrypted storage and generated MEMORY.md rejection not implemented | transport |
| Q14 | Static startup carrier, pinning and non-agent enrollment remedy unqualified; symlink carrier may be Git | transport / shared agent-config owner |
| Q15 | JSON data framing and literal multiline output built; actual consumer/model behavior unproven | qualification / receiving consumer |
| Q16 | Claude typed-entry and alternate-root census absent; user/global/unscoped records stay unshared | bridge qualification |
| Q17 | Device re-registration (registry maps a new device ID to its durable origin) and alias carry-over exercised in model; production durable identity/enrollment evidence still needed | transport identity owner |
| Q18 | Raw Claude file sync can resurrect/echo outside controlled view; narrow/disable or establish replica evidence and test actual path before bridge | parity / bridge |

Transport, two-Mac parity, cross-agent sharing and Cursor discovery stay in
their existing [Future packages](../roadmap-future.md#codex-memory-sync).
Production host-store/sync code, shared settings and release bookkeeping are
unchanged. A no-route result is evidence for those owners, not a feature gate waiver.

<a id="verification"></a>
## Verification

Working directory: the repository checkout root (a Conductor workspace on
device `889e42c0`). Python 3.13.15, ruff 0.15.12, pytest 9.1.1. Default tests
exercise owned synthetic stores and retain existing Keychain/cache protections.

Implementation stage, 2026-10-01. These receipts cover an earlier code snapshot
that the review stage below supersedes; their counts are historical.

| Command / observation | Exact result | Wall-clock measurement |
|---|---|---|
| `./bin/check tests/test_memory_contract.py tests/test_docs_routing.py`, first green run, 17:49:49 UTC | exit 0; lint/format passed; 780 passed, 1 skipped; pytest 9.77 s | 11.189 s, already bootstrapped environment |
| Same scoped command after recovery/promotion repairs, 17:54:55 UTC | exit 0; lint/format passed; 780 passed, 1 skipped; pytest 10.62 s | 11.300 s, warm environment |
| `./bin/check --rebuild tests/test_memory_contract.py tests/test_docs_routing.py`, 17:56:56 UTC | exit 0; wrapper rebuilt only its owned environment; lint/format passed; 780 passed, 1 skipped; pytest 10.42 s | 17.231 s including new venv/install; dependency download caches warm |
| Final `./bin/check tests/test_memory_contract.py tests/test_docs_routing.py`, 18:02:07 UTC, after fixture type/pin repairs | exit 0; lint/format passed; 780 passed, 1 skipped; pytest 10.10 s | 10.739 s, warm environment |
| Separate opt-in root-check recipe above | exit 0; SAFE root-q1; 1 passed, 242 deselected in 1.12 s | Read-only isolation observation; no host launch |

The initial bootstrap/lint attempt stopped at authored lint defects; subsequent
scoped attempts stopped at two lint issues and formatting drift. Those defects
were fixed before protocol execution. These are recorded failures, not green
checks. The default skip is the explicitly opted-in live root check.

Review stage, 2026-10-02. Pre-landing review found that the published recipe
pinned the authoring workspace's absolute path, so the suite failed from any
other checkout, CI included (reproduced: `1 failed, 241 passed` from a
relocated copy). Review passes and three fix cycles then closed
reference-model gaps that could serve retired content or lose retirement,
root-checker and census paths that could give a false SAFE, follow a link or
hang, promotion validators that could promote on contradicting or missing
evidence, and tests that passed for the wrong reason. The third and final
cycle (the review's cap) fixed what was safety-relevant or could serve stale
content: exports that a cold peer could not verify or that re-exported a
GC'd correction's original, stale instances that still served reads,
feedback rows any peer could author, whole-host control state a scoped control
could clear, a symlinked or multi-device owned parent, half-set live opt-ins,
invisible and combining characters in terminal display, non-ASCII recipe
lines, an unbounded census query, and absent-arm PASS rows without verified
eligibility. It also corrected every contract sentence the review showed to
be false. The review did not converge: the remaining items are recorded under
[known model limits](#known-limits) with owners, by the user's decision. The adapter fixture no
longer doubles every case by granularity, because the adapter never branched
on it; F2 still exercises both export units.

Receipts below were measured on the final code of the third fix cycle, before
this paragraph was written; they supersede the earlier cycles' receipts.

| Command / observation | Exact result | Wall-clock measurement |
|---|---|---|
| `./bin/check tests/test_memory_contract.py tests/test_docs_routing.py`, 2026-10-02 14:53:14 UTC | exit 0; lint/format passed; 828 passed, 1 skipped (290 memory, 538 docs-routing); pytest 2.82 s | 3.36 s, warm environment |
| Full `./bin/check`, 2026-10-02 14:53:23 UTC | exit 0; lint/format passed; 5657 passed, 1 skipped, 1 warning from an unrelated module; pytest 105.96 s | 106.59 s, warm environment |
| Memory suite from a relocated copy of the checkout, 2026-10-02 14:55:14 UTC | exit 0; 290 passed, 1 skipped (was `1 failed, 241 passed` before the cycle-1 fix) | 2.39 s |

Every device flush is stubbed in tests (`atomic_write_bytes` still runs end to
end, and C2 injects its own faults), which removed about 45x of per-save cost.
The default skip remains the opted-in live root check, which this stage did
not rerun.

Cold/warm execution alone is below five minutes. The target also includes
human reading, prerequisites, setup and understanding the first result; that
attended clock remains **UNPROVEN**, rather than inferred from agent execution.
Initial cold network setup time was not instrumented; the fresh-venv rebuild
above is explicitly cache-warm. Native eligible seed/scheduler waiting is zero
because prerequisites prevented seeding. No live recall, import/regeneration,
withdrawal, authority, hostile-model behavior or external engineering clearance
is established by these checks. The full `./bin/check` above is review
evidence, not a release claim.

Implementation footprint exceeded the approximate review estimate: the
tests-only adapter, guards, evidence validators and 28 behavioral groups occupy
about 4100 formatted lines, with about 1250 lines of contract/findings/structural
fixtures. This adds no production surface. Later adapters must re-express the
cases against shipped code (Q11); this model remains a separate semantic proof.

<a id="findings"></a>
## Dated findings and cleanup

2026-10-01: structural, credential-free preflight only. Read-only metadata
census used URI `mode=ro` and short transactions in owned disposable stores;
no production DB was opened, copied, checkpointed or treated as immutable.
Transactions do not establish a cross-artifact snapshot. Synthetic revision
and descriptor replacement cases test bounded refusal; an actual native
thread/revision/job/artifact join remains unproven. `mode=ro` may still create
WAL/SHM side effects, acceptable only inside an owned isolated tree.

The supported file/ephemeral credential candidates are documented, not
observed credentialed cleanup. LocalAuthentication/Touch ID and Keychain access
controls can authenticate device-owner interaction, but no target-bound,
agent-resistant enrollment/retirement workflow was exercised. Second-device
confirmation is another unimplemented candidate. No dialog, model assertion or
shared AES key is treated as user authority. Account provisioning and genuine
credential setup remain outside this run's approval.

Evidence handle `preflight-01` denotes sanitized metadata observations here and
in `qualification.json`; it is not a path to retained private prose. Raw
disposable data was removed after extraction of the allowlisted structural
fixtures. At **2026-10-01T17:56:56.478780Z**, cleanup revalidated the original
root identity and current installed source/inventory, verified modes and no
`auth.json`, and removed **554 owned objects**, including the root. A subsequent
existence check confirmed absence. No native process remained from the probe;
no credentialed login occurred. This proves this empty-home cleanup, not future
credential/Keychain cleanup. Only sanitized contract/fixtures/checklist receipts
remain; the raw RPC, DDL, schemas, databases and home are gone.

Official sources refreshed 2026-10-01: [local memories](https://learn.chatgpt.com/docs/customization/memories),
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference),
[other-agent import](https://learn.chatgpt.com/docs/import),
[authentication](https://learn.chatgpt.com/docs/auth),
[config/state locations](https://learn.chatgpt.com/docs/config-file/config-advanced#config-and-state-locations),
and [LocalAuthentication](https://developer.apple.com/documentation/localauthentication/lacontext).
Public Conductor settings/agent pages returned HTTP 403; the bundled Conductor
skill and actual process/version evidence supplied local facts, not isolated
home or recall qualification. CEO/DX external reviews completed; Claude's
engineering review was unavailable after session-limit exit 1. That missing
external review supplies no engineering clearance.
