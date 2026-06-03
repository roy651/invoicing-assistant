# SPEC — `mail-evidence` (task 1.6.5)

Portable mail→evidence conditioning engine. Standalone installable package, shared by
**invoicing-assistant** and the future **task-management assistant**. This is the
conditioning stage between raw fetch and domain reasoning.

**Status:** spec for build. Library-first, `uv`/`pyproject`, ruff, same pre-commit
(gitleaks + `detect-private-key`) as the parent repo. No secrets; `state/` git-ignored.

---

## 0. Decisions to confirm before build

1. **Domain judgment is injected, not built in.** The package owns deterministic,
   header-based tiering and exposes `RelevanceJudge` + `ContactStore` as protocols.
   The relevance notion is **"work-related"** (broad, reusable), NOT "billable" — the
   billable determination is a downstream reasoning-core decision (1.7), never the
   gate. A broader net strengthens the never-miss-an-invoice inversion. The
   Sheets-backed contacts store lives in invoicing-assistant. The package never
   imports gspread/Google and never contains the word "billable".
2. **`thread_id` is References-chain-based**, not per-folder/per-UID. Required so a
   Sent reply threads with its INBOX parent. May be a fix to the existing imap-fetch.
3. **Workspace package now, own repo at Phase-2.** Develop as a path/workspace
   dependency; enforce portability with an import-guard test; split to its own repo
   at the Phase-2 boundary. (Alternative: split immediately — decide before build.)
4. **TODO(fact):** Asura's **Sent** folder name — config value, must be confirmed
   live before INBOX+Sent fetch is verified.

---

## 1. Boundary (the whole point)

Everything **domain-agnostic** lives in the package; every **domain judgment** is an
injected dependency.

| In the package (generic) | Injected by the consumer (domain) |
|---|---|
| IMAP fetch (read-only), INBOX+Sent, watermark, windowing, cap, batch iterator | "Is this thread **work-related**?" (`RelevanceJudge`) |
| Tier classification by **headers only** (T1/T2/T3) | Contacts persistence (`ContactStore`, e.g. Google Sheets) |
| Thread assembly (References chain), chronological | `bill_to` linkage, role→completion logic |
| In-thread dedup (no quote-strip) | The relevance prompt / model wiring |
| Unified `EvidenceRecord` / `Thread` schema (incl. `from_/to/cc`) | |

**Public API (§7):** `EvidenceRecord`, `Thread`, the fetch/assemble
entry points, and the `RelevanceJudge` / `ContactStore` protocols. Everything else is
private-by-convention.

---

## 2. Data model (`records.py`)

```python
@dataclass(frozen=True)
class EvidenceRecord:         # atomic unit: one email OR one transcript file
    id:            str
    thread_id:     str
    source:        Literal["email", "transcript"]
    date:          datetime
    # Email-sourced records populate from_/to/cc/subject (flat, first-class —
    # matches 1.7 UnifiedEvidence; cc MUST NOT be collapsed). Transcripts leave
    # from_=None, to=[], cc=[] and populate participants/filename instead.
    from_:         str | None
    to:            list[str]
    cc:            list[str]                # drives subcontractor-CC completion signal
    subject:       str | None
    participants:  list[str]                # speakers (transcript); convenience union (email)
    body_text:     str                      # full body, never quote-stripped (see §5)
    attachments_meta: list[AttachmentMeta]
    filename:      str | None               # transcript provenance

@dataclass(frozen=True)
class Thread:                 # the evidence UNIT (§6.5): one thread = one unit
    thread_id: str
    records:   list[EvidenceRecord]   # chronological
    tier:      Literal["T1", "T2", "T3"]
    relevance: RelevanceDecision | None   # set only for judged/promoted T2 threads
```

**§6.6 invariant:** `from_/to/cc` are first-class fields, never collapsed into
`participants`. The subcontractor-CC completion signal reads `cc` directly. This shape
is identical to 1.7's `UnifiedEvidence` — the package owns it; 1.7's `unify()` consumes
package output. `participants` stays a convenience union for generic consumers.

The package **owns** this schema. The transcripts adapter (which stays in
invoicing-assistant — it is not "mail") imports `EvidenceRecord` from here.

---

## 3. Pipeline

```
fetch(folders=[INBOX, Sent], window, cap)   →  raw messages (batched)
  → assemble_threads()                       →  Threads (cross-folder, References-chain)
  → dedup_in_thread()                         →  Threads, in-thread repeats removed
  → classify_tier(thread, contact_store)      →  T1 / T2 / T3
  → condition(thread, judge, contact_store)   →  keep T1; judge+promote T2; drop T3
  → emit Thread
```

Each stage is independently callable and unit-testable (library-first). A top-level
`run(config, judge, store) -> Iterator[list[Thread]]` ties them together and yields
**per batch** (see §6).

### 3.1 fetch (`fetch/imap.py`)
- Read-only: `EXAMINE` only; the write-command ban from the current imap-fetch carries
  over as a package test.
- **INBOX + Sent** (§6.3). Sent often holds completion statements ("final logo
  attached, invoice to follow"). Sent folder name is config (TODO §0.4).
- Emits raw messages with `id` (Message-ID), `References`/`In-Reply-To`, `from_/to/cc`,
  date, subject, body, attachment meta. No body mutation here.

### 3.2 assemble (`assemble.py`)
- Group by **References chain** (Message-ID ← In-Reply-To ← References), NOT folder.
  A Sent reply and its INBOX parent MUST land in the same `thread_id`.
- Order records chronologically. Transcript records are their own single-record
  thread unless a `thread_id` already matches an email thread.

### 3.3 dedup (`dedup.py`) — no quote-stripping (§6.4)
Rule: remove a quoted block **iff** it is an in-thread repeat; preserve everything else.

1. Identify quoted blocks in each body (`>`-prefix runs, and text under
   `On <date>, X wrote:` boilerplate).
2. A block is an **in-thread repeat** iff it corresponds to another record already in
   the same thread — matched **identity-first** (quote attribution resolves to a
   sibling Message-ID / sender+date), **content-hash fallback** when identity is
   unrecoverable.
3. Strip only in-thread repeats. Forwarded/external quoted history (no sibling match)
   is **kept verbatim** — for forwards / "added you to an existing thread", the quoted
   body is sometimes the *only* evidence.
4. Never strip original (non-quoted) content. Never dedup across threads.

### 3.4 tiering (`tiering.py`) — deterministic, headers only
Operates at the **thread** level, using participants/headers across all records:
- **T1** if any participant is a managed contact in `ContactStore`. → full evidence.
- **T3** (drop) **only** if every record carries a positive bulk-mail signal
  (`List-Unsubscribe`, `no-reply` sender, `Precedence: bulk`). Never on allowlist
  absence.
- **T2** otherwise (unknown-but-human). → relevance judge.

**Inversion invariant (§6.1):** a stale allowlist costs extra T2 judgments, never a
dropped thread. Allowlist-absence MUST NOT drop. Bulk-drop requires positive signals.

### 3.5 condition / promote (`promote.py` + `protocols.py`)
- T1 → keep as-is.
- T2 → call injected `judge.is_relevant(thread) -> RelevanceDecision`.
  - relevant → write `promote_emails` to `ContactStore` (`role="other"`,
    `source="auto"`, logged) and keep the thread; set `thread.relevance`.
  - not relevant → drop, logged. (Polish/later: age out unused auto-added contacts.)
- T3 → drop, logged.

```python
@dataclass(frozen=True)
class RelevanceDecision:
    relevant:       bool
    reason:         str
    promote_emails: list[str]

class RelevanceJudge(Protocol):
    def is_relevant(self, thread: Thread) -> RelevanceDecision: ...

class ContactStore(Protocol):
    def is_known(self, email: str) -> bool: ...
    def role_of(self, email: str) -> str | None: ...
    def add_auto(self, email: str, reason: str) -> None: ...   # role=other, source=auto
```

The package ships only a trivial deterministic default judge for tests (e.g.
keyword/always-relevant). The real LLM-backed judge is wired in invoicing-assistant.
The package has **zero** hard LLM/network dependency in unit tests.

---

## 4. Config (`config.py`)
- `folders`: `["INBOX", "<SENT TODO>"]`
- bulk-signal ruleset (headers list above) — overridable
- `window` (bounded, e.g. default 35 days) and `max_messages` cap
- `batch_size`

---

## 5. Large-delta guard (§6.7) — fetch never does one giant pull
- Fetch accepts a bounded `window` + `max_messages` cap.
- On cold-start / long gaps, iterate in **batches**; `run()` yields one batch of
  `Thread`s at a time.
- **Watermark** lives in git-ignored `state/` (carried from imap-fetch). The package
  does **not** auto-advance it. The consumer processes a batch, durably persists, then
  calls `commit_watermark(high_water)`. → a crash re-fetches at most one batch.

---

## 6. Module layout
```
mail_evidence/
  __init__.py        # public API re-exports
  records.py         # EvidenceRecord, Thread, AttachmentMeta, RelevanceDecision
  protocols.py       # RelevanceJudge, ContactStore
  fetch/
    imap.py          # read-only EXAMINE, INBOX+Sent, window/cap, batch iterator
    watermark.py     # state/ store, commit_watermark
  assemble.py        # References-chain threading, chronological
  dedup.py           # in-thread dedup, no quote-strip
  tiering.py         # header-based T1/T2/T3
  promote.py         # T2 judge + ContactStore promotion
  pipeline.py        # run(config, judge, store) -> Iterator[list[Thread]]
```

---

## 7. Migration from existing skills
- `skills/imap-fetch/` fetch logic + `Message` shape move into `mail_evidence`. Its
  verified behaviors become package tests: read-only/examine, write-command ban,
  Asura live-verify, watermark in `state/`. Add INBOX+Sent.
- `skills/transcripts/` **stays in invoicing-assistant** (not mail) but imports
  `EvidenceRecord` from `mail_evidence`. Its VTT/txt/md handling and date resolution
  are unchanged; it must populate the unified schema (`from_=None`, `to=[]`, `cc=[]`).
- If a Cowork SKILL.md wrapper is still wanted, it imports the library — no logic in
  the skill.
- **Status (post-1.6.6):** fetch logic + watermark + `EvidenceRecord` migrated; `skills/imap-fetch/`
  deleted. Its `cli.py`/`probe_connection.py` were **not** ported — the package has the fetch
  engine but no runner/CLI. A thin runner (fetch INBOX+Sent, print/export a batch, show/advance
  watermark, connection probe) is tracked as build-plan task **1.10**, required before Phase 2
  live fetch. The `Message` shape is gone — `EvidenceRecord` is the single email+transcript type.

---

## 8. Acceptance criteria (testable)
1. **Cross-folder threading:** a Sent reply and its INBOX parent share `thread_id`.
2. **Tiering inversion:** allowlist-absence alone never yields T3; T3 requires a
   positive bulk signal; any known participant → T1 (explicit tests for each).
3. **Dedup:** an in-thread reply-quote is stripped; a forwarded external quote is
   preserved verbatim (two fixtures). Original content never stripped.
4. **CC preserved (§6.6):** for `source=email`, `cc` is a populated first-class field,
   never folded into `participants`; transcript records have `from_=None`, `cc=[]`.
5. **Read-only:** IMAP write-command ban holds (carried test).
6. **Large-delta:** batch iterator + watermark; simulated mid-run crash re-fetches
   ≤ 1 batch; watermark advances only after `commit_watermark`.
7. **Injection / no domain leak:** `RelevanceJudge` and `ContactStore` are mockable;
   unit tests make **zero** network/LLM calls.
8. **Portability guard:** an import-linter (or test) **fails** if `mail_evidence`
   imports gspread / Google / any invoicing-assistant module, or contains the string
   "billable". Enforces §7 portability from the start.

---

## 9. Out of scope (explicit)
- Any "is this billable" / pricing / `bill_to` logic (→ invoicing-assistant, 1.7).
- Contacts **persistence** implementation (→ invoicing-assistant `ContactStore` impl).
- The relevance **prompt/model** (→ invoicing-assistant judge impl).
- Any write to a mailbox; any HTTP/FastAPI server.
- Aging-out of unused auto-contacts (polish, later).

---

## 10. Build rhythm
Opus authors/reviews; Sonnet executes the bounded modules above against §8. Review
each commit against its acceptance criteria; never relax an invariant to pass a check.
Independent of 1.7 — parallelizable.
