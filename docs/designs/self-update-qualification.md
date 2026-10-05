# Real self-update qualification — Track 69A

**Documentation complete; S13 REQUIRED / PENDING, 0 of 3 checks passed.**
Owner: Karl / the trial operator. Retry when a stable release newer than the
selected installation is published and its tag, Release and `latest` commit agree.
Reinventory immediately before each command. The four automatic rows additionally
need an existing, genuinely behind, exact own `@latest` pipx installation.
This document is the durable open-obligation record; documentation and regression
tests do not close S13. No eligible real trial ran on 2026-10-04.

## Start with read-only eligibility

Use the selected installation's absolute exposed command, including any suffix.
Keep that path for the fresh invocation after installation; do not resolve the
Python symlink away from its venv. For actual agent-hook trials, capture the
hook's own executable resolution and PATH context privately. A controlled
terminal `autopull`/`autopush` exercises the detached CLI family, without proving
agent-hook integration.

The commands below write only protected evidence outside the checkout. They
never invoke a sync, installer, cache repair or configuration change. Target
discovery uses your existing `gh` login for read-only GitHub API calls, an
authenticated channel with different rate limits from the updater's own
unauthenticated tag fetch. Private paths/output belong under
`~/.local/state/mind-meld-69a-qualification/`, which must sit outside every
enabled mm sync source (confirm with `mm sources`). Do not use
`~/.gstack/projects/`: it is inside mm's default `gstack` source, so every push
would replicate raw evidence to the fleet and a push trial would upload its
own half-written receipts. Run preparation in bash from the designated
workspace:

```bash
umask 077   # this preparation terminal only; never in the trial terminal
export QUAL_ROOT="$HOME/.local/state/mind-meld-69a-qualification"
export QUAL_RUN="$QUAL_ROOT/$(date -u +%Y%m%dT%H%M%SZ)"
export QUAL_TOOLS="$QUAL_RUN/tools"
mkdir -p "$QUAL_TOOLS"
chmod 700 "$QUAL_ROOT" "$QUAL_RUN" "$QUAL_TOOLS"
export QUAL_MM="$(command -v mm)"   # choose an existing absolute suffixed command instead if needed
export QUAL_DEVICE=local-candidate # portable device label
export QUAL_OPERATOR=Karl          # whoever actually runs this trial
export QUAL_PY="$(python3 - "$QUAL_MM" <<'PYTHON'
import shlex, sys
from pathlib import Path
line = Path(sys.argv[1]).read_text().splitlines()[0]
parts = shlex.split(line.removeprefix('#!'))
assert parts and Path(parts[0]).is_absolute() and Path(parts[0]).name.startswith('python'), \
    'shebang is not a direct absolute Python (env or /bin/sh wrapper): find the venv interpreter manually'
print(parts[0])
PYTHON
)"
test -x "$QUAL_PY" && for name in QUAL_ROOT QUAL_RUN QUAL_TOOLS QUAL_MM QUAL_DEVICE QUAL_OPERATOR QUAL_PY; do
  printf 'export %s=%q\n' "$name" "${!name}"
done > "$QUAL_RUN/env.sh" &&
chmod 600 "$QUAL_RUN/env.sh" && echo "In every other terminal: source $QUAL_RUN/env.sh"
```

Every later terminal sources that `env.sh` and keeps its normal umask, so `mm`
and pipx inherit the operator's usual file modes. `trial.py` creates its own
receipts 0600; shell-redirected receipts follow the terminal's umask inside the
0700 directories and are tightened to 0600 when the arm is archived.

Save the five Python recipes embedded below as private helpers. This extractor
copies code; it does not execute it. Helpers are documentation support, never
qualification credit. Use a stable observer interpreter (`python3`) separately
from the candidate's `QUAL_PY`, which a real update may replace.

```bash
python3 - "$PWD/docs/designs/self-update-qualification.md" "$QUAL_TOOLS" <<'PYTHON' &&
import os, re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
out = Path(sys.argv[2])
for name in ('snapshot.py', 'target.py', 'probe.py', 'sample.py', 'trial.py'):
    match = re.search(r'<!-- helper: ' + re.escape(name) + r' -->\n```python\n(.*?)\n```', text, re.S)
    assert match, name
    path = out / name
    with path.open('x') as file:
        file.write(match[1] + '\n')
    os.chmod(path, 0o600)
print('helpers prepared; no installer or sync invoked')
PYTHON
"$QUAL_PY" -I "$QUAL_TOOLS/snapshot.py" "$QUAL_MM" > "$QUAL_RUN/inventory-before.json" &&
"$QUAL_PY" -I "$QUAL_TOOLS/target.py" > "$QUAL_RUN/target-before.json" &&
cat "$QUAL_RUN/inventory-before.json" "$QUAL_RUN/target-before.json"
```

Stop on a nonzero recipe exit: missing/malformed own metadata, a contended or
unreadable upgrade cache, unsupported installed helpers or target discovery are
PENDING prerequisites. Inspect only
own selected metadata, distribution `direct_url` and available prior install
receipts. Missing provenance is unknown. Do not dump shell history, full package
metadata or credentials, or infer a pipx metadata-loss bug. To find additional
existing local candidates, use `pipx list --json` privately, select only entries
whose `main_package.package` is `mind-meld`, and repeat using each own absolute
binary. That command writes a pipx log and can rotate older ones, so run it
before trials, never between an installer's exit and its log copy. Inventory
another Mac only through existing authorized access; unknown fleet candidates
remain unknown.

| Eligibility gate | Required fact / decision |
|---|---|
| Genuine delta | Updater-enabled installed version >=1.3.0 and strictly below the freshly verified stable target. Current versions, bootstrap, metadata edits, fake installers and downgrades supply no credit. |
| Automatic ownership | `main_package.package_or_url` exactly `git+https://github.com/kbitz/mind-meld.git@latest`, own `venvs/mind-meld<suffix>` prefix, own binary, held=false, real pipx available. |
| Explicit ownership | Own tracking or classifier-pinned install, held=false, real pipx, freshly known newer target. A bare own URL is classifier-pinned; this does not assert that its Git ref is frozen. |
| Target | Updater selection from first 100 tags: highest `packaging.Version` after stripping leading `v`, excluding prerelease and local versions. Resolved tag commit equals `latest`; its non-draft, non-prerelease Release exists. Preserve pre/post refs. |
| Automatic policy | Effective `auto_check` and boolean `auto_install` enabled; no `--no-check-version`. Read config without rewriting it. |
| Normal gates | Fresh tag cache lasts 24h; failed/attempted-fetch backoff is 4h. Refresh and install are one step: the first automatic invocation after the gates open fetches the target **and** installs, and any explicit `mm update`'s forced check refreshes the shared cache for every behind tracking sibling at once. At trial time, either the cache already holds the verified target or the trial invocation itself performs the fetch. Arming time per HOME: **now**, if the cached `latest_version` already reaches the target (a pinned or Homebrew mm's tail, `mm recapture`, `auto_install = false` or a sibling's `mm update` can refresh without installing); otherwise max(release published, `checked_at`+24h, `attempted_at`+4h). Same-target automatic install retry is 24h after `install_attempt_at`. Preserve actual timestamps and next times. |
| Sync tail | Valid installed config, selected/available sources, usable storage and privately confirmed passphrase access. `"$QUAL_MM" push --dry-run` and `pull --dry-run` exercise these without any upgrade check, fetch or install. Healthy no-change/usage-only paths can reach the tail; setup refusal/no-sources may prevent it. Attended `push` runs its update tail even after a failed sync; `pull` skips it. No manufactured content change is needed. |
| Quiet window | From **before the arming time** until the arm is archived: no other sync, hook, agent session, installer, `mm recapture`, `mm update` or `mm status` on that HOME. Status reads the upgrade cache under a shared lock and, for a recorded same-target attempt, briefly takes `install.lock` exclusively, which can make a concurrent retry claim read in-flight and skip. A pinned install's nudge prints the consuming `pipx install --force …@latest`; following it spends that candidate. Coordinate the operator's work without altering hooks/config. Observers ready before any invocation can consume the old installation. |
| Pin recovery | Record `recovery_command` with exact own pipx home, checked bin directory and suffix before explicit force reinstall. Check destination belongs to this venv. Failed force reinstall can remove it. Recovery is separate setup, never credit for the failed trial. |

All local venvs share the per-Mac `upgrade-state.json`, `install.lock` and
`auto-update.log`. Separate installations do not isolate these gates. After the
10-minute grace, a completed claim with no outcome can read as failed for a
still-behind sibling, even if the first install succeeded. That is a scheduling
gate, not evidence that the sibling's installer failed. Respect the actual next
eligible time; never edit cache timestamps. A newer target resets the daily
claim, but a live installer excludes another install for every target/age.

## Original required S13 checks

Source: [PR #189 preparation receipt, 2026-09-30](https://github.com/kbitz/mind-meld/pull/189#issuecomment-5919279593).
Its historical bootstrap paragraph supplies no new installation authorization
in this Track. The following three checks are preserved verbatim:

1. On an updater-enabled exact @latest pipx install with a newer release, run attended push/pull. Expect sync completion, foreground pipx upgrade under the mm lock, and the next mm --version plus pipx metadata at that newer release.
2. Exercise autopull/autopush on such an install. Expect prompt hook return, detached output in ~/.config/mind-meld/auto-update.log, no inherited mm lock, and the next invocation/metadata at the newer release.
3. Run explicit mm update while behind. Expect exit 0 only after reaching the known latest target; rerun while current and expect no installer. Save command, exit/output, install spec, tested release/commit and installed-version evidence.

One genuine attended upgrade closes check 1; one genuine detached upgrade closes
check 2; one genuine explicit upgrade **and** a proven current rerun close check 3.
At least three genuine upgrades are required. The five command rows disclose
coverage: run siblings whenever eligible, but unqualified siblings are not two
extra S13 blockers. One upgrade consumes that installation's old state.

## Five rows and concrete release schedule

Inventory receipt: 2026-10-04T12:04:09.929481Z, operator Karl / Codex,
device label `local-candidate`, macOS 27.0, installed Python 3.14.8,
pipx 1.17.9, unsuffixed own venv, held=false. Exposed binary is
`~/.local/bin/mm`, target `~/.local/pipx/venvs/mind-meld/bin/mm`;
private receipt retains absolute paths/prefix. Distribution, pipx metadata and
fresh exact-binary `--version` all report 1.4.0. Recorded spec is the bare own URL;
`direct_url.vcs_info.commit_id` is `a3c5050ee34ad4c077469e036ab25808bbe519fb`,
with no `requested_revision`. Prior bootstrap/upgrade provenance is unknown;
parser-only historical evidence does not establish how this install arrived.
The retained local pull history and its rotated sibling were read on
2026-10-04T12:22:21.795697Z: no self-upgrade transition rows were present.
PR #189's available final-preflight artifact records review state, not an install
receipt. This establishes neither historical install provenance nor fleet-wide
absence; the selected projection is protected in `prior-provenance.json`.

Target receipt: 2026-10-04T12:03:24.236528Z. [Release v1.4.0](https://github.com/kbitz/mind-meld/releases/tag/v1.4.0)
was published 2026-10-02T18:00:22Z; stable tag and `latest` both resolve to
`a3c5050ee34ad4c077469e036ab25808bbe519fb`. No newer stable target exists
in that observation. Config is valid; both automatic settings are on; five
selected source roots and storage exist. Passphrase access and real sync-tail
readiness were deliberately left unverified because no trial is eligible.
The fresh cached target is 1.4.0, checked/attempted at
2026-10-04T02:13:20.815056Z: next natural tag refresh is
2026-10-05T02:13:20.815056Z, failed-fetch-backoff endpoint
2026-10-04T06:13:20.815056Z. Install-attempt fields are null, so no daily claim
retry time applies. `install.lock` was absent; it was not created or probed.
Default-home pipx inventory found one own install; other homes/Macs are unknown.
Final read-only recheck at 2026-10-04T12:15:00.856229Z (inventory) and
12:15:02.388812Z (target) confirmed the same version/spec/commit, cache fields
and lock identities; fresh exact-binary `--version` exited 0. The selected
installation remains ineligible. Raw receipts are `inventory-final.json` and
`target-final.json` in the private preparation directory below.

Later observation (2026-10-05, read-only queries only, not a re-inventory):
[Release v1.5.0](https://github.com/kbitz/mind-meld/releases/tag/v1.5.0) was
published 2026-10-04T23:45:59Z, after the 12:03Z target receipt above. Tag
`v1.5.0`, the `latest` branch and GitHub's latest Release all resolve to
`fb39f62dff14c9803c9073235b26a5a769081938`. This Mac's `mm` still reports 1.4.0
and its pipx metadata still records the bare repository URL. The retry event for
the explicit row (row 5) has therefore occurred; it still needs fresh
`snapshot.py` and `target.py` receipts, the observer and recovery prerequisites
and a quiet window before any command, and no trial has run. The four automatic
rows' dated causes below stand, except that 1.4.0 is no longer the current
release, so what blocks them now is the classifier pin: they need another
existing behind exact `@latest` install. The cache fields above were not
re-read.

| Exact intended invocation (controlled terminal) | S13 family | Status / problem and observed cause | Candidate / target / consumed state | Owner, retry event and next action |
|---|---|---|---|---|
| `"$QUAL_MM" push` | 1 attended | REQUIRED / PENDING: current 1.4.0 and classifier-pinned bare spec | Local candidate unusable for automatic trial; other tracking candidates unknown | Karl: inventory an existing naturally-behind exact `@latest` install at a newer agreeing stable release; wait any natural cache/claim gates, confirm sync/observers; [automatic policy](../invariants/auto-upgrade.md#self-update-v130). |
| `"$QUAL_MM" pull` | 1 attended sibling | REQUIRED / PENDING: same current/pinned prerequisite | Cannot reuse old state consumed by push; needs another real old install or later release | Karl: same inventory event; disclose unqualified sibling if push alone supplies family evidence; [automatic policy](../invariants/auto-upgrade.md#self-update-v130). |
| `"$QUAL_MM" autopull` | 2 detached | REQUIRED / PENDING: same current/pinned prerequisite | Existing tracking candidate unknown; no hook integration observed | Karl: existing behind tracking install + newer agreeing release, quiet window/ready overlap observers and natural gates; [hook behavior](../../README.md#automatic-updates). |
| `"$QUAL_MM" autopush` | 2 detached sibling | REQUIRED / PENDING: same current/pinned prerequisite | Cannot reuse consumed autopull state; same-Mac/same-target second automatic candidate waits claim+24h | Karl: separate natural old install or later release; preserve origin and disclose sibling coverage; [shared exclusion](../invariants/auto-upgrade.md#self-update-v130). |
| `"$QUAL_MM" update`, then immediate current rerun | 3 explicit | REQUIRED / PENDING: no newer target in the 12:03Z observation (v1.5.0 followed, see the later observation above; not re-inventoried); negative observer capability INCONCLUSIVE | Local 1.4.0 classifier-pin can supply force-reinstall branch at the next newer release; tracking-upgrade sibling unqualified | Karl: newer agreeing stable release, recovery recorded and quiet window; after genuine target-reaching update, same invocation rerun must refresh target successfully with validated complete observer; [explicit update](../../README.md#upgrading). |

No trial was invoked, so sync outcome, installer outcome/output, parent/child
lifetime and trial lock evidence are **not observed** for every row. Inventory
`--version` is preparation, not a current-path `mm update` test. No observed
failure or observed pass is claimed.

Earliest honest local sequence: next newer stable release → explicit genuine
pinned update → its current rerun if complete observation is available → a
**following** newer release with now-tracking local install → one automatic
family → a **further** newer release → the other automatic family. Each step
rechecks actual facts; no version numbers/dates are promised. A real existing
behind install on another authorized Mac may shorten the schedule. On one Mac,
two automatic installs for the same target still respect the 24h shared claim.
Remaining command siblings need independent naturally-old state; never credit
the resulting current install as another upgrade. No new installs, config
changes, deliberate releases or repair work is authorized by this schedule.

## Prepare observers before consuming eligibility

Private preparation receipts from `20261004T1200Z/`: `s13-source.md`,
`inventory-before.json`, `target-before.json`, `observer-validation.json`,
`validation-samples.jsonl`, `eslogger.stderr` and the helper sources. Directories
are 0700 and files 0600. That run predates the root above: it lives under the
earlier `~/.gstack/projects/kbitz-mind-meld/69a-qualification/`, inside mm's
`gstack` source, so encrypted copies may already exist on other Macs. It was
left in place, since moving it would make mm propagate a deletion. Portable result at 2026-10-04T12:05:57Z: an independent
probe was blocked while a harmless holder lived, then available after it exited,
on the same disposable inode with unchanged bytes/mtime. The sampler caught a
live one-second subject and caught **0 of 12** `/usr/bin/true` subjects. That
result came from a private validation script in the same directory (a
pipe-fed holder and a 12-child loop), not from the exact recipes below. During
review on 2026-10-04 the corrected recipes were re-exercised on disposable
fixtures. This is observer preparation, no mm-driven installer evidence.

Run the probe recipe against a **disposable private file** first, with an
independent harmless holder, then after that holder exits. Validate the sampler
before each cohort; these runnable commands create no candidate installation:

```bash
printf 'harmless fixture\n' > "$QUAL_RUN/disposable.lock"
python3 -I - "$QUAL_RUN/disposable.lock" <<'PYTHON'
import fcntl, sys
path = sys.argv[1]
# stdin is this script, so wait on the terminal itself
with open(path, 'rb') as file, open('/dev/tty') as tty:
    fcntl.flock(file, fcntl.LOCK_EX)
    print('HOLDER READY; run probe.py in the second terminal, expect blocked', flush=True)
    print('After probe, press Enter to release the harmless holder: ', end='', flush=True)
    tty.readline()
PYTHON
# In the second terminal, while holder is READY, then again after holder exit:
python3 -I "$QUAL_TOOLS/probe.py" "$QUAL_RUN/disposable.lock"
# In the observer terminal; first JSON line must say READY_SAMPLED_ONLY:
python3 -I "$QUAL_TOOLS/sample.py" 10 69a-harmless-subject > "$QUAL_RUN/validation-samples.jsonl"
# In the second terminal, after that readiness line appears:
python3 -I -c 'import time; time.sleep(1)' 69a-harmless-subject
for n in $(seq 12); do /usr/bin/true 69a-harmless-subject; done
```

The sampler retains UTC/monotonic timestamps, PID, PPID, process group, start
time, state and displayed argv, plus sample intervals/ps exits. Reject zombies
and PID reuse; preserve identity before/after each probe. Sampling is for
**positive live evidence only**: start-time precision is one second, displayed
argv needs manual own-command attribution, and interval gaps or failed ps calls
can lose short-lived processes. A READY line acknowledges only sampling
readiness. Neither ordinary absence, unchanged version nor an unchanged log
establishes that no installer ran.

For whole-invocation no-installer proof, use a validated loss-accounted native
fork/exec/exit stream or independently validated equivalent. Candidate method:
macOS `/usr/bin/eslogger exec fork exit`, as described in local `man eslogger`
and [Apple Endpoint Security documentation](https://developer.apple.com/documentation/endpointsecurity/monitoring-system-events-with-endpoint-security).
On this host the nonprivileged preparation attempt exited 1 with
`ES_NEW_CLIENT_RESULT_ERR_NOT_PRIVILEGED`. Complete coverage is **unavailable**;
owner Karl must arrange an already-authorized privileged/FDA observer context
outside this Track, validate it, then retry at the genuine opportunity. No
privilege/TCC/config change was made here. The explicit row remains pending;
its no-installer capability is inconclusive, never an observed no-installer pass.

Launch the event collector in its own session, in a dedicated observer terminal.
The recipe below tries existing noninteractive authorization; failure is a
missing prerequisite, not a request to change privileges. Stop only this observer
with Ctrl-C after the end sentinel is received; do not interrupt an installer.

```bash
python3 -I - "$QUAL_RUN" <<'PYTHON'
import os, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
os.umask(0o077)
with (root / 'exec-events.jsonl').open('xb') as out, (root / 'exec-events.stderr').open('xb') as err:
    process = subprocess.Popen(['sudo', '-n', '/usr/bin/eslogger', 'exec', 'fork', 'exit'],
                               stdout=out, stderr=err, start_new_session=True)
    print('collector session PID', process.pid, '; NOT READY until sentinels validated', flush=True)
    try:
        code = process.wait()
    except KeyboardInterrupt:
        subprocess.run(['sudo', '-n', '/bin/kill', '-INT', '--', '-' + str(process.pid)], check=True)
        code = process.wait(timeout=5)
raise SystemExit(code)
PYTHON
```

The collector starts in a new session without a controlling terminal, so
`sudo -n` there may not reuse the terminal's cached sudo credential (sudoers
ties tickets to a terminal by default). An existing rule that authorizes these
exact commands without a password may therefore be the real prerequisite;
arranging it belongs to the owner, outside this Track. Stopping needs a second
`sudo -n /bin/kill`. If that is not authorized, or the sudo credential lapsed
during a long arm, the wrapper exits with an error while the root collector
keeps running: stop it from the authorized context before any further trial. Exec events can carry argv and environment, including any
passphrase or token variables, so treat the stream as secret-bearing raw
evidence.

`eslogger` suppresses its own process group: keep trial and harmless validation
subjects outside that group. Do not apply an executable filter that hides
children/reparenting or use truncated oslog output. Its JSON schema is not stable;
inspect the current `schema_version` and message version. Before claiming READY,
validate a known burst of short-lived `/usr/bin/true` children with recorded PIDs,
start times, argv and parent; find **every** fork/exec/exit event, plus before/after
sentinels, in the actual stream. Use the same collector continuously through the
rerun; record its PID, subscription, first/last received event and counters.
Verify per-event `seq_num` and, when supplied, `global_seq_num` have no dropped
events for the subscribed stream, no malformed/truncated records, no unexplained
counter gaps, and no collector restart. Explicitly retain fork relationships,
exec target audit-token PID/start identity/argv and exit status for the mm
invocation and all descendants, including short-lived/reparented ones. Buffering
must be drained through the post-exit sentinel; reaching a sample/collector
wall-clock deadline alone does not prove coverage. Missing/ambiguous schema,
loss counters, readiness, sentinel or descendant attribution → INCONCLUSIVE.
A captured live overlap may still qualify a positive lock observation; it cannot
repair missing negative evidence for the current rerun.

Generate the validation burst in the subject terminal after starting the
collector. Compare the recorded child PIDs/parent/argv/spawn timestamps to the
native event start identities; both sentinels must be received before READY.
For the current rerun, use separate before/after sentinels around that invocation
and drain the latter before stopping the collector.

```bash
python3 -I - "$QUAL_RUN/exec-validation-subjects.json" <<'PYTHON'
import json, os, subprocess, sys
from datetime import datetime, timezone
os.umask(0o077)
receipts = []
for label in ['before-sentinel', *['short-child-' + str(n) for n in range(12)], 'after-sentinel']:
    argv = ['/usr/bin/true', '69a-' + label]
    before = datetime.now(timezone.utc).isoformat()
    child = subprocess.Popen(argv)
    code = child.wait(timeout=5)
    receipts.append(dict(label=label, pid=child.pid, ppid=os.getpid(), argv=argv,
                         spawn_before=before, exit_observed=datetime.now(timezone.utc).isoformat(), exit=code))
with open(sys.argv[1], 'x') as file:
    json.dump(receipts, file, indent=2)
print('Subjects finished; compare all 14 identities and lifecycle events before declaring READY.')
PYTHON
```

## Run one eligible arm

Recheck inventory and target, reserve the quiet window, record the exact own
binary and command/origin, write the candidate/family/target/claim-time schedule,
and confirm observer readiness. Do not invoke any row until its eligibility
conditions are met. Never invoke a second installer while the first lives.

Prepare an empty private arm directory, take pre-arm snapshots, and start the
sampler **before** the command, in the observer terminal:

```bash
export QUAL_ARM="$QUAL_RUN/push" # choose pull, autopull, autopush, explicit or current-rerun per row
mkdir -m 700 "$QUAL_ARM" &&
printf 'export QUAL_ARM=%q\n' "$QUAL_ARM" >> "$QUAL_RUN/env.sh" &&
"$QUAL_PY" -I "$QUAL_TOOLS/snapshot.py" "$QUAL_MM" > "$QUAL_ARM/inventory-before.json" &&
"$QUAL_PY" -I "$QUAL_TOOLS/target.py" > "$QUAL_ARM/target-before.json" &&
QUAL_PIPX="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["pipx"] or "")' "$QUAL_ARM/inventory-before.json")" &&
printf 'export QUAL_PIPX=%q\n' "$QUAL_PIPX" >> "$QUAL_RUN/env.sh" &&
python3 -I "$QUAL_TOOLS/sample.py" 900 "$QUAL_MM" "$QUAL_PIPX" bin/mm mind_meld > "$QUAL_ARM/process-samples.jsonl"
```

The sampler watches the selected binary, the pipx binary mm itself found and
any other mm (`bin/mm`, `mind_meld`), such as a stale Homebrew copy a hook PATH
can resolve: it shares the mm lock and upgrade cache. Attribute every row by
hand. It refuses an empty needle. 900s covers a sync plus the 600s foreground
pipx timeout with margin. For a detached arm, if fewer than 10 minutes would
remain after `parent-exit.json`, start a second sampler then, from a terminal
that re-sourced `env.sh`, into its own file:

```bash
python3 -I "$QUAL_TOOLS/sample.py" 600 "$QUAL_MM" "$QUAL_PIPX" bin/mm mind_meld > "$QUAL_ARM/process-samples-2.jsonl"
```

Read the first sampler line for READY in another terminal. Use the stable
observer Python for the runner; its files store command PID, exact argv, exit,
UTC and monotonic parent-return timing, umask and the PATH/pipx environment.
Run it from a terminal that sourced `env.sh` (after the arm's `QUAL_ARM` line
was appended) and kept its normal umask. It refuses a relative arm directory and
`PYTHONPATH`/`PYTHONHOME`/`PYTHONSTARTUP`. Output goes to files, so follow it
with `tail -f "$QUAL_ARM/command.stderr"` in another terminal. **Do not press
Ctrl-C:** it reaches mm, which then interrupts its own foreground pipx, and
interrupting a pinned force reinstall can delete the venv. Detached pipx runs in
its own session and is not affected. The runner keeps waiting and records any interrupt. Set
**only the scheduled eligible command**:

```bash
export QUAL_CMD=push # exactly one of: push, pull, autopull, autopush, update
python3 -I "$QUAL_TOOLS/trial.py" "$QUAL_ARM" "$QUAL_MM" "$QUAL_CMD"
```

For an actual agent-hook-origin trial, observe the real integration's resolved
command instead of claiming the controlled runner tested it. Preserve its parent
exit/command timing and equivalent stdout/stderr/exit receipts.

**Attended push/pull:** stdout/stderr must establish sync completion separately
from updating/updated notices and real pipx outcome. While the mm parent is alive,
probe the mm lock **only after** positively identifying its own real non-zombie
pipx and exact `upgrade <own-venv>` argv. It must be blocked; the installer lock
must also be blocked. Avoid the sync-to-update lock reacquisition gap. Immediately
before and after each independent probe capture the same live pipx identity.
After the mm parent and installer complete, probe release (available) and compare
baseline/fd/path inode+device. Never probe availability while an mm parent lives.
Foreground pipx's production timeout is 600s with its own bounded cleanup; do
not shorten it or add an observer kill. A missed pipx overlap is inconclusive.
`push` runs its update tail even when its sync failed, so a failed push can
still consume the starting state (see the outcome table); `pull` skips the tail
after a refused sync. If push/pull exits with no updating notice, do not file a
cache/claim skip yet: compare before/after cache (`checked_at`,
`install_attempt_*`), sampler rows for any concurrent mm PID and the
`auto-update.log` header. When another mm took the lock between sync and update,
mm skips silently; that is an INCONCLUSIVE quiet-window breach, and the process
that took the lock may now install from the fresh cache under the wrong family.

**Detached autopull/autopush:** require `parent-exit.json` followed by positive
live non-zombie pipx identity; compare UTC/monotonic observations to show the
parent returned **before** pipx completed. Immediately probe existing same-inode
mm lock (available) and installer lock (blocked), with live pipx identity on both
sides. A concurrent mm parent invalidates availability attribution. Optionally
corroborate fd absence with `lsof -p <observed-pipx-pid>` saved privately. Preserve
sync output separately from `auto-update.log`: legitimate sync output need not
be empty. Observe for up to 10 minutes after parent exit; if child remains live,
record PENDING IN-FLIGHT with current PID/start/argv and lock receipts. This
observation deadline neither kills the child nor proves installer failure. Keep
waiting for actual completion in a subsequent observation; no second installer.

**Phase-safe independent probe commands:** fill the positive observed numeric
PID and baseline device/inodes from receipts; the displayed values below are
shell inputs, not claims that a trial ran. Inode change, missing lock, observer
OSError or any concurrent/probe-induced LockError → INCONCLUSIVE. A probe briefly
perturbs kernel locking even though it opens read-only; coordinate the window.

First record and read the live identity:

```bash
export QUAL_PIPX_PID=12345 # replace with positively observed own live pipx PID
/bin/ps -p "$QUAL_PIPX_PID" -o pid=,ppid=,pgid=,lstart=,stat=,command= | tee -a "$QUAL_ARM/probe-live.txt"
```

Only when that shows the real own non-zombie pipx in the correct phase, run the
probes, then capture identity again. Each probe receives the pre-arm
device/inode recorded in `inventory-before.json`, or none when that lock did not
exist yet:

```bash
QUAL_BASE() { python3 -c 'import json, sys; lock = json.load(open(sys.argv[1]))["locks"][sys.argv[2]]; print(lock.get("device", ""), lock.get("inode", ""))' "$QUAL_ARM/inventory-before.json" "$1"; }
python3 -I "$QUAL_TOOLS/probe.py" "$HOME/.config/mind-meld/mind-meld.lock" $(QUAL_BASE mm) >> "$QUAL_ARM/mm-probes.jsonl"
python3 -I "$QUAL_TOOLS/probe.py" "$HOME/.config/mind-meld/install.lock" $(QUAL_BASE installer) >> "$QUAL_ARM/installer-probes.jsonl"
/bin/ps -p "$QUAL_PIPX_PID" -o pid=,ppid=,pgid=,lstart=,stat=,command= >> "$QUAL_ARM/probe-live.txt"
```

Confirm both paths against `locks` in `inventory-before.json`, which records
the installed release's own lock paths. The helper opens existing regular files,
never creates/unlinks/truncates/writes PID data, uses nonblocking flock, and
closes immediately. Any argument, open or identity failure is written as an
`inconclusive` JSON line with exit 1. Reject a different inode even if it
reports the expected lock state. If `install.lock` was absent before the arm
(no mm-driven installer has run on this HOME), the first observation during
the arm, attributed to the observed own installer, becomes its baseline: pass
that line's device and inode explicitly to later probes. Absence or an inode
change counts as INCONCLUSIVE only after that. After
parent/installer exit, repeat the probes without the live-pipx expectation to
establish release.

**Explicit update:** record branch `tracking → pipx upgrade <venv>` or
`classifier-pinned → pipx install --force …@latest [--suffix=…]`, actual pipx
PID/argv and own home/bin binding, foreground mm/installer exclusion, known
target and result. Either branch can close check 3; disclose the sibling branch
as unqualified. Preserve real captured stdout/stderr, not `auto-update.log`:
explicit update does **not** write that automatic log. On success it may print
only a summary; identify pipx's own new `cmd_*.log` by command, timestamp and
home, and copy matching log and any named error-log files immediately. mm sets
`PIPX_HOME` to the install's own home, so its pipx runs log under
`$PIPX_HOME/logs` (for the default home, `~/.local/pipx/logs`); manual pipx
runs without `PIPX_HOME` log to `~/Library/Logs/pipx`. pipx keeps only the
newest 10 (`PIPX_MAX_LOGS`), and every other pipx command writes a log that can
rotate older ones out. If pipx is silent or no matching log
exists, record that fact; a prior automatic log is no replacement. Exit 0 only
qualifies when installed metadata/commit and the new process reach the known
target. Recovery must preserve the recorded home, bin directory and suffix.

**Current rerun:** only after a genuine explicit upgrade and rechecked agreeing
target, create a fresh arm directory and invoke the same exact binary's `update`
with the already validated continuous complete observer. Its **own forced** tags
check must succeed: retain cache `checked_at` advancing within invocation times,
`latest_version` equal to the known current target, target refs and current-path
output, plus complete process events, actual pipx-log inventory and metadata
before/after. A separate preflight network success cannot substitute for that
invocation's check. Offline/rate-limited forced fallback may legitimately start
pipx on tracking installs; keep it pending known-target verification, not a
current-path defect. Require no installer exec/descendant throughout the whole
invocation, verified through drained end sentinel and loss accounting.
Unchanged version, log or metadata alone cannot pass. If completeness cannot be
established, preserve partial receipts as INCONCLUSIVE and keep check 3 open.

## Archive immediately and decide

Once actual installer completion is established, copy shared cache/log before
any later arm can overwrite them. If a detached child is still live, copy an
**in-flight** snapshot now and a new completion snapshot later. Do not launch a
fresh mm process during its package swap. The snapshot's shared cache flock
closes before any mm-lock probe. These commands only archive/read state:

```bash
export QUAL_PHASE=completion # or in-flight while a detached child still lives
python3 -I - "$QUAL_ARM" "$QUAL_PHASE" <<'PYTHON'
import os, sys
from pathlib import Path
out, phase = Path(sys.argv[1]), sys.argv[2]
assert phase in ('in-flight', 'completion')
for name in ('upgrade-state.json', 'auto-update.log'):
    source = Path.home() / '.config/mind-meld' / name
    if source.exists():
        dest = out / (name + '.' + phase + '.copy')
        with dest.open('xb') as file:
            file.write(source.read_bytes())
        os.chmod(dest, 0o600)
PYTHON
```

Only after the actual installer has exited, and never in the `in-flight` phase,
take the after-state and the next process, then tighten every receipt:

```bash
if [ "$QUAL_PHASE" = completion ]; then
  "$QUAL_PY" -I "$QUAL_TOOLS/snapshot.py" "$QUAL_MM" > "$QUAL_ARM/inventory-after.json"; echo "snapshot exit $?"
  "$QUAL_PY" -I "$QUAL_TOOLS/target.py" > "$QUAL_ARM/target-after.json"; echo "target exit $? (1 = refs disagree)"
  "$QUAL_MM" --version > "$QUAL_ARM/next-process-version.txt" 2>&1; echo "next process exit $?"
fi
chmod 600 "$QUAL_ARM"/* "$QUAL_RUN"/*.json "$QUAL_RUN"/*.jsonl 2>/dev/null
```

Each step runs even if an earlier one failed, so a moved target, a contended
cache or a missing venv still leaves the other receipts; record every nonzero
exit as part of the outcome.

An explicit arm may archive the old automatic log solely to show it is unrelated;
label it that way. Preserve actual own pipx logs separately. Do not silently
normalize/rewrite the production cache; archive access failures explicitly.
Receipt copies can race an in-flight writer: label phase and require a settled
post-exit copy before claiming completion. Hash finalized raw files, retain them
immutably and scan any portable summary before publication. Keep directories
0700 and files 0600 even when a privileged collector wrote them. No raw paths, process
argv with secrets, config payloads, sync content or machine-identifying logs go
in a commit; retain only redacted findings, timestamps and release SHAs here.

For each row retain: operator/device/UTC; exact invocation/origin; before install,
prefix/home/bin/suffix/hold, direct_url and source identity; target tag/Release/
latest pre/post SHAs; shared gates and next times; observer method/readiness/loss
limits; parent PID/start/exit/timing; live pipx PID/start/argv/parent attribution;
phase-correct independent same-inode locks before/during/after; separate sync
exit/output and installer result/output/log identity; after metadata/spec/commit;
fresh exact-binary `--version`; explicit branch/current rerun evidence; outcome,
problem/cause/action/link, owner and retry event. Missing evidence stays visible.
A target moving mid-run requires both refs and attribution to the actual reached
released commit; never credit a below-known-target install or untagged bytes.

| Outcome | Decision and actionable remedy |
|---|---|
| REQUIRED / PENDING: current/wrong/unknown install | Record exact missing spec/version/own-prefix/hold/updater fact. Karl reinventories an existing eligible install at the next newer agreeing stable release; unsupported or setup changes need separate work. [Ownership](../invariants/auto-upgrade.md#self-update-v130). |
| PENDING: target unavailable/mismatched | Cause is request failure, no eligible stable tag, or tag/Release/latest disagreement. Wait for network/rate limit/release publication to recover and rerun read-only discovery; no forced pin update on a guess. [Upgrading](../../README.md#upgrading). |
| PENDING: normal cache/claim skip | Record actual checked/attempt/claim times and next eligible time. Wait for natural 24h freshness/retry or 4h failed-fetch backoff; newer target can reset claim. A skipped installer never failed. [Gates](../invariants/auto-upgrade.md#self-update-v130). |
| PENDING IN-FLIGHT / busy | Positively observed live installer or exclusion prevents another. Preserve PID/locks, wait for actual exit; ten-minute observation is not timeout. Never remove any lock. [Exclusion](../invariants/auto-upgrade.md#self-update-v130). |
| PENDING: sync refusal/tail unreached | Preserve sync stderr/exit independently; cause may be invalid config, crypto/storage refusal or no sources. `pull` skips its update tail after a refused sync. Follow [sync remedies](../../README.md#troubleshooting) in separate authorized repair work, then retry only if real old state remains eligible. Exit 0 from an auto command alone does not prove it reached the tail. |
| PENDING, state consumed: push sync failed but its tail installed | Attended `push` runs its update tail even after a failed sync. Record the installer evidence as usual, but check 1 requires sync completion, so it earns no credit and the starting state is spent. Repair the sync cause in separate work and retry with another naturally-behind install or later release. [Self-update](../invariants/auto-upgrade.md#self-update-v130). |
| OBSERVED FAILURE: pipx nonzero/foreground timeout, missing venv or below-target result | Record reproduction, installer logs and actual installed state, with sync success kept separate. Karl uses the **recorded own** home/bin/suffix recovery after quiet-window verification; do not blindly repair a sibling. File reproduced updater defects separately. [Recovery](../../README.md#upgrading). Recovery is not a retroactive pass. |
| INCONCLUSIVE: missing overlap, event loss, wrong inode/log, LockError or silent lock-gap skip | Cause is missing/ambiguous proof, not established updater failure. Retain partial evidence; validate observers/quiet window and retry with another naturally-behind install or later release. For no-installer proof arrange validated complete observation, not faster polling. [Lock/process contract](../invariants/auto-upgrade.md#self-update-v130). |
| INCONCLUSIVE: target moved / receipt lost | Resolve actual released commit against before/after refs; restore independently attributable raw receipts. If attribution remains unknown, retry at a natural opportunity. Do not promote an overwritten log or below-target result to a pass. [Release discipline](../invariants/auto-upgrade.md#release-discipline-enforced-by-mm-auto-upgrade). |
| OBSERVED PASS | Only after all required family receipts agree. Record which command and explicit branch passed; leave unqualified siblings visible. S13 closes only with checks 1, 2 and full 3 satisfied. |

This Track filed two diagnostic follow-ups as new `docs/TODOS.md` Unprocessed
items (unsafe lock-removal advice and broad pinned prose); the repairs themselves
are out of scope. No runtime/config/cache/
credential manipulation, new installation, downgrade, release bump or roadmap
edit was performed. No new product tests mirror this document or bypass the
real-pipx pytest guard.

## Timing and regression evidence

Open-document → understood eligibility target is 2–5 minutes including reading,
setup and network. **Unmeasured:** no independent operator clock was taken;
implementing agent had prior familiarity from the full plan/source review.
Installer duration, parent-return time and release waits are also unmeasured
because no genuine trial ran. Future operator records start/end UTC, elapsed,
prior familiarity and these clocks separately; this is not a release gate or
telemetry feature.

Regression command: `./bin/check tests/test_self_update.py tests/test_docs_routing.py`.
Result on 2026-10-04: exit 0; Ruff check and format clean, **698 tests passed**
in 10.04s on the final run (first run: 698 passed in 5.64s). `MM_VENV` selected the existing sibling workspace's development venv
and `PYTHONPATH` selected this checkout's `src`; no environment was installed or
modified. The protected final log is `20261004T1200Z/bin-check-final.log`;
the first run is retained in `bin-check.log` in the same directory.
Regression/observer preparation cannot satisfy real S13.

## Private helper recipes

These snippets are copied by the preparation extractor above. Candidate snapshot
uses installed code with isolated Python imports; run from its own shebang
interpreter, and stop on unreadable metadata. Target discovery does not call
`check_for_upgrade` or alter the cache. Probes run in a separate process.

<!-- helper: snapshot.py -->
```python
import dataclasses
import hashlib
import importlib.metadata as md
import json
import os
import platform
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from mind_meld import config, upgrade
from mind_meld.lockedjson import locked_json_snapshot

binary = Path(sys.argv[1]).absolute()
prefix = Path(sys.prefix)
main = json.loads((prefix / 'pipx_metadata.json').read_text())['main_package']
install = upgrade.detect_install()
dist = md.distribution('mind-meld')
direct = json.loads(dist.read_text('direct_url.json') or '{}')
now = datetime.now(timezone.utc)

def fingerprint(path):
    try:
        st = path.stat()
        return dict(device=st.st_dev, inode=st.st_ino, size=st.st_size,
                    mtime_ns=st.st_mtime_ns)
    except OSError as error:
        return dict(unknown=type(error).__name__)

try:
    cfg = config.load_config()
    sources = config.resolve_sources(cfg, bootstrap=False).selected
    cfg_view = dict(valid=True, auto_check=cfg.get('upgrade', {}).get('auto_check') is not False,
                    auto_install=upgrade.auto_install_enabled(cfg),
                    sources=[dict(name=s['name'], path=s['path'],
                                  exists=Path(s['path']).expanduser().exists()) for s in sources],
                    storage_path=cfg['storage']['path'],
                    storage_exists=Path(cfg['storage']['path']).expanduser().is_dir(),
                    passphrase_access='unverified; confirm privately before an eligible sync')
except Exception as error:
    cfg_view = dict(valid=False, cause=type(error).__name__)

with locked_json_snapshot(upgrade.CACHE_PATH, blocking=False) as snap:
    if snap.state not in ('valid', 'missing'):
        raise SystemExit('upgrade cache snapshot ' + snap.state + ': record PENDING and rerun')
    cache = dict(snap.data) if snap.state == 'valid' else {}
keys = ('latest_version', 'checked_at', 'attempted_at', 'install_attempt_version',
        'install_attempt_at', 'install_attempt_outcome')
cache_view = {key: cache.get(key) for key in keys}
gates = (('checked_at', 'next_at', upgrade.DEFAULT_THROTTLE),
         ('attempted_at', 'next_at', upgrade.DEFAULT_FAILURE_BACKOFF),
         ('install_attempt_at', 'grace_end_at', upgrade.INSTALL_GRACE),
         ('install_attempt_at', 'next_at', upgrade.DEFAULT_INSTALL_RETRY_GAP))
for key, suffix, window in gates:
    at = upgrade._parse_iso(cache.get(key))
    cache_view[key + '_' + suffix] = (at + window).isoformat() if at else None
pipx = upgrade.find_pipx()
version_probe = subprocess.run([str(binary), '--version'], capture_output=True,
                               text=True, timeout=10)
bin_dir = Path(os.environ.get('PIPX_BIN_DIR') or Path.home() / '.local/bin').expanduser().resolve()
destination = bin_dir / ('mm' + install.suffix)
destination_owned = (not (destination.exists() or destination.is_symlink())
                     or destination.resolve() == (prefix / 'bin' / 'mm').resolve())
recovery = None
if pipx and install.kind in ('pinned', 'tracking') and destination_owned:
    argv = [pipx, 'install', '--force', upgrade.INSTALL_SPEC]
    if install.suffix:
        argv.append('--suffix=' + install.suffix)
    recovery = 'PIPX_HOME=' + shlex.quote(str(prefix.parent.parent)) + ' PIPX_BIN_DIR=' + shlex.quote(str(bin_dir)) + ' ' + shlex.join(argv)
lock_paths = dict(mm=config.LOCK_PATH, installer=upgrade.CACHE_DIR / upgrade.INSTALL_LOCK_NAME)
print(json.dumps(dict(
    utc=now.isoformat(), operator=os.environ.get('QUAL_OPERATOR', 'unrecorded'),
    device_label=os.environ.get('QUAL_DEVICE', 'unrecorded'),
    macos=platform.mac_ver()[0], python=sys.version, interpreter=sys.executable, prefix=str(prefix),
    binary=str(binary), binary_target=str(binary.resolve()),
    next_process_version=version_probe.stdout.strip(),
    version_probe=dict(argv=version_probe.args, exit=version_probe.returncode,
                       stdout=version_probe.stdout, stderr=version_probe.stderr),
    distribution_version=dist.version, install=dataclasses.asdict(install),
    main_package={key: main.get(key) for key in ('package', 'package_or_url', 'package_version', 'suffix', 'pinned')},
    direct_url=dict(url=direct.get('url'), vcs_info=direct.get('vcs_info')),
    prior_install_receipt='unknown; not recovered from shell history',
    installed_upgrade_sha256=hashlib.sha256(Path(upgrade.__file__).read_bytes()).hexdigest(),
    pipx=pipx, pipx_version=subprocess.run([pipx, '--version'], capture_output=True, text=True, timeout=10).stdout.strip() if pipx else None,
    pipx_home=str(prefix.parent.parent), pipx_bin_dir=str(bin_dir),
    exposed_destination=str(destination), destination_owned=destination_owned,
    recovery_command=recovery, config=cfg_view, cache_state=snap.state, cache=cache_view,
    locks={name: dict(path=str(path), **fingerprint(path)) for name, path in lock_paths.items()},
), indent=2))
```

<!-- helper: target.py -->
```python
import json
import subprocess
from datetime import datetime, timezone
from mind_meld import upgrade

def api(endpoint):
    result = subprocess.run(['gh', 'api', 'repos/kbitz/mind-meld/' + endpoint],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('target request failed: ' + endpoint)
    return json.loads(result.stdout)

tags = api('tags?per_page=100')
picked = upgrade._pick_latest_tag(tags)
if picked is None:
    raise RuntimeError('no stable non-local tag in first 100')
tag, version = picked
obj = api('git/ref/tags/' + tag)['object']
for _ in range(5):
    if obj['type'] == 'commit':
        break
    if obj['type'] != 'tag':
        raise RuntimeError('tag does not resolve to a commit')
    obj = api('git/tags/' + obj['sha'])['object']
else:
    raise RuntimeError('tag peeling limit')
latest = api('git/ref/heads/latest')['object']['sha']
release = api('releases/tags/' + tag)
result = dict(utc=datetime.now(timezone.utc).isoformat(), tag=tag, version=str(version),
              tag_commit=obj['sha'], latest_commit=latest,
              release_url=release['html_url'], published_at=release['published_at'],
              draft=release['draft'], prerelease=release['prerelease'])
result['agrees'] = (obj['sha'] == latest and release['tag_name'] == tag and
                    not release['draft'] and not release['prerelease'] and bool(release['published_at']))
print(json.dumps(result, indent=2))
raise SystemExit(0 if result['agrees'] else 1)
```

<!-- helper: probe.py -->
```python
import fcntl
import json
import os
import stat
import sys
from datetime import datetime, timezone

record = dict(observer_pid=os.getpid(), path=sys.argv[1] if len(sys.argv) > 1 else None)
try:
    if len(sys.argv) not in (2, 4):
        raise RuntimeError('usage: probe.py LOCK_PATH [BASELINE_DEVICE BASELINE_INODE]')
    baseline = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) == 4 else None
    record['baseline'] = list(baseline) if baseline else 'none supplied'
    path = sys.argv[1]
    before = os.stat(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        identity = (opened.st_dev, opened.st_ino)
        record.update(device=identity[0], inode=identity[1])
        if not stat.S_ISREG(opened.st_mode) or identity != (before.st_dev, before.st_ino):
            raise RuntimeError('not the original regular inode')
        if baseline is not None and identity != baseline:
            raise RuntimeError('baseline inode changed')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            record['verdict'] = 'available'
        except BlockingIOError:
            record['verdict'] = 'blocked'
    finally:
        os.close(fd)
    after = os.stat(path)
    if identity != (after.st_dev, after.st_ino):
        raise RuntimeError('inode changed during probe')
except (OSError, RuntimeError, ValueError) as error:
    record.update(verdict='inconclusive', error=type(error).__name__ + ': ' + str(error))
record['utc'] = datetime.now(timezone.utc).isoformat()
print(json.dumps(record))
raise SystemExit(0 if record['verdict'] != 'inconclusive' else 1)
```

<!-- helper: sample.py -->
```python
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

seconds = float(sys.argv[1])
needles = sys.argv[2:]
assert needles and all(needles), 'provide non-empty exact own binary/pipx paths or harmless marker'
deadline = time.monotonic() + seconds
previous = time.monotonic()
first = True
while time.monotonic() < deadline:
    try:
        result = subprocess.run(['/bin/ps', '-ax', '-o', 'pid=,ppid=,pgid=,lstart=,stat=,command='],
                                capture_output=True, text=True, timeout=2)
        ps_exit, stdout = result.returncode, result.stdout
    except subprocess.TimeoutExpired:
        ps_exit, stdout = 'timeout', ''
    now = time.monotonic()
    rows = []
    for line in stdout.splitlines():
        fields = line.split(None, 9)
        if len(fields) == 10 and any(needle in fields[9] for needle in needles):
            if 'sample.py' not in fields[9]:
                rows.append(dict(pid=int(fields[0]), ppid=int(fields[1]), pgid=int(fields[2]),
                                 start=' '.join(fields[3:8]), state=fields[8], argv=fields[9]))
    print(json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(), monotonic=now,
                         readiness='READY_SAMPLED_ONLY' if first else None,
                         interval_seconds=now-previous, ps_exit=ps_exit, rows=rows)), flush=True)
    first = False
    previous = now
    time.sleep(0.05)
print(json.dumps(dict(end='deadline; absence never proves no exec', utc=datetime.now(timezone.utc).isoformat())), flush=True)
```

<!-- helper: trial.py -->
```python
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

assert len(sys.argv) == 4 and Path(sys.argv[1]).is_absolute(), 'usage: trial.py ABSOLUTE_ARM_DIR ABSOLUTE_MM COMMAND'
out = Path(sys.argv[1])
argv = sys.argv[2:]
assert out.is_dir() and Path(argv[0]).is_absolute()
assert argv[1:] in (['push'], ['pull'], ['autopull'], ['autopush'], ['update'])
unsafe = [key for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP') if os.environ.get(key)]
assert not unsafe, 'unset ' + ', '.join(unsafe) + ' before a real trial'
mask = os.umask(0o077)
os.umask(mask)  # mm and pipx keep the operator's own umask

def private(name):
    return os.fdopen(os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')

def save(name, value):
    with private(name) as file:
        file.write(json.dumps(value, indent=2).encode())

start = time.monotonic()
with private('command.stdout') as stdout, private('command.stderr') as stderr:
    process = subprocess.Popen(argv, stdout=stdout, stderr=stderr)
    save('parent-start.json', dict(utc=datetime.now(timezone.utc).isoformat(),
                                 monotonic=start, pid=process.pid, observer_pid=os.getpid(),
                                 argv=argv, origin='controlled terminal', umask=oct(mask),
                                 env={key: os.environ.get(key) for key in
                                      ('PATH', 'PIPX_HOME', 'PIPX_BIN_DIR', 'PIPX_MAX_LOGS')}))
    print('mm running as PID', process.pid, '- do not press Ctrl-C; tail command.stderr', flush=True)
    interrupts = []
    while True:
        try:
            code = process.wait()
            break
        except KeyboardInterrupt:
            interrupts.append(datetime.now(timezone.utc).isoformat())
            print('Ctrl-C also reached mm; still waiting for it to exit', flush=True)
save('parent-exit.json', dict(utc=datetime.now(timezone.utc).isoformat(),
                            monotonic=time.monotonic(), elapsed_seconds=time.monotonic()-start,
                            pid=process.pid, exit=code, interrupts=interrupts))
print('mm parent exited:', process.pid, code, flush=True)
raise SystemExit(code)
```
