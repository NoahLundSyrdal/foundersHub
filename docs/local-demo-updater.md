# Approved local demo updates

Claude edits and tests code on a **GitHub-hosted runner**. Anders's Mac runs the app and this small release supervisor; it does not run Claude Code, a coding SDK, a shell controlled by the conversation model, or a self-hosted GitHub Actions runner.

The updater is an explicit owner action in a terminal. A spoken request can create a development draft, but cannot grant release authority. The local Improve view can authorize a checked repository merge; installing that revision on the Mac remains separate. The dashboard shows the merged PR and update command; it never executes that command.

## First setup on the demo Mac

Requirements: macOS, Python **3.12+**, Git, GitHub CLI signed in with read access to this repository, Node **22.13+**, npm, and uv. Use a fresh, clean checkout of merged `main` for the demo. Keep other developers' checkouts separate.

After the setup PR is reviewed and merged, install the existing app normally:

```sh
npm ci
npm run setup:python
cp .env.example .env
```

Put the existing Anthropic credential and existing local demo settings in the ignored `.env`. Preserve the original `.data` if moving an existing demo. Do not commit either file or directory. Use absolute paths for any custom database or other runtime paths in `.env`; the default `.data` paths are shared automatically between releases.

Stop an existing `npm run dev` before starting the supervisor. Review the current full commit SHA and its successful `Office checks` run on GitHub, then explicitly approve it:

```sh
git rev-parse HEAD
python3 scripts/local_demo.py init --approve-sha FULL_REVIEWED_MAIN_SHA
python3 scripts/local_demo.py run
```

Replace the placeholder with the full reviewed SHA. `init` checks that the source checkout is clean and exactly matches that SHA, the commit belongs to this repository's `main`, and the exact revision has a successful completed GitHub Actions `Office checks` result. A re-run with a newer failing or pending check blocks initialization. `run` refuses to start without an approved manifest.

Open **http://127.0.0.1:5173**. The supervisor starts the Python API on `127.0.0.1:3001` and Vite on `127.0.0.1:5173`, using strict ports. API and browser health must pass three consecutive probes. Ctrl+C stops the supervised processes. Other hardware, speech and WhatsApp processes remain separately managed; their integration must be verified on the actual Mac.

## Install a reviewed improvement

Inspect Claude's PR after the selected manual or automatic merge mode succeeds. In another terminal on the demo Mac, approve its **merged commit SHA**, not an unmerged branch/head SHA:

```sh
python3 scripts/local_demo.py update --pr 123 --approve-sha FULL_REVIEWED_MERGE_SHA
python3 scripts/local_demo.py status
```

The updater checks the merged PR targets this repository's `main` and matches the exact approved SHA; fetches from the fixed repository URL; rejects backwards/divergent updates; verifies the exact SHA's completed successful `Office checks` from the GitHub Actions application; rejects other pending/failing Actions checks; and refuses a dirty source checkout. Automatic cloud merges attest their exact tested revision because a bot push does not trigger another workflow; manual merges use the ordinary push checks. It also checks again after dependency preparation, so a newer check re-run cannot be ignored during installation.

It archives that approved Git revision into ignored `.data/local-demo/releases/<sha>` rather than checking out or overwriting the developer's working tree. It shares the source checkout's ignored `.env` and `.data`, installs the existing dependency versions, builds the web app, and verifies tracked source files still match Git after installation. Dependencies' install/build scripts run locally only after explicit SHA approval; the coding agent stays remote. A prepared release is selected atomically in `.data/local-demo/release.json`.

A running supervisor notices the selected revision, stops its own previous app processes, starts the candidate and probes API/browser health. If candidate startup fails, it selects and restarts the previous **code** revision. It never restores or deletes the database. If no supervisor is running, the command prepares/selects the release; use `run` to start it. A prepared release is not proof of a running deployment; inspect `status` and the app.

App stdout/stderr is suppressed by this supervisor to avoid persisting potentially sensitive runtime output. For a failure needing detailed diagnosis, stop the supervisor and inspect the app manually using its existing development commands. The updater reports bounded operation names and exit codes, not subprocess diagnostics or credentials.

## Scope and failure handling

Automatic demo updates refuse changes to workflow/updater controls, dependency manifests and locks, config/policy, shared contracts, device firmware/bridge, conventional database/schema/migration files and core spending/approval files. Such changes need manual reviewed installation and a separate state/migration plan. This path is suitable for bounded UI/application fixes; it is not a general database migration system. Backend code can still change runtime state, so review its behavior before approval. A healthy API/browser proves startup only, not voice recognition, physical hardware, real supplier execution, or correct feature behavior.

If preparation fails, the selected app is unchanged. Failed candidate directories remain available for local inspection; inspect and remove only that candidate release directory before retrying the same SHA. Never delete the shared `.data`. If both candidate and previous code fail health, the supervisor stops and requires manual diagnosis. No uncertain supplier execution is retried by the updater.

Updater release state is local and owner-controlled, ignored by Git, and contains revision metadata only. Keep using the same original checkout to invoke the script. Do not edit release archives or the manifest manually. The updater uses read-only GitHub calls and does not push, merge, approve a PR, or send any external message.
