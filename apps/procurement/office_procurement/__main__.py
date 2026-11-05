import asyncio
import json
import sys

from .models import WorkerInput
from .worker import run_worker


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(128_001)
        if len(raw) > 128_000:
            raise ValueError("Input too large")
        request = WorkerInput.model_validate_json(raw)
        result = asyncio.run(run_worker(request))
        sys.stdout.write(result.model_dump_json() + "\n")
        return 0
    except Exception:
        # Provider exception text can contain secrets or user data; emit a fixed error only.
        sys.stdout.write(json.dumps({"error": "Procurement interpretation failed; check worker configuration"}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
