#!/usr/bin/env python3
"""Owner-approved local releases. Claude's coding worker never runs here."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

REPOSITORY = "NoahLundSyrdal/foundersHub"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REQUIRED_CHECK = "Office checks"


class UpdateError(Exception):
    """An update was refused; the existing app remains the selected release."""


def command(args: list[str], root: Path, *, binary: bool = False):
    """Never echo subprocess output: tools may include credentials in diagnostics."""
    result = subprocess.run(args, cwd=root, capture_output=True, check=False)
    if result.returncode:
        raise UpdateError(f"{args[0]} failed (exit {result.returncode}); current release was not changed")
    return result.stdout if binary else result.stdout.decode("utf-8").strip()


def github(path: str, root: Path):
    return json.loads(command(["gh", "api", path], root))


def clean(root: Path) -> None:
    if command(["git", "status", "--porcelain", "--untracked-files=normal"], root):
        raise UpdateError("Source checkout has local changes; commit or stash them before managing demo releases")


def require_sha(value: str) -> str:
    if not SHA.fullmatch(value):
        raise UpdateError("Approval must name the full 40-character commit SHA")
    return value


def verify_checks(root: Path, sha: str, check_name: str = REQUIRED_CHECK) -> None:
    """Require a successful latest GitHub Actions check on the exact release commit."""
    latest: dict[str, dict] = {}
    page = 1
    while True:
        response = github(f"repos/{REPOSITORY}/commits/{sha}/check-runs?per_page=100&page={page}", root)
        checks = response.get("check_runs", [])
        for check in checks:
            # GitHub Actions' application id, not an arbitrary same-named third-party status.
            if check.get("head_sha") != sha or check.get("app", {}).get("id") != 15368:
                continue
            name = check.get("name", "")
            if check.get("id", 0) > latest.get(name, {}).get("id", -1):
                latest[name] = check
        if len(checks) < 100:
            break
        page += 1
        if page > 20:
            raise UpdateError("Too many check results to safely verify this revision")
    required = latest.get(check_name)
    if not required or required.get("status") != "completed" or required.get("conclusion") != "success":
        raise UpdateError(f"Exact revision has no successful completed {check_name!r} GitHub Actions check")
    if any(c.get("status") != "completed" or c.get("conclusion") not in {"success", "skipped", "neutral"}
           for c in latest.values()):
        raise UpdateError("Exact revision has a pending or failing GitHub Actions check")


def verify_pr(root: Path, number: int, approved_sha: str) -> dict:
    pr = github(f"repos/{REPOSITORY}/pulls/{number}", root)
    if not pr.get("merged") or pr.get("base", {}).get("ref") != "main":
        raise UpdateError("Only a pull request already merged into main can be installed")
    if pr.get("base", {}).get("repo", {}).get("full_name", "").lower() != REPOSITORY.lower():
        raise UpdateError("Pull request targets a different repository")
    if pr.get("merge_commit_sha") != approved_sha:
        raise UpdateError("Approved SHA differs from the pull request's merged revision; review it again")
    return pr


def safe_changes(paths: list[str]) -> None:
    """Keep release controls, spending/identity policy, migrations and dependencies manual."""
    forbidden = []
    for path in paths:
        p = PurePosixPath(path)
        parts = {part.lower() for part in p.parts}
        if (path.startswith((".github/", ".claude/", "scripts/", "config/", "packages/contracts/", "apps/device/"))
            or p.name in {"AGENTS.md", "CLAUDE.md", "package.json", "package-lock.json", "pyproject.toml", "uv.lock", ".env.example"}
            or p.name in {".gitmodules", ".gitattributes", ".npmrc", ".yarnrc", ".yarnrc.yml"}
            or p.name in {"db.py", "config.py", "dispatch.py", "approvals.py", "procurement_workflow.py"}
            or "migration" in p.name.lower() or parts.intersection({"migrations", "schema", "schemas"})
            or p.suffix.lower() in {".sql", ".sqlite", ".sqlite3"}):
            forbidden.append(path)
    if forbidden:
        raise UpdateError("This change needs manual installation because it touches release controls, dependencies, hardware, policy or database files: "
                          + ", ".join(forbidden[:8]))


def state_dir(root: Path) -> Path:
    path = root / ".data" / "local-demo"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"No valid {path.name}; initialize an explicitly approved release first") from exc


@contextlib.contextmanager
def lock(path: Path):
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("Another local demo operation is already running") from exc
        yield


def fetch(root: Path, sha: str) -> None:
    # Fixed repository URL: never reuse an origin URL with embedded credentials or another fork.
    command(["git", "fetch", "--no-tags", f"https://github.com/{REPOSITORY}.git", "main"], root)
    if command(["git", "merge-base", "--is-ancestor", sha, "FETCH_HEAD"], root) != "":
        raise UpdateError("Revision is not reachable from this repository's main branch")


def verify_release(root: Path, release: dict) -> Path:
    sha = require_sha(release.get("sha", ""))
    expected = root if release.get("bootstrap") else state_dir(root) / "releases" / sha
    if Path(release.get("path", "")).resolve() != expected.resolve() or not expected.is_dir():
        raise UpdateError("Approved release directory is missing or invalid")
    if release.get("bootstrap"):
        clean(root)
        if command(["git", "rev-parse", "HEAD"], root) != sha:
            raise UpdateError("Bootstrap checkout no longer matches its approved revision")
    elif (expected / ".approved-sha").read_text(encoding="utf-8").strip() != sha:
        raise UpdateError("Release directory does not match its approved revision")
    else:
        if not (expected / ".data").is_symlink() or (expected / ".data").resolve() != (root / ".data").resolve():
            raise UpdateError("Release runtime data link no longer points at the original demo state")
        if (root / ".env").exists() and (not (expected / ".env").is_symlink()
                                         or (expected / ".env").resolve() != (root / ".env").resolve()):
            raise UpdateError("Release environment link no longer points at the original demo settings")
        verify_source(root, expected, sha)
    return expected


def verify_source(root: Path, release: Path, sha: str) -> None:
    """Install hooks or later local edits cannot silently replace approved source files."""
    entries = command(["git", "ls-tree", "-rz", sha], root, binary=True).split(b"\0")
    tracked = set()
    for entry in entries:
        if not entry:
            continue
        meta, raw_path = entry.split(b"\t", 1)
        mode, kind, expected_hash = meta.decode().split()
        path = raw_path.decode("utf-8")
        tracked.add(path)
        location = release / path
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise UpdateError("Submodules or unsupported file modes require manual installation")
        if mode == "120000":
            if not location.is_symlink():
                raise UpdateError("Approved source symlink was replaced")
            data = os.readlink(location).encode("utf-8")
        else:
            if location.is_symlink() or not location.is_file():
                raise UpdateError("Approved source file was replaced or removed")
            data = location.read_bytes()
            if bool(location.stat().st_mode & 0o111) != (mode == "100755"):
                raise UpdateError("Approved source executable permission was changed")
        actual_hash = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if actual_hash != expected_hash:
            raise UpdateError("Release source differs from its approved Git revision")
    generated = {"node_modules", ".venv", "__pycache__", "dist", ".pytest_cache", ".data"}
    for folder, directories, files in os.walk(release, followlinks=False):
        for name in directories:
            location = Path(folder) / name
            relative = location.relative_to(release).as_posix()
            if location.is_symlink() and relative not in tracked and relative != ".data":
                raise UpdateError("Release contains an unexpected source directory link")
        directories[:] = [name for name in directories if name not in generated]
        for filename in files:
            relative = (Path(folder) / filename).relative_to(release).as_posix()
            if relative not in tracked and relative not in {".env", ".approved-sha"}:
                raise UpdateError("Release contains an unexpected source file; prepare a fresh candidate")


def init(root: Path, approved_sha: str, check_name: str = REQUIRED_CHECK) -> None:
    require_sha(approved_sha)
    clean(root)
    if command(["git", "rev-parse", "HEAD"], root) != approved_sha:
        raise UpdateError("Checked-out code differs from the SHA you approved")
    fetch(root, approved_sha)
    verify_checks(root, approved_sha, check_name)
    path = state_dir(root) / "release.json"
    if path.exists():
        raise UpdateError("A release is already approved; use update instead of replacing it")
    write_json(path, {"sha": approved_sha, "path": str(root), "bootstrap": True, "approved_at": int(time.time())})


def prepare(root: Path, sha: str) -> Path:
    release = state_dir(root) / "releases" / sha
    if release.exists():
        # A failed or manually modified candidate is never silently reused.
        raise UpdateError("This candidate directory already exists; inspect and remove it before retrying")
    release.mkdir(parents=True)
    archive = command(["git", "archive", "--format=tar", sha], root, binary=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            tar.extractall(release, filter="data")
        if (root / ".env").exists():
            (release / ".env").symlink_to(root / ".env")
        (release / ".data").symlink_to(root / ".data", target_is_directory=True)
        # Dependencies can execute install scripts; this happens only after exact SHA approval.
        command(["npm", "ci"], release)
        command(["uv", "sync", "--frozen", "--project", "apps/agent", "--group", "dev"], release)
        command(["npm", "run", "build"], release)
        verify_source(root, release, sha)
        (release / ".approved-sha").write_text(sha + "\n", encoding="utf-8")
        return release
    except Exception:
        # Keep failed candidates for inspection; never touch the active release or runtime data.
        raise


def update(root: Path, pr_number: int, approved_sha: str, check_name: str = REQUIRED_CHECK) -> None:
    require_sha(approved_sha)
    clean(root)
    directory = state_dir(root)
    with lock(directory / "update.lock"):
        previous = read_json(directory / "release.json")
        verify_release(root, previous)
        if previous["sha"] == approved_sha:
            raise UpdateError("This exact revision is already selected")
        verify_pr(root, pr_number, approved_sha)
        fetch(root, approved_sha)
        command(["git", "merge-base", "--is-ancestor", previous["sha"], approved_sha], root)
        verify_checks(root, approved_sha, check_name)
        changes = command(["git", "diff", "--name-only", previous["sha"], approved_sha], root).splitlines()
        safe_changes(changes)
        release = prepare(root, approved_sha)
        # Recheck after preparation so a rerun/failure is not ignored while npm/uv were working.
        verify_pr(root, pr_number, approved_sha)
        verify_checks(root, approved_sha, check_name)
        if read_json(directory / "release.json") != previous:
            raise UpdateError("Selected release changed during preparation; review again")
        write_json(directory / "release.json", {
            "sha": approved_sha, "path": str(release), "pr": pr_number,
            "approved_at": int(time.time()), "previous": previous,
        })


def probe(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            if response.status != 200:
                return False
            if url.endswith("/api/health"):
                return json.load(response).get("ok") is True
            return True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def stop(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def require_free_ports() -> None:
    for port in (3001, 5173):
        with socket.socket() as connection:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                connection.bind(("127.0.0.1", port))
            except OSError as exc:
                raise UpdateError("Demo ports are already in use; stop the existing app before starting the supervisor") from exc


def start(root: Path, release: dict) -> list[subprocess.Popen]:
    location = verify_release(root, release)
    require_free_ports()
    env = os.environ.copy()
    env["OFFICE_API_URL"] = "http://127.0.0.1:3001"
    # .env and every default .data path remain shared; explicit absolute DB paths stay unchanged.
    commands = [
        (["uv", "run", "--frozen", "uvicorn", "office.api:create_app", "--factory", "--host", "127.0.0.1", "--port", "3001", "--timeout-graceful-shutdown", "2"], location / "apps/agent"),
        (["npm", "run", "dev", "-w", "@office/web", "--", "--host", "127.0.0.1", "--port", "5173", "--strictPort"], location),
    ]
    processes = []
    try:
        for args, cwd in commands:
            processes.append(subprocess.Popen(args, cwd=cwd, env=env, start_new_session=True,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        deadline = time.monotonic() + 60
        healthy = 0
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in processes):
                break
            if probe("http://127.0.0.1:3001/api/health") and probe("http://127.0.0.1:5173/"):
                healthy += 1
                if healthy >= 3:
                    return processes
            else:
                healthy = 0
            time.sleep(0.5)
    except Exception:
        stop(processes)
        raise
    stop(processes)
    raise UpdateError("Candidate app did not become healthy on loopback within 60 seconds")


def activate(root: Path, candidate: dict, current: dict | None, processes: list[subprocess.Popen], starter=start):
    """On failed candidate health, restore the prior code selection, never the database."""
    stop(processes)
    directory = state_dir(root)
    try:
        fresh = starter(root, candidate)
    except (UpdateError, OSError):
        if current is None:
            raise UpdateError("Initial release failed health; no previous running release is available")
        write_json(directory / "release.json", current)
        write_json(directory / "status.json", {"state": "rollback", "active_sha": current["sha"],
                                                "failed_sha": candidate["sha"], "database_restored": False})
        restored = starter(root, current)
        return current, restored
    write_json(directory / "status.json", {"state": "running", "active_sha": candidate["sha"], "pr": candidate.get("pr")})
    return candidate, fresh


def run(root: Path) -> None:
    directory = state_dir(root)
    with lock(directory / "run.lock"):
        read_json(directory / "release.json")
        require_free_ports()
        current = None
        processes: list[subprocess.Popen] = []
        original_term = signal.getsignal(signal.SIGTERM)
        def terminate(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, terminate)
        try:
            while True:
                candidate = read_json(directory / "release.json")
                if current is None or current["sha"] != candidate["sha"]:
                    current, processes = activate(root, candidate, current, processes)
                    print(f"Local demo ready: {current['sha'][:12]} at http://127.0.0.1:5173", flush=True)
                if any(p.poll() is not None for p in processes):
                    raise UpdateError("A demo process stopped; inspect the app manually before restarting")
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            stop(processes)
            signal.signal(signal.SIGTERM, original_term)
            write_json(directory / "status.json", {"state": "stopped", "active_sha": current["sha"] if current else None})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    subs = parser.add_subparsers(dest="operation", required=True)
    for name in ("init", "update"):
        child = subs.add_parser(name)
        child.add_argument("--approve-sha", required=True, help="Full reviewed main/merge SHA; this is release authorization")
        child.add_argument("--require-check", default=REQUIRED_CHECK)
        if name == "update":
            child.add_argument("--pr", required=True, type=int)
    subs.add_parser("run")
    subs.add_parser("status")
    args = parser.parse_args()
    root = args.repo.resolve()
    try:
        if args.operation == "init":
            init(root, args.approve_sha, args.require_check)
            print("Initial revision approved. Run this script with 'run' to start the demo.")
        elif args.operation == "update":
            if args.pr < 1:
                raise UpdateError("Pull request number must be positive")
            update(root, args.pr, args.approve_sha, args.require_check)
            print("Approved revision prepared. A running supervisor will restart and verify it; otherwise use 'run'.")
        elif args.operation == "run":
            run(root)
        else:
            directory = state_dir(root)
            print(json.dumps({"selected": read_json(directory / "release.json"),
                              "runtime": read_json(directory / "status.json") if (directory / "status.json").exists() else {"state": "not_started"}}, indent=2))
        return 0
    except (UpdateError, OSError, ValueError) as exc:
        # Do not include OS exception details containing arbitrary subprocess/file content.
        print(f"Local demo: {exc if isinstance(exc, UpdateError) else type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
