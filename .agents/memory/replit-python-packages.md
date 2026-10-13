---
name: Replit Python package install
description: Replit-specific Python package installation behavior and safe workaround.
---

The Replit `installLanguagePackages` callback crashed inside UPM's Python backend even with a short dependency list. Installing into the Nix-managed system interpreter is blocked because `/nix/store` is immutable. In this Repl, use `uv pip install --target .pythonlibs/lib/python3.11/site-packages -e .`; Python already adds that project-local package directory to `sys.path`. Keep `.pythonlibs/` ignored.

**Why:** UPM failed before making changes, while Nix-owned Python cannot be modified in place.

**How to apply:** Use this project-local target only when normal Replit module/package installation still fails. Do not force writes into `/nix/store` or create a virtual environment.
