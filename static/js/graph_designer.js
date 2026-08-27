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
    //
    // `kind` colours the pill: `error` red, `ok` green. Only a node with exactly two ways
    // out, one of which is a failure, has an `ok` — that is what makes the other one a
    // *success* rather than merely the next step. A branch's conditions, a loop's `each`
    // and `done`, and a union's `next` / `execute` stay grey deliberately: none of them is
    // a claim that the node did or did not do its work.
    const PORTS = {
        start: function () { return [{ port: "default", label: "" }]; },
        sql: function () {
            return [
                { port: "default", label: "", kind: "ok" },
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
                { port: "default", label: "", kind: "ok" },
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
                { port: "default", label: "queued", kind: "ok" },
                { port: "error", label: "not sent", kind: "error" },
            ];
        },
        // Two exits each, for the reason the Email node above has two: a file that could
        // not be written must not leave by the same edge as one that was, or the run carries
        // on as though there were something to hand over. Both refusals are knowable at the
        // moment the node runs — no rows, a path that finds nothing, a file whose window has
        // closed — which is what makes them routable at all.
        create_file: function () {
            return [
                { port: "default", label: "written", kind: "ok" },
                { port: "error", label: "failed", kind: "error" },
            ];
        },
        download_file: function () {
            return [
                { port: "default", label: "ready", kind: "ok" },
                { port: "error", label: "failed", kind: "error" },
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
        create_file: "la-file-export",
        download_file: "la-download",
        timer: "la-stopwatch",
        wait: "la-hourglass-half",
        success: "la-check-circle",
        failure: "la-times-circle",
    };

    // The colour of a node's icon disc, keyed by **what the node does** rather than one
    // entry per type — the same scheme `flow_builder.js` uses and for the same reason: a
    // reader scanning a pipeline is reading its shape, and four kinds of data node that all
    // produce rows should look like four of the same thing.
    //
    // Every one carries a white glyph at 3:1 contrast or better.
    const STEP_COLOURS = {
        entry: "#198754",     // green — the one node a run starts at
        data: "#0b7285",      // deep teal — produces rows
        ask: "#0d6efd",       // blue — stops and waits for a person
        branch: "#6610f2",    // indigo — the run forks here
        loop: "#d97706",      // amber — the run goes round
        send: "#c2410c",      // burnt orange — leaves the pipeline entirely
        file: "#0f766e",      // teal — makes or hands over a file
        clock: "#6c757d",     // grey — measures or spends time, produces nothing
        verdict_ok: "#198754",   // green — says the run succeeded
        verdict_bad: "#b02a37",  // deep red — says it failed
    };

    const COLOURS = {
        start: STEP_COLOURS.entry,
        sql: STEP_COLOURS.data,
        sql_union: STEP_COLOURS.data,
        value: STEP_COLOURS.data,
        tool_config: STEP_COLOURS.data,
        human: STEP_COLOURS.ask,
        branch: STEP_COLOURS.branch,
        for_each: STEP_COLOURS.loop,
        do_until: STEP_COLOURS.loop,
        email: STEP_COLOURS.send,
        create_file: STEP_COLOURS.file,
        download_file: STEP_COLOURS.file,
        timer: STEP_COLOURS.clock,
        wait: STEP_COLOURS.clock,
        success: STEP_COLOURS.verdict_ok,
        failure: STEP_COLOURS.verdict_bad,
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
        // The move selection: which nodes and connectors the next drag carries, and what a
        // box, Ctrl-click, Ctrl+A and Select all add to. Objects used as sets, like
        // `picked`.
        //
        // Three sets on the same nodes, and all three answer different questions.
        // `selectedNodeId` is the one whose settings are open — at most one, ever.
        // `picked` is the set a **test run** will execute, which is why shift-click still
        // means that and Ctrl-click means this. `selection` is the set that **moves
        // together**. Any two of them can be non-empty at once and the stylesheet draws
        // each differently on purpose. Never saved: `serialize()` does not read it.
        selection: { nodes: {}, edges: {} },
        pending: null,       // {nodeId, port} — a connection armed by clicking a port
        connecting: null,    // {nodeId, port, startX, startY, moved} — one being dragged out
        dragging: null,
        reattaching: null,
        dirty: false,
        // "auto" or "manual" — who decides where the nodes sit. Auto arranges the canvas on
        // open and after anything that changes the wiring; dragging a node switches to
        // manual, and Tidy up switches back. Recorded on the saved graph, and a graph with
        // no `layout` key is auto — see `flow_builder.js`, which states the reasoning for
        // both canvases.
        layout: "auto",
        // Connector ids the server reported as running back up the canvas, so a loop is
        // drawn as a return rather than as a step.
        backEdges: {},
        vocabulary: { node_types: [], value_kinds: [], operators: [], default_max_iterations: 200 },
        options: null,       // datasources / tool configs, fetched once
        run: null,           // the latest run frame
        source: null,        // the EventSource
        pollTimer: null,
        finished: false,
        warnedFallback: false,
    };

    let opts = {};
    let canvasEl, edgesSvgEl, edgesGroupEl, wrapperEl, paletteBodyEl, propertiesBodyEl;

    // The shared selection controller — the box, the multi-selection and the group move.
    // Built in `init`, because it needs the canvas elements.
    let selection = null;

    // Resolved once from the stylesheet — see `canvasMetrics`.
    let canvasMetricsCache = null;

    // True for one tick after a drag that actually moved a node, so the click that trails
    // the mouseup is not mistaken for a deliberate click on that node. See `onDragEnd`.
    let suppressNodeClick = false;

    // A press that travels less than this is a click, not a drag. Used by both drags: on a
    // port, so a plain click still arms the click-then-click gesture; on a node, so a click
    // that merely jitters is not treated as a move.
    const DRAG_THRESHOLD_PX = 4;

    // Hand-routing a connector — see "Bending a connector" below.
    //
    // Four bends, and the number is not taste: with a fixed exit stub and a fixed entry
    // stub, four free points already express more distinct orthogonal routes than anybody
    // draws, and it bounds the saved document. The server enforces the same number, because
    // a cap only the browser knows is not a cap.
    const MAX_EDGE_WAYPOINTS = 4;

    // How near a bend a press has to land to move it rather than make a new one.
    const WAYPOINT_GRAB_PX = 8;

    // How near a line-up a dragged bend has to get before it snaps onto it.
    const WAYPOINT_SNAP_PX = 6;

    // Drop a bend this close to where the wire would run without it and it is removed.
    const WAYPOINT_DISCARD_PX = 6;

    // The pending repaint of a drag, and the return lane it was computed against. Both are
    // per-frame: a mousemove only records where the cursor is, and one animation frame does
    // the drawing, however many mousemoves arrived in between. See `scheduleDragFrame`.
    let dragFrame = null;
    let laneXCache = null;

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
            // `data` is where the rows come from: one source on this canvas, an earlier
            // node's output, optionally through a path into it.
            case "create_file":
                return Object.assign(base, {
                    file_format: "csv",
                    file_name: "",
                    data: { source: "node", source_node: "", path: "" },
                });
            case "download_file":
                return Object.assign(base, { create_file_node: "" });
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
            case "create_file": {
                const src = d.data || {};
                const from = src.source_node ? nodeLabelById(src.source_node) : "?";
                return String(d.file_format || "csv").toUpperCase() + " of " + from +
                    (src.path ? "." + src.path : "");
            }
            case "download_file":
                return d.create_file_node
                    ? "link to " + nodeLabelById(d.create_file_node)
                    : "(no file chosen)";
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

    /**
     * Draw one node as a step: a round icon, its name, one line of its settings, and a
     * labelled pill per way out.
     *
     * Built with `createElement` and `textContent` throughout, which is not incidental —
     * every string here is a node name, a table name or a SQL fragment out of the user's
     * own database, and the file's own rule at the top says so.
     */
    function renderNode(node) {
        const el = document.createElement("div");
        el.className = "gd-node gd-step";
        el.id = "node-" + node.id;
        el.dataset.nodeId = node.id;
        el.style.left = ((node.position || {}).x || 0) + "px";
        el.style.top = ((node.position || {}).y || 0) + "px";

        if (node.id === state.selectedNodeId) el.classList.add("gd-node-selected");
        if (state.picked[node.id]) el.classList.add("gd-node-picked");
        // Read from state, never only set on the element at gesture time: `renderAllNodes`
        // rebuilds this layer from scratch, so a class applied by a click would vanish the
        // next time anything re-rendered.
        if (selection && selection.marksNode(node.id)) el.classList.add("gd-node-multi");

        // --- the entry, on the top edge ---
        const inPort = document.createElement("span");
        inPort.className = "gd-step-entry";
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

        // --- the disc ---
        const disc = document.createElement("div");
        disc.className = "gd-step-disc";
        disc.style.background = COLOURS[node.type] || STEP_COLOURS.clock;

        const icon = document.createElement("i");
        icon.className = "las " + (ICONS[node.type] || "la-question-circle");
        disc.appendChild(icon);

        // The live run's verdict for this node, on the corner of its disc. It was in the
        // header; a step has no header, and the corner of the disc is where a reader
        // watching a run is already looking.
        const badge = document.createElement("span");
        badge.className = "badge gd-node-status-badge";
        badge.dataset.role = "status-badge";
        badge.hidden = true;
        disc.appendChild(badge);

        // The tick that says this node is one of the ones Test selection will run. On a
        // node card that was a border; a ring alone reads as "selected for editing", which
        // is a different question, so this state gets a mark of its own.
        const pick = document.createElement("span");
        pick.className = "gd-step-pick";
        pick.innerHTML = '<i class="las la-check"></i>';
        disc.appendChild(pick);

        el.appendChild(disc);

        // --- Settings and Delete, revealed on hover ---
        const actions = document.createElement("div");
        actions.className = "gd-step-actions";

        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "gd-step-action";
        editBtn.title = "Settings";
        editBtn.innerHTML = '<i class="las la-cog"></i>';
        editBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            openProperties(node.id);
        });
        actions.appendChild(editBtn);

        // Start is not deletable: `validate_graph` requires exactly one, so offering the
        // button would offer a state the save refuses.
        if (node.type !== "start") {
            const delBtn = document.createElement("button");
            delBtn.type = "button";
            delBtn.className = "gd-step-action gd-step-action-danger";
            delBtn.title = "Delete node";
            delBtn.innerHTML = '<i class="las la-trash"></i>';
            delBtn.addEventListener("click", function (e) {
                e.stopPropagation();
                deleteNode(node.id);
            });
            actions.appendChild(delBtn);
        }
        el.appendChild(actions);

        // --- name and settings ---
        const title = document.createElement("div");
        title.className = "gd-step-title";
        title.textContent = labelOf(node);
        el.appendChild(title);

        const kind = document.createElement("div");
        kind.className = "gd-step-kind";
        kind.textContent = (state.vocabulary.node_types.find(function (t) {
            return t.type === node.type;
        }) || {}).label || node.type;
        el.appendChild(kind);

        const preview = document.createElement("div");
        preview.className = "gd-step-sub";
        preview.textContent = previewOf(node);
        el.appendChild(preview);

        // --- the ways out ---
        const specs = portsOf(node);

        if (specs.length === 1 && !specs[0].label) {
            el.appendChild(buildExitDot(node, specs[0]));
        } else if (specs.length) {
            const branches = document.createElement("div");
            branches.className = "gd-step-branches";
            specs.forEach(function (spec) {
                branches.appendChild(buildBranchPill(node, spec));
            });
            el.appendChild(branches);
        }

        // The whole step drags, and a press that never moved is the click that opens the
        // node's settings or finishes a connector — see `onDragEnd`. Not from a pill, a dot
        // or an action button: each of those is a control with its own handler.
        el.addEventListener("mousedown", function (e) {
            if (e.button !== 0) return;
            if (e.target.closest(".gd-step-pill, .gd-step-exit, .gd-step-entry, .gd-step-action")) return;
            // Offered to the selection first. It takes the press only for a Ctrl-click or a
            // real group move; anything else falls through to the drag this canvas has
            // always done, shift-click picking and all.
            if (selection && selection.beginNodePress(node.id, e)) return;
            startDrag(node.id, e);
        });

        // Any part of the step finishes a connector that is already armed. Only when armed:
        // an ordinary click is handled by `onDragEnd`.
        el.addEventListener("click", function (e) {
            if (!state.pending || suppressNodeClick) return;
            e.stopPropagation();
            onTargetPortClick(node.id, e);
        });

        canvasEl.appendChild(el);
    }

    /** The single unlabelled way out of a node: a dot on its bottom edge. */
    function buildExitDot(node, spec) {
        const dot = document.createElement("span");
        dot.className = "gd-step-exit gd-node-port-out";
        dot.dataset.port = spec.port;
        dot.title = "Drag to a node to connect, or click here and then click the target";
        wireOutPort(dot, node, spec);
        return dot;
    }

    /**
     * One labelled way out of a node, as a pill under it.
     *
     * A branch's conditions, a loop's `each` / `done`, an `on error` — all of them are the
     * same thing and now look it, which is what the reference canvas does. The pill *is* the
     * output port: it keeps the `gd-node-port-out` class and the `data-port` attribute that
     * the connect, reattach and delete paths all key off, so none of those changed. It also
     * replaces the label drawn on the connector itself, which said the same word twice.
     */
    function buildBranchPill(node, spec) {
        const pill = document.createElement("span");
        pill.className = "gd-step-pill gd-node-port-out";
        pill.dataset.port = spec.port;
        if (spec.kind) pill.dataset.portKind = spec.kind;
        pill.textContent = spec.label || spec.port;
        pill.title = (spec.label || spec.port) +
            " — drag to a node to connect, or click here and then click the target";
        wireOutPort(pill, node, spec);
        return pill;
    }

    /** The two gestures every way out offers, wired the same for a dot and for a pill. */
    function wireOutPort(el, node, spec) {
        if (state.pending && state.pending.nodeId === node.id && state.pending.port === spec.port) {
            el.classList.add("gd-port-pending");
        }
        el.addEventListener("click", function (e) {
            e.stopPropagation();
            onSourcePortClick(node.id, spec.port);
        });
        // Both gestures from one control. `mousedown` opens a drag; a drag that never moves
        // does nothing on release, leaving the `click` that follows to arm the
        // click-then-click gesture instead. The two cannot both fire for one press, because
        // a real drag releases over another element and so produces no `click` here at all.
        el.addEventListener("mousedown", function (e) {
            if (e.button !== 0) return;
            e.stopPropagation();
            startConnectDrag(node.id, spec.port, e);
        });
    }

    // -----------------------------------------------------------------
    // Rendering — connectors
    // -----------------------------------------------------------------

    // -----------------------------------------------------------------
    // Anchors, and why a drag does not measure them
    //
    // `edgeRoute` finds a port by measuring it — `getBoundingClientRect`, through
    // `GC.anchor`. That is right when the canvas is still and wrong inside a drag loop.
    // The frame has just written `style.left`, so every rect read after it forces the
    // browser to lay the whole canvas out again: once per port, per connector, per
    // mousemove. Dragging one node with six connectors did that a dozen times a frame,
    // and mousemove fires faster than the screen repaints. That is the stutter.
    //
    // So a drag measures each port it needs exactly once and keeps the result as an
    // **offset from its node's stored position**. After that an anchor is two additions
    // and the loop reads nothing.
    //
    // Taking the offset as `anchor - node.position`, both sides sampled at the same
    // instant, is what makes it exact rather than approximately right. Two constant
    // discrepancies live between those spaces: the wrapper's 1px border (anchors are
    // measured from its border box, `node.position` from the layer's content box), and
    // the ports' half-pixel placement (`left: 50%; margin-left: -5.5px`). Both land
    // inside the offset. Neither has to be named here, and neither breaks if the
    // stylesheet changes the border or moves a dot.
    // -----------------------------------------------------------------

    let dragAnchors = null;

    function measuredAnchor(nodeId, portSelector) {
        return GC.portAnchor(wrapperEl, document.getElementById("node-" + nodeId), portSelector);
    }

    function portAnchor(nodeId, portSelector) {
        if (!dragAnchors) return measuredAnchor(nodeId, portSelector);

        const key = nodeId + "|" + (portSelector || "");

        // A stationary end. Nothing about it can change while the drag runs, so it is
        // measured once and then remembered outright.
        if (!dragAnchors.moving[nodeId]) {
            if (!(key in dragAnchors.frozen)) {
                dragAnchors.frozen[key] = measuredAnchor(nodeId, portSelector);
            }
            return dragAnchors.frozen[key];
        }

        const node = findNode(nodeId);
        if (!node) return null;
        const x = (node.position || {}).x || 0;
        const y = (node.position || {}).y || 0;

        if (!(key in dragAnchors.offsets)) {
            const measured = measuredAnchor(nodeId, portSelector);
            dragAnchors.offsets[key] = measured
                ? { dx: measured.x - x, dy: measured.y - y }
                : null;
        }

        const offset = dragAnchors.offsets[key];
        return offset ? { x: x + offset.dx, y: y + offset.dy } : null;
    }

    /**
     * Open the anchor cache for a drag, and warm it while the canvas is still settled.
     *
     * Warming matters: measuring lazily on the first moved frame would take one forced
     * layout, which is harmless, but it would also measure a node the frame had already
     * moved. Doing it here, before anything has been written, keeps the whole gesture
     * free of layout reads.
     *
     * @param {Array<string>} movingIds - the nodes this drag will move
     * @param {Array<object>} chrome - the resolved connectors it will repaint
     */
    function beginDragAnchors(movingIds, chrome) {
        dragAnchors = { moving: {}, offsets: {}, frozen: {} };
        movingIds.forEach(function (id) { dragAnchors.moving[id] = true; });
        chrome.forEach(function (record) { edgeRoute(record.edge); });
    }

    function endDragAnchors() {
        dragAnchors = null;
    }

    /**
     * Where the cursor is, in canvas coordinates.
     *
     * The same three lines were written inline at four sites. Named, because the bend
     * gesture needs to agree with the drag gesture about them exactly.
     *
     * @param {{clientX: number, clientY: number}} e
     * @returns {{x: number, y: number}}
     */
    function cursorPoint(e) {
        const rect = wrapperEl.getBoundingClientRect();
        return {
            x: e.clientX + wrapperEl.scrollLeft - rect.left,
            y: e.clientY + wrapperEl.scrollTop - rect.top,
        };
    }

    /**
     * A connector's hand-placed bends, as a list that is always safe to route through.
     *
     * @param {object} edge
     * @returns {Array<{x: number, y: number}>}
     */
    function waypointsOf(edge) {
        if (!edge) return [];
        return Array.isArray(edge.waypoints) ? edge.waypoints : [];
    }

    /**
     * Bends read off a stored document, keeping only the ones that are usable.
     *
     * The save schema lets the canvas add keys to its own drawing, which is what makes
     * `waypoints` possible without a migration — and means this is where a hand-edited or
     * older document is made safe. A bend that is not two finite numbers is dropped rather
     * than drawn: `NaN` in a coordinate does not fail here, it fails silently in the SVG and
     * then again at the database, which refuses it as JSON.
     *
     * @param {*} raw
     * @returns {Array<{x: number, y: number}>}
     */
    function readWaypoints(raw) {
        if (!Array.isArray(raw)) return [];

        return raw
            .filter(function (point) {
                return point && isFinite(point.x) && isFinite(point.y);
            })
            .slice(0, MAX_EDGE_WAYPOINTS)
            .map(function (point) {
                return { x: Math.max(0, Number(point.x)), y: Math.max(0, Number(point.y)) };
            });
    }

    /**
     * The selector for the port a connector leaves by.
     *
     * One place, because `edgeRoute` and the bend gesture have to agree on it exactly or a
     * bend is computed against a different line from the one on screen.
     *
     * @param {object} edge
     * @returns {string}
     */
    function sourceSelectorFor(edge) {
        return '.gd-node-port-out[data-port="' +
            GC.cssEscape(edge.source_port || "default") + '"]';
    }

    /**
     * How far right the lane sits that a connector running back up the canvas uses.
     * Measured off the nodes, because the canvas is as wide as the pipeline drawn on it.
     */
    function returnLaneX() {
        // Memoised for the duration of one drag frame only. It reduces over every node,
        // and `edgeRoute` asks for it once per back edge, so on a canvas with a loop this
        // was O(nodes x edges) per frame. Guarded on the anchor cache, which exists for
        // exactly as long as a drag — single or group — is running, so the memo cannot
        // outlive the gesture that justified it. Outside one this is the plain reduce it
        // always was.
        if (dragAnchors && laneXCache !== null) return laneXCache;

        const rightmost = state.nodes.reduce(function (widest, node) {
            return Math.max(widest, (node.position || {}).x || 0);
        }, 0);

        const lane = rightmost + canvasMetrics().stepWidth + GC.ELBOW_LANE;
        if (dragAnchors) laneXCache = lane;
        return lane;
    }

    /**
     * The corner points one connector runs through.
     *
     * A connector the server reported as a back edge — a `for each` or `do until` sending
     * the run round again — takes the return lane rather than a downward step. There is no
     * downward step available: its target is above it, which is what makes it a loop.
     */
    function edgeRoute(edge) {
        const from = portAnchor(edge.source, sourceSelectorFor(edge));
        const to = portAnchor(edge.target, '[data-port-role="in"]') || portAnchor(edge.target, null);

        if (!from || !to) return null;

        const bends = waypointsOf(edge);

        // A hand-placed bend beats the return lane. The lane is a fallback — `elbowPoints`
        // says why it exists: there is no way down to a target that is up. A waypoint is a
        // person saying where the wire goes, and a person's decision beats a fallback. The
        // failure mode of the other order is worse: you bend a wire, nothing visible
        // happens, and you bend it again.
        return (state.backEdges[edge.id] && !bends.length)
            ? GC.backEdgePoints(from, to, returnLaneX())
            : GC.waypointPoints(from, to, bends);
    }

    function renderAllEdges() {
        edgesGroupEl.innerHTML = "";
        state.edges.forEach(renderEdge);
    }

    function renderEdge(edge) {
        const route = edgeRoute(edge);
        if (!route) return;

        const d = GC.elbowPathD(route);
        const group = GC.svg("g");
        group.id = "edge-group-" + edge.id;
        // The selected class goes on the group as well as the path, so the stylesheet can
        // reveal this connector's ✕ and handles — they are siblings of the path.
        group.setAttribute(
            "class",
            "gd-edge-group" +
            (edge.id === state.selectedEdgeId ? " gd-edge-group-selected" : "") +
            (selection && selection.marksEdge(edge.id) ? " gd-edge-multi" : ""),
        );

        const select = function (e) {
            e.stopPropagation();
            state.selectedEdgeId = edge.id;
            renderAllEdges();
        };

        // An invisible fat path under the visible one, purely to be hovered and clicked. The
        // line is 2px and its controls now only appear on hover, so without this they would
        // mean landing the cursor inside two pixels.
        const hit = GC.svg("path");
        hit.setAttribute("class", "gd-edge-hit");
        hit.setAttribute("d", d);
        hit.addEventListener("click", function (e) {
            // Not the click that trailed a bend: a wire that was just routed must not also
            // become the selected one.
            if (suppressEdgeClick) return;
            select(e);
        });
        // Press and move to bend it; press and release to select it. The same split
        // `wireOutPort` documents for a port, which is why the click listener stays: under
        // the threshold nothing happens here and the click does its job.
        hit.addEventListener("mousedown", function (e) { startBend(edge.id, e); });
        // Straightening it. This canvas has no connector properties panel, so this is the
        // only affordance there is — which is why it is the one both canvases share.
        hit.addEventListener("dblclick", function (e) {
            e.stopPropagation();
            straightenEdge(edge.id);
        });
        group.appendChild(hit);

        const path = GC.svg("path");
        path.id = "edge-" + edge.id;
        path.setAttribute("d", d);

        const port = edge.source_port || "default";
        if (port === "error") path.classList.add("gd-edge-error");
        if (port === "body") path.classList.add("gd-edge-loop");
        if (edge.id === state.selectedEdgeId) path.classList.add("gd-edge-selected");

        path.addEventListener("click", select);
        group.appendChild(path);

        // The port's name used to be drawn on the connector here. It is now on the pill the
        // connector leaves from, which is the same word in a place that does not move when
        // the line does — and one label per way out rather than one per connector.

        // The bend handles, before the ✕ and the reattach handles rather than after, so
        // that where two of them land close together on a heavily routed wire the reattach
        // handle paints on top and wins the press. Reattaching is the more destructive
        // gesture and should not be taken by accident.
        waypointsOf(edge).forEach(function (bend, index) {
            group.appendChild(waypointHandle(index, bend));
        });

        group.appendChild(deleteButton(edge.id, GC.pointAlongPolyline(route, 0.5)));
        group.appendChild(endHandle(edge.id, "source", GC.pointAlongPolyline(route, 0.15)));
        group.appendChild(endHandle(edge.id, "target", GC.pointAlongPolyline(route, 0.85)));

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

    /**
     * The small circle that marks one hand-placed bend.
     *
     * Drawn at the **stored** point rather than on the line: `elbowPathD` pulls the stroke
     * up to the corner radius inside every corner, so a handle drawn on the stroke would sit
     * visibly off the pixel the user put it at.
     *
     * @param {number} index
     * @param {{x: number, y: number}} pt
     * @returns {SVGElement}
     */
    function waypointHandle(index, pt) {
        const circle = GC.svg("circle");
        circle.setAttribute("class", "gd-edge-waypoint");
        circle.setAttribute("data-waypoint", String(index));
        circle.setAttribute("r", "4");
        circle.setAttribute("cx", pt.x);
        circle.setAttribute("cy", pt.y);
        return circle;
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

    /**
     * The elements one connector's geometry is written to.
     *
     * Resolved once and carried through a drag rather than looked up per frame: the six
     * queries below were being re-run for every connector on every mousemove to find the
     * same elements again.
     *
     * @param {object} record - `{edge}` at minimum; filled in place and returned
     * @returns {object}
     */
    function resolveEdgeChrome(record) {
        const group = document.getElementById("edge-group-" + record.edge.id);
        record.group = group;
        record.path = document.getElementById("edge-" + record.edge.id);
        record.hit = group ? group.querySelector(".gd-edge-hit") : null;
        record.deleteBtn = group ? group.querySelector(".gd-edge-delete-btn") : null;
        record.sourceHandle = group ? group.querySelector(".gd-edge-handle-source") : null;
        record.targetHandle = group ? group.querySelector(".gd-edge-handle-target") : null;
        record.waypointHandles = group
            ? Array.prototype.slice.call(group.querySelectorAll(".gd-edge-waypoint"))
            : [];
        return record;
    }

    function edgeChrome(edge) {
        return resolveEdgeChrome({ edge: edge });
    }

    /**
     * Rebuild one connector's DOM, replacing whatever was there.
     *
     * `renderEdge` appends. Every other caller reaches it through `renderAllEdges`, which
     * has just emptied the group — so calling it on its own without this would leave two
     * elements sharing one id, and `getElementById` would keep finding the stale one.
     *
     * @param {object} edge
     */
    function reRenderEdge(edge) {
        const existing = document.getElementById("edge-group-" + edge.id);
        if (existing) existing.remove();
        renderEdge(edge);
    }

    /**
     * Move an already-drawn connector to where its nodes are now.
     *
     * In place. This used to remove the whole `<g>` and call `renderEdge` again, which
     * rebuilt two paths, the ✕ with its two children, both end handles and all five of
     * their listeners — every frame, for every connector attached to a moving node. That
     * is what made a dragged line flicker: the group was briefly not in the document at
     * all. Setting four attributes leaves the listeners alone and never unparents
     * anything.
     *
     * @param {object} record - from `edgeChrome`
     */
    function updateEdgeGeometry(record) {
        // A connector whose group was never built — `renderEdge` returns early when
        // neither end could be measured — is built now rather than silently left out of
        // the drawing, and its freshly created elements picked up for the next frame.
        if (!record.group || !record.group.isConnected) {
            reRenderEdge(record.edge);
            resolveEdgeChrome(record);
            return;
        }

        const route = edgeRoute(record.edge);
        const d = route ? GC.elbowPathD(route) : "";
        if (record.path) record.path.setAttribute("d", d);
        if (record.hit) record.hit.setAttribute("d", d);
        if (!route) return;

        if (record.deleteBtn) {
            const mid = GC.pointAlongPolyline(route, 0.5);
            record.deleteBtn.setAttribute("transform", "translate(" + mid.x + "," + (mid.y + 10) + ")");
        }
        if (record.sourceHandle) {
            const pt = GC.pointAlongPolyline(route, 0.15);
            record.sourceHandle.setAttribute("cx", pt.x);
            record.sourceHandle.setAttribute("cy", pt.y);
        }
        if (record.targetHandle) {
            const pt = GC.pointAlongPolyline(route, 0.85);
            record.targetHandle.setAttribute("cx", pt.x);
            record.targetHandle.setAttribute("cy", pt.y);
        }

        // The bend handles follow the stored points, not the line — see `waypointHandle`.
        if (record.waypointHandles.length) {
            const bends = waypointsOf(record.edge);
            record.waypointHandles.forEach(function (handle) {
                const bend = bends[Number(handle.getAttribute("data-waypoint"))];
                if (!bend) return;
                handle.setAttribute("cx", bend.x);
                handle.setAttribute("cy", bend.y);
            });
        }
    }

    /**
     * The connectors a move of these nodes will disturb, resolved ready to repaint.
     *
     * Built once at the start of a drag. Doing it per frame meant walking every edge and
     * re-querying the DOM for each one, which is work that cannot have changed while the
     * mouse is down.
     *
     * @param {Array<string>} movingIds
     * @returns {Array<object>}
     */
    function chromeForMovingNodes(movingIds) {
        const moving = {};
        movingIds.forEach(function (id) { moving[id] = true; });

        return state.edges
            .filter(function (edge) { return moving[edge.source] || moving[edge.target]; })
            .map(edgeChrome);
    }

    // -----------------------------------------------------------------
    // Layout — asking the server where the nodes go
    //
    // The arithmetic is in app/services/canvas_layout/layout_service.py, shared with the
    // Flow Builder's canvas, and that file says why it is Python: layout is the part of a
    // drawing that can be wrong without looking wrong, and only Python can be tested here.
    // What stays on this side is the half that needs a browser — turning a column into an x,
    // and stacking each layer below the *measured* height of the one above, since a branch
    // with six conditions is much taller than a Wait node.
    // -----------------------------------------------------------------

    // Enough for the last of a burst of edits to settle.
    const LAYOUT_DEBOUNCE_MS = 120;

    // The canvas was a fixed size in the stylesheet, which was enough while pipelines ran
    // left to right. Top-down they do not fit, and a node placed past the edge cannot be
    // scrolled to — the wrapper only scrolls as far as its content.
    const CANVAS_MIN_WIDTH = 4000;
    const CANVAS_MIN_HEIGHT = 2400;

    let layoutTimer = null;

    /**
     * The layout's spacing, in pixels, per the stylesheet.
     *
     * Read from CSS rather than restated here so the two cannot drift — the same reasoning
     * the old `portMetrics` carried, applied to the numbers that now matter.
     */
    function canvasMetrics() {
        if (canvasMetricsCache) return canvasMetricsCache;

        const style = window.getComputedStyle(canvasEl);
        const read = function (name, fallback) {
            const value = parseFloat(style.getPropertyValue(name));
            return isFinite(value) && value > 0 ? value : fallback;
        };

        canvasMetricsCache = {
            stepWidth: read("--gd-step-w", 176),
            columnGap: read("--gd-col-gap", 48),
            rowGap: read("--gd-row-gap", 62),
            marginX: read("--gd-margin-x", 48),
            marginY: read("--gd-margin-y", 32),
        };
        return canvasMetricsCache;
    }

    /**
     * Note that the wiring changed: the graph is now unsaved, and the canvas needs
     * re-arranging.
     *
     * One function at every structural call site rather than a `markDirty()` and a
     * `syncLayout()` each, so a new kind of edit cannot dirty the graph and forget to
     * re-arrange it — which would leave a node sitting where its old connections put it.
     */
    function wiringChanged() {
        markDirty();
        syncLayout();
    }

    /** Ask for a fresh layout once the current burst of edits stops. */
    function syncLayout() {
        if (layoutTimer) window.clearTimeout(layoutTimer);
        layoutTimer = window.setTimeout(function () {
            layoutTimer = null;
            requestLayout();
        }, LAYOUT_DEBOUNCE_MS);
    }

    /**
     * Post the current wiring and use what comes back.
     *
     * Sent in both modes, applied in one: even a hand-arranged canvas needs to know which
     * connectors run backwards, because that decides whether a line is drawn as a step down
     * or as a return round the side. So positions are used only in `auto`, back edges
     * always.
     *
     * Only ids, types and ends are sent — a node's settings are not the layout's business
     * and some of them are long (a SQL statement, an email body), and this runs after every
     * edit.
     */
    function requestLayout() {
        if (!opts.layoutUrl) return;

        const edges = state.edges;

        fetch(opts.layoutUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                nodes: state.nodes.map(function (n) { return { id: n.id, type: n.type }; }),
                edges: edges.map(function (e) {
                    return { source: e.source, target: e.target };
                }),
            }),
        })
            .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error(r.status)); })
            .then(function (data) {
                // Indices into the array that was posted, mapped to connector ids while that
                // array is still in hand — keeping the indices would go stale the moment the
                // next edit reordered anything.
                state.backEdges = {};
                (data.back_edges || []).forEach(function (index) {
                    const edge = edges[index];
                    if (edge) state.backEdges[edge.id] = true;
                });

                if (state.layout === "auto") applyLayout(data.positions);
                else renderAllEdges();
            })
            .catch(function () {
                // The nodes stay where they are. Nothing is lost and nothing is blank — the
                // canvas may be holding unsaved work, and its arrangement is the least
                // important thing on it. Same rule this file's save and run paths follow: a
                // failed request is a note, never a wiped canvas.
                noteLayoutFailure();
            });
    }

    /**
     * Say why something the canvas was asked to do did not happen.
     *
     * @param {string} [message] - defaults to the layout request's own failure
     */
    function noteLayoutFailure(message) {
        const target = document.getElementById("gdSaveResult");
        if (!target) return;

        const div = document.createElement("div");
        div.className = "alert alert-warning py-2 mb-0";
        div.textContent = message || "Could not tidy the canvas just now. " +
            "The nodes have been left where they were.";
        target.innerHTML = "";
        target.appendChild(div);
    }

    /**
     * The widest thing any node draws, measured rather than assumed.
     *
     * The node itself is a fixed `--gd-step-w`, which is what keeps every disc centred over
     * the same point in its column. Its branch pills are not: they sit side by side and
     * overflow the node evenly on both sides, so a Branch carrying two written conditions is
     * far wider than the disc above it. Columns are spaced by this number so that a
     * fractional column — the mean of its parents — still lands somewhere nothing else has
     * claimed.
     *
     * @returns {number} px, 0 when nothing has been rendered yet
     */
    function widestStep() {
        let widest = 0;

        state.nodes.forEach(function (node) {
            const el = document.getElementById("node-" + node.id);
            if (!el) return;
            widest = Math.max(widest, el.getBoundingClientRect().width);
            const pills = el.querySelector(".gd-step-branches");
            if (pills) widest = Math.max(widest, pills.getBoundingClientRect().width);
        });

        return widest;
    }

    /**
     * Put every node where the server said, and redraw.
     *
     * Two passes, because the second needs the first to have happened in the document: x
     * from the column and everything rendered, then y from the layer with each row starting
     * below the tallest node of the row above. Uniform row heights would either waste a band
     * of canvas under every short row or let a six-condition branch overlap what is beneath
     * it.
     *
     * An auto-arrange does **not** mark the graph unsaved: a position changes nothing about
     * what a run does, and the unsaved badge is a warning that the drawing on screen is not
     * the drawing that would run. Flagging every open of every pipeline would make it mean
     * nothing.
     */
    function applyLayout(positions) {
        if (!positions) return;

        const m = canvasMetrics();
        const rows = {};

        // Render before measuring. A node is `--gd-step-w` wide, but its branch pills sit
        // side by side and may carry their row wider than that — so the width that decides
        // how far apart columns go is the widest row on the canvas, not the width of the
        // node above it.
        renderAllNodes();

        const widest = Math.max(m.stepWidth, widestStep());
        const pitch = widest + m.columnGap;

        // Half of a wide row hangs off each side of its node, so the first column has to
        // start far enough in for the left half to have somewhere to be.
        const leftPad = Math.max(m.marginX, Math.ceil((widest - m.stepWidth) / 2) + 8);

        state.nodes.forEach(function (node) {
            const place = positions[node.id];

            // No place means the server did not see this node — it was added while the
            // request was out. It keeps where it is and the next pass picks it up.
            if (!place) return;

            node.position = {
                x: Math.round(leftPad + place.column * pitch),
                y: (node.position || {}).y || m.marginY,
            };
            const el = document.getElementById("node-" + node.id);
            if (el) el.style.left = node.position.x + "px";
            (rows[place.layer] = rows[place.layer] || []).push(node);
        });

        let top = m.marginY;

        Object.keys(rows)
            .map(Number)
            .sort(function (a, b) { return a - b; })
            .forEach(function (layer) {
                let tallest = 0;

                rows[layer].forEach(function (node) {
                    const el = document.getElementById("node-" + node.id);
                    node.position.y = top;
                    if (!el) return;
                    el.style.top = top + "px";
                    tallest = Math.max(tallest, el.getBoundingClientRect().height);
                });

                top += tallest + m.rowGap;
            });

        fitCanvas();
        renderAllEdges();
    }

    /**
     * Grow the canvas and its connector layer to hold the drawing.
     *
     * Both, and to the same size: the connectors are their own SVG, and one sized smaller
     * than the canvas would clip every line past its edge rather than fail visibly.
     */
    function fitCanvas() {
        const m = canvasMetrics();
        let right = 0;
        let bottom = 0;

        state.nodes.forEach(function (node) {
            const el = document.getElementById("node-" + node.id);
            const box = el ? el.getBoundingClientRect() : { width: m.stepWidth, height: 0 };
            right = Math.max(right, ((node.position || {}).x || 0) + box.width);
            bottom = Math.max(bottom, ((node.position || {}).y || 0) + box.height);
        });

        // Past the rightmost node by the return lane's width, so a loop's connector has
        // somewhere to run rather than being drawn off the edge.
        const width = Math.max(CANVAS_MIN_WIDTH, right + m.stepWidth + GC.ELBOW_LANE * 2);
        const height = Math.max(CANVAS_MIN_HEIGHT, bottom + m.marginY * 2);

        [canvasEl, edgesSvgEl].forEach(function (el) {
            if (!el) return;
            el.style.width = width + "px";
            el.style.height = height + "px";
        });
    }

    /**
     * Hand the arranging back to the canvas: the Tidy up button.
     *
     * Marks the graph unsaved, unlike an arrange that happens on its own — pressing this
     * changes what gets stored (the canvas stops being manual), which is an edit even though
     * the nodes are all that visibly move.
     */
    function tidyUp() {
        // Tidy up already means "throw away my arrangement and let the canvas decide". A
        // hand-routed wire only means anything against a known arrangement, so leaving the
        // bends would give a drawing that is neither arranged nor hand-drawn, with no button
        // that fixes it. But it destroys work somebody did by hand, so it asks — with the
        // count, so the question is answerable.
        const bent = state.edges.filter(function (edge) { return waypointsOf(edge).length; });
        if (bent.length) {
            const wires = bent.length === 1 ? "1 connector" : bent.length + " connectors";
            if (!window.confirm("Tidying up will straighten " + wires +
                " you routed by hand. Continue?")) {
                return;
            }
            bent.forEach(function (edge) { delete edge.waypoints; });
        }

        state.layout = "auto";
        updateLayoutButton();
        markDirty();
        renderAllEdges();
        requestLayout();
    }

    /** Show on the Tidy up button whether the canvas is arranging itself. */
    function updateLayoutButton() {
        const button = document.getElementById("gdTidyBtn");
        if (!button) return;

        const manual = state.layout === "manual";
        button.classList.toggle("btn-outline-primary", manual);
        button.classList.toggle("btn-outline-secondary", !manual);
        button.title = manual
            ? "Nodes are where you put them. Arrange the canvas automatically again."
            : "Arrange the canvas again";
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
            clientX: e.clientX,
            clientY: e.clientY,
            moved: false,
            // Shift-click picks a node for a test run. Recorded at the press because the
            // release is where a click is finally recognised, and by then the modifier is
            // only knowable from the event that started it.
            shift: e.shiftKey,
            // Which connectors this move disturbs, and where their geometry is written.
            // Resolved now, while nothing has moved and the DOM is settled.
            chrome: chromeForMovingNodes([nodeId]),
        };
        beginDragAnchors([nodeId], state.dragging.chrome);
        document.addEventListener("mousemove", onDragMove);
        document.addEventListener("mouseup", onDragEnd);
    }

    /**
     * A mousemove during a node drag: record where the cursor is, and ask for a frame.
     *
     * Nothing is written here. Two things used to be wrong with doing the work inline.
     * The node was moved **before** the threshold was consulted, so a one-pixel click on
     * a node quietly changed its stored position — discarded on the next auto-arrange, or
     * kept as an unmarked edit in manual mode. And a repaint ran per mousemove, which
     * fires faster than the screen updates, so most of them were thrown away unseen.
     */
    function onDragMove(e) {
        if (!state.dragging) return;

        if (!state.dragging.moved) {
            const travelled = Math.abs(e.clientX - state.dragging.startX) +
                Math.abs(e.clientY - state.dragging.startY);
            if (travelled < DRAG_THRESHOLD_PX) return;
            state.dragging.moved = true;

            // Manual from the first frame that really moves, not from the release. The
            // layout request is debounced, so its answer can land while the mouse is
            // still down — and `applyLayout` is free to re-place every node while the
            // canvas is still "auto". Flipping here closes that window.
            state.layout = "manual";
            updateLayoutButton();
        }

        state.dragging.clientX = e.clientX;
        state.dragging.clientY = e.clientY;
        scheduleDragFrame();
    }

    function scheduleDragFrame() {
        if (dragFrame !== null) return;
        dragFrame = requestAnimationFrame(runDragFrame);
    }

    function cancelDragFrame() {
        if (dragFrame === null) return;
        cancelAnimationFrame(dragFrame);
        dragFrame = null;
    }

    /**
     * Drop a drag in progress without committing it.
     *
     * Called from the deletion paths. A drag carries resolved elements for the connectors
     * it is repainting, and deleting a node or an edge detaches some of them; continuing
     * would write attributes into a group that is no longer in the document.
     */
    function abandonDrag() {
        if (!state.dragging) return;
        cancelDragFrame();
        state.dragging = null;
        laneXCache = null;
        endDragAnchors();
        document.removeEventListener("mousemove", onDragMove);
        document.removeEventListener("mouseup", onDragEnd);
    }

    /**
     * One frame of a node drag: place the node, then move its connectors onto it.
     */
    function runDragFrame() {
        dragFrame = null;

        // A bend and a node move both use this frame, and only one of them is ever live.
        if (bending) {
            if (bending.moved) runBendFrame();
            return;
        }

        const drag = state.dragging;
        if (!drag || !drag.moved) return;

        const node = findNode(drag.nodeId);
        if (!node) return;

        const rect = wrapperEl.getBoundingClientRect();
        const x = drag.clientX + wrapperEl.scrollLeft - rect.left - drag.offsetX;
        const y = drag.clientY + wrapperEl.scrollTop - rect.top - drag.offsetY;
        node.position = { x: Math.max(0, x), y: Math.max(0, y) };

        const el = document.getElementById("node-" + node.id);
        if (el) {
            el.style.left = node.position.x + "px";
            el.style.top = node.position.y + "px";
        }

        laneXCache = null;
        drag.chrome.forEach(updateEdgeGeometry);
    }

    /**
     * Finish a press on a node: either it moved, or it was a click.
     *
     * A node that really moved switches the canvas to **manual** — somebody who has placed
     * a node has said where they want it, and re-arranging it on the next edit would be the
     * canvas arguing. Tidy up hands the decision back.
     *
     * A press that never moved is handled here only for the shift-click that picks a node
     * for a test run. An ordinary click is left to the step's own `click` listener, which
     * finishes an armed connector — the behaviour this canvas always had, where the cog
     * button rather than the body opens the settings.
     */
    function onDragEnd() {
        const drag = state.dragging;
        cancelDragFrame();
        state.dragging = null;
        laneXCache = null;
        endDragAnchors();
        document.removeEventListener("mousemove", onDragMove);
        document.removeEventListener("mouseup", onDragEnd);

        if (!drag) return;

        // A click always follows the mouseup that ended a drag. If the node really moved,
        // that click is the tail of the drag and must not be read as "and this node is
        // the connector's target" — cleared on the next tick, once the click has passed.
        const swallowTrailingClick = function () {
            suppressNodeClick = true;
            setTimeout(function () { suppressNodeClick = false; }, 0);
        };

        if (drag.moved) {
            // `state.layout` was already switched to manual on the first frame that
            // moved — see `onDragMove` — so all that is left is the once-per-gesture
            // work: grow the canvas to the new arrangement, and say it is unsaved.
            fitCanvas();
            markDirty();
            swallowTrailingClick();
            return;
        }

        if (drag.shift) {
            togglePicked(drag.nodeId);
            swallowTrailingClick();
        }
    }

    // -----------------------------------------------------------------
    // Bending a connector
    //
    // A connector is routed for you, and now and then the route is wrong — it runs behind a
    // node, or two of them overlap into one line nobody can follow. Dragging the wire puts a
    // bend in it, and the wire goes through that point from then on.
    //
    // Bends are stored on the connector as `waypoints`, in canvas coordinates. Absolute
    // rather than relative to the two ends, because a bend exists to dodge something *on the
    // canvas* — another node, another wire — and a bend that tracked its endpoints would
    // slide off the thing it was put there to avoid. The cost of absolute is that a bend can
    // go stale when a node moves, and that is paid explicitly: a group move carrying **both**
    // ends of a wire carries its bends with it, and a move of one end leaves them alone.
    // -----------------------------------------------------------------

    // True for one tick after a bend, so the click that trails it does not also select the
    // connector. The same device as `suppressNodeClick`.
    let suppressEdgeClick = false;

    // {edgeId, index, inserted, fromX, fromY, clientX, clientY, moved, chrome}
    let bending = null;

    /**
     * Whether a press on a connector is a bend, a group move, or nothing.
     *
     * One named decision, because this is the seam the selection layer meets: a connector
     * that is part of a **multi**-item selection drags the whole selection instead of
     * bending.
     *
     * Multi-item, not merely selected. Clicking a wire selects it, so if "selected" alone
     * meant group-drag, the ordinary sequence of clicking a wire and then dragging it would
     * move two nodes instead of putting a bend in it.
     *
     * @param {string} edgeId
     * @param {MouseEvent} e
     * @returns {"group"|"bend"|"ignore"}
     */
    function edgeGrabIntent(edgeId, e) {
        if (e.button !== 0) return "ignore";
        // Modifiers belong to the selection: the click that follows extends it.
        if (e.shiftKey || e.ctrlKey || e.metaKey) return "ignore";
        if (state.connecting || state.reattaching || state.dragging) return "ignore";
        if (selection && selection.isMulti() && selection.hasEdge(edgeId)) return "group";
        return "bend";
    }

    /**
     * Begin a bend: either move the bend that was grabbed, or put a new one in.
     *
     * @param {string} edgeId
     * @param {MouseEvent} e
     */
    function startBend(edgeId, e) {
        const intent = edgeGrabIntent(edgeId, e);
        if (intent === "ignore") return;
        if (intent === "group") {
            // The selection owns this press. It needs a node to hang the move on, and
            // either end will do — the connector being in the selection is what brings both
            // of them along.
            const edge = findEdge(edgeId);
            if (edge) selection.beginNodePress(edge.source, e);
            return;
        }

        const edge = findEdge(edgeId);
        if (!edge) return;

        e.preventDefault();
        e.stopPropagation();

        const bends = waypointsOf(edge).map(function (bend) {
            return { x: bend.x, y: bend.y };
        });
        const at = cursorPoint(e);

        let index = -1;
        bends.forEach(function (bend, i) {
            if (index === -1 &&
                Math.abs(bend.x - at.x) <= WAYPOINT_GRAB_PX &&
                Math.abs(bend.y - at.y) <= WAYPOINT_GRAB_PX) {
                index = i;
            }
        });
        let inserted = false;

        if (index === -1) {
            if (bends.length >= MAX_EDGE_WAYPOINTS) {
                // Refused, with a sentence rather than by doing nothing.
                noteLayoutFailure("A connector can have at most " + MAX_EDGE_WAYPOINTS +
                    " bends. Drag one of the bends it already has instead.");
                return;
            }

            const route = edgeRoute(edge);
            const segment = route ? GC.nearestSegment(route, at) : null;
            if (!segment) return;

            // Where in the list the new bend goes: after every existing bend that is on an
            // earlier leg, so the order of the list stays the order of the wire.
            const detail = GC.waypointRoute(
                portAnchor(edge.source, sourceSelectorFor(edge)),
                portAnchor(edge.target, '[data-port-role="in"]') || portAnchor(edge.target, null),
                bends,
            );
            const marks = detail ? detail.waypointAt : [];
            index = marks.filter(function (mark) { return mark <= segment.index; }).length;
            // Started at the cursor's foot on the leg that was grabbed, so the wire does not
            // jump the instant it is taken hold of.
            bends.splice(index, 0, { x: segment.projection.x, y: segment.projection.y });
            inserted = true;
        }

        edge.waypoints = bends;
        // A handle has to exist for the new bend, so this connector is rebuilt once — and
        // only this one.
        reRenderEdge(edge);

        bending = {
            edgeId: edgeId,
            index: index,
            inserted: inserted,
            fromX: e.clientX,
            fromY: e.clientY,
            clientX: e.clientX,
            clientY: e.clientY,
            moved: false,
            chrome: edgeChrome(edge),
        };
        // Both ends are still, so the anchor cache freezes them and the whole gesture is
        // arithmetic — the frame reads nothing.
        beginDragAnchors([], [bending.chrome]);

        document.addEventListener("mousemove", onBendMove);
        document.addEventListener("mouseup", onBendEnd);
    }

    function onBendMove(e) {
        if (!bending) return;

        if (!bending.moved) {
            const travelled = Math.abs(e.clientX - bending.fromX) +
                Math.abs(e.clientY - bending.fromY);
            if (travelled < DRAG_THRESHOLD_PX) return;
            bending.moved = true;
        }

        bending.clientX = e.clientX;
        bending.clientY = e.clientY;
        scheduleDragFrame();
    }

    /** One frame of a bend. Shares the drag frame, so only one gesture repaints per tick. */
    function runBendFrame() {
        const edge = findEdge(bending.edgeId);
        if (!edge) return;

        const bends = waypointsOf(edge);
        const bend = bends[bending.index];
        if (!bend) return;

        const at = cursorPoint({ clientX: bending.clientX, clientY: bending.clientY });

        // Lines to snap to: the two ends of the wire, and the neighbouring bends. This is
        // what makes a hand-routed wire look drawn rather than approximately placed.
        const from = portAnchor(edge.source, sourceSelectorFor(edge));
        const to = portAnchor(edge.target, '[data-port-role="in"]') || portAnchor(edge.target, null);
        const xs = [];
        const ys = [];
        if (from) { xs.push(from.x); ys.push(from.y); }
        if (to) { xs.push(to.x); ys.push(to.y); }
        bends.forEach(function (other, i) {
            if (i === bending.index) return;
            xs.push(other.x);
            ys.push(other.y);
        });

        bend.x = Math.max(0, GC.snapToAny(at.x, xs, WAYPOINT_SNAP_PX));
        bend.y = Math.max(0, GC.snapToAny(at.y, ys, WAYPOINT_SNAP_PX));

        laneXCache = null;
        updateEdgeGeometry(bending.chrome);
    }

    function onBendEnd() {
        const gesture = bending;
        cancelDragFrame();
        bending = null;
        endDragAnchors();
        laneXCache = null;
        document.removeEventListener("mousemove", onBendMove);
        document.removeEventListener("mouseup", onBendEnd);

        if (!gesture) return;

        const edge = findEdge(gesture.edgeId);
        if (!edge) return;

        // A press that never moved is a click, and the click listener on the hit path will
        // select the connector. A bend inserted for it is taken back out — clicking a wire
        // must not leave a bend behind.
        if (!gesture.moved) {
            if (gesture.inserted) {
                edge.waypoints.splice(gesture.index, 1);
                if (!edge.waypoints.length) delete edge.waypoints;
                reRenderEdge(edge);
            }
            return;
        }

        // Dropped back onto the line it would take without this bend: the bend is bending
        // nothing, so it goes and the wire straightens itself.
        discardRedundantWaypoint(edge, gesture.index);

        reRenderEdge(edge);
        markDirty();
        // Manual, for the same reason a moved node switches: a hand-routed wire is only
        // meaningful against a known arrangement, and an auto-arrange is free to move both
        // of its ends out from under it.
        state.layout = "manual";
        updateLayoutButton();

        suppressEdgeClick = true;
        setTimeout(function () { suppressEdgeClick = false; }, 0);
    }

    /**
     * Remove a bend dropped onto the route the wire would take without it.
     *
     * @param {object} edge
     * @param {number} index
     */
    function discardRedundantWaypoint(edge, index) {
        const bends = waypointsOf(edge);
        const bend = bends[index];
        if (!bend) return;

        const without = bends.slice();
        without.splice(index, 1);

        const from = portAnchor(edge.source, sourceSelectorFor(edge));
        const to = portAnchor(edge.target, '[data-port-role="in"]') || portAnchor(edge.target, null);
        if (!from || !to) return;

        const plain = state.backEdges[edge.id] && !without.length
            ? GC.backEdgePoints(from, to, returnLaneX())
            : GC.waypointPoints(from, to, without);
        const segment = plain ? GC.nearestSegment(plain, bend) : null;

        if (segment && segment.distance <= WAYPOINT_DISCARD_PX) {
            edge.waypoints = without;
            if (!edge.waypoints.length) delete edge.waypoints;
        }
    }

    /**
     * Throw away every bend on one connector.
     *
     * @param {string} edgeId
     */
    function straightenEdge(edgeId) {
        const edge = findEdge(edgeId);
        if (!edge || !waypointsOf(edge).length) return;

        delete edge.waypoints;
        reRenderEdge(edge);
        markDirty();
    }

    // -----------------------------------------------------------------
    // Moving several nodes at once
    //
    // The gesture is in static/js/graph_selection.js, shared with the other two canvases.
    // What is here is only what a group move *means* on this one: which connectors have to
    // follow, and what a finished move says about the drawing.
    // -----------------------------------------------------------------

    // The connectors a group move is repainting, resolved once when it starts, and the
    // hand-placed bends it is carrying with it.
    let groupChrome = null;
    let groupBends = null;

    function onGroupMoveBegin(ids) {
        // Manual from the first frame that really moves, exactly as a single drag switches
        // then and for the same reason: the layout request is debounced, so its answer can
        // arrive while the mouse is still down and re-place every node under the cursor.
        state.layout = "manual";
        updateLayoutButton();

        groupChrome = chromeForMovingNodes(ids);
        beginDragAnchors(ids, groupChrome);
        groupBends = captureCarriedBends(ids);
    }

    /**
     * A group move carries the bends of any connector it is moving *both* ends of.
     *
     * Bends are canvas coordinates, so picking up two nodes and their wire has to move the
     * wire's hand-routing with them or the route is left behind, dodging nothing. A wire
     * with only one end in the move keeps its bends exactly where they are, which is the
     * other half of the same rule: the bend was put there to avoid something on the canvas,
     * and that something has not moved.
     *
     * @param {Array<string>} ids
     * @returns {Array<{edge: object, starts: Array<{x: number, y: number}>}>}
     */
    function captureCarriedBends(ids) {
        const moving = {};
        ids.forEach(function (id) { moving[id] = true; });

        return state.edges
            .filter(function (edge) {
                return moving[edge.source] && moving[edge.target] && waypointsOf(edge).length;
            })
            .map(function (edge) {
                return {
                    edge: edge,
                    starts: waypointsOf(edge).map(function (bend) {
                        return { x: bend.x, y: bend.y };
                    }),
                };
            });
    }

    function onGroupMoveFrame(ids, dx, dy) {
        // From the captured start plus the delta, never by accumulating — the same reason
        // the nodes themselves are placed that way.
        if (groupBends) {
            groupBends.forEach(function (carried) {
                carried.edge.waypoints = carried.starts.map(function (start) {
                    return { x: Math.max(0, start.x + dx), y: Math.max(0, start.y + dy) };
                });
            });
        }

        laneXCache = null;
        if (groupChrome) groupChrome.forEach(updateEdgeGeometry);
    }

    /**
     * A group move has finished — committed, or put back by Escape.
     *
     * @param {Array<string>} ids
     * @param {boolean} committed
     */
    function onGroupMoveEnd(ids, committed) {
        endDragAnchors();
        groupChrome = null;
        groupBends = null;
        laneXCache = null;

        if (!committed) return;
        fitCanvas();
        markDirty();
    }

    /**
     * Keep the properties panel honest about a selection that is no longer one thing.
     *
     * The panel edits one node. A box that has just caught five of them has not asked to
     * edit any of them, so the panel is emptied — rather than left showing whichever node
     * happened to be open, and rather than closed, since closing a panel somebody opened
     * is as surprising as opening one they did not.
     */
    function onSelectionChange() {
        updateSelectAllButton();

        if (selection.count() === 1) return;
        if (!state.selectedNodeId && !state.selectedEdgeId) return;

        const previous = state.selectedNodeId;
        state.selectedNodeId = null;
        state.selectedEdgeId = null;
        if (previous) updateNodeMarks(previous);
        renderAllEdges();
        propertiesBodyEl.innerHTML =
            '<p class="text-muted small">Select a node to edit it here.</p>';
    }

    /**
     * The Select all button doubles as Clear, and says which it is.
     *
     * Its title says **move**, deliberately: `Test selection (n)` is already in this
     * header and means the picked set. Two counts side by side need telling apart.
     */
    function updateSelectAllButton() {
        const btn = document.getElementById("gdSelectAllBtn");
        if (!btn) return;

        const count = selection.count();
        btn.classList.toggle("btn-outline-primary", count > 0);
        btn.classList.toggle("btn-outline-secondary", count === 0);
        btn.innerHTML = count > 0
            ? '<i class="las la-times-circle"></i> Clear (' + count + ")"
            : '<i class="las la-object-group"></i> Select all';
        btn.title = count > 0
            ? "Clear the move selection"
            : "Select every node and connector, so they can be moved together";
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
        wiringChanged();
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
        path.setAttribute("d", GC.elbowPathD(GC.elbowPoints(from, to)));
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
                wiringChanged();
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
                wiringChanged();
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
        const node = {
            id: genId("n"),
            type: type,
            position: placementForNewNode(),
            data: defaultData(type),
        };
        state.nodes.push(node);
        wiringChanged();
        renderAllNodes();
        renderAllEdges();
        openProperties(node.id);
    }

    /**
     * Where a node being added should first appear.
     *
     * Under whichever node is selected, or under the whole drawing when none is — which is
     * where somebody adding a step to the end of a pipeline is looking. It replaces a fixed
     * grid (`x = 60 + (count % 5) * 230`) that wrapped back to the left margin every fifth
     * node and took no notice of what the drawing already looked like.
     *
     * In `auto` this position lasts a moment: the arrange that follows moves the node to
     * where its connections put it. Worth getting right anyway — a node that appeared at the
     * origin and jumped would read as a glitch.
     */
    function placementForNewNode() {
        const m = canvasMetrics();
        const below = findNode(state.selectedNodeId) || lowestNode();

        if (!below) return { x: m.marginX, y: m.marginY };

        const el = document.getElementById("node-" + below.id);
        const height = el ? el.getBoundingClientRect().height : m.rowGap;

        return {
            x: (below.position || {}).x || m.marginX,
            y: ((below.position || {}).y || m.marginY) + height + m.rowGap,
        };
    }

    /** The node furthest down the canvas, or null on an empty one. */
    function lowestNode() {
        return state.nodes.reduce(function (lowest, node) {
            const y = (node.position || {}).y || 0;
            return !lowest || y > ((lowest.position || {}).y || 0) ? node : lowest;
        }, null);
    }

    function deleteNode(nodeId) {
        // A live drag holds resolved elements for the connectors it is repainting. Those
        // are about to be detached, so the gesture is abandoned rather than left writing
        // attributes into a group that is no longer in the document.
        abandonDrag();
        if (selection) selection.abandon();
        state.nodes = state.nodes.filter(function (n) { return n.id !== nodeId; });
        // Its connectors go with it: an edge naming a node that is not there is refused
        // by the save, so leaving them would make the graph unsavable.
        state.edges = state.edges.filter(function (e) {
            return e.source !== nodeId && e.target !== nodeId;
        });
        delete state.picked[nodeId];
        if (state.selectedNodeId === nodeId) state.selectedNodeId = null;
        // Re-derived rather than one key deleted: a node takes its connectors with it, so a
        // stale connector id can outlive the node deletion that caused it.
        if (selection) selection.prune();
        wiringChanged();
        updatePickedCount();
        renderAllNodes();
        renderAllEdges();
    }

    function deleteEdge(edgeId) {
        abandonDrag();
        if (selection) selection.abandon();
        state.edges = state.edges.filter(function (e) { return e.id !== edgeId; });
        if (state.selectedEdgeId === edgeId) state.selectedEdgeId = null;
        if (selection) selection.prune();
        wiringChanged();
        renderAllEdges();
    }

    /**
     * Repaint the three marks a node can carry, from state.
     *
     * One element, three class toggles. All three of these used to be applied by rebuilding
     * every node and every connector on the canvas, which is a lot of DOM for a ring that
     * changed on one box — and it threw away hover state and the run dock's status
     * attributes on its way past.
     *
     * @param {string} nodeId
     */
    function updateNodeMarks(nodeId) {
        const el = document.getElementById("node-" + nodeId);
        if (!el) return;
        el.classList.toggle("gd-node-selected", state.selectedNodeId === nodeId);
        el.classList.toggle("gd-node-picked", !!state.picked[nodeId]);
        // Through the controller, not off `state.selection` directly: it decides *when* a
        // move mark is worth painting, and two answers to that would show a dashed box on
        // one repaint path and not on another.
        el.classList.toggle("gd-node-multi", !!(selection && selection.marksNode(nodeId)));
    }

    function selectNode(nodeId) {
        const previous = state.selectedNodeId;
        state.selectedNodeId = nodeId;
        if (previous && previous !== nodeId) updateNodeMarks(previous);
        updateNodeMarks(nodeId);
    }

    function togglePicked(nodeId) {
        if (state.picked[nodeId]) delete state.picked[nodeId];
        else state.picked[nodeId] = true;
        updatePickedCount();
        updateNodeMarks(nodeId);
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

        selectNode(nodeId);

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
            case "create_file": createFileFields(form, draft, node); break;
            case "download_file": downloadFileFields(form, draft, node); break;
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
            wiringChanged();
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
    // File formats, as the operator picks them. Mirrored from
    // `app/models/file_delivery/models.FILE_FORMATS` rather than fetched, like the node
    // vocabulary above it: four values that change roughly never, against a round trip on
    // every panel open. The server refuses anything else at save.
    const FILE_FORMATS = [
        { uuid: "csv", label: "CSV" },
        { uuid: "xlsx", label: "Excel (XLSX)" },
        { uuid: "txt", label: "Text" },
        { uuid: "parquet", label: "Parquet" },
    ];

    /**
     * A Create File node: whose rows, in what format, called what.
     *
     * The node picker is `otherNodes`, the same list a variable binding reads from — a
     * file is written out of exactly the kind of thing a binding reads, and offering a
     * different list would suggest otherwise.
     */
    function createFileFields(form, draft, node) {
        draft.data = draft.data || { source: "node", source_node: "", path: "" };

        form.appendChild(selectField(
            "Rows from", draft.data.source_node || "", otherNodes(node),
            function (v) { draft.data.source_node = v; },
            "The node whose output holds the rows. A SQL node's output is every matching "
            + "row, so the file holds the lot.",
        ));

        form.appendChild(textField(
            "Field", draft.data.path || "",
            function (v) { draft.data.path = v; },
            "Optional. A path into that node's output, such as rows — leave it empty to "
            + "write the whole output.",
        ));

        form.appendChild(selectField(
            "Format", draft.file_format || "csv", FILE_FORMATS,
            function (v) { draft.file_format = v; },
        ));

        form.appendChild(textField(
            "File name", draft.file_name || "",
            function (v) { draft.file_name = v; },
            "The extension is added for you. {{VARIABLE}} works here, so a nightly run "
            + "can write orders-{{RUN_DATE}} instead of overwriting one name.",
        ));

        attachHelp(form, "Connect failed: a node whose output holds no rows otherwise "
            + "stops the run with nothing drawn to explain it.");
    }

    /**
     * A Download File node: which file, and nothing else.
     *
     * No button text and no colour — a pipeline has no chat for one to appear in, and the
     * server refuses those fields on this canvas rather than accepting and ignoring them.
     * What this node produces is a link on its output, which an Email node can bind to.
     */
    function downloadFileFields(form, draft, node) {
        const makers = state.nodes
            .filter(function (n) { return n.type === "create_file" && n.id !== node.id; })
            .map(function (n) { return { uuid: n.id, label: labelOf(n) }; });

        if (!makers.length) {
            attachHelp(form, "There is no Create File node on this graph yet. This node "
                + "hands over a file another node writes, so add one first.");
            return;
        }

        form.appendChild(selectField(
            "File", draft.create_file_node || "", makers,
            function (v) { draft.create_file_node = v; },
            "The Create File node that writes it.",
        ));

        attachHelp(form, "Produces a download link on this node's output, for whoever owns "
            + "this graph. Bind an Email node's variable to it to send the file on. The "
            + "link lasts a day.");
    }

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
                const edge = {
                    id: e.id, source: e.source,
                    source_port: e.source_port || "default", target: e.target,
                };
                // Only when there are any, so a drawing nobody hand-routed saves exactly
                // the document it always did.
                if (waypointsOf(e).length) edge.waypoints = e.waypoints;
                return edge;
            }),
            // Whether this canvas arranges itself. Stored with the drawing because it is a
            // fact about the drawing. `GraphSaveRequest` allows extra keys for exactly
            // this, so nothing server-side had to change to keep it.
            layout: state.layout,
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
            const edge = {
                id: e.id || genId("e"), source: e.source,
                source_port: e.source_port || "default", target: e.target,
            };
            // Sanitised on the way in as well as on the way out: this loader is the one
            // place that decides what a connector *is* here, so a stored document with a
            // malformed bend draws as an unbent wire rather than throwing inside a render.
            const bends = readWaypoints(e.waypoints);
            if (bends.length) edge.waypoints = bends;
            return edge;
        });
        // Missing means auto, which is every graph saved before the canvas could arrange
        // itself — and those are the cluttered ones.
        state.layout = (data || {}).layout === "manual" ? "manual" : "auto";
        state.backEdges = {};
        state.picked = {};
        state.selectedNodeId = null;
        state.selectedEdgeId = null;
        // Emptied in place rather than replaced: the controller holds a reference to this
        // object. Cleared at all because the graph has just come from the server and ids
        // can differ — a surviving selection would hold ids that resolve to nothing, which
        // a group move would then try to drag.
        state.selection.nodes = {};
        state.selection.edges = {};
        updatePickedCount();
        updateSelectAllButton();
        renderAllNodes();
        renderAllEdges();
        updateLayoutButton();

        // Straight away rather than debounced: this is the arrange that makes a stored
        // drawing readable, and waiting would show the old positions first.
        requestLayout();
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
        edgesSvgEl = document.getElementById("gd-edges");
        edgesGroupEl = document.getElementById("gd-edges-group");
        wrapperEl = document.getElementById("gd-canvas-wrapper");
        paletteBodyEl = document.getElementById("gdPaletteBody");
        propertiesBodyEl = document.getElementById("gdPropertiesBody");

        state.vocabulary = readJsonScript("gdVocabulary", state.vocabulary);

        // Before `loadGraph`, which paints the selection as part of rendering.
        selection = window.GraphSelection.create({
            wrapperEl: wrapperEl,
            // The node layer, not the wrapper: it is the element whose box `node.position`
            // is measured against, so the box needs no border or scroll arithmetic.
            layerEl: canvasEl,
            edgesEl: edgesSvgEl,
            nodeElementId: function (id) { return "node-" + id; },
            edgeElementId: function (id) { return "edge-group-" + id; },
            selection: state.selection,
            getNodes: function () { return state.nodes; },
            getSelectableEdges: function () { return state.edges; },
            edgeRoute: edgeRoute,
            nodeWidth: function () { return canvasMetrics().stepWidth; },
            threshold: DRAG_THRESHOLD_PX,
            // No box while another gesture owns the mouse. `state.pending` is the one that
            // matters: a click on empty canvas is the only way out of an armed port, and
            // turning that press into a box would strand the user in connect mode.
            isBusy: function () {
                return !!(state.pending || state.connecting || state.reattaching || state.dragging);
            },
            classes: { node: "gd-node-multi", edge: "gd-edge-multi" },
            onSelectionChange: onSelectionChange,
            onGroupMoveBegin: onGroupMoveBegin,
            onGroupMoveFrame: onGroupMoveFrame,
            onGroupMoveEnd: onGroupMoveEnd,
            onEscape: function () {
                if (!state.pending) return false;
                state.pending = null;
                renderAllNodes();
                renderAllEdges();
                return true;
            },
            // This canvas has its own trailing-click flag, because a click on a node
            // finishes an armed connector. Kept in step with the module's rather than
            // duplicated, so the two cannot drift.
            onSwallowClick: function () {
                suppressNodeClick = true;
                setTimeout(function () { suppressNodeClick = false; }, 0);
            },
        });
        selection.attach();

        renderPalette();
        loadGraph(readJsonScript("gdGraphData", { nodes: [], edges: [] }));
        wireDock();

        // Clicking empty canvas cancels a half-drawn connection, which is the only way
        // out of one other than completing it.
        wrapperEl.addEventListener("click", function (e) {
            // The click that trails a box or a group move has the wrapper as its target,
            // and without this it would clear the selection the gesture just made — the
            // feature would appear to do nothing at all.
            if (selection.swallowedClick()) return;
            if (e.target === wrapperEl || e.target === canvasEl) {
                state.pending = null;
                state.selectedNodeId = null;
                state.selectedEdgeId = null;
                selection.clear();
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
        document.getElementById("gdTidyBtn").addEventListener("click", tidyUp);
        document.getElementById("gdSelectAllBtn").addEventListener("click", function () {
            if (selection.count()) selection.clear();
            else selection.selectAll();
            // The canvas has to hold focus for Ctrl+A and Escape to reach it, and somebody
            // who has just pressed this button is about to want both.
            wrapperEl.focus();
        });
        updateSelectAllButton();

        // A node's height depends on its font, and the layout stacks layers by measured
        // height — so a zoom or a font swap invalidates the cached spacing with it.
        window.addEventListener("resize", function () {
            canvasMetricsCache = null;
            syncLayout();
        });

        // A tab closed mid-run leaves a stream open otherwise.
        window.addEventListener("beforeunload", teardown);

        await loadOptions();
        // Redrawn once the pickers are known, so a node's preview can name the tool or
        // datasource it points at rather than its uuid. A longer preview can make a node
        // taller, so the arrangement is asked for again with it.
        renderAllNodes();
        renderAllEdges();
        syncLayout();

        if (opts.openRun) watch(opts.openRun);
    }

    return { init: init };
})();
