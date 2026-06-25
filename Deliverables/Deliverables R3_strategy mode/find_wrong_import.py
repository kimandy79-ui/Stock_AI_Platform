"""Find every file that imports from app.services.market_regime (wrong path)."""
from pathlib import Path

wrong = []
right = []

for py in Path(".").rglob("*.py"):
    if "__pycache__" in str(py):
        continue
    try:
        src = py.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue
    if "app.services.market_regime" in src:
        wrong.append(py)
    if "app.services.regime" in src:
        right.append(py)

print("=== Files with WRONG import (app.services.market_regime) ===")
for f in wrong:
    for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        if "app.services.market_regime" in line:
            print(f"  {f}:{i}  {line.strip()}")

print()
print("=== Files with CORRECT import (app.services.regime) ===")
for f in right:
    for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        if "app.services.regime" in line:
            print(f"  {f}:{i}  {line.strip()}")
