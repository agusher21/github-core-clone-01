#!/usr/bin/env python3
"""Entrypoint for the forge: python3 main.py (or python3 /bot/main.py).

The whole forge lives in forge.py (HTTP + REST v3 + GraphQL + MCP + git smart
HTTP, all on one in-memory store); gitcore.py holds the git object model and
packfile codec. Standard library only — no install, no git binary, no outside
services.

Lifecycle (per the competition's trial contract):
  1. bind an ephemeral port (never 8080),
  2. POST {"port": n} to $MAKEFUN_ENV_URL/register,
  3. serve until GET $MAKEFUN_ENV_URL/done returns 200, then exit 0.
Without MAKEFUN_ENV_URL the server just serves forever on 127.0.0.1.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge import main

if __name__ == "__main__":
    main()
