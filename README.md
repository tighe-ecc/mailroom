# Mailroom

Personal mailroom: a unified order + package tracker with a FastAPI + Tailwind/DaisyUI dashboard, LLM email parsing, EasyPost-backed carrier polling, and macOS notifications.

Drop an order-confirmation email into the inbox folder, walk away, and get a desktop notification the moment the package is delivered.

## What it does

- **Dashboard** at `http://localhost:47821` — Tailwind + DaisyUI UI served by FastAPI with HTMX for live updates. Unified view that covers the full lifecycle — *ordered → confirmed → in fulfillment → pre-transit → in transit → out for delivery → delivered* — with status badges, ETAs, and latest carrier event. Click any row to see the full detail card; edit fields inline.
- **Email ingestion** via a watched folder at `~/Mailroom/`. Drag an order-confirmation or shipping-confirmation email out of Outlook into that folder; a filesystem watcher picks it up within a second or two, extracts vendor / PO / order # / dates / tracking number via OpenAI `gpt-4o-mini`, and creates or updates the right row automatically.
- **Background poller** runs every 30 minutes via `launchd` (optional). Hits EasyPost for every active package with a tracking number, writes updates to SQLite, fires a macOS notification on shipped / out-for-delivery / delivered transitions.
- **CLI** for scripting: `mailroom add-order ...`, `add-tracking ...`, `list`, `rm`, `poll`, `process-inbox`.

Data lives in `~/Mailroom/.mailroom/db.sqlite`. Nothing leaves your machine except (a) tracking numbers sent to EasyPost — already printed on public shipping labels, and (b) email bodies sent to OpenAI for parsing.

## Setup (one-time, ~10 minutes)

### 1. Install into a local virtualenv

```bash
cd /path/to/mailroom
python3 -m venv .venv
.venv/bin/pip install -e .
```

### 2. Get API keys

1. **EasyPost** (carrier tracking) — sign up at <https://www.easypost.com/signup>. Test keys (`EZTK...`) are free; production keys (`EZAK...`) bill pennies per tracker.
2. **OpenAI** (email parsing) — get a key at <https://platform.openai.com/api-keys>. Uses `gpt-4o-mini`, typically <$0.001 per email.
3. Copy `.env.example` to `.env` and fill in both:

```bash
cp .env.example .env
# edit .env:
# EASYPOST_API_KEY=EZTK... (or EZAK...)
# OPENAI_API_KEY=sk-...
```

### 3. Smoke test

```bash
# Add an EasyPost test tracking number — it deterministically simulates delivery
.venv/bin/mailroom add-tracking EZ4000000004 --desc "test package"

# See it in the list
.venv/bin/mailroom list --all
```

### 4. Install the background daemons

Two `launchd` agents — one that runs the GUI server + inbox watcher always (so drops into `~/Mailroom/` are processed within seconds), one that re-polls carrier status every 30 minutes.

The plist files contain the placeholder `/path/to/mailroom`. Replace it with the absolute path to your repo (launchd does not expand `~` or shell variables) before copying them into place — e.g. `sed -i '' "s|/path/to/mailroom|$PWD|g" scripts/com.tighe.mailroom.*.plist` from the repo root.

```bash
cp scripts/com.tighe.mailroom.gui.plist  ~/Library/LaunchAgents/
cp scripts/com.tighe.mailroom.poll.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tighe.mailroom.gui.plist
launchctl load ~/Library/LaunchAgents/com.tighe.mailroom.poll.plist
launchctl list | grep mailroom   # both should appear
```

Logs: `/tmp/mailroom-gui.log` (GUI + watcher) and `/tmp/mailroom.log` (poller).

To stop them: `launchctl unload ~/Library/LaunchAgents/com.tighe.mailroom.{gui,poll}.plist`.

### 5. Pin `~/Mailroom/` to Finder's sidebar

This is the one-drag workflow for getting emails into Mailroom (see *Daily use* below). The folder shows up as **Mailroom** in the sidebar; dropping a `.eml` directly onto that entry feeds the pipeline.

1. Open Finder → navigate to `~/Mailroom/`.
2. Drag the folder's name from the title bar into the Favorites section of Finder's sidebar.

When idle the folder looks empty — the `.mailroom/` subfolder that holds the DB and archived `.eml` files is hidden by the macOS dot-prefix convention. You can reveal it with ⌘+⇧+. in Finder if you need to poke around.

### 6. Open the dashboard

Three equivalent ways, pick whichever fits your muscle memory:

- **Bookmark** <http://localhost:47821> in your browser. Fastest.
- Double-click [scripts/open-mailroom.command](scripts/open-mailroom.command) in Finder. Opens the tab.
- Drag `scripts/open-mailroom.command` into your Dock for a one-click launcher.

The dashboard is always running in the background via the GUI daemon; you're just opening a browser tab against it.

## Daily use

**Primary path — drag the email:**

1. Place an order. When the order-confirmation email arrives in Outlook, **select it in the message list and drag it onto the `inbox` entry in Finder's sidebar**. Outlook saves the message as an `.eml` file in `~/Mailroom/`.
2. The watcher processes it within ~2 seconds. A new row shows up in the dashboard with status *Ordered* or *Confirmed*, populated by the LLM from vendor, PO, order #, and dates it extracted.
3. When the shipping-confirmation email arrives, drag that too. The matcher recognizes the order number (or PO) and **updates the existing row** — no duplicate — adding the tracking number. From there, EasyPost polling takes over automatically.
4. Get macOS notifications on *Shipped → Out for delivery → Delivered*.

Optional: after dragging, also use Outlook's **Shift+Cmd+M** to move the email to an "Orders" folder to keep your Outlook inbox tidy. The two gestures do different jobs — the drag feeds Mailroom, the Shift+Cmd+M tidies Outlook.

**Manual path — type it in:**

- Open the dashboard, click **Add order**, fill in whatever fields you have (tracking number optional). Useful for one-offs or when you don't have an email handy.

**Drag-to-browser path (for `.eml` files already on disk):**

- If you have the dashboard tab in front of you, drag an `.eml` from any Finder location (Desktop, Downloads, a Finder window) onto the browser. The drop overlay appears on dragenter; release anywhere on the page to upload.
- **This does *not* work directly from Outlook** because Outlook uses macOS "promised files" — it only materializes the `.eml` bytes if the drop target is a filesystem location. Browsers don't accept promised files. Drag from Outlook to the Finder sidebar entry instead.

Processed `.eml` files are moved to `~/Mailroom/.mailroom/processed/`; emails the LLM can't classify go to `~/Mailroom/.mailroom/unrecognized/`; any that error during parsing go to `~/Mailroom/.mailroom/failed/` with a sidecar `.error.txt`. The `.mailroom/` subfolder is hidden in Finder (dot-prefix), so `~/Mailroom/` itself stays clean.

### A note on hotkey-based export

An earlier iteration included [scripts/send-to-mailroom.applescript](scripts/send-to-mailroom.applescript) — an AppleScript intended to be bound to ⌃⌥⌘M to export the currently-selected Outlook message to `~/Mailroom/`. It's a dead end on **New Outlook for Mac (16.79+)**: Microsoft's Swift rewrite of Outlook does not expose message selection to AppleScript. Every selection accessor (`selected objects`, `current messages`, `selection`, `selection of front window`) returns an empty list or `missing value` even when a message is clearly selected in the UI. The script is kept in the repo for future use — it works in legacy Outlook for Mac and may start working again if Microsoft restores the accessors. Until then, the drag-to-sidebar workflow is the intended path.

## File layout

```
mailroom/
├── app.py                     # FastAPI app — HTMX endpoints, Jinja2 rendering, lifespan-managed watcher
├── templates/
│   ├── base.html              # Tailwind + DaisyUI + HTMX (all via CDN)
│   ├── index.html             # dashboard layout
│   ├── _packages.html         # packages/orders table fragment
│   ├── _detail.html           # row detail modal contents
│   ├── _stats.html            # OOB-swapped stats tiles
│   └── _inbox_chip.html       # OOB-swapped inbox-pending chip
├── pyproject.toml
├── .env.example
├── src/mailroom/
│   ├── db.py                  # SQLite storage
│   ├── easypost.py            # EasyPost SDK wrapper
│   ├── parser.py              # OpenAI email extractor (gpt-4o-mini, JSON mode)
│   ├── inbox.py               # .eml watched-folder pipeline
│   ├── watcher.py             # filesystem watchdog for auto-processing drops
│   ├── notify.py              # macOS notifications (osascript / terminal-notifier)
│   ├── poll.py                # polling loop — run by launchd (inbox + carriers)
│   └── cli.py                 # typer CLI
└── scripts/
    ├── com.tighe.mailroom.gui.plist   # launchd agent: GUI server + inbox watcher, always on
    ├── com.tighe.mailroom.poll.plist  # launchd agent: carrier poller, every 30 min
    ├── open-mailroom.command          # opens the dashboard in your browser
    └── send-to-mailroom.applescript   # Classic Outlook only (dormant on new Outlook)
```

## What's next (v2 ideas)

- **Multi-device sync** — back the SQLite store with an Airtable base or shared Google Sheet so the dashboard reads from any machine.
- **Reminders / digest** — periodic summary emails of pending packages.
- **Phone push** — Pushover or ntfy.sh for notifications when you're away from your Mac.
- **Stale order alerts** — flag orders that have sat without a shipping confirmation for N days.
