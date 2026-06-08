# credentials/

Secrets and OAuth tokens live here. **Nothing in this folder is ever committed**
— the whole directory is gitignored.

## what goes here

| file | what it is | how to get it |
|------|-----------|---------------|
| `google_credentials.json` | OAuth *Desktop app* client downloaded from Google Cloud Console | console.cloud.google.com → APIs & Services → Credentials → Create OAuth client ID → Desktop app → Download JSON |
| `google_token.json` | Generated automatically | run `python credentials/google_auth.py` once |
| `google_auth.py` | One-time auth helper (not a secret) | already in repo |

Spotify tokens are managed by the `spotify-mcp` server itself (stored under
`~/.spotify-mcp/`), so nothing Spotify-related needs to live here.

The actual API keys/tokens (Brave, GitHub, Spotify client id/secret, Telegram)
live in the project-root `.env`, which is also gitignored.

## first-time setup

```bash
# 1. put google_credentials.json in this folder (see table above)
# 2. authorize Google (opens a browser once):
python credentials/google_auth.py
```

## ⚠️ never

- never commit `*.json` tokens or `google_credentials.json`
- never paste these values into chat, issues, or logs
- if a token leaks, revoke it in the provider console and re-run the auth helper
