import os
from pathlib import Path

root = Path(".")
results = list(root.rglob("market_regime_engine.py"))
for r in results:
    print(r)

# Also check for __init__.py in market_regime folder
for r in root.rglob("__init__.py"):
    if "market_regime" in str(r):
        print(f"  __init__.py: {r}")
