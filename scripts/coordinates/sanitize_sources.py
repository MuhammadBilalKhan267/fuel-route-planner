"""Remove embedded client credentials from saved public station evidence."""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SOURCES = Path(__file__).resolve().parents[2] / "data/coordinate-sources"
CREDENTIAL_PARAMETERS = {
    "key",
    "api_key",
    "apikey",
    "api-key",
    "token",
    "access_token",
    "access-token",
    "subscription-key",
    "clientid",
    "client_id",
    "client-id",
    "client_secret",
    "client-secret",
}
URL = re.compile(r"https?://[^\s\"'<>]+")
CLIENT_KEY = re.compile(
    r"(?P<prefix>[\"']?(?:x-api-key|api[_-]?key|access[_-]?token|maps_api_key|"
    r"mapboxAccessToken|mapboxKey|google_api_key)[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>[^\"'\r\n]{12,})(?P=quote)",
    re.IGNORECASE,
)
KNOWN_PUBLIC_KEY = re.compile(r"AIza[\w-]{25,}|pk\.eyJ[\w.-]{40,}")


def sanitized_url(value):
    parsed = urlsplit(value)
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    retained = [(key, item) for key, item in parameters if key.lower() not in CREDENTIAL_PARAMETERS]
    if len(parameters) == len(retained):
        return value
    return urlunsplit(parsed._replace(query=urlencode(retained)))


def sanitized_text(value):
    value = URL.sub(lambda match: sanitized_url(match.group()), value)
    value = CLIENT_KEY.sub(
        lambda match: match["prefix"] + match["quote"] + "REDACTED" + match["quote"],
        value,
    )
    return KNOWN_PUBLIC_KEY.sub("REDACTED", value)


def sanitized_record(value):
    if isinstance(value, dict):
        return {key: sanitized_record(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitized_record(item) for item in value]
    return sanitized_text(value) if isinstance(value, str) else value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Atomically replace affected caches.")
    args = parser.parse_args()
    changed = []
    for path in sorted(SOURCES.rglob("*")):
        if not path.is_file() or path.suffix not in {
            ".json",
            ".html",
            ".js",
            ".py",
            ".txt",
            ".csv",
        }:
            continue
        original = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            record = json.loads(original)
            cleaned = sanitized_record(record)
            if cleaned == record:
                continue
            output = json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n"
        else:
            output = sanitized_text(original)
            if output == original:
                continue
        changed.append(str(path.relative_to(SOURCES)))
        if args.apply:
            temporary = path.with_suffix(path.suffix + ".sanitizing")
            temporary.write_text(output, encoding="utf-8")
            temporary.replace(path)
    print({"applied": args.apply, "affected_files": changed})


if __name__ == "__main__":
    main()
