from __future__ import annotations

import base64
import difflib
import json
from urllib.parse import parse_qs, urlparse

from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from azure_repos_cli.config import Settings
from azure_repos_cli.services.pr_cache_service import PRCacheService
from azure_repos_cli.utils.http_client import HttpClient


class AzureDevOpsService:
    def __init__(self, settings: Settings, http: HttpClient, cache: PRCacheService):
        self.settings = settings
        self.http = http
        self.cache = cache

    @staticmethod
    def is_success(status_code: int) -> bool:
        return 200 <= status_code < 300

    @staticmethod
    def truncate_text(value, max_len):
        text = (value or "").strip()
        if len(text) <= max_len:
            return text
        if max_len <= 3:
            return text[:max_len]
        return text[: max_len - 3].rstrip() + "..."

    def get_auth_headers(self) -> dict:
        credentials = f":{self.settings.pat}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        return {"Authorization": f"Basic {encoded_credentials}", "Content-Type": "application/json"}

    def iter_project_repo_targets(self):
        for project, repo_ids in self.settings.project_repos.items():
            for repo_id in repo_ids:
                yield project, repo_id

    def get_pr_base_url(self, project: str, repo_id: str) -> str:
        return f"https://dev.azure.com/{self.settings.organization}/{project}/_apis/git/repositories/{repo_id}/pullRequests"

    def get_targets_text(self) -> str:
        return ", ".join([f"{project}/{repo_id}" for project, repo_id in self.iter_project_repo_targets()])

    def render_colored_diff(self, diff_text: str):
        syntax = Syntax(
            diff_text,
            "diff",
            theme="monokai",
            line_numbers=True,
            word_wrap=False,
            background_color="default",
        )
        return Panel(syntax, border_style="dim", padding=(0, 1))

    def get_current_user(self, headers: dict):
        self.http.log_operation("Fetch current user")
        url = f"https://dev.azure.com/{self.settings.organization}/_apis/connectionData?api-version=7.1-preview.1"
        response = self.http.get(url, headers=headers)
        if not self.is_success(response.status_code):
            return None, f"Failed to fetch current user info. Status code: {response.status_code}"

        user = response.json().get("authenticatedUser", {})
        user_id = user.get("id")
        if not user_id:
            return None, "User id was not found in connectionData."
        return user, None

    def find_pr_repo(self, taskno: str, headers: dict):
        self.http.log_operation(f"Find PR #{taskno} in configured repositories")
        for project, repo_id in self.iter_project_repo_targets():
            pr_url = f"{self.get_pr_base_url(project, repo_id)}/{taskno}?api-version=7.1"
            response = self.http.get(pr_url, headers=headers)
            if response.status_code == 200:
                return project, repo_id, response.json()
        return None, None, None

    def find_pr_repo_from_cache(self, taskno: str, headers: dict):
        cache_entry = self.cache.get_repo_mapping(taskno)
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

        pr_url = f"{self.get_pr_base_url(project, repo_id)}/{taskno}?api-version=7.1"
        response = self.http.get(pr_url, headers=headers)
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

    @staticmethod
    def normalize_thread_status(status_value):
        if isinstance(status_value, str):
            return status_value.strip().lower()
        if isinstance(status_value, int):
            enum_map = {1: "unknown", 2: "active", 3: "fixed", 4: "wontfix", 5: "closed", 6: "bydesign", 7: "pending"}
            return enum_map.get(status_value, str(status_value))
        return str(status_value).strip().lower()

    def is_unresolved_comment_thread(self, thread: dict) -> bool:
        comments = thread.get("comments") or []
        has_user_comment = any((comment or {}).get("commentType") == 1 for comment in comments)
        if not has_user_comment:
            return False
        return self.normalize_thread_status(thread.get("status")) in ("active", "pending")

    def fetch_file_content_at_commit(self, project, repo_id, file_path, commit_id, headers):
        if not commit_id:
            return "", None

        url = f"https://dev.azure.com/{self.settings.organization}/{project}/_apis/git/repositories/{repo_id}/items"
        params = {
            "path": file_path,
            "versionDescriptor.versionType": "commit",
            "versionDescriptor.version": commit_id,
            "includeContent": "true",
            "api-version": "7.1",
        }
        response = self.http.get(url, headers=headers, params=params)

        if response.status_code == 404:
            return "", None
        if not self.is_success(response.status_code):
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

    def get_pr_diff(self, taskno, project, repo_id, headers):
        self.http.log_operation(f"Fetch diff for PR #{taskno}")
        iterations_url = f"{self.get_pr_base_url(project, repo_id)}/{taskno}/iterations?api-version=7.1"
        iterations_response = self.http.get(iterations_url, headers=headers)
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
            f"{self.get_pr_base_url(project, repo_id)}/{taskno}/iterations/{latest_iteration}/changes"
            f"?api-version=7.1&$top=2000"
        )
        changes_response = self.http.get(changes_url, headers=headers)
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
                fetched_new, err = self.fetch_file_content_at_commit(project, repo_id, new_path, target_commit, headers)
                if err:
                    return None, err
                new_content = fetched_new or ""
            elif "delete" in change_type:
                fetched_old, err = self.fetch_file_content_at_commit(project, repo_id, old_path, base_commit, headers)
                if err:
                    return None, err
                old_content = fetched_old or ""
            else:
                fetched_old, err = self.fetch_file_content_at_commit(project, repo_id, old_path, base_commit, headers)
                if err:
                    return None, err
                fetched_new, err = self.fetch_file_content_at_commit(project, repo_id, new_path, target_commit, headers)
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

            file_diffs.append({"path": new_path, "change_type": change_type, "diff_text": "\n".join(diff_lines)})

        return file_diffs, None

    def get_reviewer_vote(self, taskno, project, repo_id, reviewer_id, headers):
        reviewer_url = f"{self.get_pr_base_url(project, repo_id)}/{taskno}/reviewers/{reviewer_id}?api-version=7.1"
        response = self.http.get(reviewer_url, headers=headers)
        if not self.is_success(response.status_code):
            return None, f"Failed to fetch reviewer vote. Status code: {response.status_code}"
        return response.json().get("vote"), None

    def approve_pr(self, taskno, project, repo_id, headers):
        user, error = self.get_current_user(headers)
        if error:
            return error

        reviewer_id = user.get("id")
        reviewer_url = f"{self.get_pr_base_url(project, repo_id)}/{taskno}/reviewers/{reviewer_id}?api-version=7.1"
        response = self.http.put(reviewer_url, json={"vote": 10}, headers=headers)
        if not self.is_success(response.status_code):
            return f"Approve request failed. Status code: {response.status_code}"
        return None

    def add_general_comment(self, taskno, project, repo_id, comment_text, headers):
        threads_url = f"{self.get_pr_base_url(project, repo_id)}/{taskno}/threads?api-version=7.1"
        payload = {"comments": [{"parentCommentId": 0, "content": comment_text, "commentType": 1}], "status": "active"}
        response = self.http.post(threads_url, headers=headers, json=payload)
        if not self.is_success(response.status_code):
            return f"Failed to add comment. Status code: {response.status_code}"
        return None

    def add_line_comment(self, taskno, project, repo_id, file_path, line_number, comment_text, headers):
        threads_url = f"{self.get_pr_base_url(project, repo_id)}/{taskno}/threads?api-version=7.1"
        payload = {
            "comments": [{"parentCommentId": 0, "content": comment_text, "commentType": 1}],
            "status": "active",
            "threadContext": {
                "filePath": file_path,
                "rightFileStart": {"line": line_number, "offset": 1},
                "rightFileEnd": {"line": line_number, "offset": 1},
            },
        }
        response = self.http.post(threads_url, headers=headers, json=payload)
        if not self.is_success(response.status_code):
            return f"Failed to add review comment. Status code: {response.status_code}"
        return None

    def list_active_prs(self, headers):
        prs = []
        for project, repo_id in self.iter_project_repo_targets():
            url = (
                f"https://dev.azure.com/{self.settings.organization}/{project}/_apis/git/repositories/"
                f"{repo_id}/pullRequests?searchCriteria.status=active&api-version=7.1"
            )
            response = self.http.get(url, headers=headers)
            if response.status_code != 200:
                continue
            repo_prs = response.json().get("value", [])
            for pr in repo_prs:
                pr["_project_name"] = project
                pr["_repo_name"] = pr.get("repository", {}).get("name") or repo_id
            prs.extend(repo_prs)
        return prs

    def get_pr_pipeline_status(self, taskno, project, repo_id, pr_data, headers):
        statuses_url = f"{self.get_pr_base_url(project, repo_id)}/{taskno}/statuses?api-version=7.1"
        response = self.http.get(statuses_url, headers=headers)
        if not self.is_success(response.status_code):
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
            if state == "expired":
                return "failed", f"{label} status is expired (from PR status)."
            if state in ("failed", "error"):
                return "failed", f"{label} status is failed (from PR status)."
            if state in ("pending", "notset", ""):
                return "progress", f"{label} status is in progress (from PR status)."
            return "error", f"{label} status is unknown ({state}) (from PR status)."

        def infer_status_from_policy_evaluations():
            project_id = (pr_data or {}).get("repository", {}).get("project", {}).get("id") or (
                pr_data or {}
            ).get("repository", {}).get("project", {}).get("name")
            if not project_id:
                return None

            artifact_id = f"vstfs:///CodeReview/CodeReviewId/{project_id}/{taskno}"
            policy_url = f"https://dev.azure.com/{self.settings.organization}/{project}/_apis/policy/evaluations"
            params = {"artifactId": artifact_id, "api-version": "7.1-preview.1"}
            policy_response = self.http.get(policy_url, headers=headers, params=params)
            if not self.is_success(policy_response.status_code):
                return None

            evaluations = policy_response.json().get("value", [])
            if not evaluations:
                return None

            build_related = []
            for evaluation in evaluations:
                config = evaluation.get("configuration", {}) or {}
                config_type = (config.get("type", {}) or {}).get("displayName", "")
                settings_text = json.dumps(config.get("settings", {})).lower()
                if "build" in config_type.lower() or "build" in settings_text:
                    build_related.append(evaluation)

            if not build_related:
                return None

            raw_text = json.dumps(build_related).lower()
            if "expired" in raw_text:
                return "failed", "Pipeline status is expired (from policy evaluation)."
            if any((item.get("status") or "").lower() in ("running", "queued") for item in build_related):
                return "progress", "Pipeline is in progress (from policy evaluation)."
            if any((item.get("status") or "").lower() in ("rejected", "broken") for item in build_related):
                return "failed", "Pipeline failed (from policy evaluation)."
            if all((item.get("status") or "").lower() in ("approved", "notapplicable") for item in build_related):
                return "passed", "Pipeline passed (from policy evaluation)."
            return None

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
            target_url = status.get("targetUrl") or status.get("details") or status.get("_links", {}).get("target", {}).get("href") or status.get("_links", {}).get("web", {}).get("href")
            build_id, build_project = extract_build_ref(target_url)
            if build_id:
                build_candidates.append({
                    "build_id": build_id,
                    "build_project": build_project or project,
                    "created_at": status.get("creationDate") or "",
                    "target_url": target_url,
                })

        if not build_candidates:
            builds_url = f"https://dev.azure.com/{self.settings.organization}/{project}/_apis/build/builds"
            params = {
                "repositoryId": repo_id,
                "repositoryType": "TfsGit",
                "reasonFilter": "pullRequest",
                "queryOrder": "queueTimeDescending",
                "$top": 50,
                "api-version": "7.1",
            }
            builds_response = self.http.get(builds_url, headers=headers, params=params)
            if builds_response.status_code == 401:
                fallback_state, fallback_message = infer_status_from_pr_statuses(statuses)
                return fallback_state, f"{fallback_message} Build API returned 401. PAT needs Build (Read) permission.", None
            if builds_response.status_code == 400:
                retry_params = {
                    "reasonFilter": "pullRequest",
                    "queryOrder": "queueTimeDescending",
                    "$top": 100,
                    "api-version": "7.1",
                }
                builds_response = self.http.get(builds_url, headers=headers, params=retry_params)
            if not self.is_success(builds_response.status_code):
                policy_fallback = infer_status_from_policy_evaluations()
                if policy_fallback:
                    state, message = policy_fallback
                    return state, message, None
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
                policy_fallback = infer_status_from_policy_evaluations()
                if policy_fallback:
                    state, message = policy_fallback
                    return state, message, None
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
        build_url = f"https://dev.azure.com/{self.settings.organization}/{build_project}/_apis/build/builds/{build_id}?api-version=7.1"
        build_response = self.http.get(build_url, headers=headers)
        if build_response.status_code == 401:
            fallback_state, fallback_message = infer_status_from_pr_statuses(statuses)
            return fallback_state, f"{fallback_message} Build API returned 401. PAT needs Build (Read) permission.", None
        if not self.is_success(build_response.status_code):
            return "error", None, f"Failed to fetch build #{build_id}. Status code: {build_response.status_code}"

        build_payload = build_response.json()
        build_status = (build_payload.get("status") or "").lower()
        build_result = (build_payload.get("result") or "").lower()
        build_web_url = selected_build.get("target_url") or build_payload.get("_links", {}).get("web", {}).get("href")
        test_stage_name = self.settings.test_pipeline

        timeline_url = f"https://dev.azure.com/{self.settings.organization}/{build_project}/_apis/build/builds/{build_id}/timeline?api-version=7.1"
        timeline_response = self.http.get(timeline_url, headers=headers)
        if timeline_response.status_code == 401:
            return "error", f"Failed to fetch build timeline for stage '{test_stage_name}' (401 unauthorized).", None
        if self.is_success(timeline_response.status_code):
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
                    if stage_result in ("failed", "canceled", "partiallysucceeded", "expired"):
                        return "failed", f"Stage '{stage_label}' failed ({stage_result}). URL: {build_web_url}", None
                    return "error", f"Stage '{stage_label}' completed with unknown result ({stage_result}). URL: {build_web_url}", None

                return "error", f"Stage '{stage_label}' status is unknown ({stage_state}). URL: {build_web_url}", None

            return "error", f"Stage '{test_stage_name}' not found in build timeline. URL: {build_web_url}", None

        if build_status in ("inprogress", "notstarted", "postponed", "cancelling"):
            return "progress", f"Build #{build_id} is in progress (stage '{test_stage_name}' was not evaluated). URL: {build_web_url}", None

        if build_status == "completed":
            if build_result == "succeeded":
                return "passed", f"Build #{build_id} passed (stage '{test_stage_name}' was not evaluated). URL: {build_web_url}", None
            if build_result in ("failed", "canceled", "partiallysucceeded", "expired"):
                return "failed", f"Build #{build_id} failed ({build_result}) (stage '{test_stage_name}' was not evaluated). URL: {build_web_url}", None
            return "error", f"Build #{build_id} completed with unknown result ({build_result}) (stage '{test_stage_name}' was not evaluated). URL: {build_web_url}", None

        return "error", f"Build #{build_id} status is unknown ({build_status}) (stage '{test_stage_name}' was not evaluated). URL: {build_web_url}", None
