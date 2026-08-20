/**
 * Tool Graphs — the canvas beside the workspace tree.
 *
 * Two drawings of the same selection, and the toggle above the canvas switches
 * between them without changing what is selected:
 *
 *   Tool Graph  the chain a nested tool config compiles to. One rectangle per tool,
 *               START and END as rectangles of their own because that is what they
 *               are in the compiled LangGraph, and a connector per link running from
 *               the tool that goes first to the tool it restricts.
 *   SQL Graph   the same tools' joins, one two-circle Venn per join, shaded by type.
 *
 * Positions are not computed here. The server returns a `layer` and a `row` per node
 * (see app/services/tool_graphs/tool_graph_service.py, which explains why: layout is
 * the part of a drawing that can be wrong without looking wrong, and only the Python
 * side of this repository has a test harness). This file multiplies them by a gap and
 * draws.
 *
 * D3 is used for three things and no more — the zoom/pan behaviour, the curve
 * generator for the connectors, and nothing else. Elements are created with
 * `createElementNS`/`createElement` and filled with `textContent`, never with
 * `.html()` or `innerHTML`: every label on this page is a tool, table or column name
 * out of the user's own database, and the rule the rest of this application's
 * JavaScript follows (see tool_configs.js) applies here for the same reason.
 *
 * Conventions copied from tool_chain.js because they are load-bearing: a `data-*`
 * root selector, an idempotency flag so an htmx swap cannot wire the same page twice,
 * JSON through the app's session-aware fetch wrapper, and a failed request that
 * degrades to a readable note instead of blanking the canvas.
 *
 * Nothing here writes. There is no save path, no drag-to-move and no stored position:
 * the picture is derived from the tool configs every time it is drawn, so it cannot
 * fall out of step with them.
 */
(function () {
    "use strict";

    var ROOT_SELECTOR = "[data-tool-graph]";
    var SVG_NS = "http://www.w3.org/2000/svg";

    // Node geometry. A tool box is wide enough for a monospace tool name plus its
    // datasource on a second line; the terminals hold one short word.
    var NODE_WIDTH = 172;
    var NODE_HEIGHT = 56;
    var TERMINAL_WIDTH = 76;
    var COLUMN_GAP = 96;
    var ROW_GAP = 44;
    var CANVAS_PADDING = 32;

    // Venn geometry. The circles overlap by a third of their diameter, which is
    // enough for the lens to be a shape rather than a sliver at this size.
    var VENN_RADIUS = 46;
    var VENN_OVERLAP = 58;
    var VENN_WIDTH = 210;
    var VENN_HEIGHT = 132;

    // Clip paths need document-unique ids and there may be a dozen diagrams on
    // screen. A counter is enough: nothing here outlives the page.
    var clipSeq = 0;

    document.addEventListener("DOMContentLoaded", function () {
        scan(document);
    });

    document.addEventListener("htmx:load", function (event) {
        scan(event.target);
    });

    scan(document);

    /** @param {Document|Element} root */
    function scan(root) {
        if (!root || !root.querySelectorAll) return;

        if (root.matches && root.matches(ROOT_SELECTOR)) init(root);
        Array.prototype.forEach.call(root.querySelectorAll(ROOT_SELECTOR), init);
    }

    /** @param {Element} root */
    function init(root) {
        if (root.dataset.graphReady === "1") return;
        root.dataset.graphReady = "1";

        var state = {
            view: "tool",
            scope: "",
            id: "",
            // Keyed by view + scope + id. Flipping the toggle back and forth is a
            // display change, not a reason to ask the server again.
            cache: {},
        };

        var elements = {
            root: root,
            scope: root.querySelector("[data-graph-scope]"),
            message: root.querySelector("[data-graph-message]"),
            svg: root.querySelector("[data-graph-svg]"),
            joins: root.querySelector("[data-graph-joins]"),
            panes: root.querySelectorAll("[data-graph-pane]"),
        };

        wireTree(root, state, elements);
        wireToggle(root, state, elements);

        var initial = readSelection(root);
        if (initial.scope) {
            select(state, elements, initial.scope, initial.id);
        } else {
            showEmpty(elements, "Pick a workspace, a data agent or a tool on the left.");
        }
    }

    // ----------------------------------------------------------------------
    // Wiring
    // ----------------------------------------------------------------------

    function wireTree(root, state, elements) {
        Array.prototype.forEach.call(
            root.querySelectorAll("[data-graph-select]"),
            function (button) {
                button.addEventListener("click", function () {
                    select(
                        state, elements,
                        button.dataset.scope || "",
                        button.dataset.id || "",
                    );
                });
            },
        );
    }

    function wireToggle(root, state, elements) {
        Array.prototype.forEach.call(
            root.querySelectorAll("[data-graph-view]"),
            function (button) {
                button.addEventListener("click", function () {
                    setView(root, state, elements, button.dataset.graphView);
                });
            },
        );
    }

    /**
     * Read the selection the page was opened with.
     *
     * Defaulted field by field: the block is server-rendered JSON, and a page that
     * threw on a malformed one would lose the tree as well as the canvas.
     */
    function readSelection(root) {
        var block = root.querySelector("[data-graph-selection]");
        var parsed = {};

        try {
            parsed = JSON.parse((block && block.textContent) || "{}");
        } catch (error) {
            parsed = {};
        }

        if (parsed.tool) return { scope: "tool", id: String(parsed.tool) };
        if (parsed.agent) return { scope: "agent", id: String(parsed.agent) };
        if (parsed.workspace) return { scope: "workspace", id: String(parsed.workspace) };

        return { scope: "", id: "" };
    }

    // ----------------------------------------------------------------------
    // Selection and loading
    // ----------------------------------------------------------------------

    function select(state, elements, scope, id) {
        if (!scope || !id) return;

        state.scope = scope;
        state.id = id;

        markSelected(elements.root, scope, id);
        rememberInUrl(scope, id);
        load(state, elements);
    }

    function setView(root, state, elements, view) {
        if (view !== "tool" && view !== "sql") return;

        state.view = view;

        Array.prototype.forEach.call(
            root.querySelectorAll("[data-graph-view]"),
            function (button) {
                var active = button.dataset.graphView === view;
                button.classList.toggle("btn-primary", active);
                button.classList.toggle("btn-outline-primary", !active);
            },
        );

        Array.prototype.forEach.call(elements.panes, function (pane) {
            pane.hidden = pane.dataset.graphPane !== view;
        });

        if (state.scope) load(state, elements);
    }

    function load(state, elements) {
        var key = state.view + ":" + state.scope + ":" + state.id;

        if (state.cache[key]) {
            draw(state, elements, state.cache[key]);
            return;
        }

        var base = state.view === "sql"
            ? elements.root.dataset.joinsUrl
            : elements.root.dataset.graphUrl;

        request(base + "?" + state.scope + "=" + encodeURIComponent(state.id))
            .then(function (payload) {
                state.cache[key] = payload || {};
                draw(state, elements, state.cache[key]);
            })
            .catch(function () {
                // The tree is still usable and the previous drawing is still true of
                // whatever it was drawn from, so nothing is cleared — the note says
                // this selection could not be read and the user can click again.
                showMessage(elements, "That view could not be loaded. Try again.");
            });
    }

    function draw(state, elements, payload) {
        setText(elements.scope, payload.scope_label || "Nothing selected");

        if (payload.error) {
            showMessage(elements, payload.error);
        } else {
            hideMessage(elements);
        }

        if (state.view === "sql") {
            renderJoins(elements, payload);
        } else {
            renderGraph(elements, payload);
        }
    }

    /**
     * Keep the selection in the address bar.
     *
     * `replaceState`, not `pushState`: clicking through a tree is browsing, not
     * navigation, and filling someone's back button with twelve canvas states would
     * make Back stop meaning "the page before this one".
     */
    function rememberInUrl(scope, id) {
        if (!window.history || !window.history.replaceState) return;

        window.history.replaceState(
            {}, "",
            window.location.pathname + "?" + scope + "=" + encodeURIComponent(id),
        );
    }

    function markSelected(root, scope, id) {
        Array.prototype.forEach.call(
            root.querySelectorAll("[data-graph-select]"),
            function (button) {
                button.classList.toggle(
                    "is-selected",
                    button.dataset.scope === scope && button.dataset.id === id,
                );
            },
        );
    }

    // ----------------------------------------------------------------------
    // The tool graph
    // ----------------------------------------------------------------------

    function renderGraph(elements, payload) {
        var svg = elements.svg;
        if (!svg) return;

        svg.textContent = "";

        if (typeof window.d3 === "undefined") {
            showMessage(elements, "The graph library could not be loaded, so the chain cannot be drawn.");
            return;
        }

        var nodes = Array.isArray(payload.nodes) ? payload.nodes : [];
        if (!nodes.length) {
            drawEmptySvg(svg, payload.error
                ? "Nothing to draw."
                : "Pick a workspace, a data agent or a tool on the left.");
            return;
        }

        var placed = place(nodes);
        var byKey = {};
        placed.forEach(function (node) { byKey[node.key] = node; });

        var viewport = createSvg("g");
        svg.appendChild(defsWithArrow());
        svg.appendChild(viewport);

        var edges = Array.isArray(payload.edges) ? payload.edges : [];
        edges.forEach(function (edge) {
            var line = edgeGroup(edge, byKey);
            if (line) viewport.appendChild(line);
        });

        placed.forEach(function (node) {
            viewport.appendChild(nodeGroup(node));
        });

        fitAndZoom(svg, viewport, placed);
    }

    /** Turn each node's (layer, row) into a box with a position and a size. */
    function place(nodes) {
        return nodes.map(function (node) {
            var terminal = node.kind === "start" || node.kind === "end";
            var width = terminal ? TERMINAL_WIDTH : NODE_WIDTH;

            return {
                key: String(node.key || ""),
                kind: node.kind || "tool",
                label: String(node.label || ""),
                datasource: String(node.datasource || ""),
                query_mode: String(node.query_mode || ""),
                agent_name: String(node.agent_name || ""),
                is_enabled: node.is_enabled !== false,
                width: width,
                height: terminal ? NODE_HEIGHT - 16 : NODE_HEIGHT,
                x: (Number(node.layer) || 0) * (NODE_WIDTH + COLUMN_GAP),
                y: (Number(node.row) || 0) * (NODE_HEIGHT + ROW_GAP),
            };
        });
    }

    function nodeGroup(node) {
        var group = createSvg("g");
        group.setAttribute("class", nodeClass(node));
        group.setAttribute("transform", "translate(" + node.x + "," + node.y + ")");

        var box = createSvg("rect");
        box.setAttribute("class", "tool-graph-box");
        box.setAttribute("width", node.width);
        box.setAttribute("height", node.height);
        box.setAttribute("rx", 8);
        group.appendChild(box);

        var terminal = node.kind === "start" || node.kind === "end";
        var centre = node.width / 2;

        group.appendChild(svgText(
            node.label, centre, terminal ? node.height / 2 + 4 : 22, "tool-graph-label",
        ));

        if (!terminal) {
            group.appendChild(svgText(
                subtitle(node), centre, 38, "tool-graph-sublabel",
            ));
        }

        // The full name is on the box whether or not it fitted, so a truncated label
        // is still readable on hover.
        var title = createSvg("title");
        title.textContent = titleFor(node);
        group.appendChild(title);

        return group;
    }

    function nodeClass(node) {
        var classes = ["tool-graph-node-group"];

        if (node.kind === "start" || node.kind === "end") classes.push("is-terminal");
        if (!node.is_enabled) classes.push("is-disabled");

        return classes.join(" ");
    }

    function subtitle(node) {
        var parts = [];
        if (node.datasource) parts.push(node.datasource);
        if (node.query_mode === "sql") parts.push("SQL");

        return parts.join(" · ");
    }

    function titleFor(node) {
        var parts = [node.label];
        if (node.agent_name) parts.push("agent: " + node.agent_name);
        if (node.datasource) parts.push("datasource: " + node.datasource);
        if (!node.is_enabled) parts.push("disabled — this stops the chain");

        return parts.join("\n");
    }

    /**
     * One connector, from the right edge of the tool that runs first to the left
     * edge of the tool it feeds.
     */
    function edgeGroup(edge, byKey) {
        var from = byKey[edge.source];
        var to = byKey[edge.target];
        if (!from || !to) return null;

        var group = createSvg("g");
        var terminal = edge.kind === "start" || edge.kind === "end";

        var start = { x: from.x + from.width, y: from.y + from.height / 2 };
        var finish = { x: to.x, y: to.y + to.height / 2 };

        var path = createSvg("path");
        path.setAttribute("class", "tool-graph-edge" + (terminal ? " is-terminal" : ""));
        path.setAttribute("d", curve(start, finish));
        path.setAttribute("marker-end", "url(#tool-graph-arrow)");
        group.appendChild(path);

        if (edge.label) {
            var label = svgText(
                String(edge.label),
                (start.x + finish.x) / 2,
                (start.y + finish.y) / 2 - 6,
                "tool-graph-edge-label",
            );
            group.appendChild(label);
        }

        return group;
    }

    /** D3's horizontal link generator, which is the whole of the curve maths here. */
    function curve(start, finish) {
        var link = window.d3.linkHorizontal()
            .x(function (point) { return point.x; })
            .y(function (point) { return point.y; });

        return link({ source: start, target: finish });
    }

    function defsWithArrow() {
        var defs = createSvg("defs");
        var marker = createSvg("marker");

        marker.setAttribute("id", "tool-graph-arrow");
        marker.setAttribute("viewBox", "0 0 10 10");
        marker.setAttribute("refX", 9);
        marker.setAttribute("refY", 5);
        marker.setAttribute("markerWidth", 6);
        marker.setAttribute("markerHeight", 6);
        marker.setAttribute("orient", "auto-start-reverse");

        var head = createSvg("path");
        head.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
        head.setAttribute("fill", "currentColor");
        marker.appendChild(head);

        defs.appendChild(marker);

        return defs;
    }

    /**
     * Scale the drawing to the pane, then hand the pane to D3's zoom behaviour.
     *
     * Never scaled *up*: a two-node chain blown up to fill a 70vh pane looks like a
     * different feature from a ten-node one, and the labels are already legible.
     */
    function fitAndZoom(svg, viewport, nodes) {
        var width = svg.clientWidth || svg.parentElement.clientWidth || 900;
        var height = svg.clientHeight || 480;

        var right = 0;
        var bottom = 0;
        nodes.forEach(function (node) {
            right = Math.max(right, node.x + node.width);
            bottom = Math.max(bottom, node.y + node.height);
        });

        var scale = Math.min(
            1,
            (width - CANVAS_PADDING * 2) / Math.max(right, 1),
            (height - CANVAS_PADDING * 2) / Math.max(bottom, 1),
        );
        var initial = window.d3.zoomIdentity
            .translate(CANVAS_PADDING, CANVAS_PADDING)
            .scale(scale);

        var selection = window.d3.select(svg);
        var zoom = window.d3.zoom()
            .scaleExtent([0.2, 2.5])
            .on("zoom", function (event) {
                viewport.setAttribute("transform", event.transform.toString());
            });

        selection.call(zoom);
        selection.call(zoom.transform, initial);
    }

    function drawEmptySvg(svg, message) {
        var text = svgText(message, 24, 40, "tool-graph-sublabel");
        text.setAttribute("text-anchor", "start");
        svg.appendChild(text);
    }

    // ----------------------------------------------------------------------
    // The SQL graph
    // ----------------------------------------------------------------------

    function renderJoins(elements, payload) {
        var host = elements.joins;
        if (!host) return;

        host.textContent = "";

        var tools = Array.isArray(payload.tools) ? payload.tools : [];
        if (!tools.length) {
            host.appendChild(mutedNote(payload.error
                ? "Nothing to draw."
                : "Pick a workspace, a data agent or a tool on the left."));
            return;
        }

        tools.forEach(function (tool) {
            host.appendChild(joinCard(tool));
        });
    }

    function joinCard(tool) {
        var card = element("div", "card shadow-sm mb-3");
        var header = element("div", "card-header bg-white d-flex flex-wrap gap-2 align-items-center py-2");

        var name = element("span", "fw-semibold font-monospace small");
        name.textContent = tool.tool_name || "";
        header.appendChild(name);

        if (tool.query_mode === "sql") {
            header.appendChild(badge("SQL", "bg-secondary-subtle text-secondary-emphasis"));
        }

        (tool.tables || []).forEach(function (table) {
            header.appendChild(badge(table, "bg-light text-dark border"));
        });

        card.appendChild(header);

        var body = element("div", "card-body");
        var joins = Array.isArray(tool.joins) ? tool.joins : [];

        if (!joins.length) {
            body.appendChild(mutedNote(tool.note || ""));
        } else {
            var strip = element("div", "d-flex flex-wrap gap-4");
            joins.forEach(function (join) {
                strip.appendChild(vennBlock(join));
            });
            body.appendChild(strip);
        }

        card.appendChild(body);

        return card;
    }

    /** One join: the diagram, the keyword it stands for, and the ON condition. */
    function vennBlock(join) {
        var block = element("div", "text-center");

        block.appendChild(venn(join.type));

        var keyword = element("div", "small fw-semibold");
        keyword.textContent = join.type_label || "JOIN";
        block.appendChild(keyword);

        var tables = element("div", "small font-monospace text-body-secondary");
        tables.textContent = (join.left_table || "") + " ⋈ " + (join.table || "");
        block.appendChild(tables);

        var on = element("div", "small font-monospace text-body-secondary");
        on.textContent = "on " + (join.left_table || "") + "." + (join.left_column || "") +
            " = " + (join.table || "") + "." + (join.right_column || "");
        block.appendChild(on);

        return block;
    }

    /**
     * Two circles, with the region the join keeps filled in.
     *
     * `inner` keeps only the rows on both sides, so only the lens is shaded; `left`
     * and `right` keep one whole side including the lens; `full` keeps everything.
     * That is the join, drawn — the shading is the definition, not decoration.
     */
    function venn(type) {
        var svg = createSvg("svg");
        svg.setAttribute("width", VENN_WIDTH);
        svg.setAttribute("height", VENN_HEIGHT);
        svg.setAttribute("viewBox", "0 0 " + VENN_WIDTH + " " + VENN_HEIGHT);

        var centreY = VENN_HEIGHT / 2;
        var leftX = VENN_WIDTH / 2 - VENN_OVERLAP / 2;
        var rightX = VENN_WIDTH / 2 + VENN_OVERLAP / 2;

        clipSeq += 1;
        var clipId = "tool-graph-clip-" + clipSeq;

        var defs = createSvg("defs");
        var clip = createSvg("clipPath");
        clip.setAttribute("id", clipId);
        clip.appendChild(circle(rightX, centreY, ""));
        defs.appendChild(clip);
        svg.appendChild(defs);

        if (type === "left" || type === "full") {
            svg.appendChild(circle(leftX, centreY, "tool-graph-venn-fill"));
        }
        if (type === "right" || type === "full") {
            svg.appendChild(circle(rightX, centreY, "tool-graph-venn-fill"));
        }
        if (type === "inner") {
            var lens = circle(leftX, centreY, "tool-graph-venn-fill");
            lens.setAttribute("clip-path", "url(#" + clipId + ")");
            svg.appendChild(lens);
        }

        svg.appendChild(circle(leftX, centreY, "tool-graph-venn-circle"));
        svg.appendChild(circle(rightX, centreY, "tool-graph-venn-circle"));

        return svg;
    }

    function circle(cx, cy, className) {
        var node = createSvg("circle");
        node.setAttribute("cx", cx);
        node.setAttribute("cy", cy);
        node.setAttribute("r", VENN_RADIUS);
        if (className) node.setAttribute("class", className);

        return node;
    }

    // ----------------------------------------------------------------------
    // Messages
    // ----------------------------------------------------------------------

    function showMessage(elements, message) {
        if (!elements.message) return;

        elements.message.textContent = message;
        elements.message.hidden = false;
    }

    function hideMessage(elements) {
        if (!elements.message) return;

        elements.message.textContent = "";
        elements.message.hidden = true;
    }

    function showEmpty(elements, message) {
        setText(elements.scope, "Nothing selected");

        if (elements.svg) {
            elements.svg.textContent = "";
            drawEmptySvg(elements.svg, message);
        }
        if (elements.joins) {
            elements.joins.textContent = "";
            elements.joins.appendChild(mutedNote(message));
        }
    }

    // ----------------------------------------------------------------------
    // DOM helpers — createElement only; every name here is user data
    // ----------------------------------------------------------------------

    function element(tag, className) {
        var node = document.createElement(tag);
        if (className) node.className = className;

        return node;
    }

    function createSvg(tag) {
        return document.createElementNS(SVG_NS, tag);
    }

    function svgText(value, x, y, className) {
        var node = createSvg("text");
        node.setAttribute("x", x);
        node.setAttribute("y", y);
        node.setAttribute("class", className);
        node.setAttribute("text-anchor", "middle");
        node.textContent = value;

        return node;
    }

    function badge(value, className) {
        var node = element("span", "badge " + className);
        node.textContent = value;

        return node;
    }

    function mutedNote(message) {
        var note = element("p", "text-muted small mb-0");
        note.textContent = message;

        return note;
    }

    function setText(node, value) {
        if (node) node.textContent = value;
    }

    /** JSON GET, through the app's fetch wrapper when one is present. */
    function request(url) {
        if (typeof window.safeFetch === "function") {
            return window.safeFetch(url).then(function (response) {
                return response.json();
            });
        }

        return fetch(url, { headers: { Accept: "application/json" } })
            .then(function (response) { return response.json(); });
    }
})();
