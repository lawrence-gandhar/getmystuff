# WIDGET_RENDERING.md

How an assistant's answer becomes what a visitor sees, and why the Markdown renderer
in the embeddable widget is written the way it is.

---

# The problem it solves

A data agent's answer is a query result, and a query result is a table. The widget
used to render every reply with `escapeHtml(result.summary)`, so a model that wrote

```
| id | crm_id | technology |
|----|--------|------------|
| 1  | 200    | Python     |
```

put exactly that on screen — pipes, dashes and all. The failure was not the model's:
it had been told nothing about the medium, and prose was the only thing that rendered
correctly, so an operator asking for a nicer "layout look and feel" had nothing to
turn. No wording in the agent's system prompt could have fixed it, because the pipes
were not a formatting choice — they were the renderer showing escaped text.

`renderMarkdown` in `chatbot_service._WIDGET_SCRIPT_TEMPLATE` renders that reply as
real markup, and grounding rule 15 tells the model it may now write it.

---

# The safety rule, which is the whole design

**The text is escaped first, before a single Markdown pattern is examined.**

```js
var lines = escapeHtml(String(text == null ? "" : text)).split(/\r?\n/);
```

After that line there is no `<` or `>` left in the string. A model that emitted
`<script>` is holding `&lt;script&gt;` and will still be holding it when the function
returns. Every tag in the output was written by the code below it, from a fixed set,
and **no attribute is ever built from message text** — the only attributes emitted are
three known class names.

That ordering is what makes the result safe to assign to `innerHTML` on a page this
application does not control. The inverse — parse first and escape after, or take the
model's raw HTML and "sanitise" it — produces byte-identical output for every benign
input and is a cross-site scripting hole in the operator's own website. No allowlist
bolted on afterwards recovers from it.

`inlineMarkdown` is the sharp edge: it applies emphasis to text that is **already
escaped** and must never be handed raw input. It has one caller outside
`renderMarkdown` — the `insights` list — which escapes first for exactly this reason,
and there is a test asserting that call site specifically.

## What is deliberately not supported

**Links and images.** `[text](javascript:alert(1))` is the classic route from Markdown
to script execution, and supporting links would mean a URL-scheme check to get wrong.
Grounding rule 10 already forbids the model writing a URL at all — the interface draws
its own download button — so the syntax is left as the literal text the model wrote,
which is honest and inert. Rule 15 says so to the model as well, so it does not spend
tokens producing something that will not render.

**Raw HTML passthrough.** There is no case where a model's `<b>` becomes bold. This
falls out of escape-first rather than being a separate rule, which is the point: it
cannot be forgotten.

## Supported syntax

Tables, headings, bullet and numbered lists, `**bold**`, `*italic*` and `` `code` ``.
Tables are the reason the renderer exists.

A table needs its `|---|---|` divider row to be recognised. Requiring it is what stops
a sentence like *"use the | character to split the file"* becoming a one-cell table.

Tables render inside `.gms-chatbot-table-wrap`, which scrolls horizontally: a widget is
around 340px wide and a six-column result is not, and without the wrapper a wide table
forces the whole chat panel wider than the embedding page allows.

---

# How it is tested

Split deliberately across two files, because the interesting property cannot be read
off the source.

**`test_widget_markdown.py` executes it.** The functions are lifted out of the built
script and run under Node, and the assertion is not "no `<script>` survived" but the
stronger, input-independent one: *every tag in the output is from the allowed set and
carries no attribute but a known class*. That holds for attacks nobody thought of.
Asserting on substrings would be both weaker and wrong — the word `onerror` appearing
inside `&lt;img … onerror=… &gt;` is the renderer working, not failing.

**It skips when Node is absent**, which the app container is. That is a real gap and is
why the second file exists.

**`test_widget_script.py` asserts the structure**, and runs everywhere. It pins the
escape-before-parse ordering, the `inlineMarkdown(escapeHtml(i))` call site, and that
no anchor or `href` is ever constructed inside the renderer's own block. A change that
inverts the ordering fails there even where the behavioural tests cannot run.

---

# Both reply paths render, and only one of them used to

The widget answers a turn two different ways, and the split is invisible to the
visitor:

* **streamed** (`GET /public/chatbot/message-stream`, SSE) for a chatbot with a data
  agent attached, so a turn that runs real queries shows text as it is written rather
  than a typing dot for a minute;
* **posted** (`POST /public/chatbot/message`) for everything else — a flow answer, a
  chatbot with no agent, or a stream that could not be opened.

`renderBotMessage` handled the posted reply through `renderMarkdown` from the day the
renderer landed. The streamed one did not: its painter assigned `bubble.textContent =
answer`, so the *same answer* displayed as a rendered table when it arrived by POST
and as a wall of `|` characters when it streamed. Since streaming is exactly the path
a data-agent chatbot takes, and a data agent is what produces tables, effectively
every table answer in a published widget was shown unrendered — the original bug this
document opens with, reintroduced through a transport that did not exist yet when the
renderer was written.

Both paths now go through `renderMarkdown`, and `test_widget_script.py` pins it:
`paint()` must contain `innerHTML = renderMarkdown(answer)` and must not contain
`textContent` or `innerHTML = answer`. The second half matters as much as the first —
the obvious "fix" of assigning `answer` to `innerHTML` renders the table correctly and
is the cross-site scripting hole this whole document exists to prevent.

**The whole answer is re-rendered on every token**, not appended to. Markdown is
block-structured, so the meaning of the last line can change when the next one
arrives: `| a | b |` is a paragraph until its `|---|---|` divider is read, at which
point it is a table header. An incremental renderer would have to buffer for that, and
re-parsing a few KB per token costs less than being wrong about it.

---

# Two different failures arrive at one `error` listener

`EventSource` has no separate channel for "the server told me the turn failed" and "the
connection broke", so the widget's single `error` handler receives both. They need
opposite responses:

| what arrived | `message.data` | correct response |
|---|---|---|
| the server's `{"event": "error"}` — a misconfigured agent, a timeout, a rate limit | a JSON payload | show the sentence; the turn already ran |
| a transport failure — endpoint missing, proxy buffering the stream | *(nothing)* | fall back to `POST /public/chatbot/message` |

The presence of `data` is the only thing that distinguishes them, so that is what the
handler branches on, and a payload marks the turn `settled` before the fallback check.

Getting this wrong is not cosmetic. The handler previously read a server error as
"nothing arrived at all" and re-POSTed the whole turn, which meant **every failing turn
ran twice** — two model calls, two rows in `chatbot_messages`, two lots of tokens billed
to the owner — while the visitor saw the POST's generic fallback text and never the
actual reason. It showed up in the turn log as `error`/`success` pairs written in the
same millisecond with the same `visitor_message`, which is the signature to look for if
it ever comes back.

---

# The operator's own prompt

`data_agents.system_prompt` is composed ahead of the generated rules by
`compose_runtime_prompt`, so an operator can shape tone, which columns to lead with,
and how much prose to put around a table. What it cannot do is override the grounding
rules, which are appended last on purpose — the same reasoning as
`ai_analytics_service._GROUNDING_ADDENDUM`. Rule 8's display-row limit and rule 15's
"no links" survive any persona.

---

# The console renders the same answer differently, on purpose

`templates/deep_agents/partials/answer.htm` shows the raw text in a monospaced block
rather than rendering it. The console is the operator's diagnostic view, where seeing
exactly what the model produced is worth more than seeing it prettified — the point of
that page is that the tools-called list and the answer can be checked against each
other. Monospace is there because a proportional font made a Markdown table's columns
wander, which was the one thing `pre-wrap` could not survive.

---

# Related

* [DEEP_AGENTS.md](DEEP_AGENTS.md) — the grounding rules, including 8 (row limit),
  10 (no URLs), 13 (describe the rows you actually got) and 15 (answer format), and
  how a change to any of them reaches an agent whose prompt is already stored.
* [DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md) — the download card, which is built from
  the turn payload rather than from anything the model wrote, and why.
