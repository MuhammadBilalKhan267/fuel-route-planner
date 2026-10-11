"""Download the pinned open-source map renderer; no account or API key needed."""

from pathlib import Path
from urllib.request import Request, urlopen

DESTINATION = Path(__file__).resolve().parent.parent / "static" / "vendor" / "leaflet"
SOURCES = {
    "leaflet.js": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "leaflet.css": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
    "LICENSE": "https://raw.githubusercontent.com/Leaflet/Leaflet/v1.9.4/LICENSE",
}

if __name__ == "__main__":
    DESTINATION.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCES.items():
        with urlopen(
            Request(url, headers={"User-Agent": "FuelRouteAssessment"}), timeout=30
        ) as response:
            content = response.read()
        (DESTINATION / name).write_bytes(content)
        print(f"Saved {name} ({len(content)} bytes)")
