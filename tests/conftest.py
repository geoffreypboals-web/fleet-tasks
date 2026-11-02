"""
Project: fleet-tasks
File: tests/conftest.py
Description: Points every test at the test fixtures (tests/policy.json and
    tests/catalog.json: a real multi-machine setup with the names replaced),
    so the tests don't depend on your own policy.json or catalog.json.
Inputs: none (runs before the tests are collected).
Outputs: FLEET_POLICY and FLEET_CATALOG set, unless you set them yourself.
Troubleshooting:
    - A test reads your own policy: check FLEET_POLICY isn't set in your shell.
"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("FLEET_POLICY", str(HERE / "policy.json"))
os.environ.setdefault("FLEET_CATALOG", str(HERE / "catalog.json"))
