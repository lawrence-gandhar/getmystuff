/**
 * Shared node-graph canvas primitives.
 *
 * The stateless half of a node/edge editor: the Bezier maths that draws a connector,
 * the right-angle maths that draws the other kind, the rectangle maths a selection box
 * needs, the measurement that finds where a connector should start, the escaping every
 * label goes through, and an id generator. Three features draw graphs on a canvas —
 * `flow_builder.js`, `graph_designer.js` and `integrations.js` — and this is what they
 * share.
 *
 * WHAT DOES NOT LIVE HERE, and why the line is drawn where it is.
 *
 * Nothing stateful, and nothing that knows what a node *means*. The node-type registry,
 * the properties panel, the palette, save/load — all of that is per-feature, because a
 * conversation flow's nodes and a data pipeline's nodes have almost nothing in common
 * beyond being boxes joined by lines. What they share is the lines.
 *
 * Every function here is pure or measures the DOM it is handed. Specifically, none of
 * them reads a module-level `wrapperEl`, a `state` object, or an id prefix: the caller
 * passes its own scroll container and its own elements. That is what makes one copy
 * safe for three canvases with different markup, different ids and different CSS.
 *
 * THE SELECTION MODEL used to be named above as a thing that stays per-feature, and it
 * is worth saying what changed rather than quietly dropping it from the list. It is now
 * shared, but not from here — it is in `graph_selection.js`, which is stateful by
 * nature: a gesture *is* state (where the press began, what was selected before it,
 * which frame is pending), and holding it here would break every promise in the
 * paragraph above. So the line moved rather than blurred. **The geometry and the gesture
 * are shared; what a selection means is not.** Which class paints a selected thing,
 * whether a properties panel opens, and what a finished move says about the drawing all
 * stay with each canvas. What this file contributes to it is four rectangle functions
 * with no more state than `elbowPoints` has.
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

    // -----------------------------------------------------------------
    // Orthogonal connectors
    //
    // The Bezier trio above draws a connector between two boxes standing side by side,
    // which is what the Integrations canvas still is. These draw one between two boxes
    // standing one *above* the other — down out of the source, across, down into the
    // target — for the two canvases that lay themselves out top-down.
    //
    // Added beside the Beziers rather than replacing them, deliberately. Three canvases
    // share this module, not two: changing `geometry`/`pathD` in place would silently
    // restyle `integrations.js`, which nobody asked to change.
    // -----------------------------------------------------------------

    // How far a connector travels straight down before it is allowed to turn. Enough that
    // the turn happens clear of the block it left, so a corner never appears to touch it.
    const ELBOW_GUTTER = 22;

    // The default corner radius. Squared-off corners read as a technical diagram; this is
    // the amount of rounding that reads as a drawn line without looking like a Bezier.
    const ELBOW_RADIUS = 10;

    // How far past the rightmost of two blocks a connector that has to climb runs. Only
    // reached by an edge pointing upward, which on an arranged canvas means a loop or a
    // block somebody dragged above the one feeding it.
    const ELBOW_LANE = 40;

    /**
     * A curved connector's control points, routed through a point somebody dragged it to.
     *
     * The curve passes **exactly** through `bend`, at its midpoint. Both control points are
     * put at the same place, which makes the cubic behave as a quadratic, and then the
     * control that puts B(0.5) on a chosen point falls out of the cubic's own definition:
     * with p1 = p2 = C, B(0.5) = (p0 + p3)/8 + 6C/8, so C = (8·bend − p0 − p3)/6.
     *
     * Exactly through, rather than near, because the handle a person drags has to end up
     * under their cursor — a curve that merely leans toward the point reads as a bug.
     *
     * With no bend this is `geometry`, unchanged, so a caller can route everything through
     * here and a drawing saved before bends existed is drawn identically.
     *
     * @param {{x: number, y: number}} from
     * @param {{x: number, y: number}} to
     * @param {{x: number, y: number}} [bend]
     * @returns {{p0: object, p1: object, p2: object, p3: object}|null}
     */
    function geometryWithBend(from, to, bend) {
        if (!from || !to) return null;
        if (!_isPoint(bend)) return geometry(from, to);

        const control = {
            x: (8 * bend.x - from.x - to.x) / 6,
            y: (8 * bend.y - from.y - to.y) / 6,
        };

        return { p0: from, p1: control, p2: control, p3: to };
    }

    /**
     * The corner points of an orthogonal connector from one point down to another.
     *
     * Three cases, and the third is the one worth knowing about:
     *
     *   1. Directly below — one straight vertical line, no corners at all.
     *   2. Below and off to one side — down to halfway, across, and down. Halfway rather
     *      than a fixed distance so two connectors crossing the same gap turn at the same
     *      height and read as a pair instead of a tangle. Any amount below is enough: a
     *      pair only ten pixels apart still gets short stubs and one clean step, and
     *      `elbowPathD` shrinks the corner radius to fit them.
     *   3. **Not below at all.** A hand-dragged block can end up level with or above the
     *      one that feeds it, and an edge can be re-pointed at anything. There is no way
     *      down to a target that is up, so this takes the return lane —
     *      :func:`backEdgePoints` — rather than a route that climbs through the boxes.
     *      What must not happen here is the obvious-looking alternative: stepping out
     *      below the source and climbing in the *target's own column*, which overshoots
     *      above the target and drops back into it, drawing a line visibly folded over
     *      itself.
     *
     * @param {{x: number, y: number}} from
     * @param {{x: number, y: number}} to
     * @param {number} [gutter]
     * @returns {Array<{x: number, y: number}>|null}
     */
    function elbowPoints(from, to, gutter) {
        if (!from || !to) return null;

        const reach = gutter == null ? ELBOW_GUTTER : gutter;

        if (to.y - from.y <= 1) {
            return backEdgePoints(from, to, Math.max(from.x, to.x) + ELBOW_LANE, reach);
        }

        if (Math.abs(to.x - from.x) < 1) return [from, to];

        const turn = from.y + (to.y - from.y) / 2;

        return [from, { x: from.x, y: turn }, { x: to.x, y: turn }, to];
    }

    /**
     * The corner points of a connector that runs *back up* the canvas.
     *
     * A Goto block's return jump, or any loop. It cannot be drawn as a step — there is no
     * "below" for it to go to — so it leaves the source downward, runs out to a lane clear
     * of the blocks (`sideX`), climbs, and comes back in over the target.
     *
     * @param {{x: number, y: number}} from
     * @param {{x: number, y: number}} to
     * @param {number} sideX - the x of the empty lane to climb in
     * @param {number} [gutter]
     * @returns {Array<{x: number, y: number}>|null}
     */
    function backEdgePoints(from, to, sideX, gutter) {
        if (!from || !to) return null;

        const reach = gutter == null ? ELBOW_GUTTER : gutter;

        return [
            from,
            { x: from.x, y: from.y + reach },
            { x: sideX, y: from.y + reach },
            { x: sideX, y: to.y - reach },
            { x: to.x, y: to.y - reach },
            to,
        ];
    }

    /**
     * The corner points of a connector routed by hand through one or more waypoints.
     *
     * A waypoint is somewhere a person dragged the wire to, because it had to go round
     * something the router does not know about. So the contract is simply that the line
     * passes **through** each of them, in order, still at right angles.
     *
     * The shape is: leave the source downward by the gutter, then one L per waypoint, then
     * come into the target from above by the gutter. The stubs are there for the reason
     * `ELBOW_GUTTER` gives — the first turn has to happen clear of the box it left — and
     * they are why a bend near a node still looks attached to it.
     *
     * Each L turns on the axis the line *arrived* on: having come in vertically it goes
     * across and then down, and having come in horizontally it goes down and then across.
     * Alternating rather than fixed, because a fixed choice makes two consecutive segments
     * collinear and then doubled back, which `elbowPathD` can only draw as a kink.
     *
     * **With no waypoints this is exactly `elbowPoints`.** That is load-bearing: a caller
     * can route every connector through here and never branch, and a drawing saved before
     * bends existed is drawn identically.
     *
     * @param {{x: number, y: number}} from
     * @param {{x: number, y: number}} to
     * @param {Array<{x: number, y: number}>} [waypoints]
     * @param {number} [gutter]
     * @returns {{points: Array<{x: number, y: number}>, waypointAt: Array<number>}|null}
     */
    function waypointRoute(from, to, waypoints, gutter) {
        if (!from || !to) return null;

        const bends = Array.isArray(waypoints) ? waypoints.filter(_isPoint) : [];
        if (!bends.length) {
            return { points: elbowPoints(from, to, gutter), waypointAt: [] };
        }

        const reach = gutter == null ? ELBOW_GUTTER : gutter;

        // The two stubs. When the target is level with or above the source there is no room
        // for both, so they collapse to the midpoint — the same accommodation `elbowPoints`
        // makes for its "directly below" case, and it keeps a bend usable on a wire that
        // climbs.
        let exit = { x: from.x, y: from.y + reach };
        let entry = { x: to.x, y: to.y - reach };
        if (entry.y < exit.y) {
            const middle = (from.y + to.y) / 2;
            exit = { x: from.x, y: middle };
            entry = { x: to.x, y: middle };
        }

        const raw = [from, exit];
        // Which entry in `raw` each waypoint is. Recomputed against the collapsed array at
        // the end, because collapsing moves the indices.
        const marks = [];

        let arrivedVertical = true;
        bends.forEach(function (bend) {
            const current = raw[raw.length - 1];
            const corner = arrivedVertical
                ? { x: bend.x, y: current.y }
                : { x: current.x, y: bend.y };

            raw.push(corner);
            raw.push({ x: bend.x, y: bend.y });
            marks.push(raw.length - 1);

            // How the line arrived at the bend, which decides the next turn. Compared
            // rather than assumed: a bend dragged level with the corner is arrived at
            // horizontally, and getting this wrong is what draws a doubled-back segment.
            arrivedVertical = Math.abs(corner.x - bend.x) < 0.5;
        });

        const last = raw[raw.length - 1];
        const corner = arrivedVertical
            ? { x: entry.x, y: last.y }
            : { x: last.x, y: entry.y };
        raw.push(corner);
        raw.push(entry);
        raw.push(to);

        return _collapse(raw, marks);
    }

    /** Just the points — what a renderer wants. See `waypointRoute` for the contract. */
    function waypointPoints(from, to, waypoints, gutter) {
        const route = waypointRoute(from, to, waypoints, gutter);
        return route ? route.points : null;
    }

    function _isPoint(p) {
        return !!p && isFinite(p.x) && isFinite(p.y);
    }

    /**
     * Drop points that add nothing, and say where the waypoints ended up.
     *
     * Two kinds add nothing: a point on top of its neighbour, and a point in line with both
     * its neighbours. The second is the one that matters — without it, a waypoint dragged
     * into line with a stub leaves a corner that is not a corner, and `pointAlongPolyline`
     * puts the ✕ at a slightly wrong place on a line that looks perfectly straight.
     */
    function _collapse(points, marks) {
        const kept = [];
        const keptIndex = [];

        points.forEach(function (point, index) {
            const previous = kept[kept.length - 1];
            if (previous && _distance(previous, point) < 0.5) {
                // Same place. If a waypoint was here, it belongs to the point already kept.
                keptIndex[index] = kept.length - 1;
                return;
            }
            keptIndex[index] = kept.length;
            kept.push(point);
        });

        // Collinear middles, back to front so an index stays valid while the tail shifts.
        for (let i = kept.length - 2; i > 0; i--) {
            const a = kept[i - 1];
            const b = kept[i];
            const c = kept[i + 1];
            const cross = (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
            if (Math.abs(cross) > 0.5) continue;
            // A waypoint sits on this point, and dropping it would lose the handle the user
            // has to be able to grab. The redundant corner is the price and it is invisible.
            if (marks.some(function (mark) { return keptIndex[mark] === i; })) continue;

            kept.splice(i, 1);
            for (let j = 0; j < keptIndex.length; j++) {
                if (keptIndex[j] > i) keptIndex[j] -= 1;
            }
        }

        return {
            points: kept,
            waypointAt: marks.map(function (mark) { return keptIndex[mark]; }),
        };
    }

    /**
     * The segment of a run of points that a loose point is closest to.
     *
     * What turns a grab on a connector into a decision about which of its legs was grabbed,
     * and therefore where a new bend goes.
     *
     * @param {Array<{x: number, y: number}>} points
     * @param {{x: number, y: number}} point
     * @returns {{index: number, distance: number, projection: {x: number, y: number}}|null}
     */
    function nearestSegment(points, point) {
        if (!points || points.length < 2) return null;

        let best = null;

        for (let i = 0; i < points.length - 1; i++) {
            const a = points[i];
            const b = points[i + 1];
            const dx = b.x - a.x;
            const dy = b.y - a.y;
            const lengthSquared = dx * dx + dy * dy;

            // Clamped to the segment, so a point past one end projects onto that end
            // rather than onto the infinite line through it.
            const t = lengthSquared
                ? Math.max(0, Math.min(1, ((point.x - a.x) * dx + (point.y - a.y) * dy) / lengthSquared))
                : 0;
            const projection = { x: a.x + dx * t, y: a.y + dy * t };
            const distance = _distance(point, projection);

            if (!best || distance < best.distance) {
                best = { index: i, distance: distance, projection: projection };
            }
        }

        return best;
    }

    /**
     * A value snapped to whichever candidate it is nearly equal to, or left alone.
     *
     * What makes a hand-routed wire line up with the thing it is being routed past instead
     * of sitting a pixel off it.
     *
     * @param {number} value
     * @param {Array<number>} candidates
     * @param {number} tolerance
     * @returns {number}
     */
    /**
     * A connector's hand-placed bends, or an empty list.
     *
     * The `derived` guard is universal rather than per-canvas, and that is deliberate: a
     * derived connector — the Flow Builder's Goto jump is the only one today — is rebuilt
     * from its block's settings on every render, so a bend written onto one would vanish
     * without a word. Two of the three canvases never set `derived` at all, which makes the
     * check a no-op there and correct in advance if either ever grows one.
     *
     * @param {object} edge
     * @returns {Array<{x: number, y: number}>}
     */
    function waypointsOf(edge) {
        if (!edge || edge.derived) return [];
        return Array.isArray(edge.waypoints) ? edge.waypoints : [];
    }

    /**
     * Bends read off a stored document, keeping only the ones that are usable.
     *
     * This is the client-side twin of `validate_edge_waypoints` in `app/schemas/base.py`,
     * and it exists for the same reason that one does. The save schemas are `extra="allow"`,
     * which is what makes `waypoints` possible with no migration — and it means a document
     * can arrive hand-edited, or written by an older version of the page. A bend that is not
     * two finite numbers is dropped rather than drawn, because `NaN` in a coordinate does
     * not fail here: it fails silently in the SVG, and then again at the database, which
     * refuses it as JSON.
     *
     * `max` is the caller's cap because the canvases genuinely disagree — four bends on the
     * two top-down canvases, one on Integrations, whose curve takes a single control point.
     *
     * @param {*} raw
     * @param {number} max
     * @returns {Array<{x: number, y: number}>}
     */
    function readWaypoints(raw, max) {
        if (!Array.isArray(raw)) return [];

        return raw
            .filter(function (point) {
                return point && isFinite(point.x) && isFinite(point.y);
            })
            .slice(0, max)
            .map(function (point) {
                return { x: Math.max(0, Number(point.x)), y: Math.max(0, Number(point.y)) };
            });
    }

    /**
     * Which of a node's ways out should carry an existing connection onward.
     *
     * Used when a node is spliced into a connector: `A → B` becomes `A → new → B`, and this
     * decides which of `new`'s ports the second half leaves by. Taking the *first* port is
     * the obvious rule and it is wrong, because two of the shapes on these canvases put
     * something other than "carry on" first:
     *
     *   for_each / do_until  →  ["body", "done"]
     *
     * `body` is the inside of the loop. Wiring B there does not insert a step before B, it
     * moves B *into* the loop and runs it once per item — a silent change to what B means,
     * on a canvas with no undo. `done` is the port that means what the original connector
     * meant, so it is the one that inherits it.
     *
     * The order, therefore: an explicit `default`, else `done`, else whatever comes first.
     * The last case is a fresh Branch, whose only port is `else` — which is genuinely where
     * a branch with no conditions yet sends everything.
     *
     * Names only, so the three canvases can call it: a Flow Builder port is
     * `{port, label, kind}`, a Graph Designer port is `{port, label}`, and an Integrations
     * port is a bare string.
     *
     * @param {Array<string>} names - port names, in the order the node declares them
     * @returns {string|null} the name to use, or null when there is no way out at all
     */
    function continuationPort(names) {
        const usable = (names || []).filter(function (name) {
            return typeof name === "string" && name.length > 0;
        });

        if (!usable.length) return null;
        if (usable.indexOf("default") !== -1) return "default";
        if (usable.indexOf("done") !== -1) return "done";
        return usable[0];
    }

    function snapToAny(value, candidates, tolerance) {
        let best = value;
        let bestGap = tolerance;

        (candidates || []).forEach(function (candidate) {
            if (candidate == null || !isFinite(candidate)) return;
            const gap = Math.abs(value - candidate);
            if (gap <= bestGap) {
                bestGap = gap;
                best = candidate;
            }
        });

        return best;
    }

    /**
     * The SVG `d` attribute for a run of corner points, corners rounded.
     *
     * Each corner becomes a quadratic curve whose control point is the corner itself,
     * which is the cheap way to round a right angle and needs no arc-flag arithmetic.
     *
     * The radius is **clamped to half of the shorter of the two segments** meeting at each
     * corner. Without that, a corner near the end of a short segment overshoots into the
     * next one and the line visibly doubles back — the failure looks like a kink in the
     * connector and is very hard to read as a rounding bug. A zero-length segment is left
     * square for the same reason: there is nothing to round against.
     *
     * @param {Array<{x: number, y: number}>} points
     * @param {number} [radius]
     * @returns {string}
     */
    function elbowPathD(points, radius) {
        if (!points || points.length < 2) return "";

        const r = radius == null ? ELBOW_RADIUS : radius;
        let d = "M " + points[0].x + " " + points[0].y;

        for (let i = 1; i < points.length - 1; i++) {
            const previous = points[i - 1];
            const corner = points[i];
            const next = points[i + 1];
            const inLength = _distance(previous, corner);
            const outLength = _distance(corner, next);

            if (!inLength || !outLength) continue;

            const reach = Math.min(r, inLength / 2, outLength / 2);
            const entry = _towards(corner, previous, reach);
            const exit = _towards(corner, next, reach);

            d += " L " + entry.x + " " + entry.y +
                " Q " + corner.x + " " + corner.y + ", " + exit.x + " " + exit.y;
        }

        const last = points[points.length - 1];

        return d + " L " + last.x + " " + last.y;
    }

    /**
     * The point a fraction of the way along a run of corner points, by length.
     *
     * By length rather than by segment count, so the ✕ on a connector whose first leg is
     * ten pixels and whose second is three hundred still lands in the middle of the line a
     * reader sees, not at the end of the short leg.
     *
     * @param {Array<{x: number, y: number}>} points
     * @param {number} t - 0 at the source, 1 at the target
     * @returns {{x: number, y: number}|null}
     */
    function pointAlongPolyline(points, t) {
        if (!points || !points.length) return null;
        if (points.length === 1) return points[0];

        const lengths = [];
        let total = 0;

        for (let i = 1; i < points.length; i++) {
            const length = _distance(points[i - 1], points[i]);
            lengths.push(length);
            total += length;
        }

        if (!total) return points[0];

        let travelled = Math.max(0, Math.min(1, t)) * total;

        for (let i = 0; i < lengths.length; i++) {
            if (travelled <= lengths[i] || i === lengths.length - 1) {
                const fraction = lengths[i] ? travelled / lengths[i] : 0;
                return {
                    x: points[i].x + (points[i + 1].x - points[i].x) * fraction,
                    y: points[i].y + (points[i + 1].y - points[i].y) * fraction,
                };
            }
            travelled -= lengths[i];
        }

        return points[points.length - 1];
    }

    function _distance(a, b) {
        return Math.sqrt((b.x - a.x) * (b.x - a.x) + (b.y - a.y) * (b.y - a.y));
    }

    // -----------------------------------------------------------------
    // Rectangles
    //
    // What a selection box needs, and nothing more. Pure arithmetic on plain
    // {x, y, w, h} and {x, y}: no DOM, no notion of what is being selected. Which of a
    // canvas's things a box has caught is that canvas's question; whether two shapes
    // overlap is arithmetic, and arithmetic written twice is arithmetic that disagrees
    // with itself eventually.
    // -----------------------------------------------------------------

    /**
     * The rectangle between two points, always with positive width and height.
     *
     * Normalised, because a box is dragged in whichever direction the hand goes. Up and
     * to the left is the common case that a naive `w = b.x - a.x` gets wrong — it yields
     * a negative width, every intersection test silently answers "no", and the box
     * selects nothing while looking perfectly drawn.
     *
     * @param {{x: number, y: number}} a
     * @param {{x: number, y: number}} b
     * @returns {{x: number, y: number, w: number, h: number}}
     */
    function rectFromPoints(a, b) {
        return {
            x: Math.min(a.x, b.x),
            y: Math.min(a.y, b.y),
            w: Math.abs(b.x - a.x),
            h: Math.abs(b.y - a.y),
        };
    }

    /**
     * Whether two rectangles overlap at all. Touching counts.
     *
     * @param {{x: number, y: number, w: number, h: number}} a
     * @param {{x: number, y: number, w: number, h: number}} b
     * @returns {boolean}
     */
    function rectsIntersect(a, b) {
        return a.x <= b.x + b.w && b.x <= a.x + a.w &&
            a.y <= b.y + b.h && b.y <= a.y + a.h;
    }

    /**
     * Whether a line segment touches a rectangle.
     *
     * Liang–Barsky, which is the version of this with no divisions by zero in it: a
     * connector's segments are axis-aligned, so `dx` or `dy` is exactly 0 for nearly
     * every one of them, and the textbook slope-based clip divides by precisely that.
     *
     * @param {{x: number, y: number}} p
     * @param {{x: number, y: number}} q
     * @param {{x: number, y: number, w: number, h: number}} rect
     * @returns {boolean}
     */
    function segmentIntersectsRect(p, q, rect) {
        const dx = q.x - p.x;
        const dy = q.y - p.y;

        // A degenerate segment is a point, and a point is inside or it is not.
        if (!dx && !dy) {
            return p.x >= rect.x && p.x <= rect.x + rect.w &&
                p.y >= rect.y && p.y <= rect.y + rect.h;
        }

        let enter = 0;
        let leave = 1;

        // Each edge of the rectangle clips the parameter range the segment is still inside
        // for. If the range ever empties, the segment misses.
        const clip = function (direction, distance) {
            if (!direction) return distance >= 0;

            const t = distance / direction;

            if (direction > 0) {
                if (t < enter) return false;
                if (t < leave) leave = t;
            } else {
                if (t > leave) return false;
                if (t > enter) enter = t;
            }
            return true;
        };

        return clip(-dx, p.x - rect.x) &&
            clip(dx, rect.x + rect.w - p.x) &&
            clip(-dy, p.y - rect.y) &&
            clip(dy, rect.y + rect.h - p.y);
    }

    /**
     * Whether a run of corner points touches a rectangle.
     *
     * Segments, not corners — and that is the whole reason this is not a one-line `some`
     * over the points. A connector taking the return lane has a single vertical segment
     * hundreds of pixels long; a small box dragged across the middle of it contains none
     * of its corners, and a corner-only test would report a miss on a line the box is
     * plainly sitting on top of.
     *
     * @param {Array<{x: number, y: number}>} points
     * @param {{x: number, y: number, w: number, h: number}} rect
     * @returns {boolean}
     */
    function polylineIntersectsRect(points, rect) {
        if (!points || !points.length) return false;

        for (let i = 1; i < points.length; i++) {
            if (segmentIntersectsRect(points[i - 1], points[i], rect)) return true;
        }

        // A single-point run has no segment to test, so it is tested as the point it is.
        return points.length === 1 && segmentIntersectsRect(points[0], points[0], rect);
    }

    /** `distance` pixels from `origin` in the direction of `target`. */
    function _towards(origin, target, distance) {
        const length = _distance(origin, target);

        if (!length) return { x: origin.x, y: origin.y };

        return {
            x: origin.x + ((target.x - origin.x) / length) * distance,
            y: origin.y + ((target.y - origin.y) / length) * distance,
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
        ELBOW_GUTTER: ELBOW_GUTTER,
        ELBOW_RADIUS: ELBOW_RADIUS,
        // Exported because a caller has to place its own return lane clear of its own
        // blocks — `backEdgePoints` takes the x, it does not choose it.
        ELBOW_LANE: ELBOW_LANE,
        makeIdGenerator: makeIdGenerator,
        anchor: anchor,
        portAnchor: portAnchor,
        // Side-by-side canvases (Integrations). Curves.
        geometry: geometry,
        // A curve routed by hand. With no bend it is `geometry`, so a caller need not branch.
        geometryWithBend: geometryWithBend,
        pathD: pathD,
        pointAt: pointAt,
        // Top-down canvases (Flow Builder, Graph Designer). Right angles.
        elbowPoints: elbowPoints,
        backEdgePoints: backEdgePoints,
        // Hand-routed connectors. `waypointRoute` with no waypoints is `elbowPoints`, so a
        // caller can route everything through it without branching.
        waypointRoute: waypointRoute,
        waypointPoints: waypointPoints,
        nearestSegment: nearestSegment,
        snapToAny: snapToAny,
        // Hand-routed connectors' stored bends: reading them, and reading them safely.
        waypointsOf: waypointsOf,
        readWaypoints: readWaypoints,
        // Splicing a node into a connector: which of its ports inherits the connection.
        continuationPort: continuationPort,
        elbowPathD: elbowPathD,
        pointAlongPolyline: pointAlongPolyline,
        // Selection boxes. Geometry only — which of a canvas's things a box has caught is
        // that canvas's question, and lives in graph_selection.js.
        rectFromPoints: rectFromPoints,
        rectsIntersect: rectsIntersect,
        segmentIntersectsRect: segmentIntersectsRect,
        polylineIntersectsRect: polylineIntersectsRect,
        svg: svg,
        escapeHtml: escapeHtml,
        escapeAttr: escapeAttr,
        cssEscape: cssEscape,
    };
})();
