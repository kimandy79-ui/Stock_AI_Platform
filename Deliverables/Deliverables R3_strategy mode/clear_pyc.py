"""Delete stale __pycache__ files for pipeline_orchestrator and regime modules."""
from pathlib import Path

deleted = 0

for pyc in Path(".").rglob("pipeline_orchestrator*.pyc"):
    pyc.unlink()
    print(f"Deleted: {pyc}")
    deleted += 1

for pyc in Path(".").rglob("market_regime*.pyc"):
    pyc.unlink()
    print(f"Deleted: {pyc}")
    deleted += 1

print(f"\nDone. {deleted} file(s) deleted.")
print("Now re-run the pipeline.")
