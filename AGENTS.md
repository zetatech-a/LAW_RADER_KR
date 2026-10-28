# AGENTS.md — LAW RADER (LAW_RADER_KR)

## Mission

Monitor 11 Korean financial-regulation sources (FSC, FSS, PIPC, regulatory-interpretation portal, National Assembly pending bills) every 15 minutes on weekdays and email new posts with body, attachments, and an AI 3-line summary (excerpt fallback when AI fails).

Detailed behavior, configuration, and known limits live in **`README.md`** (sections: 모니터링 대상, 제안이유 상태, 상세 재조회 큐, 동작 방식, 주요 설정값, 알려진 한계, 프로젝트 구조). Read the relevant section before changing a module.

## Commands (run before every PR)

```bash
pip install -r requirements.txt pytest
python -m pytest -q
python -m src.main --dry-run --no-llm                 # collect only: no email, no state change
python -m src.main --dry-run --debug --only fss_press # one source, verbose
```

- Never run `python -m src.main` **without** `--dry-run` in a dev or agent session: it sends real email and mutates `state/seen.json`.
- Tests must not touch the network; use fixtures under `tests/fixtures/`.

## Non-negotiable invariants

### New-post detection and state
- The first run for a source records a baseline only and sends nothing. Never send historical backlog.
- Never backfill past `seen` items automatically; backfill is a separate explicit task.
- `state/seen.json` is written atomically and committed by the workflow. Never hand-edit it.
- Respect `enabled: false` and `--only`: never contact a source the operator excluded, including the background re-fetch queue. Skip the re-fetch queue when SMTP is unavailable.

### Assembly bill details (`ProposalContentStatus`)
- Keep the four states distinct: `AVAILABLE` / `PENDING` (normal response, proposal text not yet published) / `ERROR` / `UNKNOWN`. Counting `PENDING` as failure, or downgrading an `ERROR` condition to `PENDING`, hides real breakage.
- Before adopting proposal text, the bill ID from the form, the request URL, and the final response URL must all match. If evidence disagrees, do not send the request; mark `ERROR`.
- Keep the detail-stage circuit breaker (consecutive-failure cap and time budget). On trip, remaining bills become `ERROR` without requests and email still goes out.

### LLM (Gemini)
- Email must always go out. Any summary failure falls back to the cleaned excerpt.
- Failure classes: `TRANSIENT` → bounded retry; `AUTH` / `RATE_LIMIT` / `BAD_REQUEST` / `MODEL_UNAVAILABLE` / `INTERNAL` → stop remaining LLM calls for this run; `CONTENT` → isolate to that post/batch only.
- Do not switch models on transient errors. Switch to `llm.fallback_models` only on model-unavailable (404 family), and on HTTP 503 only after that model's retries are exhausted.
- Model selection order: `GEMINI_MODEL` > `MODEL` > `config.yaml` `llm.model` > code default. Do not hardcode a model anywhere else.
- Keep the shared deadline `llm.total_budget_sec` across the general and assembly stages. Long-running work must stay well inside the 15-minute schedule.
- Keep the AI-accuracy disclaimer in the email footer.

### Network etiquette
- Keep request rates conservative (`fetch.delay_sec`) and per-source caps (`max_new_per_source`).
- Connection failures are isolated per source (overseas runner IPs are sometimes blocked by `fsc.go.kr`, `better.fsc.go.kr`, `open.assembly.go.kr`). Never turn one source's timeout into a global failure.

### Secrets and personal data
- Never put recipients in `config.yaml`; use the `MAIL_TO` secret (sent as BCC).
- Never commit secrets, `.env`, or raw HAR captures (`tests/fixtures/_*.raw.har` contain cookies and tokens). Commit only sanitized fixtures.
- Replace real personal contact details in captured fixtures with fake values.

### Unverified sources
- `pipc_notice` and `pipc_press` have not been verified against live HTML. Do not describe them as verified; follow README "PIPC 두 소스" before changing them.

## Definition of done

- `python -m pytest -q` passes.
- A parser change comes with a fixture-based regression test.
- README is updated when behavior, configuration keys, or limits change.
- No unrelated refactors in the same PR. PR description follows `.github/pull_request_template.md`.

## Language

- Code, identifiers, commit messages: English. README, comments, and email text: Korean (existing style).

## Lessons (append when an agent repeats a mistake)

<!-- 형식: - YYYY-MM-DD: <하지 말 것 / 대신 할 것> -->
