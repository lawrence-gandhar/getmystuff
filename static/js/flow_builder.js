/**
 * Flow Builder canvas — vanilla JS + inline SVG connectors, no external
 * graph library. Renders a node/edge graph, supports drag-to-move,
 * click-source-port-then-click-target connection creation, a right-side
 * properties panel per node type, and JSON save/load against the
 * FlowBuilderController routes.
 */
window.FlowBuilder = (function () {
    "use strict";

    var NODE_TYPES = {
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
        end: { label: "End Flow", icon: "la-flag-checkered", outputs: function () { return []; } },
    };

    var state = {
        nodes: [],
        edges: [],
        selectedNodeId: null,
        selectedEdgeId: null,
        pending: null, // {nodeId, port}
        dragging: null, // {nodeId, offsetX, offsetY}
        nextIdSeq: 1,
    };

    var opts = {};
    var canvasEl, edgesGroupEl, wrapperEl, paletteBodyEl, propertiesBodyEl;

    function genId(prefix) {
        var id = prefix + "_" + Date.now().toString(36) + "_" + (state.nextIdSeq++);
        return id;
    }

    function defaultData(type) {
        switch (type) {
            case "if_else": return { variable_name: "", operator: "not_empty", compare_value: "" };
            case "goto": return { target_node_id: "" };
            case "menu": case "dropdown": return { prompt_text: "", options: [] };
            case "ask_input": return { prompt_text: "", variable_name: "" };
            case "send_message": return { message_text: "" };
            case "end": return { message_text: "" };
            default: return {};
        }
    }

    function findNode(id) {
        for (var i = 0; i < state.nodes.length; i++) {
            if (state.nodes[i].id === id) return state.nodes[i];
        }
        return null;
    }

    function nodePreviewText(node) {
        var d = node.data || {};
        switch (node.type) {
            case "if_else": return (d.variable_name || "?") + " " + (d.operator || "") + " " + (d.compare_value || "");
            case "goto": return "→ " + (d.target_node_id || "(unset)");
            case "menu": case "dropdown": return d.prompt_text || "(no prompt)";
            case "ask_input": return d.prompt_text || "(no prompt)";
            case "send_message": return d.message_text || "(empty message)";
            case "ai_fallback": return "Hands off to AI answering";
            case "end": return d.message_text ? "Ends flow: " + d.message_text : "Ends the flow (no closing message)";
            default: return "";
        }
    }

    // ---------------------------------------------------------------
    // Rendering — nodes
    // ---------------------------------------------------------------

    function renderAllNodes() {
        canvasEl.innerHTML = "";
        state.nodes.forEach(renderNode);
    }

    var OPTION_NODE_TYPES = { menu: true, dropdown: true };

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

    function renderNode(node) {
        var meta = NODE_TYPES[node.type] || { label: node.type, icon: "la-question-circle", outputs: function () { return []; } };
        var el = document.createElement("div");
        el.className = "flow-node";
        el.id = "node-" + node.id;
        el.dataset.nodeId = node.id;
        el.style.left = (node.position.x || 0) + "px";
        el.style.top = (node.position.y || 0) + "px";

        var isOptionsType = !!OPTION_NODE_TYPES[node.type];
        var data = node.data || {};

        var bodyHtml = '<div class="flow-node-body" data-role="select-body">' +
            '<div class="flow-node-preview">' + escapeHtml(isOptionsType ? (data.prompt_text || "(no prompt)") : nodePreviewText(node)) + "</div>" +
            (isOptionsType ? '<div class="flow-node-options">' + optionRowsHtml(data.options) + "</div>" : "") +
            "</div>";

        // Options types render one connector div per option (above); every
        // other multi/single-output type keeps the compact port sidebar.
        var outPortsHtml = "";
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
        var deleteBtn = el.querySelector('[data-role="delete-node"]');
        if (deleteBtn) {
            deleteBtn.addEventListener("click", function (e) {
                e.stopPropagation();
                deleteNode(node.id);
            });
        }
        var inPort = el.querySelector('[data-port-role="in"]');
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

    function updateNodeSelectionClass(nodeId) {
        var el = document.getElementById("node-" + nodeId);
        if (!el) return;
        el.classList.toggle("fb-node-selected", state.selectedNodeId === nodeId);
    }

    // ---------------------------------------------------------------
    // Rendering — edges
    // ---------------------------------------------------------------

    function portAnchor(nodeId, portSelector) {
        var nodeEl = document.getElementById("node-" + nodeId);
        if (!nodeEl) return null;
        var portEl = portSelector ? nodeEl.querySelector(portSelector) : nodeEl;
        var target = portEl || nodeEl;
        var wrapperRect = wrapperEl.getBoundingClientRect();
        var rect = target.getBoundingClientRect();
        return {
            x: rect.left + rect.width / 2 - wrapperRect.left + wrapperEl.scrollLeft,
            y: rect.top + rect.height / 2 - wrapperRect.top + wrapperEl.scrollTop,
        };
    }

    function edgePathD(edge) {
        var from = portAnchor(edge.source, '.flow-node-port-out[data-port="' + cssEscape(edge.source_port || "default") + '"]');
        var to = portAnchor(edge.target, '[data-port-role="in"]') || portAnchor(edge.target, null);
        if (!from || !to) return "";
        var dx = Math.max(40, Math.abs(to.x - from.x) / 2);
        return "M " + from.x + " " + from.y + " C " + (from.x + dx) + " " + from.y + ", " + (to.x - dx) + " " + to.y + ", " + to.x + " " + to.y;
    }

    function renderAllEdges() {
        edgesGroupEl.innerHTML = "";
        state.edges.forEach(renderEdge);
    }

    function renderEdge(edge) {
        var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("id", "edge-" + edge.id);
        path.setAttribute("d", edgePathD(edge));
        if (state.selectedEdgeId === edge.id) path.classList.add("fb-edge-selected");
        path.addEventListener("click", function () { selectEdge(edge.id); });
        edgesGroupEl.appendChild(path);
    }

    function redrawEdgesForNode(nodeId) {
        state.edges.forEach(function (edge) {
            if (edge.source === nodeId || edge.target === nodeId) {
                var pathEl = document.getElementById("edge-" + edge.id);
                if (pathEl) pathEl.setAttribute("d", edgePathD(edge));
            }
        });
    }

    // ---------------------------------------------------------------
    // Dragging
    // ---------------------------------------------------------------

    function startDrag(nodeId, e) {
        e.preventDefault();
        var node = findNode(nodeId);
        var wrapperRect = wrapperEl.getBoundingClientRect();
        var startX = e.clientX + wrapperEl.scrollLeft - wrapperRect.left;
        var startY = e.clientY + wrapperEl.scrollTop - wrapperRect.top;
        state.dragging = {
            nodeId: nodeId,
            offsetX: startX - node.position.x,
            offsetY: startY - node.position.y,
        };
        document.addEventListener("mousemove", onDragMove);
        document.addEventListener("mouseup", onDragEnd);
    }

    function onDragMove(e) {
        if (!state.dragging) return;
        var node = findNode(state.dragging.nodeId);
        if (!node) return;
        var wrapperRect = wrapperEl.getBoundingClientRect();
        var x = e.clientX + wrapperEl.scrollLeft - wrapperRect.left - state.dragging.offsetX;
        var y = e.clientY + wrapperEl.scrollTop - wrapperRect.top - state.dragging.offsetY;
        node.position.x = Math.max(0, x);
        node.position.y = Math.max(0, y);
        var el = document.getElementById("node-" + node.id);
        el.style.left = node.position.x + "px";
        el.style.top = node.position.y + "px";
        redrawEdgesForNode(node.id);
    }

    function onDragEnd() {
        state.dragging = null;
        document.removeEventListener("mousemove", onDragMove);
        document.removeEventListener("mouseup", onDragEnd);
    }

    // ---------------------------------------------------------------
    // Connection creation — click source port, then click target
    // ---------------------------------------------------------------

    function onSourcePortClick(nodeId, port) {
        clearPendingHighlight();
        state.pending = { nodeId: nodeId, port: port };
        var portEl = document.querySelector('#node-' + nodeId + ' .flow-node-port-out[data-port="' + cssEscape(port) + '"]');
        if (portEl) portEl.classList.add("fb-port-pending");
    }

    function onTargetPortClick(nodeId) {
        completePendingConnection(nodeId);
    }

    function onNodeBodyClick(nodeId, e) {
        if (state.pending) {
            e.stopPropagation();
            completePendingConnection(nodeId);
            return;
        }
        selectNode(nodeId);
    }

    function completePendingConnection(targetNodeId) {
        if (!state.pending) return;
        var sourceNodeId = state.pending.nodeId;
        var port = state.pending.port;
        clearPendingHighlight();
        state.pending = null;
        if (sourceNodeId === targetNodeId) return;

        // At most one edge per (source, port) — replace if one exists.
        state.edges = state.edges.filter(function (ed) {
            return !(ed.source === sourceNodeId && ed.source_port === port);
        });
        state.edges.push({ id: genId("e"), source: sourceNodeId, source_port: port, target: targetNodeId });
        renderAllEdges();
    }

    function clearPendingHighlight() {
        document.querySelectorAll(".fb-port-pending").forEach(function (el) {
            el.classList.remove("fb-port-pending");
        });
    }

    function cancelPending() {
        clearPendingHighlight();
        state.pending = null;
    }

    // ---------------------------------------------------------------
    // Selection
    // ---------------------------------------------------------------

    function selectNode(nodeId) {
        var previous = state.selectedNodeId;
        state.selectedEdgeId = null;
        state.selectedNodeId = nodeId;
        if (previous) updateNodeSelectionClass(previous);
        updateNodeSelectionClass(nodeId);
        renderAllEdges();
        renderPropertyForm(findNode(nodeId));
        var offcanvas = bootstrap.Offcanvas.getOrCreateInstance(document.getElementById("propertiesOffcanvas"));
        offcanvas.show();
    }

    function selectEdge(edgeId) {
        if (state.selectedNodeId) updateNodeSelectionClass(state.selectedNodeId);
        state.selectedNodeId = null;
        state.selectedEdgeId = edgeId;
        renderAllEdges();
        renderEdgeProperties(edgeId);
        var offcanvas = bootstrap.Offcanvas.getOrCreateInstance(document.getElementById("propertiesOffcanvas"));
        offcanvas.show();
    }

    function deselectAll() {
        if (state.selectedNodeId) updateNodeSelectionClass(state.selectedNodeId);
        state.selectedNodeId = null;
        state.selectedEdgeId = null;
        renderAllEdges();
    }

    // ---------------------------------------------------------------
    // Delete
    // ---------------------------------------------------------------

    function deleteNode(nodeId) {
        state.nodes = state.nodes.filter(function (n) { return n.id !== nodeId; });
        state.edges = state.edges.filter(function (ed) { return ed.source !== nodeId && ed.target !== nodeId; });
        if (state.selectedNodeId === nodeId) state.selectedNodeId = null;
        renderAllNodes();
        renderAllEdges();
        updatePaletteAvailability();
        propertiesBodyEl.innerHTML = '<p class="text-muted small">Select a node or connector to edit it here.</p>';
    }

    function deleteEdge(edgeId) {
        state.edges = state.edges.filter(function (ed) { return ed.id !== edgeId; });
        state.selectedEdgeId = null;
        renderAllEdges();
        propertiesBodyEl.innerHTML = '<p class="text-muted small">Select a node or connector to edit it here.</p>';
    }

    // ---------------------------------------------------------------
    // Properties panel
    // ---------------------------------------------------------------

    function renderEdgeProperties(edgeId) {
        var edge = state.edges.filter(function (e) { return e.id === edgeId; })[0];
        if (!edge) return;
        propertiesBodyEl.innerHTML =
            '<p class="small text-muted">Connector from <strong>' + escapeHtml(edge.source) + "</strong> (" +
            escapeHtml(edge.source_port) + ') to <strong>' + escapeHtml(edge.target) + "</strong>.</p>" +
            '<button type="button" class="btn btn-outline-danger btn-sm" id="fbDeleteEdgeBtn"><i class="las la-trash"></i> Delete connector</button>';
        document.getElementById("fbDeleteEdgeBtn").addEventListener("click", function () { deleteEdge(edge.id); });
    }

    function reRenderSingleNode(node) {
        var old = document.getElementById("node-" + node.id);
        if (old) old.remove();
        renderNode(node);
        renderAllEdges();
    }

    function deepClone(obj) {
        return JSON.parse(JSON.stringify(obj || {}));
    }

    function closePropertiesPanel() {
        var offcanvasEl = document.getElementById("propertiesOffcanvas");
        var instance = bootstrap.Offcanvas.getInstance(offcanvasEl);
        if (instance) instance.hide();
        deselectAll();
        propertiesBodyEl.innerHTML = '<p class="text-muted small">Select a node or connector to edit it here.</p>';
    }

    // Edits happen against a private copy of node.data (`draft`) so the
    // canvas is untouched until Save commits it — Cancel just discards
    // `draft` and nothing on the node ever changes.
    function renderPropertyForm(node) {
        if (!node) return;
        var draft = deepClone(node.data);
        var html = '<div class="mb-2"><span class="badge bg-secondary">' + escapeHtml(NODE_TYPES[node.type].label) + "</span></div>";

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
                '<label class="form-label fw-semibold small mt-2">Options</label><div id="fbOptionsList"></div>' +
                '<button type="button" class="btn btn-outline-secondary btn-sm mt-1" id="fbAddOptionBtn"><i class="las la-plus"></i> Add option</button>';
        } else if (node.type === "ask_input") {
            html +=
                fieldHtml("Prompt text", '<textarea class="form-control form-control-sm" rows="2" data-field="prompt_text">' + escapeHtml(draft.prompt_text || "") + "</textarea>") +
                fieldHtml("Store answer in variable", '<input class="form-control form-control-sm" data-field="variable_name" value="' + escapeAttr(draft.variable_name || "") + '">');
        } else if (node.type === "send_message") {
            html += fieldHtml("Message text", '<textarea class="form-control form-control-sm" rows="3" data-field="message_text">' + escapeHtml(draft.message_text || "") + "</textarea>");
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

        document.getElementById("fbSavePropsBtn").addEventListener("click", function () {
            applyDraftAndClose(node, draft);
        });
        document.getElementById("fbCancelPropsBtn").addEventListener("click", function () {
            closePropertiesPanel();
        });
    }

    function applyDraftAndClose(node, draft) {
        // Any option removed from the draft needs its connector dropped too
        // — it can no longer be a valid edge source_port once saved.
        if (node.type === "menu" || node.type === "dropdown") {
            var keptIds = (draft.options || []).map(function (o) { return o.id; });
            state.edges = state.edges.filter(function (ed) {
                return ed.source !== node.id || keptIds.indexOf(ed.source_port) !== -1;
            });
        }

        node.data = draft;
        reRenderSingleNode(node);
        closePropertiesPanel();
    }

    function renderOptionsList(container, draft) {
        var options = draft.options;
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
                var row = input.closest("[data-option-id]");
                var opt = options.filter(function (o) { return o.id === row.dataset.optionId; })[0];
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

    function fieldHtml(label, inputHtml) {
        return '<div class="mb-2"><label class="form-label small fw-semibold">' + escapeHtml(label) + "</label>" + inputHtml + "</div>";
    }

    function operatorSelectHtml(current) {
        var options = [["not_empty", "Is not empty"], ["equals", "Equals"], ["contains", "Contains"]];
        return '<select class="form-select form-select-sm" data-field="operator">' + options.map(function (o) {
            return '<option value="' + o[0] + '"' + (o[0] === current ? " selected" : "") + ">" + o[1] + "</option>";
        }).join("") + "</select>";
    }

    function gotoTargetSelectHtml(node, current) {
        var opts2 = state.nodes.filter(function (n) { return n.id !== node.id; }).map(function (n) {
            return '<option value="' + escapeAttr(n.id) + '"' + (n.id === current ? " selected" : "") + ">" + escapeHtml(NODE_TYPES[n.type].label) + " (" + escapeHtml(n.id) + ")</option>";
        }).join("");
        return '<select class="form-select form-select-sm" data-field="target_node_id"><option value="">Select a node&hellip;</option>' + opts2 + "</select>";
    }

    // ---------------------------------------------------------------
    // Palette
    // ---------------------------------------------------------------

    function renderPalette() {
        paletteBodyEl.innerHTML = Object.keys(NODE_TYPES).map(function (type) {
            var meta = NODE_TYPES[type];
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

    function updatePaletteAvailability() {
        var hasStart = state.nodes.some(function (n) { return n.type === "start"; });
        var startBtn = paletteBodyEl.querySelector('[data-add-type="start"]');
        if (startBtn) startBtn.disabled = hasStart;
    }

    function addNode(type) {
        if (type === "start" && state.nodes.some(function (n) { return n.type === "start"; })) return;
        var count = state.nodes.length;
        var node = {
            id: genId("n"),
            type: type,
            position: { x: 40 + (count % 6) * 40, y: 40 + Math.floor(count / 6) * 160 },
            data: defaultData(type),
        };
        state.nodes.push(node);
        renderNode(node);
        updatePaletteAvailability();
    }

    // ---------------------------------------------------------------
    // Save / load
    // ---------------------------------------------------------------

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
    }

    function serializeGraph() {
        return {
            nodes: state.nodes.map(function (n) { return { id: n.id, type: n.type, position: n.position, data: n.data || {} }; }),
            edges: state.edges.map(function (e) { return { id: e.id, source: e.source, source_port: e.source_port, target: e.target }; }),
        };
    }

    function save() {
        var responseEl = document.getElementById(opts.responseTargetId);
        fetch(opts.saveUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(serializeGraph()),
        })
            .then(function (r) { return r.text(); })
            .then(function (html) { responseEl.innerHTML = html; })
            .catch(function () {
                responseEl.innerHTML = '<div class="alert alert-danger">Could not reach the server. Please try again.</div>';
            });
    }

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

    function escapeHtml(str) {
        var div = document.createElement("div");
        div.textContent = str == null ? "" : String(str);
        return div.innerHTML;
    }
    function escapeAttr(str) { return escapeHtml(str).replace(/"/g, "&quot;"); }
    function cssEscape(str) {
        return String(str).replace(/[^a-zA-Z0-9_-]/g, function (c) { return "\\" + c; });
    }

    // ---------------------------------------------------------------
    // Init
    // ---------------------------------------------------------------

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
