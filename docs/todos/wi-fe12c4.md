### [review:files=src/mind_meld/initflow.py|tests/test_initflow.py] Test gap: Persist retry authority before the initial pull
- **ID:** wi-fe12c4
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Interrupt a real init's first pull and assert that the saved managed identity and peer registrations are present before a later sync. The retry tests seed that state directly.
- **Revisit:** The next Track that touches the cited paths can recheck the coverage and add the case if it is still missing.
- **Upstream severity:** INFORMATIONAL
- **Evidence:** Track 170A coverage audit at `ae32fe7` and pairing repair at `d03f9b9`.
- **Effort:** ? (owner triages in the roadmap)
