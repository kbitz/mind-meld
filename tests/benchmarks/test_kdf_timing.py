"""Benchmark: key-derivation cost for encrypt+decrypt at scale.

Run manually. NOT run by pytest collection by default.

    python -m tests.benchmarks.test_kdf_timing

Prints median, p50, p95, p99, and total time for an N-file encrypt+decrypt
loop at production-ish Argon2 parameters. Used to measure the before/after
delta for Track 1C (Argon2 KDF caching). Numbers are machine-specific;
paste the output into the PR description, do not commit as a baseline.
"""

from __future__ import annotations

import os
import statistics
import sys
import time

# Allow running as a script from the repo root.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, os.path.join(_REPO, "src"))

from mind_meld.crypto import decrypt, encrypt, set_crypto_session

PASSPHRASE = "benchmark-passphrase-kdf-timing"
N_FILES = 100
FILE_SIZE = 4096  # bytes
MEMORY_KB = 65_536  # production default
# Fixed root_salt for benchmark determinism. In production this lives in
# mm-crypto-init. The benchmark doesn't need that level of plumbing.
BENCH_ROOT_SALT = bytes(range(16))


def _quantile(samples: list[float], p: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    idx = int(round((len(s) - 1) * p))
    return s[idx]


def main() -> None:
    print(f"Mind Meld KDF benchmark")
    print(f"  N_FILES={N_FILES}, FILE_SIZE={FILE_SIZE}B, MEMORY_KB={MEMORY_KB}")
    print()

    set_crypto_session(BENCH_ROOT_SALT, MEMORY_KB)

    plaintexts = [os.urandom(FILE_SIZE) for _ in range(N_FILES)]
    encrypt_times: list[float] = []
    decrypt_times: list[float] = []
    blobs: list[bytes] = []

    t_enc_start = time.perf_counter()
    for pt in plaintexts:
        t0 = time.perf_counter()
        blob = encrypt(pt, PASSPHRASE, memory_kb=MEMORY_KB)
        encrypt_times.append(time.perf_counter() - t0)
        blobs.append(blob)
    t_enc_total = time.perf_counter() - t_enc_start

    t_dec_start = time.perf_counter()
    for blob, pt in zip(blobs, plaintexts):
        t0 = time.perf_counter()
        out = decrypt(blob, PASSPHRASE, memory_kb=MEMORY_KB)
        decrypt_times.append(time.perf_counter() - t0)
        assert out == pt

    t_dec_total = time.perf_counter() - t_dec_start

    def report(name: str, samples: list[float], total: float) -> None:
        median = statistics.median(samples)
        p95 = _quantile(samples, 0.95)
        p99 = _quantile(samples, 0.99)
        print(
            f"  {name:8s} total={total*1000:8.1f}ms  "
            f"median={median*1000:7.2f}ms  "
            f"p95={p95*1000:7.2f}ms  "
            f"p99={p99*1000:7.2f}ms  "
            f"(per-op, N={len(samples)})"
        )

    report("encrypt", encrypt_times, t_enc_total)
    report("decrypt", decrypt_times, t_dec_total)

    print()
    print(
        f"  Total encrypt+decrypt: {(t_enc_total + t_dec_total):.2f}s for {N_FILES} files "
        f"(= {(t_enc_total + t_dec_total) * 10:.0f}s extrapolated to 1000 files)"
    )


if __name__ == "__main__":
    main()
