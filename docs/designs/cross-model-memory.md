# Cross-model memory

Status: shape approved by the owner on 2026-10-29; planned after 2.0 as
[docs/SPEC.md](../SPEC.md) O9. Implementation waits for the qualification
below. This design supersedes the cross-agent design in
[memory continuity](memory-continuity.md), which with the
[Codex memory contract](codex-memory-contract.md) stays as Track 68A evidence
(NO QUALIFIED ROUTE, measured on Codex 0.159.0).

## Goal

What one agent learned on any of the owner's machines can be found by the
other agents on every machine: Claude Code, Codex, and agents that read
`AGENTS.md`. Agents find memories by searching them. mm never copies a memory
into another agent's store.

## What changed since Track 68A

Observed on 2026-10-29 with codex-cli 0.164.0 on one Mac (read-only):

- `~/.codex/memories/` is plain markdown and covers every project for the user. It holds
  `memory_summary.md` (about 11 KB), `MEMORY.md` (about 224 KB, organized as
  `# Task Group: <project> <task>` sections), `raw_memories.md` (about
  486 KB), `rollout_summaries/` (about 120 timestamped files), `extensions/`
  and a `.git` directory that Codex commits to itself.
- Codex's consolidation job regenerates `MEMORY.md` and `memory_summary.md`
  wholesale on each machine, so syncing them as shared files would conflict
  on every pull.
- Claude project memory already syncs per project under `~/.claude/projects/`.

## Design

1. **Per-machine Codex memory copies.** Each machine publishes its own Codex
   memory files as a per-machine copy, with one writer per file. Peers receive
   the copies in an mm-owned directory outside every host's native store, so
   copies never conflict and Codex never sees foreign files in its own store.
   Forgetting follows the source machine: when its consolidation drops a
   memory, the next push publishes the smaller file and truth-based manifests
   carry the change. The exact file set is settled by the qualification;
   Codex's `.git` never syncs.
2. **"Search, don't read" pointers.** Static instruction files carry a few
   lines telling each agent where the other agents' memories are and to search
   them, never to load them whole: the owner's shared `AGENTS.md` (read by
   Codex, Cursor and Antigravity) and `CLAUDE.md`. The owner approves
   installing them once. mm never writes pointers into generated memory files
   (`MEMORY.md`, `memory_summary.md`); their native writers own them.
   - Claude to the others: Claude project memory under
     `~/.claude/projects/<project>/memory/`.
   - Codex to the others: this machine's `~/.codex/memories/` and the
     per-machine copies; search for the project's Task Group headings, and
     load at most `memory_summary.md`.
   - Size is the risk, not speed: one `MEMORY.md` is about 55k tokens, times
     the number of machines. The pointer says to search.
3. **Phase 2: a per-machine search index.** qmd (already installed on the
   owner's Mac) indexes the same markdown collections and serves search to
   every agent through its CLI or MCP. The markdown stays the store. The
   SQLite index is derived per machine, never synced, and rebuilt after
   `mm pull`. Admit it when grep proves too blunt in real use, or earlier if
   the owner accepts its dependencies (Bun or Node and embedding models);
   Arch Linux support is unverified. Phase 2 is not part of O9 acceptance.

Out of scope: feeding foreign files into Codex's own consolidation
(`rollout_summaries/` ingestion), writing into any host's native store,
syncing a search index, and Cursor's own memory store.

## Qualification

Two live checks, each in isolated homes (temporary `HOME` and `CODEX_HOME`,
never the owner's real stores), before implementation:

- **Q1, pointer recall.** A fresh Codex session follows an `AGENTS.md`
  pointer and finds a Claude-origin memory by search; a fresh Claude session
  follows a `CLAUDE.md` pointer and finds a Codex-origin memory. Neither
  loads `MEMORY.md` whole. Record which file Codex loads at startup
  (`memory_summary.md` or `MEMORY.md`).
- **Q2, quiet copies.** After Codex regenerates its memory files on one
  machine, a push publishes only that machine's copies, the peer applies
  them without conflict copies, and nothing is written into any native store.

The result, the observed host versions and the chosen file set are recorded
in this document. A failed check returns the design to the owner before any
implementation Track is carded.
