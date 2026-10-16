"""Strumento predefinito: temperatura del processore (Raspberry Pi e Linux)."""
from pathlib import Path

zones = sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
values = []
for z in zones:
    try:
        values.append(int(z.read_text().strip()) / 1000)
    except (OSError, ValueError):
        pass
if values:
    print(f"Temperatura del processore: {max(values):.1f} °C.")
else:
    print("Temperatura del processore non disponibile su questo computer.")
