import base64
import difflib
import json
import os
import sys

import click
import requests
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

console = Console()


def load_env_file(env_path=".env"):
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value


def parse_project_repos(value):
    if not value:
        raise ValueError("PROJECT_REPOS is required in .env (JSON object format).")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"PROJECT_REPOS must be valid JSON. Error: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("PROJECT_REPOS must be a JSON object.")

    normalized = {}
    for project, repo_list in parsed.items():
        if isinstance(repo_list, list):
            normalized[str(project)] = [str(repo).strip() for repo in repo_list if str(repo).strip()]
        elif isinstance(repo_list, str) and repo_list.strip():
            normalized[str(project)] = [repo_list.strip()]

    if not normalized:
        raise ValueError("PROJECT_REPOS cannot be empty.")
    return normalized


def parse_target_users(value):
    if not value:
        return []

    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(user).strip() for user in parsed if str(user).strip()]
    except json.JSONDecodeError:
        pass

    return [item.strip() for item in value.split(",") if item.strip()]


load_env_file()

ORGANIZATION = os.getenv("ORGANIZATION", "").strip()
PAT = os.getenv("PAT", "").strip()
PROJECT_REPOS = parse_project_repos(os.getenv("PROJECT_REPOS"))
TARGET_USERS = parse_target_users(os.getenv("TARGET_USERS"))

if not ORGANIZATION:
    raise ValueError("ORGANIZATION is required in .env")
if not PAT:
    raise ValueError("PAT is required in .env")


def get_auth_headers():
    credentials = f":{PAT}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()
    return {
        "Authorization": f"Basic {encoded_credentials}",
        "Content-Type": "application/json",
    }


def is_success(status_code):
    return 200 <= status_code < 300


def iter_project_repo_targets():
    for project, repo_ids in PROJECT_REPOS.items():
        for repo_id in repo_ids:
            yield project, repo_id


def get_pr_base_url(project, repo_id):
    return f"https://dev.azure.com/{ORGANIZATION}/{project}/_apis/git/repositories/{repo_id}/pullRequests"


def get_targets_text():
    return ", ".join([f"{project}/{repo_id}" for project, repo_id in iter_project_repo_targets()])


def get_current_user(headers):
    url = f"https://dev.azure.com/{ORGANIZATION}/_apis/connectionData?api-version=7.1-preview.1"
    response = requests.get(url, headers=headers)
    if not is_success(response.status_code):
        return None, f"Failed to fetch current user info. Status code: {response.status_code}"

    user = response.json().get("authenticatedUser", {})
    user_id = user.get("id")
    if not user_id:
        return None, "User id was not found in connectionData."
    return user, None


def find_pr_repo(taskno, headers):
    for project, repo_id in iter_project_repo_targets():
        pr_url = f"{get_pr_base_url(project, repo_id)}/{taskno}?api-version=7.1"
        response = requests.get(pr_url, headers=headers)
        if response.status_code == 200:
            return project, repo_id, response.json()
    return None, None, None


def fetch_file_content_at_commit(project, repo_id, file_path, commit_id, headers):
    if not commit_id:
        return "", None

    url = f"https://dev.azure.com/{ORGANIZATION}/{project}/_apis/git/repositories/{repo_id}/items"
    params = {
        "path": file_path,
        "versionDescriptor.versionType": "commit",
        "versionDescriptor.version": commit_id,
        "includeContent": "true",
        "api-version": "7.1",
    }
    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 404:
        return "", None
    if not is_success(response.status_code):
        return None, f"Failed to fetch file '{file_path}' at commit {commit_id}. Status code: {response.status_code}"

    try:
        payload = response.json()
        if isinstance(payload, dict):
            if payload.get("isBinary"):
                return "", None
            return payload.get("content", ""), None
    except ValueError:
        pass

    return response.text, None


def render_colored_diff(diff_text):
    text = Text()
    for line in diff_text.splitlines():
        style = None
        if line.startswith("+") and not line.startswith("+++"):
            style = "green"
        elif line.startswith("-") and not line.startswith("---"):
            style = "red"
        elif line.startswith("@@") or line.startswith("diff --git") or line.startswith("---") or line.startswith("+++"):
            style = "cyan"
        text.append(line, style=style)
        text.append("\n")
    return text


def get_pr_diff(taskno, project, repo_id, headers):
    iterations_url = f"{get_pr_base_url(project, repo_id)}/{taskno}/iterations?api-version=7.1"
    iterations_response = requests.get(iterations_url, headers=headers)
    if iterations_response.status_code != 200:
        return None, f"Failed to fetch iteration data. Status code: {iterations_response.status_code}"

    iterations = iterations_response.json().get("value", [])
    if not iterations:
        return "", None

    latest_iteration_obj = max(iterations, key=lambda x: x.get("id", 0))
    latest_iteration = latest_iteration_obj.get("id")
    base_commit = latest_iteration_obj.get("commonRefCommit", {}).get("commitId")
    target_commit = latest_iteration_obj.get("sourceRefCommit", {}).get("commitId")

    changes_url = (
        f"{get_pr_base_url(project, repo_id)}/{taskno}/iterations/{latest_iteration}/changes"
        f"?api-version=7.1&$top=2000"
    )
    changes_response = requests.get(changes_url, headers=headers)
    if changes_response.status_code != 200:
        return None, f"Failed to fetch diff changes. Status code: {changes_response.status_code}"

    changes = changes_response.json().get("changeEntries", [])
    if not changes:
        return "", None

    file_diffs = []
    for change in changes:
        item = change.get("item", {})
        path = item.get("path", "")
        original_path = (change.get("originalPath") or path).strip()
        change_type = (change.get("changeType") or "edit").lower()

        if not path and not original_path:
            continue

        old_path = original_path or path
        new_path = path or original_path

        old_content = ""
        new_content = ""

        if "add" in change_type:
            fetched_new, err = fetch_file_content_at_commit(project, repo_id, new_path, target_commit, headers)
            if err:
                return None, err
            new_content = fetched_new or ""
        elif "delete" in change_type:
            fetched_old, err = fetch_file_content_at_commit(project, repo_id, old_path, base_commit, headers)
            if err:
                return None, err
            old_content = fetched_old or ""
        else:
            fetched_old, err = fetch_file_content_at_commit(project, repo_id, old_path, base_commit, headers)
            if err:
                return None, err
            fetched_new, err = fetch_file_content_at_commit(project, repo_id, new_path, target_commit, headers)
            if err:
                return None, err
            old_content = fetched_old or ""
            new_content = fetched_new or ""

        diff_lines = list(
            difflib.unified_diff(
                old_content.splitlines(),
                new_content.splitlines(),
                fromfile=f"a{old_path}",
                tofile=f"b{new_path}",
                lineterm="",
                n=3,
            )
        )

        if not diff_lines:
            continue

        file_diffs.append(
            {
                "path": new_path,
                "change_type": change_type,
                "diff_text": "\n".join(diff_lines),
            }
        )

    return file_diffs, None


def approve_pr(taskno, project, repo_id, headers):
    user, error = get_current_user(headers)
    if error:
        return error

    reviewer_id = user.get("id")
    reviewer_url = f"{get_pr_base_url(project, repo_id)}/{taskno}/reviewers/{reviewer_id}?api-version=7.1"
    response = requests.put(reviewer_url, json={"vote": 10}, headers=headers)
    if not is_success(response.status_code):
        return f"Approve request failed. Status code: {response.status_code}"
    return None


def get_reviewer_vote(taskno, project, repo_id, reviewer_id, headers):
    reviewer_url = f"{get_pr_base_url(project, repo_id)}/{taskno}/reviewers/{reviewer_id}?api-version=7.1"
    response = requests.get(reviewer_url, headers=headers)
    if not is_success(response.status_code):
        return None, f"Failed to fetch reviewer vote. Status code: {response.status_code}"
    return response.json().get("vote"), None


# def merge_pr(taskno, project, repo_id, pr_data, headers):
#     merge_url = f"{get_pr_base_url(project, repo_id)}/{taskno}?api-version=7.1"
#     payload = {"status": "completed"}
#
#     last_merge_commit = pr_data.get("lastMergeSourceCommit", {}).get("commitId")
#     if last_merge_commit:
#         payload["lastMergeSourceCommit"] = {"commitId": last_merge_commit}
#
#     response = requests.patch(merge_url, json=payload, headers=headers)
#     if not is_success(response.status_code):
#         return f"Merge request failed. Status code: {response.status_code}"
#     return None


def mock_ai_review(diff_text):
    # Returns a mock response until Gemini/Vertex AI integration is implemented.
    if not diff_text:
        return "AI Review (mock): Diff appears empty; no changes to analyze."
    line_count = len(diff_text.splitlines())
    return (
        "AI Review (mock -> Gemini/Vertex AI): "
        f"Diff received ({line_count} lines). Potential risks and improvement suggestions "
        "will be returned here in the real integration."
    )


@click.group()
def cli():
    """🤖 Azure DevOps PR Automation and Code Review Tool"""
    pass


def check_impl(taskno, with_ai=False):
    headers = get_auth_headers()
    project, repo_id, _ = find_pr_repo(taskno, headers)

    if not project:
        console.print(
            f"[bold red]❌ PR #{taskno} was not found in configured project/repo targets ({get_targets_text()}).[/bold red]"
        )
        return

    console.print(f"⏳ [bold blue]Checking PR #{taskno}...[/bold blue]")

    # 1. Fetch PR threads (comments)
    threads_url = f"{get_pr_base_url(project, repo_id)}/{taskno}/threads?api-version=7.1"
    response = requests.get(threads_url, headers=headers)

    if response.status_code != 200:
        console.print(f"[bold red]❌ Failed to fetch PR data. Status code: {response.status_code}[/bold red]")
        return

    threads = response.json().get("value", [])

    # Check unresolved comments
    unresolved_threads = []
    for thread in threads:
        # Keep only active/pending discussion threads.
        if thread.get("status") in ["active", "pending"]:
            unresolved_threads.append(thread)

    if unresolved_threads:
        console.print(f"[bold red]⚠️ This PR still has {len(unresolved_threads)} unresolved comments.[/bold red]")
        return

    console.print("[bold green]✅ All comment threads are resolved.[/bold green]")

    user, user_error = get_current_user(headers)
    if user_error:
        console.print(f"[bold red]❌ {user_error}[/bold red]")
        return

    reviewer_id = user.get("id")
    vote, vote_error = get_reviewer_vote(taskno, project, repo_id, reviewer_id, headers)
    if vote_error:
        console.print(f"[bold red]❌ {vote_error}[/bold red]")
        return

    if vote == 10:
        console.print("[bold yellow]ℹ️ PR is already approved by you. Skipping approval prompt.[/bold yellow]")
        if with_ai:
            review_impl(taskno, with_ai=True)
        return

    # 2. Approve step (merge is temporarily disabled)
    approve = Confirm.ask(f"❓ Do you want to [bold green]approve[/bold green] PR #{taskno}?")
    if approve:
        approve_error = approve_pr(taskno, project, repo_id, headers)
        if approve_error:
            console.print(f"[bold red]❌ {approve_error}[/bold red]")
            return

        console.print("[bold green]✅ PR was approved successfully on Azure DevOps.[/bold green]")
        console.print("[yellow]ℹ️ Merge is temporarily disabled in code.[/yellow]")
    else:
        console.print("[yellow]Approval step skipped.[/yellow]")
        return

    if with_ai:
        review_impl(taskno, with_ai=True)


def list_prs_impl(user_filter=None):
    """Lists active pull requests opened by configured team members."""

    headers = get_auth_headers()

    console.print("⏳ [cyan]Fetching active pull requests...[/cyan]")
    prs = []
    for project, repo_id in iter_project_repo_targets():
        url = (
            f"https://dev.azure.com/{ORGANIZATION}/{project}/_apis/git/repositories/"
            f"{repo_id}/pullRequests?searchCriteria.status=active&api-version=7.1"
        )
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            console.print(
                f"[bold red]❌ Failed to fetch PRs for project/repo '{project}/{repo_id}'. Status code: {response.status_code}[/bold red]"
            )
            continue

        repo_prs = response.json().get("value", [])
        for pr in repo_prs:
            pr["_project_name"] = project
            pr["_repo_name"] = pr.get("repository", {}).get("name") or repo_id
        prs.extend(repo_prs)

    effective_users = [user_filter] if user_filter else TARGET_USERS

    # Filter by configured users
    if effective_users:
        filtered_prs = []
        for pr in prs:
            creator_name = pr.get("createdBy", {}).get("displayName", "").lower()

            # Keep PR if creator name matches one of the configured target users.
            if any(target.lower() in creator_name for target in effective_users):
                filtered_prs.append(pr)

        prs = filtered_prs

    if not prs:
        users_text = ", ".join(effective_users) if effective_users else "all users"
        console.print(
            f"[bold yellow]ℹ️ No active pull requests found for configured users ({users_text}).[/bold yellow]"
        )
        return

    # Build table
    table = Table(title="Active Pull Request List", show_header=True, header_style="bold magenta")
    table.add_column("PR ID", style="dim", width=8, justify="center")
    table.add_column("Project", justify="left", style="yellow")
    table.add_column("Repository", justify="left", style="cyan")
    table.add_column("Title", min_width=30)
    table.add_column("Created By", justify="left", style="green")
    table.add_column("Target Branch", justify="right", style="blue")

    for pr in prs:
        pr_id = str(pr.get("pullRequestId"))
        project_name = pr.get("_project_name", "Unknown")
        repo_name = pr.get("_repo_name", "Unknown")
        title = pr.get("title")
        creator = pr.get("createdBy", {}).get("displayName", "Unknown")
        target_ref = pr.get("targetRefName", "").replace("refs/heads/", "")

        table.add_row(pr_id, project_name, repo_name, title, creator, target_ref)

    console.print(table)


def review_impl(taskno, with_ai=False):
    headers = get_auth_headers()
    project, repo_id, pr_data = find_pr_repo(taskno, headers)
    if not project:
        console.print(
            f"[bold red]❌ PR #{taskno} was not found in configured project/repo targets ({get_targets_text()}).[/bold red]"
        )
        return

    repo_name = pr_data.get("repository", {}).get("name", repo_id)
    console.print(
        f"⏳ [cyan]Fetching diff for PR #{taskno}...[/cyan] [dim](Project: {project}, Repository: {repo_name})[/dim]"
    )

    file_diffs, error = get_pr_diff(taskno, project, repo_id, headers)
    if error:
        console.print(f"[bold red]❌ {error}[/bold red]")
        return

    if not file_diffs:
        console.print("[bold yellow]ℹ️ PR diff is empty. No changes found.[/bold yellow]")
        return

    console.print("[bold green]✅ Diff fetched successfully (file-level code diff):[/bold green]")
    for file_diff in file_diffs:
        file_path = file_diff.get("path", "unknown")
        change_type = file_diff.get("change_type", "edit")
        diff_text = file_diff.get("diff_text", "")
        console.rule(f"[bold]File: {file_path}[/bold] [dim]({change_type})[/dim]")
        console.print(render_colored_diff(diff_text))

    if with_ai:
        console.print("\n[cyan]🤖 Sending diff to mock AI review service...[/cyan]")
        merged_diff_text = "\n\n".join([fd.get("diff_text", "") for fd in file_diffs if fd.get("diff_text")])
        ai_result = mock_ai_review(merged_diff_text)
        console.print(f"[bold magenta]{ai_result}[/bold magenta]")


def comment_impl(taskno, comment_text):
    headers = get_auth_headers()
    project, repo_id, _ = find_pr_repo(taskno, headers)
    if not project:
        console.print(
            f"[bold red]❌ PR #{taskno} was not found in configured project/repo targets ({get_targets_text()}).[/bold red]"
        )
        return

    threads_url = f"{get_pr_base_url(project, repo_id)}/{taskno}/threads?api-version=7.1"
    payload = {
        "comments": [{"parentCommentId": 0, "content": comment_text, "commentType": 1}],
        "status": "active",
    }
    response = requests.post(threads_url, headers=headers, json=payload)
    if not is_success(response.status_code):
        console.print(f"[bold red]❌ Failed to add comment. Status code: {response.status_code}[/bold red]")
        return

    console.print("[bold green]✅ Comment added successfully.[/bold green]")


@cli.command(name="list")
@click.option("-u", "-user", "--user", "user_filter", type=str, help="Filter PR list by creator name")
def list_command(user_filter):
    """Lists active pull requests opened by configured team members."""
    list_prs_impl(user_filter=user_filter)


@cli.command(name="check")
@click.argument("taskno")
@click.option("-ai", "--ai", "with_ai", is_flag=True, help="Run mock AI review output")
def check_command(taskno, with_ai):
    """Checks unresolved comments in a PR and approves it."""
    check_impl(taskno, with_ai=with_ai)


@cli.command(name="review")
@click.argument("taskno")
@click.option("-ai", "--ai", "with_ai", is_flag=True, help="Send fetched diff to mock AI review flow")
def review_command(taskno, with_ai):
    """Fetches PR diff information and optionally runs mock AI review."""
    review_impl(taskno, with_ai=with_ai)


@cli.command(name="comment")
@click.argument("taskno")
@click.argument("comment_text", nargs=-1)
def comment_command(taskno, comment_text):
    """Adds a top-level comment thread to a PR."""
    comment_text_value = " ".join(comment_text).strip()
    if not comment_text_value:
        raise click.UsageError("Missing comment text. Usage: main.py comment <ID> \"your comment\"")
    comment_impl(taskno, comment_text_value)


def normalize_alias_args(argv):
    if not argv:
        return argv

    first = argv[0]
    args = argv[1:]

    if first in ("-l", "-list", "--list"):
        normalized = ["list"]
        for item in args:
            if item == "-user":
                normalized.append("--user")
            else:
                normalized.append(item)
        return normalized

    if first in ("-c", "-check", "--check"):
        with_ai = False
        remaining = []
        for item in args:
            if item in ("-ai", "--ai"):
                with_ai = True
            else:
                remaining.append(item)
        normalized = ["check"]
        if with_ai:
            normalized.append("--ai")
        return normalized + remaining

    if first in ("-r", "-review", "--review"):
        with_ai = False
        remaining = []
        for item in args:
            if item in ("-ai", "--ai"):
                with_ai = True
            else:
                remaining.append(item)
        normalized = ["review"]
        if with_ai:
            normalized.append("--ai")
        return normalized + remaining

    if first in ("-cm", "-comment", "--comment"):
        return ["comment"] + args

    return argv


if __name__ == "__main__":
    cli.main(args=normalize_alias_args(sys.argv[1:]), prog_name=os.path.basename(sys.argv[0]))
