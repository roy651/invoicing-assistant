#!/usr/bin/env python
"""Throwaway triage helper: one compact line per conditioned work thread, so the
reasoner can map a noisy month cheaply and then drill into the billable candidates.

Usage: uv run python scripts/thread_index.py <run_root>
"""

import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (
    "skills/invoicing",
    "skills/mail-evidence",
    "skills/transcripts",
    "morning-bridge",
):
    sys.path.insert(0, str(_ROOT / _p))

from invoicing_rules.phase2 import condition_corpus  # noqa: E402

# Domains that signal billable client work (agency, end-clients, direct clients).
WORK_DOMAINS = (
    "sprigconsulting",
    "vergemedical",
    "rovosystem",
    "apreohealth",
    "rhythmedix",
    "ivory",
    "atacor",
    "phagenesis",
    "lunair",
    "turncare",
    "vdyne",
    "sensoils",
    "hallura",
    "egisystems",
    "ydm.co.il",
)
INTERNAL = ("ula.co.il", "avigail.studio", "avigailstudio")


def _domain(addr: str) -> str:
    return addr.split("@")[-1].lower() if addr and "@" in addr else (addr or "").lower()


def main(root):
    emails = Path(root) / "emails"
    evidence = condition_corpus(emails)
    threads = defaultdict(list)
    for r in evidence:
        threads[r.thread_id].append(r)

    ordered = sorted(threads.values(), key=lambda recs: min(r.date for r in recs))
    work, internal, noise = [], [], []
    for i, recs in enumerate(ordered, 1):
        recs.sort(key=lambda r: r.date)
        addrs = set()
        for r in recs:
            addrs.add(_domain(r.from_ or ""))
            addrs.update(_domain(a) for a in (r.to or []))
        doms = {d for d in addrs if d}
        subj = next((r.subject for r in recs if r.subject), "(no subject)")
        d0 = recs[0].date.strftime("%m-%d")
        d1 = recs[-1].date.strftime("%m-%d")
        ext = sorted(
            d for d in doms if not any(d.endswith(x) or x in d for x in INTERNAL)
        )
        line = f"WT{i:<3} {d0}>{d1} {len(recs)}m [{','.join(ext)[:46]:46}] {subj[:70]}"
        if any(any(w in d for w in WORK_DOMAINS) for d in doms):
            work.append(line)
        elif doms <= {d for d in doms if any(x in d for x in INTERNAL)}:
            internal.append(line)
        else:
            noise.append(line)

    print(f"### WORK-DOMAIN THREADS ({len(work)}) ###")
    print("\n".join(work))
    print(f"\n### INTERNAL-ONLY THREADS ({len(internal)}) ###")
    print("\n".join(internal))
    print(f"\n### OTHER/NOISE THREADS ({len(noise)}) ###")
    print("\n".join(noise))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else _ROOT / "fixtures/runs/2026-06")
