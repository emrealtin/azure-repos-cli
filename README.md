# Azure Review CLI

A modular Python CLI to speed up Azure DevOps pull request workflows.

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
└── .pr_repo_cache.json
```

## Architecture

- `config.py`: loads `.env` and application settings
- `utils/http_client.py`: HTTP wrapper and `-log` output
- `services/pr_cache_service.py`: PR -> repository cache management
- `services/azure_devops_service.py`: Azure DevOps API integrations
- `services/ai_review_service.py`: Codex/OpenAI review flow for `review -ai`
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

## Usage

```bash
python3 main.py list
python3 main.py list -u "Name Surname"

python3 main.py check 12345
python3 main.py review 12345
python3 main.py review 12345 -ai
python3 main.py comment 12345 "test comment"
```

Alias usage:

```bash
python3 main.py -l
python3 main.py -c 12345
python3 main.py -r 12345
python3 main.py -cm 12345 "test comment"
```

## Notes

- The `list` command updates `.pr_repo_cache.json` on every run.
- `check/review/comment` resolve target repository via cache.
- `review` (without AI): displays page-by-page diffs and allows line-level comments.
- `review -ai`: analyzes all pages in one AI pass, shows suggestions, and posts them in bulk after confirmation.
