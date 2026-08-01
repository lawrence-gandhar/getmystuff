#!/usr/bin/env python3
"""
Audit the Pydantic schema layer.

Answers four questions, in the order the module-schemas skill acts on them:

  1. Which features have request/response payloads but no ``app/schemas/<feature>/``
     package at all?
  2. Which route handlers still read a raw payload (``request.form()``,
     ``request.json()``, ``form.get(...)``, ``query_params.get(...)``) instead of
     parsing it through a schema?
  3. Which schema classes exist but are not written up in
     ``documentations/SCHEMAS.md``?
  4. Which schema modules have no test file?

Nothing here edits anything. It prints a report and exits with a verdict code so
the skill knows what work is outstanding:

    0  clean — every feature covered, no raw reads, docs and tests in step
    1  a feature with payloads has no schemas package
    2  a route still reads a raw payload without a schema
    3  schemas exist that SCHEMAS.md does not document
    4  schema modules exist with no test file

The lowest-numbered non-zero condition wins, because that is the order they have
to be fixed in: a missing package before the wiring, the wiring before the docs.

Usage:
    python3 .claude/skills/module-schemas/scripts/audit_schemas.py [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set

REPO_ROOT = Path(__file__).resolve().parents[4]
APP = REPO_ROOT / "app"
SCHEMAS = APP / "schemas"
ROUTES = APP / "routes"
SERVICES = APP / "services"
MODELS = APP / "models"
DOCS = REPO_ROOT / "documentations" / "SCHEMAS.md"
TESTS = REPO_ROOT / "tests" / "unit" / "schemas"

# Shared schema modules that are not tied to one feature. They live at the top
# level of app/schemas/ the same way db_utils.py lives at the top level of db/.
SHARED_MODULES = {"base.py", "common.py"}

# Features that legitimately have no payload of their own: they render a page
# from the session user and take nothing from the request. Listing them here is a
# claim the audit re-checks on every run (see _has_payload) rather than a
# permanent exemption — if such a route grows a form, the audit fails.
NO_PAYLOAD_FEATURES = {"dashboard"}

# The raw-payload reads a route must not perform directly. Each is a place where
# untrusted input would otherwise reach a service unvalidated.
RAW_READ_PATTERNS = {
    "request.form()": re.compile(r"\brequest\.form\(\)"),
    "request.json()": re.compile(r"\brequest\.json\(\)"),
    "form.get(": re.compile(r"\bform\.get\("),
    "form.getall(": re.compile(r"\bform\.getall\("),
    "query_params.get(": re.compile(r"\bquery_params\.get\("),
}

# A route may hold a reference to the parsed form to hand to a schema. These
# lines are the sanctioned hand-off and are not counted as raw reads.
ALLOWED_LINE_PATTERNS = (
    re.compile(r"from_form\("),
    re.compile(r"from_request\("),
    re.compile(r"parse_form\("),
    re.compile(r"parse_json\("),
    re.compile(r"parse_query\("),
    re.compile(r"form_to_dict\("),
    re.compile(r"#\s*audit:\s*raw-payload-ok"),
)


@dataclass
class FeatureReport:
    name: str
    layers: Set[str] = field(default_factory=set)
    has_schemas: bool = False
    schema_modules: List[str] = field(default_factory=list)
    schema_classes: List[str] = field(default_factory=list)
    raw_reads: List[str] = field(default_factory=list)
    undocumented: List[str] = field(default_factory=list)
    untested_modules: List[str] = field(default_factory=list)

    @property
    def expects_schemas(self) -> bool:
        return "routes" in self.layers and self.name not in NO_PAYLOAD_FEATURES


def _feature_dirs(parent: Path) -> Set[str]:
    if not parent.is_dir():
        return set()
    return {
        child.name
        for child in parent.iterdir()
        if child.is_dir() and child.name != "__pycache__"
    }


def _python_files(directory: Path) -> List[Path]:
    return sorted(
        path
        for path in directory.rglob("*.py")
        if "__pycache__" not in path.parts and path.name != "__init__.py"
    )


def _schema_classes(path: Path) -> List[str]:
    """Every Pydantic-looking class defined in one schema module."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            names.append(node.name)
    return names


def _raw_reads(feature: str) -> List[str]:
    """Raw payload reads left in a feature's route modules."""
    route_dir = ROUTES / feature
    if not route_dir.is_dir():
        return []

    hits: List[str] = []
    for path in _python_files(route_dir):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if any(allowed.search(line) for allowed in ALLOWED_LINE_PATTERNS):
                continue
            for label, pattern in RAW_READ_PATTERNS.items():
                if pattern.search(line):
                    rel = path.relative_to(REPO_ROOT)
                    hits.append(f"{rel}:{number}  {label}  {line.strip()}")
                    break
    return hits


def _has_payload(feature: str) -> bool:
    """
    Does this feature's route layer take anything from the request at all?

    Used to re-check the NO_PAYLOAD_FEATURES claim: a feature listed there that
    grows a form stops being exempt automatically.
    """
    route_dir = ROUTES / feature
    if not route_dir.is_dir():
        return False

    for path in _python_files(route_dir):
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in RAW_READ_PATTERNS.values()):
            return True
        if "from_form(" in text or "from_request(" in text or "parse_query(" in text:
            return True
    return False


def _is_documented(class_name: str, doc_text: str) -> bool:
    """
    Is this class named anywhere in SCHEMAS.md?

    Tests the discovered name against the document rather than extracting names
    *from* the document with a pattern. An earlier version did the latter, keyed on
    a ``Schema``/``Request``/``Response`` suffix, and so reported every ``*View``
    and ``*Query`` class as undocumented no matter what the page said.

    The word boundary matters: without it ``FlowView`` would count as documenting
    ``Flow``.
    """
    return re.search(rf"\b{re.escape(class_name)}\b", doc_text) is not None


def _test_files() -> Set[str]:
    if not TESTS.is_dir():
        return set()
    return {path.name for path in TESTS.rglob("test_*.py")}


def build_report() -> Dict[str, object]:
    features = sorted(
        _feature_dirs(ROUTES) | _feature_dirs(SERVICES) | _feature_dirs(MODELS)
    )

    doc_text = DOCS.read_text(encoding="utf-8") if DOCS.exists() else ""
    docs_exist = DOCS.exists()
    test_names = _test_files()

    reports: List[FeatureReport] = []
    for name in features:
        report = FeatureReport(name=name)
        for layer, parent in (
            ("routes", ROUTES),
            ("services", SERVICES),
            ("models", MODELS),
        ):
            if (parent / name).is_dir():
                report.layers.add(layer)

        schema_dir = SCHEMAS / name
        report.has_schemas = schema_dir.is_dir()
        if report.has_schemas:
            for path in _python_files(schema_dir):
                report.schema_modules.append(str(path.relative_to(REPO_ROOT)))
                classes = _schema_classes(path)
                report.schema_classes.extend(classes)
                report.undocumented.extend(
                    name_ for name_ in classes if not _is_documented(name_, doc_text)
                )
                expected_test = f"test_{path.stem}.py"
                if expected_test not in test_names:
                    report.untested_modules.append(str(path.relative_to(REPO_ROOT)))

        report.raw_reads = _raw_reads(name)
        reports.append(report)

    shared: List[Dict[str, object]] = []
    for module in sorted(SHARED_MODULES):
        path = SCHEMAS / module
        if not path.exists():
            shared.append({"module": f"app/schemas/{module}", "present": False})
            continue
        classes = _schema_classes(path)
        shared.append(
            {
                "module": f"app/schemas/{module}",
                "present": True,
                "classes": classes,
                "undocumented": [
                    c for c in classes if not _is_documented(c, doc_text)
                ],
                "tested": f"test_{path.stem}.py" in test_names,
            }
        )

    missing_packages = [
        r.name for r in reports if r.expects_schemas and not r.has_schemas
    ]
    # A feature exempted as payload-free that has since grown a payload.
    broken_exemptions = [
        name
        for name in sorted(NO_PAYLOAD_FEATURES)
        if _has_payload(name) and not (SCHEMAS / name).is_dir()
    ]
    raw_reads = {r.name: r.raw_reads for r in reports if r.raw_reads}
    undocumented = {r.name: r.undocumented for r in reports if r.undocumented}
    undocumented.update(
        {
            entry["module"]: entry["undocumented"]
            for entry in shared
            if entry.get("undocumented")
        }
    )
    untested = {r.name: r.untested_modules for r in reports if r.untested_modules}
    untested.update(
        {
            entry["module"]: [entry["module"]]
            for entry in shared
            if entry.get("present") and not entry.get("tested")
        }
    )

    missing_shared = [e["module"] for e in shared if not e["present"]]

    if missing_packages or broken_exemptions or missing_shared:
        verdict = 1
    elif raw_reads:
        verdict = 2
    elif not docs_exist or undocumented:
        verdict = 3
    elif untested:
        verdict = 4
    else:
        verdict = 0

    return {
        "verdict": verdict,
        "docs_present": docs_exist,
        "features": [
            {
                "name": r.name,
                "layers": sorted(r.layers),
                "expects_schemas": r.expects_schemas,
                "has_schemas": r.has_schemas,
                "schema_modules": r.schema_modules,
                "schema_class_count": len(r.schema_classes),
                "raw_read_count": len(r.raw_reads),
                "undocumented": r.undocumented,
                "untested_modules": r.untested_modules,
            }
            for r in reports
        ],
        "shared": shared,
        "missing_packages": missing_packages,
        "missing_shared": missing_shared,
        "broken_exemptions": broken_exemptions,
        "raw_reads": raw_reads,
        "undocumented": undocumented,
        "untested": untested,
    }


VERDICTS = {
    0: "clean — every feature covered, no raw payload reads, docs and tests in step",
    1: "a feature with payloads has no schemas package",
    2: "a route still reads a raw payload without a schema",
    3: "schemas exist that SCHEMAS.md does not document",
    4: "schema modules exist with no test file",
}


def print_report(report: Dict[str, object]) -> None:
    print("# Schema layer audit\n")

    print("| Feature | Layers | Schemas | Classes | Raw reads | Undocumented | Untested |")
    print("|---|---|---|---|---|---|---|")
    for feature in report["features"]:  # type: ignore[index]
        expected = "yes" if feature["expects_schemas"] else "n/a"
        present = "yes" if feature["has_schemas"] else f"**NO** ({expected} expected)"
        print(
            f"| {feature['name']} | {','.join(feature['layers'])} | {present} "
            f"| {feature['schema_class_count']} | {feature['raw_read_count']} "
            f"| {len(feature['undocumented'])} | {len(feature['untested_modules'])} |"
        )

    print("\n## Shared modules\n")
    for entry in report["shared"]:  # type: ignore[index]
        if not entry["present"]:
            print(f"- **MISSING** `{entry['module']}`")
        else:
            print(
                f"- `{entry['module']}` — {len(entry['classes'])} classes, "
                f"{len(entry['undocumented'])} undocumented, "
                f"tested: {'yes' if entry['tested'] else 'no'}"
            )

    for title, key in (
        ("Features with payloads and no schemas package", "missing_packages"),
        ("Missing shared modules", "missing_shared"),
        ("Payload-free exemptions that now have a payload", "broken_exemptions"),
    ):
        values = report[key]  # type: ignore[index]
        if values:
            print(f"\n## {title}\n")
            for value in values:
                print(f"- {value}")

    if report["raw_reads"]:  # type: ignore[index]
        print("\n## Raw payload reads still in routes\n")
        for feature, hits in report["raw_reads"].items():  # type: ignore[union-attr]
            print(f"### {feature}\n")
            for hit in hits:
                print(f"- {hit}")
            print()

    if report["undocumented"]:  # type: ignore[index]
        print("\n## Schema classes missing from documentations/SCHEMAS.md\n")
        for feature, names in report["undocumented"].items():  # type: ignore[union-attr]
            print(f"- **{feature}**: {', '.join(names)}")

    if report["untested"]:  # type: ignore[index]
        print("\n## Schema modules with no test file\n")
        for feature, modules in report["untested"].items():  # type: ignore[union-attr]
            for module in modules:
                print(f"- {module}")

    verdict = report["verdict"]
    print(f"\n## Verdict: {verdict} — {VERDICTS[verdict]}")  # type: ignore[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit the raw report as JSON instead"
    )
    args = parser.parse_args()

    report = build_report()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)

    return int(report["verdict"])  # type: ignore[arg-type]


if __name__ == "__main__":
    sys.exit(main())
