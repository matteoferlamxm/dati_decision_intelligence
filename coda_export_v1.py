"""
Given a link copied from the browser (Superhuman Docs / Coda), exports to
CSV or Markdown the table it points to, or all the tables of the page it
points to.

"Standard" variant: cell contents are NOT modified. Every value returned by
the API is written as is (embedded newlines, lists and dicts included). With
--md the only adjustments needed to keep the Markdown table valid are: line
breaks become <br> and "|" is escaped. For a single line of text per table
row use coda_export_v2.py.

Only CODA_API_KEY is needed in the .env file (see .env.example).

Usage:
    python coda_export_v1.py [--csv | --md] "<link>" [--output-dir FOLDER]

    --csv   export to .csv (default)
    --md    export to .md (title + Markdown table)

The link can point to:
    - a table (with the #Table-Name_tuXXXX anchor) -> exports that table
    - a page                                       -> exports all its tables
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import pandas as pd
import requests
from dotenv import load_dotenv

API_BASE_URL = "https://coda.io/apis/v1"
DEFAULT_PAGE_SIZE = 200
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 2
PAGE_TABLE_TYPES = "table,view"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("coda_export_v1")


class CodaApiError(Exception):
    """Error in calls to the Coda API."""


def load_api_key() -> str:
    """Read CODA_API_KEY from the .env file in the script's folder."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        logger.error(
            ".env file not found in %s. Copy .env.example to .env and "
            "set CODA_API_KEY.",
            env_path,
        )
        sys.exit(1)

    load_dotenv(dotenv_path=env_path)
    api_key = os.getenv("CODA_API_KEY", "").strip()
    if not api_key:
        logger.error("CODA_API_KEY is not set in the .env file.")
        sys.exit(1)
    return api_key


def build_session(api_key: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    )
    return session


def request_with_retry(
    session: requests.Session, method: str, url: str, **kwargs: Any
) -> requests.Response:
    """Perform the request with exponential backoff on 429 (rate limit) and 5xx."""
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(1, MAX_RETRIES + 1):
        response = session.request(method, url, timeout=30, **kwargs)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            wait_seconds = float(retry_after) if retry_after else backoff
            logger.warning(
                "Rate limit (429). Waiting %.1f s (attempt %d/%d)...",
                wait_seconds,
                attempt,
                MAX_RETRIES,
            )
        elif response.status_code >= 500:
            wait_seconds = backoff
            logger.warning(
                "Server error (%d). Retrying in %.1f s (%d/%d)...",
                response.status_code,
                wait_seconds,
                attempt,
                MAX_RETRIES,
            )
        else:
            return response

        time.sleep(wait_seconds)
        backoff *= 2

    raise CodaApiError(
        f"Maximum number of attempts ({MAX_RETRIES}) reached for {url}."
    )


def get_json(
    session: requests.Session, url: str, error_label: str, **kwargs: Any
) -> dict[str, Any]:
    """GET with uniform error handling; returns the response JSON."""
    response = request_with_retry(session, "GET", url, **kwargs)
    if response.status_code != 200:
        raise CodaApiError(
            f"{error_label} ({response.status_code}): {response.text}"
        )
    return response.json()


def resolve_target(
    session: requests.Session, browser_url: str
) -> tuple[str, str, dict[str, Any]]:
    """
    Resolve the browser link via GET /resolveBrowserLink.
    Returns (doc_id, resource_type, resource). The response has the shape
    {"resource": {"type", "id", "name", "href"}}; the doc ID is taken from
    the resource's API href (.../docs/<docId>/...).
    """
    payload = get_json(
        session,
        f"{API_BASE_URL}/resolveBrowserLink",
        "Error resolving the link",
        params={"url": browser_url},
    )
    resource = payload.get("resource") or {}
    match = re.search(r"/docs/([^/?#]+)", resource.get("href", ""))
    if not resource.get("id") or not match:
        raise CodaApiError(
            f"Unexpected response from resolveBrowserLink: {payload}"
        )
    return match.group(1), resource.get("type", ""), resource


def get_table_details(
    session: requests.Session, doc_id: str, table_id: str
) -> dict[str, Any]:
    url = f"{API_BASE_URL}/docs/{doc_id}/tables/{quote(table_id, safe='')}"
    return get_json(
        session, url, f"Error fetching table '{table_id}'"
    )


def list_page_tables(
    session: requests.Session, doc_id: str, page_id: str
) -> list[dict[str, Any]]:
    """
    Tables (and views) that belong to the page. The API has no per-page
    filter: list the doc's tables (paginated) and keep those with
    parent.id == page_id. The `parent` field is not guaranteed in the list:
    if it is missing, read it from the table detail.
    """
    tables: list[dict[str, Any]] = []
    url = f"{API_BASE_URL}/docs/{doc_id}/tables"
    params: dict[str, Any] | None = {"tableTypes": PAGE_TABLE_TYPES}

    while url:
        payload = get_json(
            session, url, "Error fetching tables", params=params
        )
        for table in payload.get("items", []):
            parent = table.get("parent")
            if not parent:
                parent = get_table_details(session, doc_id, table["id"]).get(
                    "parent"
                )
            if (parent or {}).get("id") == page_id:
                tables.append(table)
        # nextPageLink already contains all the parameters (including pageToken)
        url = payload.get("nextPageLink")
        params = None

    return tables


def fetch_table_rows(
    session: requests.Session, doc_id: str, table_id: str
) -> list[dict[str, Any]]:
    """Download all the rows of a table following pagination."""
    rows: list[dict[str, Any]] = []
    url: str | None = (
        f"{API_BASE_URL}/docs/{doc_id}/tables/{quote(table_id, safe='')}/rows"
    )
    params: dict[str, Any] | None = {
        "useColumnNames": "true",
        "valueFormat": "simple",
        "limit": DEFAULT_PAGE_SIZE,
    }

    page_number = 1
    while url:
        payload = get_json(
            session, url, "Error fetching rows", params=params
        )
        items = payload.get("items", [])
        rows.extend(items)
        logger.info(
            "  page %d: %d rows downloaded (total: %d)",
            page_number,
            len(items),
            len(rows),
        )
        url = payload.get("nextPageLink")
        params = None
        page_number += 1

    return rows


def rows_to_dataframe(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    """
    Coda rows -> DataFrame, with the _coda_row_* metadata columns at the end.
    No normalization: values (including lists or dicts) go to pandas as they
    come from the API, and to_csv decides how to write them.
    """
    records = []
    for row in rows:
        record = dict(row.get("values", {}))
        record["_coda_row_id"] = row.get("id")
        record["_coda_row_index"] = row.get("index")
        records.append(record)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    meta_cols = ["_coda_row_id", "_coda_row_index"]
    return df[[c for c in df.columns if c not in meta_cols] + meta_cols]


def safe_name(label: str) -> str:
    """Make a label usable as a file/folder name."""
    return "".join(
        c if c.isalnum() or c in ("-", "_") else "_" for c in label
    ).strip("_")


def build_output_path(
    table_name: str,
    table_id: str,
    output_dir: Path,
    used_names: set[Path],
    extension: str,
) -> Path:
    """
    Build <output_dir>/<table_name>_<timestamp>.<extension>, creating the
    folder. If a table with the same name was already saved in this run,
    append the ID so the file is not overwritten.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    base = safe_name(table_name) or safe_name(table_id)
    if output_dir / base in used_names:
        base = f"{base}_{safe_name(table_id)}"
    used_names.add(output_dir / base)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"{base}_{timestamp}.{extension}"


def export_table_to_csv(
    df: pd.DataFrame,
    table_name: str,
    table_id: str,
    output_dir: Path,
    used_names: set[Path],
) -> Path:
    """Save the DataFrame to <output_dir>/<table_name>_<timestamp>.csv."""
    file_path = build_output_path(
        table_name, table_id, output_dir, used_names, "csv"
    )
    df.to_csv(file_path, index=False, encoding="utf-8-sig")
    return file_path


def _markdown_cell(value: Any) -> str:
    """
    Cell text for a Markdown table. The content is not normalized (lists and
    dicts are written as str(value), like pandas.to_csv does); only what
    would break the table is adjusted: "|" is escaped and line breaks become
    <br>. Missing values (None / NaN) become an empty string.
    """
    if isinstance(value, (list, tuple, dict)):
        text = str(value)
    elif value is None or pd.isna(value):
        return ""
    else:
        text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text.replace("|", "\\|").replace("\n", "<br>")


def dataframe_to_markdown(df: pd.DataFrame, title: str) -> str:
    """
    Render the DataFrame as a title plus a GitHub-flavoured Markdown table.
    Written by hand to avoid extra dependencies (DataFrame.to_markdown needs
    tabulate).
    """
    headers = [_markdown_cell(column) for column in df.columns]
    lines = [
        f"# {_markdown_cell(title)}",
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for record in df.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(_markdown_cell(v) for v in record) + " |")
    return "\n".join(lines) + "\n"


def export_table_to_markdown(
    df: pd.DataFrame,
    table_name: str,
    table_id: str,
    output_dir: Path,
    used_names: set[Path],
) -> Path:
    """Save the DataFrame to <output_dir>/<table_name>_<timestamp>.md."""
    file_path = build_output_path(
        table_name, table_id, output_dir, used_names, "md"
    )
    file_path.write_text(dataframe_to_markdown(df, table_name), encoding="utf-8")
    return file_path


EXPORTERS = {
    "csv": export_table_to_csv,
    "md": export_table_to_markdown,
}


def export_tables(
    session: requests.Session,
    doc_id: str,
    tables: list[dict[str, Any]],
    output_dir: Path,
    fmt: str = "csv",
) -> int:
    """
    Download and save (as `fmt`: "csv" or "md") the given tables (dicts with
    at least id and name). Returns 0 if everything went well, 1 if at least
    one table failed.
    """
    export_table = EXPORTERS[fmt]
    exit_code = 0
    used_names: set[Path] = set()
    for table in tables:
        name = table.get("name") or table["id"]
        logger.info("Exporting table '%s' (%s)...", name, table["id"])
        try:
            rows = fetch_table_rows(session, doc_id, table["id"])
        except CodaApiError as exc:
            logger.error("Error exporting '%s': %s", name, exc)
            exit_code = 1
            continue

        df = rows_to_dataframe(rows)
        if df.empty:
            logger.warning(
                "Table '%s' has no rows: %s file not created.", name, fmt.upper()
            )
            continue

        file_path = export_table(
            df, name, table["id"], table.get("output_dir", output_dir), used_names
        )
        logger.info(
            "Table '%s' exported: %d rows, %d columns -> %s",
            name,
            len(df),
            len(df.columns),
            file_path,
        )
    return exit_code


def run(
    session: requests.Session,
    browser_url: str,
    output_dir: Path,
    fmt: str = "csv",
) -> int:
    """Resolve the link and choose what to export based on the resource type."""
    logger.info("Resolving the link...")
    try:
        doc_id, resource_type, resource = resolve_target(session, browser_url)

        if resource_type == "table":
            logger.info("The link points to table '%s'.", resource.get("name"))
            return export_tables(session, doc_id, [resource], output_dir, fmt)

        if resource_type == "page":
            page_name = resource.get("name") or resource["id"]
            logger.info("The link points to page '%s'.", page_name)
            tables = list_page_tables(session, doc_id, resource["id"])
            if not tables:
                logger.warning("No tables found in page '%s'.", page_name)
                return 0
            logger.info("Tables found in the page: %d", len(tables))
            page_dir = output_dir / (safe_name(page_name) or resource["id"])
            for table in tables:
                table["output_dir"] = page_dir
            return export_tables(session, doc_id, tables, output_dir, fmt)
    except CodaApiError as exc:
        logger.error("%s", exc)
        return 1

    logger.error(
        "The link points to a resource of type '%s': use the link of a "
        "table (with the #Table-Name_tuXXXX anchor) or of a page.",
        resource_type,
    )
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export to CSV or Markdown the table (or the tables of a page) "
            "pointed to by a Coda / Superhuman Docs link (standard export, "
            "cell contents are not normalized)."
        )
    )
    parser.add_argument("url", help="Link copied from the browser (table or page).")
    format_group = parser.add_mutually_exclusive_group()
    format_group.add_argument(
        "--csv",
        dest="fmt",
        action="store_const",
        const="csv",
        help="Export to .csv (default).",
    )
    format_group.add_argument(
        "--md",
        dest="fmt",
        action="store_const",
        const="md",
        help="Export to .md (title + Markdown table).",
    )
    parser.set_defaults(fmt="csv")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Destination folder for the exported files (default: ./output).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    session = build_session(load_api_key())
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(__file__).resolve().parent / "output"
    )
    sys.exit(run(session, args.url, output_dir, args.fmt))


if __name__ == "__main__":
    main()
