from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class GitHubReleaseError(RuntimeError):
    """Raised when GitHub release operations fail."""


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @classmethod
    def parse(cls, repository: str) -> "RepoRef":
        if "/" not in repository:
            raise GitHubReleaseError(
                f"GITHUB_REPOSITORY must look like 'owner/repo', got: {repository!r}"
            )
        owner, name = repository.split("/", 1)
        if not owner or not name:
            raise GitHubReleaseError(
                f"GITHUB_REPOSITORY must look like 'owner/repo', got: {repository!r}"
            )
        return cls(owner=owner, name=name)


class GitHubReleasesClient:
    def __init__(self, token: str, repository: str, timeout: int = 30) -> None:
        if not token:
            raise GitHubReleaseError("GITHUB_TOKEN is required")
        self.repo = RepoRef.parse(repository)
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            allowed_methods=frozenset({"GET", "POST", "PATCH", "DELETE"}),
            status_forcelist=(429, 500, 502, 503, 504),
            backoff_factor=1,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "asus-audio-release-sync",
            }
        )

    def _api_url(self, suffix: str) -> str:
        return f"https://api.github.com/repos/{self.repo.owner}/{self.repo.name}{suffix}"

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if response.status_code >= 400:
            raise GitHubReleaseError(
                f"GitHub API error {response.status_code} for {method} {url}: {response.text}"
            )
        return response

    def get_release_by_tag(self, tag: str) -> dict[str, Any] | None:
        url = self._api_url(f"/releases/tags/{tag}")
        response = self.session.get(url, timeout=self.timeout)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GitHubReleaseError(
                f"GitHub API error {response.status_code} for GET {url}: {response.text}"
            )
        return response.json()

    def create_release(
        self,
        *,
        tag: str,
        name: str,
        body: str,
        make_latest: bool,
    ) -> dict[str, Any]:
        payload = {
            "tag_name": tag,
            "name": name,
            "body": body,
            "draft": False,
            "prerelease": False,
            "make_latest": "true" if make_latest else "false",
        }
        response = self._request("POST", self._api_url("/releases"), json=payload)
        return response.json()

    def update_release(
        self,
        release_id: int,
        *,
        name: str,
        body: str,
        make_latest: bool,
    ) -> dict[str, Any]:
        payload = {
            "name": name,
            "body": body,
            "draft": False,
            "prerelease": False,
            "make_latest": "true" if make_latest else "false",
        }
        response = self._request(
            "PATCH",
            self._api_url(f"/releases/{release_id}"),
            json=payload,
        )
        return response.json()

    def ensure_release(
        self,
        *,
        tag: str,
        name: str,
        body: str,
        make_latest: bool,
    ) -> dict[str, Any]:
        current = self.get_release_by_tag(tag)
        if current is None:
            return self.create_release(tag=tag, name=name, body=body, make_latest=make_latest)
        return self.update_release(
            int(current["id"]),
            name=name,
            body=body,
            make_latest=make_latest,
        )

    def list_assets(self, release_id: int) -> list[dict[str, Any]]:
        response = self._request("GET", self._api_url(f"/releases/{release_id}/assets"))
        payload = response.json()
        if not isinstance(payload, list):
            raise GitHubReleaseError("Unexpected asset list payload from GitHub API")
        return payload

    def delete_asset(self, asset_id: int) -> None:
        self._request("DELETE", self._api_url(f"/releases/assets/{asset_id}"))

    def upload_asset(self, upload_url: str, path: Path) -> dict[str, Any]:
        upload_base = upload_url.split("{", 1)[0]
        url = f"{upload_base}?name={path.name}"
        headers = {
            "Content-Type": "application/octet-stream",
            "Accept": "application/vnd.github+json",
        }
        with path.open("rb") as fh:
            response = self.session.post(url, data=fh, headers=headers, timeout=self.timeout)
        if response.status_code >= 400:
            raise GitHubReleaseError(
                f"GitHub upload error {response.status_code} for {url}: {response.text}"
            )
        return response.json()

    def replace_asset(self, release: dict[str, Any], asset_path: Path) -> dict[str, Any]:
        release_id = int(release["id"])
        for asset in self.list_assets(release_id):
            if asset.get("name") == asset_path.name:
                self.delete_asset(int(asset["id"]))
        return self.upload_asset(release["upload_url"], asset_path)
