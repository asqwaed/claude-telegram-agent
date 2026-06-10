#!/usr/bin/env python3
"""Self-test for the hook scripts. Feeds mock Claude Code payloads to each hook
via subprocess and checks the exit code. Run: python3 hooks/_test_hooks.py
(kept out of the live hook wiring; safe to delete.)"""
import json
import subprocess
import sys
from pathlib import Path

H = Path(__file__).resolve().parent
EPISODES = H.parent / "memory" / "episodes.jsonl"

passed = failed = 0


def run(script, payload):
    p = subprocess.run(
        [sys.executable, str(H / script)],
        input=json.dumps(payload), text=True, capture_output=True,
    )
    return p.returncode


def chk(desc, want, got):
    global passed, failed
    ok = want == got
    print(f"  {'✅' if ok else '❌'} {desc} (want {want}, got {got})")
    passed += ok
    failed += not ok


print("--- block-dangerous-bash:")
chk("rm -rf / -> block", 2, run("block-dangerous-bash.py", {"tool_input": {"command": "rm -rf /"}}))
chk("sudo rm -rf -> block", 2, run("block-dangerous-bash.py", {"tool_input": {"command": "sudo rm -rf /var"}}))
chk("dd of=/dev -> block", 2, run("block-dangerous-bash.py", {"tool_input": {"command": "dd if=/dev/zero of=/dev/disk2"}}))
chk("fork bomb -> block", 2, run("block-dangerous-bash.py", {"tool_input": {"command": ":(){:|:&};:"}}))
chk("DROP TABLE -> block", 2, run("block-dangerous-bash.py", {"tool_input": {"command": "psql -c 'DROP TABLE users'"}}))
chk("rm -rf node_modules -> allow", 0, run("block-dangerous-bash.py", {"tool_input": {"command": "rm -rf node_modules"}}))
chk("normal gog -> allow", 0, run("block-dangerous-bash.py", {"tool_input": {"command": "gog -a personal gmail search x"}}))
# False positives: the pattern merely *mentioned* inside a quoted string.
chk("commit msg mentions 'rm -rf /' -> allow", 0, run("block-dangerous-bash.py", {"tool_input": {"command": "git commit -m 'hook blocks rm -rf / and friends'"}}))
chk("echo mentions rm -rf / -> allow", 0, run("block-dangerous-bash.py", {"tool_input": {"command": "echo 'never run rm -rf /'"}}))
chk("real chained rm -rf / -> block", 2, run("block-dangerous-bash.py", {"tool_input": {"command": "cd /tmp && rm -rf /"}}))

print("--- protect-secrets:")
chk("Read .env -> block", 2, run("protect-secrets.py", {"tool_name": "Read", "tool_input": {"file_path": "/x/claude-agent/.env"}}))
chk("Read .session -> block", 2, run("protect-secrets.py", {"tool_name": "Read", "tool_input": {"file_path": "credentials/telegram_main.session"}}))
chk("cat .env via bash -> block", 2, run("protect-secrets.py", {"tool_name": "Bash", "tool_input": {"command": "cat .env | grep TOKEN"}}))
chk("token.json -> block", 2, run("protect-secrets.py", {"tool_name": "Read", "tool_input": {"file_path": "credentials/google_token.json"}}))
chk("google_auth.py -> allow", 0, run("protect-secrets.py", {"tool_name": "Read", "tool_input": {"file_path": "credentials/google_auth.py"}}))
chk(".env.example -> allow", 0, run("protect-secrets.py", {"tool_name": "Read", "tool_input": {"file_path": ".env.example"}}))
chk("config.py -> allow", 0, run("protect-secrets.py", {"tool_name": "Edit", "tool_input": {"file_path": "config.py"}}))

print("--- capture-correction:")
if EPISODES.exists():
    EPISODES.unlink()
chk("correction -> exit 0", 0, run("capture-correction.py", {"prompt": "да не надо так, я же просил по-другому", "session_id": "t1"}))
chk("normal -> exit 0", 0, run("capture-correction.py", {"prompt": "окей супер спасибо", "session_id": "t1"}))
n = len(EPISODES.read_text().splitlines()) if EPISODES.exists() else 0
chk("exactly 1 episode captured", 1, n)
if n:
    print("  episode:", EPISODES.read_text().strip())

print(f"\nИТОГО: ✅ {passed} / ❌ {failed}")
sys.exit(1 if failed else 0)
