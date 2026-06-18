#!/usr/bin/env python3
"""Compatibility proxy for the canonical AFX script path.

Canonical script location:
  AFX\\AFX_reinit.py

This file intentionally forwards imports and CLI execution so callers that
still reference the root path continue to work while runtime logic lives in one
place.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_CANONICAL_SCRIPT = Path(__file__).resolve().parent / "AFX" / "AFX_reinit.py"

if not _CANONICAL_SCRIPT.exists():
    raise FileNotFoundError(f"Canonical script not found: {_CANONICAL_SCRIPT}")

_spec = importlib.util.spec_from_file_location("afx_reinit_canonical", _CANONICAL_SCRIPT)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Unable to load module spec from {_CANONICAL_SCRIPT}")

_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

# Re-export canonical module symbols for import compatibility.
for _name, _value in _module.__dict__.items():
    if _name.startswith("__") and _name not in {"__doc__", "__all__"}:
        continue
    globals()[_name] = _value

if __name__ == "__main__":
    if hasattr(_module, "main"):
        _ret = _module.main()
        if isinstance(_ret, int):
            raise SystemExit(_ret)
        raise SystemExit(0)
    raise SystemExit("Canonical module does not define main()")
