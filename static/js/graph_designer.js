/**
 * Graph Designer canvas — author a graph, run it, and watch it run.
 *
 * Vanilla JS on top of `graph_canvas.js`, which owns the Bezier maths, the port
 * measurement, the escaping and the id generator. This file owns everything that knows
 * what a *graph* node means: the palette, the ports each type exposes, the properties
 * forms, the selection model, save/load, and the run dock.
 *
 * SECURITY. Nodes are built with `createElement` and filled with `textContent`; the two
 * places that assemble markup as a string go through `GC.escapeHtml`/`escapeAttr`. Every
 * label on this canvas is a node name, table name, column name or SQL fragment out of
 * the user's own database — the same rule `tool_configs.js`, `tool_chain.js` and
 * `tool_graphs.js` each state at the top of their files.
 *
 * THE NODE VOCABULARY COMES FROM THE SERVER. `#gdVocabulary` carries the node types, the
 * value kinds and the condition operators. They are not restated here, because these
 * lists decide what `graph_service.validate_graph` will accept: a palette offering a node
 * type the service refuses, or an operator it does not know, is a form that can only be
 * filled in wrongly.
 *
 * THE STREAM. Three things about `EventSource` are load-bearing and each one has bitten
 * this codebase before (see the notes in `deep_agent_stream.js`):
 *
 *   1. the browser reconnects automatically to a stream that ended, so `close()` must run
 *      before anything that can throw;
 *   2. *every* close arrives as an `error` event with no data — success included — so a
 *      `finished` flag is the only way to tell an expected end from a dropped connection;
 *   3. a server-sent `error` *with* data carries a sentence meant for the operator.
 *
 * When the stream drops mid-run the dock falls back to polling the run's status endpoint,
 * warning the console once rather than on every tick.
 */
window.GraphDesigner = (function () {
    "use strict";

    const GC = window.GraphCanvas;
    const genId = GC.makeIdGenerator();

    // How often the fallback poller asks, once the stream has failed. Four seconds:
    // slow enough not to matter, fast enough that a dock nobody is streaming to still
    // moves. Matches the download card's fallback interval.
    const POLL_MS = 4000;

    // Every event name the run stream can carry. The server names each frame after the
    // run's status (`_event_name` in graph_designer_routes.py), and a *named* SSE event
    // never reaches `onmessage` — so each one needs its own listener. Missing a name here
    // means silently ignoring that state.
    const FRAME_EVENTS = ["progress", "awaiting", "succeeded", "failed", "cancelled"];

    // Which ports each node type exposes, and what to call them. Derived per type
    // exactly as flow_builder derives `if_else`'s and `menu`'s, so a branch's ports
    // follow its conditions and a loop always has its two.
    const PORTS = {
        start: function () { return [{ port: "default", label: "" }]; },
        sql: function () {
            return [
                { port: "default", label: "" },
                { port: "error", label: "on error", kind: "error" },
            ];
        },
        sql_union: function () {
            // `next` rather than a blank label: this node's ordinary output goes back round
            // the loop, and `execute` is the one that leaves it, so an unlabelled dot beside
            // a labelled one would read as the way out.
            return [
                { port: "default", label: "next" },
                { port: "execute", label: "execute" },
                { port: "error", label: "on error", kind: "error" },
            ];
        },
        value: function () { return [{ port: "default", label: "" }]; },
        tool_config: function () {
            return [
                { port: "default", label: "" },
                { port: "error", label: "on error", kind: "error" },
            ];
        },
        human: function () { return [{ port: "default", label: "" }]; },
        branch: function (data) {
            const ports = (data.conditions || []).map(function (c) {
                return { port: c.port || "", label: c.label || c.port || "" };
            });
            // `else` is always offered, because a branch whose conditions all fail has to
            // go somewhere and the server treats the name as reserved.
            ports.push({ port: "else", label: "else" });
            return ports;
        },
        for_each: function () {
            return [
                { port: "body", label: "each" },
                { port: "done", label: "done" },
            ];
        },
        do_until: function () {
            return [
                { port: "body", label: "body" },
                { port: "done", label: "done" },
            ];
        },
        email: function () {
            // `error` is for a refusal that is knowable now — an unresolvable binding, a
            // switched-off template. A relay refusing the message tomorrow is not knowable
            // now and cannot route anywhere; that lives in the delivery log.
            return [
                { port: "default", label: "queued" },
                { port: "error", label: "not sent", kind: "error" },
            ];
        },
        // Both outcome nodes offer one exit. They decide what the run is *reported* as,
        // which is settled the moment they run — so anything drawn after them still runs,
        // and cannot change that verdict. Leaving it connected to nothing is the ordinary
        // case and ends the run, exactly as it did when these had no exit at all.
        //
        // "then" rather than "next" or "default": it reads as a consequence of the outcome,
        // which is what it is. The one thing it may not lead to is the other outcome node,
        // and the server says so by name if you try.
        // One exit each. A timer's illegal transition — stopping one nobody started — is
        // an authoring mistake, not a condition to route around, and carrying on with a
        // timer in an undefined state is worse than stopping. A wait cannot fail for a
        // reason a graph could act on; a bad duration is refused at save.
        timer: function () { return [{ port: "default", label: "" }]; },
        wait: function () { return [{ port: "default", label: "" }]; },
        success: function () { return [{ port: "default", label: "then" }]; },
        failure: function () { return [{ port: "default", label: "then" }]; },
    };

    const ICONS = {
        start: "la-play-circle",
        sql: "la-database",
        value: "la-list-ul",
        tool_config: "la-tools",
        human: "la-user-clock",
        branch: "la-code-branch",
        for_each: "la-redo",
        do_until: "la-sync",
        email: "la-envelope",
        timer: "la-stopwatch",
        wait: "la-hourglass-half",
        success: "la-check-circle",
        failure: "la-times-circle",
    };

    const STATUS_COLOURS = {
        running: "bg-primary",
        succeeded: "bg-success",
        failed: "bg-danger",
        skipped: "bg-secondary",
        awaiting_input: "bg-warning text-dark",
        cancelled: "bg-secondary",
    };

    const state = {
        nodes: [],
        edges: [],
        selectedNodeId: null,
        selectedEdgeId: null,
        picked: {},          // node ids chosen for a test run
        pending: null,       // {nodeId, port} — a connection armed by clicking a port
        connecting: null,    // {nodeId, port, startX, startY, moved} — one being dragged out
        dragging: null,
        reattaching: null,
        dirty: false,
        vocabulary: { node_types: [], value_kinds: [], operators: [], default_max_iterations: 200 },
        options: null,       // datasources / tool configs, fetched once
        run: null,           // the latest run frame
        source: null,        // the EventSource
        pollTimer: null,
        finished: false,
        warnedFallback: false,
    };

    let opts = {};
    let canvasEl, edgesGroupEl, wrapperEl, paletteBodyEl, propertiesBodyEl;

    // Resolved once from the stylesheet on the first node rendered — see `portMetrics`.
    let portMetricsCache = null;

    // True for one tick after a drag that actually moved a node, so the click that trails
    // the mouseup is not mistaken for a deliberate click on that node. See `onDragEnd`.
    let suppressNodeClick = false;

    // A press that travels less than this is a click, not a drag. Used by both drags: on a
    // port, so a plain click still arms the click-then-click gesture; on a node, so a click
    // that merely jitters is not treated as a move.
    const DRAG_THRESHOLD_PX = 4;

    // -----------------------------------------------------------------
    // Dirty tracking
    // -----------------------------------------------------------------

    function markDirty() {
        state.dirty = true;
        const badge = document.getElementById("gdUnsavedBadge");
        if (badge) badge.style.display = "";
    }

    function clearDirty() {
        state.dirty = false;
        const badge = document.getElementById("gdUnsavedBadge");
        if (badge) badge.style.display = "none";
    }

    // -----------------------------------------------------------------
    // Model helpers
    // -----------------------------------------------------------------

    function findNode(id) {
        return state.nodes.find(function (n) { return n.id === id; }) || null;
    }

    function findEdge(id) {
        return state.edges.find(function (e) { return e.id === id; }) || null;
    }

    function portsOf(node) {
        const builder = PORTS[node.type];
        return builder ? builder(node.data || {}) : [];
    }

    /** Is this one of the two nodes that decide what the run is reported as? */
    function isOutcome(node) {
        return !!node && (node.type === "success" || node.type === "failure");
    }

    // Refused on the canvas as well as on the server, for the same reason the
    // self-connection is: the server's version of this sentence arrives at save time and
    // this one arrives while the connector is still in your hand. Kept as one constant so
    // the two gestures that can draw such an edge cannot drift into wording it differently.
    const OUTCOME_CHAIN_REFUSAL =
        "That node already decides how the run ends, so it cannot lead to another " +
        "outcome — the first one is the one reported.";

    function labelOf(node) {
        const explicit = ((node.data || {}).label || "").trim();
        if (explicit) return explicit;
        const entry = state.vocabulary.node_types.find(function (t) { return t.type === node.type; });
        return entry ? entry.label : node.type;
    }

    /**
     * The default `data` for a newly added node.
     *
     * Each type starts with the fields its form edits, so a form never has to cope with
     * a missing key and a node saved straight after being added is refused for the one
     * thing that is genuinely missing rather than for three.
     */
    function defaultData(type) {
        const label = (state.vocabulary.node_types.find(function (t) { return t.type === type; }) || {}).label || type;
        const base = { label: label };

        switch (type) {
            case "sql":
            // A union node holds exactly what a SQL node holds — one statement, its
            // datasource, its tables, its parameters. What differs is when it runs, so
            // there is no extra field to default.
            case "sql_union":
                return Object.assign(base, {
                    datasource_id: "", sql_query: "", table_names: [], params: [], bindings: {},
                    variables: {},
                });
            case "value":
                return Object.assign(base, {
                    value_kind: "list", value_json: "[]", variables: {},
                });
            case "tool_config":
                return Object.assign(base, { tool_config_id: "" });
            case "human":
                return Object.assign(base, {
                    prompt: "", expects: "confirm", choices: [], variables: {},
                });
            case "branch":
                return Object.assign(base, { conditions: [] });
            case "for_each":
                return Object.assign(base, {
                    source_node: "", item_name: "item",
                    max_iterations: state.vocabulary.default_max_iterations,
                    // Blank means "collect nothing", which is what every loop drawn before
                    // collection existed did — so the default changes no behaviour.
                    collect_from: "", label_item_as: "",
                });
            case "do_until":
                return Object.assign(base, {
                    condition: { source_node: "", operator: "not_empty", value: "", field: "" },
                    max_iterations: state.vocabulary.default_max_iterations,
                });
            case "email":
                return Object.assign(base, {
                    template_id: "", smtp_config_id: "",
                    recipients: { to: [], cc: [], bcc: [] },
                    variable_bindings: {},
                });
            // A timer instance is a Start until told otherwise, because a Start is the
            // only one that is valid on its own — the other three need a timer to name,
            // and defaulting to one of those would create a node that cannot be saved.
            case "timer":
                return Object.assign(base, { action: "start", timer_node: "" });
            case "wait":
                return Object.assign(base, {
                    seconds: state.vocabulary.default_wait_seconds || 30,
                });
            case "success":
            case "failure":
                return Object.assign(base, { message: "", variables: {} });
            default:
                return base;
        }
    }

    /** A one-line summary shown inside the node box. */
    function previewOf(node) {
        const d = node.data || {};

        switch (node.type) {
            case "sql":
                return d.sql_query ? d.sql_query : "(no statement yet)";
            case "sql_union":
                // Says what it does with the statement, because the statement alone is
                // indistinguishable from a SQL node's and the two behave quite differently.
                return (d.sql_query || "(no statement yet)") + "\nUNION, one copy per pass";
            case "value":
                return (d.value_kind || "list") + ": " + (d.value_json || "");
            case "tool_config": {
                const tool = optionLabel("tool_configs", d.tool_config_id);
                return tool ? "runs " + tool : "(no tool selected)";
            }
            case "human":
                return d.prompt ? d.prompt : "(no question yet)";
            case "branch":
                return (d.conditions || []).length + " condition(s), then else";
            case "for_each": {
                const src = d.source_node ? nodeLabelById(d.source_node) : "?";
                // A collecting loop says so on the canvas: it produces something quite
                // different from one that does not, and that should not need a click.
                return "each of " + src + (d.collect_from
                    ? "\ncollects " + nodeLabelById(d.collect_from)
                    : "");
            }
            case "do_until": {
                const c = d.condition || {};
                return "until " + (c.source_node ? nodeLabelById(c.source_node) : "?") +
                    " " + (c.operator || "");
            }
            case "email": {
                // This case was missing, so an Email node's box body was blank and the
                // only way to see what it sent was to open the panel.
                const template = optionLabel("email_templates", d.template_id);
                const to = ((d.recipients || {}).to || []).join(", ");
                return (template ? "sends " + template : "(no template selected)")
                    + (to ? "\nto " + to : "");
            }
            case "timer": {
                const action = d.action || "start";
                if (action === "start") return "starts timing";
                return action + "s " + (d.timer_node ? nodeLabelById(d.timer_node) : "?");
            }
            case "wait":
                // The restart caveat goes on the box rather than only in the panel: it is
                // the one thing about this node somebody needs to know without clicking,
                // and it is cheapest to read here.
                return "waits " + (d.seconds || 0) + "s\ndoes not survive a restart";
            case "success":
            case "failure":
                return d.message || "";
            default:
                return "";
        }
    }

    function nodeLabelById(id) {
        const node = findNode(id);
        return node ? labelOf(node) : "(missing node)";
    }

    function optionLabel(group, uuid) {
        if (!state.options || !uuid) return "";
        const found = (state.options[group] || []).find(function (o) { return o.uuid === uuid; });
        return found ? found.label : "(no longer available)";
    }

    // -----------------------------------------------------------------
    // Rendering — nodes
    // -----------------------------------------------------------------

    function renderAllNodes() {
        canvasEl.innerHTML = "";
        state.nodes.forEach(renderNode);

        // A connector armed by clicking a port is otherwise signalled only by a 12px dot
        // changing colour. The crosshair says the canvas is waiting for a target, which is
        // the difference between a mode the user can see and one they have to remember.
        wrapperEl.classList.toggle("gd-connecting", !!state.pending);
    }

    function renderNode(node) {
        const el = document.createElement("div");
        el.className = "gd-node";
        el.id = "node-" + node.id;
        el.dataset.nodeId = node.id;
        el.style.left = ((node.position || {}).x || 0) + "px";
        el.style.top = ((node.position || {}).y || 0) + "px";

        if (node.id === state.selectedNodeId) el.classList.add("gd-node-selected");
        if (state.picked[node.id]) el.classList.add("gd-node-picked");

        // --- header ---
        const header = document.createElement("div");
        header.className = "gd-node-header";
        header.dataset.role = "drag-handle";

        const icon = document.createElement("i");
        icon.className = "las " + (ICONS[node.type] || "la-question-circle");
        header.appendChild(icon);

        const title = document.createElement("span");
        title.className = "gd-node-title";
        title.textContent = labelOf(node);
        header.appendChild(title);

        const badge = document.createElement("span");
        badge.className = "badge gd-node-status-badge";
        badge.dataset.role = "status-badge";
        badge.hidden = true;
        header.appendChild(badge);

        const icons = document.createElement("div");
        icons.className = "gd-node-header-icons";

        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "gd-node-icon-btn";
        editBtn.title = "Settings";
        editBtn.innerHTML = '<i class="las la-cog"></i>';
        editBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            openProperties(node.id);
        });
        icons.appendChild(editBtn);

        // Start is not deletable: `validate_graph` requires exactly one, so offering the
        // button would offer a state the save refuses.
        if (node.type !== "start") {
            const delBtn = document.createElement("button");
            delBtn.type = "button";
            delBtn.className = "gd-node-icon-btn gd-node-icon-btn-danger";
            delBtn.title = "Delete node";
            delBtn.innerHTML = '<i class="las la-trash"></i>';
            delBtn.addEventListener("click", function (e) {
                e.stopPropagation();
                deleteNode(node.id);
            });
            icons.appendChild(delBtn);
        }

        header.appendChild(icons);
        header.addEventListener("mousedown", function (e) {
            if (e.button === 0) startDrag(node.id, e);
        });
        el.appendChild(header);

        // --- body ---
        const body = document.createElement("div");
        body.className = "gd-node-body";

        const kind = document.createElement("div");
        kind.className = "gd-node-kind";
        kind.textContent = (state.vocabulary.node_types.find(function (t) {
            return t.type === node.type;
        }) || {}).label || node.type;
        body.appendChild(kind);

        const preview = document.createElement("div");
        preview.className = "gd-node-preview";
        preview.textContent = previewOf(node);
        body.appendChild(preview);

        body.addEventListener("click", function (e) {
            // Shift-click picks a node for a test run; a plain click selects it for
            // editing. Two different questions, so two different gestures.
            if (e.shiftKey) {
                e.stopPropagation();
                togglePicked(node.id);
                return;
            }
            onTargetPortClick(node.id, e);
        });
        el.appendChild(body);

        // --- ports ---
        const inPort = document.createElement("div");
        inPort.className = "gd-node-port gd-node-port-in";
        inPort.dataset.portRole = "in";
        inPort.title = "Drop a connector here, or click to finish one";
        if (node.type === "start") inPort.style.display = "none";
        // The dot is the obvious place to aim a connector at, so it has to accept one. It
        // used to be inert — only the node's body completed a connection — which made the
        // whole feature read as broken to anyone who clicked the circle.
        inPort.addEventListener("click", function (e) {
            e.stopPropagation();
            onTargetPortClick(node.id, e);
        });
        el.appendChild(inPort);

        const outWrap = document.createElement("div");
        outWrap.className = "gd-node-ports-out";

        portsOf(node).forEach(function (spec) {
            const row = document.createElement("div");
            row.className = "gd-node-port-out-row";
            if (spec.kind) row.dataset.portKind = spec.kind;

            if (spec.label) {
                const lbl = document.createElement("span");
                lbl.className = "gd-node-port-out-label";
                lbl.textContent = spec.label;
                row.appendChild(lbl);
            }

            const dot = document.createElement("div");
            dot.className = "gd-node-port gd-node-port-out";
            dot.dataset.port = spec.port;
            dot.title = "Drag to a node to connect, or click here and then click the target";
            dot.style.position = "static";
            dot.style.transform = "none";
            if (state.pending && state.pending.nodeId === node.id && state.pending.port === spec.port) {
                dot.classList.add("gd-port-pending");
            }
            dot.addEventListener("click", function (e) {
                e.stopPropagation();
                onSourcePortClick(node.id, spec.port);
            });
            // Both gestures from one dot. `mousedown` opens a drag; a drag that never moves
            // does nothing on release, leaving the `click` that follows to arm the
            // click-then-click gesture instead. The two cannot both fire for one press,
            // because a real drag releases over another element and so produces no `click`
            // on this dot at all.
            dot.addEventListener("mousedown", function (e) {
                if (e.button !== 0) return;
                e.stopPropagation();
                startConnectDrag(node.id, spec.port, e);
            });
            row.appendChild(dot);
            outWrap.appendChild(row);
        });

        // Any part of the box finishes a connection that is already armed — the header
        // included, which is otherwise the one place on a node where a click does nothing and
        // the gesture appears to have failed. Only when armed: an ordinary click on the
        // header still just drags.
        el.addEventListener("click", function (e) {
            if (!state.pending || suppressNodeClick) return;
            e.stopPropagation();
            onTargetPortClick(node.id, e);
        });

        el.appendChild(outWrap);
        canvasEl.appendChild(el);

        // The stack starts below the header (graph_designer.css pins that so a port's opaque
        // label cannot cover the Settings and Delete buttons), so the node has to be tall
        // enough to hold it — otherwise a branch with several conditions hangs its lower
        // ports off the bottom edge.
        //
        // The stack's height is *measured*, not predicted from the port count: a row is as
        // tall as its label, not as its dot, so arithmetic over `--gd-port-size` comes out
        // short by a few pixels per row — which is exactly how far the last port hung out of
        // the box. Reading it back needs the element in the document, hence after the append.
        if (outWrap.childElementCount) {
            const m = portMetrics(el);
            el.style.minHeight =
                (m.header + m.gap + outWrap.getBoundingClientRect().height + m.gap) + "px";
        }
    }

    /**
     * Where the port stack starts, in pixels, per the stylesheet.
     *
     * Read from CSS rather than restated here so the two cannot drift: the header's height
     * and the gap are the stylesheet's decisions (`--gd-header-h`, `--gd-port-gap`), and
     * this only needs to know what they came out as. Cached — every node in a render pass
     * resolves to the same two values.
     *
     * @param {HTMLElement} nodeEl - a node already in the document
     * @returns {{header: number, gap: number}}
     */
    function portMetrics(nodeEl) {
        if (portMetricsCache) return portMetricsCache;

        const style = window.getComputedStyle(nodeEl);
        const read = function (name, fallback) {
            const value = parseFloat(style.getPropertyValue(name));
            return isFinite(value) && value > 0 ? value : fallback;
        };

        portMetricsCache = { header: read("--gd-header-h", 30), gap: read("--gd-port-gap", 6) };
        return portMetricsCache;
    }

    // -----------------------------------------------------------------
    // Rendering — connectors
    // -----------------------------------------------------------------

    function portAnchor(nodeId, portSelector) {
        return GC.portAnchor(wrapperEl, document.getElementById("node-" + nodeId), portSelector);
    }

    function edgeGeometry(edge) {
        const from = portAnchor(
            edge.source,
            '.gd-node-port-out[data-port="' + GC.cssEscape(edge.source_port || "default") + '"]',
        );
        const to = portAnchor(edge.target, '[data-port-role="in"]') || portAnchor(edge.target, null);
        return GC.geometry(from, to);
    }

    function renderAllEdges() {
        edgesGroupEl.innerHTML = "";
        state.edges.forEach(renderEdge);
    }

    function renderEdge(edge) {
        const g = edgeGeometry(edge);
        if (!g) return;

        const group = GC.svg("g");
        group.id = "edge-group-" + edge.id;

        const path = GC.svg("path");
        path.id = "edge-" + edge.id;
        path.setAttribute("d", GC.pathD(g));

        const port = edge.source_port || "default";
        if (port === "error") path.classList.add("gd-edge-error");
        if (port === "body") path.classList.add("gd-edge-loop");
        if (edge.id === state.selectedEdgeId) path.classList.add("gd-edge-selected");

        path.addEventListener("click", function (e) {
            e.stopPropagation();
            state.selectedEdgeId = edge.id;
            renderAllEdges();
        });
        group.appendChild(path);

        // The port's name on the curve, so a graph with several outcomes leaving one node
        // is readable without clicking anything.
        if (port !== "default") {
            const mid = GC.pointAt(g, 0.5);
            const text = GC.svg("text");
            text.setAttribute("x", mid.x);
            text.setAttribute("y", mid.y - 6);
            text.setAttribute("text-anchor", "middle");
            text.setAttribute("class", "gd-edge-label");
            text.textContent = port;
            group.appendChild(text);
        }

        group.appendChild(deleteButton(edge.id, GC.pointAt(g, 0.5)));
        group.appendChild(endHandle(edge.id, "source", GC.pointAt(g, 0.15)));
        group.appendChild(endHandle(edge.id, "target", GC.pointAt(g, 0.85)));

        edgesGroupEl.appendChild(group);
    }

    function deleteButton(edgeId, pt) {
        const g = GC.svg("g");
        g.setAttribute("class", "gd-edge-delete-btn");
        g.setAttribute("transform", "translate(" + pt.x + "," + (pt.y + 10) + ")");

        const circle = GC.svg("circle");
        circle.setAttribute("r", "8");
        g.appendChild(circle);

        const text = GC.svg("text");
        text.setAttribute("text-anchor", "middle");
        text.setAttribute("dy", "4");
        text.textContent = "×";
        g.appendChild(text);

        g.addEventListener("click", function (e) {
            e.stopPropagation();
            deleteEdge(edgeId);
        });
        return g;
    }

    function endHandle(edgeId, end, pt) {
        const circle = GC.svg("circle");
        circle.setAttribute("class", "gd-edge-handle gd-edge-handle-" + end);
        circle.setAttribute("r", "5");
        circle.setAttribute("cx", pt.x);
        circle.setAttribute("cy", pt.y);
        circle.addEventListener("mousedown", function (e) {
            e.stopPropagation();
            startReattach(edgeId, end, e);
        });
        return circle;
    }

    function redrawEdgesForNode(nodeId) {
        state.edges.forEach(function (edge) {
            if (edge.source === nodeId || edge.target === nodeId) {
                const group = document.getElementById("edge-group-" + edge.id);
                if (group) group.remove();
                renderEdge(edge);
            }
        });
    }

    // -----------------------------------------------------------------
    // Dragging
    // -----------------------------------------------------------------

    function startDrag(nodeId, e) {
        e.preventDefault();
        const node = findNode(nodeId);
        if (!node) return;

        const rect = wrapperEl.getBoundingClientRect();
        const x = e.clientX + wrapperEl.scrollLeft - rect.left;
        const y = e.clientY + wrapperEl.scrollTop - rect.top;

        state.dragging = {
            nodeId: nodeId,
            offsetX: x - ((node.position || {}).x || 0),
            offsetY: y - ((node.position || {}).y || 0),
            startX: e.clientX,
            startY: e.clientY,
            moved: false,
        };
        document.addEventListener("mousemove", onDragMove);
        document.addEventListener("mouseup", onDragEnd);
    }

    function onDragMove(e) {
        if (!state.dragging) return;
        const node = findNode(state.dragging.nodeId);
        if (!node) return;

        const rect = wrapperEl.getBoundingClientRect();
        const x = e.clientX + wrapperEl.scrollLeft - rect.left - state.dragging.offsetX;
        const y = e.clientY + wrapperEl.scrollTop - rect.top - state.dragging.offsetY;

        node.position = { x: Math.max(0, x), y: Math.max(0, y) };
        if (Math.abs(e.clientX - state.dragging.startX) +
            Math.abs(e.clientY - state.dragging.startY) >= DRAG_THRESHOLD_PX) {
            state.dragging.moved = true;
        }

        const el = document.getElementById("node-" + node.id);
        if (el) {
            el.style.left = node.position.x + "px";
            el.style.top = node.position.y + "px";
        }
        redrawEdgesForNode(node.id);
    }

    function onDragEnd() {
        if (state.dragging) {
            markDirty();
            // A click always follows the mouseup that ended a drag. If the node really moved,
            // that click is the tail of the drag and must not be read as "and this node is
            // the connector's target" — cleared on the next tick, once the click has passed.
            if (state.dragging.moved) {
                suppressNodeClick = true;
                setTimeout(function () { suppressNodeClick = false; }, 0);
            }
        }
        state.dragging = null;
        document.removeEventListener("mousemove", onDragMove);
        document.removeEventListener("mouseup", onDragEnd);
    }

    // -----------------------------------------------------------------
    // Connecting
    // -----------------------------------------------------------------

    function onSourcePortClick(nodeId, port) {
        state.pending = { nodeId: nodeId, port: port };
        renderAllNodes();
        renderAllEdges();
    }

    function onTargetPortClick(nodeId) {
        if (!state.pending) {
            selectNode(nodeId);
            return;
        }
        completeConnection(nodeId);
    }

    function completeConnection(targetId) {
        const p = state.pending;
        state.pending = null;

        if (!p || p.nodeId === targetId) {
            // A self-connection is refused here as well as on the server, because the
            // server's message would arrive at save time and this one arrives now.
            renderAllNodes();
            renderAllEdges();
            return;
        }

        const target = findNode(targetId);
        if (target && target.type === "start") {
            flash("Nothing can connect into the Start node.");
            renderAllNodes();
            renderAllEdges();
            return;
        }

        if (isOutcome(findNode(p.nodeId)) && isOutcome(target)) {
            flash(OUTCOME_CHAIN_REFUSAL);
            renderAllNodes();
            renderAllEdges();
            return;
        }

        const taken = state.edges.some(function (e) {
            return e.source === p.nodeId && (e.source_port || "default") === p.port;
        });
        if (taken) {
            flash("That outcome already leads somewhere. Delete the existing connector first.");
            renderAllNodes();
            renderAllEdges();
            return;
        }

        state.edges.push({
            id: genId("e"),
            source: p.nodeId,
            source_port: p.port,
            target: targetId,
        });
        markDirty();
        renderAllNodes();
        renderAllEdges();
    }

    /**
     * Begin dragging a new connector out of an output port.
     *
     * Shares its drop resolution with `startReattach` — the highlight, the hit test and the
     * "dropped on nothing" case are the same question asked of a new connector rather than an
     * existing one, so they are the same code.
     *
     * @param {string} nodeId
     * @param {string} port
     * @param {MouseEvent} e
     */
    function startConnectDrag(nodeId, port, e) {
        e.preventDefault();
        state.connecting = {
            nodeId: nodeId, port: port, startX: e.clientX, startY: e.clientY, moved: false,
        };
        document.addEventListener("mousemove", onConnectDragMove);
        document.addEventListener("mouseup", onConnectDragEnd);
    }

    function onConnectDragMove(e) {
        const drag = state.connecting;
        if (!drag) return;

        if (!drag.moved) {
            const travelled = Math.abs(e.clientX - drag.startX) + Math.abs(e.clientY - drag.startY);
            if (travelled < DRAG_THRESHOLD_PX) return;
            drag.moved = true;
            wrapperEl.classList.add("gd-connecting");
        }

        highlightDropTarget(e);
        drawConnectPreview(drag, e);
    }

    function onConnectDragEnd(e) {
        const drag = state.connecting;
        state.connecting = null;
        document.removeEventListener("mousemove", onConnectDragMove);
        document.removeEventListener("mouseup", onConnectDragEnd);
        wrapperEl.classList.remove("gd-connecting");
        clearConnectPreview();
        document.querySelectorAll(".gd-drop-target").forEach(function (el) {
            el.classList.remove("gd-drop-target");
        });

        // Not a drag: let the click that follows arm the click-then-click gesture.
        if (!drag || !drag.moved) return;

        const under = document.elementFromPoint(e.clientX, e.clientY);
        const nodeEl = under && under.closest ? under.closest(".gd-node") : null;

        // Dropped on empty canvas. Nothing is armed, so the rubber band simply disappears
        // rather than leaving a half-made connection the user cannot see.
        if (!nodeEl) return;

        state.pending = { nodeId: drag.nodeId, port: drag.port };
        completeConnection(nodeEl.dataset.nodeId);
    }

    /** The rubber band, from the source port to the cursor, while a connector is dragged. */
    function drawConnectPreview(drag, e) {
        clearConnectPreview();

        const nodeEl = document.getElementById("node-" + drag.nodeId);
        const from = GC.portAnchor(
            wrapperEl, nodeEl,
            '.gd-node-port-out[data-port="' + GC.cssEscape(drag.port) + '"]',
        );
        if (!from) return;

        const rect = wrapperEl.getBoundingClientRect();
        const to = {
            x: e.clientX - rect.left + wrapperEl.scrollLeft,
            y: e.clientY - rect.top + wrapperEl.scrollTop,
        };

        const path = GC.svg("path");
        path.setAttribute("id", "gd-connect-preview");
        path.setAttribute("class", "gd-edge-preview");
        path.setAttribute("d", GC.pathD(GC.geometry(from, to)));
        edgesGroupEl.appendChild(path);
    }

    function clearConnectPreview() {
        const existing = document.getElementById("gd-connect-preview");
        if (existing) existing.remove();
    }

    function startReattach(edgeId, end, e) {
        e.preventDefault();
        state.reattaching = { edgeId: edgeId, end: end };
        wrapperEl.classList.add("gd-reattaching");
        document.addEventListener("mousemove", onReattachMove);
        document.addEventListener("mouseup", onReattachEnd);
    }

    function onReattachMove(e) {
        if (!state.reattaching) return;
        highlightDropTarget(e);
    }

    function highlightDropTarget(e) {
        document.querySelectorAll(".gd-drop-target").forEach(function (el) {
            el.classList.remove("gd-drop-target");
        });
        const under = document.elementFromPoint(e.clientX, e.clientY);
        const nodeEl = under && under.closest ? under.closest(".gd-node") : null;
        if (nodeEl) nodeEl.classList.add("gd-drop-target");
    }

    function onReattachEnd(e) {
        const info = state.reattaching;
        state.reattaching = null;
        wrapperEl.classList.remove("gd-reattaching");
        document.removeEventListener("mousemove", onReattachMove);
        document.removeEventListener("mouseup", onReattachEnd);
        document.querySelectorAll(".gd-drop-target").forEach(function (el) {
            el.classList.remove("gd-drop-target");
        });

        if (!info) return;

        const under = document.elementFromPoint(e.clientX, e.clientY);
        const nodeEl = under && under.closest ? under.closest(".gd-node") : null;
        const edge = findEdge(info.edgeId);

        // An invalid drop redraws unchanged, so the connector snaps back rather than
        // being left following the cursor.
        if (!nodeEl || !edge) {
            renderAllEdges();
            return;
        }

        const nodeId = nodeEl.dataset.nodeId;

        if (info.end === "target") {
            const target = findNode(nodeId);
            if (target && target.type === "start") {
                flash("Nothing can connect into the Start node.");
            } else if (isOutcome(findNode(edge.source)) && isOutcome(target)) {
                flash(OUTCOME_CHAIN_REFUSAL);
            } else if (nodeId !== edge.source) {
                edge.target = nodeId;
                markDirty();
            }
        } else if (nodeId !== edge.target) {
            const moving = findNode(nodeId);
            if (isOutcome(moving) && isOutcome(findNode(edge.target))) {
                flash(OUTCOME_CHAIN_REFUSAL);
                renderAllNodes();
                renderAllEdges();
                return;
            }
            const ports = portsOf(moving || {});
            const free = ports.find(function (spec) {
                return !state.edges.some(function (other) {
                    return other !== edge && other.source === nodeId &&
                        (other.source_port || "default") === spec.port;
                });
            });
            if (free) {
                edge.source = nodeId;
                edge.source_port = free.port;
                markDirty();
            } else {
                flash("That node has no free outcome to move this connector to.");
            }
        }

        renderAllNodes();
        renderAllEdges();
    }

    // -----------------------------------------------------------------
    // Adding, deleting, selecting
    // -----------------------------------------------------------------

    function renderPalette() {
        paletteBodyEl.innerHTML = "";

        state.vocabulary.node_types.forEach(function (entry) {
            // Only one Start node is allowed, so it is not offered once one exists.
            if (entry.type === "start") return;

            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "btn btn-outline-secondary btn-sm gd-palette-btn";
            btn.dataset.nodeType = entry.type;
            btn.innerHTML =
                '<i class="las ' + GC.escapeAttr(ICONS[entry.type] || "la-question-circle") + '"></i> ' +
                GC.escapeHtml(entry.label);
            btn.addEventListener("click", function () {
                addNode(entry.type);
            });
            paletteBodyEl.appendChild(btn);
        });
    }

    function addNode(type) {
        // Laid out on a grid rather than all at one point, so adding five nodes does not
        // produce one node with four hidden underneath it.
        const count = state.nodes.length;
        const node = {
            id: genId("n"),
            type: type,
            position: { x: 60 + (count % 5) * 230, y: 60 + Math.floor(count / 5) * 150 },
            data: defaultData(type),
        };
        state.nodes.push(node);
        markDirty();
        renderAllNodes();
        renderAllEdges();
        openProperties(node.id);
    }

    function deleteNode(nodeId) {
        state.nodes = state.nodes.filter(function (n) { return n.id !== nodeId; });
        // Its connectors go with it: an edge naming a node that is not there is refused
        // by the save, so leaving them would make the graph unsavable.
        state.edges = state.edges.filter(function (e) {
            return e.source !== nodeId && e.target !== nodeId;
        });
        delete state.picked[nodeId];
        if (state.selectedNodeId === nodeId) state.selectedNodeId = null;
        markDirty();
        updatePickedCount();
        renderAllNodes();
        renderAllEdges();
    }

    function deleteEdge(edgeId) {
        state.edges = state.edges.filter(function (e) { return e.id !== edgeId; });
        if (state.selectedEdgeId === edgeId) state.selectedEdgeId = null;
        markDirty();
        renderAllEdges();
    }

    function selectNode(nodeId) {
        state.selectedNodeId = nodeId;
        renderAllNodes();
        renderAllEdges();
    }

    function togglePicked(nodeId) {
        if (state.picked[nodeId]) delete state.picked[nodeId];
        else state.picked[nodeId] = true;
        updatePickedCount();
        renderAllNodes();
        renderAllEdges();
    }

    function updatePickedCount() {
        const ids = Object.keys(state.picked);
        const count = document.getElementById("gdPickedCount");
        if (count) count.textContent = String(ids.length);
        const btn = document.getElementById("gdTestBtn");
        if (btn) btn.disabled = ids.length === 0;
    }

    // -----------------------------------------------------------------
    // Properties
    // -----------------------------------------------------------------

    function openProperties(nodeId) {
        const node = findNode(nodeId);
        if (!node) return;

        state.selectedNodeId = nodeId;
        renderAllNodes();
        renderAllEdges();

        // A deep clone, so Cancel is free and closing the panel without saving cannot
        // half-apply an edit. Same approach as flow_builder's properties panel.
        const draft = JSON.parse(JSON.stringify(node.data || {}));

        propertiesBodyEl.innerHTML = "";
        propertiesBodyEl.appendChild(propertiesForm(node, draft));

        const panel = bootstrap.Offcanvas.getOrCreateInstance(
            document.getElementById("gdProperties"),
        );
        panel.show();
    }

    function propertiesForm(node, draft) {
        const form = document.createElement("div");

        form.appendChild(textField("Label", draft.label || "", function (v) {
            draft.label = v;
        }));

        switch (node.type) {
            case "sql": sqlFields(form, draft, node); break;
            case "sql_union": sqlUnionFields(form, draft, node); break;
            case "value": valueFields(form, draft); break;
            case "tool_config": toolConfigFields(form, draft); break;
            case "human": humanFields(form, draft); break;
            case "branch": branchFields(form, draft, node); break;
            case "for_each": forEachFields(form, draft, node); break;
            case "do_until": doUntilFields(form, draft, node); break;
            case "email": emailFields(form, draft, node); break;
            case "timer": timerFields(form, draft, node); break;
            case "wait": waitFields(form, draft); break;
            case "success":
            case "failure":
                form.appendChild(textareaField("Message", draft.message || "", 2, function (v) {
                    draft.message = v;
                }));
                break;
            default:
                break;
        }

        // After the per-type fields, because a variable is written *into* one of them and
        // the panel should read in that order. Draws nothing for a type the server says
        // substitutes nothing, so the Email node — whose variables come from its template
        // — does not get two competing Variables sections.
        variablesFields(form, draft, node);

        const actions = document.createElement("div");
        actions.className = "d-flex gap-2 mt-3";

        const save = document.createElement("button");
        save.type = "button";
        save.className = "btn btn-primary btn-sm";
        save.innerHTML = '<i class="las la-check"></i> Apply';
        save.addEventListener("click", function () {
            // Checked here as well as on the server, which is the house rule: the server
            // is the authority and refuses the same things, but being told at the keyboard
            // beats being told after a round trip that closed the panel.
            const wrong = variablesProblem(draft, node);

            if (wrong) {
                flash(wrong);
                return;
            }

            node.data = draft;
            markDirty();
            renderAllNodes();
            renderAllEdges();
            bootstrap.Offcanvas.getInstance(document.getElementById("gdProperties")).hide();
        });
        actions.appendChild(save);

        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "btn btn-outline-secondary btn-sm";
        cancel.textContent = "Cancel";
        cancel.addEventListener("click", function () {
            bootstrap.Offcanvas.getInstance(document.getElementById("gdProperties")).hide();
        });
        actions.appendChild(cancel);

        form.appendChild(actions);
        return form;
    }

    // ---- per-type field groups ----

    function sqlFields(form, draft, node) {
        form.appendChild(selectField(
            "Datasource", draft.datasource_id || "",
            (state.options ? state.options.datasources : []) || [],
            function (v) { draft.datasource_id = v; },
        ));

        form.appendChild(textField(
            "Tables this statement reads (comma separated)",
            (draft.table_names || []).join(", "),
            function (v) {
                draft.table_names = v.split(",").map(function (s) { return s.trim(); })
                    .filter(Boolean);
            },
            "Nothing reads these out of the SQL, so this list is what lets a table " +
            "switched off in Data Sources stop this node.",
        ));

        form.appendChild(textareaField("SQL statement", draft.sql_query || "", 8, function (v) {
            draft.sql_query = v;
        }, "One read-only statement. Use :name for a value supplied at run time."));

        paramsField(form, draft, node);
    }

    /**
     * A union node's settings: a SQL node's, and a paragraph saying what happens to them.
     *
     * `sqlFields` verbatim rather than a copy, because the two hold the same thing — one
     * statement, its datasource, its tables, its parameters. Two forms drifting apart is
     * how a node ends up unable to express something its runner supports.
     *
     * The paragraph is the whole difference and is not guessable from any field on screen:
     * this statement does not run when the node is reached, and each pass's `:name` is
     * renamed so one pass's value cannot land on another pass's copy.
     */
    function sqlUnionFields(form, draft, node) {
        sqlFields(form, draft, node);

        const note = document.createElement("div");
        note.className = "alert alert-secondary small mt-2 mb-0";
        note.innerHTML =
            "<strong>This statement is not run pass by pass.</strong> One copy of it is " +
            "added for every pass of the loop this node sits in, joined with UNION, and " +
            "the whole thing runs as a single query on the last pass — leaving by the " +
            "<code>execute</code> output instead of going round again. Each copy's " +
            "<code>:name</code> is bound separately, so values are never written into the " +
            "text. Put no ORDER BY or LIMIT here: unparenthesised, either one would apply " +
            "to every pass at once, so sort or cut the result in a node after " +
            "<code>execute</code>.";
        form.appendChild(note);
    }

    /**
     * The `:name` values a statement asks for, and where each one comes from.
     *
     * Two halves of one row, because they are one decision: `params` declares the
     * parameter (its name, what it holds, whether it is required) and `bindings` says
     * which node fills it. Editing them in separate lists would let a wiring outlive the
     * parameter it fills, which the save then refuses.
     *
     * @param {HTMLElement} form
     * @param {object} draft
     * @param {object} node - needed for `otherNodes`, so a wiring can name an upstream
     */
    function paramsField(form, draft, node) {
        const wrap = document.createElement("div");
        wrap.className = "mb-3";

        const label = document.createElement("label");
        label.className = "form-label fw-semibold small";
        label.textContent = "Parameters — the :name values this statement needs";
        wrap.appendChild(label);

        const rows = document.createElement("div");
        wrap.appendChild(rows);

        // One wiring per row, held here rather than edited in place inside
        // `draft.bindings`, which `rebuild` then writes from scratch. See `rebuild`.
        const wiring = (draft.params || []).map(function (param) {
            return normalisedBinding((draft.bindings || {})[param.param]);
        });

        /**
         * Write `draft.bindings` out of the rows, replacing whatever was there.
         *
         * `bindings` is keyed by parameter *name*, and the name is a text box the user
         * edits — so any attempt to keep the key in step with it as it changes has to be
         * right for every intermediate state, and it was not. Clearing the box to retype
         * moved the wiring to the key `""`, and typing the name back created a second
         * entry beside it: an orphan the panel could not show and the Remove button could
         * not reach, which the save then refused with "wires a value into ':'" — accurate,
         * and impossible to act on.
         *
         * So the rows are the truth and this is derived. A row with no name or no source
         * contributes nothing, a renamed row simply comes out under its new name, and a
         * removed row is gone — no key can outlive what it was named after. Called once
         * when the panel opens too, so a document whose keys had already drifted is healed
         * by pressing Apply.
         */
        function rebuild() {
            const next = {};

            (draft.params || []).forEach(function (param, index) {
                const name = String(param.param || "").trim();
                const bound = wiring[index] || {};

                if (!name || !bound.node) return;

                next[name] = {
                    node: bound.node,
                    field: String(bound.field || "").trim(),
                    mode: bound.mode || "one",
                };
            });

            draft.bindings = next;
        }

        function draw() {
            rows.innerHTML = "";
            (draft.params || []).forEach(function (param, index) {
                rows.appendChild(paramRow(param, wiring[index], node, rebuild, function () {
                    draft.params.splice(index, 1);
                    wiring.splice(index, 1);
                    rebuild();
                    draw();
                }));
            });
        }

        const add = document.createElement("button");
        add.type = "button";
        add.className = "btn btn-outline-secondary btn-sm mt-1";
        add.innerHTML = '<i class="las la-plus"></i> Add parameter';
        add.addEventListener("click", function () {
            draft.params = draft.params || [];
            draft.params.push({ param: "", type: "text", required: true });
            wiring.push(normalisedBinding(null));
            draw();
        });

        rebuild();
        draw();
        wrap.appendChild(add);
        attachHelp(
            wrap,
            "A parameter named after the item of the loop this node sits inside is filled " +
            "with that item automatically. Values are bound, never written into the " +
            "statement — so a list is 'IN :name', not 'IN (:name)'.",
        );
        form.appendChild(wrap);
    }

    /**
     * A binding in the shape the row edits, whatever shape it was stored in.
     *
     * A bare node id is what graphs were saved with before a binding could carry a field
     * or a mode; it is read here and written back in the object shape, so the rest of the
     * form never has to guess which of the two it is holding.
     *
     * @param {object|string|null} raw
     * @returns {{node: string, field: string, mode: string}}
     */
    function normalisedBinding(raw) {
        if (typeof raw === "string") return { node: raw, field: "", mode: "one" };

        const bound = raw || {};
        return {
            node: bound.node || "",
            field: bound.field || "",
            mode: bound.mode || "one",
        };
    }

    function paramRow(param, bound, node, changed, onRemove) {
        const row = document.createElement("div");
        row.className = "border rounded p-2 mb-2";

        row.appendChild(textField("Name (written as :name)", param.param || "", function (v) {
            param.param = v;
            changed();
        }));

        row.appendChild(selectField(
            "Holds", param.type || "text",
            (state.vocabulary.param_types || []).map(function (t) {
                return { uuid: t.value, label: t.label };
            }),
            function (v) { param.type = v; },
            "What the value is converted to before it is bound. A strict driver refuses " +
            "a string where a number belongs.",
        ));

        const requiredWrap = document.createElement("div");
        requiredWrap.className = "form-check mb-2";

        const required = document.createElement("input");
        required.type = "checkbox";
        required.className = "form-check-input";
        // A fresh id per render: hashing the name would collide across new rows, whose
        // names are all still empty, and two checkboxes sharing an id share their label.
        required.id = genId("gd-param-required");
        required.checked = param.required !== false;
        required.addEventListener("change", function () { param.required = required.checked; });

        const requiredLabel = document.createElement("label");
        requiredLabel.className = "form-check-label small";
        requiredLabel.setAttribute("for", required.id);
        requiredLabel.textContent = "Required";

        requiredWrap.appendChild(required);
        requiredWrap.appendChild(requiredLabel);
        row.appendChild(requiredWrap);

        row.appendChild(selectField(
            "Value comes from", bound.node || "", otherNodes(node),
            function (v) { bound.node = v; changed(); },
            "Leave blank for a value supplied when the graph is run, or filled by the " +
            "loop this node is inside.",
        ));

        row.appendChild(textField("Field (optional)", bound.field || "", function (v) {
            bound.field = v;
            changed();
        }, "One column of that node's result. Wired to a loop, this is a column of the item."));

        row.appendChild(selectField(
            "Takes", bound.mode || "one",
            (state.vocabulary.binding_modes || []).map(function (m) {
                return { uuid: m.value, label: m.label };
            }),
            function (v) { bound.mode = v; changed(); },
        ));

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn-outline-danger btn-sm";
        remove.innerHTML = '<i class="las la-trash"></i> Remove';
        remove.addEventListener("click", onRemove);
        row.appendChild(remove);

        return row;
    }

    function valueFields(form, draft) {
        form.appendChild(selectField(
            "Holds", draft.value_kind || "list",
            state.vocabulary.value_kinds.map(function (k) {
                return { uuid: k.value, label: k.label };
            }),
            function (v) { draft.value_kind = v; },
        ));

        form.appendChild(textareaField("Value (JSON)", draft.value_json || "", 6, function (v) {
            draft.value_json = v;
        }, 'A list is [1, 2, 3]. A dictionary is {"key": "value"}.'));
    }

    function toolConfigFields(form, draft) {
        form.appendChild(selectField(
            "Tool config", draft.tool_config_id || "",
            (state.options ? state.options.tool_configs : []) || [],
            function (v) { draft.tool_config_id = v; },
            "Runs exactly as an agent calling it would, including any tools it embeds.",
        ));
    }

    /**
     * An Email node: which template, which server, who it goes to, and where each of the
     * template's declared variables gets its value.
     *
     * The binding rows are rebuilt whenever the template changes, from the `variables` that
     * ride along on each entry of `state.options.email_templates` — which is why
     * `node_options` sends them rather than making this fetch them separately. Fetching
     * would let somebody Apply the node before its rows had loaded.
     *
     * A graph offers two sources and no more: an earlier node's output, or a fixed value.
     * There is no chat session and no record here, and a graph is not attached to a chatbot
     * so there is no agent whose prompt variables it could read. `_validate_email_node`
     * refuses anything else on save, so offering more would build a form the server
     * rejects.
     */
    function emailFields(form, draft, node) {
        const templates = (state.options ? state.options.email_templates : []) || [];
        const servers = (state.options ? state.options.smtp_configs : []) || [];

        form.appendChild(selectField(
            "Template", draft.template_id || "", templates,
            function (v) { draft.template_id = v; renderBindings(); },
            "What the email says. Editing the template later does not change emails already sent.",
        ));

        form.appendChild(selectField(
            "Send through", draft.smtp_config_id || "", servers,
            function (v) { draft.smtp_config_id = v; },
        ));

        draft.recipients = draft.recipients || { to: [], cc: [], bcc: [] };

        ["to", "cc", "bcc"].forEach(function (key) {
            const label = key === "to" ? "To" : key === "cc" ? "Cc" : "Bcc";
            form.appendChild(textField(
                label,
                (draft.recipients[key] || []).join(", "),
                function (v) {
                    draft.recipients[key] = v
                        .split(",")
                        .map(function (part) { return part.trim(); })
                        .filter(function (part) { return part !== ""; });
                },
                key === "to"
                    ? "Comma-separated. Each entry may be a fixed address or a {{VARIABLE}}."
                    : "",
            ));
        });

        // The container the binding rows are drawn into, so changing the template can
        // replace them without rebuilding the whole panel and losing the other fields.
        const bindingsWrap = document.createElement("div");
        bindingsWrap.className = "mt-3";
        form.appendChild(bindingsWrap);

        function renderBindings() {
            bindingsWrap.textContent = "";
            draft.variable_bindings = draft.variable_bindings || {};

            const chosen = templates.find(function (t) { return t.uuid === draft.template_id; });
            const declared = (chosen && chosen.variables) || [];

            const heading = document.createElement("div");
            heading.className = "form-label fw-semibold mb-1";
            heading.textContent = "Variables";
            bindingsWrap.appendChild(heading);

            if (!draft.template_id) {
                const hint = document.createElement("div");
                hint.className = "form-text";
                hint.textContent = "Choose a template to see what it needs.";
                bindingsWrap.appendChild(hint);
                return;
            }
            if (!declared.length) {
                const hint = document.createElement("div");
                hint.className = "form-text";
                hint.textContent = "This template declares no variables.";
                bindingsWrap.appendChild(hint);
                return;
            }

            // Every node that could have produced something, as binding targets.
            //
            // `otherNodes` rather than a filter written out here: there is no `state.graph`
            // — the nodes live at `state.nodes` — so the hand-written version threw a
            // TypeError the moment a template declaring a variable was chosen. It went
            // unnoticed only because the Template picker was itself always empty (the
            // response schema was dropping the list), so this line was unreachable.
            //
            // The shared helper also excludes the Start node, which is right: binding to
            // its `{"started": true}` is never useful.
            const upstream = otherNodes(node);

            declared.forEach(function (variable) {
                const current = draft.variable_bindings[variable.name] || {};

                const row = document.createElement("div");
                row.className = "border rounded p-2 mb-2";

                const name = document.createElement("div");
                name.className = "small fw-semibold";
                // textContent: the name came from a saved template and must not be markup.
                name.textContent = "{{" + variable.name + "}}"
                    + (variable.required ? " (required)" : "");
                row.appendChild(name);

                row.appendChild(selectField(
                    "From", current.source || "",
                    [
                        { uuid: "node", label: "An earlier node's output" },
                        { uuid: "literal", label: "A fixed value" },
                    ],
                    function (v) {
                        draft.variable_bindings[variable.name] =
                            Object.assign({}, draft.variable_bindings[variable.name], { source: v });
                        renderBindings();
                    },
                ));

                if (current.source === "node") {
                    row.appendChild(selectField(
                        "Node", current.node_id || "", upstream,
                        function (v) {
                            draft.variable_bindings[variable.name].node_id = v;
                        },
                    ));
                    row.appendChild(textField(
                        "Field", current.path || "",
                        function (v) { draft.variable_bindings[variable.name].path = v; },
                        "Optional. A path into that node's output, such as rows[0].email.",
                    ));
                } else if (current.source === "literal") {
                    row.appendChild(textField(
                        "Value", current.value || "",
                        function (v) { draft.variable_bindings[variable.name].value = v; },
                    ));
                }

                bindingsWrap.appendChild(row);
            });
        }

        renderBindings();
    }

    function humanFields(form, draft) {
        form.appendChild(textareaField("Question", draft.prompt || "", 3, function (v) {
            draft.prompt = v;
        }, "Shown in the log below the canvas while the run waits for an answer."));

        form.appendChild(selectField(
            "Answer", draft.expects || "confirm",
            [
                { uuid: "confirm", label: "Yes or no" },
                { uuid: "choice", label: "One of a list" },
                { uuid: "text", label: "Free text" },
            ],
            function (v) { draft.expects = v; },
        ));

        form.appendChild(textField(
            "Choices (comma separated)", (draft.choices || []).join(", "),
            function (v) {
                draft.choices = v.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
            },
            "Only used when the answer is one of a list.",
        ));
    }

    function branchFields(form, draft, node) {
        const wrap = document.createElement("div");
        wrap.className = "mb-2";

        const label = document.createElement("label");
        label.className = "form-label fw-semibold small";
        label.textContent = "Conditions — the first that matches wins; anything else takes 'else'";
        wrap.appendChild(label);

        const rows = document.createElement("div");
        wrap.appendChild(rows);

        function draw() {
            rows.innerHTML = "";
            (draft.conditions || []).forEach(function (condition, index) {
                rows.appendChild(conditionRow(condition, node, function () {
                    draft.conditions.splice(index, 1);
                    draw();
                }, true));
            });
        }

        const add = document.createElement("button");
        add.type = "button";
        add.className = "btn btn-outline-secondary btn-sm mt-1";
        add.innerHTML = '<i class="las la-plus"></i> Add condition';
        add.addEventListener("click", function () {
            draft.conditions = draft.conditions || [];
            draft.conditions.push({
                source_node: "", field: "", operator: "not_empty", value: "",
                port: "outcome" + (draft.conditions.length + 1),
                label: "",
            });
            draw();
        });

        draw();
        wrap.appendChild(add);
        form.appendChild(wrap);
    }

    function conditionRow(condition, node, onRemove, withPort) {
        const row = document.createElement("div");
        row.className = "border rounded p-2 mb-2";

        row.appendChild(selectField(
            "Read from node", condition.source_node || "", otherNodes(node),
            function (v) { condition.source_node = v; },
        ));
        row.appendChild(textField("Field (optional)", condition.field || "", function (v) {
            condition.field = v;
        }, "A column name, when the node returns rows."));
        row.appendChild(selectField(
            "Comparison", condition.operator || "not_empty",
            state.vocabulary.operators.map(function (o) {
                return { uuid: o.value, label: o.label };
            }),
            function (v) { condition.operator = v; },
        ));
        row.appendChild(textField("Value", condition.value == null ? "" : String(condition.value),
            function (v) { condition.value = v; }));

        if (withPort) {
            row.appendChild(textField("Outcome name", condition.port || "", function (v) {
                condition.port = v;
            }, "Becomes an output dot on the node. 'else' is reserved."));
        }

        if (onRemove) {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "btn btn-outline-danger btn-sm";
            remove.innerHTML = '<i class="las la-trash"></i> Remove';
            remove.addEventListener("click", onRemove);
            row.appendChild(remove);
        }

        return row;
    }

    function forEachFields(form, draft, node) {
        form.appendChild(selectField(
            "Loop over the result of", draft.source_node || "", otherNodes(node),
            function (v) { draft.source_node = v; },
        ));
        form.appendChild(textField("Call each one", draft.item_name || "item", function (v) {
            draft.item_name = v;
        }));
        form.appendChild(numberField(
            "Most passes allowed",
            draft.max_iterations == null ? state.vocabulary.default_max_iterations : draft.max_iterations,
            function (v) { draft.max_iterations = v; },
            "Reaching this stops the run and says so, rather than running part of the list.",
        ));

        form.appendChild(selectField(
            "Collect the result of (optional)", draft.collect_from || "",
            bodyNodes(node),
            function (v) { draft.collect_from = v; },
            "Every pass's rows, put together, become this loop's own result — so the node " +
            "after 'done' reads all of them. Only a node inside the body can be collected: " +
            "one outside it runs once, so every pass would collect the same rows again.",
        ));

        form.appendChild(textField(
            "Record the item as (optional)", draft.label_item_as || "",
            function (v) { draft.label_item_as = v; },
            "A column added to every collected row, holding the item that produced it. " +
            "Leave blank if the statement already returns it — asking for it twice is " +
            "refused rather than one value quietly replacing the other.",
        ));
    }

    /**
     * The nodes a loop may collect: the ones inside its own body.
     *
     * Mirrors the server's rule (`_require_collected_nodes_in_body`) rather than offering
     * every node and letting the save refuse: a picker whose options are mostly invalid
     * teaches nothing about why. Reachable from the `body` port *and* able to reach the
     * loop again — a node hanging off the body that never comes back is where a pass
     * stops, not part of it.
     */
    function bodyNodes(node) {
        const forward = {};
        const backward = {};

        state.edges.forEach(function (e) {
            (forward[e.source] = forward[e.source] || []).push(e.target);
            (backward[e.target] = backward[e.target] || []).push(e.source);
        });

        const bodyStart = state.edges
            .filter(function (e) { return e.source === node.id && e.source_port === "body"; })
            .map(function (e) { return e.target; });

        const inBody = reachable(forward, bodyStart);
        const comesBack = reachable(backward, [node.id]);

        // The loop itself satisfies both tests by construction — its body leads back to it
        // — so it is taken out by hand, exactly as the server's rule does. Collecting its
        // own output would append its item envelope to the union it is building.
        return state.nodes
            .filter(function (n) {
                return n.id !== node.id && inBody[n.id] && comesBack[n.id];
            })
            .map(function (n) { return { uuid: n.id, label: labelOf(n) }; });
    }

    function reachable(graph, roots) {
        const seen = {};
        const stack = roots.slice();

        while (stack.length) {
            const id = stack.pop();
            if (seen[id]) continue;
            seen[id] = true;
            (graph[id] || []).forEach(function (next) { stack.push(next); });
        }

        return seen;
    }

    function doUntilFields(form, draft, node) {
        draft.condition = draft.condition || {};
        const label = document.createElement("div");
        label.className = "form-label fw-semibold small";
        label.textContent = "Repeat until this is true";
        form.appendChild(label);
        form.appendChild(conditionRow(draft.condition, node, null, false));
        form.appendChild(numberField(
            "Most passes allowed",
            draft.max_iterations == null ? state.vocabulary.default_max_iterations : draft.max_iterations,
            function (v) { draft.max_iterations = v; },
            "A loop whose condition never becomes true stops here and says so.",
        ));
    }

    function otherNodes(node) {
        return state.nodes
            .filter(function (n) { return n.id !== node.id && n.type !== "start"; })
            .map(function (n) { return { uuid: n.id, label: labelOf(n) }; });
    }

    /**
     * Every Timer node set to Start, as the things a Pause/Resume/Stop may point at.
     *
     * Mirrors the server's rule rather than offering every node and letting the save
     * refuse most of them — the same call `bodyNodes` makes for a loop's collection. A
     * picker whose options are mostly invalid teaches nothing about why.
     */
    function startTimerNodes(node) {
        return state.nodes
            .filter(function (n) {
                return n.type === "timer"
                    && ((n.data || {}).action || "start") === "start"
                    && n.id !== node.id;
            })
            .map(function (n) { return { uuid: n.id, label: labelOf(n) }; });
    }

    /**
     * A Timer node: which of the four things it does, and to which timer.
     *
     * The Timer picker appears only for the three that act on somebody else's timer, so
     * the form is re-rendered when the action changes. A Start *is* the timer and naming
     * one on it is refused by the server, which is why the field is removed rather than
     * disabled.
     */
    function timerFields(form, draft, node) {
        const wrap = document.createElement("div");
        form.appendChild(wrap);

        function render() {
            wrap.textContent = "";

            wrap.appendChild(selectField(
                "What this node does", draft.action || "start",
                (state.vocabulary.timer_actions || []).map(function (a) {
                    return { uuid: a.value, label: a.label };
                }),
                function (v) {
                    draft.action = v;
                    // A Start names no timer. Cleared rather than left behind, or the
                    // stale id would be posted and refused.
                    if (v === "start") draft.timer_node = "";
                    render();
                },
                "Start begins timing. Pause and Resume bracket a stretch that should not "
                + "count. Stop ends it and works out how long it took.",
            ));

            if ((draft.action || "start") === "start") {
                attachHelp(wrap, "Later nodes read this timer by pointing at this box.");
                return;
            }

            wrap.appendChild(selectField(
                "Timer", draft.timer_node || "", startTimerNodes(node),
                function (v) { draft.timer_node = v; },
                "The Timer node set to Start that begins this measurement.",
            ));
        }

        render();
    }

    /**
     * A Wait node: one number.
     *
     * Not a duration plus a unit. Two fields have to be reconciled into one number
     * server-side, and a stored graph then has two sources of truth for the same fact.
     */
    function waitFields(form, draft) {
        const ceiling = state.vocabulary.max_wait_seconds || 900;

        form.appendChild(numberField(
            "Seconds to wait", draft.seconds == null ? "" : draft.seconds,
            function (v) { draft.seconds = v; },
            "At most " + ceiling + " (" + Math.round(ceiling / 60) + " minutes). "
            + "A waiting run does not survive a restart — for anything longer, use an "
            + "Integrations schedule.",
        ));
    }

    /**
     * The Variables section, on every node type whose fields can use one.
     *
     * Which fields those are comes from the server, not from a list here: the server's
     * table is what the validator enforces, and a panel that offered a variable in a
     * field the validator ignores would substitute nothing and say nothing about it.
     */
    function variablesFields(form, draft, node) {
        const fields = (state.vocabulary.variable_fields || {})[node.type] || [];

        if (!fields.length) return;

        const section = document.createElement("div");
        section.className = "mt-3 pt-3 border-top";
        form.appendChild(section);

        const heading = document.createElement("div");
        heading.className = "form-label fw-semibold mb-1";
        heading.textContent = "Variables";
        section.appendChild(heading);

        const hint = document.createElement("div");
        hint.className = "form-text small mb-2";
        hint.textContent = "Write {{NAME}} in: " + fields.join(", ") + ".";
        section.appendChild(hint);

        const rows = document.createElement("div");
        section.appendChild(rows);

        function render() {
            rows.textContent = "";
            draft.variables = draft.variables || {};

            Object.keys(draft.variables).forEach(function (name) {
                rows.appendChild(variableRow(name, draft, node, render));
            });

            if (!Object.keys(draft.variables).length) {
                const empty = document.createElement("div");
                empty.className = "form-text small";
                empty.textContent = "None yet.";
                rows.appendChild(empty);
            }
        }

        const add = document.createElement("button");
        add.type = "button";
        add.className = "btn btn-outline-secondary btn-sm mt-1";
        add.innerHTML = '<i class="las la-plus"></i> Add variable';
        add.addEventListener("click", function () {
            draft.variables = draft.variables || {};

            const cap = state.vocabulary.max_node_variables || 30;

            if (Object.keys(draft.variables).length >= cap) {
                flash("A node may declare at most " + cap + " variables.");
                return;
            }

            draft.variables[nextVariableName(draft.variables)] = { source: "node", node_id: "" };
            render();
        });
        section.appendChild(add);

        render();
    }

    /**
     * What is wrong with this node's variables, in the server's own words, or ``""``.
     *
     * A deliberate echo of ``node_variables.assert_valid`` rather than a second opinion.
     * The server stays the authority — this only saves a round trip that would close the
     * panel and lose the edit.
     */
    function variablesProblem(draft, node) {
        const fields = (state.vocabulary.variable_fields || {})[node.type] || [];
        const declared = draft.variables || {};
        const names = Object.keys(declared);

        if (!fields.length) return names.length ? "This node cannot use variables." : "";

        const cap = state.vocabulary.max_node_variables || 30;

        if (names.length > cap) {
            return "A node may declare at most " + cap + " variables.";
        }

        for (let i = 0; i < names.length; i += 1) {
            const name = names[i];

            if (!/^[A-Z][A-Z0-9_]{0,49}$/.test(name)) {
                return "'" + name + "' is not a usable variable name. Start with a letter "
                    + "and use capitals, digits and underscores only.";
            }

            const binding = declared[name] || {};

            if ((binding.source || "node") === "node" && !binding.node_id) {
                return "{{" + name + "}} reads an earlier node's output but no node is chosen.";
            }
        }

        const missing = usedVariableNames(draft, node).filter(function (name) {
            return !Object.prototype.hasOwnProperty.call(declared, name);
        });

        if (missing.length) {
            return "This node uses {{" + missing.join("}}, {{") + "}} but nothing declares "
                + "it. Add it under Variables, or remove it from the text.";
        }

        return "";
    }

    /** Every ``{{NAME}}`` written into this node's substitutable fields, upper-cased. */
    function usedVariableNames(draft, node) {
        // Keyed by the panel's own labels, which is what the server sends. Each maps to
        // the `data` key it edits — the one place the two vocabularies meet.
        const KEYS = {
            "SQL statement": "sql_query", "Tables": "table_names",
            "Value": "value_json", "Question": "prompt", "Choices": "choices",
            "Message": "message",
        };
        const fields = (state.vocabulary.variable_fields || {})[node.type] || [];
        const found = {};

        fields.forEach(function (label) {
            const raw = draft[KEYS[label]];
            const texts = Array.isArray(raw) ? raw : [raw];

            texts.forEach(function (text) {
                if (typeof text !== "string") return;
                const matches = text.match(/\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g) || [];
                matches.forEach(function (match) {
                    found[match.replace(/[{}\s]/g, "").toUpperCase()] = true;
                });
            });
        });

        return Object.keys(found);
    }

    /** A name nothing else on this node is using — ``NAME``, ``NAME_2``, and so on. */
    function nextVariableName(existing) {
        let index = 1;
        let name = "NAME";

        while (Object.prototype.hasOwnProperty.call(existing, name)) {
            index += 1;
            name = "NAME_" + index;
        }

        return name;
    }

    /**
     * One declared variable: its name, where its value comes from, and what happens when
     * there isn't one.
     *
     * The "If it has no value" select is how the form expresses *the `default` key is
     * absent* as against *`default` is an empty string* — a distinction a bare text input
     * cannot make, and one the server acts on: absent means fail the node and say which
     * variable, present means carry on with this.
     */
    function variableRow(name, draft, node, rerender) {
        const binding = draft.variables[name] || {};

        const row = document.createElement("div");
        row.className = "border rounded p-2 mb-2";

        const head = document.createElement("div");
        head.className = "d-flex align-items-start gap-2";
        row.appendChild(head);

        const nameWrap = document.createElement("div");
        nameWrap.className = "flex-grow-1";
        head.appendChild(nameWrap);

        nameWrap.appendChild(textField("Name", name, function (v) {
            // Upper-cased as it is typed, because the renderer upper-cases whatever it
            // matches — so a lowercase declaration would look right and never be found.
            const renamed = v.toUpperCase().trim();

            if (!renamed || renamed === name) return;
            if (Object.prototype.hasOwnProperty.call(draft.variables, renamed)) return;

            // Rebuilt in order rather than deleted and re-added, so renaming a row does
            // not send it to the bottom of the list while somebody is typing in it.
            const rebuilt = {};
            Object.keys(draft.variables).forEach(function (key) {
                rebuilt[key === name ? renamed : key] = draft.variables[key];
            });
            draft.variables = rebuilt;
            name = renamed;
        }, "Referred to as {{" + name + "}}."));

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn-outline-danger btn-sm mt-4";
        remove.innerHTML = '<i class="las la-trash"></i>';
        remove.title = "Remove this variable";
        remove.addEventListener("click", function () {
            delete draft.variables[name];
            rerender();
        });
        head.appendChild(remove);

        row.appendChild(selectField(
            "From", binding.source || "node",
            [
                { uuid: "node", label: "An earlier node's output" },
                { uuid: "literal", label: "A fixed value" },
            ],
            function (v) {
                draft.variables[name] = Object.assign({}, draft.variables[name], { source: v });
                rerender();
            },
        ));

        if ((binding.source || "node") === "node") {
            row.appendChild(selectField(
                "Node", binding.node_id || "", otherNodes(node),
                function (v) { draft.variables[name].node_id = v; },
            ));
            row.appendChild(textField(
                "Field", binding.path || "",
                function (v) { draft.variables[name].path = v; },
                "Optional. A path into that node's output, such as rows[0].email.",
            ));
        } else {
            row.appendChild(textField(
                "Value", binding.value || "",
                function (v) { draft.variables[name].value = v; },
            ));
        }

        const hasDefault = Object.prototype.hasOwnProperty.call(binding, "default");

        row.appendChild(selectField(
            "If it has no value", hasDefault ? "default" : "fail",
            [
                { uuid: "fail", label: "Stop the run and say which variable" },
                { uuid: "default", label: "Use a default" },
            ],
            function (v) {
                if (v === "default") {
                    draft.variables[name].default = binding.default || "";
                } else {
                    delete draft.variables[name].default;
                }
                rerender();
            },
        ));

        if (hasDefault) {
            row.appendChild(textField(
                "Default", binding.default || "",
                function (v) { draft.variables[name].default = v; },
            ));
        }

        return row;
    }

    // ---- field builders ----

    function fieldWrap(labelText, help) {
        const wrap = document.createElement("div");
        wrap.className = "mb-2";
        const label = document.createElement("label");
        label.className = "form-label fw-semibold small mb-1";
        label.textContent = labelText;
        wrap.appendChild(label);
        if (help) {
            wrap._help = help;
        }
        return wrap;
    }

    function attachHelp(wrap, help) {
        if (!help) return;
        const hint = document.createElement("div");
        hint.className = "form-text small";
        hint.textContent = help;
        wrap.appendChild(hint);
    }

    function textField(labelText, value, onChange, help) {
        const wrap = fieldWrap(labelText);
        const input = document.createElement("input");
        input.type = "text";
        input.className = "form-control form-control-sm";
        input.value = value == null ? "" : String(value);
        input.addEventListener("input", function () { onChange(input.value); });
        wrap.appendChild(input);
        attachHelp(wrap, help);
        return wrap;
    }

    function numberField(labelText, value, onChange, help) {
        const wrap = fieldWrap(labelText);
        const input = document.createElement("input");
        input.type = "number";
        input.min = "1";
        input.className = "form-control form-control-sm";
        input.value = value == null ? "" : String(value);
        input.addEventListener("input", function () {
            const parsed = parseInt(input.value, 10);
            onChange(isNaN(parsed) ? "" : parsed);
        });
        wrap.appendChild(input);
        attachHelp(wrap, help);
        return wrap;
    }

    function textareaField(labelText, value, rows, onChange, help) {
        const wrap = fieldWrap(labelText);
        const input = document.createElement("textarea");
        input.className = "form-control form-control-sm";
        input.rows = rows || 3;
        input.value = value == null ? "" : String(value);
        input.addEventListener("input", function () { onChange(input.value); });
        wrap.appendChild(input);
        attachHelp(wrap, help);
        return wrap;
    }

    function selectField(labelText, value, options, onChange, help) {
        const wrap = fieldWrap(labelText);
        const select = document.createElement("select");
        select.className = "form-select form-select-sm";

        const blank = document.createElement("option");
        blank.value = "";
        blank.textContent = "— choose —";
        select.appendChild(blank);

        (options || []).forEach(function (option) {
            const el = document.createElement("option");
            el.value = option.uuid;
            // Unavailable options are offered and flagged rather than hidden, the same
            // call the server's node_options makes: an operator looking for a tool that
            // is switched off needs to see that it is switched off.
            el.textContent = option.label +
                (option.detail ? " (" + option.detail + ")" : "") +
                (option.disabled_reason ? " — " + option.disabled_reason : "");
            if (option.uuid === value) el.selected = true;
            select.appendChild(el);
        });

        select.addEventListener("change", function () { onChange(select.value); });
        wrap.appendChild(select);
        attachHelp(wrap, help);
        return wrap;
    }

    // -----------------------------------------------------------------
    // Save / load
    // -----------------------------------------------------------------

    function serialize() {
        return {
            nodes: state.nodes.map(function (n) {
                return { id: n.id, type: n.type, position: n.position, data: n.data };
            }),
            edges: state.edges.map(function (e) {
                return {
                    id: e.id, source: e.source,
                    source_port: e.source_port || "default", target: e.target,
                };
            }),
        };
    }

    function loadGraph(data) {
        state.nodes = ((data || {}).nodes || []).map(function (n) {
            return {
                id: n.id, type: n.type,
                position: n.position || { x: 60, y: 60 },
                data: n.data || {},
            };
        });
        state.edges = ((data || {}).edges || []).map(function (e) {
            return {
                id: e.id || genId("e"), source: e.source,
                source_port: e.source_port || "default", target: e.target,
            };
        });
        state.picked = {};
        state.selectedNodeId = null;
        state.selectedEdgeId = null;
        updatePickedCount();
        renderAllNodes();
        renderAllEdges();
    }

    async function save() {
        const target = document.getElementById("gdSaveResult");

        try {
            const response = await request(opts.saveUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(serialize()),
            });
            target.innerHTML = await response.text();

            // The dirty flag is cleared only on a *successful* save, read from the
            // `data-success` marker the partial carries — the same contract
            // flow_builder.js reads, so the shared canvas core needs one convention.
            if (target.querySelector('[data-success="true"]')) clearDirty();
        } catch (err) {
            flash("The graph could not be saved. Check your connection and try again.");
        }
    }

    async function reload() {
        if (state.dirty && !window.confirm("Discard your unsaved changes and reload the saved graph?")) {
            return;
        }
        try {
            const response = await request(opts.graphUrl);
            loadGraph(await response.json());
            clearDirty();
        } catch (err) {
            flash("The saved graph could not be loaded.");
        }
    }

    async function loadOptions() {
        try {
            const response = await request(opts.nodeOptionsUrl);
            state.options = await response.json();
            if (state.options && state.options.error) flash(state.options.error);
        } catch (err) {
            // Not fatal: the canvas still draws and the pickers are simply empty, which
            // is better than a page that will not open.
            //
            // Every list the panel reads has to appear here, or a failed fetch leaves the
            // Email node's fields reading `undefined` and throwing on `.find`.
            state.options = {
                datasources: [], tool_configs: [], data_agents: [],
                email_templates: [], smtp_configs: [],
            };
        }
    }

    // -----------------------------------------------------------------
    // Runs
    // -----------------------------------------------------------------

    async function startRun(scope) {
        if (state.dirty) {
            flash("Save the graph before running it — a run reads the saved version.");
            return;
        }

        const body = scope === "selection"
            ? { scope: "selection", node_ids: Object.keys(state.picked) }
            : { scope: "full" };

        try {
            const response = await request(opts.runsUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            const payload = await response.json();

            if (payload.error) {
                flash(payload.error);
                return;
            }
            watch(payload.run);
        } catch (err) {
            flash("The run could not be started.");
        }
    }

    function watch(runUuid) {
        teardown();
        state.finished = false;
        state.warnedFallback = false;
        setTransport("live");

        const statusUrl = "/graph-designer/runs/" + encodeURIComponent(runUuid);
        const eventsUrl = statusUrl + "/events";

        if (typeof window.EventSource !== "function") {
            // No SSE in this browser at all: poll from the start rather than showing a
            // dock that never moves.
            pollFrom(statusUrl);
            return;
        }

        const source = new EventSource(eventsUrl);
        state.source = source;

        // The server names every event after the run's status, so a browser can switch on
        // `event.type` — and a named event does **not** reach `onmessage`, which fires
        // only for unnamed `message` frames. Listening on `onmessage` alone is a dock
        // that never moves while the run completes perfectly well behind it, which is
        // exactly what happened before these listeners were added. Every name the server
        // can emit is registered here; `_event_name` in the route is the other half of
        // this list.
        FRAME_EVENTS.forEach(function (name) {
            source.addEventListener(name, function (event) {
                applyFrame(safeParse(event.data));
            });
        });

        source.onerror = function () {
            // Every close arrives here, success included, with no data. `finished` is
            // what tells an expected end from a dropped connection — and close() runs
            // before anything else, because the browser reopens a stream that ended.
            source.close();
            state.source = null;

            if (state.finished) return;

            if (!state.warnedFallback) {
                state.warnedFallback = true;
                console.warn("Graph run stream dropped; falling back to polling.");
            }
            setTransport("polling");
            pollFrom(statusUrl);
        };
    }

    function pollFrom(statusUrl) {
        setTransport("polling");
        state.pollTimer = window.setInterval(async function () {
            try {
                const response = await request(statusUrl);
                applyFrame(await response.json());
            } catch (err) {
                // Left running: a single failed poll on a long run is not a reason to
                // stop watching, and `teardown` ends it when the run does.
            }
        }, POLL_MS);
    }

    function applyFrame(frame) {
        if (!frame) return;

        state.run = frame;
        renderDock(frame);
        paintNodeStatus(frame);

        const terminal = ["succeeded", "failed", "cancelled"].indexOf(frame.status) >= 0;
        const stopBtn = document.getElementById("gdStopBtn");
        if (stopBtn) stopBtn.disabled = terminal;

        if (terminal) {
            state.finished = true;
            teardown();
            setTransport("");
        }
    }

    function teardown() {
        if (state.source) {
            state.source.close();
            state.source = null;
        }
        if (state.pollTimer) {
            window.clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
    }

    async function stopRun() {
        if (!state.run) return;
        try {
            const response = await request(
                "/graph-designer/runs/" + encodeURIComponent(state.run.uuid) + "/cancel",
                { method: "POST" },
            );
            applyFrame(await response.json());
        } catch (err) {
            flash("The run could not be stopped.");
        }
    }

    async function answerQuestion(value) {
        if (!state.run) return;
        try {
            const response = await request(
                "/graph-designer/runs/" + encodeURIComponent(state.run.uuid) + "/resume",
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ answer: value }),
                },
            );
            const payload = await response.json();

            if (payload.error) {
                flash(payload.error);
                return;
            }
            // Re-watch: the run is going again and the stream that was open ended when it
            // paused.
            watch(state.run.uuid);
        } catch (err) {
            flash("That answer could not be sent.");
        }
    }

    // -----------------------------------------------------------------
    // The dock
    // -----------------------------------------------------------------

    function renderDock(frame) {
        const status = document.getElementById("gdRunStatus");
        if (status) {
            status.textContent = describeRun(frame);
            status.className = frame.status === "failed" ? "text-danger" : "text-muted";
        }

        renderOutput(frame);
        renderState(frame);
        renderLog(frame);
        renderQuestion(frame);
    }

    function describeRun(frame) {
        const parts = [frame.status];
        if (frame.scope === "selection") parts.push("selection of " + (frame.selected_nodes || []).length);
        if (frame.error_message) parts.push(frame.error_message);
        return parts.join(" · ");
    }

    function renderOutput(frame) {
        const body = document.getElementById("gdOutputRows");
        if (!body) return;

        body.innerHTML = "";
        const steps = frame.steps || [];

        if (!steps.length) {
            const tr = document.createElement("tr");
            const td = document.createElement("td");
            td.colSpan = 7;
            td.className = "text-muted text-center py-3";
            td.textContent = "No steps yet.";
            tr.appendChild(td);
            body.appendChild(tr);
            return;
        }

        steps.forEach(function (step) {
            const tr = document.createElement("tr");
            tr.appendChild(cell(String(step.sequence)));
            tr.appendChild(cell(step.node_label || step.node_id));
            tr.appendChild(cell(step.node_type));
            tr.appendChild(cell(step.iteration ? String(step.iteration + 1) : "1"));

            const statusCell = document.createElement("td");
            const pill = document.createElement("span");
            pill.className = "badge gd-status-pill " + (STATUS_COLOURS[step.status] || "bg-secondary");
            pill.textContent = step.status;
            statusCell.appendChild(pill);
            tr.appendChild(statusCell);

            tr.appendChild(cell(step.duration_ms == null ? "" : step.duration_ms + " ms"));

            const detail = document.createElement("td");
            if (step.message) {
                const line = document.createElement("div");
                line.textContent = step.message;
                detail.appendChild(line);
            }
            if (step.output_preview) {
                detail.appendChild(previewBlock(step.output_preview));
            }
            tr.appendChild(detail);

            body.appendChild(tr);
        });
    }

    /**
     * A step's output, as a collapsed summary that can be expanded.
     *
     * The summary always states the **real** count, not the sample size — the previews
     * are capped server-side and a dock that showed "20 rows" for a result of two
     * thousand would be quietly wrong.
     */
    function previewBlock(preview) {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.className = "small text-muted";
        summary.textContent = describePreview(preview);
        details.appendChild(summary);

        const pre = document.createElement("pre");
        pre.className = "gd-dock-json";
        pre.textContent = JSON.stringify(
            preview.rows || preview.items || preview.entries || preview.value || null,
            null, 2,
        );
        details.appendChild(pre);
        return details;
    }

    function describePreview(preview) {
        const count = preview.count == null ? 0 : preview.count;

        switch (preview.kind) {
            case "rows":
                return count + " row(s)" + (preview.truncated ? " — showing the first few" : "");
            case "list":
                return count + " value(s)" + (preview.truncated ? " — showing the first few" : "");
            case "dict":
                return count + " key(s)";
            case "value":
                return "one value";
            default:
                return "no output";
        }
    }

    function renderState(frame) {
        const pre = document.getElementById("gdStateJson");
        if (!pre) return;

        const steps = frame.steps || [];
        // The latest step that recorded a state snapshot. The newest is the interesting
        // one: "what does the run know now".
        let latest = null;
        for (let i = steps.length - 1; i >= 0; i--) {
            if (steps[i].state_preview) { latest = steps[i]; break; }
        }

        if (!latest) {
            pre.textContent = "No state recorded yet.";
            return;
        }

        pre.textContent = "after " + (latest.node_label || latest.node_id) + "\n\n" +
            JSON.stringify(latest.state_preview, null, 2);
    }

    function renderLog(frame) {
        const box = document.getElementById("gdLogLines");
        if (!box) return;

        box.innerHTML = "";
        const steps = frame.steps || [];

        if (!steps.length) {
            box.textContent = "Nothing logged yet.";
            return;
        }

        steps.forEach(function (step) {
            const line = document.createElement("div");
            line.className = "gd-log-line";
            if (step.status === "failed") line.classList.add("gd-log-line-failed");
            if (step.status === "skipped") line.classList.add("gd-log-line-skipped");
            if (step.status === "awaiting_input") line.classList.add("gd-log-line-awaiting");

            line.textContent = "[" + String(step.sequence).padStart(3, "0") + "] " +
                step.status.toUpperCase() + "  " +
                (step.node_label || step.node_id) +
                (step.iteration ? " (pass " + (step.iteration + 1) + ")" : "") +
                (step.duration_ms == null ? "" : "  " + step.duration_ms + "ms") +
                (step.message ? "  — " + step.message : "");
            box.appendChild(line);
        });

        if (frame.error_message) {
            const line = document.createElement("div");
            line.className = "gd-log-line gd-log-line-failed";
            line.textContent = "RUN  " + frame.error_message;
            box.appendChild(line);
        }
    }

    function renderQuestion(frame) {
        const box = document.getElementById("gd-question");
        if (!box) return;

        const payload = frame.interrupt_payload;

        if (!payload || frame.status !== "awaiting_input") {
            box.hidden = true;
            box.innerHTML = "";
            return;
        }

        box.hidden = false;
        box.innerHTML = "";

        const heading = document.createElement("div");
        heading.className = "fw-semibold small mb-1";
        heading.textContent = (payload.node_label || "This run") + " is waiting for you";
        box.appendChild(heading);

        const question = document.createElement("div");
        question.className = "mb-2";
        question.textContent = payload.prompt || "";
        box.appendChild(question);

        const row = document.createElement("div");
        row.className = "d-flex gap-2 flex-wrap align-items-center";

        if (payload.expects === "confirm") {
            row.appendChild(answerButton("Yes", "btn-success", "yes"));
            row.appendChild(answerButton("No", "btn-outline-danger", "no"));
        } else if (payload.expects === "choice") {
            (payload.choices || []).forEach(function (choice) {
                row.appendChild(answerButton(choice, "btn-outline-primary", choice));
            });
        } else {
            const input = document.createElement("input");
            input.type = "text";
            input.className = "form-control form-control-sm";
            input.style.maxWidth = "320px";
            input.placeholder = "Your answer";
            row.appendChild(input);

            const send = document.createElement("button");
            send.type = "button";
            send.className = "btn btn-sm btn-primary";
            send.textContent = "Send";
            send.addEventListener("click", function () { answerQuestion(input.value); });
            row.appendChild(send);
        }

        box.appendChild(row);
    }

    function answerButton(text, variant, value) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-sm " + variant;
        btn.textContent = text;
        btn.addEventListener("click", function () { answerQuestion(value); });
        return btn;
    }

    /**
     * Repaint each node with the status of its latest pass.
     *
     * The latest, because a node inside a loop has one step row per pass and the canvas
     * draws one box. The dock lists every pass; the box shows where the node stands.
     */
    function paintNodeStatus(frame) {
        const latest = {};
        (frame.steps || []).forEach(function (step) {
            latest[step.node_id] = step;
        });

        state.nodes.forEach(function (node) {
            const el = document.getElementById("node-" + node.id);
            if (!el) return;

            const step = latest[node.id];
            const badge = el.querySelector('[data-role="status-badge"]');

            if (!step) {
                el.removeAttribute("data-status");
                if (badge) badge.hidden = true;
                return;
            }

            const awaiting = frame.status === "awaiting_input" &&
                (frame.interrupt_payload || {}).node_id === node.id;
            const status = awaiting ? "awaiting_input" : step.status;

            el.dataset.status = status;

            if (badge) {
                badge.hidden = false;
                badge.className = "badge gd-node-status-badge " +
                    (STATUS_COLOURS[status] || "bg-secondary");
                badge.textContent = status === "awaiting_input" ? "waiting" : status;
            }
        });
    }

    function setTransport(mode) {
        const el = document.getElementById("gdRunTransport");
        if (!el) return;
        el.textContent = mode === "polling" ? "(polling)" : (mode === "live" ? "(live)" : "");
    }

    // -----------------------------------------------------------------
    // Utilities
    // -----------------------------------------------------------------

    function cell(text) {
        const td = document.createElement("td");
        td.textContent = text == null ? "" : String(text);
        return td;
    }

    function safeParse(raw) {
        try {
            return JSON.parse(raw);
        } catch (err) {
            return null;
        }
    }

    /** Prefer the app's session-aware fetch wrapper when it is present. */
    function request(url, init) {
        if (typeof window.safeFetch === "function") return window.safeFetch(url, init);
        return fetch(url, init);
    }

    /** One transient message in the save banner, for a client-side refusal. */
    function flash(message) {
        const target = document.getElementById("gdSaveResult");
        if (!target) return;

        const alert = document.createElement("div");
        alert.className = "alert alert-warning alert-dismissible fade show mb-0 py-2 small";
        alert.setAttribute("role", "alert");
        alert.textContent = message;

        const close = document.createElement("button");
        close.type = "button";
        close.className = "btn-close";
        close.setAttribute("data-bs-dismiss", "alert");
        alert.appendChild(close);

        target.innerHTML = "";
        target.appendChild(alert);
    }

    function readJsonScript(id, fallback) {
        const el = document.getElementById(id);
        if (!el) return fallback;
        const parsed = safeParse(el.textContent || "");
        return parsed == null ? fallback : parsed;
    }

    // -----------------------------------------------------------------
    // Dock resizing and tabs
    // -----------------------------------------------------------------

    function wireDock() {
        document.querySelectorAll("[data-gd-tab]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                const wanted = btn.dataset.gdTab;
                document.querySelectorAll("[data-gd-tab]").forEach(function (other) {
                    other.classList.toggle("active", other === btn);
                });
                document.querySelectorAll("[data-gd-pane]").forEach(function (pane) {
                    pane.hidden = pane.dataset.gdPane !== wanted;
                });
            });
        });

        const handle = document.getElementById("gd-dock-resize");
        const body = document.getElementById("gd-dock-body");
        if (!handle || !body) return;

        let dragging = null;

        handle.addEventListener("mousedown", function (e) {
            e.preventDefault();
            dragging = { y: e.clientY, height: body.getBoundingClientRect().height };
            document.addEventListener("mousemove", onResize);
            document.addEventListener("mouseup", endResize);
        });

        function onResize(e) {
            if (!dragging) return;
            // Bounded so the dock can neither vanish nor swallow the canvas it describes.
            const next = Math.min(Math.max(dragging.height - (e.clientY - dragging.y), 120), 700);
            body.style.height = next + "px";
        }

        function endResize() {
            dragging = null;
            document.removeEventListener("mousemove", onResize);
            document.removeEventListener("mouseup", endResize);
        }
    }

    // -----------------------------------------------------------------
    // Init
    // -----------------------------------------------------------------

    async function init(userOpts) {
        opts = userOpts || {};

        canvasEl = document.getElementById("gd-canvas");
        edgesGroupEl = document.getElementById("gd-edges-group");
        wrapperEl = document.getElementById("gd-canvas-wrapper");
        paletteBodyEl = document.getElementById("gdPaletteBody");
        propertiesBodyEl = document.getElementById("gdPropertiesBody");

        state.vocabulary = readJsonScript("gdVocabulary", state.vocabulary);

        renderPalette();
        loadGraph(readJsonScript("gdGraphData", { nodes: [], edges: [] }));
        wireDock();

        // Clicking empty canvas cancels a half-drawn connection, which is the only way
        // out of one other than completing it.
        wrapperEl.addEventListener("click", function (e) {
            if (e.target === wrapperEl || e.target === canvasEl) {
                state.pending = null;
                state.selectedNodeId = null;
                state.selectedEdgeId = null;
                renderAllNodes();
                renderAllEdges();
            }
        });

        document.getElementById("gdSaveBtn").addEventListener("click", save);
        document.getElementById("gdReloadBtn").addEventListener("click", reload);
        document.getElementById("gdRunBtn").addEventListener("click", function () {
            startRun("full");
        });
        document.getElementById("gdTestBtn").addEventListener("click", function () {
            startRun("selection");
        });
        document.getElementById("gdStopBtn").addEventListener("click", stopRun);

        // A tab closed mid-run leaves a stream open otherwise.
        window.addEventListener("beforeunload", teardown);

        await loadOptions();
        // Redrawn once the pickers are known, so a node's preview can name the tool or
        // datasource it points at rather than its uuid.
        renderAllNodes();
        renderAllEdges();

        if (opts.openRun) watch(opts.openRun);
    }

    return { init: init };
})();
