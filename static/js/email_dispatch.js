/**
 * Email Dispatch pages — the template form's variable-row editor, and the modal
 * close hook every mutation response drives.
 *
 * The declared variables are edited as rows and submitted as a single hidden JSON
 * field, so the server has exactly one place to parse and validate their shape
 * instead of reassembling parallel form arrays. Straight from
 * static/js/chatbot_ai_settings.js, which does the same for prompt variables — an
 * operator who has used one has used both.
 *
 * Row markup is built with createElement/textContent rather than innerHTML. A saved
 * variable name or default is user data, and re-parsing it as markup is how a
 * template someone else shares becomes stored XSS on this page.
 *
 * Everything is deferred to DOMContentLoaded: the layout pulls htmx and Bootstrap
 * in *after* the content block this file is loaded from, so neither exists at parse
 * time.
 */
(function () {
    "use strict";

    /** Must match VARIABLE_NAME_PATTERN in app/models/email_dispatch/models.py. The
     *  server is the authority; this is here so a mistake is visible before a save. */
    var NAME_PATTERN = /^[A-Z][A-Z0-9_]{0,49}$/;

    /** Must match MAX_TEMPLATE_VARIABLES. */
    var MAX_VARIABLES = 30;

    /**
     * @param {string} id
     * @param {*} fallback
     * @returns {*}
     */
    function readJsonScript(id, fallback) {
        var el = document.getElementById(id);
        if (!el) return fallback;
        try {
            return JSON.parse(el.textContent);
        } catch (e) {
            return fallback;
        }
    }

    // -----------------------------------------------------------------------
    // The variable-row editor
    // -----------------------------------------------------------------------

    /**
     * Build one editable variable row.
     *
     * @param {object} variable
     * @param {function} onChange
     * @returns {HTMLElement}
     */
    function variableRow(variable, onChange) {
        var row = document.createElement("div");
        row.className = "row g-2 align-items-center email-variable-row";

        // --- name -----------------------------------------------------------
        var nameCol = document.createElement("div");
        nameCol.className = "col-md-3";
        var name = document.createElement("input");
        name.type = "text";
        name.className = "form-control form-control-sm font-monospace";
        name.placeholder = "CUSTOMER_NAME";
        name.value = variable.name || "";
        name.setAttribute("aria-label", "Variable name");
        name.addEventListener("input", function () {
            // Upper-cased as it is typed, because the server stores names upper-case and
            // matches placeholders case-insensitively. Doing it here means what the
            // operator sees is what will be saved, rather than a silent change on submit.
            var caret = name.selectionStart;
            name.value = name.value.toUpperCase();
            name.setSelectionRange(caret, caret);
            name.classList.toggle(
                "is-invalid",
                name.value !== "" && !NAME_PATTERN.test(name.value)
            );
            onChange();
        });
        nameCol.appendChild(name);

        // --- label ----------------------------------------------------------
        var labelCol = document.createElement("div");
        labelCol.className = "col-md-3";
        var label = document.createElement("input");
        label.type = "text";
        label.className = "form-control form-control-sm";
        label.placeholder = "Customer name";
        label.value = variable.label || "";
        label.setAttribute("aria-label", "Description shown when binding");
        label.addEventListener("input", onChange);
        labelCol.appendChild(label);

        // --- default --------------------------------------------------------
        var defaultCol = document.createElement("div");
        defaultCol.className = "col-md-3";
        var fallback = document.createElement("input");
        fallback.type = "text";
        fallback.className = "form-control form-control-sm";
        fallback.placeholder = "Default (optional)";
        fallback.value = variable.default || "";
        fallback.setAttribute("aria-label", "Default value");
        fallback.addEventListener("input", onChange);
        defaultCol.appendChild(fallback);

        // --- required -------------------------------------------------------
        var requiredCol = document.createElement("div");
        requiredCol.className = "col-md-2";
        var check = document.createElement("div");
        check.className = "form-check mb-0";
        var required = document.createElement("input");
        required.type = "checkbox";
        required.className = "form-check-input";
        required.checked = !!variable.required;
        required.addEventListener("change", onChange);
        var requiredLabel = document.createElement("label");
        requiredLabel.className = "form-check-label small";
        requiredLabel.textContent = "Required";
        check.appendChild(required);
        check.appendChild(requiredLabel);
        requiredCol.appendChild(check);

        // --- remove ---------------------------------------------------------
        var removeCol = document.createElement("div");
        removeCol.className = "col-md-1 text-end";
        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn-sm btn-outline-danger";
        remove.title = "Remove this variable";
        var icon = document.createElement("i");
        icon.className = "las la-times";
        remove.appendChild(icon);
        remove.addEventListener("click", function () {
            row.remove();
            onChange();
        });
        removeCol.appendChild(remove);

        row.appendChild(nameCol);
        row.appendChild(labelCol);
        row.appendChild(defaultCol);
        row.appendChild(requiredCol);
        row.appendChild(removeCol);

        // Hung on the element so serialise() can read the row back without re-querying
        // the DOM by position — which would break the moment the layout changes.
        row._fields = {
            name: name,
            label: label,
            default: fallback,
            required: required,
        };
        return row;
    }

    /**
     * Wire the editor inside one template form.
     *
     * Idempotent per form: htmx swaps a fresh form into the modal on every open, and
     * `data-variables-ready` stops a second binding from doubling every row.
     *
     * @param {HTMLElement} form
     */
    function initVariableEditor(form) {
        var container = form.querySelector("#templateVariableRows");
        var hidden = form.querySelector("#templateVariablesJson");
        var addButton = form.querySelector("[data-add-variable]");
        if (!container || !hidden || form.dataset.variablesReady === "true") return;
        form.dataset.variablesReady = "true";

        function serialise() {
            var rows = container.querySelectorAll(".email-variable-row");
            var out = [];
            for (var i = 0; i < rows.length; i += 1) {
                var fields = rows[i]._fields;
                if (!fields) continue;
                var name = (fields.name.value || "").trim().toUpperCase();
                // A blank row is dropped rather than submitted as an error. Somebody who
                // clicked Add and changed their mind should not have to find the X button.
                if (!name) continue;
                out.push({
                    name: name,
                    label: (fields.label.value || "").trim(),
                    required: !!fields.required.checked,
                    default: fields.default.value || "",
                });
            }
            hidden.value = JSON.stringify(out);
            if (addButton) {
                addButton.disabled = out.length >= MAX_VARIABLES;
            }
        }

        function addRow(variable) {
            container.appendChild(variableRow(variable || {}, serialise));
            serialise();
        }

        var existing = readJsonScript("templateVariablesData", []);
        for (var i = 0; i < existing.length; i += 1) {
            addRow(existing[i]);
        }
        serialise();

        if (addButton) {
            addButton.addEventListener("click", function () {
                addRow({});
            });
        }

        // Serialised again on submit as a belt-and-braces measure. Every input already
        // writes the hidden field on change, but a value pasted and submitted with Enter
        // in the same tick has been seen to beat the input event.
        form.addEventListener("submit", serialise);
    }

    // -----------------------------------------------------------------------
    // The trigger form: recipients, and one binding row per template variable
    // -----------------------------------------------------------------------

    /** The sources a *trigger* can offer. Must match trigger_service.TRIGGER_BINDING_SOURCES
     *  — a trigger has no chat session, no upstream node and no record in hand, so offering
     *  those would build a form the server refuses on save. */
    var TRIGGER_SOURCES = [
        { value: "event", label: "A field on the incoming payload" },
        { value: "literal", label: "A fixed value" },
    ];

    /**
     * One binding row for one declared template variable.
     *
     * @param {object} variable  the template's declaration
     * @param {object} binding   what is currently bound, if anything
     * @param {function} onChange
     * @returns {HTMLElement}
     */
    function bindingRow(variable, binding, onChange) {
        var row = document.createElement("div");
        row.className = "row g-2 align-items-center email-binding-row";
        row.dataset.variable = variable.name;

        var nameCol = document.createElement("div");
        nameCol.className = "col-md-3";
        var code = document.createElement("code");
        code.className = "small";
        // textContent, not innerHTML: the name came from a saved template.
        code.textContent = "{{" + variable.name + "}}";
        nameCol.appendChild(code);
        if (variable.required) {
            var star = document.createElement("span");
            star.className = "text-danger";
            star.textContent = " *";
            star.title = "Required by this template";
            nameCol.appendChild(star);
        }
        if (variable.label) {
            var hint = document.createElement("div");
            hint.className = "text-muted small";
            hint.textContent = variable.label;
            nameCol.appendChild(hint);
        }

        var sourceCol = document.createElement("div");
        sourceCol.className = "col-md-4";
        var source = document.createElement("select");
        source.className = "form-select form-select-sm";
        source.setAttribute("aria-label", "Where " + variable.name + " comes from");
        var blank = document.createElement("option");
        blank.value = "";
        blank.textContent = variable.required
            ? "Choose a source…"
            : "Leave unset (use the default)";
        source.appendChild(blank);
        for (var i = 0; i < TRIGGER_SOURCES.length; i += 1) {
            var option = document.createElement("option");
            option.value = TRIGGER_SOURCES[i].value;
            option.textContent = TRIGGER_SOURCES[i].label;
            if (binding && binding.source === TRIGGER_SOURCES[i].value) {
                option.selected = true;
            }
            source.appendChild(option);
        }
        sourceCol.appendChild(source);

        var valueCol = document.createElement("div");
        valueCol.className = "col-md-5";
        var value = document.createElement("input");
        value.type = "text";
        value.className = "form-control form-control-sm";
        value.value = binding
            ? (binding.source === "literal" ? binding.value || "" : binding.path || "")
            : "";
        valueCol.appendChild(value);
        var valueHint = document.createElement("div");
        valueHint.className = "form-text";
        valueCol.appendChild(valueHint);

        /** The second field means different things per source, so its placeholder and hint
         *  follow the choice rather than being one vague label covering both. */
        function syncValueField() {
            var chosen = source.value;
            value.disabled = chosen === "";
            if (chosen === "literal") {
                value.placeholder = "The text to use";
                valueHint.textContent = "Used exactly as typed, every time.";
            } else if (chosen === "event") {
                value.placeholder = "run.name";
                valueHint.textContent =
                    "A field path into the payload. Dots go into nested objects.";
            } else {
                value.placeholder = "";
                valueHint.textContent = "";
            }
        }

        source.addEventListener("change", function () {
            syncValueField();
            onChange();
        });
        value.addEventListener("input", onChange);
        syncValueField();

        row.appendChild(nameCol);
        row.appendChild(sourceCol);
        row.appendChild(valueCol);
        row._fields = { source: source, value: value, name: variable.name };
        return row;
    }

    /**
     * Wire the trigger form: the kind switch, the recipient boxes, and the binding rows.
     *
     * @param {HTMLElement} form
     */
    function initTriggerForm(form) {
        if (form.dataset.triggerReady === "true") return;
        form.dataset.triggerReady = "true";

        var kindSelect = form.querySelector("[data-trigger-kind]");
        var templateSelect = form.querySelector("[data-trigger-template]");
        var bindingRows = form.querySelector("#triggerBindingRows");
        var bindingEmpty = form.querySelector("#triggerBindingEmpty");
        var bindingsHidden = form.querySelector("#triggerBindingsJson");
        var recipientsHidden = form.querySelector("#triggerRecipientsJson");

        // --- kind ----------------------------------------------------------
        function syncKind() {
            // On an edit there is no kind select — the field is read-only — so the panels
            // are already set correctly by the template and there is nothing to do.
            if (!kindSelect) return;
            var chosen = kindSelect.value;
            var panels = form.querySelectorAll("[data-kind-panel]");
            for (var i = 0; i < panels.length; i += 1) {
                panels[i].hidden = panels[i].dataset.kindPanel !== chosen;
            }
        }
        if (kindSelect) {
            kindSelect.addEventListener("change", syncKind);
            syncKind();
        }

        // --- recipients ----------------------------------------------------
        var recipientInputs = form.querySelectorAll("[data-recipients]");

        function serialiseRecipients() {
            if (!recipientsHidden) return;
            var out = { to: [], cc: [], bcc: [] };
            for (var i = 0; i < recipientInputs.length; i += 1) {
                var input = recipientInputs[i];
                var key = input.dataset.recipients;
                var parts = (input.value || "").split(",");
                for (var j = 0; j < parts.length; j += 1) {
                    var entry = parts[j].trim();
                    if (entry) out[key].push(entry);
                }
            }
            recipientsHidden.value = JSON.stringify(out);
        }

        var savedRecipients = readJsonScript("triggerRecipientsData", {});
        for (var r = 0; r < recipientInputs.length; r += 1) {
            var key = recipientInputs[r].dataset.recipients;
            var saved = savedRecipients[key] || [];
            recipientInputs[r].value = saved.join(", ");
            recipientInputs[r].addEventListener("input", serialiseRecipients);
        }
        serialiseRecipients();

        // --- bindings ------------------------------------------------------
        var savedBindings = readJsonScript("triggerBindingsData", {});

        function serialiseBindings() {
            if (!bindingsHidden) return;
            var rows = bindingRows ? bindingRows.querySelectorAll(".email-binding-row") : [];
            var out = {};
            for (var i = 0; i < rows.length; i += 1) {
                var fields = rows[i]._fields;
                if (!fields || !fields.source.value) continue;
                var entry = { source: fields.source.value };
                if (entry.source === "literal") {
                    entry.value = fields.value.value;
                } else {
                    entry.path = (fields.value.value || "").trim();
                }
                out[fields.name] = entry;
            }
            bindingsHidden.value = JSON.stringify(out);
        }

        /** Rebuild the rows for whichever template is chosen. Existing bindings are kept
         *  where the new template declares the same variable, so switching template by
         *  accident and switching back does not silently discard the operator's work. */
        function rebuildBindings() {
            if (!bindingRows || !templateSelect) return;
            bindingRows.textContent = "";

            var option = templateSelect.options[templateSelect.selectedIndex];
            var declared = [];
            if (option && option.dataset.variables) {
                try {
                    declared = JSON.parse(option.dataset.variables) || [];
                } catch (e) {
                    declared = [];
                }
            }

            if (bindingEmpty) {
                bindingEmpty.hidden = declared.length > 0;
                if (!templateSelect.value) {
                    bindingEmpty.textContent = "Choose a template to see what it needs.";
                } else if (!declared.length) {
                    bindingEmpty.textContent =
                        "This template declares no variables, so there is nothing to fill in.";
                }
            }

            for (var i = 0; i < declared.length; i += 1) {
                var variable = declared[i];
                if (!variable || !variable.name) continue;
                bindingRows.appendChild(
                    bindingRow(variable, savedBindings[variable.name], serialiseBindings)
                );
            }
            serialiseBindings();
        }

        if (templateSelect) {
            templateSelect.addEventListener("change", rebuildBindings);
            rebuildBindings();
        }

        // Belt and braces on submit, for a value pasted and submitted in the same tick.
        form.addEventListener("submit", function () {
            serialiseRecipients();
            serialiseBindings();
        });
    }

    // -----------------------------------------------------------------------
    // Wiring
    // -----------------------------------------------------------------------

    document.addEventListener("DOMContentLoaded", function () {
        /**
         * Close whichever modal is open once a mutation reports success.
         *
         * Keyed on the `data-success="true"` marker every *_rows.htm partial returns, so
         * one response drives both the table refresh and the dialog — and a failed save
         * leaves the dialog open with the operator's work still in it.
         */
        document.body.addEventListener("htmx:afterSwap", function (event) {
            var marker = event.target.querySelector
                ? event.target.querySelector('[data-success="true"]')
                : null;
            if (!marker) return;

            var open = document.querySelector(".modal.show");
            if (!open || !window.bootstrap) return;
            var instance = window.bootstrap.Modal.getInstance(open);
            if (instance) instance.hide();
        });

        /**
         * Bind whichever editors are present, on the page and on every fragment htmx
         * swaps in afterwards.
         *
         * Both forms arrive by hx-get into a modal, so the initial-page pass is only for
         * completeness — but it costs nothing and means the editors are not silently
         * dependent on the modal being the only way in.
         */
        function bindEditors(root) {
            if (!root || !root.querySelectorAll) return;

            var templateForms = root.matches && root.matches("[data-email-template-form]")
                ? [root]
                : root.querySelectorAll("[data-email-template-form]");
            for (var i = 0; i < templateForms.length; i += 1) {
                initVariableEditor(templateForms[i]);
            }

            var triggerForms = root.matches && root.matches("[data-email-trigger-form]")
                ? [root]
                : root.querySelectorAll("[data-email-trigger-form]");
            for (var j = 0; j < triggerForms.length; j += 1) {
                initTriggerForm(triggerForms[j]);
            }
        }

        bindEditors(document);
        document.body.addEventListener("htmx:afterSwap", function (event) {
            bindEditors(event.target);
        });
    });
})();
