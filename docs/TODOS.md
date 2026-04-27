# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

## Unprocessed

- **Parallelize blob fetches in `_download_and_apply` (cli.py:1320)** — measured root cause of `mm pull` slowness: iCloud lazy-materialization of cloud-only blobs (`st_blocks == 0`). On kb's 349C-kb-ms, 670/1449 blobs (46%) were cloud-only; sequential `backend.get()` measured 509–1050ms per blob, parallel-x8 measured 143ms per blob (**7.3× speedup**, fully network-bound — File Provider supports concurrent downloads natively). Push isn't affected because it only reads files it just wrote (always resident) and its own manifest. Fix: wrap the per-file `backend.get(bkey)` call in cli.py:1336 with `concurrent.futures.ThreadPoolExecutor(max_workers=8)` + `as_completed` (NOT `map` — one slow blob shouldn't gate the rest). Keep decrypt + `_apply_incoming_file` single-threaded (cheap, GIL-friendly, preserves the existing per-file try/except + `outcomes` dict semantics). Care: error/skip outcome ordering under reordering, and the progress bar's `_advance` callback must remain thread-safe (Rich `Progress.advance` is). Effort: M. [pull-perf 2026-04-27]

- **`mm init` should recommend pinning the storage `data/` dir against iCloud eviction** — same root cause as the parallelize-fetches TODO above. Even after parallelization, a freshly-set-up Mac will see slow first pulls until iCloud has materialized blobs. Two options, in increasing aggressiveness: (1) print a one-line `mm: notice:` after init success: `Tip: keep blobs local with: brctl download "<storage_path>/data" (or right-click → Keep Downloaded in Finder)`; (2) actually run `subprocess.run(["brctl", "download", str(data_dir)])` as a best-effort post-init step (silent on failure since brctl is macOS-specific). Option 1 is the safer first step — surfaces the knob without making decisions for the user. Should also add a section to README "Claude Code Integration" / FAQ. Effort: XS (option 1) / S (option 2). [pull-perf 2026-04-27]

