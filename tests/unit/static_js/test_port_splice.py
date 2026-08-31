"""
`GC.continuationPort` and the "filter plus two pushes" splice pattern every canvas's
`insertOnEdge` uses, checked against the real port tables — copied here rather than
loaded from `flow_builder.js`/`graph_designer.js`/`integrations.js`, because those three
files are a few thousand lines of DOM wiring each with no `module.exports`; the port
shapes below are transcribed from their `NODE_TYPES`/vocabulary tables (see
`static/js/flow_builder.js` around `NODE_TYPES`, `static/js/graph_designer.js` around
`portsOf`, and `static/js/integrations.js`'s `insertableTypes`) rather than executed
through them. `GC.continuationPort` itself, and the splice recipe, are exercised for
real — only the surrounding "which node has which ports" data is a fixture.
"""

from __future__ import annotations

import shutil

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is needed to execute the canvas connector runtime",
)


def _run(run_js, body: str) -> None:
    run_js(body, sources=("graph_canvas.js",))


class TestContinuationPort:
    def test_an_explicit_default_wins(self, run_js) -> None:
        _run(run_js, """
            const GC = window.GraphCanvas;
            assert.strictEqual(GC.continuationPort(["default", "error"]), "default");
        """)

    def test_done_wins_over_a_loop_body_port(self, run_js) -> None:
        _run(run_js, """
            const GC = window.GraphCanvas;
            // for_each / do_until: wiring the continuation to "body" would move it INTO
            // the loop rather than after it.
            assert.strictEqual(GC.continuationPort(["body", "done"]), "done");
        """)

    def test_falls_back_to_the_first_port_with_neither(self, run_js) -> None:
        _run(run_js, """
            const GC = window.GraphCanvas;
            assert.strictEqual(GC.continuationPort(["true", "false"]), "true", "If / Else");
            assert.strictEqual(GC.continuationPort(["else"]), "else", "a fresh Branch with no conditions yet");
        """)

    def test_no_way_out_is_null(self, run_js) -> None:
        _run(run_js, """
            const GC = window.GraphCanvas;
            assert.strictEqual(GC.continuationPort([]), null, "Start / End Flow / Goto");
            assert.strictEqual(GC.continuationPort(undefined), null);
            assert.strictEqual(GC.continuationPort([""]), null, "a Menu/Dropdown with no options yet");
        """)


class TestTheSpliceRecipe:
    """
    The generic shape every `insertOnEdge` follows: the original connector is removed by
    filtering, and exactly two new ones are pushed — never a mutation of the original
    plus one push, so a half-applied splice cannot exist.
    """

    _SPLICE_JS = """
        function spliceOnEdge(state, edgeId, node, onwardPort) {
            const edge = state.edges.find(function (e) { return e.id === edgeId; });
            state.nodes.push(node);
            state.edges = state.edges.filter(function (e) { return e.id !== edge.id; });
            state.edges.push({ id: edge.id + "-in", source: edge.source, source_port: edge.source_port, target: node.id });
            state.edges.push({ id: edge.id + "-out", source: node.id, source_port: onwardPort, target: edge.target });
            return edge;
        }
    """

    def test_the_original_is_gone_and_exactly_two_replace_it(self, run_js) -> None:
        _run(run_js, self._SPLICE_JS + """
            const state = {
                nodes: [{ id: "A" }, { id: "B" }],
                edges: [{ id: "e1", source: "A", source_port: "default", target: "B" }],
            };
            spliceOnEdge(state, "e1", { id: "N" }, "default");

            assert.strictEqual(state.edges.length, 2);
            assert.ok(!state.edges.some(function (e) { return e.id === "e1"; }), "the original connector is gone");
            assert.deepStrictEqual(
                state.edges.map(function (e) { return [e.source, e.source_port, e.target]; }),
                [["A", "default", "N"], ["N", "default", "B"]],
            );
        """)

    def test_the_sources_own_port_is_preserved_not_reset_to_default(self, run_js) -> None:
        _run(run_js, self._SPLICE_JS + """
            const state = {
                nodes: [{ id: "A" }, { id: "B" }],
                edges: [{ id: "e1", source: "A", source_port: "error", target: "B" }],
            };
            spliceOnEdge(state, "e1", { id: "N" }, "default");

            const incoming = state.edges.find(function (e) { return e.target === "N"; });
            assert.strictEqual(incoming.source_port, "error", "the failure leg stays the failure leg");
        """)

    def test_a_for_each_spliced_in_leaves_by_done_not_body(self, run_js) -> None:
        _run(run_js, self._SPLICE_JS + """
            const GC = window.GraphCanvas;
            const state = {
                nodes: [{ id: "A" }, { id: "B" }],
                edges: [{ id: "e1", source: "A", source_port: "default", target: "B" }],
            };
            const onward = GC.continuationPort(["body", "done"]);
            spliceOnEdge(state, "e1", { id: "loop" }, onward);

            const outgoing = state.edges.find(function (e) { return e.source === "loop"; });
            assert.strictEqual(outgoing.source_port, "done", "B runs once after the loop, not once per item");
        """)

    def test_splicing_twice_nests_correctly(self, run_js) -> None:
        _run(run_js, self._SPLICE_JS + """
            const state = {
                nodes: [{ id: "A" }, { id: "B" }],
                edges: [{ id: "e1", source: "A", source_port: "default", target: "B" }],
            };
            spliceOnEdge(state, "e1", { id: "N1" }, "default");
            const secondEdge = state.edges.find(function (e) { return e.source === "N1"; });
            spliceOnEdge(state, secondEdge.id, { id: "N2" }, "default");

            assert.strictEqual(state.edges.length, 3);
            const chain = [];
            let cursor = "A";
            for (let i = 0; i < 3; i++) {
                const leg = state.edges.find(function (e) { return e.source === cursor; });
                chain.push(leg.target);
                cursor = leg.target;
            }
            assert.deepStrictEqual(chain, ["N1", "N2", "B"], "A -> N1 -> N2 -> B");
        """)


class TestTheCatalogueFilter:
    """
    Reproduces the Flow Builder's `insertableTypes()` predicate — `type !== "start"` and
    `GC.continuationPort(outputs) !== null` — against a transcription of its `NODE_TYPES`
    port table, checking the exact exclusion list CANVAS_SELECTION.md names.
    """

    _NODE_TYPES_JS = """
        // Transcribed from static/js/flow_builder.js's NODE_TYPES: just the output
        // port names each type declares, which is all `insertableTypes` looks at.
        const NODE_TYPES = {
            start: [],
            if_else: ["true", "false"],
            goto: [],
            menu: [],
            dropdown: [],
            ask_input: ["default"],
            send_message: ["default"],
            ai_fallback: ["default"],
            send_email: ["default", "error"],
            run_graph: ["default", "error"],
            run_flow: ["default", "error"],
            create_file: ["default", "error"],
            download_file: ["default", "error"],
            end: [],
        };
    """

    def test_excludes_start_end_goto_menu_and_dropdown_offers_the_rest(self, run_js) -> None:
        _run(run_js, self._NODE_TYPES_JS + """
            const GC = window.GraphCanvas;
            const offered = Object.keys(NODE_TYPES).filter(function (type) {
                if (type === "start") return false;
                return !!GC.continuationPort(NODE_TYPES[type]);
            });

            const excluded = Object.keys(NODE_TYPES).filter(function (t) { return offered.indexOf(t) === -1; });
            assert.deepStrictEqual(excluded.sort(), ["dropdown", "end", "goto", "menu", "start"]);
            assert.strictEqual(offered.length, 9);
        """)
