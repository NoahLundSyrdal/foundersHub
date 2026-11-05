import json
from datetime import datetime, timezone


def normalize(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def proposal_fingerprint(proposal: dict) -> str:
    """Match the browser's JSON.stringify for the persisted proposal snapshot."""
    return json.dumps(normalize(proposal), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def epoch(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timezone required")
    return parsed.timestamp()
