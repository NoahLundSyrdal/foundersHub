# Cloud Claude improvements for the local demo

The Improve tab accepts a typed request or browser voice transcription. Save the draft, review the exact text, then choose **Send to cloud Claude**. Only that text is sent to the fixed `NoahLundSyrdal/foundersHub` GitHub repository. No microphone recording, office database, error log, conversation history or local environment is uploaded. Remove personal details before sending; a small credential-pattern filter is a backstop, not a guarantee of redaction.

Claude runs on GitHub-hosted Linux runners using the existing Anthropic API key stored as an Actions secret. Nothing runs a coding agent on the demo Mac. The workflow has a $5 model budget, 30-turn limit and 20-minute generation timeout; runner usage and API requests can incur costs. Every job requires an explicit operator dispatch. Ordinary office conversation never starts development work.

This first demo supports changes in `apps/web/src/` (except `api.ts`) and documentation. Examples: rename a view, add a display/filter using existing data, explain an error, or repair a frontend bug. Backend APIs, purchasing policy, identity/approval, dependencies, hardware, workflows and updater controls require manual development and review. The cloud guard rejects changes outside the supported scope.

## Repository setup

1. Review and merge the setup PR containing `.github/workflows/claude-development.yml`, `validate.yml`, and the trusted orchestration scripts. A dispatch workflow must exist on the default branch before the local app can trigger it.
2. Add the existing Anthropic key as the repository Actions secret **ANTHROPIC_API_KEY**. Use `gh secret set ANTHROPIC_API_KEY --repo NoahLundSyrdal/foundersHub` or GitHub Settings → Secrets and variables → Actions. Do not put it in YAML, issue comments, requests, prompts or Git.
3. Under Actions → General → Workflow permissions, enable **Allow GitHub Actions to create and approve pull requests**. Leave default workflow permissions read-only. The workflow grants a write token only to its final trusted publication job; it does not approve reviews or merge.
4. Require owner review before merging. Prefer a repository rule requiring the `Office checks` job for main; availability depends on the GitHub plan. The local updater independently requires a successful trusted GitHub Actions check for the exact revision.

The hosted workflow uses Anthropic's official [`claude-code-action`](https://github.com/anthropics/claude-code-action), pinned to a commit. Its separate generation, validation and publication VMs ensure generated code never runs in the PR publication job. The generation job has file-editing tools, a read-only repository token and the model key. Candidate checks run separately without the model key or repository write credentials. The final job revalidates the bounded patch and opens a draft PR; it never executes candidate code.

`GITHUB_TOKEN`-created PRs do not trigger another PR workflow automatically. The cloud run checks the candidate before publication and links the result in the PR. After a human merges, the push workflow runs `Office checks` against the merged revision. Do not bypass a failed check or change the release controls to make a demo update pass.

## Demo Mac setup

Use Node 22.13+, Python 3.12+, uv and GitHub CLI. Authenticate `gh auth login` to an account with write access to this repository, or set `OFFICE_DEVELOPMENT_GITHUB_TOKEN` to a repository-scoped token with Actions read/write, Contents read and Pull requests read. Keep credentials in the ignored `.env` file or the GitHub CLI's credential store.

In the ignored `.env`:

```dotenv
OFFICE_DEVELOPMENT_ENABLED=true
```

Start the app normally with `npm run dev`, or use the [approved-release supervisor](local-demo-updater.md) to receive approved updates. In **Improve**, say or type:

> Rename the History tab to Activity. Keep its existing behavior and add a short sentence describing the view.

Choose **Save for review**, then **Send to cloud Claude**. Follow **View cloud run**, then **Review change**. A failure or unsupported request remains a failure; the app never claims it was fixed. Browser speech recognition depends on browser support and permission, and may use the browser vendor's speech service; typed feedback works without it.

After reviewing and merging the PR, refresh its status. The app displays the exact merged SHA and the CLI command for an owner to approve the local update. The [updater](local-demo-updater.md) verifies the PR and checks before installation, then the supervisor restarts and probes the app. This is local installation, not public cloud hosting. Firmware and board connectivity are untouched and need their existing hardware validation.

## Recovery and local access

Requests have persistent IDs and text fingerprints. Duplicate sends return the existing result. The server persists dispatching before contacting GitHub. A timeout or restart during dispatch becomes **uncertain**; refreshing searches for the existing run and never starts a second paid job. If no run can be found, inspect GitHub Actions manually before creating a new request. Successful runs create draft PRs; they never merge or deploy themselves. A changed main revision makes a cloud candidate fail rather than rebase untested code.

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
