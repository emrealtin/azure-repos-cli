# Azure Review CLI

A CLI tool designed to speed up pull request workflows on Azure DevOps.

## Features

- List active PRs across multiple `project/repository` targets
- `check --taskno` flow:
  - Checks unresolved PR comment threads
  - If all threads are resolved, submits **approve** for the PR
- `review --taskno` flow:
  - Shows code-level diffs for changed files in the PR
  - Displays changed lines with colors (added/removed)
- `review --taskno --withai` runs mock AI review output

Note: Merge is intentionally disabled for now (commented out in code).

## Requirements

- Python 3.9+
- Packages:
  - `click`
  - `requests`
  - `rich`

Install:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install click requests rich
```

## Configuration (.env)

Create a `.env` file at the project root:

```env
ORGANIZATION=
PAT=YOUR_AZURE_DEVOPS_PAT // Personal Access Token
PROJECT_REPOS={"PROJECT_NAME1":["repo1","repo2"],"PROJECT_NAME2":["repo3","repo4"]}
TARGET_USERS=["Name USERNAME", "Name USERNAME"]
```

### Variables

- `ORGANIZATION`: Azure DevOps organization name
- `PAT`: Azure DevOps Personal Access Token
- `PROJECT_REPOS`: JSON mapping of project -> repository list
- `TARGET_USERS`: Users to filter by (JSON array or comma-separated string)

## Usage

### 1) List active PRs

```bash
python3 main.py list
```

### 2) PR check + approve

```bash
python3 main.py check --taskno 12345
```

### 3) PR diff review

```bash
python3 main.py review --taskno 12345
```

### 4) PR diff + mock AI review

```bash
python3 main.py review --taskno 12345 --withai
```

## Security

- Keep `PAT` only in `.env`.
- `.env` is in `.gitignore`, so it is not committed to git.