---
name: enterprise-code-audit
description: Run a full enterprise-grade audit of the codebase — security vulnerabilities, logic loopholes/edge cases, complexity & maintainability hotspots, correctness bugs, and prioritized code-review feedback — then produce a serious, ranked report. Use when the user asks to "audit", "security review", "find vulnerabilities/loopholes", "code review the whole codebase", or wants an enterprise-grade quality/security assessment.
---

# Enterprise Code Audit

Produce a **serious, evidence-based, prioritized audit** of the whole codebase suitable for
an enterprise stakeholder (engineering lead, security, or an auditor). Every finding must be
grounded in a specific file and line and be independently verifiable. **No hand-waving, no
inflated severities, no findings you have not confirmed against the source.**

## Operating principles

1. **Evidence or it doesn't ship.** Each finding cites `path:line`, quotes the relevant code,
   and states a concrete failure scenario (inputs/state → wrong or unsafe outcome). If you
   cannot construct the scenario, downgrade to an observation or drop it.
2. **Adversarial verification.** Before reporting a Critical/High finding, actively try to
   refute it: is the input actually attacker-controlled? Is there validation upstream? Is the
   path reachable with the app's real config? Keep only what survives.
3. **Severity is earned, not assumed.** Use the rubric below. Reserve Critical/High for issues
   with real, demonstrable impact given how the system is actually deployed.
4. **Respect the architecture's intent.** Read `CLAUDE.md` first — it encodes deliberate
   decisions (e.g. the Fabric isolation boundary, read-only ClickHouse contract, app-layer
   view gating). Judge the code against those invariants; flag where code *violates* a stated
   invariant, and separately flag where an invariant itself is weak.
5. **Cover the whole system, not just the happy path** — web tier, auth/session, data access,
   the sync pipeline, task queue/broker, config/secrets, containers/networking, and tests.

## Scope & dimensions

Audit across these dimensions (each becomes a section in the report):

- **A. Security vulnerabilities** — authn/authz gaps, injection (SQL/command/template), SSRF,
  secret handling, CSRF, session/cookie safety, security headers/CSP, deserialization, SSTI,
  open redirects, privilege boundaries, task-queue/broker exposure, container/network isolation.
  Map each to a **CWE** and, where relevant, **OWASP Top 10 (2021)**.
- **B. Logic loopholes & correctness** — auth bypass paths, race conditions/TOCTOU, error
  handling that fails open, unvalidated trust boundaries, idempotency gaps, edge cases
  (empty/oversized/malformed input), incorrect state transitions.
- **C. Complexity & maintainability** — oversized/over-branching functions, duplication,
  leaky abstractions, tight coupling, dead code, unclear ownership, config sprawl.
- **D. Reliability & performance** — blocking I/O on the event loop, unbounded queries/results,
  missing timeouts/retries/back-pressure, connection lifecycle, N+1, memory blowups.
- **E. Code review & conventions** — type-hint gaps (project mandates `mypy --strict`), lint
  debt, naming, docstrings, testability, adherence to the repo's own stated conventions.
- **F. Testing & supply chain** — coverage of security-critical paths, mocked boundaries,
  dependency pinning and known-risky deps, Dockerfile/base-image hygiene.

## Method

1. **Map.** Read `CLAUDE.md`, `README.md`, `docker-compose.yml`, `pyproject.toml`,
   `requirements.txt`, and list the source tree. Note the deployment topology and trust
   boundaries. **Never open `.env`** (only `.env.example`).
2. **Divide & review.** Review each dimension across the tree. For a large codebase, dispatch
   **parallel subagents**, one per dimension or per subsystem (`app/` web tier, `sync/`
   pipeline), each returning structured findings. Give each agent the architecture context and
   the finding template.
3. **Deduplicate & verify.** Merge findings, drop duplicates, then adversarially verify every
   Critical/High against the actual source before it makes the report.
4. **Rank & synthesize.** Order by severity then blast radius. Write the report.

## Severity rubric

| Severity | Meaning |
|---|---|
| **Critical** | Remotely exploitable or data-exposing with no/low precondition; or guaranteed data loss/corruption. Fix immediately. |
| **High** | Exploitable with a realistic precondition (e.g. an authenticated low-priv user), or a correctness bug that fails open / leaks data. Fix before release. |
| **Medium** | Real weakness with limited impact or requiring an unlikely precondition; hardening gap; risky pattern that will bite under change. |
| **Low** | Minor/defense-in-depth, maintainability, or convention issue. |
| **Info** | Observation, positive note, or accepted trade-off worth recording. |

## Finding template

```
### [SEVERITY] <short title>
- **ID:** AUD-<NN>
- **Dimension:** A–F
- **Location:** `path:line` (+ related refs)
- **CWE / OWASP:** e.g. CWE-89 / A03:2021-Injection  (security findings only)
- **Evidence:** the specific code, quoted.
- **Impact / scenario:** concrete inputs/state → the bad outcome.
- **Confidence:** Confirmed | Plausible.
- **Recommendation:** the specific fix (and a code sketch if non-obvious).
```

## Report format (write to `docs/audits/CODE_AUDIT_<YYYY-MM-DD>.md`)

1. **Title + metadata** — repo, commit/branch, date, auditor, scope, method.
2. **Executive summary** — 1 short paragraph + a severity tally table (counts per level).
3. **Findings register** — a table (ID · severity · dimension · title · location), most severe first.
4. **Detailed findings** — one entry per finding using the template, ordered by severity.
5. **Positive observations** — what is genuinely well done (security controls that work, good boundaries).
6. **Prioritized remediation plan** — Now / Next / Later, mapped to finding IDs.
7. **Appendix** — scope covered, files reviewed, limitations, and what was explicitly out of scope.

Tone: precise, factual, and sober. This is a document an enterprise will act on and archive —
write it accordingly. Do not fabricate CWE numbers or invent issues to pad the count; a short,
correct report beats a long, speculative one.
