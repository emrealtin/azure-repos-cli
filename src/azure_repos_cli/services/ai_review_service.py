from __future__ import annotations

import json
import re

import requests
from rich.syntax import Syntax

from azure_repos_cli.config import Settings
from azure_repos_cli.services.azure_devops_service import AzureDevOpsService
from azure_repos_cli.utils.http_client import HttpClient


class AIReviewService:
    def __init__(self, settings: Settings, azure_service: AzureDevOpsService, http: HttpClient, console):
        self.settings = settings
        self.azure_service = azure_service
        self.http = http
        self.console = console

    @staticmethod
    def extract_json_object(text):
        if not text:
            return None

        fenced_match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced_match:
            try:
                return json.loads(fenced_match.group(1))
            except json.JSONDecodeError:
                pass

        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None

    @staticmethod
    def read_response_output_text(payload):
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        chunks = []
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    chunks.append(content["text"])
        return "\n".join(chunks).strip()

    def get_ai_review_suggestions(self, file_diffs):
        if not self.settings.openai_api_key:
            return None, "OPENAI_API_KEY is missing. Set it in .env for -ai."

        diff_blocks = []
        for index, file_diff in enumerate(file_diffs, start=1):
            path = file_diff.get("path", "unknown")
            diff_text = file_diff.get("diff_text", "")
            diff_blocks.append(f"### Page {index} | File: {path}\n```diff\n{diff_text}\n```")

        system_prompt = (
            "You are a strict pull request reviewer. Return JSON only, no markdown, no explanation. "
            "Provide actionable comments from the diff."
        )
        user_prompt = (
            "Review the diff pages and propose comments.\n"
            "Output JSON schema exactly:\n"
            '{"summary":"string","comments":[{"file_path":"string","line":123,"comment":"string","severity":"low|medium|high"}]}\n'
            "Only include comments worth posting.\n\n"
            + "\n\n".join(diff_blocks)
        )

        url = f"{self.settings.openai_base_url.rstrip('/')}/v1/responses"
        payload = {
            "model": self.settings.openai_model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}", "Content-Type": "application/json"}
        verify_value = self.settings.openai_ca_bundle if self.settings.openai_ca_bundle else self.settings.openai_ssl_verify

        try:
            response = self.http.post(url, headers=headers, json=payload, timeout=120, verify=verify_value)
        except requests.exceptions.SSLError:
            return None, "OpenAI SSL verification failed. Set OPENAI_CA_BUNDLE or OPENAI_SSL_VERIFY=false (not recommended)."
        except requests.exceptions.RequestException as exc:
            return None, f"AI review request failed: {exc}"

        if not self.azure_service.is_success(response.status_code):
            return None, f"AI review request failed. Status code: {response.status_code}"

        try:
            result = response.json()
        except ValueError:
            return None, "AI review response is not valid JSON."

        parsed = self.extract_json_object(self.read_response_output_text(result))
        if not parsed:
            return None, "AI review response could not be parsed."

        summary = str(parsed.get("summary") or "").strip()
        comments = parsed.get("comments") if isinstance(parsed.get("comments"), list) else []
        valid_paths = {fd.get("path", "unknown") for fd in file_diffs}
        normalized = []
        for item in comments:
            if not isinstance(item, dict):
                continue
            file_path = str(item.get("file_path") or "").strip()
            comment_text = str(item.get("comment") or "").strip()
            severity = str(item.get("severity") or "medium").strip().lower()
            try:
                line = int(item.get("line"))
            except (TypeError, ValueError):
                continue
            if not file_path or file_path not in valid_paths or not comment_text or line <= 0:
                continue
            if severity not in ("low", "medium", "high"):
                severity = "medium"
            normalized.append({"file_path": file_path, "line": line, "comment": comment_text, "severity": severity})

        return {"summary": summary, "comments": normalized}, None

    def run_ai_review_flow(self, taskno, project, repo_id, file_diffs, headers):
        ai_result, ai_error = self.get_ai_review_suggestions(file_diffs)
        if ai_error:
            self.console.print(f"[bold red]❌ {ai_error}[/bold red]")
            return

        summary = ai_result.get("summary") or "No summary."
        comments = ai_result.get("comments") or []
        self.console.rule("[bold]AI Review Result[/bold]")
        self.console.print(f"[bold]Summary:[/bold] {summary}")

        if not comments:
            self.console.print("[bold yellow]ℹ️ No comment suggestions from AI.[/bold yellow]")
            return

        self.console.print("[bold]Suggested Comments (List)[/bold]")
        for i, item in enumerate(comments, start=1):
            self.console.print(f"{i}. [{item['severity']}] {item['file_path']}:{item['line']} -> {item['comment']}")

        self.console.print("[bold]Suggested Comments (Code Block)[/bold]")
        self.console.print(Syntax(json.dumps(comments, indent=2, ensure_ascii=False), "json", theme="monokai"))

        from rich.prompt import Confirm

        if not Confirm.ask("Do you want to post all suggested comments to this pull request?", default=False):
            self.console.print("[yellow]Skipped posting AI comments.[/yellow]")
            return

        posted = 0
        failed = 0
        for item in comments:
            error = self.azure_service.add_line_comment(
                taskno=taskno,
                project=project,
                repo_id=repo_id,
                file_path=item["file_path"],
                line_number=item["line"],
                comment_text=item["comment"],
                headers=headers,
            )
            if error:
                failed += 1
                self.console.print(f"[bold red]❌ {item['file_path']}:{item['line']} -> {error}[/bold red]")
            else:
                posted += 1
                self.console.print(f"[bold green]✅ Posted: {item['file_path']}:{item['line']}[/bold green]")

        self.console.print(f"[bold]Posted {posted} comment(s), failed {failed}.[/bold]")
