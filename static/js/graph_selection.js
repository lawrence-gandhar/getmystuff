/**
 * Selecting several things on a canvas, and moving them together.
 *
 * Shared by the Graph Designer, the Flow Builder and the Integrations canvas. Those three
 * are separate implementations on purpose — what a node *means*, how it is built, what a
 * port is, all of that is per-feature and stays there. But a rubber-band box, a set of
 * picked ids, a group move that keeps its shape, and the keyboard that goes with them are
 * the same gesture on all three, and a gesture that behaves differently on each canvas is
 * a gesture a user has to learn three times.
 *
 * So the line this file draws is: **the geometry and the gesture are shared; what a
 * selection means is not.** `graph_canvas.js` keeps the arithmetic, because it is stateless
 * and says so. This file keeps the gesture, because a gesture is state — where the press
 * started, what was selected before it, which frame is pending — and that is exactly what
 * `graph_canvas.js` refuses to hold.
 *
 * What stays with each canvas: whether a properties panel opens, which class paints a
 * selected thing, what "commit" means, and the single-item drag it already had. This module
 * takes over a press only when the press is a Ctrl-click or a genuine group move
 * (`beginNodePress` says which by returning a boolean), so the existing one-node drag in
 * all three files runs exactly as it did.
 */
window.GraphSelection = (function () {
    "use strict";

    const GC = window.GraphCanvas;

    // Pointer events, not mouse events, and that is a deliberate choice about sharing.
    // Two of the three canvases drive their own drags with `mousedown`, the third with
    // `pointerdown`. Pointer events are the family that works from either starting point:
    // for a mouse, `pointermove`/`pointerup` fire whether the press that began the gesture
    // was a `mousedown` or a `pointerdown`, so this module can be handed a MouseEvent by
    // one canvas and a PointerEvent by another without caring which. The reverse is not
    // true, and choosing mouse events would have meant converting a working canvas to suit
    // a new module.

    // How close to the wrapper's edge the cursor has to get before the canvas starts
    // scrolling itself, and the most it will scroll in one frame. A box dragged toward the
    // edge of a long flow has to be able to keep going, and the cursor stops sending
    // mousemoves the moment it is parked still — so the scroll is driven by a clock.
    const EDGE_BAND = 36;
    const EDGE_STEP_MAX = 24;

    // Below this a press on empty canvas is a click, not a box. Each canvas passes its own
    // drag threshold for node presses; this one only has to be big enough that a click
    // never paints a one-pixel rectangle.
    const MARQUEE_MIN_PX = 4;

    /**
     * A selection controller for one canvas.
     *
     * @param {object} config - see the README-shaped comment at each call site; every key
     *   is a per-canvas difference and there are no keys that are not
     * @returns {object} the controller
     */
    function create(config) {
        const wrapperEl = config.wrapperEl;
        const layerEl = config.layerEl;
        const selection = config.selection;

        // The gesture in progress — at most one of these is ever non-null.
        let marquee = null;
        let groupDrag = null;

        // The rubber band itself, created once and re-used.
        let boxEl = null;

        // True for one tick after a gesture that must not be followed by its own trailing
        // click. See `swallowedClick`.
        let swallow = false;

        let frame = null;
        let scrollFrame = null;

        // -------------------------------------------------------------
        // Coordinates
        //
        // Measured against the **node layer**, not the wrapper plus its scroll offset.
        // Those are the same number for a scrolling canvas, and the layer is the one that
        // is right for all three: it is the element whose box `node.position` is expressed
        // against, so a rectangle placed at these coordinates lands pixel-exact on the
        // nodes it is selecting, with no border or scroll arithmetic to get wrong.
        //
        // Re-read per frame rather than cached: the canvas can scroll under the cursor
        // (that is what the auto-scroll below does) and the Graph Designer's dock is
        // resizable.
        // -------------------------------------------------------------

        function toCanvas(clientX, clientY) {
            const rect = layerEl.getBoundingClientRect();
            return { x: clientX - rect.left, y: clientY - rect.top };
        }

        // -------------------------------------------------------------
        // The selection itself
        // -------------------------------------------------------------

        function nodeIds() {
            return Object.keys(selection.nodes);
        }

        function edgeIds() {
            return Object.keys(selection.edges);
        }

        function count() {
            return nodeIds().length + edgeIds().length;
        }

        function hasNode(id) {
            return !!selection.nodes[id];
        }

        function hasEdge(id) {
            return !!selection.edges[id];
        }

        /**
         * Paint the current selection, and tell the canvas it changed.
         *
         * Only the class is touched — never the DOM structure. A live box updates the
         * selection as the hand moves, and a canvas that re-rendered itself on each change
         * would rebuild every node and every connector many times a second.
         */
        function changed() {
            config.getNodes().forEach(function (node) {
                const el = document.getElementById(config.nodeElementId(node.id));
                if (el) el.classList.toggle(config.classes.node, marksNode(node.id));
            });

            config.getSelectableEdges().forEach(function (edge) {
                const el = document.getElementById(config.edgeElementId(edge.id));
                if (el) el.classList.toggle(config.classes.edge, marksEdge(edge.id));
            });

            paintSelectAllButton();

            if (config.onSelectionChange) config.onSelectionChange();
        }

        /**
         * Keep the header's **Select all** / **Clear (n)** button in step.
         *
         * Here rather than in each canvas because all three had the same fourteen lines,
         * differing only in the button's id and one noun. It is the selection's own count on
         * the selection's own button, so this is where it belongs — and unlike the connector
         * runtime the two top-down canvases share, Integrations gets this one too.
         *
         * Silent when `selectAllButtonId` is not configured, so a canvas without such a
         * button needs no opt-out.
         */
        function paintSelectAllButton() {
            if (!config.selectAllButtonId) return;

            const btn = document.getElementById(config.selectAllButtonId);
            if (!btn) return;

            const n = count();
            btn.classList.toggle("btn-outline-primary", n > 0);
            btn.classList.toggle("btn-outline-secondary", n === 0);
            btn.innerHTML = n > 0
                ? '<i class="las la-times-circle"></i> Clear (' + n + ")"
                : '<i class="las la-object-group"></i> Select all';
            btn.title = n > 0
                ? "Clear the move selection"
                : "Select every " + (config.selectAllNoun || "block and connector") +
                  ", so they can be moved together";
        }

        /**
         * Whether a node should carry the "moves with the next drag" mark.
         *
         * Not simply "is it in the set", and the asymmetry with `marksEdge` below is
         * deliberate.
         *
         * Pressing any node puts it in the move set, because a drag of one node is a
         * group of one and the code should not have two paths for that. But every canvas
         * already draws a ring on the node whose settings are open, and painting a second
         * mark on top of it every time somebody clicks a node would be telling them
         * something they can already see. So the mark appears when it starts to mean
         * something: when more than one thing will move.
         *
         * @param {string} id
         * @returns {boolean}
         */
        function marksNode(id) {
            return hasNode(id) && count() > 1;
        }

        /**
         * Whether a connector should carry the mark.
         *
         * Always, when it is in the set — unlike a node. A connector caught by a box is not
         * the connector whose properties are open, so nothing else on the canvas says it
         * has been caught: without this, selecting one wire and dragging it would move two
         * nodes for no visible reason.
         *
         * @param {string} id
         * @returns {boolean}
         */
        function marksEdge(id) {
            return hasEdge(id);
        }

        function clear() {
            if (!count()) return;
            selection.nodes = {};
            selection.edges = {};
            changed();
        }

        /**
         * Reduce the selection to one node, without opening anything.
         *
         * Deliberately not "select this node" in the canvas's sense: that function opens a
         * properties panel, and this is called from a press that may be about to become a
         * drag. The canvas decides separately whether a panel opens, on release.
         *
         * @param {string} nodeId
         */
        function selectOnly(nodeId) {
            selection.nodes = {};
            selection.edges = {};
            if (nodeId) selection.nodes[nodeId] = true;
            changed();
        }

        function toggleNode(nodeId) {
            if (selection.nodes[nodeId]) delete selection.nodes[nodeId];
            else selection.nodes[nodeId] = true;
            changed();
        }

        function toggleEdge(edgeId) {
            if (selection.edges[edgeId]) delete selection.edges[edgeId];
            else selection.edges[edgeId] = true;
            changed();
        }

        /**
         * Everything on the canvas.
         *
         * Connectors as well as nodes. Selecting every node but no connector would be an
         * odd half-state, and since a selected connector only ever contributes nodes that
         * are already in the set, including them costs nothing and means "select all"
         * means what it says.
         */
        function selectAll() {
            selection.nodes = {};
            selection.edges = {};
            config.getNodes().forEach(function (node) { selection.nodes[node.id] = true; });
            config.getSelectableEdges().forEach(function (edge) { selection.edges[edge.id] = true; });
            changed();
        }

        /**
         * Drop ids that no longer exist.
         *
         * Re-derived from the canvas rather than deleting the one id that was removed:
         * deleting a node takes its connectors with it, so a stale *edge* id can outlive
         * the node deletion that caused it. Re-deriving covers every removal path there is
         * now and every one added later.
         */
        function prune() {
            const before = count();
            const nodes = {};
            const edges = {};

            config.getNodes().forEach(function (node) {
                if (selection.nodes[node.id]) nodes[node.id] = true;
            });
            config.getSelectableEdges().forEach(function (edge) {
                if (selection.edges[edge.id]) edges[edge.id] = true;
            });

            selection.nodes = nodes;
            selection.edges = edges;
            if (before !== count()) changed();
        }

        // -------------------------------------------------------------
        // The trailing click
        //
        // A click always follows the mouseup that ended a drag, and its target is whatever
        // the press landed on. For a box that means the wrapper — whose existing click
        // handler clears the selection. Without this the whole feature would appear to do
        // nothing: the box would select three nodes and the click behind it would
        // immediately unselect them.
        // -------------------------------------------------------------

        function swallowNextClick() {
            swallow = true;
            setTimeout(function () { swallow = false; }, 0);
            if (config.onSwallowClick) config.onSwallowClick();
        }

        function swallowedClick() {
            return swallow;
        }

        // -------------------------------------------------------------
        // Auto-scroll
        // -------------------------------------------------------------

        function cursor() {
            if (marquee) return { x: marquee.clientX, y: marquee.clientY };
            if (groupDrag) return { x: groupDrag.clientX, y: groupDrag.clientY };
            return null;
        }

        /** How far to scroll this frame, on one axis. 0 when the cursor is clear of the edge. */
        function scrollStep(position, low, high) {
            if (position < low + EDGE_BAND) {
                return -Math.min(EDGE_STEP_MAX, Math.max(1, low + EDGE_BAND - position));
            }
            if (position > high - EDGE_BAND) {
                return Math.min(EDGE_STEP_MAX, Math.max(1, position - (high - EDGE_BAND)));
            }
            return 0;
        }

        function runScrollFrame() {
            scrollFrame = null;
            const at = cursor();
            if (!at) return;

            const rect = wrapperEl.getBoundingClientRect();
            const dx = scrollStep(at.x, rect.left, rect.right);
            const dy = scrollStep(at.y, rect.top, rect.bottom);

            // Nothing to do, and no reason to keep a frame loop alive: the next mousemove
            // that enters the band starts it again. A loop that ran for as long as a box
            // was open would burn a frame callback for the whole gesture.
            if (!dx && !dy) return;

            const wasLeft = wrapperEl.scrollLeft;
            const wasTop = wrapperEl.scrollTop;
            wrapperEl.scrollLeft += dx;
            wrapperEl.scrollTop += dy;

            // Already against the end of the scroll on both axes. `scrollLeft` clamps
            // itself, so the arithmetic above happily asks for a scroll that cannot happen
            // — and without this the loop would keep asking, every frame, for as long as
            // the cursor sat in the band at the edge of a canvas that had run out.
            if (wrapperEl.scrollLeft === wasLeft && wrapperEl.scrollTop === wasTop) return;

            // The cursor has not moved but the canvas under it has, so the gesture's
            // geometry has changed and has to be recomputed from the same client point.
            scheduleFrame();
            scrollFrame = requestAnimationFrame(runScrollFrame);
        }

        function scheduleScroll() {
            if (scrollFrame !== null) return;
            scrollFrame = requestAnimationFrame(runScrollFrame);
        }

        function cancelScroll() {
            if (scrollFrame === null) return;
            cancelAnimationFrame(scrollFrame);
            scrollFrame = null;
        }

        // -------------------------------------------------------------
        // Frames
        //
        // Both gestures record where the cursor is and ask for a frame; one frame does the
        // drawing, however many mousemoves arrived in between. mousemove fires faster than
        // the screen updates, so work done per event is largely work thrown away unseen.
        // -------------------------------------------------------------

        function scheduleFrame() {
            if (frame !== null) return;
            frame = requestAnimationFrame(runFrame);
        }

        function cancelFrame() {
            if (frame === null) return;
            cancelAnimationFrame(frame);
            frame = null;
        }

        function runFrame() {
            frame = null;
            if (marquee) drawMarquee();
            else if (groupDrag) moveGroup();
        }

        // -------------------------------------------------------------
        // The rubber band
        // -------------------------------------------------------------

        function ensureBox() {
            if (!boxEl) {
                boxEl = document.createElement("div");
                boxEl.className = "gc-marquee";
            }
            // The node layer is rebuilt wholesale by every canvas's `renderAllNodes`, which
            // would take the box with it. Nothing re-renders while a box is open today, so
            // this is a guard rather than a fix — but it is two lines and the alternative
            // failure is an invisible box that still selects things.
            if (!boxEl.isConnected) layerEl.appendChild(boxEl);
            return boxEl;
        }

        function removeBox() {
            if (boxEl && boxEl.isConnected) boxEl.remove();
        }

        /**
         * Every node's box, in canvas coordinates.
         *
         * Position comes from the model and size from the DOM, and the split is deliberate.
         * The model is what a group move changes and what the save posts, so it is the
         * authority on where a node is. But nothing in JavaScript knows how *tall* a node
         * is — a branch with six conditions and a step with a two-line preview differ, and
         * by an amount only the browser has worked out. So height is measured, once,
         * because nothing can move while a box is being dragged.
         */
        function measureNodes() {
            return config.getNodes().map(function (node) {
                const el = document.getElementById(config.nodeElementId(node.id));
                const rect = el ? el.getBoundingClientRect() : null;
                return {
                    id: node.id,
                    x: (node.position || {}).x || 0,
                    y: (node.position || {}).y || 0,
                    w: rect ? rect.width : config.nodeWidth(),
                    h: rect ? rect.height : 0,
                };
            });
        }

        function drawMarquee() {
            const at = toCanvas(marquee.clientX, marquee.clientY);
            const rect = GC.rectFromPoints({ x: marquee.originX, y: marquee.originY }, at);

            const box = ensureBox();
            box.style.left = rect.x + "px";
            box.style.top = rect.y + "px";
            box.style.width = rect.w + "px";
            box.style.height = rect.h + "px";

            marquee.rect = rect;
        }

        /**
         * Commit a box: whatever it touches becomes the selection.
         *
         * **Touches**, not contains. Containment would mean dragging a box right around a
         * node to catch it, and a node's branch pills are wider than its own box — so the
         * obvious gesture of sweeping across a column of nodes would select none of them.
         */
        function commitMarquee() {
            const rect = marquee.rect;
            if (!rect) return;

            if (!marquee.additive) {
                selection.nodes = {};
                selection.edges = {};
            }

            marquee.boxes.forEach(function (box) {
                if (GC.rectsIntersect(box, rect)) selection.nodes[box.id] = true;
            });

            config.getSelectableEdges().forEach(function (edge) {
                const route = config.edgeRoute(edge);
                if (route && GC.polylineIntersectsRect(route, rect)) {
                    selection.edges[edge.id] = true;
                }
            });

            changed();
        }

        function onWrapperPointerDown(e) {
            if (e.button !== 0) return;
            if (marquee || groupDrag) return;

            // What counts as background is the same test the canvas's own click-to-deselect
            // uses, so there are not two answers to it. A press on a node, a port, a pill
            // or a connector has one of those as its target and is none of this module's
            // business.
            const onBackground = e.target === wrapperEl ||
                e.target === layerEl ||
                (config.edgesEl && e.target === config.edgesEl);
            if (!onBackground) return;

            // An armed port is waiting for a target click, and a click on the background is
            // the documented way out of it. Turning that press into a box would strand the
            // user half way through drawing a connector.
            if (config.isBusy && config.isBusy()) return;

            // Stops the browser selecting the node labels the box is dragged over, and
            // stops its own drag-image appearing — the same call every node drag makes.
            //
            // It also suppresses the mousedown that would otherwise follow, and with it the
            // focus that press would have given the wrapper — so focus is taken here
            // instead. Without it Ctrl+A and Escape would do nothing until the user
            // happened to press something that was not the background.
            e.preventDefault();
            wrapperEl.focus();

            const at = toCanvas(e.clientX, e.clientY);
            marquee = {
                originX: at.x,
                originY: at.y,
                fromX: e.clientX,
                fromY: e.clientY,
                clientX: e.clientX,
                clientY: e.clientY,
                // Read at the press, not at the release. The modifier can be let go mid
                // drag, and the release is where the commit happens — reading it late would
                // silently discard what the user asked for.
                additive: e.ctrlKey || e.metaKey,
                moved: false,
                boxes: null,
                rect: null,
            };

            document.addEventListener("pointermove", onMarqueeMove);
            document.addEventListener("pointerup", onMarqueeUp);
        }

        function onMarqueeMove(e) {
            if (!marquee) return;

            if (!marquee.moved) {
                const travelled = Math.max(
                    Math.abs(e.clientX - marquee.fromX),
                    Math.abs(e.clientY - marquee.fromY),
                );
                if (travelled < MARQUEE_MIN_PX) return;
                marquee.moved = true;
                // Measured at the transition rather than at the press, so a plain click on
                // the background costs nothing at all.
                marquee.boxes = measureNodes();
            }

            marquee.clientX = e.clientX;
            marquee.clientY = e.clientY;
            scheduleFrame();
            scheduleScroll();
        }

        function onMarqueeUp() {
            const box = marquee;
            cancelFrame();
            cancelScroll();
            document.removeEventListener("pointermove", onMarqueeMove);
            document.removeEventListener("pointerup", onMarqueeUp);
            marquee = null;
            removeBox();

            if (!box) return;

            // A press that never moved is a plain background click. Nothing happens here,
            // and the canvas's existing click handler runs untouched — it cancels an armed
            // port and clears the selection, exactly as it always did.
            if (!box.moved) return;

            marquee = box;
            commitMarquee();
            marquee = null;
            swallowNextClick();
        }

        // -------------------------------------------------------------
        // The group move
        // -------------------------------------------------------------

        /**
         * Which nodes a drag of this selection moves.
         *
         * A selected connector brings the two nodes it joins. That is what makes selecting
         * a wire useful: it is the quick way to pick up a step and the step it feeds
         * without drawing a box around both.
         */
        function movingNodeIds() {
            const ids = {};
            nodeIds().forEach(function (id) { ids[id] = true; });

            config.getSelectableEdges().forEach(function (edge) {
                if (!hasEdge(edge.id)) return;
                ids[edge.source] = true;
                ids[edge.target] = true;
            });

            return Object.keys(ids);
        }

        /**
         * Take over a press on a node, or decline it.
         *
         * Declining is the important half. Returning false leaves the canvas to run the
         * single-node drag it already had, with all of its own click and modifier
         * behaviour intact — so adopting this module changes nothing about a press on an
         * unselected node.
         *
         * @param {string} nodeId
         * @param {MouseEvent} e
         * @returns {boolean} true when this module has taken the press
         */
        function beginNodePress(nodeId, e) {
            if (e.button !== 0) return false;

            // Ctrl-click edits the selection and does not drag. Letting it also start a
            // drag would nudge a node every time somebody added one to the set.
            if (e.ctrlKey || e.metaKey) {
                e.preventDefault();
                toggleNode(nodeId);
                swallowNextClick();
                return true;
            }

            // A press on something outside the selection abandons it, which is what every
            // editor of this kind does. The node pressed becomes the selection so that a
            // drag of it is still a group of one, then the canvas drags it as usual.
            if (!hasNode(nodeId)) {
                selectOnly(nodeId);
                return false;
            }

            const moving = movingNodeIds();
            if (moving.length < 2) return false;

            e.preventDefault();
            startGroupDrag(moving, e);
            return true;
        }

        function startGroupDrag(moving, e) {
            const at = toCanvas(e.clientX, e.clientY);
            const starts = [];
            let minX = Infinity;
            let minY = Infinity;

            config.getNodes().forEach(function (node) {
                if (moving.indexOf(node.id) === -1) return;
                const x = (node.position || {}).x || 0;
                const y = (node.position || {}).y || 0;
                starts.push({ node: node, x: x, y: y });
                minX = Math.min(minX, x);
                minY = Math.min(minY, y);
            });

            if (!starts.length) return;

            groupDrag = {
                originX: at.x,
                originY: at.y,
                fromX: e.clientX,
                fromY: e.clientY,
                clientX: e.clientX,
                clientY: e.clientY,
                // Captured once. Recomputing an offset against a live `node.position` — the
                // way a single drag does — is fine for one node and wrong for a group: the
                // second node's motion would be measured from a position the first node's
                // frame had already changed.
                starts: starts,
                minX: minX === Infinity ? 0 : minX,
                minY: minY === Infinity ? 0 : minY,
                ids: starts.map(function (entry) { return entry.node.id; }),
                moved: false,
            };

            document.addEventListener("pointermove", onGroupMove);
            document.addEventListener("pointerup", onGroupUp);
        }

        function onGroupMove(e) {
            if (!groupDrag) return;

            if (!groupDrag.moved) {
                const travelled = Math.abs(e.clientX - groupDrag.fromX) +
                    Math.abs(e.clientY - groupDrag.fromY);
                if (travelled < config.threshold) return;
                groupDrag.moved = true;
                if (config.onGroupMoveBegin) config.onGroupMoveBegin(groupDrag.ids);
            }

            groupDrag.clientX = e.clientX;
            groupDrag.clientY = e.clientY;
            scheduleFrame();
            scheduleScroll();
        }

        /**
         * One frame of a group move.
         *
         * One delta, applied to every member from its captured start. The clamp is on the
         * **delta**, not on each node, and that is the part worth reading twice: clamping
         * per node — which is what a single drag does, correctly, for one node — would
         * stop the leftmost member at the wall while the rest kept going, and the shape of
         * the selection would be permanently squashed. There is no undo on these canvases,
         * so that damage sticks. Clamping the delta slides the whole group along the wall
         * with its spacing exactly preserved.
         */
        function moveGroup() {
            const at = toCanvas(groupDrag.clientX, groupDrag.clientY);
            const dx = Math.max(at.x - groupDrag.originX, -groupDrag.minX);
            const dy = Math.max(at.y - groupDrag.originY, -groupDrag.minY);

            groupDrag.starts.forEach(function (entry) {
                entry.node.position = { x: entry.x + dx, y: entry.y + dy };
                const el = document.getElementById(config.nodeElementId(entry.node.id));
                if (el) {
                    el.style.left = entry.node.position.x + "px";
                    el.style.top = entry.node.position.y + "px";
                }
            });

            // The delta is handed over as well as the ids, because a canvas may have its
            // own things to carry: a connector routed by hand between two nodes that are
            // *both* moving has to keep its bends, and those are stored in canvas
            // coordinates rather than relative to the nodes.
            if (config.onGroupMoveFrame) config.onGroupMoveFrame(groupDrag.ids, dx, dy);
        }

        function onGroupUp() {
            const drag = groupDrag;
            cancelFrame();
            cancelScroll();
            document.removeEventListener("pointermove", onGroupMove);
            document.removeEventListener("pointerup", onGroupUp);
            groupDrag = null;

            if (!drag) return;
            // Never moved: nothing began, so there is nothing to commit or tear down, and
            // no trailing click to swallow either — the canvas's own click behaviour on a
            // press that did not move is untouched.
            if (!drag.moved) return;

            if (config.onGroupMoveEnd) config.onGroupMoveEnd(drag.ids, true);
            swallowNextClick();
        }

        /**
         * Put a group move back where it started, and drop it.
         *
         * The captured start positions make this nearly free, and it is the one undo this
         * feature can honestly offer: Escape during a move puts fifteen nodes back.
         */
        function abortGroupDrag() {
            if (!groupDrag) return false;

            groupDrag.starts.forEach(function (entry) {
                entry.node.position = { x: entry.x, y: entry.y };
                const el = document.getElementById(config.nodeElementId(entry.node.id));
                if (el) {
                    el.style.left = entry.x + "px";
                    el.style.top = entry.y + "px";
                }
            });

            const ids = groupDrag.ids;
            const began = groupDrag.moved;
            cancelFrame();
            cancelScroll();
            document.removeEventListener("pointermove", onGroupMove);
            document.removeEventListener("pointerup", onGroupUp);
            groupDrag = null;

            if (began) {
                // The connectors have to be put back too, and then whatever the canvas set
                // up when the move began has to be torn down — the same call the committed
                // path makes, with `false` so it knows not to mark anything.
                // A zero delta, which is what puts anything the canvas was carrying back
                // where it started too.
                if (config.onGroupMoveFrame) config.onGroupMoveFrame(ids, 0, 0);
                if (config.onGroupMoveEnd) config.onGroupMoveEnd(ids, false);
            }
            return true;
        }

        function abortMarquee() {
            if (!marquee) return false;
            cancelFrame();
            cancelScroll();
            document.removeEventListener("pointermove", onMarqueeMove);
            document.removeEventListener("pointerup", onMarqueeUp);
            marquee = null;
            removeBox();
            return true;
        }

        /**
         * Give up any gesture in progress without committing it.
         *
         * Called by the canvas from its deletion paths: a group move holds direct
         * references to node objects and to the connectors it is repainting, and deleting
         * one of them would leave the frame writing to something detached.
         */
        function abandon() {
            abortMarquee();
            abortGroupDrag();
        }

        // -------------------------------------------------------------
        // Keyboard
        //
        // Bound to the **wrapper**, which carries `tabindex="0"`, rather than to the
        // document. That is what makes Ctrl+A safe: a document-level handler would have to
        // prove the key was not meant for something else — every input in every properties
        // panel, every field in the mapping grid, the schedule form, the run dock's tables
        // — and every field added later would be a new chance to break typing. A handler on
        // the wrapper simply never fires when focus is elsewhere, and clicking anywhere on
        // the canvas focuses it, because the browser focuses the nearest focusable
        // ancestor of whatever was pressed.
        // -------------------------------------------------------------

        function onKeyDown(e) {
            // Belt and braces: the properties panels are outside the wrapper, but nothing
            // stops a future field being placed inside a node.
            if (e.target && e.target.closest &&
                e.target.closest("input, textarea, select, [contenteditable]")) {
                return;
            }

            if ((e.ctrlKey || e.metaKey) && (e.key === "a" || e.key === "A")) {
                // Mandatory: without it the browser also selects every word on the page.
                e.preventDefault();
                selectAll();
                return;
            }

            if (e.key === "Escape") {
                if (abortGroupDrag() || abortMarquee()) {
                    e.preventDefault();
                    return;
                }
                // An armed port first, if the canvas has one. It is the state a user is
                // most likely to want out of, and the only other way out is a click on
                // empty canvas.
                if (config.onEscape && config.onEscape()) {
                    e.preventDefault();
                    return;
                }
                if (count()) {
                    e.preventDefault();
                    clear();
                }
            }
        }

        // -------------------------------------------------------------

        let attached = false;

        function attach() {
            if (attached) return;
            attached = true;
            wrapperEl.addEventListener("pointerdown", onWrapperPointerDown);
            wrapperEl.addEventListener("keydown", onKeyDown);
        }

        function detach() {
            if (!attached) return;
            attached = false;
            abandon();
            wrapperEl.removeEventListener("pointerdown", onWrapperPointerDown);
            wrapperEl.removeEventListener("keydown", onKeyDown);
        }

        return {
            attach: attach,
            detach: detach,

            hasNode: hasNode,
            hasEdge: hasEdge,
            // What a renderer should paint, which is not the same question — see the two
            // functions' own comments.
            marksNode: marksNode,
            marksEdge: marksEdge,
            count: count,
            isMulti: function () { return count() > 1; },
            selectedNodeIds: nodeIds,
            selectedEdgeIds: edgeIds,

            selectOnly: selectOnly,
            toggleNode: toggleNode,
            toggleEdge: toggleEdge,
            selectAll: selectAll,
            clear: clear,
            prune: prune,
            repaint: changed,

            beginNodePress: beginNodePress,
            movingNodeIds: movingNodeIds,
            abandon: abandon,
            swallowedClick: swallowedClick,
        };
    }

    return { create: create };
})();
