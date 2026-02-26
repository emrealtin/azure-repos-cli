import json
import os
from dataclasses import dataclass


@dataclass
class Settings:
    organization: str
    pat: str
    project_repos: dict[str, list[str]]
    target_users: list[str]
    test_pipeline: str
    openai_api_key: str
    openai_model: str
    openai_base_url: str
    openai_ca_bundle: str
    openai_ssl_verify: bool
    title_max_len: int = 60
    pr_cache_file: str = ".pr_repo_cache.json"


def load_env_file(env_path: str = ".env") -> None:
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


def parse_project_repos(value: str | None) -> dict[str, list[str]]:
    if not value:
        raise ValueError("PROJECT_REPOS is required in .env (JSON object format).")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"PROJECT_REPOS must be valid JSON. Error: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("PROJECT_REPOS must be a JSON object.")

    normalized: dict[str, list[str]] = {}
    for project, repo_list in parsed.items():
        if isinstance(repo_list, list):
            normalized[str(project)] = [str(repo).strip() for repo in repo_list if str(repo).strip()]
        elif isinstance(repo_list, str) and repo_list.strip():
            normalized[str(project)] = [repo_list.strip()]

    if not normalized:
        raise ValueError("PROJECT_REPOS cannot be empty.")
    return normalized


def parse_target_users(value: str | None) -> list[str]:
    if not value:
        return []

    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(user).strip() for user in parsed if str(user).strip()]
    except json.JSONDecodeError:
        pass

    return [item.strip() for item in value.split(",") if item.strip()]


def load_settings() -> Settings:
    load_env_file()

    organization = os.getenv("ORGANIZATION", "").strip()
    pat = os.getenv("PAT", "").strip()

    if not organization:
        raise ValueError("ORGANIZATION is required in .env")
    if not pat:
        raise ValueError("PAT is required in .env")

    return Settings(
        organization=organization,
        pat=pat,
        project_repos=parse_project_repos(os.getenv("PROJECT_REPOS")),
        target_users=parse_target_users(os.getenv("TARGET_USERS")),
        test_pipeline=os.getenv("TEST_PIPELINE", "Test").strip() or "Test",
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini").strip() or "gpt-5-mini",
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com").strip() or "https://api.openai.com",
        openai_ca_bundle=os.getenv("OPENAI_CA_BUNDLE", "").strip(),
        openai_ssl_verify=os.getenv("OPENAI_SSL_VERIFY", "true").strip().lower() not in ("0", "false", "no"),
    )
