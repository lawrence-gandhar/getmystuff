/* Streaming the test console's answer as the agent writes it.
 *
 * The console's form carries hx-post to /deep-agents/<uuid>/ask, and that path is
 * untouched — it is the fallback, and it is what renders the tools-called footer. This
 * script takes the submit over when the browser supports EventSource, opens /ask-stream,
 * and paints the answer as it arrives.
 *
 * Why bother: an agent turn runs real queries against a real database and can take
 * a minute or more. A spinner that says nothing for that long is indistinguishable
 * from a hang, and the operator's first question is always "is it stuck?". The tool
 * events answer it — "running total_units" is a different message from silence.
 *
 * Falls back rather than degrades. No EventSource, or a stream that dies before it says
 * anything, and the same POST is issued through htmx.ajax; the operator sees the
 * blocking answer they saw before this file existed. If this file fails to load at all,
 * hx-post on the form is still there and htmx handles the submit exactly as it always
 * did — which is why the attribute stays in the markup rather than moving in here.
 *
 * Three things about EventSource drive the shape of this file, and all three were
 * observed in a real browser against this server rather than guessed at:
 *
 *   1. It reconnects by itself. A stream that ends — including one that ended perfectly,
 *      having sent `done` — makes the browser open it again, which re-runs the entire
 *      agent turn: more model calls, more queries, another download offer. The only
 *      thing that stops it is close(), so close() happens before anything that could
 *      throw, and it happens on every path out.
 *   2. Every close arrives as an `error` event with no data, success included. So a
 *      disconnect on its own says nothing about whether the turn worked; `finished`
 *      is what distinguishes the expected close from a lost connection.
 *   3. Our own server-sent `error` event lands on the same listener, but with a payload.
 *      That one carries a sentence written for the operator and is always shown.
 */
(function () {
    "use strict";

    var form = document.getElementById("deepAgentAskForm");
    var target = document.getElementById("deepAgentAnswer");

    if (!form || !target || typeof window.EventSource === "undefined") {
        // No streaming: htmx's own hx-post on the form is left in charge.
        return;
    }

    var streamUrl = form.getAttribute("data-stream-url");
    var askUrl = form.getAttribute("hx-post");
    var spinner = document.getElementById("deepAgentSpinner");

    var source = null;
    var settled = false;   // something arrived, so the stream owns the answer
    var finished = false;  // `done` arrived, so the disconnect that follows is expected

    function escapeHtml(text) {
        var div = document.createElement("div");
        div.appendChild(document.createTextNode(text));
        return div.innerHTML;
    }

    function shell(question) {
        // Deliberately the same markup as templates/deep_agents/partials/answer.htm,
        // so a streamed answer and a posted one look identical. The footer is left
        // empty until the `done` event, which is the only thing that knows what was
        // called.
        target.innerHTML =
            '<div class="card shadow-sm mb-3">' +
            '  <div class="card-header bg-white d-flex justify-content-between align-items-center">' +
            '    <span class="fw-semibold"><i class="las la-comment text-primary"></i> Answer</span>' +
            '    <span class="badge bg-light text-dark border" id="deepAgentModel">streaming&hellip;</span>' +
            "  </div>" +
            '  <div class="card-body">' +
            '    <p class="text-muted small mb-2"><em>' + escapeHtml(question) + "</em></p>" +
            '    <div class="mb-0" id="deepAgentText" style="white-space: pre-wrap;"></div>' +
            '    <div class="text-muted small mt-2" id="deepAgentActivity"></div>' +
            "  </div>" +
            '  <div class="card-footer bg-white small" id="deepAgentFooter">' +
            '    <span class="text-muted"><span class="spinner-border spinner-border-sm"></span> Working&hellip;</span>' +
            "  </div>" +
            "</div>";
    }

    function failure(message) {
        target.innerHTML =
            '<div class="alert alert-danger mb-0">' +
            '<i class="las la-exclamation-triangle"></i> ' + escapeHtml(message) +
            "</div>";
    }

    function renderFooter(toolsCalled) {
        var footer = document.getElementById("deepAgentFooter");
        if (!footer) {
            return;
        }

        if (!toolsCalled || !toolsCalled.length) {
            // The same warning the server-rendered fragment shows, and for the same
            // reason: an answer with figures and no tool call is the failure this
            // whole feature exists to prevent, so it is never hidden.
            footer.innerHTML =
                '<span class="text-warning fw-semibold">' +
                '<i class="las la-exclamation-circle"></i> No tool was called.</span> ' +
                '<span class="text-muted">Nothing was read from your data, so this ' +
                "answer contains no figures from it.</span>";
            return;
        }

        var codes = toolsCalled.map(function (name) {
            return '<code class="ms-1">' + escapeHtml(name) + "</code>";
        }).join("");

        footer.innerHTML =
            '<span class="text-success fw-semibold">' +
            '<i class="las la-check-circle"></i> Read from ' + toolsCalled.length +
            " tool call" + (toolsCalled.length === 1 ? "" : "s") + ":</span>" + codes;
    }

    function close() {
        if (source) {
            source.close();
            source = null;
        }
        if (spinner) {
            spinner.classList.remove("htmx-request");
        }
    }

    function fallbackToPost(question) {
        // The blocking path, issued by hand because the submit that would have
        // triggered it was stopped below. Same verb, URL, target and swap the form
        // declares, so the answer renders through the same partial.
        if (!askUrl || !window.htmx) {
            failure(
                "The connection to the agent was lost. Please try again."
            );
            return;
        }

        window.htmx.ajax("POST", askUrl, {
            source: form,
            target: "#deepAgentAnswer",
            swap: "innerHTML",
            values: { question: question },
        });
    }

    function ask(question) {
        close();
        settled = false;
        finished = false;
        shell(question);

        if (spinner) {
            spinner.classList.add("htmx-request");
        }

        source = new EventSource(
            streamUrl + "?question=" + encodeURIComponent(question)
        );

        var answer = "";

        source.addEventListener("tool", function (message) {
            settled = true;

            var activity = document.getElementById("deepAgentActivity");
            var payload = JSON.parse(message.data);

            if (activity) {
                activity.innerHTML =
                    '<span class="spinner-border spinner-border-sm"></span> Running ' +
                    "<code>" + escapeHtml(payload.name || "") + "</code>&hellip;";
            }
        });

        source.addEventListener("token", function (message) {
            settled = true;

            var body = document.getElementById("deepAgentText");
            var payload = JSON.parse(message.data);

            answer += payload.text || "";

            if (body) {
                body.textContent = answer;
            }
        });

        source.addEventListener("done", function (message) {
            // Closed first, and before anything that can throw. A `done` that painted
            // badly but left the stream open would be re-run in full by the browser's
            // own reconnect, and the operator would be billed for it twice.
            settled = true;
            finished = true;
            close();

            var payload = JSON.parse(message.data);
            var body = document.getElementById("deepAgentText");
            var badge = document.getElementById("deepAgentModel");
            var activity = document.getElementById("deepAgentActivity");

            // The final answer wins over the accumulated tokens. They are normally
            // identical; when a provider streams nothing (some tool-calling turns
            // emit no text chunks at all) this is the only place the answer exists.
            if (body && payload.answer) {
                body.textContent = payload.answer;
            }
            if (badge) {
                badge.textContent = payload.model || "";
            }
            if (activity) {
                activity.innerHTML = "";
            }

            renderFooter(payload.tools_called);
        });

        source.addEventListener("error", function (message) {
            close();

            if (finished) {
                // The stream ended because the turn ended. The browser reports that as
                // an error; it is not one, and the answer is already on screen.
                return;
            }

            if (message && message.data) {
                // Our own error event: a refusal the service phrased for the operator.
                var detail = "The agent could not answer. Please try again.";

                try {
                    detail = JSON.parse(message.data).message || detail;
                } catch (ignored) {
                    // Keep the generic sentence.
                }

                failure(detail);
                return;
            }

            if (!settled) {
                // Nothing arrived at all — no route on this server, or something
                // between us and it buffering the stream. Fall back rather than fail:
                // the POST is the path that has always worked.
                fallbackToPost(question);
                return;
            }

            failure("The connection to the agent was lost. Please try again.");
        });
    }

    // Capture phase, on the document, so this runs before htmx's own listener on the
    // form and can stop the event reaching it. Removing hx-post instead would not work:
    // htmx captures the verb and path in a closure when it processes the node, so the
    // attribute is only read once, at page load. Without this the form both posts and
    // streams — two complete agent turns per question, two sets of model calls, two
    // download offers, and whichever finished last overwriting the other's answer.
    document.addEventListener("submit", function (event) {
        if (event.target !== form) {
            return;
        }

        var box = form.querySelector('[name="question"]');
        var question = box ? box.value.trim() : "";

        if (!question) {
            return;  // The textarea's own `required` handles it.
        }

        event.preventDefault();
        event.stopPropagation();

        ask(question);
    }, true);
}());
