from __future__ import annotations

import json
import os


class PRCacheService:
    def __init__(self, cache_file: str):
        self.cache_file = cache_file

    def load(self) -> dict:
        if not os.path.exists(self.cache_file):
            return {}
        try:
            with open(self.cache_file, "r", encoding="utf-8") as cache_file:
                payload = json.load(cache_file)
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def save(self, cache_data: dict) -> None:
        try:
            with open(self.cache_file, "w", encoding="utf-8") as cache_file:
                json.dump(cache_data, cache_file, ensure_ascii=True, indent=2, sort_keys=True)
        except OSError:
            return

    def update_from_prs(self, prs: list[dict]) -> None:
        cache_data = self.load()
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
        self.save(cache_data)

    def get_repo_mapping(self, taskno: str):
        return self.load().get(str(taskno))
