# Local Crack

A self-hosted rewrite of the [crack.wrtn.ai](https://crack.wrtn.ai) story
builder and chat, running entirely on `127.0.0.1` against your own models.
Zero dependencies — Python standard library only, four static HTML pages.

```
python3 server.py     # → http://127.0.0.1:8787
```

## Why

Crack is a hosted roleplay service. This is the same workflow — build a story,
edit its prompt and keyword book, then play it — but the model is whichever one
you have a key for, the data is a folder of JSON files you own, and nothing
leaves your machine except the model call itself.

## Pages

| Page | What it does |
|---|---|
| `index.html` | Launcher. Chat or builder. |
| `chat.html` | Play. Model picker, per-chat settings drawer. |
| `builder.html` | Edit a story: profile, prompt, start settings, keyword book. |
| `works.html` | Your stories. Open, edit, delete. |
| `settings.html` | Default model, output cap, provider connections. |

## Models

The model list is never hardcoded. Providers are discovered from three places
and shown in separate groups so you always know where a model came from:

- **Subscription (CLI)** — if the `claude` binary is on PATH, your Claude Code
  subscription is used directly via `claude -p`. No API key, no extra billing.
- **Hermes auto** — every `*_API_KEY` in `~/.hermes/.env` that maps to a known
  endpoint, plus `custom_providers` from `~/.hermes/config.yaml`.
- **Added by hand** — any OpenAI-compatible base URL you enter in settings.
  The server calls `/models` before saving, so a bad key fails at entry rather
  than mid-conversation.
- **Local** — ollama on `127.0.0.1:11434`, when it is running.

Model IDs are merged from the live API response *and* Hermes's cache, so
providers that only return their newest models still expose the older ones.

Usage is surfaced where it matters: Claude subscription limits (session,
weekly, credits) render as bars under the Anthropic entry, and providers with a
balance endpoint (Moonshot, OpenRouter, DeepSeek) show their remaining credit
next to the name.

## Prompt assembly

Every turn the system prompt is built in a fixed order:

```
story prompt
play guide
[what has happened so far]     ← event log, relevance-matched
[long-term memory]             ← rolling summary of turns that scrolled off
[<name> profile]               ← the persona you are playing
[user note]                    ← rules scoped to this one chat
[<title>]                      ← keyword-book notes that fired this turn
```

`{char}` and `{user}` are substituted last, across all of it — prompt, guide,
profile, notes, your own message, and the stored history. `{{char}}` works too.

### Keyword book

A note carries a title, an info body, up to five keywords, and a scope. It is
injected only when one of its keywords appears in the last four turns —
including the model's own output, so an emoji style-switch keeps itself alive
until it scrolls out of the window. Scope pins a note to one start setting;
notes scoped elsewhere never fire.

### Images

Images are addressed by number, not by URL. A story keeps a list —

```json
"imageBase": "https://uubao.uk/BA",
"images": [{"n": 1, "label": "Kanna, blank", "url": "C02.webp"}]
```

— and the prompt receives one line, `1=Kanna, blank 2=...`, instead of a code
table and a URL-assembly rule. The model writes `{img::2}` and cannot get an
address wrong, because it never sees one.

Images are uploaded, not hosted. The builder POSTs the raw bytes to
`/api/images`; the server checks the magic number (not the extension), names
the file by its own SHA-256 so a re-upload is deduplicated, and serves it from
`/img/<hash>`. No bucket, no CDN, no configuration. Stories written against an
external host still work — an entry keeps whatever URL it was saved with, and
the legacy `imageBase` prefix is applied only when one is present.

Chats store the raw `{img::2}` token. Substitution happens at render time, so
re-pointing an image updates every past conversation. An unregistered number
renders as a visible placeholder rather than vanishing. Going the other way,
history sent to the model is rewritten to `[image: Kanna, blank]` — a bare
number tells it nothing about what it just showed.

### Event log

Every N turns (per chat, default 20) the server summarises what actually
happened — events, movement, what each character learned, promises made — and
appends it. Three modes:

- **append** — summarise only the new stretch, add it below.
- **full** — fold the existing log and the whole conversation into one pass.
- **by character** — one paragraph per character, recording what that character
  knows *and does not know*. Information asymmetry survives the summary.

By default the log is not pasted in wholesale. It is split into blocks (and, in
by-character mode, into per-character paragraphs), scored against the last four
turns by 2-gram overlap, and only the top three matching blocks are included —
ask about one character and you get that character's block, ask about lunch and
you get nothing. Switch to "always include" per chat if you'd rather pay the
tokens.

When the log passes 3,500 characters a banner appears above the composer
offering to re-summarise, because past that point it starts eating the space
the conversation needs.

### Long-term memory

Separate from the event log and optional. Keep the last N turns verbatim;
everything older is condensed into a single rolling summary and folded into the
prompt. Both the memory and the event log are editable by hand — a bad summary
follows you forever otherwise.

## Data

Everything lives in `data/`, one JSON file per item:

```
data/drafts/     stories
data/chats/      conversations (log, memory, event log, per-chat settings)
data/personas.json
data/providers.json   ← contains API keys, chmod 600
data/backups/    one zip per day, kept 14 days
```

Set `CRACK_DATA` to move the folder elsewhere. Writes go to a temp file and are
renamed into place, so a crash mid-save leaves the previous file intact.

`data/` is gitignored. Do not commit it — `providers.json` holds plaintext keys.

## Tests

```
python3 server.py --selftest
```

Covers keyword firing and its scan window, note scoping, `{char}`/`{user}`
substitution, prompt block assembly, event-log splitting and relevance
matching, provider discovery and key precedence, balance parsers, and the
save/delete/backup round-trip. Summarisation guards assert that a failed model
call leaves existing memory untouched.
