# ASUS PRIME B650M-K Audio Driver Release Monitor

This repository monitors and archives **ASUS PRIME B650M-K Audio driver releases** and mirrors each package into GitHub Releases.

## Source of truth

The sync script uses this exact ASUS endpoint:

`https://www.asus.com/support/webapi/ProductV2/GetPDDrivers?website=tr&model=PRIME-B650M-K&pdhashedid=tox0or00hu0kx8ix&cpu=&osid=52&pdid=24005&siteID=www&sitelang=`

Driver categories are read from `Result.Obj`, and the script only processes the category whose `Name == "Audio"`.

## Identity and deduplication

Deduplication and state tracking use the raw ASUS `Id` exactly as returned by ASUS.

Important behavior:

- ASUS Audio `Id` is treated as an **opaque string**, not a numeric value.
- The script never converts ASUS `Id` with `int(...)` and never sorts by numeric coercion.
- `Version` is included in metadata, but **Version alone is not the stable identity**.

State is stored in `state.json` with:

- `processed_ids`: sorted list of raw ASUS Id strings.
- `items`: metadata keyed by raw ASUS Id (including `tag`, `version`, `release_date`, `filename`).

## Tag format

Each ASUS Audio package gets exactly one deterministic tag:

`audio-YYYYMMDD-<sanitized-filename-without-extension>-id<sanitized-asus-id>`

Sanitization for tag components:

- lowercase
- replace non-alphanumeric characters with `-`
- collapse repeated `-`
- trim leading/trailing `-`

Raw ASUS Id is preserved in `state.json`; only the tag uses the sanitized variant.

## Chronology preservation without polluting main history

To preserve ASUS historical ordering while keeping `main` clean, the script:

1. creates a synthetic commit object via low-level `git commit-tree`
2. stamps `GIT_AUTHOR_DATE` and `GIT_COMMITTER_DATE` to ASUS release date at `12:00:00 UTC`
3. creates and pushes a tag pointing to that synthetic commit

It does **not** create ordinary empty commits on the main branch for release timing.

## GitHub Releases behavior

For each ASUS Audio package, the sync:

- ensures one GitHub Release exists for that tag
- updates existing releases idempotently when rerun
- uploads the original ASUS package as release asset
- replaces asset cleanly if same filename already exists
- marks only the newest ASUS Audio package as GitHub `Latest`

Historical imports run oldest-first by ASUS release date. Later runs process only newly discovered ASUS Ids while still reconciling existing releases.

## Required environment variables

- `GITHUB_TOKEN`
- `GITHUB_REPOSITORY` (format: `owner/repo`)

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GITHUB_TOKEN=...
export GITHUB_REPOSITORY=owner/repo
python src/check.py
```
