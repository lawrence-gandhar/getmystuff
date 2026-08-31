/*
 * graph_edges.js — the connector runtime shared by the two top-down canvases.
 *
 * The **Flow Builder** and the **Graph Designer** (Pipelines) draw the same kind of picture:
 * blocks stacked top to bottom, right-angled connectors between their ports, a dock or panel
 * to the side. They grew by copy-adaptation, and measuring their functions found ~450 lines
 * that were 93–100% identical — the anchor cache, the animation frame, the edge-chrome
 * repaint, the bend gesture and the group-move callbacks. This file is that code, once.
 *
 * The **Integrations** canvas is deliberately not a client. It is a different picture — steps
 * side by side, Bézier curves, one control point rather than four waypoints — and the same
 * measurement put its versions of these functions at 19–40% similar. Forcing it in would mean
 * abstracting elbow-versus-curve behind a flag, which is how a shared module becomes a worse
 * copy of two working ones. It keeps its own runtime and shares only what is genuinely
 * common: `GC.waypointsOf`, `GC.readWaypoints`, and the selection's button painter.
 *
 * ---------------------------------------------------------------------------------------
 * Why a factory and not more of `graph_canvas.js`
 *
 * This owns five pieces of mutable state — the anchor cache, the frame handle, the live bend,
 * the return-lane memo and the one-tick click swallow. `graph_canvas.js` states at the top of
 * the file that it holds nothing stateful, and that promise is worth more than the
 * convenience of putting these there. So this is the third instance of the pattern
 * `graph_selection.js` and `graph_insert.js` already use: `window.GraphEdges.create(config)`,
 * called once per page, returning a controller the canvas keeps.
 *
 * The boundary between the three is worth stating once, because all three are "shared canvas
 * code" and that is not a useful distinction:
 *
 *     graph_selection.js   owns a selection — what is picked, and moving it as one
 *     graph_insert.js      owns a menu     — what a "+" offers, and where it appears
 *     graph_edges.js       owns connectors — measuring, repainting, bending
 *
 * ---------------------------------------------------------------------------------------
 * Every difference between the two canvases is a named config key
 *
 * That was a requirement, not an accident: this refactor had to be behaviour-neutral on two
 * canvases already verified by hand. Where the two disagreed, neither won — the difference
 * became a config value. The list is short and each entry is a real difference:
 *
 *   chromePrefix        "fb" / "gd"        — the class names on a connector's controls
 *   chromeYOffset       0 / 10             — the Graph Designer sits its ✕ 10px lower
 *   isBusy()            the Graph Designer also refuses while `state.connecting`
 *   isRoutable(edge)    the Flow Builder excludes its derived Goto jumps
 *   anchorFallback      the Flow Builder falls back to the node's own box
 *   thresholdPx + how it is measured — the Flow Builder uses Chebyshev distance
 *                       (`max(|dx|,|dy|)`), the Graph Designer Manhattan (`|dx|+|dy|`).
 *                       Preserved per canvas rather than unified, because unifying it would
 *                       change how one of them feels. Worth reconciling one day; not here,
 *                       and not silently.
 */
(function () {
    "use strict";

    const GC = window.GraphCanvas;

    /**
     * Create a connector runtime bound to one canvas.
     *
     * @param {object} config - see the file header; every key is a documented difference
     * @returns {object} the controller the canvas keeps
     */
    function create(config) {
        const state = config.state;
        const wrapperEl = config.wrapperEl;

        // ---- the five pieces of state this module exists to own ----------------------

        /**
         * Port offsets held still for the duration of one drag.
         *
         * `null` outside a gesture, which is what makes `portAnchor` fall back to measuring.
         * See `portAnchor` for what the two halves are for.
         */
        let dragAnchors = null;

        /** The pending animation frame, or null. At most one is ever outstanding. */
        let dragFrame = null;

        /** The bend being dragged, or null. */
        let bending = null;

        /** `returnLaneX`'s memo, valid only while `dragAnchors` is. */
        let laneXCache = null;

        /** Armed for one tick after a bend, so its trailing click selects nothing. */
        let suppressEdgeClick = false;

        // ---- helpers the canvas owns, reached through config -------------------------

        function findNode(id) {
            return state.nodes.find(function (node) { return node.id === id; }) || null;
        }

        function findEdge(id) {
            return state.edges.find(function (edge) { return edge.id === id; }) || null;
        }

        /** Every connector that gets drawn, derived ones included. */
        function drawableEdges() {
            return config.getDrawableEdges ? config.getDrawableEdges() : state.edges;
        }

        function chromeClass(suffix) {
            return "." + config.chromePrefix + "-edge-" + suffix;
        }

        // =============================================================================
        // ANCHORS
        // =============================================================================

        /** Where a port actually is, by measurement. The expensive one. */
        function measuredAnchor(nodeId, portSelector) {
            return GC.portAnchor(
                wrapperEl, document.getElementById(config.nodeElementId(nodeId)), portSelector,
            );
        }

        /**
         * Where a port is, without measuring it more than once per gesture.
         *
         * This is the fix that made a group move possible at all. The drag loop used to write
         * `style.left` and then call `getBoundingClientRect()` on every port of every
         * connector, forcing the browser to lay the whole canvas out again — once per port,
         * per connector, per mousemove, and mousemove fires faster than the screen repaints.
         *
         * Outside a gesture there is nothing to cache and this is the plain measurement.
         * Inside one there are two cases:
         *
         *   a **stationary** end cannot move, so it is measured once and remembered outright;
         *
         *   a **moving** end is measured once as an *offset from its node's stored position*,
         *   and every later frame is two additions. Taking the offset as
         *   `anchor − node.position`, both sampled at the same instant, disposes of two
         *   problems without naming either: the ports sit at half-pixels
         *   (`left: 50%; margin-left: -5.5px`), which integer `offsetLeft` would corrupt, and
         *   anchor space is exactly 1px from node-position space because the wrapper has a
         *   1px border while `node.position` is measured against the layer's content box.
         *   Both fold into `dx`/`dy` and neither has to be stated anywhere.
         *
         * @param {string} nodeId
         * @param {string|null} portSelector
         * @returns {{x: number, y: number}|null}
         */
        function portAnchor(nodeId, portSelector) {
            if (!dragAnchors) return measuredAnchor(nodeId, portSelector);

            const key = nodeId + "|" + (portSelector || "");

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
         * Open the anchor cache for a gesture, and prime it.
         *
         * The `edgeRoute` call per connector is the priming: it walks every anchor the
         * gesture will need while nothing has moved yet, so the offsets are all taken from a
         * consistent layout rather than one measured halfway through a frame.
         *
         * @param {Array<string>} movingIds
         * @param {Array<object>} chrome - from `chromeForMovingNodes`
         */
        function beginDragAnchors(movingIds, chrome) {
            dragAnchors = { moving: {}, offsets: {}, frozen: {} };
            movingIds.forEach(function (id) { dragAnchors.moving[id] = true; });
            chrome.forEach(function (record) { config.edgeRoute(record.edge); });
        }

        function endDragAnchors() {
            dragAnchors = null;
        }

        /** Whether a gesture currently owns the anchor cache. */
        function anchorsHeld() {
            return !!dragAnchors;
        }

        /** The cursor, in the coordinates `node.position` is written in. */
        function cursorPoint(e) {
            const rect = wrapperEl.getBoundingClientRect();
            return {
                x: e.clientX + wrapperEl.scrollLeft - rect.left,
                y: e.clientY + wrapperEl.scrollTop - rect.top,
            };
        }

        /**
         * How far to the right the lane sits that a connector climbing back up runs in.
         *
         * Memoised for one drag frame. It reduces over every node and `edgeRoute` asks for it
         * once per back edge, so a canvas with a loop paid O(nodes × connectors) every frame.
         * Guarded on the anchor cache, which exists for exactly as long as a drag — single or
         * group — is running, so the memo cannot outlive the gesture that justified it.
         * Outside one this is the plain reduce it always was.
         */
        function returnLaneX() {
            if (dragAnchors && laneXCache !== null) return laneXCache;

            const rightmost = state.nodes.reduce(function (widest, node) {
                return Math.max(widest, (node.position || {}).x || 0);
            }, 0);

            const lane = rightmost + config.metrics().stepWidth + GC.ELBOW_LANE;
            if (dragAnchors) laneXCache = lane;
            return lane;
        }

        /** Drop the lane memo. Called whenever a position changes mid-gesture. */
        function invalidateLane() {
            laneXCache = null;
        }

        // =============================================================================
        // THE FRAME
        // =============================================================================

        function scheduleFrame() {
            if (dragFrame !== null) return;
            dragFrame = requestAnimationFrame(runFrame);
        }

        function cancelFrame() {
            if (dragFrame === null) return;
            cancelAnimationFrame(dragFrame);
            dragFrame = null;
        }

        /**
         * One frame of whichever gesture is live.
         *
         * A bend and a node move share this frame because only one of them is ever running,
         * and one repaint per tick is the whole point: mousemove fires faster than the screen
         * does, and the old code repainted per event.
         */
        function runFrame() {
            dragFrame = null;

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

            // Guarded: a block deleted mid-drag would otherwise throw here.
            const el = document.getElementById(config.nodeElementId(node.id));
            if (el) {
                el.style.left = node.position.x + "px";
                el.style.top = node.position.y + "px";
            }

            laneXCache = null;
            drag.chrome.forEach(updateEdgeGeometry);
        }

        /**
         * Drop a node drag in progress without committing it.
         *
         * The listener removal goes through the canvas, because the canvas owns the two
         * handlers — `startDrag`/`onDragMove`/`onDragEnd` stayed with it, being the one part
         * of this cluster the two canvases genuinely wrote differently.
         */
        function abandonDrag() {
            if (!state.dragging) return;
            cancelFrame();
            state.dragging = null;
            laneXCache = null;
            endDragAnchors();
            if (config.detachNodeDrag) config.detachNodeDrag();
        }

        // =============================================================================
        // EDGE CHROME
        // =============================================================================

        /** The elements one connector's geometry is written to, resolved once. */
        function resolveEdgeChrome(record) {
            const group = document.getElementById(config.edgeElementId(record.edge.id));
            record.group = group;
            record.path = document.getElementById(config.edgePathId(record.edge.id));
            record.hit = group ? group.querySelector(chromeClass("hit")) : null;
            record.deleteBtn = group ? group.querySelector(chromeClass("delete-btn")) : null;
            record.insertBtn = group ? group.querySelector(chromeClass("insert-btn")) : null;
            record.sourceHandle = group ? group.querySelector(chromeClass("handle-source")) : null;
            record.targetHandle = group ? group.querySelector(chromeClass("handle-target")) : null;
            record.waypointHandles = group
                ? Array.prototype.slice.call(group.querySelectorAll(chromeClass("waypoint")))
                : [];
            return record;
        }

        function edgeChrome(edge) {
            return resolveEdgeChrome({ edge: edge });
        }

        /** The connectors a move of these nodes will disturb, resolved ready to repaint. */
        function chromeForMovingNodes(movingIds) {
            const moving = {};
            movingIds.forEach(function (id) { moving[id] = true; });

            return drawableEdges()
                .filter(function (edge) { return moving[edge.source] || moving[edge.target]; })
                .map(edgeChrome);
        }

        /**
         * Rebuild one connector's DOM, replacing whatever was there.
         *
         * `renderEdge` appends. Every other caller reaches it through `renderAllEdges`, which
         * has just emptied the group — so calling it on its own without this would leave two
         * elements sharing one id, and `getElementById` would then keep finding the stale one.
         */
        function reRenderEdge(edge) {
            const existing = document.getElementById(config.edgeElementId(edge.id));
            if (existing) existing.remove();
            config.renderEdge(edge);
        }

        /**
         * Move an already-drawn connector to where its nodes are now.
         *
         * In place, and cheaper than a re-render by more than it looks: nothing is unparented,
         * so the ✕'s and the handles' listeners survive and the line never blinks out of the
         * document mid-drag. The Graph Designer used to remove the whole `<g>` and rebuild it
         * every frame, which is why a dragged wire there flickered.
         *
         * @param {object} record - from `edgeChrome`
         */
        function updateEdgeGeometry(record) {
            // A connector whose group is missing is built now rather than left out of the
            // drawing, and its new elements picked up for the next frame.
            if (!record.group || !record.group.isConnected) {
                reRenderEdge(record.edge);
                resolveEdgeChrome(record);
                return;
            }

            const route = config.edgeRoute(record.edge);
            const d = route ? GC.elbowPathD(route) : "";
            if (record.path) record.path.setAttribute("d", d);
            if (record.hit) record.hit.setAttribute("d", d);
            if (!route) return;

            const dy = config.chromeYOffset || 0;

            if (record.deleteBtn || record.insertBtn) {
                const mid = GC.pointAlongPolyline(route, 0.5);
                if (record.deleteBtn) {
                    record.deleteBtn.setAttribute(
                        "transform",
                        "translate(" + (mid.x - config.buttonGapPx) + "," + (mid.y + dy) + ")");
                }
                if (record.insertBtn) {
                    record.insertBtn.setAttribute(
                        "transform",
                        "translate(" + (mid.x + config.buttonGapPx) + "," + (mid.y + dy) + ")");
                }
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

            // The bend handles follow the *stored* points, not the line: `elbowPathD` pulls
            // the stroke up to the corner radius inside every corner, so a handle drawn on
            // the stroke would sit visibly off the pixel the hand put it at.
            if (record.waypointHandles.length) {
                const bends = GC.waypointsOf(record.edge);
                record.waypointHandles.forEach(function (handle) {
                    const bend = bends[Number(handle.getAttribute("data-waypoint"))];
                    if (!bend) return;
                    handle.setAttribute("cx", bend.x);
                    handle.setAttribute("cy", bend.y);
                });
            }
        }

        // =============================================================================
        // BENDS
        //
        // Pressing a connector and moving puts a corner in it. The gesture shares the frame
        // above with the node drag, because only one of them is ever live.
        // =============================================================================

        /** Both ends of a connector, as this canvas measures them. */
        function endsOf(edge) {
            return {
                from: config.sourceAnchor(edge),
                to: config.targetAnchor(edge),
            };
        }

        /**
         * What a press on a connector means.
         *
         * `"group"` requires a **multi**-item selection rather than merely a selected
         * connector. Clicking a wire selects it, so if "selected" alone meant group-drag,
         * the ordinary sequence of clicking a wire and then dragging it would move two nodes
         * instead of putting a bend in it.
         *
         * @returns {"group"|"bend"|"ignore"}
         */
        function edgeGrabIntent(edgeId, e) {
            if (e.button !== 0) return "ignore";
            // Modifiers belong to the selection: the click that follows extends it.
            if (e.shiftKey || e.ctrlKey || e.metaKey) return "ignore";
            if (config.isBusy()) return "ignore";

            const selection = config.getSelection();
            if (selection && selection.isMulti() && selection.hasEdge(edgeId)) return "group";
            return "bend";
        }

        /** Begin a bend: either move the bend that was grabbed, or put a new one in. */
        function startBend(edgeId, e) {
            const intent = edgeGrabIntent(edgeId, e);
            if (intent === "ignore") return;

            if (intent === "group") {
                // The selection owns this press. It needs a node to hang the move on, and
                // either end of this connector will do — the connector being in the
                // selection is what brings both of them along.
                const held = findEdge(edgeId);
                if (held) config.getSelection().beginNodePress(held.source, e);
                return;
            }

            const edge = findEdge(edgeId);
            if (!edge) return;
            // A derived connector is rebuilt from its block's settings on every render, so a
            // bend written onto one would vanish without a word.
            if (config.isRoutable && !config.isRoutable(edge)) return;

            e.preventDefault();
            e.stopPropagation();

            const bends = GC.waypointsOf(edge).map(function (bend) {
                return { x: bend.x, y: bend.y };
            });
            const at = cursorPoint(e);

            // Near an existing bend: move that one.
            let index = -1;
            bends.forEach(function (bend, i) {
                if (index === -1 &&
                    Math.abs(bend.x - at.x) <= config.waypointGrabPx &&
                    Math.abs(bend.y - at.y) <= config.waypointGrabPx) {
                    index = i;
                }
            });
            let inserted = false;

            if (index === -1) {
                if (bends.length >= config.maxWaypoints) {
                    // Refused with a sentence, rather than by doing nothing.
                    config.flash("A connector can have at most " + config.maxWaypoints +
                        " bends. Drag one of the bends it already has instead.");
                    return;
                }

                const route = config.edgeRoute(edge);
                const segment = route ? GC.nearestSegment(route, at) : null;
                if (!segment) return;

                // Where in the list the new bend goes: after every existing bend that is on
                // an earlier leg, so the order of the list stays the order of the wire.
                const ends = endsOf(edge);
                const detail = GC.waypointRoute(ends.from, ends.to, bends);
                const marks = detail ? detail.waypointAt : [];
                index = marks.filter(function (mark) { return mark <= segment.index; }).length;
                // Started at the cursor's foot on the leg that was grabbed, so the wire does
                // not jump the instant it is taken hold of.
                bends.splice(index, 0, { x: segment.projection.x, y: segment.projection.y });
                inserted = true;
            }

            edge.waypoints = bends;
            // A handle has to exist for the new bend, so this connector is rebuilt once —
            // and only this one.
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
            // Both ends are still, so the frame reads nothing: the anchor cache freezes them
            // and the whole gesture is arithmetic.
            beginDragAnchors([], [bending.chrome]);

            document.addEventListener("mousemove", onBendMove);
            document.addEventListener("mouseup", onBendEnd);
        }

        function onBendMove(e) {
            if (!bending) return;

            if (!bending.moved) {
                // How "far" is measured is the canvas's, not this file's: the Flow Builder
                // uses Chebyshev distance and the Graph Designer Manhattan. Preserved rather
                // than unified, because unifying it changes how one of them feels.
                if (config.travelled(e.clientX - bending.fromX, e.clientY - bending.fromY)
                    < config.thresholdPx) return;
                bending.moved = true;
            }

            bending.clientX = e.clientX;
            bending.clientY = e.clientY;
            scheduleFrame();
        }

        /** One frame of a bend. Shares the drag frame, so only one gesture repaints a tick. */
        function runBendFrame() {
            const edge = findEdge(bending.edgeId);
            if (!edge) return;

            const bends = GC.waypointsOf(edge);
            const bend = bends[bending.index];
            if (!bend) return;

            const at = cursorPoint({ clientX: bending.clientX, clientY: bending.clientY });

            // Lines to snap to: the two ends of the wire, and the neighbouring bends. This is
            // what makes a hand-routed wire look drawn rather than approximately placed.
            const ends = endsOf(edge);
            const xs = [];
            const ys = [];
            if (ends.from) { xs.push(ends.from.x); ys.push(ends.from.y); }
            if (ends.to) { xs.push(ends.to.x); ys.push(ends.to.y); }
            bends.forEach(function (other, i) {
                if (i === bending.index) return;
                xs.push(other.x);
                ys.push(other.y);
            });

            bend.x = Math.max(0, GC.snapToAny(at.x, xs, config.waypointSnapPx));
            bend.y = Math.max(0, GC.snapToAny(at.y, ys, config.waypointSnapPx));

            laneXCache = null;
            updateEdgeGeometry(bending.chrome);
        }

        function onBendEnd() {
            const gesture = bending;
            cancelFrame();
            bending = null;
            endDragAnchors();
            laneXCache = null;
            document.removeEventListener("mousemove", onBendMove);
            document.removeEventListener("mouseup", onBendEnd);

            if (!gesture) return;

            const edge = findEdge(gesture.edgeId);
            if (!edge) return;

            // A press that never moved is a click, and the click listener on the hit path
            // will select the connector. A bend inserted for it is taken back out — clicking
            // a wire must not leave a bend behind.
            if (!gesture.moved) {
                if (gesture.inserted) {
                    edge.waypoints.splice(gesture.index, 1);
                    if (!edge.waypoints.length) delete edge.waypoints;
                    reRenderEdge(edge);
                }
                return;
            }

            // Dropped back onto the line it would take without this bend: the bend is
            // bending nothing, so it goes and the wire straightens itself.
            discardRedundantWaypoint(edge, gesture.index);

            reRenderEdge(edge);
            config.markDirty();
            // Manual, for the same reason a moved block switches: a hand-routed wire is only
            // meaningful against a known arrangement, and an auto-arrange is free to move
            // both of its ends out from under it.
            state.layout = "manual";
            config.updateLayoutButton();

            swallowEdgeClick();
        }

        /** Take a bend out if it is sitting on the line the wire would take without it. */
        function discardRedundantWaypoint(edge, index) {
            const bends = GC.waypointsOf(edge);
            const bend = bends[index];
            if (!bend) return;

            const without = bends.slice();
            without.splice(index, 1);

            const ends = endsOf(edge);
            if (!ends.from || !ends.to) return;

            const plain = state.backEdges[edge.id] && !without.length
                ? GC.backEdgePoints(ends.from, ends.to, returnLaneX())
                : GC.waypointPoints(ends.from, ends.to, without);
            const segment = plain ? GC.nearestSegment(plain, bend) : null;

            if (segment && segment.distance <= config.waypointDiscardPx) {
                edge.waypoints = without;
                if (!edge.waypoints.length) delete edge.waypoints;
            }
        }

        /** Clear every bend on one connector. */
        function straightenEdge(edgeId) {
            const edge = findEdge(edgeId);
            if (!edge || !GC.waypointsOf(edge).length) return;

            delete edge.waypoints;
            reRenderEdge(edge);
            config.markDirty();
        }

        // =============================================================================
        // GROUP MOVE — the callbacks graph_selection.js drives
        // =============================================================================

        let groupChrome = null;
        let groupBends = null;

        /**
         * Connectors whose **source and target both move**, with their bends captured.
         *
         * Both ends, not either: a bend exists to dodge something on the canvas, so when only
         * one end moves the bend should stay where it was put. When the whole run moves, it
         * travels with it.
         */
        function captureCarriedBends(ids) {
            const moving = {};
            ids.forEach(function (id) { moving[id] = true; });

            return state.edges
                .filter(function (edge) {
                    return moving[edge.source] && moving[edge.target] &&
                        GC.waypointsOf(edge).length;
                })
                .map(function (edge) {
                    return {
                        edge: edge,
                        starts: GC.waypointsOf(edge).map(function (bend) {
                            return { x: bend.x, y: bend.y };
                        }),
                    };
                });
        }

        function onGroupMoveBegin(ids) {
            // Manual from the first frame that really moves, exactly as a single drag
            // switches then and for the same reason: the layout request is debounced, so its
            // answer can arrive while the mouse is still down and re-place every block.
            state.layout = "manual";
            config.updateLayoutButton();

            groupChrome = chromeForMovingNodes(ids);
            beginDragAnchors(ids, groupChrome);
            groupBends = captureCarriedBends(ids);
        }

        function onGroupMoveFrame(ids, dx, dy) {
            // From the captured start plus the delta, never by accumulating — the same
            // reason the blocks themselves are placed that way.
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

        function onGroupMoveEnd(ids, committed) {
            endDragAnchors();
            groupChrome = null;
            groupBends = null;
            laneXCache = null;

            if (!committed) return;
            // A block dragged toward an edge has to be able to go there, and the canvas only
            // scrolls as far as its own box.
            config.fitCanvas();
            config.markDirty();
        }

        function swallowEdgeClick() {
            suppressEdgeClick = true;
            setTimeout(function () { suppressEdgeClick = false; }, 0);
        }

        return {
            // anchors
            portAnchor: portAnchor,
            measuredAnchor: measuredAnchor,
            beginDragAnchors: beginDragAnchors,
            endDragAnchors: endDragAnchors,
            anchorsHeld: anchorsHeld,
            cursorPoint: cursorPoint,
            returnLaneX: returnLaneX,
            invalidateLane: invalidateLane,

            // the frame
            scheduleFrame: scheduleFrame,
            cancelFrame: cancelFrame,
            abandonDrag: abandonDrag,

            // edge chrome
            edgeChrome: edgeChrome,
            resolveEdgeChrome: resolveEdgeChrome,
            chromeForMovingNodes: chromeForMovingNodes,
            reRenderEdge: reRenderEdge,
            updateEdgeGeometry: updateEdgeGeometry,

            // bends
            edgeGrabIntent: edgeGrabIntent,
            startBend: startBend,
            straightenEdge: straightenEdge,

            // the callbacks graph_selection.js drives
            onGroupMoveBegin: onGroupMoveBegin,
            onGroupMoveFrame: onGroupMoveFrame,
            onGroupMoveEnd: onGroupMoveEnd,

            // the one-tick swallow a connector's click handler consults
            swallowedEdgeClick: function () { return suppressEdgeClick; },
            swallowEdgeClick: swallowEdgeClick,
        };
    }

    window.GraphEdges = { create: create };
})();
