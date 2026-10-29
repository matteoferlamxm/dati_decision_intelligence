#!/usr/bin/env python3
"""
coda_export_v2.py  (EXPERIMENTAL)
=================================

Given a link to a Coda / Superhuman Docs table, reads the WHOLE page the
table lives in (exported as markdown) and saves it to a single .md file.

When a cell of the table refers to another section of the page ("see
Country Weights below") and that section links to an external spreadsheet
that has been placed in the local import folder, the spreadsheet is
embedded as an HTML table inside the cell that makes the reference (table
inside table). Numbers are written as Excel displays them. The links to the
original files are removed from the output (only their label is kept).

Only CODA_API_KEY is needed in the .env file (see .env.example).

Usage:
    python coda_export_v2.py "<table link>" [--import-dir import] [--output-dir output]

External files:
    The spreadsheets linked from the page are not downloaded (they are
    private): put them as .xlsx files in the import folder and map the
    Google file ID (the part after /d/ in the link) to the file name in
    import/mapping.json:
        {"1-Ubu2JUC0y41_AOs9wJfNe_9ZFGdFd2z": "my_file.xlsx"}
    Links without an entry are left as a placeholder and reported in the log.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import re
import sys
import time
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from openpyxl import load_workbook

API_BASE_URL = "https://coda.io/apis/v1"
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 2
EXPORT_POLL_SECONDS = 2
EXPORT_TIMEOUT_SECONDS = 120

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("coda_export_v2")


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


def resolve_table_link(
    session: requests.Session, browser_url: str
) -> tuple[str, dict[str, Any]]:
    """
    Resolve a table link via GET /resolveBrowserLink and read the table
    detail. Returns (doc_id, table), where table includes the `parent` page
    and `rowCount`. The doc ID is taken from the resource's API href.
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
    if resource.get("type") != "table":
        raise CodaApiError(
            f"The link points to a resource of type '{resource.get('type')}': "
            "use the link of a table (with the #Table-Name_tuXXXX anchor)."
        )

    doc_id = match.group(1)
    table = get_json(
        session,
        f"{API_BASE_URL}/docs/{doc_id}/tables/{quote(resource['id'], safe='')}",
        "Error fetching the table",
    )
    if not (table.get("parent") or {}).get("id"):
        raise CodaApiError("The table has no parent page.")
    return doc_id, table


def export_page_markdown(
    session: requests.Session, doc_id: str, page_id: str
) -> str:
    """
    Export the whole page as markdown (POST /pages/{id}/export, then poll the
    status URL until the download link is ready). The text comes back as
    UTF-8 decoded as latin-1, so it is repaired before returning.
    """
    response = request_with_retry(
        session,
        "POST",
        f"{API_BASE_URL}/docs/{doc_id}/pages/{quote(page_id, safe='')}/export",
        json={"outputFormat": "markdown"},
    )
    if response.status_code not in (200, 202):
        raise CodaApiError(
            f"Error starting the page export ({response.status_code}): "
            f"{response.text}"
        )
    status_url = response.json()["href"]

    deadline = time.monotonic() + EXPORT_TIMEOUT_SECONDS
    while True:
        status = get_json(session, status_url, "Error reading the export status")
        if status.get("status") == "complete":
            break
        if status.get("status") == "failed":
            raise CodaApiError(f"The page export failed: {status}")
        if time.monotonic() > deadline:
            raise CodaApiError("Timeout waiting for the page export.")
        time.sleep(EXPORT_POLL_SECONDS)

    # The download link is a pre-signed URL: no Authorization header needed.
    download = requests.get(status["downloadLink"], timeout=60)
    if download.status_code != 200:
        raise CodaApiError(f"Error downloading the export ({download.status_code}).")
    return repair_encoding(download.content)


def repair_encoding(raw: bytes) -> str:
    """
    The export is UTF-8 text that arrives mis-decoded (e.g. 'â' instead of
    '–'). Decode as UTF-8, then undo the latin-1 mis-decoding if present.
    """
    text = raw.decode("utf-8", errors="replace")
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
TABLE_ROW_RE = re.compile(r"^\|.*\|\s*$")
LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
GOOGLE_FILE_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)")
# "see Country Weights below", "(see Country Weights below)", "vedi Country Weights sotto"
REFERENCE_RE = re.compile(
    r"\b(?:see|vedi)\s+(?:the\s+)?(?P<name>[^.\n|()*<>]{2,80}?)"
    r"(?:\s+(?:section|sezione|tab))?\s+(?:below|sotto)\b",
    re.IGNORECASE,
)


def normalize_title(text: str) -> str:
    """Lowercase and strip markdown/punctuation, to compare titles and names."""
    text = re.sub(r"[*_`]", "", text)
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip().lower()


def find_table_block(lines: list[str], table_name: str) -> tuple[int, int, int]:
    """
    Locate the markdown table that follows the heading named `table_name`.
    Returns (heading_index, first_row_index, end_index_exclusive).
    """
    wanted = normalize_title(table_name)
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if not m or normalize_title(m.group(2)) != wanted:
            continue
        start = i + 1
        while start < len(lines) and not TABLE_ROW_RE.match(lines[start]):
            if HEADING_RE.match(lines[start]):
                break
            start += 1
        if start >= len(lines) or not TABLE_ROW_RE.match(lines[start]):
            continue
        end = start
        while end < len(lines) and TABLE_ROW_RE.match(lines[end]):
            end += 1
        return i, start, end
    raise CodaApiError(
        f"Table '{table_name}' not found in the exported page (no heading "
        "with that name followed by a table)."
    )


def split_sections(lines: list[str]) -> dict[str, list[str]]:
    """Map each normalized heading title to the lines of its section."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        m = HEADING_RE.match(line)
        if m:
            current = normalize_title(m.group(2))
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


def find_external_links(section_lines: list[str]) -> list[tuple[str, str, str]]:
    """Return (label, url, file_id) for every Google file linked in a section."""
    found = []
    for label, url in LINK_RE.findall("\n".join(section_lines)):
        m = GOOGLE_FILE_ID_RE.search(url)
        if "docs.google.com" in url and m:
            found.append((label, url, m.group(1)))
    return found


def find_references(cell: str) -> list[re.Match[str]]:
    """Find the 'see X below' references inside a table cell."""
    return list(REFERENCE_RE.finditer(cell))


def strip_external_links(lines: list[str]) -> int:
    """
    Replace, in place, every markdown link to a Google file with its label
    only, so the URLs of the original files do not appear in the output:
    "**[my_file](https://docs.google.com/...)**" -> "**my_file**".
    Returns the number of links removed.
    """
    removed = 0

    def keep_label(match: re.Match[str]) -> str:
        nonlocal removed
        if "docs.google.com" in match.group(2) and GOOGLE_FILE_ID_RE.search(
            match.group(2)
        ):
            removed += 1
            return match.group(1)
        return match.group(0)

    for i, line in enumerate(lines):
        lines[i] = LINK_RE.sub(keep_label, line)
    return removed


DEFAULT_DECIMALS = 2


def quantize_half_up(value: Any, decimals: int) -> Decimal:
    """Round like Excel (half away from zero), not like Python's round()."""
    return Decimal(repr(value)).quantize(
        Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP
    )


def format_number(value: int | float, number_format: str) -> str:
    """
    Write a number as Excel displays it, based on the cell's number format.
    Supported: General (2 decimals for non-integers), 0 / 0.00 / 0.000...,
    #,##0.00 (thousands separator), prefix/suffix such as $ and %.
    Anything else falls back to 2 decimals and logs a warning.
    """
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return str(value)

    section = number_format.split(";")[0]
    # Currency symbol / literal text: \$ , "$" , [$$-409]
    prefix = "$" if re.search(r"(\\\$|\"\$\"|\[\$\$)", section) else ""
    body = re.sub(r'\\.|"[^"]*"|\[[^\]]*\]', "", section)
    body = body.replace("$", "").replace("_", "").replace("*", "").strip()

    if body in ("", "General"):
        if float(value).is_integer():
            return f"{int(value)}"
        return str(quantize_half_up(value, DEFAULT_DECIMALS))

    is_percent = body.endswith("%")
    core = body.rstrip("%")
    m = re.fullmatch(r"(#,##)?[0#]+(?:\.([0#]+))?", core)
    if not m:
        logger.warning(
            "Number format '%s' not supported: using %d decimals.",
            number_format,
            DEFAULT_DECIMALS,
        )
        return str(quantize_half_up(value, DEFAULT_DECIMALS))

    decimals = len(m.group(2) or "")
    number = Decimal(repr(value)) * (100 if is_percent else 1)
    number = number.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    text = f"{abs(number):,.{decimals}f}" if m.group(1) else f"{abs(number):.{decimals}f}"
    sign = "-" if number < 0 else ""
    return f"{sign}{prefix}{text}{'%' if is_percent else ''}"


def format_cell(value: Any, number_format: str) -> str:
    """Display text of a worksheet cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return format_number(value, number_format)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S").removesuffix(" 00:00:00")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def read_sheet(ws: Any) -> dict[str, Any]:
    """
    Split a worksheet into: title lines above the table, the table itself
    (header row + body) and note lines below it. The header is the first row
    with at least 2 non-empty cells that is followed by another such row.
    Merged cells are expanded to their top-left value only.
    """
    grid: list[list[str]] = []
    for row in ws.iter_rows():
        grid.append([format_cell(c.value, c.number_format) for c in row])

    def filled(r: list[str]) -> int:
        return sum(1 for c in r if c != "")

    header_idx = next(
        (
            i
            for i in range(len(grid) - 1)
            if filled(grid[i]) >= 2 and filled(grid[i + 1]) >= 2
        ),
        None,
    )
    if header_idx is None:
        lines = [" ".join(c for c in r if c) for r in grid]
        return {"title": [l for l in lines if l], "header": [], "rows": [], "notes": []}

    width = max(i + 1 for i, c in enumerate(grid[header_idx]) if c != "")
    end = header_idx + 1
    while end < len(grid) and filled(grid[end]) >= 2:
        end += 1

    def text_of(r: list[str]) -> str:
        return " ".join(c for c in r if c)

    return {
        "title": [t for t in (text_of(r) for r in grid[:header_idx]) if t],
        "header": grid[header_idx][:width],
        "rows": [r[:width] for r in grid[header_idx + 1 : end]],
        "notes": [t for t in (text_of(r) for r in grid[end:]) if t],
    }


def escape_html_text(text: str) -> str:
    """
    Escape text for the embedded HTML. "$" is written as the entity &#36;:
    markdown renderers with math support read "$ ... $" as a formula and
    would swallow everything between two dollar signs, breaking the outer
    table. A browser still shows it as "$".
    """
    return html.escape(text).replace("$", "&#36;")


def sheet_to_html(name: str, sheet: dict[str, Any], source: str) -> str:
    """Single-line HTML for one sheet: title, table and notes."""
    esc = escape_html_text
    parts = [f'<div data-source="{esc(source)}" data-sheet="{esc(name)}">']
    parts.append(f"<p><b>{esc(source)} — sheet: {esc(name)}</b></p>")
    parts.extend(f"<p>{esc(t)}</p>" for t in sheet["title"])
    if sheet["header"]:
        parts.append("<table><thead><tr>")
        parts.extend(f"<th>{esc(h)}</th>" for h in sheet["header"])
        parts.append("</tr></thead><tbody>")
        for row in sheet["rows"]:
            parts.append(
                "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>"
            )
        parts.append("</tbody></table>")
    parts.extend(f"<p>{esc(t)}</p>" for t in sheet["notes"])
    parts.append("</div>")
    return "".join(parts)


def workbook_to_html(path: Path) -> str:
    """Convert every sheet of an .xlsx file into HTML (cached values only)."""
    wb = load_workbook(path, data_only=True, read_only=False)
    return "".join(
        sheet_to_html(ws.title, read_sheet(ws), path.name) for ws in wb.worksheets
    )


def load_mapping(import_dir: Path) -> dict[str, str]:
    """Read import/mapping.json: {google_file_id: file_name}. Missing = empty."""
    path = import_dir / "mapping.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CodaApiError(f"{path} is not valid JSON: {exc}") from exc


def build_embedded_html(
    name: str,
    sections: dict[str, list[str]],
    import_dir: Path,
    mapping: dict[str, str],
    cache: dict[str, str],
) -> str | None:
    """
    HTML to embed for a reference to the section `name`, or None if the
    section is missing or has no Google file link. A link whose file is not
    mapped / not found becomes a placeholder. Every reason is logged.
    """
    key = normalize_title(name)
    if key not in sections:
        logger.warning("Reference '%s': no section with that title.", name.strip())
        return None

    links = find_external_links(sections[key])
    if not links:
        logger.warning("Reference '%s': the section has no spreadsheet link.", name.strip())
        return None

    chunks = []
    for label, url, file_id in links:
        file_name = mapping.get(file_id)
        if not file_name:
            logger.warning(
                "Reference '%s': file '%s' is not in mapping.json. Add: "
                '"%s": "<file>.xlsx"',
                name.strip(),
                label,
                file_id,
            )
            chunks.append(f"<p>[file not available: {html.escape(label)}]</p>")
            continue
        path = import_dir / file_name
        if not path.exists():
            logger.warning("Reference '%s': %s not found.", name.strip(), path)
            chunks.append(f"<p>[file not found: {html.escape(file_name)}]</p>")
            continue
        if file_name not in cache:
            cache[file_name] = workbook_to_html(path)
        logger.info("Embedded '%s' for the reference '%s'.", file_name, name.strip())
        chunks.append(cache[file_name])
    return "".join(chunks) or None


def embed_in_table(
    lines: list[str],
    first_row: int,
    end: int,
    sections: dict[str, list[str]],
    import_dir: Path,
    mapping: dict[str, str],
) -> int:
    """
    Rewrite the table rows in place: after each 'see X below' reference found
    in a cell, append the HTML of the linked spreadsheet (table inside
    table). Returns the number of references resolved.
    """
    cache: dict[str, str] = {}
    resolved = 0
    for i in range(first_row, end):
        cells = lines[i].split("|")
        changed = False
        for c, cell in enumerate(cells):
            extra = []
            for ref in find_references(cell):
                embedded = build_embedded_html(
                    ref.group("name"), sections, import_dir, mapping, cache
                )
                if embedded:
                    extra.append(embedded)
                    resolved += 1
            if extra:
                cells[c] = cell.rstrip() + "<br/><br/>" + "<br/>".join(extra) + " "
                changed = True
        if changed:
            lines[i] = "|".join(cells)
    return resolved


def count_data_rows(first_row: int, end: int) -> int:
    """Rows of a markdown table without the header and the separator."""
    return max(0, end - first_row - 2)


def run(
    session: requests.Session,
    browser_url: str,
    import_dir: Path,
    output_dir: Path,
) -> int:
    """Resolve the table link, export its page and embed the linked files."""
    try:
        logger.info("Resolving the link...")
        doc_id, table = resolve_table_link(session, browser_url)
        page = table["parent"]
        logger.info(
            "Table '%s' (%d rows) in page '%s'.",
            table["name"],
            table["rowCount"],
            page["name"],
        )

        logger.info("Exporting the page as markdown...")
        text = export_page_markdown(session, doc_id, page["id"])
        lines = text.splitlines()

        _, first_row, end = find_table_block(lines, table["name"])
        found = count_data_rows(first_row, end)
        if found != table["rowCount"]:
            logger.warning(
                "The exported table has %d rows but the API reports %d: "
                "the export may be truncated.",
                found,
                table["rowCount"],
            )

        resolved = embed_in_table(
            lines,
            first_row,
            end,
            split_sections(lines),
            import_dir,
            load_mapping(import_dir),
        )
        logger.info("References resolved: %d", resolved)

        # After the embedding: the links were needed to find the files.
        logger.info("Links to original files removed: %d", strip_external_links(lines))
    except CodaApiError as exc:
        logger.error("%s", exc)
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w-]+", "_", table["name"]).strip("_") or table["id"]
    file_path = output_dir / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.md"
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Saved: %s", file_path)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export to markdown the whole page of a Coda / Superhuman Docs "
            "table, embedding the linked spreadsheets found in the import "
            "folder inside the cells that refer to them (experimental)."
        )
    )
    parser.add_argument(
        "url", help="Link of a table (with the #Table-Name_tuXXXX anchor)."
    )
    parser.add_argument(
        "--import-dir",
        default=None,
        help="Folder with the .xlsx files and mapping.json (default: ./import).",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Destination folder for the .md file (default: ./output).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base = Path(__file__).resolve().parent
    session = build_session(load_api_key())
    sys.exit(
        run(
            session,
            args.url,
            Path(args.import_dir) if args.import_dir else base / "import",
            Path(args.output_dir) if args.output_dir else base / "output",
        )
    )


if __name__ == "__main__":
    main()
