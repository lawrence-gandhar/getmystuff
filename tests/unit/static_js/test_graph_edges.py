"""
`static/js/graph_edges.js` — the connector runtime `graph_designer.js` and
`flow_builder.js` both delegate to, exercised through `window.GraphEdges.create(config)`
against a stubbed DOM.

This is the coverage CANVAS_SELECTION.md's "Tests" section describes: before the
extraction the anchor cache, the frame, the bend gesture and the group-move callbacks
were closures inside two files whose only export is `{ init }`, and nothing here could
be reached at all. As a factory taking an injected config, it can.

`_HARNESS` builds one connector runtime per test with real DOM nodes standing in for
the elements `nodeElementId`/`edgeElementId`/`edgePathId` name, and the same config
shape `flow_builder.js` and `graph_designer.js` pass in production — see their own
`window.GraphEdges.create({...})` calls for the config this mirrors.
"""

from __future__ import annotations

import shutil

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is needed to execute the canvas connector runtime",
)

_HARNESS = """
function makeConfig(overrides) {
    const state = { nodes: [], edges: [], backEdges: {}, dragging: null, layout: "auto" };
    const wrapperEl = new FakeElement("div");
    wrapperEl._rect = { left: 0, top: 0, width: 4000, height: 4000 };
    wrapperEl.scrollLeft = 0;
    wrapperEl.scrollTop = 0;

    let edgesRef = null;
    const flashes = [];
    const calls = { renderEdge: [], markDirty: 0, updateLayoutButton: 0, fitCanvas: 0 };

    const config = Object.assign({
        state: state,
        wrapperEl: wrapperEl,
        chromePrefix: "fb",
        chromeYOffset: 0,
        buttonGapPx: 11,
        nodeElementId: function (id) { return "node-" + id; },
        edgeElementId: function (id) { return "edge-group-" + id; },
        edgePathId: function (id) { return "edge-" + id; },
        edgeRoute: function (edge) {
            const from = edgesRef.portAnchor(edge.source, null);
            const to = edgesRef.portAnchor(edge.target, null);
            return window.GraphCanvas.waypointPoints(from, to, window.GraphCanvas.waypointsOf(edge));
        },
        renderEdge: function (edge) {
            calls.renderEdge.push(edge.id);
            buildEdgeChromeDom(document, config.chromePrefix, edge.id,
                { waypoints: (edge.waypoints || []).length });
        },
        metrics: function () { return { stepWidth: 140 }; },
        sourceAnchor: function (edge) { return edgesRef.portAnchor(edge.source, null); },
        targetAnchor: function (edge) { return edgesRef.portAnchor(edge.target, null); },
        isRoutable: function (edge) { return !edge.derived; },
        isBusy: function () { return false; },
        travelled: function (dx, dy) { return Math.max(Math.abs(dx), Math.abs(dy)); },
        thresholdPx: 3,
        maxWaypoints: 4,
        waypointGrabPx: 8,
        waypointSnapPx: 6,
        waypointDiscardPx: 6,
        flash: function (message) { flashes.push(message); },
        markDirty: function () { calls.markDirty++; },
        updateLayoutButton: function () { calls.updateLayoutButton++; },
        fitCanvas: function () { calls.fitCanvas++; },
        detachNodeDrag: function () {},
        getSelection: function () { return null; },
    }, overrides || {});

    const edges = window.GraphEdges.create(config);
    edgesRef = edges;
    return { state: state, config: config, edges: edges, flashes: flashes, calls: calls };
}

function addNode(state, id, x, y) {
    state.nodes.push({ id: id, position: { x: x, y: y } });
    buildNodeDom(document, id, { left: x, top: y, width: 0, height: 0 });
}

function addEdge(state, id, source, target, extra) {
    const edge = Object.assign({ id: id, source: source, target: target }, extra || {});
    state.edges.push(edge);
    buildEdgeChromeDom(document, "fb", id, { waypoints: (edge.waypoints || []).length });
    return edge;
}
"""


def _run(run_js, body: str) -> None:
    run_js(_HARNESS + "\n" + body)


class TestAnchorCache:
    def test_stationary_end_measured_once_moving_end_by_arithmetic(self, run_js) -> None:
        _run(run_js, """
            const h = makeConfig();
            addNode(h.state, "A", 0, 0);
            addNode(h.state, "B", 200, 0);
            addEdge(h.state, "e1", "A", "B");

            const nodeAEl = document.getElementById("node-A");
            const nodeBEl = document.getElementById("node-B");
            let countA = 0, countB = 0;
            const origA = nodeAEl.getBoundingClientRect.bind(nodeAEl);
            nodeAEl.getBoundingClientRect = function () { countA++; return origA(); };
            const origB = nodeBEl.getBoundingClientRect.bind(nodeBEl);
            nodeBEl.getBoundingClientRect = function () { countB++; return origB(); };

            const chrome = h.edges.chromeForMovingNodes(["B"]);
            h.edges.beginDragAnchors(["B"], chrome);
            assert.strictEqual(countA, 1, "the stationary end is measured while priming");
            assert.strictEqual(countB, 1, "the moving end is measured while priming");

            h.edges.portAnchor("A", null);
            h.edges.portAnchor("A", null);
            assert.strictEqual(countA, 1, "a stationary end is never measured again");

            const before = h.edges.portAnchor("B", null);
            assert.deepStrictEqual(before, { x: 200, y: 0 });

            h.state.nodes.find(function (n) { return n.id === "B"; }).position = { x: 260, y: 40 };
            const after = h.edges.portAnchor("B", null);
            assert.deepStrictEqual(after, { x: 260, y: 40 }, "a moving end is tracked by arithmetic");
            assert.strictEqual(countB, 1, "a moving end is never measured again after the first frame");

            h.edges.endDragAnchors();
        """)

    def test_node_deleted_mid_drag_yields_null_not_a_throw(self, run_js) -> None:
        _run(run_js, """
            const h = makeConfig();
            addNode(h.state, "A", 0, 0);
            addNode(h.state, "B", 200, 0);
            addEdge(h.state, "e1", "A", "B");

            const chrome = h.edges.chromeForMovingNodes(["B"]);
            h.edges.beginDragAnchors(["B"], chrome);
            h.state.nodes = h.state.nodes.filter(function (n) { return n.id !== "B"; });

            assert.strictEqual(h.edges.portAnchor("B", null), null);
            h.edges.endDragAnchors();
        """)

    def test_return_lane_memoises_inside_a_gesture_and_re_reads_outside_one(self, run_js) -> None:
        _run(run_js, """
            const h = makeConfig();
            addNode(h.state, "A", 0, 0);
            addNode(h.state, "B", 200, 0);

            assert.strictEqual(h.edges.returnLaneX(), 200 + 140 + 40);

            h.state.nodes.find(function (n) { return n.id === "B"; }).position.x = 500;
            assert.strictEqual(h.edges.returnLaneX(), 500 + 140 + 40, "re-measures with no gesture running");

            h.edges.beginDragAnchors(["A"], []);
            const inside1 = h.edges.returnLaneX();
            assert.strictEqual(inside1, 500 + 140 + 40);

            h.state.nodes.find(function (n) { return n.id === "B"; }).position.x = 900;
            assert.strictEqual(h.edges.returnLaneX(), inside1, "memoised for the rest of the gesture");

            h.edges.invalidateLane();
            assert.strictEqual(h.edges.returnLaneX(), 900 + 140 + 40, "recomputed once invalidated");

            h.edges.endDragAnchors();
            assert.strictEqual(h.edges.returnLaneX(), 900 + 140 + 40, "plain measurement resumes once the gesture ends");
        """)


class TestEdgeChrome:
    def test_the_cross_and_the_plus_sit_22px_apart_and_the_y_offset_moves_both(self, run_js) -> None:
        _run(run_js, """
            function midpoint(chromeYOffset) {
                const h = makeConfig({ chromeYOffset: chromeYOffset });
                addNode(h.state, "A", 0, 0);
                addNode(h.state, "B", 0, 100);
                const edge = addEdge(h.state, "e1", "A", "B");
                const chrome = h.edges.edgeChrome(edge);
                h.edges.updateEdgeGeometry(chrome);

                function xy(el) {
                    const m = el.getAttribute("transform").match(/translate\\(([-\\d.]+),([-\\d.]+)\\)/);
                    return { x: parseFloat(m[1]), y: parseFloat(m[2]) };
                }
                return { del: xy(chrome.deleteBtn), ins: xy(chrome.insertBtn) };
            }

            const flat = midpoint(0);
            assert.strictEqual(flat.ins.x - flat.del.x, 22, "the delete and insert buttons sit 22px apart");
            assert.strictEqual(flat.del.y, 50);

            const shifted = midpoint(10);
            assert.strictEqual(shifted.del.y - flat.del.y, 10, "chromeYOffset moves the delete button");
            assert.strictEqual(shifted.ins.y - flat.ins.y, 10, "chromeYOffset moves the insert button the same amount");
        """)

    def test_a_connector_whose_group_has_gone_is_rerendered_not_skipped(self, run_js) -> None:
        _run(run_js, """
            const h = makeConfig();
            addNode(h.state, "A", 0, 0);
            addNode(h.state, "B", 0, 100);
            const edge = addEdge(h.state, "e1", "A", "B");
            const chrome = h.edges.edgeChrome(edge);

            chrome.group.isConnected = false;
            h.edges.updateEdgeGeometry(chrome);

            assert.deepStrictEqual(h.calls.renderEdge, ["e1"], "a missing group triggers a re-render");
            assert.ok(chrome.group.isConnected, "the record now points at the freshly rendered group");
        """)


class TestBends:
    def test_a_press_that_never_moved_leaves_no_bend(self, run_js) -> None:
        _run(run_js, """
            const h = makeConfig();
            addNode(h.state, "A", 0, 0);
            addNode(h.state, "B", 0, 100);
            const edge = addEdge(h.state, "e1", "A", "B");

            h.edges.startBend("e1", Object.assign(noEvent(), { button: 0, clientX: 0, clientY: 50 }));
            assert.strictEqual((edge.waypoints || []).length, 1, "a press always tries a bend where it landed");

            document.dispatch("mouseup", {});
            assert.strictEqual(edge.waypoints, undefined, "a press-and-release is a click and leaves no bend");
        """)

    def test_a_press_and_drag_past_the_threshold_keeps_a_bend(self, run_js) -> None:
        _run(run_js, """
            const h = makeConfig();
            addNode(h.state, "A", 0, 0);
            addNode(h.state, "B", 0, 100);
            const edge = addEdge(h.state, "e1", "A", "B");

            h.edges.startBend("e1", Object.assign(noEvent(), { button: 0, clientX: 0, clientY: 50 }));
            document.dispatch("mousemove", { clientX: 30, clientY: 50 });
            frames.flush();
            document.dispatch("mouseup", {});

            assert.strictEqual(edge.waypoints.length, 1, "the bend survives release");
            assert.strictEqual(edge.waypoints[0].x, 30, "it follows the cursor rather than snapping back onto the line");
        """)

    def test_a_fifth_bend_is_refused_with_its_sentence(self, run_js) -> None:
        _run(run_js, """
            const h = makeConfig();
            addNode(h.state, "A", 0, 0);
            addNode(h.state, "B", 0, 500);
            const edge = addEdge(h.state, "e1", "A", "B", { waypoints: [
                { x: 10, y: 10 }, { x: 10, y: 20 }, { x: 10, y: 30 }, { x: 10, y: 40 },
            ] });

            h.edges.startBend("e1", Object.assign(noEvent(), { button: 0, clientX: 200, clientY: 250 }));

            assert.strictEqual(edge.waypoints.length, 4, "no fifth bend is added");
            assert.strictEqual(h.flashes.length, 1);
            assert.ok(/at most 4 bends/.test(h.flashes[0]), h.flashes[0]);
        """)

    def test_a_derived_connector_cannot_be_bent(self, run_js) -> None:
        _run(run_js, """
            const h = makeConfig();
            addNode(h.state, "A", 0, 0);
            addNode(h.state, "B", 0, 100);
            const edge = addEdge(h.state, "e1", "A", "B", { derived: true });

            h.edges.startBend("e1", Object.assign(noEvent(), { button: 0, clientX: 0, clientY: 50 }));
            assert.strictEqual(edge.waypoints, undefined, "a derived connector is never given a bend");

            document.dispatch("mousemove", { clientX: 50, clientY: 50 });
            document.dispatch("mouseup", {});
            assert.strictEqual(edge.waypoints, undefined, "no gesture was started for it to finish");
        """)


class TestGroupMove:
    def test_carries_bends_of_connectors_whose_both_ends_move(self, run_js) -> None:
        _run(run_js, """
            const h = makeConfig();
            addNode(h.state, "A", 0, 0);
            addNode(h.state, "B", 100, 0);
            addNode(h.state, "C", 300, 0);
            const eAB = addEdge(h.state, "eAB", "A", "B", { waypoints: [{ x: 50, y: 20 }] });
            const eBC = addEdge(h.state, "eBC", "B", "C", { waypoints: [{ x: 200, y: 20 }] });

            h.edges.onGroupMoveBegin(["A", "B"]);
            h.edges.onGroupMoveFrame(["A", "B"], 10, 5);
            assert.deepStrictEqual(eAB.waypoints[0], { x: 60, y: 25 }, "a bend whose both ends move travels with them");
            assert.deepStrictEqual(eBC.waypoints[0], { x: 200, y: 20 }, "a bend whose other end stayed put is left alone");

            h.edges.onGroupMoveFrame(["A", "B"], 20, 5);
            assert.deepStrictEqual(eAB.waypoints[0], { x: 70, y: 25 }, "each frame is the captured start plus the delta");

            h.edges.onGroupMoveFrame(["A", "B"], -1000, -1000);
            assert.deepStrictEqual(eAB.waypoints[0], { x: 0, y: 0 }, "a carried bend clamps at zero");

            h.edges.onGroupMoveEnd(["A", "B"], true);
            assert.strictEqual(h.calls.fitCanvas, 1);
            assert.strictEqual(h.calls.markDirty, 1);

            h.edges.onGroupMoveBegin(["A", "B"]);
            h.edges.onGroupMoveEnd(["A", "B"], false);
            assert.strictEqual(h.calls.fitCanvas, 1, "an abandoned move commits nothing");
            assert.strictEqual(h.calls.markDirty, 1);
        """)
