"""Explicit feedback drafts and a remote Claude job. Never executes generated code locally."""
import asyncio
import hashlib
import json
import os
import re
import shutil
from urllib.parse import quote

import httpx

from office import db

REPOSITORY = "NoahLundSyrdal/foundersHub"
WORKFLOW = "claude-development.yml"
REF = "main"
_PRIVATE = re.compile(r"sk-ant-[\w-]+|gh[pousr]_[\w]+|github_pat_[\w]+|-----BEGIN .*PRIVATE KEY-----|(?:api[_ -]?key|password|secret|token)\s*[=:]\s*\S+|authorization:\s*bearer", re.I)


class DevelopmentError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class RemoteError(Exception):
    def __init__(self, *, uncertain=False):
        self.uncertain = uncertain


class GitHub:
    """Use an explicit repo-scoped token, or the operator's existing gh login. No shell."""
    async def request(self, method, path, body=None):
        token = os.getenv("OFFICE_DEVELOPMENT_GITHUB_TOKEN")
        if token:
            try:
                async with httpx.AsyncClient(timeout=25) as client:
                    response = await client.request(method, "https://api.github.com/" + path,
                        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}, json=body)
                if not response.is_success:
                    raise RemoteError(uncertain=response.status_code >= 500)
                return response.json() if response.content else {}
            except (httpx.HTTPError, ValueError):
                raise RemoteError(uncertain=True) from None
        if not shutil.which("gh"):
            raise RemoteError()
        args = ["gh", "api", "--hostname", "github.com", path, "--method", method]
        if body is not None:
            args += ["--input", "-"]
        try:
            process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError:
            raise RemoteError() from None
        try:
            out, err = await asyncio.wait_for(process.communicate(json.dumps(body).encode() if body is not None else None), 30)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise RemoteError(uncertain=True) from None
        if process.returncode:
            # A known rejection did not dispatch. All other errors require reconciliation.
            rejected = re.search(rb"HTTP (400|401|403|404|422)\b", err) is not None
            raise RemoteError(uncertain=not rejected)
        try:
            return json.loads(out) if out.strip() else {}
        except ValueError:
            raise RemoteError(uncertain=True) from None


class Development:
    def __init__(self, conn, *, github=None, enabled=None, recover_interrupted=True):
        self.conn = conn
        self.github = github or GitHub()
        self.enabled = (os.getenv("OFFICE_DEVELOPMENT_ENABLED", "false").lower() == "true") if enabled is None else enabled
        self.lock = asyncio.Lock()
        conn.execute("""CREATE TABLE IF NOT EXISTS development_requests(
            id TEXT PRIMARY KEY, text TEXT NOT NULL, fingerprint TEXT NOT NULL,
            state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            expected_base TEXT, run_id INTEGER, run_url TEXT, pr_url TEXT, pr_number INTEGER,
            merge_sha TEXT, error TEXT)""")
        if "auto_merge" not in {row[1] for row in conn.execute("PRAGMA table_info(development_requests)")}:
            conn.execute("ALTER TABLE development_requests ADD COLUMN auto_merge INTEGER NOT NULL DEFAULT 0")
        if recover_interrupted:
            conn.execute("UPDATE development_requests SET state='uncertain', error=? WHERE state='dispatching'",
                         ("Dispatch was interrupted. Refresh to find the existing cloud run; do not submit it again.",))

    def _enabled(self):
        if not self.enabled:
            raise DevelopmentError("Cloud development is disabled. Follow docs/self-improvement.md to enable it.", 503)

    def get(self, request_id):
        item = db.row(self.conn, "SELECT * FROM development_requests WHERE id=?", request_id)
        if not item:
            raise DevelopmentError("Development request not found", 404)
        return self._public(item)

    @staticmethod
    def _public(item):
        return item | {"auto_merge": bool(item["auto_merge"]),
                       "merge_fingerprint": hashlib.sha256(("merge-after-checks:" + item["fingerprint"]).encode()).hexdigest()}

    def snapshot(self):
        return {"enabled": self.enabled, "repository": REPOSITORY, "workflow": WORKFLOW,
                "requests": [self._public(item) for item in db.rows(self.conn, "SELECT * FROM development_requests ORDER BY rowid DESC LIMIT 100")]}

    def _update(self, request_id, **fields):
        fields["updated_at"] = db.now()
        self.conn.execute("UPDATE development_requests SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                          (*fields.values(), request_id))
        return self.get(request_id)

    def draft(self, request_id, text):
        self._enabled()
        text = text.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id) or not 1 <= len(text) <= 8000 or "\0" in text:
            raise DevelopmentError("Provide a request ID and 1–8000 characters of feedback", 400)
        if _PRIVATE.search(text):
            raise DevelopmentError("Remove credentials or secret values before saving feedback", 400)
        fingerprint = hashlib.sha256(json.dumps([REPOSITORY, WORKFLOW, REF, request_id, text], ensure_ascii=False).encode()).hexdigest()
        previous = db.row(self.conn, "SELECT * FROM development_requests WHERE id=?", request_id)
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise DevelopmentError("This request ID already belongs to different feedback")
            return self._public(previous)
        self.conn.execute("""INSERT INTO development_requests(id,text,fingerprint,state,created_at,updated_at)
            VALUES(?,?,?,'draft',?,?)""", (request_id, text, fingerprint, db.now(), db.now()))
        return self.get(request_id)

    async def send(self, request_id, fingerprint, auto_merge=False):
        self._enabled()
        async with self.lock:
            item = self.get(request_id)
            if type(auto_merge) is not bool:
                raise DevelopmentError("Choose whether this exact request may merge after checks", 400)
            expected_fingerprint = item["merge_fingerprint"] if auto_merge else item["fingerprint"]
            if expected_fingerprint != fingerprint:
                raise DevelopmentError("Feedback changed. Review the saved request before sending")
            if item["state"] not in {"draft", "failed_to_dispatch"}:
                if item["auto_merge"] != auto_merge:
                    raise DevelopmentError("Merge permission cannot be changed after dispatch")
                # Retries, including after lost responses, return the durable result.
                return item
            active = db.row(self.conn, "SELECT id FROM development_requests WHERE state IN ('dispatching','submitted','queued','in_progress','uncertain') LIMIT 1")
            if active:
                raise DevelopmentError("A cloud request is already active. Refresh its result before starting another")
            try:
                base = await self.github.request("GET", f"repos/{REPOSITORY}/commits/{REF}")
                sha = base["sha"]
                if not re.fullmatch(r"[0-9a-f]{40}", sha):
                    raise ValueError
            except (RemoteError, KeyError, ValueError):
                return self._update(request_id, state="failed_to_dispatch", error="Could not read GitHub main. Check repository access; nothing was dispatched.")
            self._update(request_id, state="dispatching", expected_base=sha, auto_merge=int(auto_merge), error=None)
            try:
                await self.github.request("POST", f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/dispatches",
                    {"ref": REF, "inputs": {"request_id": request_id, "request": item["text"], "expected_base": sha, "auto_merge": auto_merge}})
            except RemoteError as error:
                return self._update(request_id, state="uncertain" if error.uncertain else "failed_to_dispatch",
                    error="GitHub acceptance is uncertain. Refresh to reconcile; this request will not be dispatched again." if error.uncertain else
                          "GitHub rejected dispatch. Ensure the setup PR is merged, Actions is enabled and your token has Actions write access.")
            return self._update(request_id, state="submitted", error=None)

    async def refresh(self, request_id):
        self._enabled()
        async with self.lock:
            item = self.get(request_id)
            if item["state"] in {"draft", "failed_to_dispatch"}:
                return item
            try:
                run = None
                if item["run_id"]:
                    run = await self.github.request("GET", f"repos/{REPOSITORY}/actions/runs/{item['run_id']}")
                else:
                    # Bound reads to this request's creation time; paginate busy repositories.
                    for page in range(1, 11):
                        payload = await self.github.request("GET", f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100&page={page}&created=" + quote(">=" + item["created_at"], safe=""))
                        runs = payload.get("workflow_runs", [])
                        matches = [r for r in runs if r.get("display_title") == f"Development {request_id}" and r.get("event") == "workflow_dispatch"]
                        if matches:
                            if len(matches) > 1:
                                return self._update(request_id, state="uncertain", error="Multiple cloud runs matched. Inspect GitHub before proceeding.")
                            run = matches[0]
                            break
                        if len(runs) < 100:
                            break
                if run is None:
                    return item
                run_id = int(run["id"])
                run_url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
                attempt = int(run.get("run_attempt", 1))
                pulls = await self.github.request("GET", f"repos/{REPOSITORY}/pulls?state=all&head=" + quote(REPOSITORY.split('/')[0] + f":NLS/claude-{run_id}-{attempt}", safe="") + "&per_page=10")
                if pulls:
                    pr = pulls[0]
                    number = int(pr["number"])
                    state = "merged" if pr.get("merged_at") else "closed" if pr["state"] == "closed" else "pr_ready"
                    return self._update(request_id, state=state, run_id=run_id, run_url=run_url,
                        pr_number=number, pr_url=f"https://github.com/{REPOSITORY}/pull/{number}", merge_sha=pr.get("merge_commit_sha") if state == "merged" else None, error=None)
                status = run.get("status")
                state = "queued" if status in {"queued", "waiting", "requested", "pending"} else "in_progress"
                if status == "completed":
                    state = "failed"
                return self._update(request_id, state=state, run_id=run_id, run_url=run_url,
                    error="Cloud run finished without a PR. Open its run details; no local update occurred." if state == "failed" else None)
            except (RemoteError, KeyError, TypeError, ValueError):
                raise DevelopmentError("Could not refresh GitHub. Existing request state was preserved", 502) from None
