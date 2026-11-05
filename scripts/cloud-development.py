#!/usr/bin/env python3
"""Trusted orchestration for the hosted Claude demo. Never executes candidate code."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.request import Request, urlopen


MAX_PATCH_BYTES = 512_000
MAX_FILES = 25
REQUEST_FILE = ".cloud-development-request.json"
ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z", re.ASCII)
SHA = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
SECRET = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{12,}|(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}"
    r"|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"
)


def git(*args, **kwargs):
    return subprocess.run(["git", *args], check=True, capture_output=True, **kwargs).stdout


def api(path):
    request = Request(
        "https://api.github.com/" + path,
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def allowed_path(name):
    # Deliberately narrow first-demo scope. API/purchasing/auth/release controls
    # and dependency manifests cannot be changed by this automated workflow.
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", name, flags=re.ASCII):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {"", ".", ".."} or p.startswith(".") for p in name.split("/")):
        return False
    if name.startswith("apps/web/src/"):
        return name != "apps/web/src/api.ts" and path.suffix in {".ts", ".tsx", ".css", ".svg", ".json"}
    return name.startswith("docs/") and path.suffix in {".md", ".txt"}


def validate_index():
    paths = git("diff", "--cached", "--name-only", "-z").decode("utf-8").split("\0")[:-1]
    if not paths or len(paths) > MAX_FILES:
        raise ValueError("Candidate must change between 1 and 25 files")
    for name in paths:
        if not allowed_path(name):
            raise ValueError("Candidate touches a protected or unsupported path")
        entries = git("ls-files", "--stage", "-z", "--", name).split(b"\0")
        for entry in entries:
            if entry and entry.split(b" ", 1)[0] != b"100644":
                raise ValueError("Candidate contains a symlink, executable, submodule or unresolved entry")
        if entries[0]:
            content = git("show", ":" + name)
            if b"\0" in content:
                raise ValueError("Binary candidate content is unsupported")
            text = content.decode("utf-8")
            if SECRET.search(text):
                raise ValueError("Credential-like text is not accepted")
    patch = git("diff", "--cached", "--binary", "--full-index")
    if len(patch) > MAX_PATCH_BYTES:
        raise ValueError("Candidate exceeds the 512 KB demo limit")
    git("diff", "--cached", "--check")
    return patch


def validate_id(value):
    if not ID.fullmatch(value):
        raise ValueError("request_id must contain 1–80 ASCII letters, digits, underscores or hyphens")
    return value


def validate_base(value):
    if not SHA.fullmatch(value):
        raise ValueError("Expected base must be a full lowercase commit SHA")
    if git("rev-parse", "HEAD").decode().strip() != value:
        raise ValueError("Checkout no longer matches the expected base")


def prepare():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise ValueError("Only explicit development dispatches are accepted")
    branch = event["repository"]["default_branch"]
    if os.environ.get("GITHUB_REF") != "refs/heads/" + branch:
        raise ValueError("Development workflow must run from the default branch")
    repo = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Unexpected repository")
    # workflow_dispatch normally requires write access. Check again explicitly,
    # including a different actor who requested a rerun.
    for actor in {os.environ["GITHUB_ACTOR"], os.environ.get("GITHUB_TRIGGERING_ACTOR", os.environ["GITHUB_ACTOR"])}:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", actor):
            raise ValueError("Unexpected actor")
        permission = api(f"repos/{repo}/collaborators/{actor}/permission")["permission"]
        if permission not in {"admin", "maintain", "write"}:
            raise ValueError("Development dispatch requires repository write access")
    inputs = event["inputs"]
    request_id = validate_id(inputs["request_id"])
    request = inputs["request"]
    if not isinstance(request, str) or not 1 <= len(request.strip()) <= 8000 or "\0" in request:
        raise ValueError("Request must contain 1–8000 characters")
    if SECRET.search(request):
        raise ValueError("Do not send credentials in development requests")
    base = inputs.get("expected_base") or git("rev-parse", "HEAD").decode().strip()
    validate_base(base)
    Path(REQUEST_FILE).write_text(json.dumps({"request_id": request_id, "request": request}, ensure_ascii=False))
    exclude_path = Path(git("rev-parse", "--git-path", "info/exclude").decode().strip())
    with exclude_path.open("a") as exclude:
        exclude.write("\n/" + REQUEST_FILE + "\n")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"base={base}\nrequest_id={request_id}\n")


def collect(base, output):
    validate_base(base)
    git("add", "--all")
    Path(output).write_bytes(validate_index())


def apply(base, patch_file):
    validate_base(base)
    if git("status", "--porcelain"):
        raise ValueError("Candidate requires a clean checkout")
    patch = Path(patch_file).read_bytes()
    if not patch or len(patch) > MAX_PATCH_BYTES:
        raise ValueError("Invalid candidate patch size")
    git("apply", "--index", "--whitespace=error-all", "--", str(Path(patch_file).resolve()))
    validate_index()


def publish(base, patch_file, request_id):
    validate_id(request_id)
    apply(base, patch_file)
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = api(f"repos/{repo}")["default_branch"]
    # The tested diff must still be based on current main. No rebasing/merging
    # untested changes or retrying a stale request automatically.
    from urllib.parse import quote
    if api(f"repos/{repo}/commits/{quote(branch, safe='')}")["sha"] != base:
        raise ValueError("Default branch changed during implementation; submit a fresh request")
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    if not run_id.isdigit() or not attempt.isdigit():
        raise ValueError("Unexpected workflow run identifier")
    candidate = f"NLS/claude-{run_id}-{attempt}"
    git("config", "user.name", "Claude development workflow")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("checkout", "-b", candidate)
    git("commit", "-m", f"Implement development request {request_id}")
    subprocess.run(["gh", "auth", "setup-git"], check=True, capture_output=True)
    git("push", "origin", "HEAD:refs/heads/" + candidate)
    run_url = f"https://github.com/{repo}/actions/runs/{run_id}"
    body = (
        f"Implements development request `{request_id}` using hosted Claude.\n\n"
        f"Validation: scope guard, npm run check, npm run build, npm test, and procurement tests passed in [{run_id}]({run_url}).\n\n"
        "Automated scope is limited to frontend source (excluding api.ts) and documentation. "
        "No backend, purchasing policy, dependency, hardware or release controls changed.\n\n"
        "Owner review is required. This workflow never merges or deploys a change. "
        "The local demo updater accepts only an explicitly selected, checked main revision after merge.\n"
    )
    body_file = Path(os.environ["RUNNER_TEMP"]) / "development-pr-body.md"
    body_file.write_text(body)
    result = subprocess.run(
        ["gh", "pr", "create", "--repo", repo, "--base", branch, "--head", candidate,
         "--draft", "--title", f"Claude development {request_id}", "--body-file", str(body_file)],
        check=True, capture_output=True, text=True,
    )
    url = result.stdout.strip()
    if not url.startswith(f"https://github.com/{repo}/pull/"):
        raise ValueError("Pull request returned an unexpected URL")
    print(url)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(f"Development request `{request_id}`: [review the draft PR]({url}). No release was performed.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "collect", "apply", "publish"])
    parser.add_argument("--base")
    parser.add_argument("--patch")
    parser.add_argument("--request-id")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "collect":
        collect(args.base, args.patch)
    elif args.command == "apply":
        apply(args.base, args.patch)
    else:
        publish(args.base, args.patch, args.request_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not echo GitHub response bodies, commands, requests or credentials.
        print(f"Cloud development stopped: {exc if isinstance(exc, ValueError) else type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
