### [review:files=src/mind_meld/meshflow.py|tests/test_mesh.py] Test gap: Use a clear result in the same retirement pass
- **ID:** wi-a0a7c5
- **Test gap:** no demonstrated defect; filed under `test-gaps=record`
- **Description:** Queue a retired device behind a fleet-only introducer, then exercise confirmed clear and HTTP 500. Verify deletion only with the inactive live introducer. The current tests cover the two behaviors separately.
- **Revisit:** The next Track that touches the cited paths can recheck the coverage and add the case if it is still missing.
- **Upstream severity:** INFORMATIONAL
- **Evidence:** Track 170A coverage audit at `ae32fe7` and pairing repair at `d03f9b9`.
- **Effort:** ? (owner triages in the roadmap)
