from __future__ import annotations

import os

import click
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from azure_repos_cli.config import load_settings
from azure_repos_cli.services.ai_review_service import AIReviewService
from azure_repos_cli.services.azure_devops_service import AzureDevOpsService
from azure_repos_cli.services.pr_cache_service import PRCacheService
from azure_repos_cli.utils.http_client import HttpClient


console = Console()
settings = load_settings()
http = HttpClient(console)
cache = PRCacheService(settings.pr_cache_file)
azure = AzureDevOpsService(settings=settings, http=http, cache=cache)
ai = AIReviewService(settings=settings, azure_service=azure, http=http, console=console)


def check_impl(taskno, with_ai=False):
    http.log_operation(f"Run check command for PR #{taskno}")
    headers = azure.get_auth_headers()
    project, repo_id, pr_data, cache_error = azure.find_pr_repo_from_cache(taskno, headers)

    if not project:
        console.print(f"[bold red]❌ {cache_error}[/bold red]")
        return

    console.print(f"⏳ [bold blue]Checking PR #{taskno}...[/bold blue]")

    threads_url = f"{azure.get_pr_base_url(project, repo_id)}/{taskno}/threads?api-version=7.1"
    response = http.get(threads_url, headers=headers)

    threads_ok = False
    unresolved_count = 0
    thread_error_message = None
    if response.status_code != 200:
        thread_error_message = f"Failed to fetch PR thread data. Status code: {response.status_code}"
    else:
        threads = response.json().get("value", [])
        unresolved_count = sum(1 for thread in threads if azure.is_unresolved_comment_thread(thread))
        threads_ok = unresolved_count == 0

    pipeline_state, pipeline_message, pipeline_error = azure.get_pr_pipeline_status(taskno, project, repo_id, pr_data, headers)

    if thread_error_message:
        console.print(f"[bold red]⚠️ Comment: {thread_error_message}[/bold red]")
    elif threads_ok:
        console.print("[bold green]✅ Comment: All comment threads are resolved.[/bold green]")
    else:
        console.print(f"[bold yellow]⚠️ Comment: {unresolved_count} unresolved comment thread(s).[/bold yellow]")

    is_draft = bool((pr_data or {}).get("isDraft"))
    if is_draft:
        console.print("[bold yellow]⚠️ Status: Pull request is draft.[/bold yellow]")
    else:
        console.print("[bold green]✅ Status: Pull request is ready. Not Draft[/bold green]")

    if pipeline_error:
        console.print(f"[bold red]⚠️ Pipeline: {pipeline_error}[/bold red]")
        return

    if pipeline_state == "passed":
        console.print(f"[bold green]✅ Pipeline: {pipeline_message}[/bold green]")
    elif pipeline_state == "progress":
        console.print(f"[bold yellow]⚠️ Pipeline: {pipeline_message}[/bold yellow]")
        return
    elif pipeline_state == "failed":
        console.print(f"[bold red]❌ Pipeline: {pipeline_message}[/bold red]")
        return
    else:
        console.print(f"[bold red]❌ Pipeline: {pipeline_message}[/bold red]")
        return

    if not threads_ok:
        return

    if is_draft:
        console.print("[bold yellow]❗ Draft PR cannot be approved.[/bold yellow]")
        return

    console.print("[bold green]✅ All threads are resolved[/bold green]")

    user, user_error = azure.get_current_user(headers)
    if user_error:
        console.print(f"[bold red]❌ {user_error}[/bold red]")
        return

    reviewer_id = user.get("id")
    vote, vote_error = azure.get_reviewer_vote(taskno, project, repo_id, reviewer_id, headers)
    if vote_error:
        console.print(f"[bold red]❌ {vote_error}[/bold red]")
        return

    if vote == 10:
        console.print("[bold yellow]ℹ️ PR is already approved by you. Skipping approval prompt.[/bold yellow]")
        if with_ai:
            review_impl(taskno, with_ai=True)
        return

    approve = Confirm.ask(f"❓ Do you want to [bold green]approve[/bold green] PR #{taskno}?")
    if approve:
        approve_error = azure.approve_pr(taskno, project, repo_id, headers)
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
    http.log_operation("Run list command")
    headers = azure.get_auth_headers()

    console.print("⏳ [cyan]Fetching active pull requests...[/cyan]")
    prs = azure.list_active_prs(headers)
    cache.update_from_prs(prs)

    effective_users = [user_filter] if user_filter else settings.target_users
    if effective_users:
        prs = [
            pr
            for pr in prs
            if any(target.lower() in pr.get("createdBy", {}).get("displayName", "").lower() for target in effective_users)
        ]

    if not prs:
        users_text = ", ".join(effective_users) if effective_users else "all users"
        console.print(f"[bold yellow]ℹ️ No active pull requests found for configured users ({users_text}).[/bold yellow]")
        return

    table = Table(title="Active Pull Request List", show_header=True, header_style="bold magenta")
    table.add_column("PR ID", style="dim", width=8, justify="center")
    table.add_column("Project", justify="left", style="yellow")
    table.add_column("Repository", justify="left", style="cyan")
    table.add_column("Title", min_width=30)
    table.add_column("Created By", justify="left", style="green")
    table.add_column("Target", justify="right", style="blue")

    for pr in prs:
        pr_id = str(pr.get("pullRequestId"))
        project_name = pr.get("_project_name", "Unknown")
        repo_name = pr.get("_repo_name", "Unknown")
        title = azure.truncate_text(pr.get("title"), settings.title_max_len)
        creator = pr.get("createdBy", {}).get("displayName", "Unknown")
        target_ref = pr.get("targetRefName", "").replace("refs/heads/", "")
        pr_url = f"https://dev.azure.com/{settings.organization}/{project_name}/_git/{repo_name}/pullrequest/{pr_id}"
        pr_id_cell = Text(pr_id, style=f"link {pr_url}")
        table.add_row(pr_id_cell, project_name, repo_name, title, creator, target_ref)

    console.print(table)


def review_impl(taskno, with_ai=False):
    http.log_operation(f"Run review command for PR #{taskno}")
    headers = azure.get_auth_headers()
    project, repo_id, pr_data, cache_error = azure.find_pr_repo_from_cache(taskno, headers)
    if not project:
        console.print(f"[bold red]❌ {cache_error}[/bold red]")
        return
    block_reason = azure.get_pr_block_reason_for_review_or_comment(pr_data)
    if block_reason:
        console.print(f"[bold yellow]⚠️ {block_reason}[/bold yellow]")
        return

    repo_name = pr_data.get("repository", {}).get("name", repo_id)
    console.print(f"⏳ [cyan]Fetching diff for PR #{taskno}...[/cyan] [dim](Project: {project}, Repository: {repo_name})[/dim]")

    file_diffs, error = azure.get_pr_diff(taskno, project, repo_id, headers)
    if error:
        console.print(f"[bold red]❌ {error}[/bold red]")
        return

    if not file_diffs:
        console.print("[bold yellow]ℹ️ PR diff is empty. No changes found.[/bold yellow]")
        return

    console.print("[bold green]✅ Diff fetched successfully (file-level code diff):[/bold green]")

    if with_ai:
        ai.run_ai_review_flow(taskno=taskno, project=project, repo_id=repo_id, file_diffs=file_diffs, headers=headers)
        return

    total_pages = len(file_diffs)
    for page_index, file_diff in enumerate(file_diffs, start=1):
        file_path = file_diff.get("path", "unknown")
        change_type = file_diff.get("change_type", "edit")
        diff_text = file_diff.get("diff_text", "")
        console.rule(f"[bold]Review Pages {page_index}/{total_pages}[/bold]")
        console.print(f"[cyan]File:[/cyan] {file_path} [dim]({change_type})[/dim]")
        console.print(azure.render_colored_diff(diff_text))

        if Confirm.ask("Do you want to leave a comment for this page?", default=False):
            while True:
                while True:
                    raw_line = Prompt.ask("Enter line number")
                    try:
                        line_number = int(raw_line)
                    except ValueError:
                        line_number = 0
                    if line_number > 0:
                        break
                    console.print("[bold yellow]⚠️ Line number must be a positive integer.[/bold yellow]")

                while True:
                    comment_text = Prompt.ask("Enter comment text").strip()
                    if comment_text:
                        break
                    console.print("[bold yellow]⚠️ Comment cannot be empty.[/bold yellow]")

                error = azure.add_line_comment(
                    taskno=taskno,
                    project=project,
                    repo_id=repo_id,
                    file_path=file_path,
                    line_number=line_number,
                    comment_text=comment_text,
                    headers=headers,
                )
                if error:
                    console.print(f"[bold red]❌ {error}[/bold red]")
                else:
                    console.print("[bold green]✅ Comment added for this page.[/bold green]")

                if not Confirm.ask("Do you want to add another comment on this page?", default=False):
                    break


def comment_impl(taskno, comment_text):
    http.log_operation(f"Run comment command for PR #{taskno}")
    headers = azure.get_auth_headers()
    project, repo_id, pr_data, cache_error = azure.find_pr_repo_from_cache(taskno, headers)
    if not project:
        console.print(f"[bold red]❌ {cache_error}[/bold red]")
        return
    block_reason = azure.get_pr_block_reason_for_review_or_comment(pr_data)
    if block_reason:
        console.print(f"[bold yellow]⚠️ {block_reason}[/bold yellow]")
        return

    error = azure.add_general_comment(taskno, project, repo_id, comment_text, headers)
    if error:
        console.print(f"[bold red]❌ {error}[/bold red]")
        return

    console.print("[bold green]✅ Comment added successfully.[/bold green]")


@click.group()
def cli():
    """Azure DevOps PR automation and review tool."""


@cli.command(name="list")
@click.option("-u", "-user", "--user", "user_filter", type=str, help="Filter PR list by creator name")
@click.option("-log", "--log", "log_enabled", is_flag=True, help="Print request and operation logs")
def list_command(user_filter, log_enabled):
    http.set_log_enabled(log_enabled)
    list_prs_impl(user_filter=user_filter)


@cli.command(name="check")
@click.argument("taskno")
@click.option("-ai", "--ai", "with_ai", is_flag=True, help="Run AI review flow after check succeeds")
@click.option("-log", "--log", "log_enabled", is_flag=True, help="Print request and operation logs")
def check_command(taskno, with_ai, log_enabled):
    http.set_log_enabled(log_enabled)
    check_impl(taskno, with_ai=with_ai)


@cli.command(name="review")
@click.argument("taskno")
@click.option("-ai", "--ai", "with_ai", is_flag=True, help="Run Codex AI review flow for all diff pages")
@click.option("-log", "--log", "log_enabled", is_flag=True, help="Print request and operation logs")
def review_command(taskno, with_ai, log_enabled):
    http.set_log_enabled(log_enabled)
    review_impl(taskno, with_ai=with_ai)


@cli.command(name="comment")
@click.argument("taskno")
@click.argument("comment_text", nargs=-1)
@click.option("-log", "--log", "log_enabled", is_flag=True, help="Print request and operation logs")
def comment_command(taskno, comment_text, log_enabled):
    http.set_log_enabled(log_enabled)
    comment_text_value = " ".join(comment_text).strip()
    if not comment_text_value:
        raise click.UsageError('Missing comment text. Usage: main.py comment <ID> "your comment"')
    comment_impl(taskno, comment_text_value)


def normalize_alias_args(argv):
    if not argv:
        return argv

    first = argv[0]
    args = argv[1:]

    if first in ("-l", "-list", "--list"):
        with_log = False
        normalized = ["list"]
        for item in args:
            if item == "-user":
                normalized.append("--user")
            elif item in ("-log", "--log"):
                with_log = True
            else:
                normalized.append(item)
        if with_log:
            normalized.append("--log")
        return normalized

    if first in ("-c", "-check", "--check"):
        with_ai = False
        with_log = False
        remaining = []
        for item in args:
            if item in ("-ai", "--ai"):
                with_ai = True
            elif item in ("-log", "--log"):
                with_log = True
            else:
                remaining.append(item)
        normalized = ["check"]
        if with_ai:
            normalized.append("--ai")
        if with_log:
            normalized.append("--log")
        return normalized + remaining

    if first in ("-r", "-review", "--review"):
        with_ai = False
        with_log = False
        remaining = []
        for item in args:
            if item in ("-ai", "--ai"):
                with_ai = True
            elif item in ("-log", "--log"):
                with_log = True
            else:
                remaining.append(item)
        normalized = ["review"]
        if with_ai:
            normalized.append("--ai")
        if with_log:
            normalized.append("--log")
        return normalized + remaining

    if first in ("-cm", "-comment", "--comment"):
        with_log = False
        remaining = []
        for item in args:
            if item in ("-log", "--log"):
                with_log = True
            else:
                remaining.append(item)
        normalized = ["comment"]
        if with_log:
            normalized.append("--log")
        return normalized + remaining

    return argv


def run(argv=None):
    args = normalize_alias_args(argv if argv is not None else [])
    cli.main(args=args, prog_name=os.path.basename("main.py"))
