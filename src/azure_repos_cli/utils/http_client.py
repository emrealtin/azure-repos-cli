from __future__ import annotations

import warnings

import requests
from rich.console import Console
from urllib3.exceptions import InsecureRequestWarning


class HttpClient:
    def __init__(self, console: Console):
        self.console = console
        self.log_enabled = False

    def set_log_enabled(self, enabled: bool) -> None:
        self.log_enabled = bool(enabled)

    def log_operation(self, message: str) -> None:
        if self.log_enabled:
            self.console.print(f"[dim]LOG {message}[/dim]")

    def request(self, method: str, url: str, **kwargs):
        if self.log_enabled:
            self.console.print(f"[dim]HTTP {method.upper()} {url}[/dim]")
            if kwargs.get("params"):
                self.console.print(f"[dim]  params={kwargs['params']}[/dim]")
            if kwargs.get("json"):
                self.console.print(f"[dim]  json={kwargs['json']}[/dim]")

        verify_value = kwargs.get("verify", True)
        if verify_value is False:
            self.console.print("[yellow]SSL certificate verification is disabled for this HTTP request.[/yellow]")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", InsecureRequestWarning)
                response = requests.request(method=method.upper(), url=url, **kwargs)
        else:
            response = requests.request(method=method.upper(), url=url, **kwargs)

        if self.log_enabled:
            self.console.print(f"[dim]  -> status={response.status_code}[/dim]")
        return response

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        return self.request("PUT", url, **kwargs)
