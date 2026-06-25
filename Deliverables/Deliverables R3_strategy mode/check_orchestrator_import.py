import re
from pathlib import Path

p = Path("app/services/pipeline/pipeline_orchestrator.py")
src = p.read_text(encoding="utf-8")

# Find all market_regime references
for i, line in enumerate(src.splitlines(), 1):
    if "market_regime" in line or "regime" in line.lower():
        print(f"  line {i:4d}: {line}")
