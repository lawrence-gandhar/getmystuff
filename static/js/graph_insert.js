/*
 * graph_insert.js — the "+" menu that inserts a block into an existing connector.
 *
 * Shared by all three canvases: the Flow Builder, the Graph Designer (Pipelines) and the
 * Integrations workflow canvas. What it owns is the *menu*: where it opens, how it is
 * dismissed, how it is driven from the keyboard, and what an empty catalogue looks like.
 *
 * What it deliberately does not own is **what may be inserted, or what inserting means**.
 * That is the same split `graph_selection.js` draws and for the same reason: the three
 * canvases genuinely disagree. A Flow Builder block's ports come from a local registry, a
 * Graph Designer node's come from a server vocabulary, and an Integrations step's come from
 * a spec whose ports are plain strings. A shared module that tried to know all three would
 * be a config object pretending to be a primitive. So the canvas supplies the list and does
 * the splice; this file makes the list appear next to the wire and hands back a choice.
 *
 * The menu is parented to `document.body`, not to the canvas. Both the wrapper and the
 * scrolling canvas have `overflow: auto`, so a menu inside either is clipped by whichever
 * edge it opens near — which is precisely where a connector in a long pipeline sits. Being
 * `position: fixed` on the body also means it needs no scroll arithmetic: it is placed at
 * the cursor's viewport coordinates, which is where the press already was.
 */
(function () {
    "use strict";

    /** How far from the viewport edge the menu will not go. */
    const EDGE_MARGIN_PX = 8;

    /**
     * Create an insert menu bound to one canvas.
     *
     * @param {object} config
     * @param {HTMLElement} config.wrapperEl   the scrolling canvas wrapper; scrolling it
     *                                         closes the menu, because the wire the menu
     *                                         refers to has moved out from under it
     * @param {function(string): Array<{type: string, label: string, icon: (string|undefined)}>}
     *        config.getChoices                what may be inserted into the given connector,
     *                                         already filtered by the canvas — this file
     *                                         never decides
     * @param {function(string, string): void} config.onChoose  (type, edgeId)
     * @param {string} [config.emptyMessage]   shown when `getChoices()` is empty, because a
     *                                         menu that opens blank reads as broken
     * @returns {object} the menu's public interface
     */
    function create(config) {
        const wrapperEl = config.wrapperEl;
        const getChoices = config.getChoices;
        const onChoose = config.onChoose;
        const emptyMessage = config.emptyMessage ||
            "Nothing here can be inserted into a connector.";

        let menuEl = null;
        let edgeId = null;

        function isOpen() {
            return !!menuEl;
        }

        /**
         * The items, as live DOM buttons. Read back rather than tracked, so the keyboard
         * and the pointer cannot disagree about what is on screen.
         */
        function items() {
            return menuEl ? Array.prototype.slice.call(menuEl.querySelectorAll("[data-insert-type]")) : [];
        }

        function close() {
            if (!menuEl) return;

            document.removeEventListener("pointerdown", onOutsidePointer, true);
            document.removeEventListener("keydown", onKey, true);
            window.removeEventListener("resize", close);
            if (wrapperEl) wrapperEl.removeEventListener("scroll", close);

            menuEl.remove();
            menuEl = null;
            edgeId = null;
        }

        function choose(type) {
            const forEdge = edgeId;
            close();
            // Closed before the callback runs: the splice re-renders the canvas, and the
            // connector this menu was opened from is one of the things it destroys.
            if (forEdge) onChoose(type, forEdge);
        }

        function onOutsidePointer(e) {
            if (menuEl && !menuEl.contains(e.target)) close();
        }

        function onKey(e) {
            if (!menuEl) return;

            if (e.key === "Escape") {
                e.preventDefault();
                e.stopPropagation();
                close();
                return;
            }

            const all = items();
            if (!all.length) return;

            const at = all.indexOf(document.activeElement);

            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                e.stopPropagation();
                const step = e.key === "ArrowDown" ? 1 : -1;
                // Wraps, so holding one arrow key cannot dead-end at either end.
                const next = at === -1 ? 0 : (at + step + all.length) % all.length;
                all[next].focus();
            }
        }

        /**
         * Keep the menu inside the viewport.
         *
         * Measured after it is in the document, because the height depends on how many
         * choices the canvas offered and no arithmetic here can know that in advance.
         */
        function place(clientX, clientY) {
            const box = menuEl.getBoundingClientRect();
            const maxLeft = window.innerWidth - box.width - EDGE_MARGIN_PX;
            const maxTop = window.innerHeight - box.height - EDGE_MARGIN_PX;

            // `Math.max` last, so a menu taller or wider than the viewport pins to the top
            // left rather than being pushed off it — `maxTop` goes negative in that case,
            // and the stylesheet's `max-height` makes the list itself scroll.
            menuEl.style.left = Math.max(EDGE_MARGIN_PX, Math.min(clientX, maxLeft)) + "px";
            menuEl.style.top = Math.max(EDGE_MARGIN_PX, Math.min(clientY, maxTop)) + "px";
        }

        /**
         * Open the menu for one connector, at a point in viewport coordinates.
         *
         * @param {string} forEdgeId
         * @param {number} clientX
         * @param {number} clientY
         */
        function openFor(forEdgeId, clientX, clientY) {
            close();

            edgeId = forEdgeId;
            menuEl = document.createElement("div");
            menuEl.className = "gc-insert-menu";
            menuEl.setAttribute("role", "menu");
            menuEl.setAttribute("aria-label", "Insert a block into this connector");

            // Passed the connector, so a canvas can filter per edge rather than once. The
            // Graph Designer needs it: whether an outcome node may be inserted depends on
            // what this particular connector already leads to.
            const choices = getChoices(forEdgeId) || [];

            if (!choices.length) {
                const note = document.createElement("p");
                note.className = "gc-insert-empty";
                note.textContent = emptyMessage;
                menuEl.appendChild(note);
            } else {
                choices.forEach(function (choice) {
                    const btn = document.createElement("button");
                    btn.type = "button";
                    btn.className = "gc-insert-item";
                    btn.setAttribute("role", "menuitem");
                    btn.dataset.insertType = choice.type;

                    if (choice.icon) {
                        const icon = document.createElement("i");
                        icon.className = "las " + choice.icon;
                        btn.appendChild(icon);
                        btn.appendChild(document.createTextNode(" "));
                    }
                    // textContent, never innerHTML: a Graph Designer label comes from a
                    // server vocabulary and an Integrations one from a connector spec.
                    btn.appendChild(document.createTextNode(choice.label || choice.type));

                    btn.addEventListener("click", function (e) {
                        e.preventDefault();
                        e.stopPropagation();
                        choose(choice.type);
                    });
                    menuEl.appendChild(btn);
                });
            }

            document.body.appendChild(menuEl);
            place(clientX, clientY);

            // Capture phase on `pointerdown`, so a press that lands on the canvas closes
            // the menu before the canvas's own handlers read it as a click on a wire.
            document.addEventListener("pointerdown", onOutsidePointer, true);
            document.addEventListener("keydown", onKey, true);
            window.addEventListener("resize", close);
            if (wrapperEl) wrapperEl.addEventListener("scroll", close);

            const first = items()[0];
            if (first) first.focus();
        }

        return {
            openFor: openFor,
            close: close,
            isOpen: isOpen,
        };
    }

    window.GraphInsert = { create: create };
})();
