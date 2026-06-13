from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ADAPTER_SETTINGS_PATH = BASE_DIR / "cortex_adapter_settings.json"
DEFAULT_ADAPTER_NAME = "Ethernet"


def normalize_adapter_name(value: str | None) -> str:
    text = (value or "").strip()
    return text or DEFAULT_ADAPTER_NAME


def load_adapter_name(default: str = DEFAULT_ADAPTER_NAME) -> str:
    if ADAPTER_SETTINGS_PATH.exists():
        try:
            data = json.loads(ADAPTER_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                adapter_name = data.get("adapter_name")
                if isinstance(adapter_name, str) and adapter_name.strip():
                    return adapter_name.strip()
        except Exception:
            pass

    return default


def save_adapter_name(adapter_name: str) -> str:
    resolved = normalize_adapter_name(adapter_name)
    ADAPTER_SETTINGS_PATH.write_text(
        json.dumps({"adapter_name": resolved}, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return resolved