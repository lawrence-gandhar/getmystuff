#!/usr/bin/env python3
"""
Turn a test run into a durable, timestamped record and enforce the coverage
ratchet.

Everything deterministic about a run lives here rather than in the model's head:
the timestamps, the junit/coverage parsing, and the baseline comparison. A model
asked to eyeball a terminal dump and write down a percentage will eventually
write down the wrong one, and a coverage report nobody can trust is worse than
none. This script is the source of truth; the skill's prose only interprets it.

Outputs
  tests/reports/<UTC timestamp>-report.md   full detail for this run
  tests/reports/HISTORY.md                  one appended row per run
  tests/coverage_baseline.json              the ratchet's stored high-water mark

Exit codes
  0  suite passed, coverage did not regress, every source file was measured
  1  coverage regressed below the stored baseline
  2  the suite itself failed (or produced no usable artifacts)
  3  source files exist that coverage never measured (see the report)

Standard library only — this runs on the host, which is Python 3.10.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Floating-point coverage percentages wobble in the last decimal between runs on
# identical code. Only a drop larger than this counts as a real regression.
REGRESSION_EPSILON = 0.01

# Failure tracebacks are quoted in full up to this length; beyond it the middle
# is elided so one enormous assertion diff cannot bury the rest of the report.
MAX_TRACEBACK_CHARS = 3000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def git(*args: str, cwd: Path) -> str:
    """Best-effort git query; returns "unknown" outside a repo."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def compress_ranges(numbers: List[int]) -> str:
    """[1,2,3,7,9,10] -> '1-3, 7, 9-10' so a gap table stays readable."""
    if not numbers:
        return ""

    ordered = sorted(set(numbers))
    spans: List[Tuple[int, int]] = []
    start = previous = ordered[0]

    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        spans.append((start, previous))
        start = previous = value
    spans.append((start, previous))

    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


def elide(text: str, limit: int = MAX_TRACEBACK_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    omitted = len(text) - limit
    return f"{head}\n\n... [{omitted} characters omitted] ...\n\n{tail}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
# pytest's junit-xml carries neither the source location nor the exception class
# as attributes — both only exist inside the traceback text, whose final frame
# reads "path/to/test.py:42: AssertionError". Without parsing it the report can
# only say "failure", which is not enough to act on months later.
_TRACEBACK_TAIL = re.compile(
    r"^(?P<file>[^\s:][^:\n]*\.py):(?P<line>\d+):\s*(?P<exc>[A-Za-z_][\w.]*)?",
    re.MULTILINE,
)


def locate_failure(detail: str, message: str) -> Tuple[str, str, str]:
    """
    Recover (file, line, exception type) for a failed test.

    Falls back to the exception name leading the junit ``message`` attribute
    (pytest formats it as "AssertionError: ...") when there is no usable frame.
    """
    file_name = line_number = exception = ""

    matches = list(_TRACEBACK_TAIL.finditer(detail or ""))
    if matches:
        # The last frame is the one that actually raised.
        last = matches[-1]
        file_name = last.group("file")
        line_number = last.group("line")
        exception = last.group("exc") or ""

    if not exception:
        head = (message or "").split(":", 1)[0].strip()
        if head and head.replace(".", "").replace("_", "").isalnum():
            exception = head

    return file_name, line_number, exception or "failure"


def parse_junit(path: Path) -> Dict[str, Any]:
    """Extract totals and per-failure detail from pytest's junit-xml."""
    if not path.is_file():
        return {
            "available": False,
            "tests": 0,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "passed": 0,
            "duration": 0.0,
            "problems": [],
        }

    root = ElementTree.parse(path).getroot()
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]

    tests = failures = errors = skipped = 0
    duration = 0.0
    problems: List[Dict[str, str]] = []

    for suite in suites:
        tests += int(suite.get("tests", 0))
        failures += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
        duration += float(suite.get("time", 0.0))

        for case in suite.iter("testcase"):
            for kind in ("failure", "error"):
                node = case.find(kind)
                if node is None:
                    continue

                classname = case.get("classname", "")
                name = case.get("name", "")
                message = (node.get("message", "") or "").strip()
                detail = (node.text or "").strip()

                found_file, found_line, exception = locate_failure(detail, message)

                problems.append(
                    {
                        "kind": kind,
                        "test_id": f"{classname}::{name}" if classname else name,
                        "file": (
                            case.get("file")
                            or found_file
                            or classname.replace(".", "/") + ".py"
                        ),
                        "line": case.get("line") or found_line,
                        "type": node.get("type") or exception,
                        "message": message,
                        "detail": detail,
                    }
                )

    return {
        "available": True,
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "passed": tests - failures - errors - skipped,
        "duration": duration,
        "problems": problems,
    }


def parse_coverage(path: Path) -> Dict[str, Any]:
    """Extract the total percentage and per-file gaps from coverage's json."""
    if not path.is_file():
        return {"available": False, "total": 0.0, "files": {}}

    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)

    files: Dict[str, Dict[str, Any]] = {}
    for filename, entry in raw.get("files", {}).items():
        summary = entry.get("summary", {})
        files[filename] = {
            "percent": round(float(summary.get("percent_covered", 0.0)), 2),
            "statements": int(summary.get("num_statements", 0)),
            "missing": int(summary.get("missing_lines", 0)),
            "missing_lines": entry.get("missing_lines", []) or [],
        }

    return {
        "available": True,
        "total": round(float(raw.get("totals", {}).get("percent_covered", 0.0)), 2),
        "statements": int(raw.get("totals", {}).get("num_statements", 0)),
        "covered": int(raw.get("totals", {}).get("covered_lines", 0)),
        "files": files,
    }


def discover_source_files(repo_root: Path) -> List[str]:
    """
    Every source file that *should* be measured, found on disk.

    This exists because coverage.py cannot be trusted to find them all. Its
    scan for never-imported files only walks directories that are real packages,
    and `app/services`, `app/models`, `app/utils` and `app/schemas` have no
    `__init__.py`. A brand-new module dropped into one of those and imported by
    nothing is therefore absent from coverage's report entirely — not listed at
    0%, simply invisible, so it drags the percentage down by exactly nothing.

    That is precisely the case this skill exists to catch, so the work-list is
    taken from the filesystem rather than from the coverage data.
    """
    found: List[str] = []

    main_py = repo_root / "main.py"
    if main_py.is_file():
        found.append("main.py")

    app_dir = repo_root / "app"
    if app_dir.is_dir():
        for path in app_dir.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            found.append(str(path.relative_to(repo_root)))

    return sorted(found)


def count_statements(path: Path) -> int:
    """Rough executable-statement count, used only to size an unmeasured file."""
    try:
        import ast

        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError, ValueError):
        return 0

    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt) and not isinstance(node, (ast.Expr, ast.Pass))
    )


def load_baseline(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def build_report(
    *,
    now_utc: datetime,
    now_local: datetime,
    junit: Dict[str, Any],
    coverage: Dict[str, Any],
    baseline: Optional[Dict[str, Any]],
    new_modules: List[str],
    untracked: List[str],
    verdict: str,
    delta: Optional[float],
    repo_root: Path,
    python_version: str,
) -> str:
    lines: List[str] = []
    add = lines.append

    suite_ok = junit["failures"] == 0 and junit["errors"] == 0 and junit["available"]
    status_word = "PASSED" if suite_ok else "FAILED"

    add(f"# Test run — {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    add("")
    add(f"**Suite: {status_word}**  |  **Coverage: {coverage['total']:.2f}%**  |  **{verdict}**")
    add("")

    # --- When / where -------------------------------------------------------
    add("## Run details")
    add("")
    add("| | |")
    add("|---|---|")
    add(f"| Started (UTC) | `{now_utc.isoformat(timespec='seconds')}` |")
    add(f"| Started (local) | `{now_local.isoformat(timespec='seconds')}` |")
    add(f"| Duration | {junit['duration']:.2f}s |")
    add(f"| Git commit | `{git('rev-parse', '--short', 'HEAD', cwd=repo_root)}` |")
    add(f"| Git branch | `{git('rev-parse', '--abbrev-ref', 'HEAD', cwd=repo_root)}` |")
    add(f"| Python | {python_version} |")
    add("| Runtime | `docker compose exec app` |")
    add("| Test database | in-memory SQLite |")
    add("")

    # --- Totals -------------------------------------------------------------
    add("## Results")
    add("")
    add("| Outcome | Count |")
    add("|---|---:|")
    add(f"| Passed | {junit['passed']} |")
    add(f"| Failed | {junit['failures']} |")
    add(f"| Errors | {junit['errors']} |")
    add(f"| Skipped | {junit['skipped']} |")
    add(f"| **Total** | **{junit['tests']}** |")
    add("")

    # --- Coverage -----------------------------------------------------------
    previous = baseline.get("total_coverage") if baseline else None
    add("## Coverage")
    add("")
    add("| | |")
    add("|---|---:|")
    add(f"| This run | {coverage['total']:.2f}% |")
    add(f"| Baseline | {'—' if previous is None else f'{previous:.2f}%'} |")
    if delta is not None:
        add(f"| Change | {delta:+.2f} pp |")
    add(f"| Statements covered | {coverage.get('covered', 0)} / {coverage.get('statements', 0)} |")
    add(f"| Files measured | {len(coverage['files'])} |")
    add(f"| Files unmeasured | {len(untracked)} |")
    add(f"| Remaining to 100% | {100 - coverage['total']:.2f} pp |")
    add(f"| Verdict | {verdict} |")
    add("")
    if untracked:
        add(
            f"> The percentage above covers only the {len(coverage['files'])} files "
            f"coverage could see. {len(untracked)} source file(s) were never "
            "imported by anything and are absent from the measurement entirely — "
            "they are listed below and are **completely untested**. Until that "
            "list is empty the headline number is an overstatement."
        )
        add("")

    # --- Unmeasured ---------------------------------------------------------
    add("## Unmeasured source files")
    add("")
    if not untracked:
        add("None — every source file on disk was measured.")
        add("")
    else:
        add(
            "Found on disk but missing from the coverage report, because nothing "
            "imports them. These do not lower the percentage, which is exactly "
            "why they are called out separately: they are the blind spot."
        )
        add("")
        add("| File | Statements (approx.) |")
        add("|---|---:|")
        for name in untracked:
            add(f"| `{name}` | {count_statements(repo_root / name)} |")
        add("")

    # --- Failures -----------------------------------------------------------
    add("## Failures")
    add("")
    if not junit["problems"]:
        add("None — every test passed.")
        add("")
    else:
        add(
            f"{len(junit['problems'])} test(s) did not pass. Each is recorded with "
            "the exception it raised so the history stays diagnosable after the "
            "fact."
        )
        add("")
        for index, problem in enumerate(junit["problems"], start=1):
            location = problem["file"]
            if problem["line"]:
                location = f"{location}:{problem['line']}"

            add(f"### {index}. `{problem['test_id']}`")
            add("")
            add(f"- **Kind**: {problem['kind']}")
            add(f"- **Location**: `{location}`")
            add(f"- **Exception**: `{problem['type']}`")
            add(f"- **Message**: {problem['message'] or '_(none)_'}")
            add("")
            if problem["detail"]:
                add("<details><summary>Traceback</summary>")
                add("")
                add("```text")
                add(elide(problem["detail"]))
                add("```")
                add("")
                add("</details>")
                add("")
            add("- **Root cause**: _to be filled in by the reviewing agent_")
            add("- **Fix**: _to be filled in by the reviewing agent_")
            add("")

    # --- New modules --------------------------------------------------------
    add("## New modules this run")
    add("")
    if new_modules:
        add("Present in this run but absent from the previous baseline:")
        add("")
        for name in sorted(new_modules):
            percent = coverage["files"].get(name, {}).get("percent", 0.0)
            add(f"- `{name}` — {percent:.2f}% covered")
        add("")
    else:
        add("None.")
        add("")

    # --- Gaps ---------------------------------------------------------------
    gaps = [
        (name, data)
        for name, data in coverage["files"].items()
        if data["percent"] < 100.0
    ]
    gaps.sort(key=lambda item: (-item[1]["missing"], item[0]))

    add("## Coverage gaps")
    add("")
    if not gaps:
        add("None — every measured file is at 100%.")
        add("")
    else:
        add(
            f"{len(gaps)} file(s) below 100%, ordered by how many uncovered "
            "statements they carry. This is the work queue for the next run."
        )
        add("")
        add("| File | Coverage | Missing | Uncovered lines |")
        add("|---|---:|---:|---|")
        for name, data in gaps:
            ranges = compress_ranges(data["missing_lines"])
            if len(ranges) > 90:
                ranges = ranges[:87] + "..."
            add(f"| `{name}` | {data['percent']:.1f}% | {data['missing']} | {ranges or '—'} |")
        add("")

    return "\n".join(lines) + "\n"


def history_row(
    *,
    now_utc: datetime,
    junit: Dict[str, Any],
    coverage: Dict[str, Any],
    delta: Optional[float],
    verdict: str,
    report_name: str,
) -> str:
    suite_ok = junit["failures"] == 0 and junit["errors"] == 0 and junit["available"]
    return (
        f"| {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC "
        f"| {'pass' if suite_ok else 'FAIL'} "
        f"| {junit['passed']} "
        f"| {junit['failures'] + junit['errors']} "
        f"| {junit['skipped']} "
        f"| {coverage['total']:.2f}% "
        f"| {'—' if delta is None else f'{delta:+.2f}'} "
        f"| {verdict} "
        f"| [report]({report_name}) |"
    )


HISTORY_HEADER = """# Test run history

Append-only. One row per invocation of the full-test-coverage skill, newest at
the bottom. Coverage is measured over all of `app/` and `main.py` with nothing
excluded, and the stored baseline may never decrease — see
[TESTING.md](../../documentations/TESTING.md).

| When | Suite | Passed | Failed | Skipped | Coverage | Change | Verdict | Detail |
|---|---|---:|---:|---:|---:|---:|---|---|
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[4],
        help="Repository root (default: inferred from this script's location).",
    )
    parser.add_argument(
        "--tests-exit-code",
        type=int,
        default=0,
        help="Exit code pytest returned, so a failing suite is recorded as such.",
    )
    parser.add_argument(
        "--python-version", default="3.12 (container)", help="Reported in the run details."
    )
    parser.add_argument(
        "--no-baseline-update",
        action="store_true",
        help="Write the report but leave the stored baseline untouched.",
    )
    args = parser.parse_args()

    repo_root: Path = args.repo_root
    report_dir = repo_root / "tests" / "reports"
    baseline_path = repo_root / "tests" / "coverage_baseline.json"
    report_dir.mkdir(parents=True, exist_ok=True)

    junit = parse_junit(report_dir / ".junit.xml")
    coverage = parse_coverage(report_dir / ".coverage.json")
    baseline = load_baseline(baseline_path)

    if not junit["available"] and not coverage["available"]:
        print(
            "ERROR: neither tests/reports/.junit.xml nor .coverage.json exists. "
            "The suite did not run — check the pytest output above.",
            file=sys.stderr,
        )
        return 2

    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()

    suite_ok = junit["failures"] == 0 and junit["errors"] == 0 and args.tests_exit_code == 0

    previous_total = baseline.get("total_coverage") if baseline else None
    delta = None if previous_total is None else round(coverage["total"] - previous_total, 2)

    # Source files on disk that coverage never reported. Taken from the
    # filesystem because coverage cannot see never-imported modules in the
    # namespace-package directories (app/services, app/models, app/utils,
    # app/schemas) — see discover_source_files().
    discovered = discover_source_files(repo_root)
    untracked = [name for name in discovered if name not in coverage["files"]]

    new_modules = []
    if baseline and baseline.get("per_file"):
        known = set(baseline["per_file"])
        new_modules = [
            name for name in list(coverage["files"]) + untracked if name not in known
        ]

    # --- Verdict ------------------------------------------------------------
    regressed = (
        previous_total is not None
        and coverage["total"] < previous_total - REGRESSION_EPSILON
    )

    if not suite_ok:
        verdict = "suite failed — baseline not updated"
    elif regressed:
        verdict = f"REGRESSION — coverage fell {abs(delta):.2f} pp below the baseline"
    elif untracked:
        verdict = f"{len(untracked)} source file(s) unmeasured — coverage is incomplete"
    elif previous_total is None:
        verdict = "baseline established"
    elif delta and delta > 0:
        verdict = "improved"
    else:
        verdict = "held"

    # --- Write --------------------------------------------------------------
    stamp = now_utc.strftime("%Y-%m-%dT%H-%M-%SZ")
    report_name = f"{stamp}-report.md"
    report_path = report_dir / report_name

    report_path.write_text(
        build_report(
            now_utc=now_utc,
            now_local=now_local,
            junit=junit,
            coverage=coverage,
            baseline=baseline,
            new_modules=new_modules,
            untracked=untracked,
            verdict=verdict,
            delta=delta,
            repo_root=repo_root,
            python_version=args.python_version,
        ),
        encoding="utf-8",
    )

    history_path = report_dir / "HISTORY.md"
    if not history_path.is_file():
        history_path.write_text(HISTORY_HEADER, encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(
            history_row(
                now_utc=now_utc,
                junit=junit,
                coverage=coverage,
                delta=delta,
                verdict=verdict,
                report_name=report_name,
            )
            + "\n"
        )

    # The baseline only ever moves on a green run that did not regress. Storing a
    # number from a failing suite would let a broken run lower the bar.
    should_update = suite_ok and not regressed and not args.no_baseline_update
    if should_update:
        baseline_path.write_text(
            json.dumps(
                {
                    "total_coverage": coverage["total"],
                    "updated_utc": now_utc.isoformat(timespec="seconds"),
                    "commit": git("rev-parse", "--short", "HEAD", cwd=repo_root),
                    "tests": junit["tests"],
                    "unmeasured": untracked,
                    # Unmeasured files are recorded at 0.0 so they stop being
                    # reported as "new" on every subsequent run. They keep
                    # showing up under "unmeasured" until they are tested or
                    # deleted, which is the signal that actually matters.
                    "per_file": dict(
                        sorted(
                            {
                                **{name: 0.0 for name in untracked},
                                **{
                                    name: data["percent"]
                                    for name, data in coverage["files"].items()
                                },
                            }.items()
                        )
                    ),
                },
                indent=2,
                sort_keys=False,
            )
            + "\n",
            encoding="utf-8",
        )

    # --- Console summary ----------------------------------------------------
    print()
    print(f"  Report    {report_path.relative_to(repo_root)}")
    print(f"  History   {history_path.relative_to(repo_root)}")
    print(f"  Suite     {junit['passed']} passed, {junit['failures']} failed, "
          f"{junit['errors']} errors, {junit['skipped']} skipped")
    print(f"  Coverage  {coverage['total']:.2f}%"
          + ("" if delta is None else f"  ({delta:+.2f} pp)"))
    print(f"  Verdict   {verdict}")
    if new_modules:
        print(f"  New       {len(new_modules)} module(s) not in the previous baseline")
    if untracked:
        print(f"  UNMEASURED {len(untracked)} source file(s) never imported "
              "— completely untested:")
        for name in untracked[:10]:
            print(f"              {name}")
        if len(untracked) > 10:
            print(f"              ... and {len(untracked) - 10} more")
    if should_update:
        print(f"  Baseline  updated -> {coverage['total']:.2f}%")
    else:
        print("  Baseline  unchanged")
    print()

    if regressed:
        return 1
    if not suite_ok:
        return 2
    if untracked:
        # Not a pass. A file nothing imports contributes nothing to the
        # percentage, so a green run with unmeasured files would otherwise look
        # identical to a genuinely complete one.
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
