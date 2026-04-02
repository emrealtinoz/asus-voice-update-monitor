from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from github_release import GitHubReleasesClient

ASUS_ENDPOINT = (
    "https://www.asus.com/support/webapi/ProductV2/GetPDDrivers"
    "?website=tr&model=PRIME-B650M-K&pdhashedid=tox0or00hu0kx8ix"
    "&cpu=&osid=52&pdid=24005&siteID=www&sitelang="
)
DOWNLOAD_BASE = "https://dlcdnets.asus.com"
STATE_PATH = Path("state.json")


class SyncError(RuntimeError):
    """Raised for invalid external data or sync failures."""


@dataclass(frozen=True)
class AudioPackage:
    id: str
    version: str
    title: str
    description: str
    file_size: str
    release_date: date
    sha256: str | None
    relative_download_url: str
    resolved_download_url: str
    filename: str


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        backoff_factor=1,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "asus-audio-release-sync"})
    return session


def clean_text(value: str | None) -> str:
    text = (value or "").replace("\r", "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def sanitize_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", value.lower())
    token = re.sub(r"-+", "-", token).strip("-")
    return token or "x"


def parse_release_date(raw_value: str) -> date:
    try:
        return datetime.strptime(raw_value, "%Y/%m/%d").date()
    except ValueError as exc:
        raise SyncError(f"Invalid ReleaseDate value: {raw_value!r}") from exc


def build_tag(pkg: AudioPackage) -> str:
    filename_stem = Path(pkg.filename).stem
    return (
        f"audio-{pkg.release_date.strftime('%Y%m%d')}-"
        f"{sanitize_token(filename_stem)}-id{sanitize_token(pkg.id)}"
    )


def release_name(pkg: AudioPackage) -> str:
    title = pkg.title or "ASUS Audio Driver"
    if pkg.version and pkg.version not in title:
        return f"{title} ({pkg.version})"
    return title


def release_body(pkg: AudioPackage) -> str:
    lines = [
        f"Title: {pkg.title or 'Unknown title'}",
        f"Version: {pkg.version or 'Unknown'}",
        f"Release date: {pkg.release_date.isoformat()}",
        f"File size: {pkg.file_size or 'Unknown'}",
    ]
    if pkg.sha256:
        lines.append(f"SHA-256: {pkg.sha256}")
    lines.extend(
        [
            f"ASUS download path: {pkg.relative_download_url}",
            "",
            "Description:",
            pkg.description or "No description provided.",
        ]
    )
    return "\n".join(lines)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"processed_ids": [], "items": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SyncError("state.json must contain a JSON object")
    processed_ids = data.get("processed_ids", [])
    items = data.get("items", {})
    if not isinstance(processed_ids, list) or not isinstance(items, dict):
        raise SyncError("state.json schema invalid: expected processed_ids list and items object")
    normalized_ids = [str(item) for item in processed_ids]
    normalized_items = {str(key): value for key, value in items.items()}
    return {"processed_ids": normalized_ids, "items": normalized_items}


def save_state(path: Path, state: dict) -> None:
    state["processed_ids"] = sorted({str(item) for item in state.get("processed_ids", [])})
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch_audio_packages(session: requests.Session) -> list[AudioPackage]:
    response = session.get(ASUS_ENDPOINT, timeout=30)
    if response.status_code >= 400:
        raise SyncError(f"ASUS API request failed ({response.status_code}): {response.text}")

    payload = response.json()
    if not isinstance(payload, dict):
        raise SyncError("ASUS API returned a non-object JSON payload")

    result = payload.get("Result")
    if not isinstance(result, dict):
        raise SyncError("ASUS API payload missing Result object")

    success_candidates = [
        payload.get("IsSuccess"),
        payload.get("Success"),
        result.get("IsSuccess"),
        result.get("Success"),
    ]
    if any(candidate is False for candidate in success_candidates):
        raise SyncError(f"ASUS API indicated failure: {json.dumps(payload)[:500]}")

    categories = result.get("Obj")
    if not isinstance(categories, list):
        raise SyncError("ASUS API Result.Obj is missing or not a list")

    audio_category = None
    for category in categories:
        if isinstance(category, dict) and category.get("Name") == "Audio":
            audio_category = category
            break
    if audio_category is None:
        raise SyncError("ASUS API payload does not include Audio category")

    files = audio_category.get("Files")
    if not isinstance(files, list):
        raise SyncError("ASUS Audio category is missing Files array")

    packages: list[AudioPackage] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("Id")
        raw_release_date = entry.get("ReleaseDate")
        raw_relative_url = (entry.get("DownloadUrl") or {}).get("Global")
        if raw_id is None or not raw_release_date or not raw_relative_url:
            logging.warning("Skipping malformed ASUS Audio item: %s", entry)
            continue

        package_id = str(raw_id)
        relative_url = str(raw_relative_url).strip()
        resolved_url = urljoin(DOWNLOAD_BASE, relative_url)
        filename = Path(urlparse(resolved_url).path).name
        if not filename:
            raise SyncError(f"Unable to derive filename from URL: {resolved_url}")

        sha256_value = clean_text(str(entry.get("sha256", "")))
        sha256 = sha256_value if sha256_value else None

        packages.append(
            AudioPackage(
                id=package_id,
                version=clean_text(str(entry.get("Version", ""))),
                title=clean_text(str(entry.get("Title", ""))),
                description=clean_text(str(entry.get("Description", ""))),
                file_size=clean_text(str(entry.get("FileSize", ""))),
                release_date=parse_release_date(str(raw_release_date)),
                sha256=sha256,
                relative_download_url=relative_url,
                resolved_download_url=resolved_url,
                filename=filename,
            )
        )

    if not packages:
        raise SyncError("No audio packages discovered from ASUS API")

    return sorted(packages, key=lambda p: (p.release_date, p.id))


def verify_historical_entries(packages: list[AudioPackage]) -> None:
    expected = {
        (date(2025, 12, 18), "6.0.9888.1"),
        (date(2024, 8, 2), "6.0.9700.1"),
        (date(2023, 5, 29), "6.0.9350.1"),
    }
    got = {(pkg.release_date, pkg.version) for pkg in packages}
    missing = sorted(expected - got)
    if missing:
        logging.warning("Historical ASUS Audio entries not found: %s", missing)


def run_git(*args: str, env: dict[str, str] | None = None) -> str:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
        env=merged_env,
    )
    if result.returncode != 0:
        raise SyncError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def ensure_tag_for_date(tag: str, release_date: date) -> None:
    tag_exists = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
        check=False,
        capture_output=True,
        text=True,
    ).returncode == 0
    if tag_exists:
        return

    tree_hash = run_git("rev-parse", "HEAD^{tree}")
    parent_hash = run_git("rev-parse", "HEAD")
    timestamp = datetime(
        release_date.year,
        release_date.month,
        release_date.day,
        12,
        0,
        0,
        tzinfo=timezone.utc,
    )
    iso_timestamp = timestamp.isoformat().replace("+00:00", "Z")
    env = {
        "GIT_AUTHOR_DATE": iso_timestamp,
        "GIT_COMMITTER_DATE": iso_timestamp,
    }
    commit_hash = run_git(
        "commit-tree",
        tree_hash,
        "-p",
        parent_hash,
        "-m",
        f"Synthetic commit for {tag}",
        env=env,
    )
    run_git("tag", "-a", tag, commit_hash, "-m", f"ASUS audio package {tag}")
    run_git("push", "origin", f"refs/tags/{tag}")


def download_package(session: requests.Session, pkg: AudioPackage, download_dir: Path) -> Path:
    destination = download_dir / pkg.filename
    with session.get(pkg.resolved_download_url, timeout=120, stream=True, allow_redirects=True) as response:
        if response.status_code >= 400:
            raise SyncError(
                f"Failed to download ASUS package {pkg.resolved_download_url} ({response.status_code})"
            )
        with destination.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
    return destination


def sync() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token:
        raise SyncError("GITHUB_TOKEN is required")
    if not repository:
        raise SyncError("GITHUB_REPOSITORY is required")

    session = make_session()
    gh = GitHubReleasesClient(token=token, repository=repository)

    packages = fetch_audio_packages(session)
    verify_historical_entries(packages)
    newest_package = max(packages, key=lambda p: (p.release_date, p.id))

    state = load_state(STATE_PATH)
    processed_ids = set(state["processed_ids"])
    items: dict[str, dict] = dict(state["items"])

    with tempfile.TemporaryDirectory(prefix="asus-audio-") as temp_dir:
        download_dir = Path(temp_dir)

        for pkg in packages:
            is_newest = pkg.id == newest_package.id
            should_process = pkg.id not in processed_ids
            tag = build_tag(pkg)

            if should_process:
                logging.info("Processing new package %s (%s)", pkg.id, pkg.version)
            else:
                logging.info("Reconciling existing package %s (%s)", pkg.id, pkg.version)

            ensure_tag_for_date(tag, pkg.release_date)

            release = gh.ensure_release(
                tag=tag,
                name=release_name(pkg),
                body=release_body(pkg),
                make_latest=is_newest,
            )

            if should_process:
                package_path = download_package(session, pkg, download_dir)
                gh.replace_asset(release, package_path)

            processed_ids.add(pkg.id)
            items[pkg.id] = {
                "tag": tag,
                "version": pkg.version,
                "release_date": pkg.release_date.isoformat(),
                "filename": pkg.filename,
                "title": pkg.title,
            }

    state["processed_ids"] = sorted(processed_ids)
    state["items"] = items
    save_state(STATE_PATH, state)
    logging.info("Sync complete: %d packages tracked.", len(processed_ids))


if __name__ == "__main__":
    sync()
