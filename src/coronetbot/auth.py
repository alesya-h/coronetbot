from __future__ import annotations

import argparse
import os

from codex_backend_sdk import OpenAI


def main() -> None:
    parser = argparse.ArgumentParser(description="Authenticate CoronetBot with ChatGPT Codex")
    parser.add_argument(
        "--check",
        action="store_true",
        help="check stored credentials without starting an interactive login",
    )
    parser.add_argument("--force", action="store_true", help="force a fresh interactive login")
    args = parser.parse_args()
    codex_home = os.environ.get("CB_CODEX_HOME", "").strip()
    if codex_home:
        os.environ["CODEX_HOME"] = codex_home
    if args.check and args.force:
        parser.error("--check and --force cannot be combined")

    try:
        client = OpenAI().authenticate(interactive=not args.check, force=args.force)
    except Exception as exc:
        raise SystemExit(f"Codex authentication failed: {exc}") from None

    info = client.account_info()
    plan = info.get("plan_type") or "unknown"
    print(f"Codex authentication is usable (plan: {plan}).")


if __name__ == "__main__":
    main()
