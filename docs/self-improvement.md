# Cloud Claude improvements for the local demo

The Improve tab accepts a typed request or browser voice transcription. Save the draft, review the exact text, then choose **Implement and open a PR** or **Implement and merge after checks**. The latter explicitly authorizes the scoped code change to enter main after validation. Only the reviewed text and merge choice are sent to the fixed `NoahLundSyrdal/foundersHub` GitHub repository. No microphone recording, office database, error log, conversation history or local environment is uploaded. Remove personal details before sending; a small credential-pattern filter is a backstop, not a guarantee of redaction.

Claude runs on GitHub-hosted Linux runners using the existing Anthropic API key stored as an Actions secret. Nothing runs a coding agent on the demo Mac. The workflow has a $5 model budget, 30-turn limit and 20-minute generation timeout; runner usage and API requests can incur costs. Every job requires an explicit operator dispatch. Ordinary office conversation never starts cloud coding.

The workflow supports actual application code changes: ordinary Python backend modules and route registration, frontend/API-client code, tests and documentation. For example, Claude can add temperature-change detection, an API for its results, and a dashboard view using the existing sensor feed. It must add meaningful behavior tests and pass the existing checks before a draft PR is published. Hardware access or live sensor behavior is never implied by a successful software test.

The guard keeps credentials, core purchasing/approval/execution, database/schema migrations, dependencies, firmware and release/development controls outside automated edits. Requests touching those areas stop for manual development and review. Allowed application code can still affect behavior: passing tests and the path guard do not prove semantic safety. Choose the PR-review mode whenever you want to inspect the diff before merging. Dependencies or migrations require a separate installation plan rather than assuming code rollback can undo state changes.

You can also describe a missing software feature in the normal office conversation, for example “Add monitoring that detects a three-degree temperature drop in ten minutes.” Live Claude can call `propose_development_change` to save a local draft in Improve. This draft tool never dispatches cloud jobs, changes monitoring settings, installs a feature, approves code or spends API credits on coding. Review its exact text and select its send/merge mode from the local Improve tab. Ordinary temperature questions should use the existing read tools and do not create development requests.

## Repository setup

1. Review and merge the setup PR containing `.github/workflows/claude-development.yml`, `validate.yml`, and the trusted orchestration scripts. A dispatch workflow must exist on the default branch before the local app can trigger it.
2. Add the existing Anthropic key as the repository Actions secret **ANTHROPIC_API_KEY**. Use `gh secret set ANTHROPIC_API_KEY --repo NoahLundSyrdal/foundersHub` or GitHub Settings → Secrets and variables → Actions. Do not put it in YAML, issue comments, requests, prompts or Git.
3. Under Actions → General → Workflow permissions, enable **Allow GitHub Actions to create and approve pull requests**. Leave default workflow permissions read-only. Write tokens exist only in the trusted publication/merge jobs; the Claude model never receives them. The workflow does not forge review approvals or bypass repository protection.
4. Keep any required repository reviews/checks enabled. The workflow respects repository protection and leaves a PR when policy prevents automatic merging. The local updater independently requires a successful trusted GitHub Actions check for the exact revision.

The hosted workflow uses Anthropic's official [`claude-code-action`](https://github.com/anthropics/claude-code-action), pinned to a commit. Separate generation, validation, publication and optional merge VMs ensure generated code never runs with repository write credentials. The generation job has file-editing tools, a read-only repository token and the model key. Candidate checks run separately without the model key or repository write credentials. Trusted orchestration revalidates the bounded patch, opens a PR, and merges only if the original dispatch explicitly allowed it and the exact tested revision remains eligible.

`GITHUB_TOKEN`-created PRs/pushes do not trigger another workflow automatically. The cloud run checks the candidate before publication and links the result in the PR; the trusted merge job records successful validation against the exact merged revision for the updater. A human merge triggers the normal push `Office checks`. Do not bypass a failed check or change release controls to make an update pass.

## Demo Mac setup

Use Node 22.13+, Python 3.12+, uv and GitHub CLI. Authenticate `gh auth login` to an account with write access to this repository, or set `OFFICE_DEVELOPMENT_GITHUB_TOKEN` to a repository-scoped token with Actions read/write, Contents read and Pull requests read. Keep credentials in the ignored `.env` file or the GitHub CLI's credential store.

In the ignored `.env`:

```dotenv
OFFICE_DEVELOPMENT_ENABLED=true
```

Start the app normally with `npm run dev`, or use the [approved-release supervisor](local-demo-updater.md) to receive approved updates. In **Improve**, say or type:

> Add a backend feature that detects when room temperature drops three degrees within ten minutes. Use existing sensor readings, expose the result in the dashboard, and test fresh, stale and simulated data separately.

Choose **Save for review**, then select the review-only or checked-merge mode. Follow **View cloud run**, then **Review change**. A failure or unsupported request remains a failure; the app never claims it was fixed. Browser speech recognition depends on browser support and permission, and may use the browser vendor's speech service; typed feedback works without it.

After a manual or checked automatic merge, refresh its status and inspect the change. The app displays the exact merged SHA and the CLI command for an owner to approve the local update. The [updater](local-demo-updater.md) verifies the PR and checks before installation, then the supervisor restarts and probes the app. This is local installation, not public cloud hosting. Firmware and board connectivity are untouched and need their existing hardware validation.

## Recovery and local access

Requests have persistent IDs and fingerprints binding their text and selected merge permission. Duplicate sends return the existing result; changing merge permission after dispatch is rejected. The server persists dispatching before contacting GitHub. A timeout or restart during dispatch becomes **uncertain**; refreshing searches for the existing run and never starts a second paid job. If no run can be found, inspect GitHub Actions manually before creating a new request. Review-only runs leave draft PRs. Checked-merge runs may merge, but neither mode installs code on the Mac. A changed main revision makes a cloud candidate fail rather than rebase untested code.

The development controls require loopback peer/Host and reject remote browser origins and forwarding headers. Keep API and Vite bound to loopback. Never forward `/api/development` through a tunnel or WhatsApp gateway, including gateways that strip forwarding headers. This is a single-operator local demo surface, not production user authentication. The browser receives no GitHub or Anthropic token. Disable with `OFFICE_DEVELOPMENT_ENABLED=false` and restart the API.

Useful local checks:

```sh
npm run check
npm run build
npm test
npm run test:procurement
python3 scripts/cloud-development-tests.py
python3 -m unittest discover -s scripts/tests
```
