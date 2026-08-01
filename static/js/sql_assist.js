/**
 * Ask AI — the small amount of behaviour the SQL panel needs beyond HTMX.
 *
 * Everything that matters (the cascade, generating, refining) is plain HTMX against
 * app/routes/sql_assist. This file covers two things HTMX can't:
 *
 *   1. Copying the generated query to the clipboard.
 *   2. Clearing the panel when it closes, so reopening it doesn't show the query from
 *      last time — and, more importantly, doesn't carry the previous conversation's
 *      hidden history into a new question.
 *
 * Delegated from the document rather than bound per render, because the result panel
 * is replaced on every generate; there is nothing to re-bind after a swap.
 */
(function () {
    "use strict";

    var COPY_SELECTOR = "[data-copy-target]";
    var PANEL_ID = "sqlAssistOffcanvas";
    var RESULT_ID = "sqlAssistResult";
    var PROMPT_ID = "sqlAssistPrompt";

    document.addEventListener("click", function (event) {
        var button = event.target.closest(COPY_SELECTOR);
        if (button) copy(button);
    });

    // Reset on close, not on open: the panel body is fetched fresh each open anyway,
    // and clearing here means a half-finished question is gone the moment the user
    // decides to leave rather than lingering in the DOM.
    document.addEventListener("hidden.bs.offcanvas", function (event) {
        if (event.target && event.target.id === PANEL_ID) reset();
    });

    /**
     * Copy the text of whatever the button points at, and say so on the button
     * itself — a silent copy is indistinguishable from a broken one.
     * @param {Element} button
     */
    function copy(button) {
        var source = document.querySelector(button.getAttribute("data-copy-target"));
        if (!source) return;

        write(source.textContent || "")
            .then(function () { flash(button, "Copied", "btn-success"); })
            .catch(function () { flash(button, "Press Ctrl+C", "btn-warning"); });
    }

    /**
     * Clipboard write, with a selection-based fallback.
     *
     * navigator.clipboard is unavailable on a page served over plain HTTP to
     * anything but localhost, which is exactly how this app runs in development.
     */
    function write(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text);
        }

        return new Promise(function (resolve, reject) {
            var holder = document.createElement("textarea");
            holder.value = text;
            holder.setAttribute("readonly", "readonly");
            holder.style.position = "fixed";
            holder.style.opacity = "0";
            document.body.appendChild(holder);
            holder.select();

            try {
                // Deprecated, but still the only option without a secure context.
                if (document.execCommand("copy")) resolve();
                else reject(new Error("Copy was refused"));
            } catch (error) {
                reject(error);
            } finally {
                document.body.removeChild(holder);
            }
        });
    }

    /** Swap a button's label and colour for a moment, then put it back. */
    function flash(button, message, className) {
        if (button.dataset.flashing === "1") return;
        button.dataset.flashing = "1";

        var original = button.innerHTML;
        var originalClass = button.className;

        button.textContent = message;
        button.className = originalClass.replace(/btn-outline-\w+/, className);

        window.setTimeout(function () {
            button.innerHTML = original;
            button.className = originalClass;
            delete button.dataset.flashing;
        }, 1500);
    }

    /** Drop the last result — and with it the conversation history it carried. */
    function reset() {
        var result = document.getElementById(RESULT_ID);
        if (result) result.innerHTML = "";

        var prompt = document.getElementById(PROMPT_ID);
        if (prompt) prompt.value = "";
    }
})();
