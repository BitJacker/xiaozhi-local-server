"""Strumento predefinito: spazio libero sul disco dove è installato il server."""
import shutil
from pathlib import Path

total, used, free = shutil.disk_usage(Path(__file__).resolve().parents[2])
gb = 1024 ** 3
print(f"Spazio libero: {free / gb:.1f} GB su {total / gb:.1f} GB ({100 * free / total:.0f}% libero).")
