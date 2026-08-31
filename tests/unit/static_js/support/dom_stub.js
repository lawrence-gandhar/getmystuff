/*
 * A DOM small enough to fit in one file, and no smaller: exactly what
 * graph_canvas.js, graph_edges.js and graph_insert.js touch, and nothing a real
 * browser also provides. Prepended (via subprocess) to the real source files, so the
 * three modules run unmodified under `node` instead of a shimmed copy of themselves.
 */
"use strict";

function makeEventTarget() {
    const listeners = {};
    return {
        addEventListener(type, fn) {
            (listeners[type] = listeners[type] || []).push(fn);
        },
        removeEventListener(type, fn) {
            const list = listeners[type];
            if (!list) return;
            const at = list.indexOf(fn);
            if (at !== -1) list.splice(at, 1);
        },
        dispatch(type, evt) {
            (listeners[type] || []).slice().forEach(function (fn) { fn(evt); });
        },
    };
}

class FakeElement {
    constructor(tagName) {
        this.tagName = tagName;
        this.id = "";
        this.className = "";
        this.children = [];
        this.parentNode = null;
        this.attributes = {};
        this.dataset = {};
        this.style = {};
        this.isConnected = false;
        this._rect = { left: 0, top: 0, width: 0, height: 0 };
        Object.assign(this, makeEventTarget());
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "id") this.id = String(value);
    }

    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name)
            ? this.attributes[name] : null;
    }

    appendChild(child) {
        child.parentNode = this;
        child.isConnected = this.isConnected;
        this.children.push(child);
        return child;
    }

    remove() {
        this.isConnected = false;
        if (!this.parentNode) return;
        const at = this.parentNode.children.indexOf(this);
        if (at !== -1) this.parentNode.children.splice(at, 1);
        this.parentNode = null;
    }

    contains(node) {
        if (node === this) return true;
        return this.children.some(function (child) {
            return child instanceof FakeElement && child.contains(node);
        });
    }

    getBoundingClientRect() {
        return this._rect;
    }

    // Only the two forms this codebase's selectors ever use: ".class" and
    // "[data-x]" / "[data-x=\"y\"]" — not a CSS engine, a lookup table for four
    // fixed patterns.
    _matches(selector) {
        if (selector[0] === ".") {
            const cls = selector.slice(1);
            return (" " + this.className + " ").indexOf(" " + cls + " ") !== -1;
        }
        const attr = selector.match(/^\[([\w-]+)(="([^"]*)")?\]$/);
        if (!attr) return false;
        const name = attr[1];
        const wanted = attr[3];
        if (name.indexOf("data-") === 0) {
            const key = name.slice(5).replace(/-([a-z])/g, function (_, c) { return c.toUpperCase(); });
            const actual = this.dataset[key];
            return wanted === undefined ? actual !== undefined : actual === wanted;
        }
        const actual = this.getAttribute(name);
        return wanted === undefined ? actual !== null : actual === wanted;
    }

    _all() {
        let found = [];
        this.children.forEach(function (child) {
            found.push(child);
            // A text node (plain `{nodeType: 3, textContent}`) has no children of its
            // own to walk into.
            if (child instanceof FakeElement) found = found.concat(child._all());
        });
        return found;
    }

    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }

    querySelectorAll(selector) {
        return this._all()
            .filter(function (el) { return el instanceof FakeElement; })
            .filter(function (el) { return el._matches(selector); });
    }

    focus() {
        if (this._doc) this._doc.activeElement = this;
    }
}

function createDocument() {
    const registry = new Map();
    const doc = Object.assign(makeEventTarget(), {
        activeElement: null,
        body: new FakeElement("body"),
        createElement(tag) {
            const el = new FakeElement(tag);
            el._doc = doc;
            return el;
        },
        createElementNS(ns, tag) { return doc.createElement(tag); },
        createTextNode(text) { return { nodeType: 3, textContent: String(text) }; },
        getElementById(id) { return registry.get(id) || null; },
        // Test-only: the real DOM derives this from insertion, this stub is told.
        register(id, el) {
            el.setAttribute("id", id);
            registry.set(id, el);
        },
    });
    doc.body.isConnected = true;
    doc.body._doc = doc;
    return doc;
}

/**
 * One connector's DOM: the group `getElementById` resolves by id, the path resolved
 * the same way, and inside the group exactly the classed/attributed children
 * `resolveEdgeChrome` looks for — real markup shape, not a mock of the lookup.
 */
function buildEdgeChromeDom(doc, prefix, edgeId, opts) {
    opts = opts || {};
    const group = new FakeElement("g");
    group.isConnected = opts.connected !== false;
    doc.register("edge-group-" + edgeId, group);

    const path = new FakeElement("path");
    doc.register("edge-" + edgeId, path);

    function child(tag, cls) {
        const el = new FakeElement(tag);
        el.className = cls;
        group.appendChild(el);
        return el;
    }

    const hit = child("path", prefix + "-edge-hit");
    const del = child("g", prefix + "-edge-delete-btn");
    const ins = child("g", prefix + "-edge-insert-btn");
    const sourceHandle = child("circle", prefix + "-edge-handle-source");
    const targetHandle = child("circle", prefix + "-edge-handle-target");

    const waypointHandles = [];
    for (let i = 0; i < (opts.waypoints || 0); i++) {
        const wp = child("circle", prefix + "-edge-waypoint");
        wp.setAttribute("data-waypoint", String(i));
        waypointHandles.push(wp);
    }

    return { group, path, hit, del, ins, sourceHandle, targetHandle, waypointHandles };
}

/** A node box at a given rect. `nodeElementId` callers register it by "node-<id>". */
function buildNodeDom(doc, id, rect) {
    const el = new FakeElement("div");
    el._rect = rect;
    doc.register("node-" + id, el);
    return el;
}

/** requestAnimationFrame queue a test steps by hand, one tick at a time. */
function createFrameQueue() {
    let queue = [];
    return {
        requestAnimationFrame(cb) {
            queue.push(cb);
            return queue.length;
        },
        cancelAnimationFrame(id) {
            queue[id - 1] = null;
        },
        flush() {
            const jobs = queue;
            queue = [];
            jobs.forEach(function (cb) { if (cb) cb(); });
        },
    };
}

function noEvent() {
    return { preventDefault: function () {}, stopPropagation: function () {} };
}
