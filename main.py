import base64
import difflib
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

import click
import requests
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

console = Console()
TITLE_MAX_LEN = 60
LOG_ENABLED = False
PR_CACHE_FILE = ".pr_repo_cache.json"


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


def get_auth_headers():
    credentials = f":{PAT}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()
    return {
        "Authorization": f"Basic {encoded_credentials}",
        "Content-Type": "application/json",
    }


def is_success(status_code):
    return 200 <= status_code < 300


def set_log_enabled(value):
    global LOG_ENABLED
    LOG_ENABLED = bool(value)


def log_operation(message):
    if LOG_ENABLED:
        console.print(f"[dim]LOG {message}[/dim]")


def http_request(method, url, **kwargs):
    if LOG_ENABLED:
        console.print(f"[dim]HTTP {method.upper()} {url}[/dim]")
        params = kwargs.get("params")
        payload = kwargs.get("json")
        if params:
            console.print(f"[dim]  params={params}[/dim]")
        if payload:
            console.print(f"[dim]  json={payload}[/dim]")

    response = requests.request(method=method.upper(), url=url, **kwargs)

    if LOG_ENABLED:
        console.print(f"[dim]  -> status={response.status_code}[/dim]")
    return response


def http_get(url, **kwargs):
    return http_request("GET", url, **kwargs)


def http_post(url, **kwargs):
    return http_request("POST", url, **kwargs)


def http_put(url, **kwargs):
    return http_request("PUT", url, **kwargs)


def truncate_text(value, max_len):
    text = (value or "").strip()
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3].rstrip() + "..."


load_env_file()

ORGANIZATION = os.getenv("ORGANIZATION", "").strip()
PAT = os.getenv("PAT", "").strip()
PROJECT_REPOS = parse_project_repos(os.getenv("PROJECT_REPOS"))
TARGET_USERS = parse_target_users(os.getenv("TARGET_USERS"))
TEST_PIPELINE = os.getenv("TEST_PIPELINE", "Test").strip() or "Test"

if not ORGANIZATION:
    raise ValueError("ORGANIZATION is required in .env")
if not PAT:
    raise ValueError("PAT is required in .env")


def iter_project_repo_targets():
    for project, repo_ids in PROJECT_REPOS.items():
        for repo_id in repo_ids:
            yield project, repo_id


def get_pr_base_url(project, repo_id):
    return f"https://dev.azure.com/{ORGANIZATION}/{project}/_apis/git/repositories/{repo_id}/pullRequests"


def get_targets_text():
    return ", ".join([f"{project}/{repo_id}" for project, repo_id in iter_project_repo_targets()])


def load_pr_repo_cache():
    if not os.path.exists(PR_CACHE_FILE):
        return {}
    try:
        with open(PR_CACHE_FILE, "r", encoding="utf-8") as cache_file:
            payload = json.load(cache_file)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def save_pr_repo_cache(cache_data):
    try:
        with open(PR_CACHE_FILE, "w", encoding="utf-8") as cache_file:
            json.dump(cache_data, cache_file, ensure_ascii=True, indent=2, sort_keys=True)
    except OSError:
        pass


def update_pr_repo_cache(prs):
    cache_data = load_pr_repo_cache()
    for pr in prs:
        pr_id = str(pr.get("pullRequestId") or "").strip()
        project_name = str(pr.get("_project_name") or "").strip()
        repo_id = str(pr.get("repository", {}).get("id") or "").strip()
        repo_name = str(pr.get("_repo_name") or "").strip()
        if not pr_id or not project_name or not repo_id:
            continue
        cache_data[pr_id] = {
            "project": project_name,
            "repo_id": repo_id,
            "repo_name": repo_name,
        }
    save_pr_repo_cache(cache_data)


def find_pr_repo_from_cache(taskno, headers):
    cache_data = load_pr_repo_cache()
    cache_entry = cache_data.get(str(taskno))
    if not cache_entry:
        return None, None, None, (
            f"PR #{taskno} repository mapping was not found in cache. "
            "Run `list` first so PR->repo mapping can be cached."
        )

    project = cache_entry.get("project")
    repo_id = cache_entry.get("repo_id")
    if not project or not repo_id:
        return None, None, None, (
            f"PR #{taskno} cache entry is invalid. "
            "Run `list` again to refresh cache."
        )

    pr_url = f"{get_pr_base_url(project, repo_id)}/{taskno}?api-version=7.1"
    response = http_get(pr_url, headers=headers)
    if response.status_code == 200:
        return project, repo_id, response.json(), None

    if response.status_code == 404:
        return None, None, None, (
            f"PR #{taskno} was not found in cached repo target ({project}/{repo_id}). "
            "Run `list` again to refresh cache."
        )

    return None, None, None, (
        f"Failed to fetch PR #{taskno} from cached repo target ({project}/{repo_id}). "
        f"Status code: {response.status_code}"
    )


def get_current_user(headers):
    log_operation("Fetch current user")
    url = f"https://dev.azure.com/{ORGANIZATION}/_apis/connectionData?api-version=7.1-preview.1"
    response = http_get(url, headers=headers)
    if not is_success(response.status_code):
        return None, f"Failed to fetch current user info. Status code: {response.status_code}"

    user = response.json().get("authenticatedUser", {})
    user_id = user.get("id")
    if not user_id:
        return None, "User id was not found in connectionData."
    return user, None


def find_pr_repo(taskno, headers):
    log_operation(f"Find PR #{taskno} in configured repositories")
    for project, repo_id in iter_project_repo_targets():
        pr_url = f"{get_pr_base_url(project, repo_id)}/{taskno}?api-version=7.1"
        response = http_get(pr_url, headers=headers)
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
    response = http_get(url, headers=headers, params=params)

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
    log_operation(f"Fetch diff for PR #{taskno}")
    iterations_url = f"{get_pr_base_url(project, repo_id)}/{taskno}/iterations?api-version=7.1"
    iterations_response = http_get(iterations_url, headers=headers)
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
    changes_response = http_get(changes_url, headers=headers)
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
    log_operation(f"Approve PR #{taskno}")
    user, error = get_current_user(headers)
    if error:
        return error

    reviewer_id = user.get("id")
    reviewer_url = f"{get_pr_base_url(project, repo_id)}/{taskno}/reviewers/{reviewer_id}?api-version=7.1"
    response = http_put(reviewer_url, json={"vote": 10}, headers=headers)
    if not is_success(response.status_code):
        return f"Approve request failed. Status code: {response.status_code}"
    return None


def get_reviewer_vote(taskno, project, repo_id, reviewer_id, headers):
    log_operation(f"Fetch reviewer vote for PR #{taskno}")
    reviewer_url = f"{get_pr_base_url(project, repo_id)}/{taskno}/reviewers/{reviewer_id}?api-version=7.1"
    response = http_get(reviewer_url, headers=headers)
    if not is_success(response.status_code):
        return None, f"Failed to fetch reviewer vote. Status code: {response.status_code}"
    return response.json().get("vote"), None


def get_pr_pipeline_status(taskno, project, repo_id, pr_data, headers):
    log_operation(f"Evaluate pipeline status for PR #{taskno}")
    statuses_url = f"{get_pr_base_url(project, repo_id)}/{taskno}/statuses?api-version=7.1"
    response = http_get(statuses_url, headers=headers)
    if not is_success(response.status_code):
        return "error", None, f"Failed to fetch PR pipeline status. Status code: {response.status_code}"

    statuses = response.json().get("value", [])

    def infer_status_from_pr_statuses(status_items):
        if not status_items:
            return "error", "No pipeline status found on this PR."

        ordered = sorted(status_items, key=lambda x: x.get("creationDate") or "", reverse=True)
        latest = ordered[0]
        state = (latest.get("state") or "").lower()
        context = latest.get("context", {}) or {}
        label = context.get("name") or context.get("genre") or "pipeline"

        if state == "succeeded":
            return "passed", f"{label} status is succeeded (from PR status)."
        if state in ("failed", "error"):
            return "failed", f"{label} status is failed (from PR status)."
        if state in ("pending", "notset", ""):
            return "progress", f"{label} status is in progress (from PR status)."
        return "error", f"{label} status is unknown ({state}) (from PR status)."

    def extract_build_ref(target_url):
        if not target_url or not isinstance(target_url, str):
            return None, None
        parsed = urlparse(target_url)
        query_params = parse_qs(parsed.query)
        build_id = (query_params.get("buildId") or [None])[0]
        if not build_id:
            return None, None

        path_parts = [part for part in parsed.path.split("/") if part]
        build_project = path_parts[1] if len(path_parts) > 1 else project
        return str(build_id), build_project

    build_candidates = []
    for status in statuses:
        target_url = (
            status.get("targetUrl")
            or status.get("details")
            or status.get("_links", {}).get("target", {}).get("href")
            or status.get("_links", {}).get("web", {}).get("href")
        )
        build_id, build_project = extract_build_ref(target_url)
        if build_id:
            build_candidates.append(
                {
                    "build_id": build_id,
                    "build_project": build_project or project,
                    "created_at": status.get("creationDate") or "",
                    "target_url": target_url,
                }
            )

    if not build_candidates:
        builds_url = f"https://dev.azure.com/{ORGANIZATION}/{project}/_apis/build/builds"
        params = {
            "repositoryId": repo_id,
            "repositoryType": "TfsGit",
            "reasonFilter": "pullRequest",
            "queryOrder": "queueTimeDescending",
            "$top": 50,
            "api-version": "7.1",
        }
        builds_response = http_get(builds_url, headers=headers, params=params)
        if builds_response.status_code == 401:
            fallback_state, fallback_message = infer_status_from_pr_statuses(statuses)
            return (
                fallback_state,
                f"{fallback_message} Build API returned 401. PAT needs Build (Read) permission.",
                None,
            )
        if builds_response.status_code == 400:
            # Some repos return 400 when repository filters are not accepted; retry without repository constraints.
            retry_params = {
                "reasonFilter": "pullRequest",
                "queryOrder": "queueTimeDescending",
                "$top": 100,
                "api-version": "7.1",
            }
            builds_response = http_get(builds_url, headers=headers, params=retry_params)
        if not is_success(builds_response.status_code):
            return "error", None, f"Failed to fetch PR build list. Status code: {builds_response.status_code}"

        source_ref = (pr_data or {}).get("sourceRefName", "")
        taskno_token = str(taskno)
        fallback_candidates = []
        for build in builds_response.json().get("value", []):
            trigger_info = build.get("triggerInfo") or {}
            source_branch = (build.get("sourceBranch") or "").strip()
            parameters = str(build.get("parameters") or "").replace(" ", "").lower()
            trigger_text = " ".join([f"{k}:{v}" for k, v in trigger_info.items()]).lower()

            matches_pr = (
                taskno_token in trigger_text
                or f"/{taskno_token}/" in source_branch
                or f"\"pullrequestid\":\"{taskno_token}\"" in parameters
            )
            if source_ref and source_branch and source_ref == source_branch:
                matches_pr = True

            if matches_pr and build.get("id"):
                fallback_candidates.append(build)

        if not fallback_candidates:
            return "error", "No pipeline status found on this PR.", None

        latest_build_obj = max(fallback_candidates, key=lambda item: item.get("queueTime") or "")
        build_candidates.append(
            {
                "build_id": str(latest_build_obj.get("id")),
                "build_project": (latest_build_obj.get("project") or {}).get("name") or project,
                "created_at": latest_build_obj.get("queueTime") or "",
                "target_url": (latest_build_obj.get("_links", {}).get("web", {}).get("href")),
            }
        )

    selected_build = max(build_candidates, key=lambda item: item.get("created_at") or "")
    build_id = selected_build["build_id"]
    build_project = selected_build["build_project"]
    build_url = f"https://dev.azure.com/{ORGANIZATION}/{build_project}/_apis/build/builds/{build_id}?api-version=7.1"
    build_response = http_get(build_url, headers=headers)
    if build_response.status_code == 401:
        fallback_state, fallback_message = infer_status_from_pr_statuses(statuses)
        return (
            fallback_state,
            f"{fallback_message} Build API returned 401. PAT needs Build (Read) permission.",
            None,
        )
    if not is_success(build_response.status_code):
        return "error", None, f"Failed to fetch build #{build_id}. Status code: {build_response.status_code}"

    build_payload = build_response.json()
    build_status = (build_payload.get("status") or "").lower()
    build_result = (build_payload.get("result") or "").lower()
    build_web_url = selected_build.get("target_url") or build_payload.get("_links", {}).get("web", {}).get("href")
    test_stage_name = TEST_PIPELINE

    timeline_url = (
        f"https://dev.azure.com/{ORGANIZATION}/{build_project}/_apis/build/builds/{build_id}/timeline?api-version=7.1"
    )
    timeline_response = http_get(timeline_url, headers=headers)
    if timeline_response.status_code == 401:
        return "error", f"Failed to fetch build timeline for stage '{test_stage_name}' (401 unauthorized).", None
    if is_success(timeline_response.status_code):
        timeline_records = timeline_response.json().get("records", [])
        stage_record = None
        for record in timeline_records:
            if (record.get("type") or "").lower() != "stage":
                continue
            if (record.get("name") or "").strip().lower() == test_stage_name.lower():
                stage_record = record
                break

        if stage_record:
            stage_state = (stage_record.get("state") or "").lower()
            stage_result = (stage_record.get("result") or "").lower()
            stage_label = stage_record.get("name") or test_stage_name

            if stage_state in ("inprogress", "pending"):
                return "progress", f"Stage '{stage_label}' is in progress. URL: {build_web_url}", None
            if stage_state in ("notstarted",):
                return "progress", f"Stage '{stage_label}' has not started yet. URL: {build_web_url}", None
            if stage_state == "completed":
                if stage_result == "succeeded":
                    return "passed", f"Stage '{stage_label}' passed. URL: {build_web_url}", None
                if stage_result in ("failed", "canceled", "partiallysucceeded"):
                    return "failed", f"Stage '{stage_label}' failed ({stage_result}). URL: {build_web_url}", None
                return "error", f"Stage '{stage_label}' completed with unknown result ({stage_result}). URL: {build_web_url}", None

            return "error", f"Stage '{stage_label}' status is unknown ({stage_state}). URL: {build_web_url}", None

        return "error", f"Stage '{test_stage_name}' not found in build timeline. URL: {build_web_url}", None

    if build_status in ("inprogress", "notstarted", "postponed", "cancelling"):
        return "progress", f"Build #{build_id} is in progress (stage '{test_stage_name}' was not evaluated). URL: {build_web_url}", None

    if build_status == "completed":
        if build_result == "succeeded":
            return "passed", f"Build #{build_id} passed (stage '{test_stage_name}' was not evaluated). URL: {build_web_url}", None
        if build_result in ("failed", "canceled", "partiallysucceeded"):
            return "failed", f"Build #{build_id} failed ({build_result}) (stage '{test_stage_name}' was not evaluated). URL: {build_web_url}", None
        return "error", f"Build #{build_id} completed with unknown result ({build_result}) (stage '{test_stage_name}' was not evaluated). URL: {build_web_url}", None

    return "error", f"Build #{build_id} status is unknown ({build_status}) (stage '{test_stage_name}' was not evaluated). URL: {build_web_url}", None


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
    log_operation(f"Run check command for PR #{taskno}")
    headers = get_auth_headers()
    project, repo_id, pr_data, cache_error = find_pr_repo_from_cache(taskno, headers)

    if not project:
        console.print(f"[bold red]❌ {cache_error}[/bold red]")
        return

    console.print(f"⏳ [bold blue]Checking PR #{taskno}...[/bold blue]")

    # 1. Fetch PR threads (comments)
    threads_url = f"{get_pr_base_url(project, repo_id)}/{taskno}/threads?api-version=7.1"
    response = http_get(threads_url, headers=headers)

    threads_ok = False
    unresolved_count = 0
    thread_error_message = None
    if response.status_code != 200:
        thread_error_message = f"Failed to fetch PR thread data. Status code: {response.status_code}"
    else:
        threads = response.json().get("value", [])
        unresolved_count = sum(1 for thread in threads if thread.get("status") in ["active", "pending"])
        threads_ok = unresolved_count == 0

    pipeline_state, pipeline_message, pipeline_error = get_pr_pipeline_status(taskno, project, repo_id, pr_data, headers)

    if thread_error_message:
        console.print(f"[bold red]❗ Comment: {thread_error_message}[/bold red]")
    elif threads_ok:
        console.print("[bold green]✅ Comment: All comment threads are resolved.[/bold green]")
    else:
        console.print(f"[bold yellow]❗ Comment: {unresolved_count} unresolved comment thread(s).[/bold yellow]")

    is_draft = bool((pr_data or {}).get("isDraft"))
    if is_draft:
        console.print("[bold yellow]❗ Draft: Pull request is draft.[/bold yellow]")
    else:
        console.print("[bold green]✅ Draft: Pull request is ready for review.[/bold green]")

    if pipeline_error:
        console.print(f"[bold red]❗ Pipeline: {pipeline_error}[/bold red]")
        return

    if pipeline_state == "passed":
        console.print(f"[bold green]✅ Pipeline: {pipeline_message}[/bold green]")
    elif pipeline_state == "progress":
        console.print(f"[bold yellow]❗ Pipeline: {pipeline_message}[/bold yellow]")
        return
    elif pipeline_state == "failed":
        console.print(f"[bold red]❗ Pipeline: {pipeline_message}[/bold red]")
        return
    else:
        console.print(f"[bold red]❗ Pipeline: {pipeline_message}[/bold red]")
        return

    if not threads_ok:
        return

    if is_draft:
        console.print("[bold yellow]❗ Draft PR cannot be approved.[/bold yellow]")
        return

    if threads_ok and pipeline_state == "passed":
        console.print("[bold green]✅ All threads are resolved[/bold green]")
    else:
        return

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

    log_operation("Run list command")
    headers = get_auth_headers()

    console.print("⏳ [cyan]Fetching active pull requests...[/cyan]")
    prs = []
    for project, repo_id in iter_project_repo_targets():
        url = (
            f"https://dev.azure.com/{ORGANIZATION}/{project}/_apis/git/repositories/"
            f"{repo_id}/pullRequests?searchCriteria.status=active&api-version=7.1"
        )
        response = http_get(url, headers=headers)

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

    update_pr_repo_cache(prs)

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
    table.add_column("Target", justify="right", style="blue")

    for pr in prs:
        pr_id = str(pr.get("pullRequestId"))
        project_name = pr.get("_project_name", "Unknown")
        repo_name = pr.get("_repo_name", "Unknown")
        title = truncate_text(pr.get("title"), TITLE_MAX_LEN)
        creator = pr.get("createdBy", {}).get("displayName", "Unknown")
        target_ref = pr.get("targetRefName", "").replace("refs/heads/", "")
        pr_url = f"https://dev.azure.com/{ORGANIZATION}/{project_name}/_git/{repo_name}/pullrequest/{pr_id}"
        pr_id_cell = Text(pr_id, style=f"link {pr_url}")

        table.add_row(pr_id_cell, project_name, repo_name, title, creator, target_ref)

    console.print(table)


def review_impl(taskno, with_ai=False):
    log_operation(f"Run review command for PR #{taskno}")
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
    log_operation(f"Run comment command for PR #{taskno}")
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
    response = http_post(threads_url, headers=headers, json=payload)
    if not is_success(response.status_code):
        console.print(f"[bold red]❌ Failed to add comment. Status code: {response.status_code}[/bold red]")
        return

    console.print("[bold green]✅ Comment added successfully.[/bold green]")


@cli.command(name="list")
@click.option("-u", "-user", "--user", "user_filter", type=str, help="Filter PR list by creator name")
@click.option("-log", "--log", "log_enabled", is_flag=True, help="Print request and operation logs")
def list_command(user_filter, log_enabled):
    """Lists active pull requests opened by configured team members."""
    set_log_enabled(log_enabled)
    list_prs_impl(user_filter=user_filter)


@cli.command(name="check")
@click.argument("taskno")
@click.option("-ai", "--ai", "with_ai", is_flag=True, help="Run mock AI review output")
@click.option("-log", "--log", "log_enabled", is_flag=True, help="Print request and operation logs")
def check_command(taskno, with_ai, log_enabled):
    """Checks unresolved comments in a PR and approves it."""
    set_log_enabled(log_enabled)
    check_impl(taskno, with_ai=with_ai)


@cli.command(name="review")
@click.argument("taskno")
@click.option("-ai", "--ai", "with_ai", is_flag=True, help="Send fetched diff to mock AI review flow")
@click.option("-log", "--log", "log_enabled", is_flag=True, help="Print request and operation logs")
def review_command(taskno, with_ai, log_enabled):
    """Fetches PR diff information and optionally runs mock AI review."""
    set_log_enabled(log_enabled)
    review_impl(taskno, with_ai=with_ai)


@cli.command(name="comment")
@click.argument("taskno")
@click.argument("comment_text", nargs=-1)
@click.option("-log", "--log", "log_enabled", is_flag=True, help="Print request and operation logs")
def comment_command(taskno, comment_text, log_enabled):
    """Adds a top-level comment thread to a PR."""
    set_log_enabled(log_enabled)
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


if __name__ == "__main__":
    cli.main(args=normalize_alias_args(sys.argv[1:]), prog_name=os.path.basename(sys.argv[0]))
