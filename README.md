# CoronetBot

LLM-based moderation bot for the Coronet strata Discord community.

For each new text message, reply, thread message, or forum post in the server, the bot
checks the configured rules. Allowed messages are left untouched. For a clear violation,
the bot DMs the author with the original draft, reasons, and a suggested revision, then
deletes the public message. Classifier failures **fail open** and never delete a message.

## Commands

- `/validate text` — privately validate and refine a draft before posting
- `/rules` — show the active moderation rules
- `/help` — show commands, version, model, and configuration

Command responses are ephemeral when invoked in a server. The globally registered
commands can also be used in a DM with the bot.

## Moderation rules

Rules are loaded from [`RULES.md`](RULES.md) at startup. The included rules are
**provisional defaults inferred from the original specification**; replace them with the
server's canonical rules before deploying.

The model must return a structured decision. CoronetBot validates that every cited quote
is an exact substring of the original message and that blocked decisions include both a
violation and a suggested revision. Invalid responses fail open.

## Local setup

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
cp .env.example .env
# Fill in .env, then:
set -a; . ./.env; set +a
uv run coronetbot
```

The `set` commands above are POSIX shell syntax. With another shell, load/export `.env`
using its normal mechanism. CoronetBot itself reads environment variables, not `.env`
directly.

Run checks:

```sh
uv run ruff check .
uv run pytest
```

## Discord application setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. On **Bot**, create/reset the bot token. Put it in `.env` as `DISCORD_TOKEN`; do not
   commit or paste it into chat.
3. On **Bot → Privileged Gateway Intents**, enable **Message Content Intent**.
4. On **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands`.
5. Grant only these bot permissions:
   - View Channels
   - Read Message History
   - Manage Messages
6. Open the generated URL and install the bot in the server.

For development, set `DISCORD_GUILD_ID` to the test server ID so slash commands sync to
that server immediately. Leave it unset in production to register commands globally;
Discord may take up to an hour to propagate them.

The bot's role must be able to view every moderated channel and must sit high enough in
the server's permission structure to delete messages there. Private channels not visible
to the bot cannot be moderated.

## LLM configuration

CoronetBot calls an OpenAI-compatible chat-completions endpoint with JSON mode:

| Variable | Required | Default |
|---|---:|---|
| `DISCORD_TOKEN` | yes | — |
| `LLM_API_KEY` | for authenticated endpoints | — |
| `LLM_BASE_URL` | no | `https://api.openai.com/v1/chat/completions` |
| `LLM_MODEL` | no | `gpt-4.1-mini` |
| `RULES_PATH` | no | `RULES.md` |
| `DISCORD_GUILD_ID` | no | global command registration |
| `MAX_CONCURRENCY` | no | `8` |
| `LLM_TIMEOUT_SECONDS` | no | `30` |
| `LLM_RETRIES` | no | `2` |

The selected endpoint must support `response_format: {"type": "json_object"}`. Model
calls are bounded by a concurrency semaphore and retry transient errors with exponential
backoff.

## Container deployment

```sh
cp .env.example .env
# Fill in .env and update RULES.md
docker compose up --build -d
docker compose logs -f bot
```

The container runs as an unprivileged user and mounts `RULES.md` read-only. Restart it
after changing rules or environment variables.

## Privacy and operational behaviour

- CoronetBot does not persist message bodies and deliberately omits them from logs.
- Message text and active rules are sent to the configured LLM provider. Confirm that
  provider's retention/privacy policy before production use.
- Discord and the LLM provider may independently retain data; deleting a Discord message
  is not a guarantee of erasure from their systems.
- If a user has DMs disabled, the bot still deletes a confirmed violating message and
  records only IDs and rule names in its operational log.
- Attachment-only messages are currently left untouched; attached file contents are not
  downloaded or sent to the model.
- Messages from bots and webhooks are ignored to avoid automation loops.

## Behaviour

A removal DM follows this structure (split across messages when Discord's 2,000-character
limit requires it):

> Your message in **#general** was removed and has not been retained publicly.
>
> **Original draft:**
> > complete original message
>
> **Reasons:**
> • **Personal attack:** “dishonest idiot”  
>   Criticises the person rather than their conduct.
>
> **Suggested revision:**
> > The reported figures do not appear to match the payment records.  
> > Could someone review and clarify the discrepancy?
>
> You can copy and revise your original draft above. You may use `/validate` here to
> validate/refine your message before trying to send it again. Use `/rules` to see the
> active rules.
