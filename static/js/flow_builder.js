/**
 * Flow Builder canvas — vanilla JS + inline SVG connectors, no external
 * graph library. Renders a node/edge graph, supports drag-to-move,
 * click-source-port-then-click-target connection creation, a right-side
 * properties panel per node type, and JSON save/load against the
 * FlowBuilderController routes.
 *
 * The stateless primitives — the Bezier maths, the port measurement, the escaping and
 * the id generator — live in `static/js/graph_canvas.js`, shared with the Graph
 * Designer's canvas. Everything that knows what a *flow* node means stays here:
 * NODE_TYPES, the properties panel, the option rows, save/load. `graph_canvas.js` must
 * therefore be loaded before this file (see templates/flow_builder/canvas.htm).
 */
window.FlowBuilder = (function () {
    "use strict";

    // The shared primitives. Aliased once so the call sites below read as they did
    // before the extraction.
    const GC = window.GraphCanvas;

    // The colour of a step's icon disc, keyed by **what the block does** rather than one
    // entry per type. Two blocks that are the same kind of thing should look it — Menu and
    // Dropdown are one question asked two ways, Create File and Download File are two
    // halves of handing somebody a file — and a reader scanning a canvas is reading the
    // shape of the flow, not fourteen individual hues.
    //
    // Every one of these carries a white glyph at 3:1 contrast or better, which is why
    // none of them is a light Bootstrap tint: `warning` and `info` at their stock values
    // leave the icon barely visible on the disc.
    const STEP_COLOURS = {
        edge_of_flow: "#6c757d",   // grey — a jump: the flow carries on, just elsewhere
        stop: "#b02a37",           // deep red — the flow ends here, and that is the whole point
                                   // of the block. Same red the Graph Designer's Failure node
                                   // uses, so "this run is over" is one colour on both canvases.
        entry: "#198754",          // green — the one block a reader starts at
        talk: "#0d6efd",           // blue — says or asks something
        branch: "#6610f2",         // indigo — offers a choice, so the canvas forks here
        decide: "#d97706",         // amber — forks without asking anybody
        think: "#6f42c1",          // purple — hands the turn to a model
        work: "#0b7285",           // deep teal — runs something else and waits for it
        send: "#c2410c",           // burnt orange — leaves the conversation entirely
        file: "#0f766e",           // teal — makes or hands over a file
    };

    const NODE_TYPES = {
        start: { label: "Start", icon: "la-play-circle", colour: STEP_COLOURS.entry, outputs: function () { return [{ port: "default", label: "" }]; } },
        if_else: { label: "If / Else", icon: "la-code-branch", colour: STEP_COLOURS.decide, outputs: function () {
            return [{ port: "true", label: "True" }, { port: "false", label: "False" }];
        } },
        goto: { label: "Goto", icon: "la-share", colour: STEP_COLOURS.edge_of_flow, outputs: function () { return []; } },
        menu: { label: "Menu / Buttons", icon: "la-list", colour: STEP_COLOURS.branch, outputs: function (data) {
            return (data.options || []).map(function (o) { return { port: o.id, label: o.label }; });
        } },
        dropdown: { label: "Dropdown", icon: "la-caret-square-down", colour: STEP_COLOURS.branch, outputs: function (data) {
            return (data.options || []).map(function (o) { return { port: o.id, label: o.label }; });
        } },
        ask_input: { label: "Ask for Input", icon: "la-keyboard", colour: STEP_COLOURS.talk, outputs: function () { return [{ port: "default", label: "" }]; } },
        send_message: { label: "Send Message", icon: "la-comment-dots", colour: STEP_COLOURS.talk, outputs: function () { return [{ port: "default", label: "" }]; } },
        ai_fallback: { label: "AI Fallback", icon: "la-robot", colour: STEP_COLOURS.think, outputs: function () { return [{ port: "default", label: "" }]; } },
        // Two ports on purpose. A graph that could not run must not leave by the same
        // edge as one that succeeded, or the flow says "all done" about work that never
        // happened. With no `error` edge drawn the engine signs off instead.
        send_email: { label: "Send Email", icon: "la-envelope", colour: STEP_COLOURS.send, outputs: function () {
            // `error` is for a refusal knowable now — no template, an unresolvable binding.
            // Whether the mail is later accepted by the relay is not knowable here and
            // routes nowhere; the delivery log answers that.
            return [
                { port: "default", label: "queued", kind: "ok" },
                { port: "error", label: "failed", kind: "error" },
            ];
        } },
        run_graph: { label: "Run Graph", icon: "la-project-diagram", colour: STEP_COLOURS.work, outputs: function () {
            return [
                { port: "default", label: "done", kind: "ok" },
                { port: "error", label: "failed", kind: "error" },
            ];
        } },
        // Runs another flow as one step of this one. Two ports for the reason Run Graph and
        // Send Email have two: a call that could not be made must not leave by the same edge
        // as one that succeeded, or the conversation carries on as though it had.
        run_flow: { label: "Run Flow", icon: "la-sitemap", colour: STEP_COLOURS.work, outputs: function () {
            return [
                { port: "default", label: "done", kind: "ok" },
                { port: "error", label: "failed", kind: "error" },
            ];
        } },
        // Writes rows to a file and hands it over. Two ports each, for the reason Send
        // Email, Run Graph and Run Flow all have two: a file that could not be written must
        // not leave by the same edge as one that was, or the conversation offers a download
        // of nothing. With no `error` edge drawn the engine signs off instead.
        create_file: { label: "Create File", icon: "la-file-export", colour: STEP_COLOURS.file, outputs: function () {
            return [
                { port: "default", label: "written", kind: "ok" },
                { port: "error", label: "failed", kind: "error" },
            ];
        } },
        download_file: { label: "Download File", icon: "la-download", colour: STEP_COLOURS.file, outputs: function () {
            return [
                { port: "default", label: "offered", kind: "ok" },
                { port: "error", label: "failed", kind: "error" },
            ];
        } },
        end: { label: "End Flow", icon: "la-flag-checkered", colour: STEP_COLOURS.stop, outputs: function () { return []; } },
    };

    // What a block with no registry entry is drawn as. A graph saved by a newer version of
    // this page can hold a type this one does not know, and the block still has to appear —
    // an operator needs to see that something is there before they can delete it.
    const UNKNOWN_TYPE = {
        label: "Unknown block", icon: "la-question-circle", colour: STEP_COLOURS.edge_of_flow,
        outputs: function () { return []; },
    };

    /** One node type's registry entry, or the placeholder for a type this page predates. */
    function metaFor(type) {
        return NODE_TYPES[type] || UNKNOWN_TYPE;
    }

    const SVG_NS = GC.SVG_NS;

    const state = {
        nodes: [],
        edges: [],
        selectedNodeId: null,
        selectedEdgeId: null,
        pending: null, // {nodeId, port}
        // The move selection: which blocks and connectors the next drag carries, and what
        // a box, Ctrl-click, Ctrl+A and Select all add to. Objects used as sets.
        //
        // Deliberately not `selectedNodeId`. That field means "the thing whose properties
        // panel is open" and there is at most one of those, ever. This is a different
        // question — "the things that move together" — with a different answer count, and
        // collapsing the two would mean either a panel opening for fifteen blocks or a
        // group move that can only ever carry one. Never saved: `serializeGraph` does not
        // read it.
        selection: { nodes: {}, edges: {} },
        dragging: null, // {nodeId, offsetX, offsetY}
        reattaching: null, // {edgeId, end: "source"|"target"}
        // "auto" or "manual" — who decides where the blocks sit.
        //
        // In `auto` the canvas arranges itself: on open, and again after anything that
        // changes the wiring. In `manual` the stored positions are used as they are.
        // Dragging a block switches to `manual` (an operator moving a block has said where
        // they want it), and Tidy up switches back.
        //
        // Recorded on the saved graph rather than inferred, because inference here is a
        // guess that is wrong for somebody: a hand-arranged canvas and an auto-arranged one
        // hold exactly the same kind of coordinates. **A graph with no `layout` key is
        // `auto`** — which is every flow saved before this existed, and is the point: those
        // are the cluttered ones.
        layout: "auto",
        // The back-edge indices the server reported for the current wiring, so a
        // connector that runs *up* the canvas is drawn as a return rather than as a step.
        backEdges: {},
        // True whenever the in-browser graph has changed since the last
        // successful Save/Reload — every edit here (node properties, adding/
        // deleting nodes or connectors, dragging) only touches this client-side
        // copy; nothing is live for visitors until the flow-level Save button
        // posts it to the server. Drives the "Unsaved changes" indicator.
        dirty: false,
    };

    let opts = {};
    let canvasEl, edgesSvgEl, edgesGroupEl, wrapperEl, paletteBodyEl, propertiesBodyEl;

    // The shared selection controller — the box, the multi-selection and the group move.
    // Built in `init`, because it needs the canvas elements.
    let selection = null;

    // The shared "+" menu that inserts a block into a connector. Also built in `init`.
    let insertMenu = null;

    // The shared connector runtime — the anchor cache, the edge-chrome repaint and the
    // return lane. Built in `init`, because it needs the canvas elements.
    let edges = null;

    // How far the ✕ and the + sit either side of a connector's midpoint. Both are r=8, so
    // 11 leaves a 6px gap: close enough to read as one pair of controls belonging to this
    // wire, far enough that neither is pressed by aiming at the other.
    const EDGE_BTN_GAP_PX = 11;

    /**
     * Generate a unique id for a new node/edge/option, scoped to this page load.
     * Delegates to the shared generator; `state.nextIdSeq` is no longer read.
     * @param {string} prefix - short type tag, e.g. "n", "e", "opt"
     * @returns {string}
     */
    const genId = GC.makeIdGenerator();

    /**
     * Flag the in-browser graph as having unsaved changes and reveal the
     * "Unsaved changes" badge.
     */
    function markDirty() {
        state.dirty = true;
        const badge = document.getElementById("fbUnsavedBadge");
        if (badge) badge.style.display = "";
    }

    /**
     * Clear the unsaved-changes flag and hide the "Unsaved changes" badge.
     */
    function clearDirty() {
        state.dirty = false;
        const badge = document.getElementById("fbUnsavedBadge");
        if (badge) badge.style.display = "none";
    }

    /**
     * Build the default `data` payload for a newly added node of the given type.
     * @param {string} type - one of the NODE_TYPES keys
     * @returns {object}
     */
    function defaultData(type) {
        switch (type) {
            case "if_else": return { variable_name: "", operator: "not_empty", compare_value: "" };
            case "goto": return { target_node_id: "" };
            case "menu": case "dropdown": return { prompt_text: "", options: [], variable_name: "" };
            case "ask_input": return { prompt_text: "", variable_name: "" };
            case "send_message": return { message_text: "" };
            case "run_graph": return { graph_id: "", variable_name: "" };
            // `inputs` maps a name the called flow reads to where its value comes from;
            // `outputs` maps a name that flow writes to the name to keep it under here.
            case "run_flow": return { flow_id: "", inputs: {}, outputs: {} };
        case "send_email": return {
            template_id: "", smtp_config_id: "",
            recipients: { to: [], cc: [], bcc: [] },
            variable_bindings: {}, variable_name: "",
        };
            case "ai_fallback": return {
                guardrails: "",
                prompt: "",
                context_source: "datasource",
                llm_mode: "in_built",
                llm_api_key_id: "",
                variable_name: "",
            };
            // `data` is where the rows come from — a block on this canvas by default,
            // because that is what "the previous block's output" means here. The variable
            // holds the finished file's path.
            case "create_file": return {
                file_format: "csv",
                file_name: "",
                data: { source: "block", block_id: "", name: "" },
                variable_name: "",
            };
            // No button until somebody asks for one: a block that drew something in a
            // visitor's chat by default would be putting words in the operator's mouth.
            case "download_file": return {
                create_file_node_id: [],
                show_button: false,
                button_text: "",
                button_colour: "#0d6efd",
                variable_name: "",
            };
            case "end": return { message_text: "" };
            default: return {};
        }
    }

    /**
     * Look up a node in state by id.
     * @param {string} id
     * @returns {object|null}
     */
    function findNode(id) {
        for (let i = 0; i < state.nodes.length; i++) {
            if (state.nodes[i].id === id) return state.nodes[i];
        }
        return null;
    }

    /**
     * Look up an edge in state by id.
     * @param {string} id
     * @returns {object|null}
     */
    function findEdge(id) {
        for (let i = 0; i < state.edges.length; i++) {
            if (state.edges[i].id === id) return state.edges[i];
        }
        return null;
    }

    /**
     * Build the one-line preview text shown inside a node's body, summarizing
     * its currently configured data.
     * @param {object} node
     * @returns {string}
     */
    function nodePreviewText(node) {
        const d = node.data || {};
        switch (node.type) {
            case "if_else": return (d.variable_name || "?") + " " + (d.operator || "") + " " + (d.compare_value || "");
            case "goto": return "→ " + (d.target_node_id || "(unset)");
            case "menu": case "dropdown": return d.prompt_text || "(no prompt)";
            case "ask_input": return d.prompt_text || "(no prompt)";
            case "send_message": return d.message_text || "(empty message)";
            case "run_graph": return d.graph_id ? "Runs a saved graph" : "(no graph chosen)";
            case "run_flow": {
                const called = (opts.flows || []).find(function (f) { return f.id === d.flow_id; });
                if (!called) return d.flow_id ? "Runs another flow" : "(no flow chosen)";
                const brought = Object.keys(d.outputs || {}).filter(function (name) {
                    return String(d.outputs[name] || "").trim() !== "";
                });
                return "▸ " + called.label + (brought.length ? " → " + brought.length + " value(s)" : "");
            }
            case "ai_fallback":
                const ctxLabel = { datasource: "attached datasource", knowledge_base: "knowledge base", prompt: "prompt only" }[d.context_source] || "attached datasource";
                return "AI answers using " + ctxLabel +
                    (d.variable_name ? " → " + d.variable_name : "");
            case "create_file": {
                const src = d.data || {};
                const from = src.source === "variable"
                    ? (src.name ? "{{" + src.name + "}}" : "(no variable chosen)")
                    : (src.block_id ? blockLabelById(src.block_id) : "(no data chosen)");
                return String(d.file_format || "csv").toUpperCase() + " from " + from +
                    (d.variable_name ? "\n→ " + d.variable_name : "");
            }
            case "download_file": {
                const ids = downloadSourceIds(d);
                const of = ids.length
                    ? ids.map(blockLabelById).join(", ")
                    : "(no file chosen)";
                // Whether a visitor sees anything is the one thing about this block worth
                // knowing without opening it, so it goes on the box.
                return "Offers " + of + "\n" +
                    (d.show_button
                        ? "button: " + (d.button_text || "Download file")
                        : "link into " + (d.variable_name || "a variable"));
            }
            case "end": return d.message_text ? "Ends flow: " + d.message_text : "Ends the flow (no closing message)";
            default: return "";
        }
    }

    /**
     * A block's name for a preview line or a dropdown.
     *
     * An operator-given name (`data.label`, set from the "Name this block" field every
     * type carries) wins outright when there is one — that is the whole point of it. With
     * none, falls back to type plus id: two Run Graph blocks on one canvas are told apart
     * only by the id, and an id on its own says nothing about what the block is.
     *
     * @param {string} nodeId
     * @returns {string}
     */
    function blockLabelById(nodeId) {
        const node = findNode(nodeId);
        if (!node) return "(deleted block)";
        const custom = String((node.data || {}).label || "").trim();
        if (custom) return custom;
        const meta = NODE_TYPES[node.type];
        // No name given: the id is the only thing left to tell two blocks of the same
        // type apart, so it stays rather than being dropped for a prettier string that
        // could name the wrong box.
        return (meta ? meta.label : node.type) + " (" + node.id + ")";
    }

    /**
     * A Download File block's Create File sources, always read as an array.
     *
     * Saved data may still be the single legacy string — this block used to name exactly
     * one Create File block. Reading both shapes here means an old flow keeps working
     * without a migration, and every caller sees one consistent shape.
     *
     * @param {object} data - a download_file block's `data`
     * @returns {Array<string>}
     */
    function downloadSourceIds(data) {
        const raw = data.create_file_node_id;
        if (Array.isArray(raw)) return raw.filter(Boolean);
        return raw ? [raw] : [];
    }

    // ---------------------------------------------------------------
    // Rendering — nodes
    // ---------------------------------------------------------------

    /**
     * Clear the canvas and render every node currently in state.
     */
    function renderAllNodes() {
        canvasEl.innerHTML = "";
        state.nodes.forEach(renderNode);
    }

    const OPTION_NODE_TYPES = { menu: true, dropdown: true };

    /**
     * Build the branch bus under a step: one labelled pill per outgoing connector.
     *
     * This is where a Menu's options and a two-port block's `written` / `failed` become the
     * same thing, which is the point — they always *were* the same thing, drawn two
     * different ways. The reference canvas does exactly this with its Branch node's
     * `call originator equals …` / `Else`.
     *
     * A pill **is** the output port: it carries the `data-port` attribute that
     * `onSourcePortClick`, `edgeRoute` and the reattach drop-target check all key off, so
     * every one of those keeps working with no change. What used to be a 12px dot is now a
     * target the width of a word, which is the incidental part that makes connecting
     * blocks much easier.
     *
     * @param {Array<{port: string, label: string}>} outputs
     * @returns {string}
     */
    function branchPillsHtml(outputs) {
        return (outputs || []).map(function (o) {
            return (
                '<span class="fb-step-pill flow-node-port-out" data-port="' +
                escapeAttr(o.port) + '"' +
                (o.kind ? ' data-port-kind="' + escapeAttr(o.kind) + '"' : "") +
                ' title="' +
                escapeAttr((o.label || "(unlabeled)") + " — click to start a connector from here") +
                '">' +
                escapeHtml(o.label || "(unlabeled)") +
                "</span>"
            );
        }).join("");
    }

    /**
     * Render a single node into the canvas: builds its DOM element, wires up
     * drag/select/port event listeners, and applies selection styling.
     * @param {object} node
     */
    function renderNode(node) {
        const meta = metaFor(node.type);
        const data = node.data || {};
        const el = document.createElement("div");

        // `flow-node` is kept alongside the new class name because it is the hook four
        // other things already query for — the reattach drop-target test, the palette's
        // outside-click handler, `deselectAll` — and renaming it would mean finding every
        // one of those rather than restyling one block.
        el.className = "flow-node fb-step" +
            (state.selectedNodeId === node.id ? " fb-node-selected" : "") +
            // Read from state, never only set on the element at gesture time:
            // `renderAllNodes` rebuilds this layer from scratch, so a class applied by a
            // click would vanish the next time anything re-rendered.
            (selection && selection.marksNode(node.id) ? " fb-node-multi" : "");
        el.id = "node-" + node.id;
        el.dataset.nodeId = node.id;
        el.style.left = (node.position.x || 0) + "px";
        el.style.top = (node.position.y || 0) + "px";

        // A Menu's prompt is what it *says*; every other block's summary comes from its
        // settings. Its options are not in the summary any more — they are the pills.
        const summary = OPTION_NODE_TYPES[node.type]
            ? (data.prompt_text || "(no prompt)")
            : nodePreviewText(node);
        const outputs = meta.outputs(data);

        // An operator-given name, when there is one, takes the title — four Create File
        // blocks that otherwise all say "Create File" are how this got asked for. The type
        // moves down into the summary line instead of disappearing, so the box still says
        // what it *is* as well as what it is *for*.
        const customLabel = String(data.label || "").trim();
        const titleText = customLabel || meta.label;
        const subText = customLabel ? meta.label + " — " + summary : summary;

        // One plain dot for a block with a single way out, a labelled pill each when there
        // is a choice to make. A single output needs no label — "default" told a reader
        // nothing that the line leaving the block did not.
        const exitHtml = outputs.length === 1
            ? '<span class="fb-step-exit flow-node-port-out" data-port="' +
                escapeAttr(outputs[0].port) + '" title="Click to start a connector from here"></span>'
            : '<div class="fb-step-branches">' + branchPillsHtml(outputs) + "</div>";

        el.innerHTML =
            (node.type === "start"
                ? ""
                : '<span class="fb-step-entry" data-port-role="in" title="Connect a source here"></span>') +
            '<div class="fb-step-disc" style="background:' + escapeAttr(meta.colour) + '">' +
            '<i class="las ' + escapeAttr(meta.icon) + '"></i>' +
            "</div>" +
            '<div class="fb-step-actions">' +
            '<button type="button" class="fb-step-action" data-role="edit-node" title="Edit"><i class="las la-edit"></i></button>' +
            (node.type === "start"
                ? ""
                : '<button type="button" class="fb-step-action fb-step-action-danger" data-role="delete-node" title="Delete"><i class="las la-trash"></i></button>') +
            "</div>" +
            '<div class="fb-step-title">' + escapeHtml(titleText) + "</div>" +
            '<div class="fb-step-sub">' + escapeHtml(subText) + "</div>" +
            (outputs.length ? exitHtml : "") +
            // Where a Goto's return jump leaves from. Deliberately not an output port: a
            // Goto's destination is a setting, so there is nothing here to connect and
            // nothing to drag — this is only somewhere for the dashed line to start.
            (node.type === "goto" ? '<span class="fb-step-exit fb-step-exit-jump" data-jump-exit></span>' : "");

        canvasEl.appendChild(el);

        // The whole step is the drag handle, and a press that never moved is a click that
        // opens the properties panel — see `onDragEnd`. That replaces the old split, where
        // the header dragged and the body selected: on a step this size there is no header
        // to aim at, and "press and move to move it, press and release to open it" is what
        // every other canvas of this kind does.
        el.addEventListener("mousedown", function (e) {
            // Not from a pill, a dot, or one of the two action buttons: each of those is a
            // control with its own click, and starting a drag from one would eat it.
            if (e.target.closest(".fb-step-pill, .fb-step-exit, .fb-step-entry, .fb-step-action")) return;
            // Offered to the selection first. It takes the press only for a Ctrl-click or a
            // real group move; anything else falls through to the drag this canvas has
            // always done, with its click and modifier behaviour untouched.
            if (selection && selection.beginNodePress(node.id, e)) return;
            startDrag(node.id, e);
        });
        el.querySelector('[data-role="edit-node"]').addEventListener("click", function (e) {
            e.stopPropagation();
            selectNode(node.id);
        });
        const deleteBtn = el.querySelector('[data-role="delete-node"]');
        if (deleteBtn) {
            deleteBtn.addEventListener("click", function (e) {
                e.stopPropagation();
                deleteNode(node.id);
            });
        }
        const inPort = el.querySelector('[data-port-role="in"]');
        if (inPort) {
            inPort.addEventListener("click", function (e) {
                e.stopPropagation();
                onTargetPortClick(node.id);
            });
        }
        el.querySelectorAll(".flow-node-port-out").forEach(function (portEl) {
            portEl.addEventListener("click", function (e) {
                e.stopPropagation();
                onSourcePortClick(node.id, portEl.dataset.port);
            });
        });

        updateNodeSelectionClass(node.id);
    }

    let canvasMetricsCache = null;

    /**
     * The layout's spacing, in pixels, per the stylesheet.
     *
     * Read from CSS rather than restated here so the two cannot drift — the same reasoning
     * the old `portMetrics` carried, applied to the numbers that now matter. A step's width
     * is a styling decision (`--fb-step-w`), and the layout has to know what it came out as
     * to turn a column number into an x. Hard-coding it here would mean a step restyled
     * wider silently started overlapping its neighbour, with nothing about the symptom
     * pointing at the stylesheet.
     *
     * Cached: every node in a pass resolves to the same numbers.
     *
     * @returns {{stepWidth: number, columnGap: number, rowGap: number, marginX: number, marginY: number}}
     */
    function canvasMetrics() {
        if (canvasMetricsCache) return canvasMetricsCache;

        const style = window.getComputedStyle(canvasEl);
        const read = function (name, fallback) {
            const value = parseFloat(style.getPropertyValue(name));
            return isFinite(value) && value > 0 ? value : fallback;
        };

        canvasMetricsCache = {
            stepWidth: read("--fb-step-w", 168),
            columnGap: read("--fb-col-gap", 44),
            rowGap: read("--fb-row-gap", 58),
            marginX: read("--fb-margin-x", 48),
            marginY: read("--fb-margin-y", 32),
        };
        return canvasMetricsCache;
    }

    // ---------------------------------------------------------------
    // Layout — asking the server where the blocks go
    //
    // The arithmetic is in app/services/canvas_layout/layout_service.py, not here, because
    // layout is the part of a drawing that can be wrong without looking wrong and this
    // repository can only test Python. What is left on this side is the half that needs a
    // browser: turning a column into an x, and stacking each layer below the *measured*
    // height of the one above it — a step with four branch pills is taller than one with
    // none, and only the browser knows by how much.
    // ---------------------------------------------------------------

    // Enough for the last of a burst of edits to settle. Adding three blocks in a row
    // should arrange once, not three times.
    const LAYOUT_DEBOUNCE_MS = 120;

    let layoutTimer = null;

    /**
     * Note that the wiring changed: the flow is now unsaved, and the canvas needs
     * re-arranging.
     *
     * One function at every structural call site rather than `markDirty()` plus a
     * `syncLayout()` each, so a new kind of edit cannot be added that dirties the flow and
     * forgets to re-arrange it — which would leave a block sitting where its old
     * connections put it.
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
     * **Sent in both modes, applied in one.** Even a hand-arranged canvas needs to know
     * which connectors run backwards, because that is what decides whether a line is drawn
     * as a step down or as a return round the side — and a Goto's jump has to be drawn
     * either way. So the positions are used only in `auto`, and the back edges always.
     *
     * Only ids, types and ends are sent. The block settings are not the layout's business
     * and some of them are long — an AI Fallback's prompt, a knowledge base's text — and
     * this runs after every edit.
     */
    function requestLayout() {
        if (!opts.layoutUrl) return;

        const edges = drawableEdges();

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
                // Indices into the array that was *posted*, mapped back to connector ids
                // while that array is still in hand. Keeping the indices instead would go
                // stale the moment the next edit reordered anything.
                state.backEdges = {};
                (data.back_edges || []).forEach(function (index) {
                    const edge = edges[index];
                    if (edge) state.backEdges[edge.id] = true;
                });

                if (state.layout === "auto") applyLayout(data.positions);
                else renderAllEdges();
            })
            .catch(function () {
                // The blocks stay where they are. Nothing is lost and nothing is blank —
                // the canvas is holding unsaved work, and an arrangement is the least
                // important thing on it.
                noteLayoutFailure();
            });
    }

    /**
     * Say why something the canvas was asked to do did not happen.
     *
     * @param {string} [message] - defaults to the layout request's own failure
     */
    function noteLayoutFailure(message) {
        const target = document.getElementById(opts.responseTargetId);
        if (!target) return;

        target.innerHTML =
            '<div class="alert alert-warning py-2 mb-0">' +
            escapeHtml(message || "Could not tidy the canvas just now. " +
                "The blocks have been left where they were.") +
            "</div>";
    }

    /**
     * The widest thing any block draws, measured rather than assumed.
     *
     * The block itself is a fixed `--fb-step-w`, which is what keeps every disc centred
     * over the same point in its column. Its branch pills are not: they sit side by side
     * and overflow the block evenly on both sides, so a Menu whose options read
     * "Tell me about the Proposal" is far wider than the disc above it. Columns are spaced
     * by this number so that a fractional column — the mean of its parents — still lands
     * somewhere nothing else has claimed.
     *
     * @returns {number} px, 0 when nothing has been rendered yet
     */
    function widestStep() {
        let widest = 0;

        state.nodes.forEach(function (node) {
            const el = document.getElementById("node-" + node.id);
            if (!el) return;
            widest = Math.max(widest, el.getBoundingClientRect().width);
            const pills = el.querySelector(".fb-step-branches");
            if (pills) widest = Math.max(widest, pills.getBoundingClientRect().width);
        });

        return widest;
    }

    /**
     * Put every block where the server said, and redraw.
     *
     * Two passes, because the second needs the first to have happened in the document:
     *
     *   1. **x from the column**, and every block rendered.
     *   2. **y from the layer**, each one starting below the tallest block of the layer
     *      above. Uniform row heights would either waste a band of empty canvas under
     *      every short row or let a four-pill Menu overlap whatever is beneath it.
     *
     * An auto-arrange does **not** mark the flow unsaved, and that is deliberate: a
     * position changes nothing about what a visitor experiences, and the unsaved badge is
     * a warning that the *behaviour* on screen is not the behaviour that is live. Flagging
     * every open of every flow would make the badge mean nothing.
     *
     * @param {object} positions - {nodeId: {layer, column}}
     */
    function applyLayout(positions) {
        if (!positions) return;

        const m = canvasMetrics();
        const rows = {};

        // Render before measuring. A block is `--fb-step-w` wide, but its branch pills sit
        // side by side and are allowed to carry their row wider than that — so the width
        // that decides how far apart columns go is the widest row on the canvas, not the
        // width of the block above it.
        renderAllNodes();

        const widest = Math.max(m.stepWidth, widestStep());
        const pitch = widest + m.columnGap;

        // Half of a wide row hangs off each side of its block, so the first column has to
        // start far enough in for the left half to have somewhere to be. Without this a
        // Menu in column 0 would have its pills clipped by the edge of the canvas.
        const leftPad = Math.max(m.marginX, Math.ceil((widest - m.stepWidth) / 2) + 8);

        state.nodes.forEach(function (node) {
            const place = positions[node.id];

            // No place for this block means the server did not see it — a save is in
            // flight, or it was added while the request was out. It keeps where it is and
            // the next pass picks it up.
            if (!place) return;

            node.position.x = Math.round(leftPad + place.column * pitch);
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

    // The canvas and its connector layer were a fixed 3000 × 2000 in the stylesheet, which
    // was enough while flows ran left to right. Top-down they do not fit: fourteen blocks
    // at roughly 150px a layer is already past 2000, and a block placed beyond the edge
    // simply could not be scrolled to — the wrapper only scrolls as far as its content.
    const CANVAS_MIN_WIDTH = 3000;
    const CANVAS_MIN_HEIGHT = 2000;

    /**
     * Grow the canvas and the connector layer to hold the drawing.
     *
     * Both, and to the same size: the connectors are an SVG of its own, and one sized
     * smaller than the canvas would clip every line past its edge rather than fail
     * visibly.
     */
    function fitCanvas() {
        const m = canvasMetrics();
        let right = 0;
        let bottom = 0;

        state.nodes.forEach(function (node) {
            const el = document.getElementById("node-" + node.id);
            const box = el ? el.getBoundingClientRect() : { width: m.stepWidth, height: 0 };
            right = Math.max(right, (node.position.x || 0) + box.width);
            bottom = Math.max(bottom, (node.position.y || 0) + box.height);
        });

        // Past the rightmost block by the width of the return lane, so a Goto's jump has
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
     * Marks the flow unsaved, unlike an arrange that happens on its own — pressing this
     * changes what gets *stored* (the canvas stops being manual), which is an edit even
     * though the blocks are all that visibly move.
     */
    function tidyUp() {
        // Tidy up already means "throw away my arrangement and let the canvas decide". A
        // hand-routed wire only means anything against a known arrangement, so leaving the
        // bends would give a drawing that is neither arranged nor hand-drawn, with no button
        // that fixes it. But it destroys work somebody did by hand, so it asks — with the
        // count, so the question is answerable.
        const bent = state.edges.filter(function (edge) { return GC.waypointsOf(edge).length; });
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

    /**
     * Show on the Tidy up button whether the canvas is currently arranging itself.
     *
     * Highlighted while the canvas is manual, which is the only time the button has
     * anything to offer — in `auto` it is already tidy and the button is a plain
     * re-arrange.
     */
    function updateLayoutButton() {
        const button = document.getElementById("fbTidyBtn");
        if (!button) return;

        const manual = state.layout === "manual";
        button.classList.toggle("btn-outline-primary", manual);
        button.classList.toggle("btn-outline-secondary", !manual);
        button.title = manual
            ? "Blocks are where you put them. Arrange the canvas automatically again."
            : "Arrange the canvas again";
    }

    /**
     * Toggle the selected-node CSS class on a node's DOM element to match
     * state.selectedNodeId.
     * @param {string} nodeId
     */
    function updateNodeSelectionClass(nodeId) {
        const el = document.getElementById("node-" + nodeId);
        if (!el) return;
        el.classList.toggle("fb-node-selected", state.selectedNodeId === nodeId);
    }

    // ---------------------------------------------------------------
    // Rendering — edges
    // ---------------------------------------------------------------

    /**
     * Compute the canvas-relative center point of a node (or one of its
     * ports), used as a Bezier endpoint when drawing edges.
     * @param {string} nodeId
     * @param {string|null} portSelector - CSS selector for the port within the node, or null for the node itself
     * @returns {{x: number, y: number}|null}
     */
    // -----------------------------------------------------------------
    // Anchors, and why a drag does not measure them
    //
    // `GC.portAnchor` finds a port by measuring it. That is right when the canvas is still
    // and wrong inside a drag loop: the frame has just written `style.left`, so every rect
    // read after it forces the browser to lay the whole canvas out again — once per port,
    // per connector, per mousemove, and mousemove fires faster than the screen repaints.
    //
    // A drag therefore measures each port it needs exactly once and keeps the result as an
    // **offset from its node's stored position**. After that an anchor is two additions.
    //
    // Taking that offset as `anchor - node.position`, both sampled at the same instant, is
    // what makes it exact rather than nearly right. Two constant discrepancies sit between
    // those two spaces — the wrapper's 1px border (anchors are measured from its border
    // box, `node.position` is against the layer's content box) and the ports' half-pixel
    // placement (`left: 50%; margin-left: -5.5px`) — and both land inside the offset,
    // where neither has to be named nor maintained if the stylesheet changes.
    // -----------------------------------------------------------------


    // A connector is a run of right-angled corner points from the source's exit down to
    // the target's entry dot, not a curve. The points are returned rather than only the
    // path string for the same reason the Bezier control points were: placing the ✕ at the
    // middle of a connector, or a drag handle a fifth of the way along it, needs the line's
    // definition and not its rendering.

    /**
     * The corner points one connector runs through, or null if either end is missing.
     *
     * A connector the server reported as a **back edge** — a Goto's return, or any loop —
     * takes the return lane instead of a downward step. There is no downward step it could
     * take: its target is above it, which is what made it a back edge.
     *
     * @param {object} edge
     * @returns {Array<{x: number, y: number}>|null}
     */
    function edgeRoute(edge) {
        // A Goto's jump leaves from its own dot — which is not an output port, because
        // there is nothing to connect there — so it is found by a different selector. In
        // `sourceSelectorFor`, because the bend gesture has to compute against exactly the
        // line that is on screen.
        const from = edges.portAnchor(edge.source, sourceSelectorFor(edge)) || edges.portAnchor(edge.source, null);
        const to = edges.portAnchor(edge.target, '[data-port-role="in"]')
            || edges.portAnchor(edge.target, null);

        if (!from || !to) return null;

        const bends = GC.waypointsOf(edge);

        // A hand-placed bend beats the return lane. The lane is a fallback — `elbowPoints`
        // says why it exists: there is no way down to a target that is up. A waypoint is a
        // person saying where the wire goes, and a person's decision beats a fallback. The
        // failure mode of the other order is worse: you bend a wire, nothing visible
        // happens, and you bend it again.
        return (state.backEdges[edge.id] && !bends.length)
            ? GC.backEdgePoints(from, to, edges.returnLaneX())
            : GC.waypointPoints(from, to, bends);
    }

    /**
     * The connectors to draw: the ones an operator wired, plus every Goto's return jump.
     *
     * A Goto is not connected by an edge — it names its target in its settings — so until
     * now the jump was drawn nowhere at all, and a flow that loops back to its own menu
     * looked like a flow that stopped. It is a *derived* connector: dashed, not selectable,
     * and with no ✕ or handles, because the way to change it is to edit the Goto block. A
     * Goto naming a block that has been deleted contributes nothing, which is the canvas
     * agreeing with the validator that refuses to save it.
     *
     * @returns {Array<object>}
     */
    function drawableEdges() {
        const jumps = [];

        state.nodes.forEach(function (node) {
            if (node.type !== "goto") return;

            const target = String((node.data || {}).target_node_id || "");

            if (target && findNode(target)) {
                jumps.push({
                    id: "goto-" + node.id,
                    source: node.id,
                    source_port: "goto",
                    target: target,
                    derived: true,
                });
            }
        });

        return state.edges.concat(jumps);
    }

    /**
     * Clear the edges SVG group and render every edge currently in state.
     */
    function renderAllEdges() {
        edgesGroupEl.innerHTML = "";
        drawableEdges().forEach(renderEdge);
    }

    /**
     * Build the small × button rendered at an edge's midpoint, used to
     * delete that edge.
     * @param {string} edgeId
     * @param {{x: number, y: number}} pt
     * @returns {SVGElement}
     */
    function buildDeleteButton(edgeId, pt) {
        const g = document.createElementNS(SVG_NS, "g");
        g.setAttribute("class", "fb-edge-delete-btn");
        g.setAttribute("transform", "translate(" + pt.x + "," + pt.y + ")");
        const circle = document.createElementNS(SVG_NS, "circle");
        circle.setAttribute("r", "8");
        const text = document.createElementNS(SVG_NS, "text");
        text.setAttribute("text-anchor", "middle");
        text.setAttribute("dy", "3");
        text.textContent = "×";
        g.appendChild(circle);
        g.appendChild(text);
        g.addEventListener("mousedown", function (e) { e.stopPropagation(); });
        g.addEventListener("click", function (e) {
            e.stopPropagation();
            deleteEdge(edgeId);
        });
        return g;
    }

    /**
     * Build the "+" that inserts a block into this connector.
     *
     * The `mousedown` stopper is not decoration: without it the press would reach the
     * wire's own hit path underneath and start a bend, so pressing the + would put a
     * corner in the line as a side effect of opening a menu.
     *
     * @param {string} edgeId
     * @param {{x: number, y: number}} pt
     * @returns {SVGElement}
     */
    function buildInsertButton(edgeId, pt) {
        const g = document.createElementNS(SVG_NS, "g");
        g.setAttribute("class", "fb-edge-insert-btn");
        g.setAttribute("transform", "translate(" + pt.x + "," + pt.y + ")");
        g.setAttribute("role", "button");
        g.setAttribute("tabindex", "-1");

        const title = document.createElementNS(SVG_NS, "title");
        title.textContent = "Insert a block here";
        g.appendChild(title);

        const circle = document.createElementNS(SVG_NS, "circle");
        circle.setAttribute("r", "8");
        const text = document.createElementNS(SVG_NS, "text");
        text.setAttribute("text-anchor", "middle");
        text.setAttribute("dy", "3");
        text.textContent = "+";
        g.appendChild(circle);
        g.appendChild(text);

        g.addEventListener("mousedown", function (e) { e.stopPropagation(); });
        g.addEventListener("click", function (e) {
            e.stopPropagation();
            if (insertMenu) insertMenu.openFor(edgeId, e.clientX, e.clientY);
        });
        return g;
    }

    /**
     * Build the small circle that marks one hand-placed bend.
     *
     * Drawn at the **stored** point rather than on the line. `elbowPathD` pulls the stroke
     * up to the corner radius inside every corner, so a handle drawn on the stroke would
     * sit visibly off the pixel the user put it at.
     *
     * @param {string} edgeId
     * @param {number} index
     * @param {{x: number, y: number}} pt
     * @returns {SVGElement}
     */
    function buildWaypointHandle(edgeId, index, pt) {
        const circle = document.createElementNS(SVG_NS, "circle");
        circle.setAttribute("class", "fb-edge-waypoint");
        circle.setAttribute("data-waypoint", String(index));
        circle.setAttribute("cx", pt.x);
        circle.setAttribute("cy", pt.y);
        circle.setAttribute("r", "4");
        return circle;
    }

    /**
     * Build a draggable circle handle at one end of an edge, used to start a
     * reattach drag.
     * @param {string} edgeId
     * @param {"source"|"target"} end
     * @param {{x: number, y: number}} pt
     * @returns {SVGElement}
     */
    function buildEndHandle(edgeId, end, pt) {
        const circle = document.createElementNS(SVG_NS, "circle");
        circle.setAttribute("class", "fb-edge-handle fb-edge-handle-" + end);
        circle.setAttribute("cx", pt.x);
        circle.setAttribute("cy", pt.y);
        circle.setAttribute("r", "5");
        circle.addEventListener("mousedown", function (e) {
            e.preventDefault();
            e.stopPropagation();
            startEdgeReattach(edgeId, end);
        });
        return circle;
    }

    /**
     * Render a single edge into the edges group: its path plus its delete
     * button and reattach handles.
     * @param {object} edge
     */
    function renderEdge(edge) {
        const group = document.createElementNS(SVG_NS, "g");
        // The selected class goes on the *group* as well as the path. The stylesheet needs
        // to reveal this connector's ✕ and handles, which are siblings of the path — and
        // `:has()` would do it without this, but a class the renderer sets is one less
        // browser-support question on a page an operator has to be able to use.
        group.setAttribute(
            "class",
            "flow-edge-group" +
            (edge.derived ? " fb-edge-derived" : "") +
            (state.selectedEdgeId === edge.id ? " fb-edge-group-selected" : "") +
            (selection && selection.marksEdge(edge.id) ? " fb-edge-multi" : ""),
        );
        group.setAttribute("id", "edge-group-" + edge.id);

        const route = edgeRoute(edge);
        const d = route ? GC.elbowPathD(route) : "";

        // An invisible fat path under the visible one, purely to be hovered and clicked.
        // The line itself is 2px, and the ✕ and the two handles only appear on hover — so
        // without this, revealing them would mean landing the cursor inside two pixels,
        // which is not a control anybody can use. It carries no class the stylesheet
        // colours, so it is never seen; `pointer-events` is what it is for.
        //
        // Built whether or not a route could be measured. It used to be conditional, and
        // an edge rendered before both its ends were in the DOM got a group with a visible
        // path and nothing else — no hit path, no ✕, no handles — which
        // `updateEdgeGeometry` could never add later. Such a connector could not be
        // selected, deleted or reattached for the rest of the session. With an empty `d`
        // these are unhittable and unseen, and the next geometry update fills them in.
        if (!edge.derived) {
            const hit = document.createElementNS(SVG_NS, "path");
            hit.setAttribute("class", "fb-edge-hit");
            hit.setAttribute("d", d);
            hit.addEventListener("click", function () {
                // Not the click that trailed a bend. `startBend` arms the same one-tick
                // flag a drag does, so a wire that was just routed does not also open its
                // properties panel.
                if (edges.swallowedEdgeClick()) return;
                selectEdge(edge.id);
            });
            // Press and move to bend it; press and release to select it. The same split
            // `wireOutPort` documents for a port, and the reason the click listener above
            // stays: under the threshold nothing happens here and the click does its job.
            hit.addEventListener("mousedown", function (e) { edges.startBend(edge.id, e); });
            // Straightening it. The only affordance the Graph Designer can offer at all —
            // it has no connector properties panel — so it is the one both canvases share.
            hit.addEventListener("dblclick", function (e) {
                e.stopPropagation();
                edges.straightenEdge(edge.id);
            });
            group.appendChild(hit);
        }

        const path = document.createElementNS(SVG_NS, "path");
        path.setAttribute("id", "edge-" + edge.id);
        path.setAttribute("d", d);
        // A connector leaving a failure port is drawn as one. Grey would have said the
        // two ways out of a Create File block lead to the same kind of place; they do not,
        // and which one a reader is following is the question they came to the canvas with.
        if ((edge.source_port || "default") === "error") path.classList.add("fb-edge-error");
        if (state.selectedEdgeId === edge.id) path.classList.add("fb-edge-selected");
        if (!edge.derived) {
            path.addEventListener("click", function () { selectEdge(edge.id); });
        }
        group.appendChild(path);

        // The bend handles, before the reattach handles rather than after, so that where the
        // two land close together on a heavily bent wire the reattach handle paints on top
        // and wins the press. Reattaching is the more destructive of the two and should not
        // be taken by accident. Smaller than a reattach handle for the same reason.
        if (!edge.derived) {
            GC.waypointsOf(edge).forEach(function (bend, index) {
                group.appendChild(buildWaypointHandle(edge.id, index, bend));
            });
        }

        // A derived connector gets no chrome: a Goto's jump is changed by editing the Goto
        // block, so a ✕ offering to delete it would be offering something that cannot
        // happen.
        if (!edge.derived) {
            // Parked at the origin when there is no route yet, for the reason above: they
            // are hidden until the group is hovered, and a group with an empty hit path
            // cannot be hovered, so they stay out of sight until the geometry arrives.
            const at = function (t) { return route ? GC.pointAlongPolyline(route, t) : { x: 0, y: 0 }; };
            const mid = at(0.5);
            group.appendChild(buildDeleteButton(edge.id, { x: mid.x - EDGE_BTN_GAP_PX, y: mid.y }));
            group.appendChild(buildInsertButton(edge.id, { x: mid.x + EDGE_BTN_GAP_PX, y: mid.y }));
            group.appendChild(buildEndHandle(edge.id, "source", at(0.15)));
            group.appendChild(buildEndHandle(edge.id, "target", at(0.85)));
        }

        edgesGroupEl.appendChild(group);
    }

    // ---------------------------------------------------------------
    // Dragging
    // ---------------------------------------------------------------

    /**
     * Begin dragging a node: record the cursor offset from the node's
     * top-left corner and attach the move/up listeners that drive the drag.
     * @param {string} nodeId
     * @param {MouseEvent} e
     */
    // How far the cursor has to travel before a press counts as a drag rather than a click.
    // Not zero: a click always emits a mousemove or two, and without a threshold every
    // click on a step would move it a pixel, mark the flow dirty, and — worse — flip the
    // canvas to manual layout, so simply opening a block's properties would stop it ever
    // arranging itself again.
    const DRAG_THRESHOLD = 3;

    // Hand-routing a connector — see "Bending a connector" below.
    //
    // Four bends, and the number is not taste. With a fixed exit stub and a fixed entry
    // stub, four free points already express more distinct orthogonal routes than anybody
    // draws, and it bounds the saved document: 2000 connectors x 4 points x 2 numbers is
    // the same order as the block settings the 500-block cap already allows. The server
    // enforces the same number, because a cap only the browser knows is not a cap.
    const MAX_EDGE_WAYPOINTS = 4;

    // How near a bend a press has to land to move it rather than make a new one.
    const WAYPOINT_GRAB_PX = 8;

    // How near a line-up a dragged bend has to get before it snaps onto it. What makes a
    // hand-routed wire agree with the thing it is being routed past instead of sitting a
    // pixel off it.
    const WAYPOINT_SNAP_PX = 6;

    // Drop a bend this close to where the wire would run without it and it is removed. The
    // wire straightens itself rather than keeping a bend that bends nothing.
    const WAYPOINT_DISCARD_PX = 6;

    // The pending repaint of a drag, and the return lane it was computed against. Both are
    // per-frame: a mousemove only records where the cursor is, and one animation frame does
    // the drawing, however many mousemoves arrived in between. See `scheduleDragFrame`.

    function startDrag(nodeId, e) {
        e.preventDefault();
        const node = findNode(nodeId);
        const wrapperRect = wrapperEl.getBoundingClientRect();
        const startX = e.clientX + wrapperEl.scrollLeft - wrapperRect.left;
        const startY = e.clientY + wrapperEl.scrollTop - wrapperRect.top;
        state.dragging = {
            nodeId: nodeId,
            offsetX: startX - node.position.x,
            offsetY: startY - node.position.y,
            fromX: e.clientX,
            fromY: e.clientY,
            clientX: e.clientX,
            clientY: e.clientY,
            moved: false,
            // Which connectors this move disturbs, and where their geometry is written.
            // Resolved now, while nothing has moved and the DOM is settled.
            chrome: edges.chromeForMovingNodes([nodeId]),
        };
        edges.beginDragAnchors([nodeId], state.dragging.chrome);
        document.addEventListener("mousemove", onDragMove);
        document.addEventListener("mouseup", onDragEnd);
    }

    /**
     * Move the currently dragged node to follow the cursor and redraw its
     * edges.
     * @param {MouseEvent} e
     */
    function onDragMove(e) {
        if (!state.dragging) return;

        if (!state.dragging.moved) {
            const travelled = Math.max(
                Math.abs(e.clientX - state.dragging.fromX),
                Math.abs(e.clientY - state.dragging.fromY),
            );
            if (travelled < DRAG_THRESHOLD) return;
            state.dragging.moved = true;

            // Manual from the first frame that really moves, not from the release. The
            // layout request is debounced, so its answer can arrive while the mouse is
            // still down, and `applyLayout` is free to re-place every block for as long as
            // the canvas is still "auto". Flipping here closes that window.
            state.layout = "manual";
            updateLayoutButton();
        }

        state.dragging.clientX = e.clientX;
        state.dragging.clientY = e.clientY;
        edges.scheduleFrame();
    }

    /**
     * Finish a press on a step: either it moved the block, or it was a click.
     *
     * A block that actually moved switches the canvas to **manual** — an operator who has
     * put a block somewhere has said where they want it, and re-arranging it out from under
     * them on the next edit would be the canvas arguing. Tidy up is how they hand the
     * decision back.
     *
     * A press that never moved is the click that opens the properties panel, or the second
     * half of drawing a connector. It has to be handled here rather than by a separate
     * click listener, because the mousedown that started this already claimed the event.
     *
     * @param {MouseEvent} e
     */
    function onDragEnd(e) {
        const drag = state.dragging;
        edges.cancelFrame();
        state.dragging = null;
        edges.invalidateLane();
        edges.endDragAnchors();
        document.removeEventListener("mousemove", onDragMove);
        document.removeEventListener("mouseup", onDragEnd);

        if (!drag) return;

        if (drag.moved) {
            // `state.layout` was switched to manual on the first frame that moved — see
            // `onDragMove` — so what is left is the once-per-gesture work. A block dragged
            // toward an edge has to be able to go there, and the canvas only scrolls as
            // far as its own box.
            fitCanvas();
            markDirty();
            return;
        }

        onNodeBodyClick(drag.nodeId, e);
    }

    // ---------------------------------------------------------------
    // Bending a connector
    //
    // A connector is routed for you, and now and then the route is wrong — it runs behind
    // a block, or two of them overlap into one line nobody can follow. Dragging the wire
    // puts a bend in it, and the wire goes through that point from then on.
    //
    // The bends are stored on the connector as `waypoints`, in canvas coordinates. Absolute
    // rather than relative to the two ends, because a bend exists to dodge something *on
    // the canvas* — another block, another wire — and a bend that tracked its endpoints
    // would slide off the thing it was put there to avoid. The cost of absolute is that a
    // bend can go stale when a block moves, and that is paid explicitly: a group move that
    // carries **both** ends of a wire carries its bends with it, and a move of one end
    // leaves them where they are.
    // ---------------------------------------------------------------

    // True for one tick after a bend, so the click that trails it does not also select the
    // connector. The same device as `suppressNodeClick` in the graph designer.

    // {edgeId, index, inserted, fromX, fromY, clientX, clientY, moved, chrome, anchors}

    /**
     * The selector for the port a connector leaves by. One place, because `edgeRoute` and
     * the bend gesture have to agree on it exactly or a bend is computed against a
     * different line from the one on screen.
     *
     * @param {object} edge
     * @returns {string}
     */
    function sourceSelectorFor(edge) {
        return edge.derived
            ? "[data-jump-exit]"
            : '.flow-node-port-out[data-port="' + cssEscape(edge.source_port || "default") + '"]';
    }

    // ---------------------------------------------------------------
    // Moving several blocks at once
    //
    // The gesture itself is in static/js/graph_selection.js, shared with the other two
    // canvases. What is here is only what a group move *means* on this one: which
    // connectors have to follow, and what a finished move says about the drawing.
    // ---------------------------------------------------------------

    // The connectors a group move is repainting, resolved once when it starts, and the
    // hand-placed bends it is carrying with it.

    /**
     * Keep the properties panel honest about a selection that is no longer one thing.
     *
     * The panel shows one block or one connector. A box that has just caught five of them
     * has not asked to edit any of them, so the panel is emptied rather than left showing
     * whichever one happened to be open — and it is emptied rather than hidden, because
     * closing a panel somebody opened is as surprising as opening one they did not.
     */
    function onSelectionChange() {
        if (selection.count() === 1) return;
        if (!state.selectedNodeId && !state.selectedEdgeId) return;

        const previousNode = state.selectedNodeId;
        state.selectedNodeId = null;
        state.selectedEdgeId = null;
        if (previousNode) updateNodeSelectionClass(previousNode);
        renderAllEdges();
        propertiesBodyEl.innerHTML =
            '<p class="text-muted small">Select a node or connector to edit it here.</p>';
    }

    // ---------------------------------------------------------------
    // Move a connector to another node — drag one of its two small end
    // handles (near the source or the target) and drop it on a new spot.
    // Nothing in `state.edges` changes until a valid drop is found; an
    // invalid drop just re-renders from the unchanged edge, snapping the
    // curve back to where it started.
    // ---------------------------------------------------------------

    /**
     * Begin dragging one end of an existing edge to a new node/port.
     * @param {string} edgeId
     * @param {"source"|"target"} end
     */
    function startEdgeReattach(edgeId, end) {
        cancelPending();
        state.reattaching = { edgeId: edgeId, end: end };
        wrapperEl.classList.add("fb-reattaching");
        document.addEventListener("mousemove", onEdgeReattachMove);
        document.addEventListener("mouseup", onEdgeReattachEnd);
    }

    /**
     * Redraw the in-progress reattach edge following the cursor and
     * highlight whatever element is under it as a drop target.
     * @param {MouseEvent} e
     */
    function onEdgeReattachMove(e) {
        if (!state.reattaching) return;
        const edge = findEdge(state.reattaching.edgeId);
        const route = edge && edgeRoute(edge);
        const path = edge && document.getElementById("edge-" + edge.id);
        if (!route || !path) return;

        // The end being dragged follows the cursor; the other stays where the connector
        // already starts or ends, which is its first or last corner point.
        const cursor = edges.cursorPoint(e);
        const draggingTarget = state.reattaching.end === "target";
        const fixed = draggingTarget ? route[0] : route[route.length - 1];
        const from = draggingTarget ? fixed : cursor;
        const to = draggingTarget ? cursor : fixed;

        path.setAttribute("d", GC.elbowPathD(GC.elbowPoints(from, to)));

        highlightDropTarget(e, state.reattaching.end);
    }

    /**
     * Highlight the node (for a target reattach) or output port (for a
     * source reattach) currently under the cursor as a valid drop target.
     * @param {MouseEvent} e
     * @param {"source"|"target"} end
     */
    function highlightDropTarget(e, end) {
        document.querySelectorAll(".fb-drop-target").forEach(function (el) { el.classList.remove("fb-drop-target"); });
        const el = document.elementFromPoint(e.clientX, e.clientY);
        if (!el) return;
        const hit = end === "target" ? el.closest(".flow-node") : el.closest(".flow-node-port-out");
        if (hit) hit.classList.add("fb-drop-target");
    }

    /**
     * Finish an edge reattach drag: apply the retarget/resource change if
     * dropped on a valid target, then re-render (which also snaps the curve
     * back on an invalid drop).
     * @param {MouseEvent} e
     */
    function onEdgeReattachEnd(e) {
        if (!state.reattaching) return;
        const end = state.reattaching.end;
        const edge = findEdge(state.reattaching.edgeId);
        state.reattaching = null;
        document.removeEventListener("mousemove", onEdgeReattachMove);
        document.removeEventListener("mouseup", onEdgeReattachEnd);
        wrapperEl.classList.remove("fb-reattaching");
        document.querySelectorAll(".fb-drop-target").forEach(function (el) { el.classList.remove("fb-drop-target"); });
        if (!edge) return;

        const el = document.elementFromPoint(e.clientX, e.clientY);
        if (el) {
            if (end === "target") {
                const nodeEl = el.closest(".flow-node");
                if (nodeEl) applyEdgeRetarget(edge, nodeEl.dataset.nodeId);
            } else {
                const portEl = el.closest(".flow-node-port-out");
                const portNodeEl = portEl && portEl.closest(".flow-node");
                if (portNodeEl) applyEdgeResource(edge, portNodeEl.dataset.nodeId, portEl.dataset.port);
            }
        }
        // Re-render unconditionally: commits the change on a valid drop,
        // or snaps the curve back to its prior geometry otherwise.
        renderAllEdges();
    }

    // Re-point the target (input) end of an edge at a different node.
    // Mirrors the "Start node cannot have incoming edges" / no-self-loop
    // rules the backend re-checks in flow_service._validate_edges on save.
    /**
     * Re-point the target (input) end of an edge at a different node.
     * @param {object} edge
     * @param {string} newTargetId
     */
    function applyEdgeRetarget(edge, newTargetId) {
        if (!newTargetId || newTargetId === edge.source) return;
        const targetNode = findNode(newTargetId);
        if (!targetNode || targetNode.type === "start") return;
        edge.target = newTargetId;
        wiringChanged();
    }

    // Re-point the source (output port) end of an edge at a different
    // node/port. Mirrors the "at most one edge per (source, port)" rule
    // the backend re-checks on save by dropping any edge already using
    // that port, same as completePendingConnection() does for new edges.
    /**
     * Re-point the source (output port) end of an edge at a different
     * node/port.
     * @param {object} edge
     * @param {string} newSourceId
     * @param {string} newPort
     */
    function applyEdgeResource(edge, newSourceId, newPort) {
        if (!newSourceId || !newPort || newSourceId === edge.target) return;
        state.edges = state.edges.filter(function (ed) {
            return ed.id === edge.id || !(ed.source === newSourceId && ed.source_port === newPort);
        });
        edge.source = newSourceId;
        edge.source_port = newPort;
        wiringChanged();
    }

    // ---------------------------------------------------------------
    // Connection creation — click source port, then click target
    // ---------------------------------------------------------------

    /**
     * Begin a new connection: remember the clicked source port and
     * highlight it while a target click is awaited.
     * @param {string} nodeId
     * @param {string} port
     */
    function onSourcePortClick(nodeId, port) {
        clearPendingHighlight();
        state.pending = { nodeId: nodeId, port: port };
        const portEl = document.querySelector('#node-' + nodeId + ' .flow-node-port-out[data-port="' + cssEscape(port) + '"]');
        if (portEl) portEl.classList.add("fb-port-pending");
    }

    /**
     * Complete a pending connection by using this node as its target.
     * @param {string} nodeId
     */
    function onTargetPortClick(nodeId) {
        completePendingConnection(nodeId);
    }

    /**
     * Complete a pending connection if one is active, otherwise select this
     * node.
     * @param {string} nodeId
     * @param {MouseEvent} e
     */
    function onNodeBodyClick(nodeId, e) {
        if (state.pending) {
            e.stopPropagation();
            completePendingConnection(nodeId);
            return;
        }
        selectNode(nodeId);
    }

    /**
     * Finalize the pending source port into a new edge targeting
     * targetNodeId, replacing any existing edge already on that port.
     * @param {string} targetNodeId
     */
    function completePendingConnection(targetNodeId) {
        if (!state.pending) return;
        const sourceNodeId = state.pending.nodeId;
        const port = state.pending.port;
        clearPendingHighlight();
        state.pending = null;
        if (sourceNodeId === targetNodeId) return;

        // At most one edge per (source, port) — replace if one exists.
        state.edges = state.edges.filter(function (ed) {
            return !(ed.source === sourceNodeId && ed.source_port === port);
        });
        state.edges.push({ id: genId("e"), source: sourceNodeId, source_port: port, target: targetNodeId });
        renderAllEdges();
        wiringChanged();
    }

    /**
     * Remove the pending-connection highlight from whichever port has it.
     */
    function clearPendingHighlight() {
        document.querySelectorAll(".fb-port-pending").forEach(function (el) {
            el.classList.remove("fb-port-pending");
        });
    }

    /**
     * Cancel an in-progress connection.
     */
    function cancelPending() {
        clearPendingHighlight();
        state.pending = null;
    }

    // ---------------------------------------------------------------
    // Selection
    // ---------------------------------------------------------------

    /**
     * Select a node, update its styling, and open the properties panel for
     * it.
     * @param {string} nodeId
     */
    function selectNode(nodeId) {
        const previous = state.selectedNodeId;
        state.selectedEdgeId = null;
        state.selectedNodeId = nodeId;
        if (previous) updateNodeSelectionClass(previous);
        updateNodeSelectionClass(nodeId);
        renderAllEdges();
        renderPropertyForm(findNode(nodeId));
        const offcanvas = bootstrap.Offcanvas.getOrCreateInstance(document.getElementById("propertiesOffcanvas"));
        offcanvas.show();
    }

    /**
     * Select an edge, update its styling, and open the properties panel for
     * it.
     * @param {string} edgeId
     */
    function selectEdge(edgeId) {
        if (state.selectedNodeId) updateNodeSelectionClass(state.selectedNodeId);
        state.selectedNodeId = null;
        state.selectedEdgeId = edgeId;
        renderAllEdges();
        renderEdgeProperties(edgeId);
        const offcanvas = bootstrap.Offcanvas.getOrCreateInstance(document.getElementById("propertiesOffcanvas"));
        offcanvas.show();
    }

    /**
     * Clear the current node/edge selection.
     */
    function deselectAll() {
        if (state.selectedNodeId) updateNodeSelectionClass(state.selectedNodeId);
        state.selectedNodeId = null;
        state.selectedEdgeId = null;
        renderAllEdges();
    }

    // ---------------------------------------------------------------
    // Delete
    // ---------------------------------------------------------------

    /**
     * Remove a node and any edges touching it, then re-render.
     * @param {string} nodeId
     */
    function deleteNode(nodeId) {
        edges.abandonDrag();
        if (selection) selection.abandon();
        state.nodes = state.nodes.filter(function (n) { return n.id !== nodeId; });
        state.edges = state.edges.filter(function (ed) { return ed.source !== nodeId && ed.target !== nodeId; });
        if (state.selectedNodeId === nodeId) state.selectedNodeId = null;
        // Re-derived rather than one key deleted: a block takes its connectors with it, so
        // a stale connector id can outlive the block deletion that caused it.
        if (selection) selection.prune();
        renderAllNodes();
        renderAllEdges();
        updatePaletteAvailability();
        propertiesBodyEl.innerHTML = '<p class="text-muted small">Select a node or connector to edit it here.</p>';
        wiringChanged();
    }

    /**
     * Remove an edge and re-render.
     * @param {string} edgeId
     */
    /**
     * The block types a connector's "+" offers.
     *
     * Both exclusions are correctness rather than tidiness.
     *
     * A type with **no way out** — End Flow, Goto — would leave the connector's target with
     * nothing leading to it. Splicing one in does not add a step, it silently severs the
     * rest of the flow, and the operator's next clue is a visitor's conversation stopping
     * dead. Start is excluded from the other side: it has no inlet, so nothing can lead
     * into it.
     *
     * Menu and Dropdown fail the same test for a subtler reason worth stating, because it
     * looks like an oversight. Their ports *are* their options, so a freshly added one has
     * none, and there is no port for the flow to carry on through. They stay in the main
     * palette, where you add one, give it options, and wire it deliberately.
     *
     * @returns {Array<{type: string, label: string, icon: string}>}
     */
    function insertableTypes() {
        return Object.keys(NODE_TYPES)
            .filter(function (type) {
                if (type === "start") return false;
                return !!GC.continuationPort(
                    metaFor(type).outputs(defaultData(type)).map(function (spec) {
                        return spec.port;
                    }));
            })
            .map(function (type) {
                const meta = metaFor(type);
                return { type: type, label: meta.label, icon: meta.icon };
            });
    }

    /**
     * Put a new block in the middle of an existing connector.
     *
     * `A → B` becomes `A → new → B`: the block arrives already wired, which is the whole
     * point of the gesture — otherwise it is three separate actions, one of which is
     * remembering to delete the connector you have just bypassed.
     *
     * The new block leaves by its **first** port. On a two-port block — Create File, Run
     * Graph, Run Flow, Send Email — that is the success one, and it is the only defensible
     * reading: what used to continue to B continues to B when the work succeeds, and the
     * failure port is left free so routing it stays a decision somebody makes rather than
     * one this function makes for them.
     *
     * @param {string} type
     * @param {string} edgeId
     */
    function insertOnEdge(type, edgeId) {
        const edge = findEdge(edgeId);
        if (!edge) return;

        // A Goto's jump is drawn from the block's settings rather than stored as a
        // connector, so there is no edge here to split in two.
        if (edge.derived) {
            noteLayoutFailure("That dashed jump comes from the Goto block's settings. " +
                "Edit the Goto block to change where it goes.");
            return;
        }

        const meta = metaFor(type);
        const data = defaultData(type);
        // Not `outputs(data)[0]` — see `GC.continuationPort` for why the first port is the
        // wrong answer on a shape whose first way out is not "carry on".
        const onward = GC.continuationPort(meta.outputs(data).map(function (spec) {
            return spec.port;
        }));

        if (!onward) {
            noteLayoutFailure("A " + meta.label + " has no way out until it is configured, " +
                "so it cannot be put inside a connector. Add it from the palette instead.");
            return;
        }

        const node = {
            id: genId("n"),
            type: type,
            position: betweenBlocks(findNode(edge.source), findNode(edge.target)),
            data: data,
        };

        state.nodes.push(node);
        // One connector out, two in. Done as a filter plus two pushes rather than by
        // mutating the edge and adding one, so a half-applied splice cannot exist: either
        // both new connectors are there or the original still is.
        state.edges = state.edges.filter(function (other) { return other.id !== edge.id; });
        state.edges.push({
            id: genId("e"), source: edge.source, source_port: edge.source_port, target: node.id,
        });
        state.edges.push({
            id: genId("e"), source: node.id, source_port: onward, target: edge.target,
        });

        // The connector that was selected no longer exists, and a drag or a selection
        // holding its id would be holding a dead one.
        edges.abandonDrag();
        if (selection) selection.abandon();
        state.selectedEdgeId = null;
        if (selection) selection.prune();

        renderAllNodes();
        renderAllEdges();
        updatePaletteAvailability();
        wiringChanged();
        // Opened straight away: a spliced block is almost never useful with its default
        // settings, and this is the one moment the operator is certainly looking at it.
        selectNode(node.id);
    }

    /**
     * Where a spliced block first appears: midway between the two it now sits between.
     *
     * In `auto` this lasts a moment — the arrange that follows puts it wherever its new
     * connections say. It is worth getting right anyway, because in `manual` it is final,
     * and a block that appeared at the origin and jumped would read as a glitch.
     *
     * @param {object|null} source
     * @param {object|null} target
     * @returns {{x: number, y: number}}
     */
    function betweenBlocks(source, target) {
        const a = (source || {}).position || null;
        const b = (target || {}).position || null;

        if (a && b) return { x: Math.round((a.x + b.x) / 2), y: Math.round((a.y + b.y) / 2) };
        if (a) return { x: a.x, y: a.y + canvasMetrics().rowGap };
        if (b) return { x: b.x, y: Math.max(0, b.y - canvasMetrics().rowGap) };
        return placementForNewBlock();
    }

    function deleteEdge(edgeId) {
        edges.abandonDrag();
        if (selection) selection.abandon();
        state.edges = state.edges.filter(function (ed) { return ed.id !== edgeId; });
        state.selectedEdgeId = null;
        if (selection) selection.prune();
        renderAllEdges();
        propertiesBodyEl.innerHTML = '<p class="text-muted small">Select a node or connector to edit it here.</p>';
        wiringChanged();
    }

    // ---------------------------------------------------------------
    // Properties panel
    // ---------------------------------------------------------------

    /**
     * Render the properties panel contents for a selected edge (its
     * endpoints and a delete button).
     * @param {string} edgeId
     */
    function renderEdgeProperties(edgeId) {
        const edge = state.edges.filter(function (e) { return e.id === edgeId; })[0];
        if (!edge) return;
        const bends = GC.waypointsOf(edge).length;

        propertiesBodyEl.innerHTML =
            '<p class="small text-muted">Connector from <strong>' + escapeHtml(edge.source) + "</strong> (" +
            escapeHtml(edge.source_port) + ') to <strong>' + escapeHtml(edge.target) + "</strong>.</p>" +
            // Offered only when there is something to straighten, so the panel does not
            // carry a button that would do nothing. Double-clicking the wire does the same
            // thing; this is the discoverable half.
            (bends
                ? '<p class="small text-muted">Routed by hand through ' + bends +
                  (bends === 1 ? " bend" : " bends") + ".</p>" +
                  '<button type="button" class="btn btn-outline-secondary btn-sm mb-2" id="fbStraightenEdgeBtn">' +
                  '<i class="las la-ruler-horizontal"></i> Straighten connector</button><br>'
                : "") +
            '<button type="button" class="btn btn-outline-danger btn-sm" id="fbDeleteEdgeBtn"><i class="las la-trash"></i> Delete connector</button>';
        document.getElementById("fbDeleteEdgeBtn").addEventListener("click", function () { deleteEdge(edge.id); });
        const straightenBtn = document.getElementById("fbStraightenEdgeBtn");
        if (straightenBtn) {
            straightenBtn.addEventListener("click", function () {
                edges.straightenEdge(edge.id);
                renderEdgeProperties(edge.id);
            });
        }
    }

    /**
     * Replace a single node's DOM element in place and redraw its edges.
     * @param {object} node
     */
    function reRenderSingleNode(node) {
        const old = document.getElementById("node-" + node.id);
        if (old) old.remove();
        renderNode(node);
        renderAllEdges();
    }

    /**
     * Deep-clone a plain JSON-serializable object.
     * @param {object} obj
     * @returns {object}
     */
    function deepClone(obj) {
        return JSON.parse(JSON.stringify(obj || {}));
    }

    /**
     * Hide the properties offcanvas and reset selection and panel content.
     */
    function closePropertiesPanel() {
        const offcanvasEl = document.getElementById("propertiesOffcanvas");
        const instance = bootstrap.Offcanvas.getInstance(offcanvasEl);
        if (instance) instance.hide();
        deselectAll();
        propertiesBodyEl.innerHTML = '<p class="text-muted small">Select a node or connector to edit it here.</p>';
    }

    // Edits happen against a private copy of node.data (`draft`) so the
    // canvas is untouched until Save commits it — Cancel just discards
    // `draft` and nothing on the node ever changes.
    /**
     * Render the type-specific properties form for a node into a private
     * draft copy of its data, wiring inputs to mutate the draft and
     * Save/Cancel to commit or discard it.
     * @param {object} node
     */
    function renderPropertyForm(node) {
        if (!node) return;
        const draft = deepClone(node.data);
        let html = '<div class="mb-2"><span class="badge bg-secondary">' + escapeHtml(NODE_TYPES[node.type].label) + "</span></div>";

        // Every block type but Start — there is only ever one of those, so nothing to
        // tell apart. Given here rather than per-type: it is the one field every block
        // shares, and every picker on this canvas (`blockLabelById`) reads it the same
        // way regardless of what block it is naming.
        if (node.type !== "start") {
            html += fieldHtml(
                "Name this block (optional)",
                '<input class="form-control form-control-sm" data-field="label" ' +
                'placeholder="' + escapeAttr(NODE_TYPES[node.type].label) + '" ' +
                'value="' + escapeAttr(draft.label || "") + '">',
            );
        }

        if (node.type === "if_else") {
            html +=
                fieldHtml("Variable name", '<input class="form-control form-control-sm" data-field="variable_name" value="' + escapeAttr(draft.variable_name || "") + '">') +
                fieldHtml("Operator", operatorSelectHtml(draft.operator)) +
                fieldHtml("Compare value", '<input class="form-control form-control-sm" data-field="compare_value" value="' + escapeAttr(draft.compare_value || "") + '">');
        } else if (node.type === "goto") {
            html += fieldHtml("Target node", gotoTargetSelectHtml(node, draft.target_node_id));
        } else if (node.type === "menu" || node.type === "dropdown") {
            html +=
                fieldHtml("Prompt text", '<textarea class="form-control form-control-sm" rows="2" data-field="prompt_text">' + escapeHtml(draft.prompt_text || "") + "</textarea>") +
                fieldHtml("Store choice in variable (optional)", '<input class="form-control form-control-sm" data-field="variable_name" value="' + escapeAttr(draft.variable_name || "") + '">') +
                '<label class="form-label fw-semibold small mt-2">Options</label><div id="fbOptionsList"></div>' +
                '<button type="button" class="btn btn-outline-secondary btn-sm mt-1" id="fbAddOptionBtn"><i class="las la-plus"></i> Add option</button>';
        } else if (node.type === "ask_input") {
            html +=
                fieldHtml("Prompt text", '<textarea class="form-control form-control-sm" rows="2" data-field="prompt_text">' + escapeHtml(draft.prompt_text || "") + "</textarea>") +
                fieldHtml("Store answer in variable", '<input class="form-control form-control-sm" data-field="variable_name" value="' + escapeAttr(draft.variable_name || "") + '">');
        } else if (node.type === "send_message") {
            html += fieldHtml("Message text", '<textarea class="form-control form-control-sm" rows="3" data-field="message_text">' + escapeHtml(draft.message_text || "") + "</textarea>");
        } else if (node.type === "send_email") {
            html += sendEmailFieldsHtml(draft);
        } else if (node.type === "run_graph") {
            html += runGraphFieldsHtml(draft);
        } else if (node.type === "run_flow") {
            html += runFlowFieldsHtml(draft);
        } else if (node.type === "create_file") {
            html += createFileFieldsHtml(node, draft);
        } else if (node.type === "download_file") {
            html += downloadFileFieldsHtml(node, draft);
        } else if (node.type === "ai_fallback") {
            html += aiFallbackFieldsHtml(node, draft);
        } else if (node.type === "end") {
            html +=
                fieldHtml("Closing message (optional)", '<textarea class="form-control form-control-sm" rows="3" data-field="message_text">' + escapeHtml(draft.message_text || "") + "</textarea>") +
                '<p class="text-muted small mb-0">Ends the conversation here. The visitor\'s next message after this gets a normal AI-answered reply instead of continuing the flow.</p>';
        } else {
            html += '<p class="text-muted small">This block has no configurable properties.</p>';
        }

        html +=
            '<div class="fb-prop-footer d-flex gap-2 mt-3 pt-3 border-top">' +
            '<button type="button" class="btn btn-primary btn-sm flex-fill" id="fbSavePropsBtn"><i class="las la-check"></i> Save</button>' +
            '<button type="button" class="btn btn-outline-secondary btn-sm flex-fill" id="fbCancelPropsBtn">Cancel</button>' +
            "</div>";

        propertiesBodyEl.innerHTML = html;

        propertiesBodyEl.querySelectorAll("[data-field]").forEach(function (input) {
            input.addEventListener("input", function () {
                draft[input.dataset.field] = input.value;
            });
        });

        if (node.type === "send_email") {
            wireSendEmailFields(node, draft);
        }

        if (node.type === "run_flow") {
            wireRunFlowFields(node, draft);
        }

        if (node.type === "create_file") {
            wireCreateFileFields(node, draft);
        }

        if (node.type === "download_file") {
            wireDownloadFileFields(node, draft);
        }

        if (node.type === "menu" || node.type === "dropdown") {
            draft.options = draft.options || [];
            renderOptionsList(document.getElementById("fbOptionsList"), draft);
            document.getElementById("fbAddOptionBtn").addEventListener("click", function () {
                draft.options.push({ id: genId("opt"), label: "", value: "" });
                renderOptionsList(document.getElementById("fbOptionsList"), draft);
            });
        }

        if (node.type === "ai_fallback") {
            wireAiFallbackFields(node, draft);
        }

        document.getElementById("fbSavePropsBtn").addEventListener("click", function () {
            applyDraftAndClose(node, draft);
        });
        document.getElementById("fbCancelPropsBtn").addEventListener("click", function () {
            closePropertiesPanel();
        });
    }

    /**
     * Commit a draft's edits onto the node, dropping edges for any menu/
     * dropdown options that were removed from the draft, then close the
     * properties panel.
     * @param {object} node
     * @param {object} draft
     */
    function applyDraftAndClose(node, draft) {
        // Any option removed from the draft needs its connector dropped too
        // — it can no longer be a valid edge source_port once saved.
        if (node.type === "menu" || node.type === "dropdown") {
            const keptIds = (draft.options || []).map(function (o) { return o.id; });
            state.edges = state.edges.filter(function (ed) {
                return ed.source !== node.id || keptIds.indexOf(ed.source_port) !== -1;
            });
        }

        node.data = draft;
        reRenderSingleNode(node);
        closePropertiesPanel();
        wiringChanged();

        // This node's fields only changed in the browser — nothing here is
        // live for visitors until the flow-level Save button (in the
        // toolbar) posts the whole graph to the server. Surface that
        // immediately rather than leaving people to discover it by testing
        // a live chat and finding their guardrails/prompt/settings weren't
        // applied.
        const responseEl = document.getElementById(opts.responseTargetId);
        if (responseEl) {
            responseEl.innerHTML =
                '<div class="alert alert-warning py-2 mb-0" role="alert">' +
                '<i class="las la-exclamation-triangle"></i> Node settings updated in the editor only. ' +
                'Click <strong>Save</strong> above to publish this flow — visitors won\'t see these changes until then.' +
                "</div>";
        }
    }

    /**
     * Render a menu/dropdown node's editable option rows and wire their
     * label input and remove button.
     * @param {HTMLElement} container
     * @param {object} draft
     */
    function renderOptionsList(container, draft) {
        const options = draft.options;
        container.innerHTML = options.map(function (o, idx) {
            return (
                '<div class="input-group input-group-sm mb-1" data-option-id="' + escapeAttr(o.id) + '">' +
                '<input class="form-control" placeholder="Label" data-opt-field="label" value="' + escapeAttr(o.label || "") + '">' +
                '<button class="btn btn-outline-danger" type="button" data-opt-remove="' + idx + '"><i class="las la-trash"></i></button>' +
                "</div>"
            );
        }).join("");

        container.querySelectorAll("[data-opt-field]").forEach(function (input) {
            input.addEventListener("input", function () {
                const row = input.closest("[data-option-id]");
                const opt = options.filter(function (o) { return o.id === row.dataset.optionId; })[0];
                if (!opt) return;
                opt.label = input.value;
                opt.value = input.value;
            });
        });
        container.querySelectorAll("[data-opt-remove]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                options.splice(Number(btn.dataset.optRemove), 1);
                renderOptionsList(container, draft);
            });
        });
    }

    /**
     * Wrap an input control with its form-group label markup.
     * @param {string} label
     * @param {string} inputHtml
     * @returns {string}
     */
    function fieldHtml(label, inputHtml) {
        return '<div class="mb-2"><label class="form-label small fw-semibold">' + escapeHtml(label) + "</label>" + inputHtml + "</div>";
    }

    /**
     * Build the operator `<select>` for an if/else node's properties form.
     * @param {string} current - currently selected operator value
     * @returns {string}
     */
    function operatorSelectHtml(current) {
        const options = [["not_empty", "Is not empty"], ["equals", "Equals"], ["contains", "Contains"]];
        return '<select class="form-select form-select-sm" data-field="operator">' + options.map(function (o) {
            return '<option value="' + o[0] + '"' + (o[0] === current ? " selected" : "") + ">" + o[1] + "</option>";
        }).join("") + "</select>";
    }

    /**
     * Build the target-node `<select>` for a goto node's properties form,
     * excluding the node itself.
     * @param {object} node - the goto node being edited
     * @param {string} current - currently selected target node id
     * @returns {string}
     */
    function gotoTargetSelectHtml(node, current) {
        const opts2 = state.nodes.filter(function (n) { return n.id !== node.id; }).map(function (n) {
            return '<option value="' + escapeAttr(n.id) + '"' + (n.id === current ? " selected" : "") + ">" + escapeHtml(NODE_TYPES[n.type].label) + " (" + escapeHtml(n.id) + ")</option>";
        }).join("");
        return '<select class="form-select form-select-sm" data-field="target_node_id"><option value="">Select a node&hellip;</option>' + opts2 + "</select>";
    }

    // ---------------------------------------------------------------
    // AI Fallback properties — guardrails/prompt, context source (attached
    // datasource / knowledge base / prompt only), and LLM choice (in-built
    // vs. an attached AI Settings key). The guardrails/prompt/context/LLM
    // fields are plain node.data (deferred to the graph Save button like
    // every other field); the knowledge base's documents are NOT — they're
    // uploaded/typed/trained against KnowledgeBaseController immediately,
    // independent of whether the flow graph itself has been saved yet.
    // ---------------------------------------------------------------

    /**
     * Build the AI Fallback node's properties form: guardrails/prompt,
     * context source, the knowledge base panel, and LLM choice.
     * @param {object} draft
     * @returns {string}
     */
    /**
     * An AI Fallback block's properties.
     *
     * The last field is the one that connects this block to the rest of the canvas:
     * naming a variable keeps the answer, so an Email block further down can mail what
     * the AI worked out and an If/Else can branch on whether it answered at all. The
     * answer is stored whole — narrative, bullet points and table rows — because a
     * visitor who asked to be emailed the data meant the figures.
     *
     * @param {object} draft
     * @returns {string}
     */
    function aiFallbackFieldsHtml(node, draft) {
        draft.context_source = draft.context_source || "datasource";
        draft.llm_mode = draft.llm_mode || "in_built";
        return (
            fieldHtml("Guardrails", '<textarea class="form-control form-control-sm" rows="2" data-field="guardrails" placeholder="e.g. Never discuss pricing, stay polite and on-topic">' + escapeHtml(draft.guardrails || "") + "</textarea>") +
            fieldHtml("Prompt / instructions", '<textarea class="form-control form-control-sm" rows="2" data-field="prompt" placeholder="Extra instructions for how the AI should answer">' + escapeHtml(draft.prompt || "") + "</textarea>") +
            fieldHtml("Answer using", contextSourceSelectHtml(draft.context_source)) +
            '<div id="fbKbPanel" style="' + (draft.context_source === "knowledge_base" ? "" : "display:none;") + '">' + knowledgeBasePanelHtml(node, draft) + "</div>" +
            fieldHtml("Language model", llmModeSelectHtml(draft.llm_mode)) +
            '<div id="fbLlmKeyField" style="' + (draft.llm_mode === "attached" ? "" : "display:none;") + '">' +
            fieldHtml("Attached API key", llmApiKeySelectHtml(draft.llm_api_key_id)) +
            "</div>" +
            fieldHtml(
                "Store the answer in variable (optional)",
                '<input class="form-control form-control-sm" data-field="variable_name" value="' +
                escapeAttr(draft.variable_name || "") + '">'
            ) +
            '<p class="text-muted small mb-0">The answer is shown to the visitor either ' +
            "way. Naming a variable <strong>also keeps it</strong> — the whole answer, " +
            "including any bullet points and table rows — so a later Send Email block can " +
            "mail it, or an If / Else can branch on whether the AI answered at all. This " +
            "block ends the turn, so an Email block after it sends on the visitor's next " +
            "message; the variable is still there by then.</p>"
        );
    }

    /**
     * Build the "Answer using" `<select>` (datasource / knowledge base /
     * prompt only) for an AI Fallback node.
     * @param {string} current
     * @returns {string}
     */
    function contextSourceSelectHtml(current) {
        const options = [["datasource", "Attached datasource"], ["knowledge_base", "Knowledge base"], ["prompt", "Prompt only"]];
        return '<select class="form-select form-select-sm" data-field="context_source" id="fbContextSourceSelect">' + options.map(function (o) {
            return '<option value="' + o[0] + '"' + (o[0] === current ? " selected" : "") + ">" + o[1] + "</option>";
        }).join("") + "</select>";
    }

    /**
     * Build the LLM mode `<select>` (in-built vs. attached API key) for an
     * AI Fallback node.
     * @param {string} current
     * @returns {string}
     */
    function llmModeSelectHtml(current) {
        const options = [["in_built", "In-built LLM"], ["attached", "Attached LLM API"]];
        return '<select class="form-select form-select-sm" data-field="llm_mode" id="fbLlmModeSelect">' + options.map(function (o) {
            return '<option value="' + o[0] + '"' + (o[0] === current ? " selected" : "") + ">" + o[1] + "</option>";
        }).join("") + "</select>";
    }

    /**
     * The Run Graph node's fields: which published graph, and where to keep the count.
     *
     * Two things are said in the help text because neither is guessable from the canvas
     * and both change how the conversation behaves. A graph containing an "Ask a human"
     * node will put its question to the visitor and wait for their reply — this is the
     * only block other than Ask for Input, Menu and Dropdown that can do that. And the
     * variable holds *how many* rows the graph produced, not the rows themselves, because
     * a variable is text that goes into a message.
     */

    /**
     * An Email block's properties: template, server, recipients, and one binding row per
     * variable the chosen template declares.
     *
     * The declared variables ride along on each entry of `opts.emailTemplates`, sent by the
     * edit route, so choosing a template can draw its rows without a round trip — a fetch
     * here would let somebody Save the block before its rows had loaded.
     *
     * A flow offers three sources and no more: something the conversation collected, one of
     * the agent's prompt variables from the Agents section, or a fixed value. There are no
     * upstream node outputs in this engine — its state is one flat string map — and
     * `_validate_send_email_data` refuses anything else on save.
     *
     * An AI Fallback block's answer arrives through the *first* of those rather than needing
     * a fourth: that block stores its answer under a variable name of the operator's
     * choosing, so "email me what the AI found" is one binding on a value the conversation
     * collected, like any other.
     */
    function sendEmailFieldsHtml(draft) {
        const templates = opts.emailTemplates || [];
        const servers = opts.smtpConfigs || [];

        if (!templates.length || !servers.length) {
            return '<p class="text-muted small mb-0">' +
                "You need at least one email template and one SMTP server before this " +
                'block can send anything. Set them up under <a href="/emails/smtp/">Email</a>.' +
                "</p>";
        }

        const templateSelect =
            '<select class="form-select form-select-sm" data-email-template>' +
            '<option value="">Select a template&hellip;</option>' +
            templates.map(function (t) {
                return '<option value="' + escapeAttr(t.uuid) + '"' +
                    (t.uuid === draft.template_id ? " selected" : "") + ">" +
                    escapeHtml(t.label) + (t.disabled_reason ? " — " + escapeHtml(t.disabled_reason) : "") +
                    "</option>";
            }).join("") + "</select>";

        const serverSelect =
            '<select class="form-select form-select-sm" data-field="smtp_config_id">' +
            '<option value="">Select a server&hellip;</option>' +
            servers.map(function (c) {
                return '<option value="' + escapeAttr(c.uuid) + '"' +
                    (c.uuid === draft.smtp_config_id ? " selected" : "") + ">" +
                    escapeHtml(c.label) + (c.disabled_reason ? " — " + escapeHtml(c.disabled_reason) : "") +
                    "</option>";
            }).join("") + "</select>";

        draft.recipients = draft.recipients || { to: [], cc: [], bcc: [] };

        const recipientRows = ["to", "cc", "bcc"].map(function (key) {
            const label = key === "to" ? "To" : key === "cc" ? "Cc" : "Bcc";
            return fieldHtml(
                label,
                '<input class="form-control form-control-sm" data-recipients="' + key + '" value="' +
                escapeAttr((draft.recipients[key] || []).join(", ")) + '">'
            );
        }).join("");

        return fieldHtml("Template", templateSelect) +
            fieldHtml("Send through", serverSelect) +
            recipientRows +
            '<p class="text-muted small">Comma-separated. Each entry may be a fixed ' +
            "address or a {{VARIABLE}}.</p>" +
            '<label class="form-label fw-semibold small mt-2">Variables</label>' +
            '<div id="fbEmailBindings"></div>' +
            '<p class="text-muted small">“A value the conversation collected” covers ' +
            "anything an Ask for Input, a Menu or an <strong>AI Fallback</strong> block " +
            "stored — give that block a variable name and bind to it here to email what " +
            "the AI answered.</p>" +
            fieldHtml(
                "Store the email's id in (optional)",
                '<input class="form-control form-control-sm" data-field="variable_name" value="' +
                escapeAttr(draft.variable_name || "") + '">'
            ) +
            '<p class="text-muted small mb-0">The email is queued and the flow carries on — ' +
            "nothing is said to the visitor unless you say it with a Send Message block. " +
            "Whether it was delivered is in the " +
            '<a href="/emails/messages/">delivery log</a>.</p>';
    }

    /** Wire the email panel's own controls after its HTML has been placed. */
    function wireSendEmailFields(node, draft) {
        const templateEl = propertiesBodyEl.querySelector("[data-email-template]");
        const bindingsEl = propertiesBodyEl.querySelector("#fbEmailBindings");

        propertiesBodyEl.querySelectorAll("[data-recipients]").forEach(function (input) {
            input.addEventListener("input", function () {
                draft.recipients[input.dataset.recipients] = input.value
                    .split(",")
                    .map(function (part) { return part.trim(); })
                    .filter(function (part) { return part !== ""; });
            });
        });

        if (!templateEl || !bindingsEl) return;

        function renderBindings() {
            draft.variable_bindings = draft.variable_bindings || {};
            const chosen = (opts.emailTemplates || []).find(function (t) {
                return t.uuid === draft.template_id;
            });
            const declared = (chosen && chosen.variables) || [];

            if (!draft.template_id) {
                bindingsEl.innerHTML =
                    '<p class="text-muted small mb-0">Choose a template to see what it needs.</p>';
                return;
            }
            if (!declared.length) {
                bindingsEl.innerHTML =
                    '<p class="text-muted small mb-0">This template declares no variables.</p>';
                return;
            }

            bindingsEl.innerHTML = declared.map(function (variable) {
                const current = draft.variable_bindings[variable.name] || {};
                const sources = [
                    { value: "session", label: "A value the conversation collected" },
                    { value: "agent", label: "An agent variable (Agents section)" },
                    { value: "literal", label: "A fixed value" },
                ];
                const sourceSelect =
                    '<select class="form-select form-select-sm" data-binding-source="' +
                    escapeAttr(variable.name) + '">' +
                    '<option value="">' +
                    (variable.required ? "Choose a source&hellip;" : "Leave unset (use the default)") +
                    "</option>" +
                    sources.map(function (s) {
                        return '<option value="' + s.value + '"' +
                            (current.source === s.value ? " selected" : "") + ">" +
                            escapeHtml(s.label) + "</option>";
                    }).join("") + "</select>";

                const valueField = current.source
                    ? '<input class="form-control form-control-sm mt-1" data-binding-value="' +
                      escapeAttr(variable.name) + '" placeholder="' +
                      (current.source === "literal" ? "The text to use"
                          : current.source === "agent" ? "COMPANY" : "the variable name")
                      + '" value="' +
                      escapeAttr(current.source === "literal" ? (current.value || "") : (current.path || "")) +
                      '">'
                    : "";

                return '<div class="border rounded p-2 mb-2">' +
                    '<div class="small fw-semibold">{{' + escapeHtml(variable.name) + "}}" +
                    (variable.required ? " (required)" : "") + "</div>" +
                    (variable.label ? '<div class="text-muted small">' + escapeHtml(variable.label) + "</div>" : "") +
                    sourceSelect + valueField +
                    "</div>";
            }).join("");

            bindingsEl.querySelectorAll("[data-binding-source]").forEach(function (select) {
                select.addEventListener("change", function () {
                    const name = select.dataset.bindingSource;
                    if (!select.value) {
                        delete draft.variable_bindings[name];
                    } else {
                        draft.variable_bindings[name] = { source: select.value };
                    }
                    renderBindings();
                });
            });

            bindingsEl.querySelectorAll("[data-binding-value]").forEach(function (input) {
                input.addEventListener("input", function () {
                    const name = input.dataset.bindingValue;
                    const binding = draft.variable_bindings[name];
                    if (!binding) return;
                    if (binding.source === "literal") {
                        binding.value = input.value;
                    } else {
                        binding.path = input.value.trim();
                    }
                });
            });
        }

        templateEl.addEventListener("change", function () {
            draft.template_id = templateEl.value;
            // The bindings belong to the old template's variables, so keeping them would
            // save a binding the new template does not declare — which the server refuses.
            draft.variable_bindings = {};
            renderBindings();
        });

        renderBindings();
    }

    /**
     * A Run Flow block's properties: which flow to call, what to pass it, what to keep.
     *
     * Both lists of rows are drawn from the chosen flow's own `reads`/`writes`, which ride
     * along on each entry of `opts.flows` — sent by the edit route for the same reason the
     * email templates carry their declared variables: fetching them when a flow is picked
     * would let somebody Save the block before its rows had loaded.
     *
     * The lists are what that flow *does*, not what somebody once typed here, so they cannot
     * drift from it. What they cannot cover is a value the called flow consumes invisibly — a
     * Run Graph block inside it is handed the whole variable map — which is what the
     * add-by-hand row is for.
     *
     * @param {object} draft
     * @returns {string}
     */
    function runFlowFieldsHtml(draft) {
        const flows = opts.flows || [];

        if (!flows.length) {
            return '<p class="text-muted small mb-0">No published <strong>generic</strong> ' +
                "flows yet. A Run Flow block calls a <em>child</em> flow, so in " +
                '<a href="/flow-builder/">Flow Builder</a> create one (or switch an existing ' +
                'one to <strong>Generic</strong>) and make it Active — then it can be picked ' +
                "here. Agent flows are not offered: those are a chatbot's own conversation.</p>";
        }

        const select = '<select class="form-select form-select-sm" data-run-flow-select>' +
            '<option value="">Select a flow&hellip;</option>' +
            flows.map(function (f) {
                return '<option value="' + escapeAttr(f.id) + '"' +
                    (f.id === draft.flow_id ? " selected" : "") + ">" +
                    escapeHtml(f.label) + "</option>";
            }).join("") + "</select>";

        return fieldHtml("Flow to run", select) +
            '<label class="form-label fw-semibold small mt-2">Values passed in</label>' +
            '<div id="fbFlowInputs"></div>' +
            '<button type="button" class="btn btn-outline-secondary btn-sm mt-1" id="fbAddFlowInputBtn">' +
            '<i class="las la-plus"></i> Add a value</button>' +
            '<label class="form-label fw-semibold small mt-3">Values brought back</label>' +
            '<div id="fbFlowOutputs"></div>' +
            '<p class="text-muted small mb-0">The called flow starts with what you pass in ' +
            "and nothing else, and only the values you name come back — so its own variable " +
            "names stay its own business. An <strong>End Flow</strong> block inside it means " +
            "<em>return</em>, not goodbye: this flow carries on from the block after this " +
            "one. Connect <code>failed</code> — a flow since unpublished or deleted " +
            "otherwise ends the conversation without explanation.</p>";
    }

    // ---------------------------------------------------------------
    // Create File / Download File properties
    //
    // The data-source dropdown is built from the blocks already on this canvas, so it
    // needs nothing from the server — unlike the email and Run Flow panels, whose choices
    // are somebody's saved templates and flows. What that costs is that it cannot know
    // whether a Run Graph block will *actually* return rows, which is a fact about
    // somebody's data rather than about the drawing; that is a run-time refusal down the
    // `error` port, with a sentence naming the block.
    // ---------------------------------------------------------------

    // Blocks whose result a Create File block can write. Run Graph produces rows and AI
    // Fallback produces the small table it answered with — see
    // `engine_service._store_node_result`. Nothing else on this canvas produces anything
    // but strings, which is what the variable source is for.
    const FILE_DATA_BLOCK_TYPES = { run_graph: true, ai_fallback: true };

    const FILE_FORMATS = [
        { value: "csv", label: "CSV" },
        { value: "xlsx", label: "Excel (XLSX)" },
        { value: "txt", label: "Text" },
        { value: "parquet", label: "Parquet" },
    ];

    /**
     * Build the Create File panel: where the rows come from, what to write, what to call it.
     *
     * @param {object} node - the block being edited
     * @param {object} draft
     * @returns {string}
     */
    function createFileFieldsHtml(node, draft) {
        const source = (draft.data || {}).source || "block";

        const sourceSelect =
            '<select class="form-select form-select-sm" data-file-source>' +
            '<option value="block"' + (source === "block" ? " selected" : "") + ">" +
            "A block earlier in this flow</option>" +
            '<option value="variable"' + (source === "variable" ? " selected" : "") + ">" +
            "A variable holding a dataset</option>" +
            "</select>";

        const formatSelect =
            '<select class="form-select form-select-sm" data-field="file_format">' +
            FILE_FORMATS.map(function (f) {
                return '<option value="' + f.value + '"' +
                    (f.value === (draft.file_format || "csv") ? " selected" : "") + ">" +
                    escapeHtml(f.label) + "</option>";
            }).join("") + "</select>";

        return fieldHtml("Where the rows come from", sourceSelect) +
            '<div id="fbFileSource"></div>' +
            fieldHtml("File format", formatSelect) +
            fieldHtml(
                "File name",
                '<input class="form-control form-control-sm" data-field="file_name" ' +
                'placeholder="orders-{{ORDER_REF}}" value="' +
                escapeAttr(draft.file_name || "") + '">'
            ) +
            fieldHtml(
                "Store the file path in variable",
                '<input class="form-control form-control-sm" data-field="variable_name" ' +
                'value="' + escapeAttr(draft.variable_name || "") + '">'
            ) +
            '<p class="text-muted small mb-0">The extension is added for you, so a name ' +
            "cannot promise a format the file is not. Nothing is said to the visitor — add " +
            "a <strong>Download File</strong> block to hand it over, and a " +
            "<strong>Send Message</strong> block if they should be told. Connect " +
            "<code>failed</code>: a block whose data holds no rows otherwise ends the " +
            "conversation without explanation.</p>";
    }

    /**
     * Wire the Create File panel: the source dropdown and the field it swaps in.
     */
    function wireCreateFileFields(node, draft) {
        const sourceEl = propertiesBodyEl.querySelector("[data-file-source]");
        const detailEl = propertiesBodyEl.querySelector("#fbFileSource");

        if (!sourceEl || !detailEl) return;

        draft.data = draft.data || { source: "block", block_id: "", name: "" };

        function renderDetail() {
            if (draft.data.source === "variable") {
                detailEl.innerHTML = fieldHtml(
                    "Variable",
                    '<input class="form-control form-control-sm" data-file-variable ' +
                    'placeholder="ORDER_ROWS" value="' +
                    escapeAttr(draft.data.name || "") + '">'
                ) +
                '<p class="text-muted small">Rows stored as JSON become columns. Anything ' +
                "else is text, which only <strong>Text</strong> can write — an AI answer is " +
                "prose, and there is no honest way to make prose a spreadsheet.</p>";

                const input = detailEl.querySelector("[data-file-variable]");
                input.addEventListener("input", function () {
                    draft.data.name = input.value;
                });
                return;
            }

            // Blocks that can produce rows, excluding this one. Ordered as they appear in
            // state, which is the order they were added — the same reasoning the Run Flow
            // panel's derived rows keep.
            const blocks = state.nodes.filter(function (n) {
                return n.id !== node.id && FILE_DATA_BLOCK_TYPES[n.type];
            });

            if (!blocks.length) {
                detailEl.innerHTML = '<p class="text-muted small">No block on this canvas ' +
                    "produces rows yet. Add a <strong>Run Graph</strong> block (a pipeline's " +
                    "rows) or an <strong>AI Fallback</strong> block (the table it answers " +
                    "with) — or take the data from a variable instead.</p>";
                return;
            }

            detailEl.innerHTML = fieldHtml(
                "Block",
                '<select class="form-select form-select-sm" data-file-block>' +
                '<option value="">Select a block&hellip;</option>' +
                blocks.map(function (n) {
                    return '<option value="' + escapeAttr(n.id) + '"' +
                        (n.id === draft.data.block_id ? " selected" : "") + ">" +
                        escapeHtml(blockLabelById(n.id)) + "</option>";
                }).join("") + "</select>"
            ) +
            '<p class="text-muted small">A Run Graph block\'s rows are re-read in full ' +
            "when the file is written, so the file holds every row rather than the twenty " +
            "the conversation saw.</p>";

            const select = detailEl.querySelector("[data-file-block]");
            select.addEventListener("change", function () {
                draft.data.block_id = select.value;
            });
        }

        sourceEl.addEventListener("change", function () {
            draft.data.source = sourceEl.value;
            renderDetail();
        });

        renderDetail();
    }

    /**
     * Build the Download File panel: which file(s), and whether the visitor sees a button.
     *
     * More than one Create File block may be ticked — a menu offering CSV / XLSX /
     * Parquet, say, each running its own Create File block, can share one Download File
     * block rather than needing one per branch. At hand-over time the engine takes
     * whichever ticked block **ran most recently** in the conversation; see
     * `_step_download_file` in `engine_service.py` for why that is "named", not "wired
     * in": an operator may put other blocks between the two.
     *
     * @param {object} node - the block being edited
     * @param {object} draft
     * @returns {string}
     */
    function downloadFileFieldsHtml(node, draft) {
        const makers = state.nodes.filter(function (n) { return n.type === "create_file"; });

        if (!makers.length) {
            return '<p class="text-muted small mb-0">There is no <strong>Create File</strong> ' +
                "block on this canvas yet. This block hands over a file another block made, " +
                "so add one first.</p>";
        }

        const selected = downloadSourceIds(draft);
        const checklist = makers.map(function (n) {
            const id = "fbDlSrc-" + n.id;
            return '<div class="form-check">' +
                '<input class="form-check-input" type="checkbox" data-download-source ' +
                'value="' + escapeAttr(n.id) + '" id="' + escapeAttr(id) + '"' +
                (selected.indexOf(n.id) !== -1 ? " checked" : "") + ">" +
                '<label class="form-check-label small" for="' + escapeAttr(id) + '">' +
                escapeHtml(blockLabelById(n.id)) + "</label></div>";
        }).join("");

        return fieldHtml("File(s) to hand over", checklist +
            '<p class="text-muted small mt-1 mb-0">Tick every Create File block a visitor ' +
            "might reach on their way here. Whichever one ran most recently in the " +
            "conversation is the file handed over.</p>") +
            fieldHtml(
                "Store the download link in variable",
                '<input class="form-control form-control-sm" data-field="variable_name" ' +
                'placeholder="FILE_URL" value="' + escapeAttr(draft.variable_name || "") + '">'
            ) +
            '<div class="form-check mb-2">' +
            '<input class="form-check-input" type="checkbox" id="fbShowButton" ' +
            'data-download-button' + (draft.show_button ? " checked" : "") + ">" +
            '<label class="form-check-label small fw-semibold" for="fbShowButton">' +
            "Show a download button in the chat</label></div>" +
            '<div id="fbButtonFields"></div>' +
            '<p class="text-muted small mb-0">With the button off this block says nothing ' +
            "— the link goes into the variable, and it is up to you to use it: " +
            "<code>{{FILE_URL}}</code> in a Send Message block, or an Email block. The " +
            "link works for this one conversation and lasts a day.</p>";
    }

    /**
     * Wire the Download File panel: the source block, the button toggle and its fields.
     */
    function wireDownloadFileFields(node, draft) {
        const sourceEls = propertiesBodyEl.querySelectorAll("[data-download-source]");
        const toggleEl = propertiesBodyEl.querySelector("[data-download-button]");
        const fieldsEl = propertiesBodyEl.querySelector("#fbButtonFields");

        Array.prototype.forEach.call(sourceEls, function (checkbox) {
            checkbox.addEventListener("change", function () {
                draft.create_file_node_id = Array.prototype.filter.call(sourceEls, function (c) {
                    return c.checked;
                }).map(function (c) { return c.value; });
            });
        });

        if (!toggleEl || !fieldsEl) return;

        function renderButtonFields() {
            if (!draft.show_button) {
                fieldsEl.innerHTML = "";
                return;
            }

            fieldsEl.innerHTML = fieldHtml(
                "Button text",
                '<input class="form-control form-control-sm" data-button-text ' +
                'placeholder="Download my orders" value="' +
                escapeAttr(draft.button_text || "") + '">'
            ) + fieldHtml(
                "Button colour",
                '<input class="form-control form-control-color form-control-sm" ' +
                'type="color" data-button-colour value="' +
                escapeAttr(draft.button_colour || "#0d6efd") + '">'
            );

            const textEl = fieldsEl.querySelector("[data-button-text]");
            textEl.addEventListener("input", function () {
                draft.button_text = textEl.value;
            });

            const colourEl = fieldsEl.querySelector("[data-button-colour]");
            colourEl.addEventListener("input", function () {
                draft.button_colour = colourEl.value;
            });
        }

        // Its own listener rather than the panel's generic `[data-field]` wiring, which
        // reads `input.value` — for a checkbox that is the string "on" whether it is
        // ticked or not, so a block saved through it would always show a button.
        toggleEl.addEventListener("change", function () {
            draft.show_button = toggleEl.checked;
            renderButtonFields();
        });

        renderButtonFields();
    }

    /** The chosen flow's entry in `opts.flows`, or null. */
    function chosenFlow(draft) {
        return (opts.flows || []).find(function (f) { return f.id === draft.flow_id; }) || null;
    }

    /**
     * Wire the Run Flow panel: the flow dropdown, and the two lists of rows it drives.
     */
    function wireRunFlowFields(node, draft) {
        const selectEl = propertiesBodyEl.querySelector("[data-run-flow-select]");
        const inputsEl = propertiesBodyEl.querySelector("#fbFlowInputs");
        const outputsEl = propertiesBodyEl.querySelector("#fbFlowOutputs");
        const addBtn = document.getElementById("fbAddFlowInputBtn");

        if (!selectEl || !inputsEl || !outputsEl) return;

        draft.inputs = draft.inputs || {};
        draft.outputs = draft.outputs || {};

        const INPUT_SOURCES = [
            { value: "session", label: "A value the conversation collected" },
            { value: "agent", label: "An agent variable (Agents section)" },
            { value: "literal", label: "A fixed value" },
        ];

        function renderInputs() {
            const called = chosenFlow(draft);
            if (!called) {
                inputsEl.innerHTML =
                    '<p class="text-muted small mb-0">Choose a flow to see what it reads.</p>';
                return;
            }

            // The flow's own reads first, then anything added by hand — so the derived rows
            // keep the order they appear in on that flow's canvas, which is information.
            const names = (called.reads || []).slice();
            Object.keys(draft.inputs).forEach(function (name) {
                if (names.indexOf(name) === -1) names.push(name);
            });

            if (!names.length) {
                inputsEl.innerHTML =
                    '<p class="text-muted small mb-0">That flow reads no variables. ' +
                    "Add one by hand if it needs something this page cannot see.</p>";
                return;
            }

            inputsEl.innerHTML = names.map(function (name) {
                const current = draft.inputs[name] || {};
                const sourceSelect =
                    '<select class="form-select form-select-sm" data-input-source="' +
                    escapeAttr(name) + '">' +
                    '<option value="">Leave unset</option>' +
                    INPUT_SOURCES.map(function (source) {
                        return '<option value="' + source.value + '"' +
                            (current.source === source.value ? " selected" : "") + ">" +
                            escapeHtml(source.label) + "</option>";
                    }).join("") + "</select>";

                const valueField = current.source
                    ? '<input class="form-control form-control-sm mt-1" data-input-value="' +
                      escapeAttr(name) + '" placeholder="' +
                      (current.source === "literal" ? "The text to use"
                          : current.source === "agent" ? "COMPANY" : "the variable name here")
                      + '" value="' +
                      escapeAttr(current.source === "literal" ? (current.value || "") : (current.path || "")) +
                      '">'
                    : "";

                return '<div class="border rounded p-2 mb-2">' +
                    '<div class="small fw-semibold">' + escapeHtml(name) + "</div>" +
                    sourceSelect + valueField +
                    "</div>";
            }).join("");

            inputsEl.querySelectorAll("[data-input-source]").forEach(function (select) {
                select.addEventListener("change", function () {
                    const name = select.dataset.inputSource;
                    if (!select.value) {
                        delete draft.inputs[name];
                    } else {
                        draft.inputs[name] = { source: select.value };
                    }
                    renderInputs();
                });
            });

            inputsEl.querySelectorAll("[data-input-value]").forEach(function (input) {
                input.addEventListener("input", function () {
                    const binding = draft.inputs[input.dataset.inputValue];
                    if (!binding) return;
                    if (binding.source === "literal") {
                        binding.value = input.value;
                    } else {
                        binding.path = input.value.trim();
                    }
                });
            });
        }

        function renderOutputs() {
            const called = chosenFlow(draft);
            if (!called) {
                outputsEl.innerHTML =
                    '<p class="text-muted small mb-0">Choose a flow to see what it can hand back.</p>';
                return;
            }
            if (!(called.writes || []).length) {
                outputsEl.innerHTML =
                    '<p class="text-muted small mb-0">That flow stores no values, so it has ' +
                    "nothing to hand back. It can still be run for what it says or does.</p>";
                return;
            }

            outputsEl.innerHTML = (called.writes || []).map(function (name) {
                return '<div class="d-flex align-items-center gap-2 mb-2">' +
                    '<span class="small text-truncate" style="width:40%;" title="' +
                    escapeAttr(name) + '">' + escapeHtml(name) + "</span>" +
                    '<span class="text-muted small">&rarr;</span>' +
                    '<input class="form-control form-control-sm" data-output-for="' +
                    escapeAttr(name) + '" placeholder="keep it as&hellip; (blank = do not)" value="' +
                    escapeAttr(draft.outputs[name] || "") + '">' +
                    "</div>";
            }).join("");

            outputsEl.querySelectorAll("[data-output-for]").forEach(function (input) {
                input.addEventListener("input", function () {
                    const name = input.dataset.outputFor;
                    const value = input.value.trim();
                    if (value) {
                        draft.outputs[name] = value;
                    } else {
                        delete draft.outputs[name];
                    }
                });
            });
        }

        selectEl.addEventListener("change", function () {
            draft.flow_id = selectEl.value;
            // The rows belonged to the old flow's variables, so keeping them would save a
            // mapping the new flow does not have — which the server refuses. Same reason the
            // email panel clears its bindings when the template changes.
            draft.inputs = {};
            draft.outputs = {};
            renderInputs();
            renderOutputs();
        });

        if (addBtn) {
            addBtn.addEventListener("click", function () {
                if (!chosenFlow(draft)) return;
                const name = (window.prompt("Name of the variable that flow reads") || "").trim();
                if (!name || draft.inputs[name]) return;
                draft.inputs[name] = { source: "session" };
                renderInputs();
            });
        }

        renderInputs();
        renderOutputs();
    }

    function runGraphFieldsHtml(draft) {
        const graphs = opts.graphs || [];

        if (!graphs.length) {
            return '<p class="text-muted small mb-0">No published graphs yet — ' +
                'create one in <a href="/graph-designer">Pipelines</a> and ' +
                "publish it, then it can be picked here.</p>";
        }

        const select = '<select class="form-select form-select-sm" data-field="graph_id">' +
            '<option value="">Select a graph&hellip;</option>' +
            graphs.map(function (g) {
                return '<option value="' + escapeAttr(g.id) + '"' +
                    (g.id === draft.graph_id ? " selected" : "") + ">" +
                    escapeHtml(g.label) + "</option>";
            }).join("") + "</select>";

        return fieldHtml("Graph", select) +
            fieldHtml(
                "Store the number of results in",
                '<input class="form-control form-control-sm" data-field="variable_name" value="' +
                escapeAttr(draft.variable_name || "") + '">'
            ) +
            '<p class="text-muted small mb-0">The graph runs as one step and the flow ' +
            "carries on — nothing is said to the visitor unless you say it with a Send " +
            "Message block. The variable holds <strong>how many</strong> rows it found, " +
            "so a later block can use it in a message or branch on it." +
            "<br><br>If the graph contains an <strong>Ask a human</strong> block, its " +
            "question is put to the visitor word for word and the flow waits for their " +
            "reply before going on.</p>";
    }

    /**
     * Build the attached-API-key `<select>` for an AI Fallback node, or a
     * hint message if none are configured.
     * @param {string} current
     * @returns {string}
     */
    function llmApiKeySelectHtml(current) {
        const keys = opts.aiApiKeys || [];
        if (!keys.length) {
            return '<p class="text-muted small mb-0">No AI API keys saved yet — add one in AI Settings.</p>';
        }
        return '<select class="form-select form-select-sm" data-field="llm_api_key_id"><option value="">Select a key&hellip;</option>' +
            keys.map(function (k) {
                return '<option value="' + escapeAttr(k.id) + '"' + (k.id === current ? " selected" : "") + ">" + escapeHtml(k.label) + " (" + escapeHtml(k.provider) + ")</option>";
            }).join("") +
            "</select>";
    }

    /**
     * Build one mode's checkbox checklist — a list of {id, label} entries,
     * ticked when their id is in `draft[field]`, or a hint when the list of
     * candidates is empty. Shared shape for the pipeline and tool config
     * source kinds; see `downloadFileFieldsHtml` for the pattern this mirrors.
     * @param {Array<{id: string, label: string}>} candidates
     * @param {Array<string>} selectedIds
     * @param {string} checkboxAttr - the `data-*` attribute name marking each checkbox
     * @param {string} idPrefix
     * @param {string} emptyHint
     * @returns {string}
     */
    function kbSourceChecklistHtml(candidates, selectedIds, checkboxAttr, idPrefix, emptyHint) {
        if (!candidates.length) {
            return '<p class="text-muted small mb-0">' + emptyHint + "</p>";
        }
        return candidates.map(function (c) {
            const elId = idPrefix + "-" + c.id;
            return '<div class="form-check">' +
                '<input class="form-check-input" type="checkbox" ' + checkboxAttr + ' ' +
                'value="' + escapeAttr(c.id) + '" id="' + escapeAttr(elId) + '"' +
                (selectedIds.indexOf(c.id) !== -1 ? " checked" : "") + ">" +
                '<label class="form-check-label small" for="' + escapeAttr(elId) + '">' +
                escapeHtml(c.label) + "</label></div>";
        }).join("");
    }

    /**
     * Build the knowledge base management panel markup: upload/type-text
     * controls plus the pipeline/tool config checklists, document list,
     * status badge, and train button.
     *
     * Pipelines and tool configs run live on every visitor message and are
     * never embedded into the vector store — unlike uploads and typed text,
     * they are ordinary draft fields saved by the panel's own Save button,
     * not immediately like the rest of this panel. See
     * `_compose_kb_context` in `ai_fallback_service.py`.
     * @param {object} node
     * @param {object} draft
     * @returns {string}
     */
    function knowledgeBasePanelHtml(node, draft) {
        draft.kb_pipeline_ids = draft.kb_pipeline_ids || [];
        draft.kb_tool_config_ids = draft.kb_tool_config_ids || [];

        const pipelineChecklist = kbSourceChecklistHtml(
            opts.graphs || [], draft.kb_pipeline_ids, "data-kb-pipeline", "fbKbPipeline",
            "No published pipelines yet — create one in " +
            '<a href="/graph-designer">Pipelines</a> and publish it, then it can be picked here.'
        );
        const toolConfigChecklist = kbSourceChecklistHtml(
            opts.toolConfigs || [], draft.kb_tool_config_ids, "data-kb-tool-config", "fbKbToolConfig",
            "No tool configs yet — set one up first, then it can be picked here."
        );

        return (
            '<div class="border rounded p-2 mb-2 bg-light">' +
            '<p class="text-muted small mb-2">Uploads, typed text, and training below are saved immediately — ' +
            "unlike the fields above, they don't need the flow's Save button. Pipelines and " +
            "tool configs below are the opposite: they're ordinary fields, kept only once " +
            "you save this block.</p>" +
            '<div class="d-flex justify-content-between align-items-center mb-2">' +
            '<span class="small fw-semibold">Knowledge base</span>' +
            '<span class="badge bg-secondary" id="fbKbStatusBadge">untrained</span>' +
            "</div>" +
            '<div class="btn-group btn-group-sm mb-2 w-100" role="group">' +
            '<button type="button" class="btn btn-outline-primary active" data-kb-mode="upload">Upload files</button>' +
            '<button type="button" class="btn btn-outline-primary" data-kb-mode="manual">Type text</button>' +
            '<button type="button" class="btn btn-outline-primary" data-kb-mode="pipeline">From a pipeline</button>' +
            '<button type="button" class="btn btn-outline-primary" data-kb-mode="tool_config">From a tool config</button>' +
            "</div>" +
            '<div id="fbKbUploadMode">' +
            '<input type="file" class="form-control form-control-sm mb-1" id="fbKbFileInput" accept=".pdf,.txt,.docx" multiple>' +
            '<button type="button" class="btn btn-outline-secondary btn-sm w-100" id="fbKbUploadBtn"><i class="las la-upload"></i> Upload</button>' +
            "</div>" +
            '<div id="fbKbManualMode" style="display:none;">' +
            '<input type="text" class="form-control form-control-sm mb-1" id="fbKbManualLabel" placeholder="Label (optional)">' +
            '<textarea class="form-control form-control-sm mb-1" rows="3" id="fbKbManualText" placeholder="Type or paste text here"></textarea>' +
            '<button type="button" class="btn btn-outline-secondary btn-sm w-100" id="fbKbAddTextBtn"><i class="las la-plus"></i> Add text</button>' +
            "</div>" +
            '<div id="fbKbPipelineMode" style="display:none;">' + pipelineChecklist +
            '<p class="text-muted small mt-1 mb-0">Every pipeline ticked here runs on every ' +
            "visitor message and its result is added to what the AI reads — it is never " +
            "stored or embedded.</p></div>" +
            '<div id="fbKbToolConfigMode" style="display:none;">' + toolConfigChecklist +
            '<p class="text-muted small mt-1 mb-0">Every tool config ticked here runs its ' +
            "own stored query on every visitor message and its result is added to what the " +
            "AI reads — it is never stored or embedded.</p></div>" +
            '<div id="fbKbDocList" class="mt-2 small"></div>' +
            '<div id="fbKbMessage" class="small mt-1"></div>' +
            '<button type="button" class="btn btn-primary btn-sm w-100 mt-2" id="fbKbTrainBtn"><i class="las la-brain"></i> Train knowledge base</button>' +
            "</div>"
        );
    }

    /**
     * Build a KnowledgeBaseController URL for a given node.
     * @param {string} nodeId
     * @param {string} [suffix] - path appended after the base knowledge-base URL, e.g. "/upload"
     * @returns {string}
     */
    function kbUrl(nodeId, suffix) {
        return opts.kbBaseUrl + "/" + encodeURIComponent(nodeId) + "/knowledge-base" + (suffix || "/");
    }

    // Success/failure is the HTTP status code (2xx vs. not) — never a
    // same-shaped "status" field in the body, since knowledge-base state
    // itself has its own "status" (untrained/trained/failed) that would
    // otherwise collide with an envelope-level "status": "success".
    /**
     * Fetch a URL and parse its JSON body.
     * @param {string} url
     * @param {object} [fetchOpts] - passed through to fetch()
     * @returns {Promise<{ok: boolean, data: object}>}
     */
    function fetchJson(url, fetchOpts) {
        return fetch(url, fetchOpts).then(function (r) {
            return r.json().then(function (data) {
                return { ok: r.ok, data: data };
            });
        });
    }

    /**
     * Show a status or error message in the knowledge base panel.
     * @param {string} text
     * @param {boolean} isError
     */
    function setKbMessage(text, isError) {
        const el = document.getElementById("fbKbMessage");
        if (!el) return;
        el.textContent = text;
        el.className = "small mt-1 " + (isError ? "text-danger" : "text-muted");
    }

    const KB_STATUS_BADGE_CLASS = { untrained: "bg-secondary", trained: "bg-success", failed: "bg-danger" };

    /**
     * Render a knowledge base's status badge and document list, wiring each
     * document's delete button.
     * @param {string} nodeId
     * @param {object} kbState - knowledge base state as returned by the server
     */
    function renderKbState(nodeId, kbState) {
        const badge = document.getElementById("fbKbStatusBadge");
        if (badge) {
            badge.textContent = kbState.status || "untrained";
            badge.className = "badge " + (KB_STATUS_BADGE_CLASS[kbState.status] || "bg-secondary");
        }
        const listEl = document.getElementById("fbKbDocList");
        if (!listEl) return;
        const docs = kbState.documents || [];
        if (!docs.length) {
            listEl.innerHTML = '<p class="text-muted mb-0">No documents yet.</p>';
            return;
        }
        listEl.innerHTML = docs.map(function (d) {
            const statusBadge = d.extraction_status === "error"
                ? '<span class="badge bg-danger" title="' + escapeAttr(d.error_message || "") + '">error</span>'
                : '<span class="badge bg-light text-dark border">' + escapeHtml(d.extraction_status) + "</span>";
            return (
                '<div class="d-flex justify-content-between align-items-center border-bottom py-1">' +
                '<span class="text-truncate" style="max-width: 60%;" title="' + escapeAttr(d.label) + '">' + escapeHtml(d.label) + "</span>" +
                '<span class="d-flex align-items-center gap-1">' + statusBadge +
                '<button type="button" class="btn btn-sm btn-link text-danger p-0" data-doc-delete="' + escapeAttr(d.id) + '"><i class="las la-trash"></i></button>' +
                "</span></div>"
            );
        }).join("");

        listEl.querySelectorAll("[data-doc-delete]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                fetchJson(kbUrl(nodeId, "/documents/" + btn.dataset.docDelete + "/delete"), { method: "POST" })
                    .then(function (result) {
                        if (!result.ok) { setKbMessage(result.data.message || "Could not delete document.", true); return; }
                        loadKbState(nodeId);
                    })
                    .catch(function () { setKbMessage("Could not reach the server.", true); });
            });
        });
    }

    /**
     * Fetch and render a node's current knowledge base state.
     * @param {string} nodeId
     */
    function loadKbState(nodeId) {
        fetchJson(kbUrl(nodeId))
            .then(function (result) {
                if (!result.ok) { setKbMessage(result.data.message || "Could not load the knowledge base.", true); return; }
                renderKbState(nodeId, result.data);
            })
            .catch(function () { setKbMessage("Could not reach the server.", true); });
    }

    /**
     * Wire up all interactive behavior for an AI Fallback node's properties
     * form: the context-source and LLM-mode toggles, the upload/manual-text
     * mode switch, and the knowledge base upload/add-text/train actions.
     * @param {object} node
     * @param {object} draft
     */
    function wireAiFallbackFields(node, draft) {
        const contextSelect = document.getElementById("fbContextSourceSelect");
        const kbPanel = document.getElementById("fbKbPanel");
        contextSelect.addEventListener("change", function () {
            const showKb = contextSelect.value === "knowledge_base";
            kbPanel.style.display = showKb ? "" : "none";
            if (showKb) loadKbState(node.id);
        });

        const llmModeSelect = document.getElementById("fbLlmModeSelect");
        const llmKeyField = document.getElementById("fbLlmKeyField");
        llmModeSelect.addEventListener("change", function () {
            llmKeyField.style.display = llmModeSelect.value === "attached" ? "" : "none";
        });

        const kbModes = {
            upload: { btn: '[data-kb-mode="upload"]', div: "fbKbUploadMode" },
            manual: { btn: '[data-kb-mode="manual"]', div: "fbKbManualMode" },
            pipeline: { btn: '[data-kb-mode="pipeline"]', div: "fbKbPipelineMode" },
            tool_config: { btn: '[data-kb-mode="tool_config"]', div: "fbKbToolConfigMode" },
        };
        Object.keys(kbModes).forEach(function (key) {
            kbModes[key].btnEl = propertiesBodyEl.querySelector(kbModes[key].btn);
            kbModes[key].divEl = document.getElementById(kbModes[key].div);
        });
        Object.keys(kbModes).forEach(function (key) {
            kbModes[key].btnEl.addEventListener("click", function () {
                Object.keys(kbModes).forEach(function (other) {
                    kbModes[other].btnEl.classList.toggle("active", other === key);
                    kbModes[other].divEl.style.display = other === key ? "" : "none";
                });
            });
        });

        const pipelineCheckboxEls = propertiesBodyEl.querySelectorAll("[data-kb-pipeline]");
        Array.prototype.forEach.call(pipelineCheckboxEls, function (cb) {
            cb.addEventListener("change", function () {
                draft.kb_pipeline_ids = Array.prototype.filter.call(pipelineCheckboxEls, function (c) {
                    return c.checked;
                }).map(function (c) { return c.value; });
            });
        });

        const toolConfigCheckboxEls = propertiesBodyEl.querySelectorAll("[data-kb-tool-config]");
        Array.prototype.forEach.call(toolConfigCheckboxEls, function (cb) {
            cb.addEventListener("change", function () {
                draft.kb_tool_config_ids = Array.prototype.filter.call(toolConfigCheckboxEls, function (c) {
                    return c.checked;
                }).map(function (c) { return c.value; });
            });
        });

        document.getElementById("fbKbUploadBtn").addEventListener("click", function () {
            const input = document.getElementById("fbKbFileInput");
            if (!input.files.length) return;
            const formData = new FormData();
            for (let i = 0; i < input.files.length; i++) formData.append("files", input.files[i]);
            setKbMessage("Uploading…", false);
            fetchJson(kbUrl(node.id, "/upload"), { method: "POST", body: formData })
                .then(function (result) {
                    if (!result.ok) { setKbMessage(result.data.message || "Upload failed.", true); return; }
                    input.value = "";
                    const errors = (result.data.results || []).filter(function (r) { return r.status === "error"; });
                    setKbMessage(
                        errors.length
                            ? errors.map(function (e) { return e.original_filename + ": " + e.message; }).join("; ")
                            : "Uploaded.",
                        errors.length > 0
                    );
                    loadKbState(node.id);
                })
                .catch(function () { setKbMessage("Could not reach the server.", true); });
        });

        document.getElementById("fbKbAddTextBtn").addEventListener("click", function () {
            const label = document.getElementById("fbKbManualLabel").value;
            const text = document.getElementById("fbKbManualText").value;
            if (!text.trim()) return;
            setKbMessage("Saving…", false);
            fetchJson(kbUrl(node.id, "/manual-text"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ label: label, text: text }),
            })
                .then(function (result) {
                    if (!result.ok) { setKbMessage(result.data.message || "Could not save text.", true); return; }
                    document.getElementById("fbKbManualLabel").value = "";
                    document.getElementById("fbKbManualText").value = "";
                    setKbMessage("Added.", false);
                    loadKbState(node.id);
                })
                .catch(function () { setKbMessage("Could not reach the server.", true); });
        });

        document.getElementById("fbKbTrainBtn").addEventListener("click", function () {
            setKbMessage("Training…", false);
            fetchJson(kbUrl(node.id, "/train"), { method: "POST" })
                .then(function (result) {
                    if (!result.ok) { setKbMessage(result.data.message || "Training failed.", true); return; }
                    renderKbState(node.id, result.data);
                    setKbMessage(
                        result.data.status === "failed"
                            ? (result.data.error_message || "Training failed — check each document's status below.")
                            : "Training complete.",
                        result.data.status === "failed"
                    );
                })
                .catch(function () { setKbMessage("Could not reach the server.", true); });
        });

        if (draft.context_source === "knowledge_base") {
            loadKbState(node.id);
        }
    }

    // ---------------------------------------------------------------
    // Palette
    // ---------------------------------------------------------------

    /**
     * Render the node-type palette buttons and wire them to add nodes on
     * click.
     */
    function renderPalette() {
        paletteBodyEl.innerHTML = Object.keys(NODE_TYPES).map(function (type) {
            const meta = NODE_TYPES[type];
            return (
                '<button type="button" class="btn btn-outline-primary fb-palette-btn" data-add-type="' + type + '">' +
                '<i class="las ' + meta.icon + '"></i> ' + escapeHtml(meta.label) +
                "</button>"
            );
        }).join("");

        paletteBodyEl.querySelectorAll("[data-add-type]").forEach(function (btn) {
            btn.addEventListener("click", function () { addNode(btn.dataset.addType); });
        });
        updatePaletteAvailability();
    }

    /**
     * Disable the Start palette button once a Start node already exists in
     * the graph — a flow may only have one.
     */
    function updatePaletteAvailability() {
        const hasStart = state.nodes.some(function (n) { return n.type === "start"; });
        const startBtn = paletteBodyEl.querySelector('[data-add-type="start"]');
        if (startBtn) startBtn.disabled = hasStart;
    }

    /**
     * Add a new node of the given type to the graph at an auto-computed
     * grid position.
     * @param {string} type
     */
    function addNode(type) {
        if (type === "start" && state.nodes.some(function (n) { return n.type === "start"; })) return;

        const node = {
            id: genId("n"),
            type: type,
            position: placementForNewBlock(),
            data: defaultData(type),
        };
        state.nodes.push(node);
        renderNode(node);
        updatePaletteAvailability();
        wiringChanged();
    }

    /**
     * Where a block being added should first appear.
     *
     * Under whichever block is selected, or under the whole drawing when none is — which is
     * where somebody adding a step to the end of a flow is looking. It replaces a fixed
     * stagger (`x = 40 + (count % 6) * 40`) that put every sixth block back at the left
     * margin and every one of them 40px from its neighbour, which is the single reason a
     * canvas nobody had dragged into shape was unreadable.
     *
     * In `auto` this position lasts a moment: the arrange that follows moves the block to
     * wherever its connections put it. It is worth getting right anyway — a block that
     * appeared at the origin and jumped would read as a glitch.
     *
     * @returns {{x: number, y: number}}
     */
    function placementForNewBlock() {
        const m = canvasMetrics();
        const below = findNode(state.selectedNodeId) || lowestNode();

        if (!below) return { x: m.marginX, y: m.marginY };

        const el = document.getElementById("node-" + below.id);
        const height = el ? el.getBoundingClientRect().height : m.rowGap;

        return { x: below.position.x, y: below.position.y + height + m.rowGap };
    }

    /** The block furthest down the canvas, or null on an empty one. */
    function lowestNode() {
        return state.nodes.reduce(function (lowest, node) {
            const y = (node.position || {}).y || 0;
            return !lowest || y > ((lowest.position || {}).y || 0) ? node : lowest;
        }, null);
    }

    // ---------------------------------------------------------------
    // Save / load
    // ---------------------------------------------------------------

    /**
     * Replace the in-browser graph with the given nodes/edges, clear
     * selection, and re-render.
     * @param {object} graphData - {nodes, edges} as returned by the server
     */
    function loadGraph(graphData) {
        state.nodes = (graphData.nodes || []).map(function (n) {
            return { id: n.id, type: n.type, position: { x: (n.position || {}).x || 0, y: (n.position || {}).y || 0 }, data: n.data || {} };
        });
        state.edges = (graphData.edges || []).map(function (e) {
            const edge = {
                id: e.id || genId("e"), source: e.source,
                source_port: e.source_port || "default", target: e.target,
            };
            // Sanitised on the way in as well as on the way out. This loader is the one
            // place that decides what a connector *is* in this canvas, so a stored document
            // with a malformed bend — hand-edited, or written by an older client — draws as
            // an unbent wire rather than throwing inside a render.
            const bends = GC.readWaypoints(e.waypoints, MAX_EDGE_WAYPOINTS);
            if (bends.length) edge.waypoints = bends;
            return edge;
        });
        // Missing means auto, which is every flow saved before the canvas could arrange
        // itself — and those are exactly the ones that need it.
        state.layout = graphData.layout === "manual" ? "manual" : "auto";
        state.backEdges = {};
        state.selectedNodeId = null;
        state.selectedEdgeId = null;
        // Cleared, not kept: the graph has just been replaced from the server, and a
        // surviving selection would hold ids that resolve to nothing — which a group move
        // would then try to drag.
        // Emptied in place rather than replaced: the controller holds a reference to this
        // object, and swapping it for a new one would leave it watching the old.
        state.selection.nodes = {};
        state.selection.edges = {};
        renderAllNodes();
        renderAllEdges();
        updatePaletteAvailability();
        updateLayoutButton();
        selection.repaint();
        clearDirty();

        // Straight away rather than debounced: this is the arrange that makes a stored
        // drawing readable, and waiting would show the old positions first.
        requestLayout();
    }

    /**
     * Build the plain nodes/edges payload sent to the server on Save.
     * @returns {{nodes: Array<object>, edges: Array<object>}}
     */
    function serializeGraph() {
        return {
            nodes: state.nodes.map(function (n) { return { id: n.id, type: n.type, position: n.position, data: n.data || {} }; }),
            edges: state.edges.map(function (e) {
                const edge = {
                    id: e.id, source: e.source, source_port: e.source_port, target: e.target,
                };
                // Only when there are any, so a drawing nobody hand-routed saves exactly the
                // document it always did.
                if (GC.waypointsOf(e).length) edge.waypoints = e.waypoints;
                return edge;
            }),
            // Whether this canvas arranges itself. Stored with the drawing because it is a
            // fact about the drawing — an operator who arranged it by hand should find it
            // that way next time, and one who never did should not have to keep pressing
            // Tidy up. The save schema allows extra keys for exactly this
            // (`FlowGraphSaveRequest`), so nothing server-side had to change to keep it.
            layout: state.layout,
        };
    }

    /**
     * POST the current graph to the server and render its response,
     * clearing the dirty flag on success.
     */
    function save() {
        const responseEl = document.getElementById(opts.responseTargetId);
        fetch(opts.saveUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(serializeGraph()),
        })
            .then(function (r) { return r.text(); })
            .then(function (html) {
                responseEl.innerHTML = html;
                const successEl = responseEl.querySelector('[data-success="true"]');
                if (successEl) clearDirty();
            })
            .catch(function () {
                responseEl.innerHTML = '<div class="alert alert-danger">Could not reach the server. Please try again.</div>';
            });
    }

    /**
     * Refetch the graph from the server, discarding any unsaved local
     * changes.
     */
    function reload() {
        fetch(opts.graphUrl)
            .then(function (r) { return r.json(); })
            .then(function (data) { loadGraph(data); })
            .catch(function () {
                document.getElementById(opts.responseTargetId).innerHTML =
                    '<div class="alert alert-danger">Could not reload the flow.</div>';
            });
    }

    // ---------------------------------------------------------------
    // Escaping helpers
    // ---------------------------------------------------------------

    /**
     * HTML-escape a string for safe interpolation into innerHTML.
     * @param {*} str
     * @returns {string}
     */
    function escapeHtml(str) {
        return GC.escapeHtml(str);
    }
    /**
     * HTML-escape a string for safe interpolation into a quoted HTML
     * attribute.
     * @param {*} str
     * @returns {string}
     */
    function escapeAttr(str) { return GC.escapeAttr(str); }
    /**
     * Escape a string for safe use inside a CSS attribute-selector value.
     * @param {*} str
     * @returns {string}
     */
    function cssEscape(str) {
        return GC.cssEscape(str);
    }

    // ---------------------------------------------------------------
    // Init
    // ---------------------------------------------------------------

    /**
     * Entry point: wire up DOM references, render the palette, load the
     * initial graph, and bind the toolbar Save/Reload buttons.
     * @param {object} userOpts - {graphData, saveUrl, graphUrl, responseTargetId, kbBaseUrl, aiApiKeys}
     */
    function init(userOpts) {
        opts = userOpts;
        canvasEl = document.getElementById("flow-canvas");
        edgesSvgEl = document.getElementById("flow-edges");
        edgesGroupEl = document.getElementById("flow-edges-group");
        wrapperEl = document.getElementById("flow-canvas-wrapper");
        paletteBodyEl = document.getElementById("fbPaletteBody");
        propertiesBodyEl = document.getElementById("fbPropertiesBody");

        // First, because `edgeRoute` — which almost everything below reaches through —
        // measures its anchors with it.
        edges = window.GraphEdges.create({
            state: state,
            wrapperEl: wrapperEl,
            chromePrefix: "fb",
            // The ✕ and the + sit on the line's midpoint here. The Graph Designer drops
            // them 10px, because its node labels sit under the disc and would collide.
            chromeYOffset: 0,
            buttonGapPx: EDGE_BTN_GAP_PX,
            nodeElementId: function (id) { return "node-" + id; },
            edgeElementId: function (id) { return "edge-group-" + id; },
            edgePathId: function (id) { return "edge-" + id; },
            // Derived Goto jumps are drawn, so a move of either end has to repaint them —
            // which is why this is `drawableEdges()` and not `state.edges`.
            getDrawableEdges: drawableEdges,
            edgeRoute: edgeRoute,
            renderEdge: renderEdge,
            metrics: canvasMetrics,
            // Both ends of a connector, as this canvas measures them. The `|| null` fallback
            // is the Flow Builder's: a block whose port element is missing still anchors on
            // its own box rather than dropping the connector.
            sourceAnchor: function (edge) {
                return edges.portAnchor(edge.source, sourceSelectorFor(edge)) ||
                    edges.portAnchor(edge.source, null);
            },
            targetAnchor: function (edge) {
                return edges.portAnchor(edge.target, '[data-port-role="in"]') ||
                    edges.portAnchor(edge.target, null);
            },
            // A derived Goto jump is not a connector in its own right, so it cannot be bent.
            isRoutable: function (edge) { return !edge.derived; },
            // No `state.connecting` here: this canvas has no drag-from-port gesture.
            isBusy: function () { return !!(state.reattaching || state.dragging); },
            // Chebyshev distance, which is what this canvas has always used. The Graph
            // Designer uses Manhattan. Preserved per canvas rather than unified.
            travelled: function (dx, dy) { return Math.max(Math.abs(dx), Math.abs(dy)); },
            thresholdPx: DRAG_THRESHOLD,
            maxWaypoints: MAX_EDGE_WAYPOINTS,
            waypointGrabPx: WAYPOINT_GRAB_PX,
            waypointSnapPx: WAYPOINT_SNAP_PX,
            waypointDiscardPx: WAYPOINT_DISCARD_PX,
            flash: noteLayoutFailure,
            markDirty: markDirty,
            updateLayoutButton: updateLayoutButton,
            fitCanvas: fitCanvas,
            detachNodeDrag: function () {
                document.removeEventListener("mousemove", onDragMove);
                document.removeEventListener("mouseup", onDragEnd);
            },
            // Late-bound: the selection is built after this, and the bend gesture only asks
            // for it when a press arrives.
            getSelection: function () { return selection; },
        });

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
            // `state.edges` rather than `drawableEdges()`: a Goto's jump is derived, and it
            // is changed by editing the Goto block. Offering to select one would offer
            // something that cannot be acted on.
            getSelectableEdges: function () { return state.edges; },
            edgeRoute: edgeRoute,
            nodeWidth: function () { return canvasMetrics().stepWidth; },
            threshold: DRAG_THRESHOLD,
            // No box while another gesture owns the mouse. `state.pending` is the one that
            // matters: a click on empty canvas is the documented way out of an armed port,
            // and turning that press into a box would strand the operator there.
            isBusy: function () {
                return !!(state.pending || state.reattaching || state.dragging);
            },
            classes: { node: "fb-node-multi", edge: "fb-edge-multi" },
            // The header's Select all / Clear (n) button is the selection's own, so the
            // selection keeps it in step — see `paintSelectAllButton`.
            selectAllButtonId: "fbSelectAllBtn",
            selectAllNoun: "block and connector",
            onSelectionChange: onSelectionChange,
            onGroupMoveBegin: edges.onGroupMoveBegin,
            onGroupMoveFrame: edges.onGroupMoveFrame,
            onGroupMoveEnd: edges.onGroupMoveEnd,
            onEscape: function () {
                if (!state.pending) return false;
                cancelPending();
                return true;
            },
        });
        selection.attach();

        // The "+" menu on a connector. Built after the selection controller only because
        // both need the canvas elements; they are independent of one another.
        insertMenu = window.GraphInsert.create({
            wrapperEl: wrapperEl,
            getChoices: insertableTypes,
            onChoose: insertOnEdge,
            emptyMessage: "No block can be inserted into a connector on its own — the ones " +
                "that could either have no way out yet or need their options first.",
        });

        renderPalette();
        loadGraph(opts.graphData || { nodes: [], edges: [] });

        wrapperEl.addEventListener("click", function (e) {
            // The click that trails a box or a group move has the wrapper as its target,
            // and without this it would clear the selection the gesture just made — the
            // feature would appear to do nothing at all.
            if (selection.swallowedClick()) return;
            if (e.target === wrapperEl || e.target === canvasEl) {
                cancelPending();
                deselectAll();
                selection.clear();
            }
        });

        document.getElementById("fbSaveBtn").addEventListener("click", save);
        document.getElementById("fbReloadBtn").addEventListener("click", reload);
        document.getElementById("fbTidyBtn").addEventListener("click", tidyUp);
        document.getElementById("fbSelectAllBtn").addEventListener("click", function () {
            if (selection.count()) selection.clear();
            else selection.selectAll();
            // The canvas has to be focused for Ctrl+A and Escape to reach it, and somebody
            // who has just pressed this button is about to want both.
            wrapperEl.focus();
        });
        selection.repaint();

        // A step's height depends on its font, and the layout stacks layers by measured
        // height — so a zoom or a font swap invalidates the cached spacing along with it.
        window.addEventListener("resize", function () {
            canvasMetricsCache = null;
            syncLayout();
        });
    }

    return { init: init };
})();
