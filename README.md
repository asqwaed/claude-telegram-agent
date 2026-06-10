# claude-agent — your own personal AI assistant on Telegram

A self-hosted personal assistant that runs **locally on your machine** through
[Claude Code](https://claude.com/claude-code) and talks to you over a **Telegram
bot**. It's not a sandboxed chatbot — it has your filesystem, terminal, web
access, email/calendar/drive, your Telegram account, and a persistent
**Obsidian** knowledge base. You message it from your phone; it actually gets
things done.

> ⚠️ This agent runs with full local permissions (`--dangerously-skip-permissions`).
> Run it only on a machine you control, and only let your own Telegram user ID
> talk to it (see `ALLOWED_USERS`). It can read/write files and run commands.

---

## What it can do

- **Chat & tasks** — answer questions, write/run code, edit files, automate things
  on your machine.
- **Rich Telegram input** — understands **text, photos** (it can see them),
  **voice messages** (transcribed locally with Whisper), **replies** (knows which
  message you replied to) and **forwards** (knows who the message is from).
- **Web** — search (Brave), fetch pages, drive a real browser (Playwright) for
  JS-heavy pages, logins, screenshots.
- **Google (multi-account)** — read/search/send Gmail, list/create Calendar
  events, read/write Google Drive — across several accounts (e.g. `personal`,
  `work`).
- **Your Telegram account (MTProto, multi-account)** — read any chat, search your
  history, summarize unread, digest channels, transcribe voice notes, and (with
  confirmation) send/reply/react/forward **as you**.
- **Persistent memory** — a three-layer system (see below) backed by an Obsidian
  vault, so it remembers who you are and what you're working on between sessions.
- **Token usage** — `/usage` shows your Claude subscription limits (5h + weekly)
  and per-request token spend, with an optional chart.
- **GitHub / YouTube / Spotify / SQLite** — repos & issues, video transcripts,
  music, local databases.

## How it talks

The voice is defined in `CLAUDE.md` (the system prompt) — concise, direct, matches
your language. Edit that file to give it whatever personality you want.

## Architecture

```
Telegram  ──>  bot (python-telegram-bot)  ──>  claude --print (headless)
                     │                              │
                     │                              ├─ MCP: local-tools (files, shell,
                     │                              │       obsidian, telegram MTProto,
                     │                              │       voice STT)
                     │                              ├─ Bash: gog (gogcli) for Gmail/
                     │                              │        Drive/Calendar/Docs
                     │                              └─ MCP: brave-search, fetch, playwright,
                     │                                      github, youtube, spotify, sqlite
                     ▼
            memory/ (sessions, usage)  +  Obsidian vault (knowledge base)
```

Each incoming message becomes a headless `claude` invocation with your context.
The reply (and token usage) comes back as JSON and is delivered to Telegram.

### Memory — three layers

1. **Session** — recent conversation, auto-compressed past a token budget.
2. **Profile** (`<vault>/_meta/Profile.md`) — a small always-loaded card about you.
3. **Obsidian vault** — the deep knowledge base; relationships are `[[wikilinks]]`.
   The agent **proactively** captures people, projects, decisions and links them
   on its own, and pings you with the file path whenever it writes.

## Requirements

- **macOS or Linux** (developed on macOS).
- **[Claude Code CLI](https://claude.com/claude-code)** installed and logged in
  (`claude --version` must work). A Claude subscription or API key.
- **Python 3.11+**.
- **Node.js** (for the npm-based MCP servers: brave-search, github, playwright,
  youtube, spotify, filesystem).
- **ffmpeg** (for voice-message transcription) — `brew install ffmpeg` /
  `apt install ffmpeg`.
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather).

## Setup

### 1. Clone & install
```bash
git clone https://github.com/<you>/claude-agent.git
cd claude-agent
python3 -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
```
Fill in `.env`:
- `TELEGRAM_TOKEN` — from @BotFather
- `ALLOWED_USERS` — your numeric Telegram user ID (get it from
  [@userinfobot](https://t.me/userinfobot)); only these IDs may use the bot
- `TELEGRAM_CHAT_ID` — your chat ID for proactive notifications
- service keys you want: `BRAVE_API_KEY`, `GITHUB_TOKEN`, Spotify, etc.

### 3. Register the MCP servers
The agent loads MCP servers from `~/.claude/mcp.json`. Create it (see
`docs/mcp.json.example` below) with the servers you want. At minimum, point
`local-tools` at this repo's `mcp/server.py`:
```json
{
  "mcpServers": {
    "local-tools": {
      "command": "python3",
      "args": ["/absolute/path/to/claude-agent/mcp/server.py"]
    }
  }
}
```
Add `brave-search`, `fetch`, `playwright`, `github`, `youtube`, `spotify`,
`filesystem` as desired (all are `npx`/`python3 -m` based). **Use `python3`, not
`python`**, if your system has no `python` alias.

### 4. (Optional) Google accounts — via gogcli
Gmail/Drive/Calendar/Docs are handled by the [`gog`](https://gogcli.sh/) binary
(called through Bash, so its command schemas don't bloat the model's context).
Install it, register an OAuth **Desktop app** client JSON from Google Cloud
Console, then authorize each account:
```bash
brew install openclaw/tap/gogcli            # or see gogcli.sh for other installs
gog auth credentials set credentials.json --client default
gog auth add you@gmail.com --services gmail,calendar,drive --client default
gog auth add you@work.com  --services gmail,calendar,drive --client default
gog auth alias set personal you@gmail.com   # friendly -a aliases
gog auth alias set work     you@work.com
```
The agent then runs e.g. `gog -a personal --plain gmail search "is:unread"`.

### 5. (Optional) Telegram user account(s)
For the `tg_*` tools, get `api_id`/`api_hash` from
[my.telegram.org](https://my.telegram.org). In `.env` set
`TELEGRAM_ACCOUNTS=personal` and `TELEGRAM_PERSONAL_API_ID/_API_HASH/_PHONE`,
then log in (a code is sent to your Telegram):
```bash
python credentials/telegram_auth.py personal
```

### 6. (Optional) Obsidian vault
Set `VAULT_DIR` (default `~/Documents/Vault`). Create the folder and open it in
Obsidian as a vault. The agent will populate it.

### 7. Run
```bash
python main.py
```
Message your bot on Telegram. To keep it running 24/7 (auto-restart on crash,
network blip, or reboot), see [`deploy/`](deploy/README.md) for a ready-made
macOS **launchd** LaunchAgent — or use any process manager (`pm2`, `systemd`).

## Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | intro / command list |
| `/profile` | show the always-loaded profile |
| `/note <text>` | quick-capture a note to the vault Inbox |
| `/today [text]` | show or append today's journal note |
| `/find <query>` | search the vault |
| `/usage`, `/usage chart` | token usage + subscription limits, optional chart |
| `/context`, `/compress` | conversation context size / compress history |
| `/clear` | wipe conversation history (knowledge is kept) |
| `/tools` | manual tool-test commands |

## Security notes

- **Secrets never leave your machine.** `.env`, `credentials/` (OAuth tokens,
  Telegram `.session` login keys) and `memory/` are gitignored. Don't commit them.
- The Telegram `.session` files are **login keys to your account** — treat them
  like passwords.
- Restrict the bot with `ALLOWED_USERS`. Anyone else who messages it is refused.
- The agent runs commands with your user's permissions. Only run it on machines
  and networks you trust.

## License

MIT — see [LICENSE](LICENSE).
