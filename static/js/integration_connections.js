/**
 * The add-a-connection form, rearranged for whichever system is chosen.
 *
 * Two shapes of connector exist and they need different questions. A generic REST
 * connection is defined by an address the user types. A vendor connection — Shopify —
 * computes its own address from the account it belongs to, deliberately, because a typed
 * base URL is how something labelled "Shopify" and carrying a Shopify token ends up
 * pointing at somebody else's host. So one asks for an API address and the other asks for
 * a shop domain, and asking for both would invite the user to fill in the one that is
 * ignored.
 *
 * Everything this needs is already on the `<option>` as data attributes, put there by the
 * server from the connector registry. No fetch, and no second copy of the connector list
 * living in JavaScript where it could drift from the one the validator reads.
 *
 * **A hidden field is also disabled.** A disabled input is not submitted, which is what
 * stops a base URL typed before switching to Shopify from arriving on a connector that
 * would refuse it — and, the other way round, stops an empty `external_account_id` being
 * posted for a connector that has no concept of one.
 *
 * If this file fails to load, the account field stays hidden and disabled, and a Shopify
 * connection is refused by the server with a sentence saying a shop domain is required.
 * That is the correct way for this to break: nothing is created wrongly.
 */
(function () {
    "use strict";

    var HIDDEN = "d-none";

    function ready(fn) {
        if (document.readyState !== "loading") {
            fn();
            return;
        }
        document.addEventListener("DOMContentLoaded", fn);
    }

    /** Show or hide one field group, keeping the input's disabled state in step. */
    function toggleGroup(group, input, shown) {
        if (!group) {
            return;
        }
        group.classList.toggle(HIDDEN, !shown);
        if (input) {
            input.disabled = !shown;
            if (!shown) {
                input.required = false;
            }
        }
    }

    function applyConnector(option, fields) {
        if (!option) {
            return;
        }

        var data = option.dataset || {};
        var wantsAccount = data.asksForAccountId === "true";

        toggleGroup(fields.baseUrlGroup, fields.baseUrl, data.asksForBaseUrl === "true");
        toggleGroup(fields.accountGroup, fields.accountId, wantsAccount);

        if (!wantsAccount || !fields.accountId) {
            return;
        }

        var label = data.accountIdLabel || "Account";
        var help = data.accountIdHelp || "";
        var pattern = data.accountIdPattern || "";

        fields.accountId.required = true;
        fields.accountId.placeholder = help;

        // The frontend half of the validation. The server checks the same pattern on the
        // way in, and the connector checks it a third time immediately before the value
        // becomes a hostname — this one exists to save a round trip, not to be trusted.
        if (pattern) {
            fields.accountId.setAttribute("pattern", pattern);
        } else {
            fields.accountId.removeAttribute("pattern");
        }

        if (fields.accountLabel) {
            fields.accountLabel.textContent = label;
        }
        if (fields.accountHelp) {
            fields.accountHelp.textContent = help
                ? "The " + label.toLowerCase() + " of the store, for example " + help + "."
                : "";
        }
    }

    ready(function () {
        var select = document.getElementById("newConnectionConnector");
        if (!select) {
            return;
        }

        var fields = {
            baseUrlGroup: document.getElementById("newConnectionBaseUrlGroup"),
            baseUrl: document.getElementById("newConnectionBaseUrl"),
            accountGroup: document.getElementById("newConnectionAccountGroup"),
            accountId: document.getElementById("newConnectionAccountId"),
            accountLabel: document.getElementById("newConnectionAccountLabel"),
            accountHelp: document.getElementById("newConnectionAccountHelp")
        };

        function refresh() {
            applyConnector(select.options[select.selectedIndex], fields);
        }

        select.addEventListener("change", refresh);

        // Once now, so the form is correct for whichever connector is selected first
        // rather than only after the user touches the dropdown.
        refresh();
    });
})();
