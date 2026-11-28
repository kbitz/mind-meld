### [review:files=src/mind_meld/cli.py] Test gap: Check the prior event blob read limit during attended push
- **ID:** wi-cfb1fe
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Exercise an attended push with multiple unavailable prior day revisions and a refused fallback.
- **Hypothesis (untested):** Use synthetic prior event files and count bounded object reads. Verify the receipt remains conservative and the command still completes.
- **Upstream severity:** INFORMATIONAL
- **Found in:** src/mind_meld/cli.py, `_read_published_event_blob`
- **Evidence:** Bounded decryption and a successful one-blob fallback have direct assertions. The coverage audit found no attended run checking exhaustion after a refused fallback.
- **Context:** Track 171A review at `d1d0dd4` (2026-11-27). Revisit when a track next changes the published event comparison.
- **Effort:** ? (user triages in /roadmap)
