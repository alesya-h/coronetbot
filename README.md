# CoronetBot

LLM-based moderation bot for the Coronet strata Discord community.

For each new or edited message, reply, thread message, or forum post in the server, the
bot checks text and supported image attachments against the configured rules. Allowed
messages are left untouched. For a clear violation,
the bot DMs the author with the original draft, reasons, and a suggested revision, then
deletes the public message. If classification or audit logging fails, the bot leaves the
message in place.

## Commands

- `/validate [text] [image]` — privately validate and refine draft text, an image, or both
- `/rules` — show the active moderation rules
- `/help` — show commands, version, model, and configuration

Commands are registered only in the server selected by `CB_MODE`, and their responses are
ephemeral. They are not available in DMs with the bot. Invoke `/validate` in the channel
or thread where the draft is intended so the review receives relevant context.

## Moderation rules

Rules are loaded from [`RULES.md`](RULES.md) at startup. The operational policy is adapted
from [`resources/moderation-agent-prompt.md`](resources/moderation-agent-prompt.md) and the
source [`resources/forum-rules.md`](resources/forum-rules.md); those artifacts are policy
inputs rather than runtime prompts that must be copied verbatim.

The model must return a structured decision. CoronetBot validates that every cited quote
is an exact substring of the proposed title/body and that blocked decisions include both
a violation and a suggested revision. An invalid response leaves the message in place.

Reviews include the channel type/name/description, forum title and root post, reply target,
recent context, recent same-author messages, and attachment metadata when available. This
lets the policy distinguish general chat, original `C:`/`Q:` forum posts, and scoped forum
replies without treating quoted/contextual text as if the author wrote it. Supported image
attachments are downloaded from Discord, signature-checked, and sent as ephemeral
multimodal inputs so visible text and imagery can be assessed. Other attachment contents
are not inferred when no extracted text is available.

## Local setup

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run coronetbot-auth     # Browser OAuth; reuses ~/.codex/auth.json when already logged in
uv run coronetbot-auth --check
cp .env.example .env
# Fill in CB_DISCORD_TOKEN and choose CB_MODE, then:
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
2. On **Bot**, create/reset the bot token. Put it in `.env` as `CB_DISCORD_TOKEN`; do not
   commit or paste it into chat.
3. On **Bot → Privileged Gateway Intents**, enable **Message Content Intent**.
4. On **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands`.
5. Grant only these bot permissions:
   - View Channels
   - Read Message History
   - Send Messages
   - Manage Messages
   - Manage Threads
6. Open the generated URL and install the bot in the server.

Create exactly one text channel named `#bot-moderation-audit` and restrict it to the intended audit
readers plus the bot. The bot must be able to view and send messages there. It will refuse
to operate if that channel is absent, duplicated, or inaccessible.

`CB_MODE=dev` selects **Alesya's test server** (`1526764377171296296`);
`CB_MODE=production` selects **Coronet** (`1439793454153601066`). Slash commands and
moderation are scoped to the selected server.

The bot's role must be able to view every moderated channel and must sit high enough in
the server's permission structure to delete messages and threads there. When a new forum
post or chat-thread starter is substantively rejected, the entire thread container is
deleted so a rule-breaking title cannot remain visible. A missing, malformed, or mismatched
forum `C: ` / `Q: ` prefix is only an organising issue: the post remains and the author
receives an audited private reminder. Private channels not visible to the bot cannot be
moderated.

## LLM configuration

The backend uses
[`codex-backend-sdk`](https://pypi.org/project/codex-backend-sdk/) directly to call the
ChatGPT Codex Responses endpoint with subscription OAuth. It runs `gpt-5.6-sol` at high
reasoning effort and requests a strict Pydantic-derived JSON schema. There is no pi,
Codex CLI, Node.js, or per-message subprocess in the runtime.

| Variable | Required | Default |
|---|---:|---|
| `CB_DISCORD_TOKEN` | yes | — |
| `CB_MODE` | no | `dev` |
| `CB_LLM_MODEL` | no | `gpt-5.6-sol` |
| `CB_LLM_THINKING` | no | `high` |
| `CB_RULES_PATH` | no | `RULES.md` |
| `CB_MAX_CONCURRENCY` | no | `2` |
| `CB_LLM_TIMEOUT_SECONDS` | no | `120` |
| `CB_LLM_RETRIES` | no | `2` |
| `CB_STATE_PATH` | no | `.coronetbot-state.json` |
| `CB_BACKFILL_LOOKBACK_SECONDS` | no | `3600` |
| `CB_MAX_IMAGES_PER_MESSAGE` | no | `4` |
| `CB_MAX_IMAGE_BYTES` | no | `8000000` |
| `CB_CODEX_HOME` | no | `~/.codex` |

Calls are bounded by a concurrency semaphore. Authentication/refresh operations are
serialized, while API requests use independent clients and may run concurrently. Each
classification gets up to two full application attempts with a fresh client; this covers
transient transport, structured-output, and response-validation failures. Each underlying
SDK request may additionally use `CB_LLM_RETRIES`. Keep concurrency conservative because
ChatGPT subscription limits differ from API limits.

The bot stores the highest processed message ID per channel, a hash of each reviewed
thread title, pending failed message IDs, and up to 5,000 recent approved message versions
in `CB_STATE_PATH`. Approved versions contain message text and attachment IDs/names so later
edits can be compared and the pre-edit version can be preserved. Protect this state file as
private moderation data. Messages left in place after transient moderation/audit/deletion
failures are queued independently of channel cursors and explicitly retried during startup
backfill. On startup it also fetches and processes visible messages newer
than that cursor and reviews active titles that are new or renamed. For channels with no
cursor yet, it processes visible recent history up to `CB_BACKFILL_LOOKBACK_SECONDS` old;
set this to `0` before a first production launch if you want to start from a clean
baseline instead of moderating recent pre-existing messages.

`codex-backend-sdk` is an unofficial community library over undocumented ChatGPT
endpoints. It is pinned exactly; backend changes may require an SDK upgrade.

## Container deployment

First authenticate on a machine with a browser using `uv run coronetbot-auth`. Securely
transfer `~/.codex/auth.json` to the server, then seed the container's credential volume:

```sh
cp .env.example .env
# Fill in CB_DISCORD_TOKEN/CB_MODE and place auth.json temporarily on the server, then:
docker compose build
docker compose run --rm -T bot sh -c 'umask 077; cat > "$CB_CODEX_HOME/auth.json"' < auth.json
rm auth.json
docker compose run --rm -T bot coronetbot-auth --check
docker compose up -d
docker compose logs -f bot
```

The Python-only container runs as an unprivileged user, mounts `RULES.md` read-only, and
stores OAuth credentials in the `codex_auth` Docker volume and per-channel cursors in the
`bot_state` volume, so SDK token refreshes and missed-message backfill survive container
replacement. Treat both the source credential file and the auth volume as secrets; never
put either in logs, source control, images, or chat. Restart the service after changing
rules or environment variables.

## Audit retention and operational behaviour

- Every in-scope user message and message edit is copied to `#bot-moderation-audit` with
  its author, location, attachments, moderation judgement, bot response, and action outcome. `/validate`,
  `/rules`, and `/help` inputs/responses are also audited.
- Audit records are retained by Discord according to the server's retention practices.
  Access to `#bot-moderation-audit` should therefore be tightly restricted.
- The bot never moderates `#bot-moderation-audit` itself, bot messages, or webhooks; this prevents
  recursive audit loops.
- The entire **Committee internal** category (`1491596963647324180`) is excluded from
  message, thread-title, and startup-backfill moderation. Audit output still works when the
  audit channel is located inside that category.
- If the initial audit record or a blocked judgement cannot be written, moderation fails
  open and the source message is not deleted. Successfully processed messages advance a
  per-channel cursor so restarts can backfill visible messages that arrived while the bot
  was offline.
- A blocked edit to the latest message is removed and returned to its author privately. If
  later messages exist, the bot also posts a continuity notice containing the approved
  pre-edit version. A blocked starter-body edit removes only the starter message. A blocked
  thread-title edit deletes the thread and DMs other participants copies of their messages.
- Message text and active rules are also sent through the ChatGPT Codex subscription
  provider. Confirm its current retention/privacy policy before production use.
- Discord and the LLM provider may independently retain data; deleting a public Discord
  message is not erasure from `#bot-moderation-audit` or provider systems.
- PNG, JPEG, GIF, and WebP attachments are downloaded and sent to the model up to the
  configured count and per-image byte limits. Image-only messages are classified. If an
  apparent image cannot be downloaded or safely recognized, moderation fails open and the
  message remains. Non-image attachment contents are not downloaded or analysed.

## Behaviour

Removal and `/validate` feedback is split into mobile-copy-friendly messages. Explanations
and labels are separate from copyable content; each suggested revision and original draft
is sent as its own plain-text message (and split further only when Discord's 2,000-character
limit makes that unavoidable). The sequence is:

1. Removal status and reasons
2. “Suggested revision — copy the next message”
3. The plain suggested revision
4. “Original draft — copy the next message”
5. The plain original draft
6. Instructions for revising, `/validate`, and `/rules`

While we don't take appeals regarding the moderation of individual messages, feedback on
the rules used for moderation is welcome in
`#discord-server-feedback`.
