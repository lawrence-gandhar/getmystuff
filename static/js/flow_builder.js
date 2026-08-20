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

    const NODE_TYPES = {
        start: { label: "Start", icon: "la-play-circle", outputs: function () { return [{ port: "default", label: "" }]; } },
        if_else: { label: "If / Else", icon: "la-code-branch", outputs: function () {
            return [{ port: "true", label: "True" }, { port: "false", label: "False" }];
        } },
        goto: { label: "Goto", icon: "la-share", outputs: function () { return []; } },
        menu: { label: "Menu / Buttons", icon: "la-list", outputs: function (data) {
            return (data.options || []).map(function (o) { return { port: o.id, label: o.label }; });
        } },
        dropdown: { label: "Dropdown", icon: "la-caret-square-down", outputs: function (data) {
            return (data.options || []).map(function (o) { return { port: o.id, label: o.label }; });
        } },
        ask_input: { label: "Ask for Input", icon: "la-keyboard", outputs: function () { return [{ port: "default", label: "" }]; } },
        send_message: { label: "Send Message", icon: "la-comment-dots", outputs: function () { return [{ port: "default", label: "" }]; } },
        ai_fallback: { label: "AI Fallback", icon: "la-robot", outputs: function () { return [{ port: "default", label: "" }]; } },
        // Two ports on purpose. A graph that could not run must not leave by the same
        // edge as one that succeeded, or the flow says "all done" about work that never
        // happened. With no `error` edge drawn the engine signs off instead.
        run_graph: { label: "Run Graph", icon: "la-project-diagram", outputs: function () {
            return [{ port: "default", label: "done" }, { port: "error", label: "failed" }];
        } },
        end: { label: "End Flow", icon: "la-flag-checkered", outputs: function () { return []; } },
    };

    const SVG_NS = GC.SVG_NS;

    const state = {
        nodes: [],
        edges: [],
        selectedNodeId: null,
        selectedEdgeId: null,
        pending: null, // {nodeId, port}
        dragging: null, // {nodeId, offsetX, offsetY}
        reattaching: null, // {edgeId, end: "source"|"target"}
        // True whenever the in-browser graph has changed since the last
        // successful Save/Reload — every edit here (node properties, adding/
        // deleting nodes or connectors, dragging) only touches this client-side
        // copy; nothing is live for visitors until the flow-level Save button
        // posts it to the server. Drives the "Unsaved changes" indicator.
        dirty: false,
    };

    let opts = {};
    let canvasEl, edgesGroupEl, wrapperEl, paletteBodyEl, propertiesBodyEl;

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
            case "ai_fallback": return {
                guardrails: "",
                prompt: "",
                context_source: "datasource",
                llm_mode: "in_built",
                llm_api_key_id: "",
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
            case "ai_fallback":
                const ctxLabel = { datasource: "attached datasource", knowledge_base: "knowledge base", prompt: "prompt only" }[d.context_source] || "attached datasource";
                return "AI answers using " + ctxLabel;
            case "end": return d.message_text ? "Ends flow: " + d.message_text : "Ends the flow (no closing message)";
            default: return "";
        }
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
     * Build the HTML for a menu/dropdown node's per-option connector rows.
     * @param {Array<object>} options
     * @returns {string}
     */
    function optionRowsHtml(options) {
        return (options || []).map(function (o) {
            return (
                '<div class="flow-node-option-row">' +
                '<span class="flow-node-option-label">' + escapeHtml(o.label || "(unlabeled)") + "</span>" +
                '<div class="flow-node-port flow-node-port-out" data-port="' + escapeAttr(o.id) + '" title="Drag/click to connect"></div>' +
                "</div>"
            );
        }).join("");
    }

    /**
     * Render a single node into the canvas: builds its DOM element, wires up
     * drag/select/port event listeners, and applies selection styling.
     * @param {object} node
     */
    function renderNode(node) {
        const meta = NODE_TYPES[node.type] || { label: node.type, icon: "la-question-circle", outputs: function () { return []; } };
        const el = document.createElement("div");
        el.className = "flow-node";
        el.id = "node-" + node.id;
        el.dataset.nodeId = node.id;
        el.style.left = (node.position.x || 0) + "px";
        el.style.top = (node.position.y || 0) + "px";

        const isOptionsType = !!OPTION_NODE_TYPES[node.type];
        const data = node.data || {};

        const bodyHtml = '<div class="flow-node-body" data-role="select-body">' +
            '<div class="flow-node-preview">' + escapeHtml(isOptionsType ? (data.prompt_text || "(no prompt)") : nodePreviewText(node)) + "</div>" +
            (isOptionsType ? '<div class="flow-node-options">' + optionRowsHtml(data.options) + "</div>" : "") +
            "</div>";

        // Options types render one connector div per option (above); every
        // other multi/single-output type keeps the compact port sidebar.
        let outPortsHtml = "";
        if (!isOptionsType) {
            outPortsHtml = meta.outputs(data).map(function (o) {
                return (
                    '<div class="flow-node-port-out-row">' +
                    (o.label ? '<span class="flow-node-port-out-label">' + escapeHtml(o.label) + "</span>" : "") +
                    '<div class="flow-node-port flow-node-port-out" data-port="' + escapeAttr(o.port) + '" title="Drag/click to connect"></div>' +
                    "</div>"
                );
            }).join("");
        }

        el.innerHTML =
            '<div class="flow-node-header" data-role="drag-handle">' +
            '<i class="las ' + meta.icon + '"></i>' +
            '<span class="flow-node-title">' + escapeHtml(meta.label) + "</span>" +
            '<div class="flow-node-header-icons">' +
            '<button type="button" class="flow-node-icon-btn" data-role="edit-node" title="Edit"><i class="las la-edit"></i></button>' +
            (node.type === "start" ? "" : '<button type="button" class="flow-node-icon-btn flow-node-icon-btn-danger" data-role="delete-node" title="Delete"><i class="las la-trash"></i></button>') +
            "</div>" +
            "</div>" +
            bodyHtml +
            (node.type === "start" ? "" : '<div class="flow-node-port flow-node-port-in" data-port-role="in" title="Connect a source here"></div>') +
            '<div class="flow-node-ports-out">' + outPortsHtml + "</div>";

        canvasEl.appendChild(el);

        el.querySelector('[data-role="drag-handle"]').addEventListener("mousedown", function (e) {
            startDrag(node.id, e);
        });
        el.querySelector('[data-role="select-body"]').addEventListener("click", function (e) {
            onNodeBodyClick(node.id, e);
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
    function portAnchor(nodeId, portSelector) {
        return GC.portAnchor(
            wrapperEl, document.getElementById("node-" + nodeId), portSelector,
        );
    }

    // An edge's geometry is a cubic Bezier from the source port to the
    // target's input dot. Exposing the control points (not just the path
    // string) lets us place the delete button and reattach handles at
    // specific points along the curve instead of only at its two ends.
    /**
     * Compute an edge's cubic Bezier control points from its source port to
     * its target's input dot.
     * @param {object} edge
     * @returns {{p0: object, p1: object, p2: object, p3: object}|null}
     */
    function edgeGeometry(edge) {
        const from = portAnchor(edge.source, '.flow-node-port-out[data-port="' + cssEscape(edge.source_port || "default") + '"]');
        const to = portAnchor(edge.target, '[data-port-role="in"]') || portAnchor(edge.target, null);
        return GC.geometry(from, to);
    }

    /**
     * Build the SVG path `d` attribute string for a Bezier geometry.
     * @param {object} g - geometry from edgeGeometry()
     * @returns {string}
     */
    function geometryPathD(g) {
        return GC.pathD(g);
    }

    /**
     * Compute the point on a cubic Bezier curve at parameter t.
     * @param {object} g - geometry from edgeGeometry()
     * @param {number} t - 0 (start) to 1 (end)
     * @returns {{x: number, y: number}}
     */
    function bezierPointAt(g, t) {
        return GC.pointAt(g, t);
    }

    /**
     * Clear the edges SVG group and render every edge currently in state.
     */
    function renderAllEdges() {
        edgesGroupEl.innerHTML = "";
        state.edges.forEach(renderEdge);
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
        group.setAttribute("class", "flow-edge-group");
        group.setAttribute("id", "edge-group-" + edge.id);

        const g = edgeGeometry(edge);
        const path = document.createElementNS(SVG_NS, "path");
        path.setAttribute("id", "edge-" + edge.id);
        path.setAttribute("d", g ? geometryPathD(g) : "");
        if (state.selectedEdgeId === edge.id) path.classList.add("fb-edge-selected");
        path.addEventListener("click", function () { selectEdge(edge.id); });
        group.appendChild(path);

        if (g) {
            group.appendChild(buildDeleteButton(edge.id, bezierPointAt(g, 0.5)));
            group.appendChild(buildEndHandle(edge.id, "source", bezierPointAt(g, 0.15)));
            group.appendChild(buildEndHandle(edge.id, "target", bezierPointAt(g, 0.85)));
        }

        edgesGroupEl.appendChild(group);
    }

    // Cheaper than a full renderAllEdges(): updates the path + delete
    // button + reattach handles of edges touching one node in place,
    // without tearing down/recreating DOM (and their listeners).
    /**
     * Update the path, delete button, and reattach handles of an
     * already-rendered edge in place.
     * @param {object} edge
     */
    function updateEdgeGroupGeometry(edge) {
        const group = document.getElementById("edge-group-" + edge.id);
        if (!group) return;
        const g = edgeGeometry(edge);
        const path = document.getElementById("edge-" + edge.id);
        if (path) path.setAttribute("d", g ? geometryPathD(g) : "");
        if (!g) return;

        const delBtn = group.querySelector(".fb-edge-delete-btn");
        if (delBtn) {
            const mid = bezierPointAt(g, 0.5);
            delBtn.setAttribute("transform", "translate(" + mid.x + "," + mid.y + ")");
        }
        const srcHandle = group.querySelector(".fb-edge-handle-source");
        if (srcHandle) {
            const sp = bezierPointAt(g, 0.15);
            srcHandle.setAttribute("cx", sp.x);
            srcHandle.setAttribute("cy", sp.y);
        }
        const tgtHandle = group.querySelector(".fb-edge-handle-target");
        if (tgtHandle) {
            const tp = bezierPointAt(g, 0.85);
            tgtHandle.setAttribute("cx", tp.x);
            tgtHandle.setAttribute("cy", tp.y);
        }
    }

    /**
     * Redraw every edge attached to a node — called after that node moves.
     * @param {string} nodeId
     */
    function redrawEdgesForNode(nodeId) {
        state.edges.forEach(function (edge) {
            if (edge.source === nodeId || edge.target === nodeId) {
                updateEdgeGroupGeometry(edge);
            }
        });
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
        };
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
        const node = findNode(state.dragging.nodeId);
        if (!node) return;
        const wrapperRect = wrapperEl.getBoundingClientRect();
        const x = e.clientX + wrapperEl.scrollLeft - wrapperRect.left - state.dragging.offsetX;
        const y = e.clientY + wrapperEl.scrollTop - wrapperRect.top - state.dragging.offsetY;
        node.position.x = Math.max(0, x);
        node.position.y = Math.max(0, y);
        const el = document.getElementById("node-" + node.id);
        el.style.left = node.position.x + "px";
        el.style.top = node.position.y + "px";
        redrawEdgesForNode(node.id);
    }

    /**
     * Finish a node drag: mark the graph dirty and detach the drag
     * listeners.
     */
    function onDragEnd() {
        if (state.dragging) markDirty();
        state.dragging = null;
        document.removeEventListener("mousemove", onDragMove);
        document.removeEventListener("mouseup", onDragEnd);
    }

    // ---------------------------------------------------------------
    // Move a connector to another node — drag one of its two small end
    // handles (near the source or the target) and drop it on a new spot.
    // Nothing in `state.edges` changes until a valid drop is found; an
    // invalid drop just re-renders from the unchanged edge, snapping the
    // curve back to where it started.
    // ---------------------------------------------------------------

    /**
     * Convert a mouse event's client coordinates into canvas-relative
     * coordinates.
     * @param {MouseEvent} e
     * @returns {{x: number, y: number}}
     */
    function cursorPoint(e) {
        const wrapperRect = wrapperEl.getBoundingClientRect();
        return {
            x: e.clientX + wrapperEl.scrollLeft - wrapperRect.left,
            y: e.clientY + wrapperEl.scrollTop - wrapperRect.top,
        };
    }

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
        const g = edge && edgeGeometry(edge);
        const path = edge && document.getElementById("edge-" + edge.id);
        if (!g || !path) return;

        const cursor = cursorPoint(e);
        const fixed = state.reattaching.end === "target" ? g.p0 : g.p3;
        const moving = cursor;
        const from = state.reattaching.end === "target" ? fixed : moving;
        const to = state.reattaching.end === "target" ? moving : fixed;
        const dx = Math.max(40, Math.abs(to.x - from.x) / 2);
        path.setAttribute("d", "M " + from.x + " " + from.y + " C " + (from.x + dx) + " " + from.y + ", " + (to.x - dx) + " " + to.y + ", " + to.x + " " + to.y);

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
        markDirty();
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
        markDirty();
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
        markDirty();
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
        state.nodes = state.nodes.filter(function (n) { return n.id !== nodeId; });
        state.edges = state.edges.filter(function (ed) { return ed.source !== nodeId && ed.target !== nodeId; });
        if (state.selectedNodeId === nodeId) state.selectedNodeId = null;
        renderAllNodes();
        renderAllEdges();
        updatePaletteAvailability();
        propertiesBodyEl.innerHTML = '<p class="text-muted small">Select a node or connector to edit it here.</p>';
        markDirty();
    }

    /**
     * Remove an edge and re-render.
     * @param {string} edgeId
     */
    function deleteEdge(edgeId) {
        state.edges = state.edges.filter(function (ed) { return ed.id !== edgeId; });
        state.selectedEdgeId = null;
        renderAllEdges();
        propertiesBodyEl.innerHTML = '<p class="text-muted small">Select a node or connector to edit it here.</p>';
        markDirty();
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
        propertiesBodyEl.innerHTML =
            '<p class="small text-muted">Connector from <strong>' + escapeHtml(edge.source) + "</strong> (" +
            escapeHtml(edge.source_port) + ') to <strong>' + escapeHtml(edge.target) + "</strong>.</p>" +
            '<button type="button" class="btn btn-outline-danger btn-sm" id="fbDeleteEdgeBtn"><i class="las la-trash"></i> Delete connector</button>';
        document.getElementById("fbDeleteEdgeBtn").addEventListener("click", function () { deleteEdge(edge.id); });
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
        } else if (node.type === "run_graph") {
            html += runGraphFieldsHtml(draft);
        } else if (node.type === "ai_fallback") {
            html += aiFallbackFieldsHtml(draft);
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
        markDirty();

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
    function aiFallbackFieldsHtml(draft) {
        draft.context_source = draft.context_source || "datasource";
        draft.llm_mode = draft.llm_mode || "in_built";
        return (
            fieldHtml("Guardrails", '<textarea class="form-control form-control-sm" rows="2" data-field="guardrails" placeholder="e.g. Never discuss pricing, stay polite and on-topic">' + escapeHtml(draft.guardrails || "") + "</textarea>") +
            fieldHtml("Prompt / instructions", '<textarea class="form-control form-control-sm" rows="2" data-field="prompt" placeholder="Extra instructions for how the AI should answer">' + escapeHtml(draft.prompt || "") + "</textarea>") +
            fieldHtml("Answer using", contextSourceSelectHtml(draft.context_source)) +
            '<div id="fbKbPanel" style="' + (draft.context_source === "knowledge_base" ? "" : "display:none;") + '">' + knowledgeBasePanelHtml() + "</div>" +
            fieldHtml("Language model", llmModeSelectHtml(draft.llm_mode)) +
            '<div id="fbLlmKeyField" style="' + (draft.llm_mode === "attached" ? "" : "display:none;") + '">' +
            fieldHtml("Attached API key", llmApiKeySelectHtml(draft.llm_api_key_id)) +
            "</div>"
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
    function runGraphFieldsHtml(draft) {
        const graphs = opts.graphs || [];

        if (!graphs.length) {
            return '<p class="text-muted small mb-0">No published graphs yet — ' +
                'create one in the <a href="/graph-designer">Graph Designer</a> and ' +
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
     * Build the knowledge base management panel markup: upload/type-text
     * controls, document list, status badge, and train button.
     * @returns {string}
     */
    function knowledgeBasePanelHtml() {
        return (
            '<div class="border rounded p-2 mb-2 bg-light">' +
            '<p class="text-muted small mb-2">Uploads, typed text, and training below are saved immediately — ' +
            "unlike the fields above, they don't need the flow's Save button.</p>" +
            '<div class="d-flex justify-content-between align-items-center mb-2">' +
            '<span class="small fw-semibold">Knowledge base</span>' +
            '<span class="badge bg-secondary" id="fbKbStatusBadge">untrained</span>' +
            "</div>" +
            '<div class="btn-group btn-group-sm mb-2 w-100" role="group">' +
            '<button type="button" class="btn btn-outline-primary active" data-kb-mode="upload">Upload files</button>' +
            '<button type="button" class="btn btn-outline-primary" data-kb-mode="manual">Type text</button>' +
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

        const uploadModeBtn = propertiesBodyEl.querySelector('[data-kb-mode="upload"]');
        const manualModeBtn = propertiesBodyEl.querySelector('[data-kb-mode="manual"]');
        const uploadModeDiv = document.getElementById("fbKbUploadMode");
        const manualModeDiv = document.getElementById("fbKbManualMode");
        uploadModeBtn.addEventListener("click", function () {
            uploadModeBtn.classList.add("active");
            manualModeBtn.classList.remove("active");
            uploadModeDiv.style.display = "";
            manualModeDiv.style.display = "none";
        });
        manualModeBtn.addEventListener("click", function () {
            manualModeBtn.classList.add("active");
            uploadModeBtn.classList.remove("active");
            manualModeDiv.style.display = "";
            uploadModeDiv.style.display = "none";
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
        const count = state.nodes.length;
        const node = {
            id: genId("n"),
            type: type,
            position: { x: 40 + (count % 6) * 40, y: 40 + Math.floor(count / 6) * 160 },
            data: defaultData(type),
        };
        state.nodes.push(node);
        renderNode(node);
        updatePaletteAvailability();
        markDirty();
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
            return { id: e.id || genId("e"), source: e.source, source_port: e.source_port || "default", target: e.target };
        });
        state.selectedNodeId = null;
        state.selectedEdgeId = null;
        renderAllNodes();
        renderAllEdges();
        updatePaletteAvailability();
        clearDirty();
    }

    /**
     * Build the plain nodes/edges payload sent to the server on Save.
     * @returns {{nodes: Array<object>, edges: Array<object>}}
     */
    function serializeGraph() {
        return {
            nodes: state.nodes.map(function (n) { return { id: n.id, type: n.type, position: n.position, data: n.data || {} }; }),
            edges: state.edges.map(function (e) { return { id: e.id, source: e.source, source_port: e.source_port, target: e.target }; }),
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
        edgesGroupEl = document.getElementById("flow-edges-group");
        wrapperEl = document.getElementById("flow-canvas-wrapper");
        paletteBodyEl = document.getElementById("fbPaletteBody");
        propertiesBodyEl = document.getElementById("fbPropertiesBody");

        renderPalette();
        loadGraph(opts.graphData || { nodes: [], edges: [] });

        wrapperEl.addEventListener("click", function (e) {
            if (e.target === wrapperEl || e.target === canvasEl) {
                cancelPending();
                deselectAll();
            }
        });

        document.getElementById("fbSaveBtn").addEventListener("click", save);
        document.getElementById("fbReloadBtn").addEventListener("click", reload);
    }

    return { init: init };
})();
