"""Export each service's OpenAPI document to docs/openapi/*.yaml (no Docker needed).

    python scripts/export_openapi.py
"""

import importlib
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {
    "services/customer-service": "customer-api.yaml",
    "services/order-service": "order-api.yaml",
    "agents/coordinator": "agent-api.yaml",
}

for service, filename in SERVICES.items():
    service_dir = ROOT / service
    sys.path.insert(0, str(service_dir))
    for mod in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[mod]
    main = importlib.import_module("app.main")
    spec = main.create_app().openapi()
    out = ROOT / "docs" / "openapi" / filename
    out.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
    print(f"wrote {out.relative_to(ROOT)}")
    sys.path.remove(str(service_dir))
