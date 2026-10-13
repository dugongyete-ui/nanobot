#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  fail "Python 3.11 or newer is required."
fi

command -v "$PYTHON_BIN" >/dev/null 2>&1 ||
  fail "Python executable '$PYTHON_BIN' was not found."
command -v uv >/dev/null 2>&1 ||
  fail "uv is required. On Replit, make sure the Python module is enabled."

PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' ||
  fail "Python 3.11 or newer is required; found $PYTHON_VERSION."

SITE_PACKAGES="$ROOT_DIR/.pythonlibs/lib/python${PYTHON_VERSION}/site-packages"
mkdir -p "$SITE_PACKAGES"

command -v bun >/dev/null 2>&1 ||
  fail "Bun is required for the WebUI and TUI. Enable the Replit Node.js module."

mapfile -t CHANNEL_MANIFESTS < <(
  find "$ROOT_DIR/nanobot/channels" -mindepth 2 -maxdepth 2 \
    -type f -name manifest.py -print | sort
)
DEPENDENCY_FILES=(
  pyproject.toml
  webui/package.json
  webui/bun.lock
  tui/package.json
  tui/bun.lock
  "${CHANNEL_MANIFESTS[@]}"
)

DEPENDENCY_FINGERPRINT="$(
  {
    printf '%s\n' \
      "$PYTHON_VERSION" \
      "$(uname -s)" \
      "$(uname -m)" \
      "$(uv --version)" \
      "$(bun --version)"
    sha256sum -- "${DEPENDENCY_FILES[@]}"
  } | sha256sum | awk '{print $1}'
)"
STAMP_FILE="$ROOT_DIR/.pythonlibs/.nanobot-runtime-dependencies"

if [[ -f "$STAMP_FILE" ]] && [[ "$(<"$STAMP_FILE")" == "$DEPENDENCY_FINGERPRINT" ]]; then
  printf 'Python runtime dependencies already match the project manifests.\n'
else
  RUNTIME_EXTRAS="$("$PYTHON_BIN" - <<'PY'
import tomllib
from pathlib import Path

project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
extras = project.get("project", {}).get("optional-dependencies", {})
print(",".join(sorted(name for name in extras if name != "dev")))
PY
)"

  PROJECT_SPEC="."
  if [[ -n "$RUNTIME_EXTRAS" ]]; then
    PROJECT_SPEC=".[${RUNTIME_EXTRAS}]"
  fi

  printf 'Installing Python project and runtime extras: %s\n' "${RUNTIME_EXTRAS:-none}"
  uv pip install --target "$SITE_PACKAGES" -e "$PROJECT_SPEC"

  CHANNEL_REQUIREMENTS="$(mktemp "${TMPDIR:-/tmp}/nanobot-channel-deps.XXXXXX")"
  trap 'rm -f "$CHANNEL_REQUIREMENTS"' EXIT

  PYTHONPATH="$SITE_PACKAGES:$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" - "$CHANNEL_REQUIREMENTS" <<'PY'
from pathlib import Path
import sys

from nanobot.channels.registry import discover_plugins

plugins = discover_plugins()
requirements = sorted(
    {dependency for plugin in plugins.values() for dependency in plugin.dependencies}
)
Path(sys.argv[1]).write_text(
    "".join(f"{requirement}\n" for requirement in requirements),
    encoding="utf-8",
)
print(
    f"Found {len(requirements)} dependency requirements across "
    f"{len(plugins)} built-in channels."
)
PY

  if [[ -s "$CHANNEL_REQUIREMENTS" ]]; then
    printf 'Installing dependencies for all built-in chat channels.\n'
    uv pip install --target "$SITE_PACKAGES" -r "$CHANNEL_REQUIREMENTS"
  fi

  PYTHONPATH="$SITE_PACKAGES:$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" - <<'PY'
from pathlib import Path
import sys
import tomllib

from nanobot.channels.registry import discover_plugins
from nanobot.optional_features import extra_installed

project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
extras = project.get("project", {}).get("optional-dependencies", {})
missing = []

for name, dependencies in extras.items():
    if name != "dev" and not extra_installed(name, dependencies):
        missing.append(f"Python extra: {name}")

for name, plugin in discover_plugins().items():
    if plugin.dependencies and not extra_installed(name, list(plugin.dependencies)):
        missing.append(f"Channel: {name}")

if missing:
    print("Some runtime dependencies are still missing:", file=sys.stderr)
    for item in missing:
        print(f"  - {item}", file=sys.stderr)
    raise SystemExit(1)

print("Python runtime dependencies verified.")
PY

  printf '%s\n' "$DEPENDENCY_FINGERPRINT" > "$STAMP_FILE"
fi

printf 'Installing locked TUI dependencies with Bun.\n'
(
  cd "$ROOT_DIR/tui"
  bun install --frozen-lockfile
)

printf 'Installing locked WebUI dependencies and building its bundle with Bun.\n'
(
  cd "$ROOT_DIR/webui"
  bun install --frozen-lockfile
  bun run build
)

printf '\nInstallation complete.\n'
printf 'To use AI chat, configure a provider credential and model in WebUI → Settings → Models.\n'
printf 'To use the remote WebUI, keep SESSION_SECRET set in Replit Secrets.\n'
