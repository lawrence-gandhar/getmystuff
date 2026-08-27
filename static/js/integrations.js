/**
 * The workflow canvas: draw steps, connect them, set them up, run them and watch.
 *
 * `window.GraphCanvas` must already be loaded — this reads it at module scope. Those 242
 * stateless lines (geometry, path building, anchors, the escaping trio, the id generator)
 * are shared with the Graph Designer canvas **unforked**, because they are the part of a
 * canvas that has nothing to do with what the canvas is for.
 *
 * Four decisions worth knowing about before changing anything here.
 *
 * **The vocabulary comes from the server.** Step types, their ports, the operators, the
 * transforms and the caps are all read out of `#intVocabulary`, which the server built
 * from `flow_rules` — the same module the validator uses. This is the deliberate
 * improvement over `graph_designer.js`, which keeps a hardcoded `PORTS` table: a palette
 * that offers a step type the validator refuses, or a port it does not know, is a form
 * that can only be filled in wrongly. Adding a step type touches no JavaScript.
 *
 * **A refusal is data, not a status code.** Save, Publish and Run answer 200 with
 * `{ok: false, error, node_id}`, so the page holding somebody's unsaved work is never
 * replaced by an error page, and the step at fault is highlighted rather than described.
 *
 * **Three EventSource rules, each of which has bitten this codebase:**
 *   1. `close()` before anything that can throw, or a failed handler leaves the stream
 *      open and it reconnects forever.
 *   2. Every close arrives as an `error` with no data, so a `finished` flag is the only
 *      way to tell a completed run from a dropped connection.
 *   3. A server `error` *with* data is a sentence to show, not a transport failure.
 *   ...and a fourth specific to named events: a named SSE event never reaches `onmessage`,
 *   so every name the server can send is registered explicitly below.
 *
 * **The counters are whole totals.** Every frame carries absolute numbers, so a frame the
 * browser missed cannot leave the strip wrong. Nothing here accumulates.
 */

var IntegrationsCanvas = (function () {
    "use strict";

    var GC = window.GraphCanvas;

    /** How far the pointer must move before a press counts as a drag rather than a click.
     *  Four pixels: below it a shaky click on a port would start a connector nobody
     *  wanted; above it a short deliberate drag reads as a click. Same value the graph
     *  canvas uses, for the same reason. */
    var DRAG_THRESHOLD_PX = 4;

    /** How many bends a connection can be routed through by hand. One, here, and unlike the
     *  two top-down canvases that is a property of the geometry rather than a chosen cap:
     *  these connectors are curves, and one control point is what a single curve has. A
     *  second bend would mean splitting it into two curves, which is a different drawing. */
    var MAX_EDGE_WAYPOINTS = 1;

    /** How near a line-up a dragged bend snaps. */
    var WAYPOINT_SNAP_PX = 6;

    /** Where a new step lands when the palette adds one. Stepped so several added in a
     *  row do not stack exactly on top of each other and look like one. */
    var DROP_ORIGIN = { x: 160, y: 120 };
    var DROP_STEP = 48;

    /** How often the dock re-polls when the stream is unavailable. The server's own frame
     *  rate; a fallback that polled faster would ask for work nobody can perceive. */
    var POLL_MS = 4000;

    /** How a workflow can be set off. Only these two in Phase 1 — a webhook trigger is
     *  Phase 3, and offering it before the endpoint exists would be offering a schedule
     *  that never fires. The server's own list is the authority on what `validate_flow`
     *  accepts; this is the subset the canvas can currently set up end to end. */
    var TRIGGER_KINDS = [
        { value: "manual", label: "By hand" },
        { value: "schedule", label: "On a schedule" }
    ];

    var state = {
        flowUuid: "",
        nodes: [],
        edges: [],
        vocabulary: { nodes: [], operators: [], transforms: [], defaults: {} },
        connections: [],
        triggers: [],
        selectedId: null,
        // The move selection: which steps and connectors the next drag carries, and what a
        // box, Ctrl-click, Ctrl+A and Select all add to. Objects used as sets.
        //
        // Separate from `selectedId`, which is the one step whose settings panel is open —
        // at most one, ever. This is a different question with a different answer count.
        // Never saved: the graph payload does not include it.
        selection: { nodes: {}, edges: {} },
        dirty: false,
        dropCount: 0,
        genId: GC.makeIdGenerator(),
        armedPort: null,      // {nodeId, port} — click-then-click connecting
        run: null,            // the current run's uuid
        source: null,         // EventSource
        poller: null,
        finished: false,
        schemaCache: {}       // "connectionUuid|operationId" -> schema payload
    };

    var el = {};

    // The shared selection controller — the box, the multi-selection and the group move.
    // Built in `init`, because it needs the canvas elements.
    var selection = null;

    // =====================================================================
    // BOOT
    // =====================================================================

    function init(options) {
        state.flowUuid = options.flowUuid;
        state.defaultBatchSize = options.defaultBatchSize || 500;

        el.wrap = document.getElementById("intCanvasWrap");
        el.edges = document.getElementById("intEdges");
        el.nodes = document.getElementById("intNodes");
        el.banner = document.getElementById("intBanner");
        el.paletteBody = document.getElementById("intPaletteBody");
        el.propsBody = document.getElementById("intPropertiesBody");
        el.scheduleBody = document.getElementById("intScheduleBody");
        el.steps = document.getElementById("intSteps");
        el.unsaved = document.getElementById("intUnsavedBadge");
        el.versionBadge = document.getElementById("intVersionBadge");
        el.runStatus = document.getElementById("intRunStatus");
        el.runMode = document.getElementById("intRunMode");
        el.runStarted = document.getElementById("intRunStarted");
        el.truncated = document.getElementById("intTruncatedNote");
        el.stopBtn = document.getElementById("intStopBtn");
        el.counts = {
            read: document.getElementById("intCountRead"),
            written: document.getElementById("intCountWritten"),
            failed: document.getElementById("intCountFailed"),
            skipped: document.getElementById("intCountSkipped")
        };

        state.vocabulary = readJson("intVocabulary", state.vocabulary);
        state.connections = readJson("intConnections", []);
        state.triggers = readJson("intTriggers", []);

        var graph = readJson("intGraphData", { nodes: [], edges: [] });
        state.nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
        state.edges = (Array.isArray(graph.edges) ? graph.edges : []).map(function (edge) {
            var bends = readWaypoints(edge.waypoints);
            if (bends.length) { edge.waypoints = bends; } else { delete edge.waypoints; }
            return edge;
        });

        // Before `render`, which paints the selection as part of drawing each step.
        selection = window.GraphSelection.create({
            wrapperEl: el.wrap,
            // The node layer, not the wrapper. On this canvas it is also the origin every
            // anchor is measured against (`GC.portAnchor(el.nodes, …)`), so a box placed at
            // these coordinates lands exactly on the steps it is selecting — and, unlike
            // the other two canvases, this layer does not scroll, which is precisely why
            // measuring the layer rather than the wrapper is the rule that works for all
            // three.
            layerEl: el.nodes,
            edgesEl: el.edges,
            nodeElementId: function (id) { return "int-node-" + id; },
            edgeElementId: function (id) { return "int-edge-group-" + id; },
            selection: state.selection,
            getNodes: function () { return state.nodes; },
            getSelectableEdges: function () { return state.edges; },
            // Sampled rather than exact: these connectors are cubic curves, not runs of
            // corner points, so nine points along one is what the box is tested against.
            // For a curve whose control reach is floored at 40px that is well inside the
            // tolerance a box has anyway.
            edgeRoute: function (edge) {
                var from = portAnchorFor(edge.source, sourcePortOf(edge));
                var to = targetAnchorFor(edge.target);
                if (!from || !to) { return null; }
                var g = GC.geometry(from, to);
                var points = [];
                for (var i = 0; i <= 8; i += 1) { points.push(GC.pointAt(g, i / 8)); }
                return points;
            },
            nodeWidth: function () { return 210; },
            threshold: DRAG_THRESHOLD_PX,
            // No box while a port is armed for click-then-click connecting: a click on the
            // background is the way out of that, and turning the press into a box would
            // strand the user there.
            isBusy: function () { return !!(state.armedPort || nodeDrag); },
            classes: { node: "int-node-multi", edge: "int-edge-multi" },
            onSelectionChange: updateSelectAllButton,
            onGroupMoveBegin: onGroupMoveBegin,
            onGroupMoveFrame: onGroupMoveFrame,
            onGroupMoveEnd: onGroupMoveEnd,
        });
        selection.attach();

        buildPalette();
        buildSchedule();
        render();
        wireToolbar();
        wireCanvas();

        if (options.openRun) {
            watchRun(options.openRun);
        }

        // A drawing with unsaved changes must not be lost to a stray click on a link.
        window.addEventListener("beforeunload", function (event) {
            if (!state.dirty) { return; }
            event.preventDefault();
            event.returnValue = "";
        });
    }

    function readJson(id, fallback) {
        var node = document.getElementById(id);
        if (!node) { return fallback; }
        try {
            return JSON.parse(node.textContent || "null") || fallback;
        } catch (err) {
            return fallback;
        }
    }

    // =====================================================================
    // PALETTE
    // =====================================================================

    function buildPalette() {
        var types = state.vocabulary.nodes || [];
        var html = types.map(function (spec) {
            // A trigger is not offered: the validator refuses a second one, so a
            // palette entry for it would be an entry that can only produce a refusal.
            if (spec.type === "trigger") { return ""; }
            return (
                '<button class="btn btn-outline-secondary btn-sm w-100 text-start mb-2" ' +
                'data-int-add="' + GC.escapeAttr(spec.type) + '">' +
                '<span class="fw-semibold">' + GC.escapeHtml(spec.label || spec.type) + "</span>" +
                (spec.description
                    ? '<div class="small text-muted">' + GC.escapeHtml(spec.description) + "</div>"
                    : "") +
                "</button>"
            );
        }).join("");

        el.paletteBody.innerHTML = html ||
            '<div class="text-muted small">No step types available.</div>';

        el.paletteBody.addEventListener("click", function (event) {
            var button = event.target.closest("[data-int-add]");
            if (button) { addNode(button.getAttribute("data-int-add")); }
        });
    }

    function specFor(type) {
        var types = state.vocabulary.nodes || [];
        for (var i = 0; i < types.length; i += 1) {
            if (types[i].type === type) { return types[i]; }
        }
        return { type: type, label: type, ports: ["default"] };
    }

    function portsFor(type) {
        var spec = specFor(type);
        return spec.ports || [];
    }

    function addNode(type) {
        // One trigger, always, because the validator refuses anything else — and a
        // palette that let somebody add a second would be a palette offering a save that
        // cannot succeed.
        if (type === "trigger" && state.nodes.some(function (n) { return n.type === "trigger"; })) {
            banner("warning", "A workflow has exactly one trigger. Edit the one you have.");
            return;
        }

        var offset = state.dropCount * DROP_STEP;
        state.dropCount += 1;

        var node = {
            id: state.genId(type),
            type: type,
            position: { x: DROP_ORIGIN.x + offset, y: DROP_ORIGIN.y + (offset % 240) },
            data: { label: specFor(type).label || type }
        };

        if (type === "batch") { node.data.batch_size = state.defaultBatchSize; }
        if (type === "trigger") { node.data.kind = "manual"; }

        state.nodes.push(node);
        markDirty();
        render();
        select(node.id);
    }

    // =====================================================================
    // RENDER
    // =====================================================================

    function render() {
        renderNodes();
        renderEdges();
    }

    function renderNodes() {
        el.nodes.innerHTML = state.nodes.map(nodeHtml).join("");
    }

    function nodeHtml(node) {
        var spec = specFor(node.type);
        var data = node.data || {};
        var ports = portsFor(node.type);

        var portsHtml = ports.map(function (port) {
            return (
                '<span class="int-port' + (port === "error" ? " int-port-error" : "") + '" ' +
                'data-int-port="' + GC.escapeAttr(port) + '" ' +
                'data-int-node="' + GC.escapeAttr(node.id) + '">' +
                GC.escapeHtml(port) + "</span>"
            );
        }).join("");

        return (
            '<div class="int-node' +
            (state.selectedId === node.id ? " int-selected" : "") +
            // Read from state, never only set on the element at gesture time: this layer is
            // rebuilt from strings by `renderNodes`, so a class applied by a click would
            // vanish the next time anything re-rendered — including on every run frame,
            // which repaints the statuses.
            (selection && selection.marksNode(node.id) ? " int-node-multi" : "") + '" ' +
            'id="int-node-' + GC.escapeAttr(node.id) + '" ' +
            'data-int-node="' + GC.escapeAttr(node.id) + '" ' +
            'style="left:' + (node.position && node.position.x || 0) + "px;top:" +
            (node.position && node.position.y || 0) + 'px;">' +
            '<div class="int-target"></div>' +
            '<div class="int-node-head">' +
            GC.escapeHtml(data.label || spec.label || node.type) +
            '<span class="int-node-status badge bg-light text-dark border" ' +
            'data-int-status="' + GC.escapeAttr(node.id) + '"></span>' +
            "</div>" +
            '<div class="int-node-body">' + GC.escapeHtml(summarise(node)) + "</div>" +
            '<div class="int-ports">' + portsHtml + "</div>" +
            "</div>"
        );
    }

    /** One line under a step's name saying what it will actually do. Written from the
     *  step's own settings rather than from its type, because "Write" on forty steps tells
     *  somebody nothing and "Create invoice → Billing API" tells them everything. */
    function summarise(node) {
        var data = node.data || {};

        if (node.type === "connector_read" || node.type === "connector_write") {
            var connection = connectionLabel(data.connection_uuid);
            if (!connection) { return "No connection chosen"; }
            return (data.operation_id || "no operation") + " → " + connection;
        }
        if (node.type === "batch") {
            return "Batches of " + (data.batch_size || state.defaultBatchSize);
        }
        if (node.type === "trigger") {
            return (data.kind || "manual") + " trigger";
        }
        if (node.type === "transform") {
            return (data.mappings || []).length + " field mapping(s)";
        }
        if (node.type === "filter" || node.type === "validate") {
            return (data.conditions || data.rules || []).length + " rule(s)";
        }
        if (node.type === "branch") {
            return (data.conditions || []).length + " condition(s)";
        }
        return specFor(node.type).description || "";
    }

    function connectionLabel(uuid) {
        for (var i = 0; i < state.connections.length; i += 1) {
            if (state.connections[i].uuid === uuid) { return state.connections[i].label; }
        }
        return "";
    }

    function renderEdges() {
        while (el.edges.firstChild) { el.edges.removeChild(el.edges.firstChild); }
        state.edges.forEach(renderEdge);
    }

    /** One connector: its curve, its delete control and its port label, in a group.
     *
     *  Grouped, and each child given a class, so a drag can move an existing connector by
     *  writing four attributes instead of rebuilding the whole layer. It used to redraw
     *  **every** edge on the canvas on every pointermove — see `runNodeDragFrame`. */
    function renderEdge(edge) {
        var g = GC.svg("g");
        g.setAttribute("id", "int-edge-group-" + edge.id);
        g.setAttribute("class", "int-edge-group" +
            (selection && selection.marksEdge(edge.id) ? " int-edge-multi" : ""));

        // An invisible fat twin of the curve, and the only part of it wide enough to grab.
        // The line is 2px; dragging it to bend it would otherwise mean landing the cursor
        // inside two pixels.
        var hit = GC.svg("path");
        hit.setAttribute("class", "int-edge-hit");
        hit.addEventListener("pointerdown", function (event) { startBend(edge.id, event); });
        hit.addEventListener("dblclick", function (event) {
            event.stopPropagation();
            straightenEdge(edge.id);
        });
        g.appendChild(hit);

        var path = GC.svg("path");
        path.setAttribute("class", "int-edge-path " + edgeClass(edge));
        g.appendChild(path);

        // The bend handle, when there is one.
        if (waypointsOf(edge).length) {
            var handle = GC.svg("circle");
            handle.setAttribute("class", "int-edge-waypoint");
            handle.setAttribute("r", 4);
            g.appendChild(handle);
        }

        // The delete control sits at the curve's midpoint, which is where somebody
        // looking to remove a connection points. A whole-path click target would
        // fight with dragging a step that happens to sit under the curve.
        var circle = GC.svg("circle");
        circle.setAttribute("r", 8);
        circle.setAttribute("class", "int-edge-delete");
        circle.addEventListener("click", function () { removeEdge(edge.id); });
        g.appendChild(circle);

        var cross = GC.svg("text");
        cross.setAttribute("text-anchor", "middle");
        cross.setAttribute("class", "int-edge-delete-x");
        cross.textContent = "×";
        g.appendChild(cross);

        // The port's name, when it is not the plain one. Which branch a connector
        // leaves by is the whole meaning of a validate or a branch step, and reading
        // it off the drawing beats opening two panels to find out.
        var port = sourcePortOf(edge);
        if (port && port !== "default") {
            var label = GC.svg("text");
            label.setAttribute("text-anchor", "middle");
            label.setAttribute("class", "int-edge-label");
            label.textContent = port;
            g.appendChild(label);
        }

        el.edges.appendChild(g);
        updateEdgeGeometry(edgeChrome(edge));
    }

    /** The elements one connector's geometry is written to, resolved once. */
    function resolveEdgeChrome(record) {
        var group = document.getElementById("int-edge-group-" + record.edge.id);
        record.group = group;
        record.path = group ? group.querySelector(".int-edge-path") : null;
        record.hit = group ? group.querySelector(".int-edge-hit") : null;
        record.waypointHandle = group ? group.querySelector(".int-edge-waypoint") : null;
        record.deleteBtn = group ? group.querySelector(".int-edge-delete") : null;
        record.deleteCross = group ? group.querySelector(".int-edge-delete-x") : null;
        record.label = group ? group.querySelector(".int-edge-label") : null;
        return record;
    }

    function edgeChrome(edge) {
        return resolveEdgeChrome({ edge: edge });
    }

    /** Move an already-drawn connector onto where its steps are now.
     *
     *  A connector with an end that cannot be measured is emptied rather than removed, so
     *  its group and its listeners survive until the step comes back. */
    function updateEdgeGeometry(record) {
        if (!record.group) { return; }

        var from = portAnchorFor(record.edge.source, sourcePortOf(record.edge));
        var to = targetAnchorFor(record.edge.target);

        if (!from || !to) {
            record.group.setAttribute("visibility", "hidden");
            return;
        }
        record.group.removeAttribute("visibility");

        var g = GC.geometryWithBend(from, to, waypointsOf(record.edge)[0]);
        var mid = GC.pointAt(g, 0.5);
        var d = GC.pathD(g);

        if (record.path) { record.path.setAttribute("d", d); }
        if (record.hit) { record.hit.setAttribute("d", d); }
        // Drawn at the stored point, which for these curves is also exactly the midpoint —
        // `geometryWithBend` puts the curve through it.
        if (record.waypointHandle) {
            var bend = waypointsOf(record.edge)[0];
            if (bend) {
                record.waypointHandle.setAttribute("cx", bend.x);
                record.waypointHandle.setAttribute("cy", bend.y);
            }
        }
        if (record.deleteBtn) {
            record.deleteBtn.setAttribute("cx", mid.x);
            record.deleteBtn.setAttribute("cy", mid.y);
        }
        if (record.deleteCross) {
            record.deleteCross.setAttribute("x", mid.x);
            record.deleteCross.setAttribute("y", mid.y + 4);
        }
        if (record.label) {
            record.label.setAttribute("x", mid.x);
            record.label.setAttribute("y", mid.y - 12);
        }
    }

    /** The connectors a move of these steps will disturb, resolved ready to repaint. */
    function chromeForMovingNodes(movingIds) {
        var moving = {};
        movingIds.forEach(function (id) { moving[id] = true; });

        return state.edges
            .filter(function (edge) { return moving[edge.source] || moving[edge.target]; })
            .map(edgeChrome);
    }

    /** A connection's hand-placed bends. */
    function waypointsOf(edge) {
        if (!edge) { return []; }
        return Array.isArray(edge.waypoints) ? edge.waypoints : [];
    }

    /** Bends read off a stored drawing, keeping only the ones that are usable.
     *
     *  The save schema takes this drawing as an opaque object, which is what let `waypoints`
     *  be added without a schema change — and means this is where a hand-edited or older
     *  document is made safe. A bend that is not two finite numbers is dropped rather than
     *  drawn: `NaN` in a coordinate fails silently in the SVG and then again at the
     *  database, which refuses it as JSON. */
    function readWaypoints(raw) {
        if (!Array.isArray(raw)) { return []; }

        return raw
            .filter(function (point) {
                return point && isFinite(point.x) && isFinite(point.y);
            })
            .slice(0, MAX_EDGE_WAYPOINTS)
            .map(function (point) {
                return { x: Math.max(0, Number(point.x)), y: Math.max(0, Number(point.y)) };
            });
    }

    /** The curve one connection runs along, hand-routed or not. */
    function edgeGeometry(edge) {
        var from = portAnchorFor(edge.source, sourcePortOf(edge));
        var to = targetAnchorFor(edge.target);
        if (!from || !to) { return null; }
        return GC.geometryWithBend(from, to, waypointsOf(edge)[0]);
    }

    function edgeClass(edge) {
        var port = sourcePortOf(edge);
        if (port === "error") { return "int-edge-error"; }
        if (port === "body") { return "int-edge-body"; }
        return "";
    }

    /** The server accepts either spelling and the drawing may hold either, so both are
     *  read. Written once here rather than at each use, because a missed one silently
     *  routes an edge through the default port. */
    function sourcePortOf(edge) {
        return edge.source_port || edge.sourcePort || "default";
    }

    // A drag measures each port it needs once and then keeps the result as an offset from
    // its step's stored position. Measuring per frame means a `getBoundingClientRect` after
    // the frame has already written `style.left`, which forces the browser to lay the whole
    // canvas out again — once per port, per connector, per pointermove. Taking the offset
    // as `anchor - node.position`, both sampled at the same instant, keeps it exact: the
    // ports' half-pixel placement lands inside the offset rather than being rounded away.
    var dragAnchors = null;

    function measuredAnchor(nodeId, selector) {
        var nodeEl = document.getElementById("int-node-" + nodeId);
        if (!nodeEl) { return null; }
        return GC.portAnchor(el.nodes, nodeEl, selector);
    }

    function cachedAnchor(nodeId, selector) {
        if (!dragAnchors) { return measuredAnchor(nodeId, selector); }

        var key = nodeId + "|" + selector;

        if (!dragAnchors.moving[nodeId]) {
            if (!(key in dragAnchors.frozen)) {
                dragAnchors.frozen[key] = measuredAnchor(nodeId, selector);
            }
            return dragAnchors.frozen[key];
        }

        var node = findNode(nodeId);
        if (!node) { return null; }
        var x = (node.position && node.position.x) || 0;
        var y = (node.position && node.position.y) || 0;

        if (!(key in dragAnchors.offsets)) {
            var measured = measuredAnchor(nodeId, selector);
            dragAnchors.offsets[key] = measured
                ? { dx: measured.x - x, dy: measured.y - y }
                : null;
        }

        var offset = dragAnchors.offsets[key];
        return offset ? { x: x + offset.dx, y: y + offset.dy } : null;
    }

    /** Open the anchor cache for a drag, and warm it while the canvas is still settled. */
    function beginDragAnchors(movingIds, chrome) {
        dragAnchors = { moving: {}, offsets: {}, frozen: {} };
        movingIds.forEach(function (id) { dragAnchors.moving[id] = true; });
        chrome.forEach(function (record) {
            portAnchorFor(record.edge.source, sourcePortOf(record.edge));
            targetAnchorFor(record.edge.target);
        });
    }

    function endDragAnchors() {
        dragAnchors = null;
    }

    function portAnchorFor(nodeId, port) {
        return cachedAnchor(nodeId, '[data-int-port="' + GC.cssEscape(port) + '"]');
    }

    function targetAnchorFor(nodeId) {
        return cachedAnchor(nodeId, ".int-target");
    }

    // =====================================================================
    // CANVAS INTERACTION
    // =====================================================================

    function wireCanvas() {
        el.nodes.addEventListener("pointerdown", onPointerDown);
        el.nodes.addEventListener("click", onCanvasClick);
    }

    function onCanvasClick(event) {
        // The click that trails a box or a group move must not be read as a click on the
        // canvas, or the gesture would undo itself.
        if (selection && selection.swallowedClick()) { return; }

        var portEl = event.target.closest("[data-int-port]");
        if (portEl) {
            armPort(portEl.getAttribute("data-int-node"), portEl.getAttribute("data-int-port"));
            return;
        }

        var nodeEl = event.target.closest("[data-int-node]");
        if (!nodeEl) { return; }

        // Click-then-click connecting: a port was armed, and this is the second click.
        // Offered as well as drag because a long drag across a scrolling canvas is
        // genuinely hard, and both gestures end in the same `connect`.
        if (state.armedPort) {
            connect(state.armedPort.nodeId, state.armedPort.port, nodeEl.getAttribute("data-int-node"));
            disarmPort();
            return;
        }

        select(nodeEl.getAttribute("data-int-node"));
    }

    function armPort(nodeId, port) {
        disarmPort();
        state.armedPort = { nodeId: nodeId, port: port };

        var selector = '.int-port[data-int-node="' + GC.cssEscape(nodeId) +
            '"][data-int-port="' + GC.cssEscape(port) + '"]';
        var portEl = el.nodes.querySelector(selector);
        if (portEl) { portEl.classList.add("int-port-armed"); }
        el.wrap.classList.add("int-connecting");
    }

    function disarmPort() {
        state.armedPort = null;
        el.wrap.classList.remove("int-connecting");
        Array.prototype.forEach.call(
            el.nodes.querySelectorAll(".int-port-armed"),
            function (node) { node.classList.remove("int-port-armed"); }
        );
    }

    function onPointerDown(event) {
        var portEl = event.target.closest("[data-int-port]");
        if (portEl) {
            startConnectorDrag(event, portEl);
            return;
        }

        var nodeEl = event.target.closest("[data-int-node]");
        if (!nodeEl) { return; }

        // Offered to the selection first. It takes the press only for a Ctrl-click or a
        // real group move; anything else falls through to the drag this canvas has always
        // done.
        var nodeId = nodeEl.getAttribute("data-int-node");
        if (selection && selection.beginNodePress(nodeId, event)) { return; }
        startNodeDrag(event, nodeEl);
    }

    // The drag in progress, and its pending repaint. Held here rather than in closures
    // inside `startNodeDrag` for two reasons: one animation frame has to be able to find
    // the drag that scheduled it, and a step deleted mid-drag has to be able to call the
    // gesture off.
    var nodeDrag = null;
    var nodeDragFrame = null;

    function startNodeDrag(event, nodeEl) {
        var nodeId = nodeEl.getAttribute("data-int-node");
        var node = findNode(nodeId);
        if (!node) { return; }

        nodeDrag = {
            nodeId: nodeId,
            nodeEl: nodeEl,
            startX: event.clientX,
            startY: event.clientY,
            clientX: event.clientX,
            clientY: event.clientY,
            originX: (node.position && node.position.x) || 0,
            originY: (node.position && node.position.y) || 0,
            moved: false,
            // The connectors this move disturbs, resolved now while the canvas is settled.
            chrome: chromeForMovingNodes([nodeId])
        };
        beginDragAnchors([nodeId], nodeDrag.chrome);

        document.addEventListener("pointermove", onNodeDragMove);
        document.addEventListener("pointerup", onNodeDragEnd);
    }

    /** A pointermove during a step drag: record where the pointer is, and ask for a frame.
     *
     *  Nothing is drawn here. pointermove fires faster than the screen updates, so most of
     *  the work done inline was thrown away unseen — and what it threw away was a redraw
     *  of every connector on the canvas, not just the ones that had moved. */
    function onNodeDragMove(event) {
        if (!nodeDrag) { return; }

        if (!nodeDrag.moved) {
            var travelled = Math.abs(event.clientX - nodeDrag.startX) +
                Math.abs(event.clientY - nodeDrag.startY);
            if (travelled < DRAG_THRESHOLD_PX) { return; }
            nodeDrag.moved = true;
        }

        nodeDrag.clientX = event.clientX;
        nodeDrag.clientY = event.clientY;

        if (nodeDragFrame === null) {
            nodeDragFrame = window.requestAnimationFrame(runNodeDragFrame);
        }
    }

    function runNodeDragFrame() {
        nodeDragFrame = null;
        if (!nodeDrag || !nodeDrag.moved) { return; }

        var node = findNode(nodeDrag.nodeId);
        if (!node) { return; }

        var dx = nodeDrag.clientX - nodeDrag.startX;
        var dy = nodeDrag.clientY - nodeDrag.startY;
        node.position = {
            x: Math.max(0, nodeDrag.originX + dx),
            y: Math.max(0, nodeDrag.originY + dy)
        };
        nodeDrag.nodeEl.style.left = node.position.x + "px";
        nodeDrag.nodeEl.style.top = node.position.y + "px";

        nodeDrag.chrome.forEach(updateEdgeGeometry);
    }

    function onNodeDragEnd() {
        var moved = nodeDrag && nodeDrag.moved;
        endNodeDrag();
        if (moved) { markDirty(); }
    }

    /** Tear the drag down without committing it. Also called from the deletion paths: the
     *  drag holds resolved elements for the connectors it repaints, and removing a step or
     *  a connector detaches some of them. */
    function endNodeDrag() {
        if (nodeDragFrame !== null) {
            window.cancelAnimationFrame(nodeDragFrame);
            nodeDragFrame = null;
        }
        nodeDrag = null;
        endDragAnchors();
        document.removeEventListener("pointermove", onNodeDragMove);
        document.removeEventListener("pointerup", onNodeDragEnd);
    }

    // =====================================================================
    // BENDING A CONNECTION
    //
    // Dragging a connection routes it by hand: the curve passes through the point it was
    // dragged to. One bend, because one curve has one control point — see
    // `GC.geometryWithBend`, which is where the arithmetic lives.
    //
    // The bend is stored on the connection as `waypoints`, in canvas coordinates, the same
    // key and the same shape the other two canvases use. Absolute rather than relative to
    // the two ends, because a bend exists to dodge something on the canvas.
    // =====================================================================

    var bending = null;
    var bendFrame = null;
    var suppressEdgeClick = false;

    /** Whether a press on a connection is a bend, a group move, or nothing. */
    function edgeGrabIntent(edgeId, event) {
        if (event.button !== 0) { return "ignore"; }
        // Modifiers belong to the selection: the click that follows extends it.
        if (event.shiftKey || event.ctrlKey || event.metaKey) { return "ignore"; }
        if (nodeDrag || state.armedPort) { return "ignore"; }
        if (selection && selection.isMulti() && selection.hasEdge(edgeId)) { return "group"; }
        return "bend";
    }

    function startBend(edgeId, event) {
        var intent = edgeGrabIntent(edgeId, event);
        if (intent === "ignore") { return; }

        var edge = findEdge(edgeId);
        if (!edge) { return; }

        if (intent === "group") {
            // The selection owns this press. It needs a step to hang the move on, and either
            // end will do — the connection being in the selection brings both along.
            selection.beginNodePress(edge.source, event);
            return;
        }

        event.preventDefault();
        event.stopPropagation();

        // Starting from where the curve already is, so it does not jump when taken hold of.
        var geometry = edgeGeometry(edge);
        if (!geometry) { return; }
        // Read before the bend is written, not after: this is what decides whether a press
        // that turns out to be a click leaves a bend behind on a connection that never had
        // one.
        var hadBend = waypointsOf(edge).length > 0;
        var start = waypointsOf(edge)[0] || GC.pointAt(geometry, 0.5);

        edge.waypoints = [{ x: start.x, y: start.y }];
        renderEdges();

        bending = {
            edgeId: edgeId,
            hadBend: hadBend,
            startX: event.clientX,
            startY: event.clientY,
            clientX: event.clientX,
            clientY: event.clientY,
            moved: false,
            chrome: edgeChrome(edge)
        };
        // Both ends are still, so the anchor cache freezes them and the gesture reads nothing.
        beginDragAnchors([], [bending.chrome]);

        document.addEventListener("pointermove", onBendMove);
        document.addEventListener("pointerup", onBendEnd);
    }

    function onBendMove(event) {
        if (!bending) { return; }

        if (!bending.moved) {
            var travelled = Math.abs(event.clientX - bending.startX) +
                Math.abs(event.clientY - bending.startY);
            if (travelled < DRAG_THRESHOLD_PX) { return; }
            bending.moved = true;
        }

        bending.clientX = event.clientX;
        bending.clientY = event.clientY;

        if (bendFrame === null) {
            bendFrame = window.requestAnimationFrame(runBendFrame);
        }
    }

    function runBendFrame() {
        bendFrame = null;
        if (!bending || !bending.moved) { return; }

        var edge = findEdge(bending.edgeId);
        if (!edge) { return; }

        var at = canvasPoint(bending.clientX, bending.clientY);
        var from = portAnchorFor(edge.source, sourcePortOf(edge));
        var to = targetAnchorFor(edge.target);
        var xs = [];
        var ys = [];
        if (from) { xs.push(from.x); ys.push(from.y); }
        if (to) { xs.push(to.x); ys.push(to.y); }

        edge.waypoints = [{
            x: Math.max(0, GC.snapToAny(at.x, xs, WAYPOINT_SNAP_PX)),
            y: Math.max(0, GC.snapToAny(at.y, ys, WAYPOINT_SNAP_PX))
        }];
        updateEdgeGeometry(bending.chrome);
    }

    function onBendEnd() {
        var gesture = bending;
        if (bendFrame !== null) {
            window.cancelAnimationFrame(bendFrame);
            bendFrame = null;
        }
        bending = null;
        endDragAnchors();
        document.removeEventListener("pointermove", onBendMove);
        document.removeEventListener("pointerup", onBendEnd);

        if (!gesture) { return; }

        var edge = findEdge(gesture.edgeId);
        if (!edge) { return; }

        // A press that never moved is a click. The bend that was put in for it comes back
        // out, unless the connection already had one.
        if (!gesture.moved) {
            if (!gesture.hadBend) {
                delete edge.waypoints;
                renderEdges();
            }
            return;
        }

        markDirty();
        renderEdges();

        suppressEdgeClick = true;
        window.setTimeout(function () { suppressEdgeClick = false; }, 0);
    }

    /** Throw the bend away, so the connection is drawn for you again. */
    function straightenEdge(edgeId) {
        var edge = findEdge(edgeId);
        if (!edge || !waypointsOf(edge).length) { return; }

        delete edge.waypoints;
        markDirty();
        renderEdges();
    }

    /** Where a client point is, in the coordinates the steps and the bends are stored in.
     *
     *  This layer does not scroll — unlike the other two canvases — so there is no scroll
     *  offset to add. Measuring the layer rather than the wrapper is what makes one rule
     *  work for all three. */
    function canvasPoint(clientX, clientY) {
        var rect = el.nodes.getBoundingClientRect();
        return { x: clientX - rect.left, y: clientY - rect.top };
    }

    // =====================================================================
    // MOVING SEVERAL STEPS AT ONCE
    //
    // The gesture is in static/js/graph_selection.js, shared with the Graph Designer and
    // the Flow Builder. What is here is only what a group move means on this canvas.
    // =====================================================================

    var groupChrome = null;

    function onGroupMoveBegin(ids) {
        groupChrome = chromeForMovingNodes(ids);
        beginDragAnchors(ids, groupChrome);
    }

    function onGroupMoveFrame() {
        if (groupChrome) { groupChrome.forEach(updateEdgeGeometry); }
    }

    function onGroupMoveEnd(ids, committed) {
        endDragAnchors();
        groupChrome = null;
        // No auto-layout on this canvas, so there is no "manual" to switch to — a move is
        // simply an unsaved change.
        if (committed) { markDirty(); }
    }

    /** The Select all button doubles as Clear, and says which it is. */
    function updateSelectAllButton() {
        var btn = document.getElementById("intSelectAllBtn");
        if (!btn) { return; }

        var count = selection.count();
        btn.classList.toggle("btn-outline-primary", count > 0);
        btn.classList.toggle("btn-outline-secondary", count === 0);
        btn.innerHTML = count > 0
            ? '<i class="las la-times-circle"></i> Clear (' + count + ")"
            : '<i class="las la-object-group"></i> Select all';
        btn.title = count > 0
            ? "Clear the move selection"
            : "Select every step and connection, so they can be moved together";
    }

    function startConnectorDrag(event, portEl) {
        event.preventDefault();

        var nodeId = portEl.getAttribute("data-int-node");
        var port = portEl.getAttribute("data-int-port");
        var from = GC.portAnchor(el.nodes, portEl.closest(".int-node"), null);
        var preview = GC.svg("path");
        preview.setAttribute("class", "int-edge-preview");
        el.edges.appendChild(preview);

        var moved = false;
        var startX = event.clientX;
        var startY = event.clientY;

        function onMove(moveEvent) {
            if (!moved &&
                Math.abs(moveEvent.clientX - startX) + Math.abs(moveEvent.clientY - startY)
                < DRAG_THRESHOLD_PX) {
                return;
            }
            moved = true;

            var rect = el.nodes.getBoundingClientRect();
            var to = { x: moveEvent.clientX - rect.left, y: moveEvent.clientY - rect.top };
            preview.setAttribute("d", GC.pathD(GC.geometry(from, to)));
            highlightDroppable(moveEvent);
        }

        function onUp(upEvent) {
            document.removeEventListener("pointermove", onMove);
            document.removeEventListener("pointerup", onUp);
            if (preview.parentNode) { preview.parentNode.removeChild(preview); }
            clearDroppable();

            if (!moved) {
                // A press that never moved is a click, and a click on a port arms it for
                // the click-then-click gesture. Handling it here as well as in the click
                // listener would arm and immediately disarm.
                return;
            }

            var dropped = document.elementFromPoint(upEvent.clientX, upEvent.clientY);
            var targetEl = dropped && dropped.closest("[data-int-node]");
            if (targetEl) { connect(nodeId, port, targetEl.getAttribute("data-int-node")); }
        }

        document.addEventListener("pointermove", onMove);
        document.addEventListener("pointerup", onUp);
    }

    function highlightDroppable(event) {
        clearDroppable();
        var over = document.elementFromPoint(event.clientX, event.clientY);
        var nodeEl = over && over.closest(".int-node");
        if (nodeEl) { nodeEl.classList.add("int-droppable"); }
    }

    function clearDroppable() {
        Array.prototype.forEach.call(
            el.nodes.querySelectorAll(".int-droppable"),
            function (node) { node.classList.remove("int-droppable"); }
        );
    }

    function connect(sourceId, port, targetId) {
        if (sourceId === targetId) {
            banner("warning", "A step cannot connect to itself.");
            return;
        }

        // Refused here as well as by the validator, because the useful moment to say it is
        // while the pointer is still on the port. Two edges on one port is the rule; a
        // second edge into the same *step* from a different port is fine.
        var taken = state.edges.some(function (edge) {
            return edge.source === sourceId && sourcePortOf(edge) === port;
        });
        if (taken) {
            banner("warning", "That output already goes somewhere. Delete the existing connection first.");
            return;
        }

        state.edges.push({
            id: state.genId("edge"),
            source: sourceId,
            source_port: port,
            target: targetId
        });

        markDirty();
        render();
    }

    function removeEdge(edgeId) {
        endNodeDrag();
        if (selection) { selection.abandon(); }
        state.edges = state.edges.filter(function (edge) { return edge.id !== edgeId; });
        if (selection) { selection.prune(); }
        markDirty();
        render();
    }

    function findEdge(edgeId) {
        for (var i = 0; i < state.edges.length; i += 1) {
            if (state.edges[i].id === edgeId) { return state.edges[i]; }
        }
        return null;
    }

    function findNode(nodeId) {
        for (var i = 0; i < state.nodes.length; i += 1) {
            if (state.nodes[i].id === nodeId) { return state.nodes[i]; }
        }
        return null;
    }

    // =====================================================================
    // PROPERTIES
    // =====================================================================

    function select(nodeId) {
        state.selectedId = nodeId;
        renderNodes();
        renderEdges();
        buildProperties(nodeId);

        var panel = document.getElementById("intProperties");
        bootstrap.Offcanvas.getOrCreateInstance(panel).show();
    }

    function buildProperties(nodeId) {
        var node = findNode(nodeId);
        if (!node) { el.propsBody.innerHTML = ""; return; }

        var data = node.data || {};
        var parts = [
            field("Step name", "label", data.label || "", "text")
        ];

        if (node.type === "trigger") {
            parts.push(select_("How it starts", "kind", data.kind || "manual",
                TRIGGER_KINDS));
        }

        if (node.type === "connector_read" || node.type === "connector_write") {
            parts.push(connectionPicker(data.connection_uuid));
            parts.push(operationPicker(data.operation_id));
        }

        if (node.type === "batch") {
            parts.push(field("Records per batch", "batch_size",
                data.batch_size || state.defaultBatchSize, "number"));
            parts.push(
                '<div class="form-text mb-3">A whole batch is held in memory at once, ' +
                "which is what the upper limit is protecting. Bigger batches mean fewer " +
                "passes and more memory per pass.</div>"
            );
        }

        if (node.type === "connector_write") {
            parts.push(mappingGrid(node));
        }

        parts.push(
            '<hr><button class="btn btn-outline-danger btn-sm" data-int-delete-node="' +
            GC.escapeAttr(node.id) + '">Delete this step</button>'
        );

        el.propsBody.innerHTML = parts.join("");
        wireProperties(node);
    }

    function field(label, name, value, type) {
        return (
            '<div class="mb-3"><label class="form-label fw-semibold small">' +
            GC.escapeHtml(label) + "</label>" +
            '<input type="' + type + '" class="form-control form-control-sm" ' +
            'data-int-field="' + GC.escapeAttr(name) + '" ' +
            'value="' + GC.escapeAttr(String(value)) + '"></div>'
        );
    }

    function select_(label, name, value, options) {
        var opts = options.map(function (option) {
            var v = option.value !== undefined ? option.value : option;
            var l = option.label !== undefined ? option.label : option;
            return '<option value="' + GC.escapeAttr(v) + '"' +
                (v === value ? " selected" : "") + ">" + GC.escapeHtml(l) + "</option>";
        }).join("");

        return (
            '<div class="mb-3"><label class="form-label fw-semibold small">' +
            GC.escapeHtml(label) + "</label>" +
            '<select class="form-select form-select-sm" data-int-field="' +
            GC.escapeAttr(name) + '"><option value=""></option>' + opts + "</select></div>"
        );
    }

    function connectionPicker(value) {
        // Built from the user's real connections, so a step cannot name one that does not
        // exist — which is the single most effective thing this panel does, because the
        // failure it prevents happens at 3am in somebody else's system.
        var options = state.connections
            .filter(function (c) { return c.is_active && c.status === "active"; })
            .map(function (c) { return { value: c.uuid, label: c.label }; });

        if (!options.length) {
            return (
                '<div class="alert alert-warning small">No usable connections. ' +
                '<a href="/integrations/connections/">Add one</a> before setting up this step.</div>'
            );
        }
        return select_("Connection", "connection_uuid", value || "", options);
    }

    function operationPicker(value) {
        // Filled in by `loadOperations` once a connection is chosen, because what a
        // connection offers is a question only the server can answer.
        return (
            '<div class="mb-3"><label class="form-label fw-semibold small">Operation</label>' +
            '<select class="form-select form-select-sm" data-int-field="operation_id" ' +
            'data-int-operations>' +
            '<option value="' + GC.escapeAttr(value || "") + '">' +
            GC.escapeHtml(value || "choose a connection first") + "</option></select></div>"
        );
    }

    function mappingGrid(node) {
        var data = node.data || {};
        var mappings = data.mappings || [];

        var rows = mappings.map(function (mapping, index) {
            return (
                '<div class="int-map-row" data-int-map-row="' + index + '">' +
                '<input class="form-control form-control-sm" placeholder="source.path" ' +
                'data-int-map="source" value="' + GC.escapeAttr(mapping.source || "") + '">' +
                '<input class="form-control form-control-sm" placeholder="destination field" ' +
                'data-int-map="target" value="' + GC.escapeAttr(mapping.target || "") + '">' +
                '<input class="form-control form-control-sm" placeholder="transform" ' +
                'data-int-map="transform" value="' + GC.escapeAttr(mapping.transform || "") + '">' +
                '<button class="btn btn-sm btn-outline-danger" data-int-map-remove="' +
                index + '">&times;</button></div>'
            );
        }).join("");

        return (
            "<hr><div class='fw-semibold small mb-2'>Field mapping</div>" +
            '<div class="int-map-row int-map-head">' +
            "<div>From the record</div><div>To the destination</div><div>Transform</div><div></div></div>" +
            '<div data-int-map-rows>' + rows + "</div>" +
            '<div class="d-flex gap-2 mt-2">' +
            '<button class="btn btn-sm btn-outline-secondary" data-int-map-add>Add a field</button>' +
            '<button class="btn btn-sm btn-outline-primary" data-int-map-match>Map matching names</button>' +
            "</div>" +
            '<div class="form-text">A destination field marked required must have something ' +
            "mapped to it before this workflow can be published — a scheduled run has nobody " +
            "to ask.</div>"
        );
    }

    function wireProperties(node) {
        el.propsBody.querySelectorAll("[data-int-field]").forEach(function (input) {
            input.addEventListener("change", function () {
                var name = input.getAttribute("data-int-field");
                var value = input.type === "number" ? Number(input.value) : input.value;

                node.data = node.data || {};
                node.data[name] = value;
                markDirty();
                renderNodes();
                renderEdges();

                if (name === "connection_uuid") { loadOperations(node); }
                if (name === "operation_id") { loadSchema(node); }
            });
        });

        var deleteBtn = el.propsBody.querySelector("[data-int-delete-node]");
        if (deleteBtn) {
            deleteBtn.addEventListener("click", function () {
                deleteNode(deleteBtn.getAttribute("data-int-delete-node"));
            });
        }

        wireMapping(node);

        if ((node.data || {}).connection_uuid) { loadOperations(node); }
    }

    function wireMapping(node) {
        var add = el.propsBody.querySelector("[data-int-map-add]");
        if (add) {
            add.addEventListener("click", function () {
                node.data = node.data || {};
                node.data.mappings = (node.data.mappings || []).concat([{ source: "", target: "" }]);
                markDirty();
                buildProperties(node.id);
            });
        }

        var match = el.propsBody.querySelector("[data-int-map-match]");
        if (match) { match.addEventListener("click", function () { matchByName(node); }); }

        el.propsBody.querySelectorAll("[data-int-map-remove]").forEach(function (button) {
            button.addEventListener("click", function () {
                var index = Number(button.getAttribute("data-int-map-remove"));
                node.data.mappings.splice(index, 1);
                markDirty();
                buildProperties(node.id);
            });
        });

        el.propsBody.querySelectorAll("[data-int-map]").forEach(function (input) {
            input.addEventListener("change", function () {
                var row = input.closest("[data-int-map-row]");
                var index = Number(row.getAttribute("data-int-map-row"));
                node.data.mappings[index][input.getAttribute("data-int-map")] = input.value;
                markDirty();
                renderNodes();
            });
        });
    }

    /**
     * Fill the mapping grid from the destination's own field list, pairing names that
     * already agree.
     *
     * **Labelled as matching by name, never as a suggestion.** It is a string comparison,
     * and dressing it up as intelligence would invite somebody to trust it with a field
     * whose name happens to coincide.
     */
    function matchByName(node) {
        var schema = state.schemaCache[schemaKey(node)];
        if (!schema) {
            banner("warning", "Choose a connection and an operation first.");
            return;
        }

        node.data = node.data || {};
        var already = {};
        (node.data.mappings || []).forEach(function (m) { already[m.target] = true; });

        var added = (schema.inputs || []).filter(function (input) {
            return !already[input.name];
        }).map(function (input) {
            return { source: input.name, target: input.name };
        });

        if (!added.length) {
            banner("info", "Every destination field already has a mapping.");
            return;
        }

        node.data.mappings = (node.data.mappings || []).concat(added);
        markDirty();
        buildProperties(node.id);
        banner("info", "Matched " + added.length + " field(s) by name. Check them — this is a " +
            "name comparison, not a suggestion.");
    }

    function deleteNode(nodeId) {
        endNodeDrag();
        if (selection) { selection.abandon(); }
        state.nodes = state.nodes.filter(function (n) { return n.id !== nodeId; });
        // Edges to or from a deleted step go with it. Left behind they would be edges
        // naming a missing step, which the validator refuses — so the drawing would become
        // unsaveable by deleting something.
        state.edges = state.edges.filter(function (edge) {
            return edge.source !== nodeId && edge.target !== nodeId;
        });

        state.selectedId = null;
        // Re-derived rather than one key deleted: a step takes its connectors with it, so a
        // stale connector id can outlive the step deletion that caused it.
        if (selection) { selection.prune(); }
        markDirty();
        render();
        bootstrap.Offcanvas.getOrCreateInstance(document.getElementById("intProperties")).hide();
    }

    function schemaKey(node) {
        var data = node.data || {};
        return (data.connection_uuid || "") + "|" + (data.operation_id || "");
    }

    function loadOperations(node) {
        var data = node.data || {};
        if (!data.connection_uuid) { return; }

        fetch("/integrations/connections/" + data.connection_uuid + "/operations")
            .then(function (response) { return response.json(); })
            .then(function (body) {
                var picker = el.propsBody.querySelector("[data-int-operations]");
                if (!picker) { return; }

                var wanted = node.type === "connector_read" ? "read" : "write";
                var options = (body.operations || []).filter(function (op) {
                    return op.kind === wanted;
                });

                picker.innerHTML = '<option value=""></option>' + options.map(function (op) {
                    return '<option value="' + GC.escapeAttr(op.operation_id) + '"' +
                        (op.operation_id === data.operation_id ? " selected" : "") + ">" +
                        GC.escapeHtml(op.label || op.operation_id) + "</option>";
                }).join("");

                if (data.operation_id) { loadSchema(node); }
            })
            .catch(function () {
                banner("warning", "Could not load what that connection can do. Try again.");
            });
    }

    function loadSchema(node) {
        var data = node.data || {};
        if (!data.connection_uuid || !data.operation_id) { return; }

        var key = schemaKey(node);
        if (state.schemaCache[key]) { markRequired(node); return; }

        fetch("/integrations/connections/" + data.connection_uuid +
            "/schema?operation_id=" + encodeURIComponent(data.operation_id))
            .then(function (response) { return response.json(); })
            .then(function (body) {
                state.schemaCache[key] = body;
                markRequired(node);
            })
            .catch(function () { /* The panel still works without it. */ });
    }

    /** Ring the destination fields the operation will not accept a record without. The
     *  same rule Publish enforces — said here first, while the panel is open. */
    function markRequired(node) {
        var schema = state.schemaCache[schemaKey(node)];
        if (!schema) { return; }

        var mapped = {};
        ((node.data || {}).mappings || []).forEach(function (m) {
            if (m.target && (m.source || m.const !== undefined)) { mapped[m.target] = true; }
        });

        var missing = (schema.required || []).filter(function (name) { return !mapped[name]; });
        if (!missing.length) { return; }

        var note = document.createElement("div");
        note.className = "alert alert-danger small mt-2";
        note.textContent = "Nothing is mapped to: " + missing.join(", ") +
            ". This workflow can be saved but not published until it is.";
        el.propsBody.appendChild(note);
    }

    // =====================================================================
    // SCHEDULE
    // =====================================================================

    function buildSchedule() {
        var trigger = state.triggers[0] || {};
        var node = state.nodes.filter(function (n) { return n.type === "trigger"; })[0];
        var nodeId = trigger.node_id || (node && node.id) || "";

        el.scheduleBody.innerHTML =
            '<form id="intScheduleForm">' +
            '<input type="hidden" name="node_id" value="' + GC.escapeAttr(nodeId) + '">' +
            '<input type="hidden" name="kind" value="schedule">' +
            '<div class="mb-3"><label class="form-label fw-semibold small">Run every</label>' +
            '<select class="form-select form-select-sm" name="interval_seconds">' +
            intervalOptions(trigger.interval_seconds) + "</select>" +
            '<div class="form-text">A minute is the shortest. Every fire is a run, a queue ' +
            "job and a compile, and a sync that takes longer than its interval spends its " +
            "life skipping.</div></div>" +
            '<div class="mb-3"><label class="form-label fw-semibold small">' +
            "If the last run is still going</label>" +
            '<select class="form-select form-select-sm" name="overlap_policy">' +
            '<option value="skip"' + (trigger.overlap_policy === "skip" ? " selected" : "") +
            ">Skip this slot (and say so)</option>" +
            '<option value="queue"' + (trigger.overlap_policy === "queue" ? " selected" : "") +
            ">Queue it, up to three</option>" +
            '<option value="cancel_previous"' +
            (trigger.overlap_policy === "cancel_previous" ? " selected" : "") +
            ">Stop the running one</option></select>" +
            '<div class="form-text">A skipped slot writes a run saying why. Doing nothing ' +
            "quietly looks exactly like a schedule that is working.</div></div>" +
            '<div class="form-check mb-3">' +
            '<input class="form-check-input" type="checkbox" name="is_enabled" ' +
            'id="intScheduleEnabled" value="true"' + (trigger.is_enabled ? " checked" : "") + ">" +
            '<label class="form-check-label" for="intScheduleEnabled">Enabled</label>' +
            '<div class="form-text">Needs a published version.</div></div>' +
            (trigger.next_run_at
                ? '<div class="alert alert-info small">Next run: ' +
                  GC.escapeHtml(String(trigger.next_run_at).slice(0, 19).replace("T", " ")) +
                  "</div>"
                : "") +
            '<button class="btn btn-primary btn-sm" type="submit">Save schedule</button>' +
            "</form>";

        document.getElementById("intScheduleForm").addEventListener("submit", saveSchedule);
    }

    function intervalOptions(current) {
        var choices = [
            [60, "minute"], [300, "5 minutes"], [900, "15 minutes"], [1800, "30 minutes"],
            [3600, "hour"], [21600, "6 hours"], [86400, "day"]
        ];
        return choices.map(function (choice) {
            return '<option value="' + choice[0] + '"' +
                (Number(current) === choice[0] ? " selected" : "") + ">" + choice[1] + "</option>";
        }).join("");
    }

    function saveSchedule(event) {
        event.preventDefault();
        var body = new FormData(event.target);

        fetch("/integrations/" + state.flowUuid + "/triggers", { method: "POST", body: body })
            .then(function (response) { return response.json(); })
            .then(function (result) {
                if (!result.ok) { banner("danger", result.error); return; }
                state.triggers = [result.trigger];
                buildSchedule();
                banner("success", "Schedule saved.");
            })
            .catch(function () { banner("danger", "Could not save the schedule."); });
    }

    // =====================================================================
    // TOOLBAR
    // =====================================================================

    function wireToolbar() {
        document.getElementById("intSaveBtn").addEventListener("click", save);
        document.getElementById("intPublishBtn").addEventListener("click", publish);
        document.getElementById("intRunBtn").addEventListener("click", function () { run("live"); });
        document.getElementById("intDryRunBtn").addEventListener("click", function () { run("dry_run"); });
        document.getElementById("intSelectAllBtn").addEventListener("click", function () {
            if (selection.count()) { selection.clear(); } else { selection.selectAll(); }
            // The canvas has to hold focus for Ctrl+A and Escape to reach it, and somebody
            // who has just pressed this button is about to want both.
            el.wrap.focus();
        });
        updateSelectAllButton();
        el.stopBtn.addEventListener("click", stop);
    }

    function save() {
        clearInvalid();

        return fetch("/integrations/" + state.flowUuid + "/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ graph_data: { nodes: state.nodes, edges: state.edges } })
        })
            .then(function (response) { return response.json(); })
            .then(function (result) {
                if (!result.ok) { refused(result); return false; }
                markClean();
                banner("success", "Saved.");
                return true;
            })
            .catch(function () {
                banner("danger", "Could not reach the server. Your drawing is still here.");
                return false;
            });
    }

    function publish() {
        // Saved first, deliberately: publishing snapshots what is *stored*, and publishing
        // a drawing that differs from what is on screen would freeze a version nobody drew.
        save().then(function (saved) {
            if (!saved) { return; }

            fetch("/integrations/" + state.flowUuid + "/publish", { method: "POST" })
                .then(function (response) { return response.json(); })
                .then(function (result) {
                    if (!result.ok) { refused(result); return; }
                    el.versionBadge.className = "badge bg-success";
                    el.versionBadge.textContent = "v" + result.version.version_number;
                    banner("success", "Published as version " + result.version.version_number +
                        ". This is what a schedule will run.");
                })
                .catch(function () { banner("danger", "Could not publish. Try again."); });
        });
    }

    function run(mode) {
        var body = new FormData();
        body.append("mode", mode);

        fetch("/integrations/" + state.flowUuid + "/runs", { method: "POST", body: body })
            .then(function (response) { return response.json(); })
            .then(function (result) {
                if (!result.ok) { refused(result); return; }
                watchRun(result.run_uuid);
            })
            .catch(function () { banner("danger", "Could not start the run."); });
    }

    function stop() {
        if (!state.run) { return; }
        fetch("/integrations/runs/" + state.run + "/stop", { method: "POST" })
            .then(function () {
                banner("info", "Stopping. A step already waiting on another system finishes " +
                    "that call first.");
            });
    }

    function refused(result) {
        banner("danger", result.error || "That was refused.");
        if (result.node_id) { highlightInvalid(result.node_id); }
    }

    function highlightInvalid(nodeId) {
        clearInvalid();
        var nodeEl = document.getElementById("int-node-" + nodeId);
        if (!nodeEl) { return; }
        nodeEl.classList.add("int-invalid");
        nodeEl.scrollIntoView({ behavior: "smooth", block: "center" });
    }

    function clearInvalid() {
        Array.prototype.forEach.call(
            el.nodes.querySelectorAll(".int-invalid"),
            function (node) { node.classList.remove("int-invalid"); }
        );
    }

    // =====================================================================
    // WATCHING A RUN
    // =====================================================================

    function watchRun(runUuid) {
        closeStream();

        state.run = runUuid;
        state.finished = false;
        el.stopBtn.disabled = false;

        var source = new EventSource("/integrations/runs/" + runUuid + "/events");
        state.source = source;

        // **A named SSE event never reaches `onmessage`.** Every name the server can send
        // is registered here; the list comes from `_event_name`, which derives it from the
        // run's own status so the two cannot drift apart in meaning — only in this list,
        // which is why it is short.
        ["progress", "succeeded", "partial", "failed", "cancelled", "awaiting"]
            .forEach(function (name) {
                source.addEventListener(name, function (event) { onFrame(event, name); });
            });

        source.onerror = function (event) {
            // A server `error` *with* data is a sentence to show. Every ordinary close
            // also arrives here with no data, which is why `finished` is the only way to
            // tell a completed run from a dropped connection.
            if (event && event.data) {
                banner("danger", String(event.data));
            }
            closeStream();
            if (!state.finished) { startPolling(runUuid); }
        };
    }

    function onFrame(event, name) {
        var frame;
        try {
            frame = JSON.parse(event.data);
        } catch (err) {
            return;
        }

        paint(frame);

        if (name !== "progress" && name !== "awaiting") {
            state.finished = true;
            // `close()` before anything that can throw — a handler that failed after this
            // point would otherwise leave the stream open and reconnecting forever.
            closeStream();
            el.stopBtn.disabled = true;
        }
    }

    function startPolling(runUuid) {
        stopPolling();
        state.poller = setInterval(function () {
            fetch("/integrations/runs/" + runUuid)
                .then(function (response) { return response.json(); })
                .then(function (frame) {
                    paint(frame);
                    if (isTerminal(frame.status)) { stopPolling(); el.stopBtn.disabled = true; }
                })
                .catch(function () { stopPolling(); });
        }, POLL_MS);
    }

    function isTerminal(status) {
        return ["succeeded", "partial", "failed", "cancelled", "skipped"].indexOf(status) >= 0;
    }

    function closeStream() {
        if (state.source) { state.source.close(); state.source = null; }
        stopPolling();
    }

    function stopPolling() {
        if (state.poller) { clearInterval(state.poller); state.poller = null; }
    }

    /** Repaint the dock from one whole frame. Nothing accumulates: every number here is
     *  absolute, so a frame the browser missed cannot leave a total wrong. */
    function paint(frame) {
        var counts = frame.counts || {};
        el.counts.read.textContent = counts.read || 0;
        el.counts.written.textContent = counts.written || 0;
        el.counts.failed.textContent = counts.failed || 0;
        el.counts.skipped.textContent = counts.skipped || 0;

        el.truncated.style.display = frame.records_log_truncated ? "" : "none";

        el.runStatus.className = "badge bg-" + tone(frame.status);
        el.runStatus.textContent = frame.cancel_requested && !isTerminal(frame.status)
            ? "stopping" : (frame.status || "");

        if (frame.mode === "dry_run") {
            el.runMode.style.display = "";
            el.runMode.textContent = "dry run — nothing was sent";
        } else {
            el.runMode.style.display = "none";
        }

        el.runStarted.textContent = frame.started_at
            ? String(frame.started_at).slice(0, 19).replace("T", " ") : "";

        paintSteps(frame.steps || [], frame.steps_total || 0);
        paintNodeStatuses(frame.steps || []);

        if (frame.error_message) { banner("danger", frame.error_message); }
    }

    function paintSteps(steps, total) {
        if (!steps.length) { el.steps.innerHTML = "No steps yet."; return; }

        var html = steps.map(function (step) {
            return (
                '<div class="int-step-line">' +
                '<span class="int-step-name">' + GC.escapeHtml(step.node_label || step.node_id) +
                "</span>" +
                '<span class="badge bg-' + tone(step.status) + '">' +
                GC.escapeHtml(step.status) + "</span>" +
                '<span class="text-muted">' + step.records_in + " in / " +
                step.records_out + " out" +
                (step.is_rollup ? " · " + step.rollup_count + " passes" : "") + "</span>" +
                (step.message ? '<span class="int-step-msg">' +
                    GC.escapeHtml(step.message) + "</span>" : "") +
                "</div>"
            );
        }).join("");

        // The window, and how much of the log it is. A run of fifty thousand records
        // collapses its step rows, so "showing 100 of 4231" is the honest statement —
        // without it the list reads as the whole thing.
        if (total > steps.length) {
            html += '<div class="text-muted mt-1">Showing the last ' + steps.length +
                " of " + total + " steps.</div>";
        }

        el.steps.innerHTML = html;
    }

    /** One ring per step, showing where that step stands — however many passes it made.
     *  Keyed by node id rather than by step, because the canvas draws one shape per step
     *  and the log lists the passes. */
    function paintNodeStatuses(steps) {
        var latest = {};
        steps.forEach(function (step) { latest[step.node_id] = step; });

        Array.prototype.forEach.call(
            el.nodes.querySelectorAll("[data-int-status]"),
            function (badge) {
                var step = latest[badge.getAttribute("data-int-status")];
                if (!step) { badge.textContent = ""; badge.className = "int-node-status"; return; }
                badge.className = "int-node-status badge bg-" + tone(step.status);
                badge.textContent = step.status;
            }
        );
    }

    function tone(status) {
        return {
            succeeded: "success",
            partial: "warning",
            failed: "danger",
            cancelled: "secondary",
            skipped: "secondary",
            running: "primary",
            queued: "info",
            awaiting_input: "info"
        }[status] || "secondary";
    }

    // =====================================================================
    // SMALL THINGS
    // =====================================================================

    function markDirty() {
        state.dirty = true;
        el.unsaved.style.display = "";
    }

    function markClean() {
        state.dirty = false;
        el.unsaved.style.display = "none";
    }

    function banner(tone_, message) {
        el.banner.innerHTML =
            '<div class="alert alert-' + tone_ + ' alert-dismissible fade show mb-0">' +
            GC.escapeHtml(String(message)) +
            '<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>';
    }

    return { init: init };
})();
