# Running the bot 24/7 (macOS launchd)

`python main.py` dies if the network blips, Python crashes, or you reboot. A
launchd **LaunchAgent** keeps it alive: it restarts the bot automatically and
starts it at login.

## Install

1. Fill in the template placeholders, then copy it into `~/Library/LaunchAgents/`:
   ```bash
   PY=$(which python3)
   PROJ=$(pwd)                      # run from the repo root
   MYPATH="$PATH"                   # your interactive PATH (resolves claude, gog, npx)

   sed -e "s#__PYTHON__#$PY#g" \
       -e "s#__PROJECT__#$PROJ#g" \
       -e "s#__PATH__#$MYPATH#g" \
       deploy/com.example.claude-agent.plist \
       > ~/Library/LaunchAgents/com.example.claude-agent.plist
   ```

2. Load it:
   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.claude-agent.plist
   ```

The bot is now running and will survive crashes, network drops, and reboots.

## Manage it

```bash
UID=$(id -u); LABEL=com.example.claude-agent
PLIST=~/Library/LaunchAgents/$LABEL.plist

# restart (after a code change):
launchctl kickstart -k gui/$UID/$LABEL

# stop / unload completely:
launchctl bootout gui/$UID $PLIST

# start again:
launchctl bootstrap gui/$UID $PLIST

# status / pid / last exit code:
launchctl print gui/$UID/$LABEL | grep -E "state|pid|last exit"
```

> Note: an ordinary `kill` no longer stops the bot — launchd respawns it within
> ~1s. Use `bootout` to actually stop it.

## Caveat

A LaunchAgent runs only while you're logged into the GUI session, and pauses
when the Mac **sleeps** (e.g. a laptop lid closing) — like any user process. It
fully covers crashes / network / reboot-to-login, but not "always-on while the
machine sleeps." For true 24/7, run the bot on an always-on host instead.
