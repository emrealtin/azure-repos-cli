# Azure Repos CLI

AI-first Python CLI for Azure DevOps pull request workflows.
It speeds up PR checks, approval, and AI-assisted automated review in a single workflow.

## Highlights

- Automatically analyzes all PR diffs with AI integration.
- `check -ai` can run AI review after approval when PR conditions are valid.
- Shows AI suggestions in a table format: `Path`, `Comment`, `Diff`.
- Pushes only selected comment numbers to Azure (for example `1,3,5`) instead of all comments.

## Project Structure

```text
.
├── main.py
├── requirements.txt
├── src/
│   └── azure_repos_cli/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── services/
│       │   ├── ai_review_service.py
│       │   ├── azure_devops_service.py
│       │   └── pr_cache_service.py
│       └── utils/
│           └── http_client.py
            └── cert/ (optional)
└── .pr_repo_cache.json
```

## Architecture

- `config.py`: loads `.env` and application settings
- `utils/http_client.py`: HTTP wrapper and `-log` output
- `services/pr_cache_service.py`: PR -> repository cache management
- `services/azure_devops_service.py`: Azure DevOps API integrations
- `services/ai_review_service.py`: Codex/OpenAI review flow (`review -ai`, `check -ai`)
- `cli.py`: command definitions and user interaction
- `main.py`: entrypoint only

## Requirements

- Python 3.9+
- `click`, `requests`, `rich`

Install:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration (.env)

```env
ORGANIZATION=YourOrganization
PAT=YOUR_AZURE_DEVOPS_PAT
PROJECT_REPOS={"PROJECT_NAME1":["repo1","repo2"],"PROJECT_NAME2":["repo3"]}
TARGET_USERS=["Name Surname", "Another User"]
TEST_PIPELINE=Test

# AI review
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5-mini
OPENAI_BASE_URL=https://api.openai.com
OPENAI_CA_BUNDLE=
OPENAI_SSL_VERIFY=true
```

`OPENAI_CA_BUNDLE` should point to a trusted corporate/root CA bundle. Do not pin a single leaf certificate for `api.openai.com`, because it can become stale and cause intermittent SSL failures.

## Usage

```bash
python3 main.py list
python3 main.py list -u "Name SURNAME"

python3 main.py check 12345
python3 main.py check 12345 -ai
python3 main.py review 12345
python3 main.py review 12345 -ai
python3 main.py comment 12345 "test comment"
```

Alias usage:

```bash
python3 main.py -l
python3 main.py -c 12345
python3 main.py -c 12345 -ai
python3 main.py -r 12345
python3 main.py -r 12345 -ai
python3 main.py -cm 12345 "test comment"
```

## AI Review Flow

1. PR diffs are fetched per file.
2. The OpenAI/Codex model reviews the full diff in one pass.
3. Results are listed per comment:
   - `Path`: `severity + file_path:line`
   - `Comment`: actionable feedback in a team-lead-to-developer tone
   - `Diff`: contextual, colorized snippet around the target line
4. Comments to post are selected by number (`all` or `1,3,5`).

## Approve + AI

- `check <PR_ID>`: validates comment status, draft status, and pipeline status.
- `check <PR_ID> -ai`: if conditions pass, AI review can run automatically after approval.
- If the PR is already approved, approval is skipped and AI review can still run.

## Notes

- The `list` command updates `.pr_repo_cache.json` on every run.
- `check/review/comment` resolve target repository via cache.
- `review` (without AI): shows page-by-page diff and allows manual line comments.
- `review -ai`: runs automated AI review, lists suggestions, and posts selected ones to Azure.
