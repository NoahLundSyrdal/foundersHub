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

# These existing files own credentials, authority, persistence, purchasing
# execution, or the improvement mechanism itself. An owner can change them in a
# separate manual PR; the hosted feature worker cannot propose edits here.
PROTECTED_FILES = frozenset({
    *("apps/agent/office/" + name for name in (
        "agent.py", "config.py", "db.py", "deps.py", "development.py", "development_api.py",
        "dispatch.py", "integrations.py", "team_gateway.py", "connect.py", "whatsapp_delivery.py",
        "order_confirmations.py", "oda_api.py", "oda_food.py", "procurement_workflow.py",
        "requests_api.py", "work_api.py", "work_bridge.py", "work_service.py", "plugins/procurement.py",
        "approval_email.py", "speaker.py", "voice.py", "chat_api.py",
    )),
    *("apps/procurement/office_procurement/" + name for name in (
        "config.py", "oda.py", "planning.py", "serialization.py",
    )),
    "apps/agent/tests/test_development.py", "apps/agent/tests/test_shared_connection.py",
})
SENSITIVE_COMPONENT = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:auth|authentication|authorization|credential|credentials|secret|secrets|"
    r"migration|migrations|deploy|deployment|release)(?:[^A-Za-z0-9]|$)", re.I
)


def git(*args, **kwargs):
    return subprocess.run(["git", *args], check=True, capture_output=True, **kwargs).stdout


def api(path, *, method="GET", body=None):
    request = Request(
        "https://api.github.com/" + path,
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"},
        method=method, data=json.dumps(body).encode() if body is not None else None,
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def allowed_path(name):
    # Ordinary application features are eligible, including API registration and
    # conversation tools. This is a file-scope control, not proof that generated
    # Python/TypeScript is semantically safe. The owner explicitly chooses either
    # a review-only draft or checked automatic merge when authorizing the request.
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", name, flags=re.ASCII):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {"", ".", ".."} or p.startswith(".") for p in name.split("/")):
        return False
    if name in PROTECTED_FILES or path.name == "conftest.py":
        return False
    if any(SENSITIVE_COMPONENT.search(PurePosixPath(part).stem) for part in path.parts):
        return False
    if name.startswith("apps/web/src/"):
        return path.suffix in {".ts", ".tsx", ".css", ".svg", ".json"}
    if name.startswith(("apps/agent/office/", "apps/agent/tests/",
                        "apps/procurement/office_procurement/", "apps/procurement/tests/")):
        return path.suffix == ".py"
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


def parse_auto_merge(value=False):
    # github.event.inputs serializes workflow_dispatch booleans as strings;
    # accept native booleans too, but never truthy arbitrary strings/numbers.
    if type(value) is bool:
        return value
    if type(value) is str and value in {"true", "false"}:
        return value == "true"
    raise ValueError("auto_merge must be the boolean true or false")


def candidate_branch():
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    if not run_id.isdigit() or not attempt.isdigit():
        raise ValueError("Unexpected workflow run identifier")
    return f"NLS/claude-{run_id}-{attempt}"


def set_outputs(**values):
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


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
    auto_merge = parse_auto_merge(inputs.get("auto_merge", False))
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
    set_outputs(base=base, request_id=request_id, auto_merge=str(auto_merge).lower())


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


def publish(base, patch_file, request_id, auto_merge=False, tree_sha=None):
    validate_id(request_id)
    auto_merge = parse_auto_merge(auto_merge)
    apply(base, patch_file)
    if not SHA.fullmatch(tree_sha or "") or git("write-tree").decode().strip() != tree_sha:
        raise ValueError("Candidate tree differs from the successfully validated tree")
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = api(f"repos/{repo}")["default_branch"]
    # The tested diff must still be based on current main. No rebasing/merging
    # untested changes or retrying a stale request automatically.
    from urllib.parse import quote
    if api(f"repos/{repo}/commits/{quote(branch, safe='')}")["sha"] != base:
        raise ValueError("Default branch changed during implementation; submit a fresh request")
    run_id = os.environ["GITHUB_RUN_ID"]
    candidate = candidate_branch()
    git("config", "user.name", "Claude development workflow")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("checkout", "-b", candidate)
    git("commit", "-m", f"Implement development request {request_id}")
    head_sha = git("rev-parse", "HEAD").decode().strip()
    if not SHA.fullmatch(head_sha):
        raise ValueError("Candidate commit has an unexpected identifier")
    subprocess.run(["gh", "auth", "setup-git"], check=True, capture_output=True)
    git("push", "origin", "HEAD:refs/heads/" + candidate)
    run_url = f"https://github.com/{repo}/actions/runs/{run_id}"
    body = (
        f"Implements development request `{request_id}` using hosted Claude.\n\n"
        f"Validation: scope guard, npm run check, npm run build, npm test, and procurement tests passed in [{run_id}]({run_url}).\n\n"
        "Eligible scope includes ordinary Python backend feature modules/routes and tests, procurement application code/tests, "
        "frontend source/API client and documentation. Direct edits to credential, authority, purchasing approval/execution, "
        "database core, migration, dependency, shared-contract, hardware and release-control paths are blocked.\n\n"
        "The file-scope guard and passing tests do not prove semantic safety. "
        + ("The operator explicitly authorized checked automatic merge for this request. A separate trusted job may merge only "
           "this exact tested head with a non-forced fast-forward, if the base remains unchanged and GitHub repository protections permit it. "
           if auto_merge else
           "Review the exact diff, especially API registration and conversation tool changes. This request is review-only; "
           "owner PR approval/merge remains manual. ")
        + "No local release is performed by this workflow. "
        "The local demo updater accepts only an explicitly selected, checked main revision after merge.\n"
    )
    body_file = Path(os.environ["RUNNER_TEMP"]) / "development-pr-body.md"
    body_file.write_text(body)
    command = ["gh", "pr", "create", "--repo", repo, "--base", branch, "--head", candidate,
               "--title", f"Claude development {request_id}", "--body-file", str(body_file)]
    if not auto_merge:
        command.append("--draft")
    result = subprocess.run(
        command,
        check=True, capture_output=True, text=True,
    )
    url = result.stdout.strip()
    match = re.fullmatch(re.escape(f"https://github.com/{repo}/pull/") + r"([1-9][0-9]*)", url)
    if not match:
        raise ValueError("Pull request returned an unexpected URL")
    number = match.group(1)
    verify_pr(api(f"repos/{repo}/pulls/{number}"), repo, branch, base, head_sha, request_id, number,
              require_ready=auto_merge)
    # These values come only from trusted orchestration after publication, not
    # model output or candidate code. The merge job consumes them on a fresh VM.
    set_outputs(pr_number=number, head_sha=head_sha)
    print(url)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(f"Development request `{request_id}`: [view the {'ready' if auto_merge else 'draft'} PR]({url}). "
                      f"{'Checked automatic merge was requested.' if auto_merge else 'Manual owner review/merge is required.'} "
                      "No local release was performed.\n")


def verify_pr(pr, repo, branch, base, head_sha, request_id, number, *, require_ready):
    if (pr.get("number") != int(number) or pr.get("state") != "open" or pr.get("merged_at")
            or (require_ready and pr.get("draft") is not False)
            or pr.get("title") != f"Claude development {request_id}"
            or pr.get("user", {}).get("login") != "github-actions[bot]"
            or pr.get("head", {}).get("ref") != candidate_branch()
            or pr.get("head", {}).get("sha") != head_sha
            or pr.get("head", {}).get("repo", {}).get("full_name") != repo
            or pr.get("base", {}).get("ref") != branch
            or pr.get("base", {}).get("sha") != base
            or pr.get("base", {}).get("repo", {}).get("full_name") != repo):
        raise ValueError("PR identity, tested head or expected base no longer matches this request")


def merge(base, head_sha, number, request_id, auto_merge=False, tree_sha=None):
    if not parse_auto_merge(auto_merge):
        raise ValueError("This request did not authorize automatic merge")
    validate_id(request_id)
    validate_base(base)
    if (not SHA.fullmatch(head_sha or "") or not SHA.fullmatch(tree_sha or "")
            or not re.fullmatch(r"[1-9][0-9]*", number or "")):
        raise ValueError("Unexpected merge candidate identifier")
    if os.environ.get("VALIDATION_RESULT") != "success":
        raise ValueError("Candidate validation did not succeed")
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    candidate_branch()  # validates run identifiers before using them in URLs
    jobs = api(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100").get("jobs", [])
    validations = [job for job in jobs if job.get("name") == "Validate candidate"]
    if (len(validations) != 1 or validations[0].get("status") != "completed"
            or validations[0].get("conclusion") != "success"):
        raise ValueError("This workflow attempt has no successful candidate validation job")
    branch = api(f"repos/{repo}")["default_branch"]
    pr = api(f"repos/{repo}/pulls/{number}")
    verify_pr(pr, repo, branch, base, head_sha, request_id, number, require_ready=True)
    head = api(f"repos/{repo}/commits/{head_sha}")
    if ([parent.get("sha") for parent in head.get("parents", [])] != [base]
            or head.get("commit", {}).get("tree", {}).get("sha") != tree_sha):
        raise ValueError("Candidate ancestry or tree differs from the successfully validated candidate")
    from urllib.parse import quote
    if api(f"repos/{repo}/commits/{quote(branch, safe='')}")["sha"] != base:
        raise ValueError("Default branch advanced; automatic merge was refused")
    # GITHUB_TOKEN-created pushes do not trigger another Actions workflow.
    # Attest the checks actually completed above on the exact installed SHA so
    # the local updater can verify it. No self-review or fabricated test result.
    api(f"repos/{repo}/check-runs", method="POST", body={
        "name": "Office checks", "head_sha": head_sha, "status": "completed", "conclusion": "success",
        "details_url": f"https://github.com/{repo}/actions/runs/{run_id}/attempts/{attempt}",
        "external_id": f"cloud-development:{run_id}:{attempt}:{head_sha}",
        "output": {"title": "Hosted Claude candidate checks passed",
                   "summary": "Scope guard, type checks, frontend build, backend/device/procurement tests and unchanged shared-schema checks passed for this exact tree in the linked workflow attempt."},
    })
    # A sole-parent candidate is a direct child of the expected base. A
    # force:false update therefore rejects *atomically* if main advanced onto
    # a different commit, preserving the exact tested revision. GitHub applies
    # its repository protections; never bypass them or retry uncertain writes.
    try:
        result = api(f"repos/{repo}/git/refs/heads/{quote(branch, safe='/')}", method="PATCH",
                     body={"sha": head_sha, "force": False})
    except Exception:
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(f"Automatic merge for PR #{number} has no confirmed result. Inspect GitHub before any retry. "
                          "This workflow will not retry or release locally.\n")
        raise ValueError("Merge result was not confirmed. Reconcile GitHub; do not retry automatically") from None
    if result.get("object", {}).get("sha") != head_sha:
        raise ValueError("GitHub did not confirm the exact checked revision; inspect GitHub before any retry")
    set_outputs(merge_sha=head_sha)
    # GitHub marks a PR merged when its head is pushed into the default branch.
    # These are bounded reconciliation reads only; never repeat the ref update.
    merged_pr = None
    import time
    for attempt_number in range(3):
        merged_pr = api(f"repos/{repo}/pulls/{number}")
        if merged_pr.get("merged") is True and merged_pr.get("merged_at"):
            break
        if attempt_number < 2:
            time.sleep(1)
    if not merged_pr or merged_pr.get("merged") is not True or not merged_pr.get("merged_at"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(f"Main was updated to checked revision `{head_sha}`. GitHub has not yet confirmed PR #{number} as merged. "
                          "Inspect GitHub; no retry or local release was performed.\n")
        raise ValueError("Exact main update succeeded but PR merge status needs reconciliation")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(f"Development request `{request_id}`: PR #{number} merged by non-forced fast-forward to `{head_sha}` with successful Office checks on that exact revision. "
                      "No local release was performed.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "collect", "apply", "publish", "merge"])
    parser.add_argument("--base")
    parser.add_argument("--patch")
    parser.add_argument("--request-id")
    parser.add_argument("--auto-merge", default="false")
    parser.add_argument("--head-sha")
    parser.add_argument("--pr-number")
    parser.add_argument("--tree-sha")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "collect":
        collect(args.base, args.patch)
    elif args.command == "apply":
        apply(args.base, args.patch)
    elif args.command == "publish":
        publish(args.base, args.patch, args.request_id, args.auto_merge, args.tree_sha)
    else:
        merge(args.base, args.head_sha, args.pr_number, args.request_id, args.auto_merge, args.tree_sha)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not echo GitHub response bodies, commands, requests or credentials.
        print(f"Cloud development stopped: {exc if isinstance(exc, ValueError) else type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
