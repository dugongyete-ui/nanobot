"""Prepare a private nanobot config for a Replit-hosted WebUI."""

from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> None:
    if not os.environ.get("SESSION_SECRET"):
        raise SystemExit(
            "Missing Replit Secret SESSION_SECRET. Add it in Replit Secrets, then restart."
        )

    config_path = Path.home() / ".nanobot" / "replit-config.json"
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(
                f"Unable to read nanobot's Replit config at {config_path}: {exc}"
            ) from exc
        if not isinstance(config, dict):
            raise SystemExit(f"Nanobot's Replit config must be a JSON object: {config_path}")
    else:
        config = {}

    channels = config.setdefault("channels", {})
    if not isinstance(channels, dict):
        raise SystemExit("Nanobot config field 'channels' must be a JSON object.")
    websocket = channels.setdefault("websocket", {})
    if not isinstance(websocket, dict):
        raise SystemExit("Nanobot config field 'channels.websocket' must be a JSON object.")

    has_bootstrap_secret = any(
        websocket.get(key)
        for key in ("tokenIssueSecret", "token_issue_secret", "token")
    )
    if not has_bootstrap_secret:
        websocket["tokenIssueSecret"] = "${SESSION_SECRET}"
    websocket["websocketRequiresToken"] = True

    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    config_path.chmod(0o600)
    print("Replit WebUI config is ready; authentication remains enabled.")


if __name__ == "__main__":
    main()
