"""Run the editor option-creation contract against an isolated in-memory store.

Usage from ``BTN_bot``::

    python dev_tools/verify_editor_option_creation.py --confirm-local

This tool is never imported by ``bot.py``. It refuses production-like environments and
executes only the composed-option pytest fixture, which replaces persistence with an in-memory
store for the lifetime of the test process. It does not write BigQuery or a local runtime file.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


BTN_BOT_DIR = Path(__file__).resolve().parents[1]
TEST_TARGET = (
    "tests/test_editor_blocks_endpoints.py::TestEditorComposedPost::"
    "test_creates_messages_and_option_and_fourth_item_becomes_list"
)


def assert_local_execution() -> None:
    reasons = []
    if os.getenv("GOOGLE_CLOUD_PROJECT", "").strip():
        reasons.append("GOOGLE_CLOUD_PROJECT está configurado")
    if os.getenv("CLOUD_RUN", "").strip().lower() == "true" or os.getenv(
        "K_SERVICE", ""
    ).strip():
        reasons.append("se detectó Cloud Run")
    if os.getenv("APP_ENV", "").strip().lower() in {"production", "prod"}:
        reasons.append("APP_ENV es productivo")
    if reasons:
        raise RuntimeError(
            "La verificación de altas solo puede ejecutarse en desarrollo local: "
            + ", ".join(reasons)
        )


def build_command() -> Sequence[str]:
    return (sys.executable, "-m", "pytest", "-q", TEST_TARGET)


def verify_option_creation() -> int:
    assert_local_execution()
    completed = subprocess.run(build_command(), cwd=BTN_BOT_DIR, check=False)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prueba un alta de opción con persistencia aislada en memoria."
    )
    parser.add_argument(
        "--confirm-local",
        action="store_true",
        help="Confirma que la prueba se ejecutará solamente en desarrollo local.",
    )
    args = parser.parse_args()
    if not args.confirm_local:
        parser.error("falta --confirm-local; no se ejecutó ninguna prueba")

    return verify_option_creation()


if __name__ == "__main__":
    raise SystemExit(main())
