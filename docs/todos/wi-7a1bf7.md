### [review:files=src/mind_meld/syncthing.py|tests/test_syncthing_write.py] Test gap: Refuse an unreadable Ignore list before a join add
- **ID:** wi-7a1bf7
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Return a successful configuration response with missing or malformed Ignore data and check that no add is sent. Existing tests cover an HTTP failure and a valid Ignore entry.
- **Revisit:** The next Track that touches the cited paths can recheck the coverage and add the case if it is still missing.
- **Upstream severity:** INFORMATIONAL
- **Evidence:** Track 170A coverage audit at `ae32fe7` and pairing repair at `d03f9b9`.
- **Effort:** ? (owner triages in the roadmap)
