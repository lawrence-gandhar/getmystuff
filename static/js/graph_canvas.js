/**
 * Shared node-graph canvas primitives.
 *
 * The stateless half of a node/edge editor: the Bezier maths that draws a connector,
 * the measurement that finds where a connector should start, the escaping every label
 * goes through, and an id generator. Two features draw graphs on a canvas —
 * `flow_builder.js` and `graph_designer.js` — and this is what they share.
 *
 * WHAT DOES NOT LIVE HERE, and why the line is drawn where it is.
 *
 * Nothing stateful, and nothing that knows what a node *means*. The node-type registry,
 * the properties panel, the palette, save/load, the selection model — all of that is
 * per-feature, because a conversation flow's nodes and a data pipeline's nodes have
 * almost nothing in common beyond being boxes joined by curves. What they share is the
 * curves.
 *
 * Every function here is pure or measures the DOM it is handed. Specifically, none of
 * them reads a module-level `wrapperEl`, a `state` object, or an id prefix: the caller
 * passes its own scroll container and its own elements. That is what makes one copy
 * safe for two canvases with different markup, different ids and different CSS.
 *
 * SECURITY. `escapeHtml` and `escapeAttr` exist because both callers build some markup
 * as strings, and every label on either canvas is a table, column, tool or node name
 * out of the user's own database. The same rule `tool_configs.js`, `tool_chain.js` and
 * `tool_graphs.js` each state at the top of their files applies to both callers of this
 * one.
 */
window.GraphCanvas = (function () {
    "use strict";

    const SVG_NS = "http://www.w3.org/2000/svg";

    // The minimum horizontal reach of a connector's control points. Without a floor,
    // two nodes stacked almost vertically produce a curve that collapses into a
    // straight line through both boxes and reads as no connector at all.
    const MIN_CONTROL_REACH = 40;

    // -----------------------------------------------------------------
    // Ids
    // -----------------------------------------------------------------

    // Shared by every generator this module hands out, and deliberately not per
    // generator. Two generators created in the same millisecond would otherwise both
    // start at 1 and mint identical ids — which is not hypothetical, it is what a
    // straightforward `let sequence = 1` inside the factory actually does. One counter
    // for the page means "unique id" is true however many canvases ask for one.
    let sequence = 1;

    /**
     * Build an id generator.
     *
     * The timestamp makes ids unique across page loads as well as within one: a graph
     * saved, reloaded and added to must not mint an id that collides with one already in
     * the stored document. The counter makes them unique within a millisecond.
     *
     * @returns {function(string): string} genId(prefix)
     */
    function makeIdGenerator() {
        return function genId(prefix) {
            return prefix + "_" + Date.now().toString(36) + "_" + (sequence++);
        };
    }

    // -----------------------------------------------------------------
    // Measurement
    // -----------------------------------------------------------------

    /**
     * The canvas-relative centre of an element.
     *
     * Canvas-relative, not viewport-relative: the connector layer is an SVG sized to
     * the canvas, so a point measured against the viewport would drift by exactly the
     * scroll offset the moment anybody scrolled. Adding the wrapper's scroll back in is
     * what keeps a connector attached to its node.
     *
     * @param {HTMLElement} wrapperEl - the scrolling container the canvas sits in
     * @param {HTMLElement} targetEl
     * @returns {{x: number, y: number}|null}
     */
    function anchor(wrapperEl, targetEl) {
        if (!wrapperEl || !targetEl) return null;

        const wrapperRect = wrapperEl.getBoundingClientRect();
        const rect = targetEl.getBoundingClientRect();

        return {
            x: rect.left + rect.width / 2 - wrapperRect.left + wrapperEl.scrollLeft,
            y: rect.top + rect.height / 2 - wrapperRect.top + wrapperEl.scrollTop,
        };
    }

    /**
     * The canvas-relative centre of one of a node's ports, or of the node itself.
     *
     * Falls back to the node when the port is not found, rather than returning null: a
     * node re-rendered without a port it used to have should still show its connectors
     * attached to the box while the graph is fixed up, instead of dropping them.
     *
     * @param {HTMLElement} wrapperEl
     * @param {HTMLElement} nodeEl
     * @param {string|null} portSelector - CSS selector within the node, or null for the node
     * @returns {{x: number, y: number}|null}
     */
    function portAnchor(wrapperEl, nodeEl, portSelector) {
        if (!nodeEl) return null;

        const portEl = portSelector ? nodeEl.querySelector(portSelector) : nodeEl;
        return anchor(wrapperEl, portEl || nodeEl);
    }

    // -----------------------------------------------------------------
    // Connectors
    // -----------------------------------------------------------------

    /**
     * A connector's cubic Bezier control points, from one point to another.
     *
     * The control points are returned rather than only the path string, and that is the
     * whole reason this is a separate function: placing a delete button at the middle of
     * a curve, or a drag handle a fifth of the way along it, needs the curve's
     * definition and not its rendering.
     *
     * @param {{x: number, y: number}} from
     * @param {{x: number, y: number}} to
     * @returns {{p0: object, p1: object, p2: object, p3: object}|null}
     */
    function geometry(from, to) {
        if (!from || !to) return null;

        const dx = Math.max(MIN_CONTROL_REACH, Math.abs(to.x - from.x) / 2);

        return {
            p0: from,
            p1: { x: from.x + dx, y: from.y },
            p2: { x: to.x - dx, y: to.y },
            p3: to,
        };
    }

    /**
     * The SVG `d` attribute for a geometry.
     *
     * @param {object} g - from geometry()
     * @returns {string}
     */
    function pathD(g) {
        return "M " + g.p0.x + " " + g.p0.y +
            " C " + g.p1.x + " " + g.p1.y +
            ", " + g.p2.x + " " + g.p2.y +
            ", " + g.p3.x + " " + g.p3.y;
    }

    /**
     * The point on a cubic Bezier at parameter t.
     *
     * @param {object} g - from geometry()
     * @param {number} t - 0 at the source, 1 at the target
     * @returns {{x: number, y: number}}
     */
    function pointAt(g, t) {
        const mt = 1 - t;

        return {
            x: mt * mt * mt * g.p0.x + 3 * mt * mt * t * g.p1.x +
                3 * mt * t * t * g.p2.x + t * t * t * g.p3.x,
            y: mt * mt * mt * g.p0.y + 3 * mt * mt * t * g.p1.y +
                3 * mt * t * t * g.p2.y + t * t * t * g.p3.y,
        };
    }

    /**
     * Create an SVG element in the right namespace.
     *
     * `document.createElement("path")` produces an unknown HTML element that renders as
     * nothing at all, with no error — which is the kind of failure worth a named
     * helper.
     *
     * @param {string} name
     * @returns {SVGElement}
     */
    function svg(name) {
        return document.createElementNS(SVG_NS, name);
    }

    // -----------------------------------------------------------------
    // Escaping
    // -----------------------------------------------------------------

    /**
     * HTML-escape a value for interpolation into markup.
     *
     * Via a detached element's textContent/innerHTML round trip, which is the browser's
     * own escaping rather than a hand-written replace table that could miss a character.
     *
     * @param {*} str
     * @returns {string}
     */
    function escapeHtml(str) {
        const div = document.createElement("div");
        div.textContent = str == null ? "" : String(str);
        return div.innerHTML;
    }

    /**
     * HTML-escape a value for a double-quoted attribute.
     *
     * @param {*} str
     * @returns {string}
     */
    function escapeAttr(str) {
        return escapeHtml(str).replace(/"/g, "&quot;");
    }

    /**
     * Escape a value for use inside a CSS attribute-selector.
     *
     * Ports and node ids reach selectors, and a generated id contains characters a
     * selector reads as syntax.
     *
     * @param {*} str
     * @returns {string}
     */
    function cssEscape(str) {
        return String(str).replace(/[^a-zA-Z0-9_-]/g, function (c) {
            return "\\" + c;
        });
    }

    return {
        SVG_NS: SVG_NS,
        makeIdGenerator: makeIdGenerator,
        anchor: anchor,
        portAnchor: portAnchor,
        geometry: geometry,
        pathD: pathD,
        pointAt: pointAt,
        svg: svg,
        escapeHtml: escapeHtml,
        escapeAttr: escapeAttr,
        cssEscape: cssEscape,
    };
})();
