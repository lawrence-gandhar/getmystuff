"""
Fixture shared by every static-JS test: run a snippet of real source under Node.

`graph_canvas.js`, `graph_edges.js` and `graph_insert.js` are browser scripts with no
`module.exports`, so they cannot be `import`-ed — they are read as text, concatenated with
a DOM stub and the caller's assertions, and executed with `node`, exactly as
`tests/unit/services/chatbot/test_widget_markdown.py` already does for the widget's
renderer. Skipped when Node is absent, for the same reason that file is.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_STATIC_JS = Path(__file__).resolve().parents[3] / "static" / "js"
_DOM_STUB = (Path(__file__).parent / "support" / "dom_stub.js").read_text()


def _source(*names: str) -> str:
    return "\n".join((_STATIC_JS / name).read_text() for name in names)


@pytest.fixture
def run_js():
    """
    Run `snippet` after the DOM stub and the given real source files.

    `window`/`document`/`requestAnimationFrame` must exist before `graph_canvas.js`
    assigns `window.GraphCanvas` at load — the stub sets those up. A thrown assertion
    (Node's own `assert`) exits non-zero, surfaced as a readable failure rather than a
    silent pass, the same contract `_render` uses in the widget test.
    """

    def _run(snippet: str, sources: tuple[str, ...] = ("graph_canvas.js", "graph_edges.js")) -> str:
        script = (
            "const assert = require('assert');\n"
            + _DOM_STUB
            + "\n"
            + "global.document = createDocument();\n"
            + "global.window = { document: global.document, innerWidth: 1200, innerHeight: 800 };\n"
            + "Object.assign(global.window, makeEventTarget());\n"
            + "const frames = createFrameQueue();\n"
            + "global.requestAnimationFrame = frames.requestAnimationFrame;\n"
            + "global.cancelAnimationFrame = frames.cancelAnimationFrame;\n"
            + "\n"
            + _source(*sources)
            + "\n"
            + snippet
        )
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise AssertionError(f"JS snippet failed:\n{result.stderr.strip()}")
        return result.stdout

    return _run
