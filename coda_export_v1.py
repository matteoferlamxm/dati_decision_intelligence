#!/usr/bin/env python3
"""
coda_export_v1.py
=================

Dato un link copiato dal browser (Superhuman Docs / Coda), esporta in CSV
la tabella a cui punta, oppure tutte le tabelle della pagina a cui punta.

Variante "standard": il contenuto delle celle NON viene modificato. Ogni
valore restituito dall'API va a pandas.to_csv così com'è (newline interni,
liste e dizionari compresi). Per un CSV con una sola riga di testo per
ogni riga di tabella usa coda_export_v2.py.

Nel file .env serve solo CODA_API_KEY (vedi .env.example).

Uso:
    python coda_export_v1.py "<link>" [--output-dir CARTELLA]

Il link può puntare a:
    - una tabella (con l'ancora #Nome-Tabella_tuXXXX) -> esporta quella tabella
    - una pagina                                      -> esporta tutte le sue tabelle
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
    """Errore nelle chiamate verso l'API Coda."""


def load_api_key() -> str:
    """Legge CODA_API_KEY dal file .env nella cartella dello script."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        logger.error(
            "File .env non trovato in %s. Copia .env.example in .env e "
            "inserisci CODA_API_KEY.",
            env_path,
        )
        sys.exit(1)

    load_dotenv(dotenv_path=env_path)
    api_key = os.getenv("CODA_API_KEY", "").strip()
    if not api_key:
        logger.error("CODA_API_KEY non impostata nel file .env.")
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
    """Esegue la richiesta con backoff esponenziale su 429 (rate limit) e 5xx."""
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(1, MAX_RETRIES + 1):
        response = session.request(method, url, timeout=30, **kwargs)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            wait_seconds = float(retry_after) if retry_after else backoff
            logger.warning(
                "Rate limit (429). Attesa di %.1f s (tentativo %d/%d)...",
                wait_seconds,
                attempt,
                MAX_RETRIES,
            )
        elif response.status_code >= 500:
            wait_seconds = backoff
            logger.warning(
                "Errore server (%d). Nuovo tentativo tra %.1f s (%d/%d)...",
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
        f"Numero massimo di tentativi ({MAX_RETRIES}) raggiunto per {url}."
    )


def get_json(
    session: requests.Session, url: str, error_label: str, **kwargs: Any
) -> dict[str, Any]:
    """GET con gestione errori uniforme; restituisce il JSON della risposta."""
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
    Risolve il link del browser tramite GET /resolveBrowserLink.
    Restituisce (doc_id, tipo_risorsa, risorsa). La risposta ha la forma
    {"resource": {"type", "id", "name", "href"}}; il doc ID si ricava
    dall'href API della risorsa (.../docs/<docId>/...).
    """
    payload = get_json(
        session,
        f"{API_BASE_URL}/resolveBrowserLink",
        "Errore nella risoluzione del link",
        params={"url": browser_url},
    )
    resource = payload.get("resource") or {}
    match = re.search(r"/docs/([^/?#]+)", resource.get("href", ""))
    if not resource.get("id") or not match:
        raise CodaApiError(
            f"Risposta inattesa da resolveBrowserLink: {payload}"
        )
    return match.group(1), resource.get("type", ""), resource


def get_table_details(
    session: requests.Session, doc_id: str, table_id: str
) -> dict[str, Any]:
    url = f"{API_BASE_URL}/docs/{doc_id}/tables/{quote(table_id, safe='')}"
    return get_json(
        session, url, f"Errore nel recupero della tabella '{table_id}'"
    )


def list_page_tables(
    session: requests.Session, doc_id: str, page_id: str
) -> list[dict[str, Any]]:
    """
    Tabelle (e view) che appartengono alla pagina. L'API non ha un filtro per
    pagina: si elencano le tabelle del doc (con paginazione) e si tengono
    quelle con parent.id == page_id. Il campo `parent` non è garantito
    nell'elenco: se manca lo si legge dal dettaglio della tabella.
    """
    tables: list[dict[str, Any]] = []
    url = f"{API_BASE_URL}/docs/{doc_id}/tables"
    params: dict[str, Any] | None = {"tableTypes": PAGE_TABLE_TYPES}

    while url:
        payload = get_json(
            session, url, "Errore nel recupero delle tabelle", params=params
        )
        for table in payload.get("items", []):
            parent = table.get("parent")
            if not parent:
                parent = get_table_details(session, doc_id, table["id"]).get(
                    "parent"
                )
            if (parent or {}).get("id") == page_id:
                tables.append(table)
        # nextPageLink contiene già tutti i parametri (incluso pageToken)
        url = payload.get("nextPageLink")
        params = None

    return tables


def fetch_table_rows(
    session: requests.Session, doc_id: str, table_id: str
) -> list[dict[str, Any]]:
    """Scarica tutte le righe di una tabella seguendo la paginazione."""
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
            session, url, "Errore nel recupero delle righe", params=params
        )
        items = payload.get("items", [])
        rows.extend(items)
        logger.info(
            "  pagina %d: %d righe scaricate (totale: %d)",
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
    Righe Coda -> DataFrame, con i metadati _coda_row_* in fondo.
    Nessuna normalizzazione: i valori (anche liste o dizionari) vanno a
    pandas così come arrivano dall'API, e sarà to_csv a scriverli.
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
    """Rende un'etichetta utilizzabile come nome di file/cartella."""
    return "".join(
        c if c.isalnum() or c in ("-", "_") else "_" for c in label
    ).strip("_")


def export_table_to_csv(
    df: pd.DataFrame,
    table_name: str,
    table_id: str,
    output_dir: Path,
    used_names: set[Path],
) -> Path:
    """
    Salva il DataFrame in <output_dir>/<nome_tabella>_<timestamp>.csv.
    Se nella stessa esecuzione c'è già una tabella con lo stesso nome,
    aggiunge l'ID per non sovrascrivere il file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    base = safe_name(table_name) or safe_name(table_id)
    if output_dir / base in used_names:
        base = f"{base}_{safe_name(table_id)}"
    used_names.add(output_dir / base)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = output_dir / f"{base}_{timestamp}.csv"
    df.to_csv(file_path, index=False, encoding="utf-8-sig")
    return file_path


def export_tables(
    session: requests.Session,
    doc_id: str,
    tables: list[dict[str, Any]],
    output_dir: Path,
) -> int:
    """
    Scarica e salva in CSV le tabelle indicate (dict con almeno id e name).
    Restituisce 0 se tutto è andato bene, 1 se almeno una tabella è fallita.
    """
    exit_code = 0
    used_names: set[Path] = set()
    for table in tables:
        name = table.get("name") or table["id"]
        logger.info("Esportazione tabella '%s' (%s)...", name, table["id"])
        try:
            rows = fetch_table_rows(session, doc_id, table["id"])
        except CodaApiError as exc:
            logger.error("Errore nell'esportazione di '%s': %s", name, exc)
            exit_code = 1
            continue

        df = rows_to_dataframe(rows)
        if df.empty:
            logger.warning("La tabella '%s' non contiene righe: CSV non creato.", name)
            continue

        file_path = export_table_to_csv(
            df, name, table["id"], table.get("output_dir", output_dir), used_names
        )
        logger.info(
            "Tabella '%s' esportata: %d righe, %d colonne -> %s",
            name,
            len(df),
            len(df.columns),
            file_path,
        )
    return exit_code


def run(session: requests.Session, browser_url: str, output_dir: Path) -> int:
    """Risolve il link e sceglie cosa esportare in base al tipo di risorsa."""
    logger.info("Risoluzione del link...")
    try:
        doc_id, resource_type, resource = resolve_target(session, browser_url)

        if resource_type == "table":
            logger.info("Il link punta alla tabella '%s'.", resource.get("name"))
            return export_tables(session, doc_id, [resource], output_dir)

        if resource_type == "page":
            page_name = resource.get("name") or resource["id"]
            logger.info("Il link punta alla pagina '%s'.", page_name)
            tables = list_page_tables(session, doc_id, resource["id"])
            if not tables:
                logger.warning("Nessuna tabella trovata nella pagina '%s'.", page_name)
                return 0
            logger.info("Tabelle trovate nella pagina: %d", len(tables))
            page_dir = output_dir / (safe_name(page_name) or resource["id"])
            for table in tables:
                table["output_dir"] = page_dir
            return export_tables(session, doc_id, tables, output_dir)
    except CodaApiError as exc:
        logger.error("%s", exc)
        return 1

    logger.error(
        "Il link punta a una risorsa di tipo '%s': serve il link di una "
        "tabella (con l'ancora #Nome-Tabella_tuXXXX) o di una pagina.",
        resource_type,
    )
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Esporta in CSV la tabella (o le tabelle di una pagina) "
            "indicata da un link Coda / Superhuman Docs (export standard, "
            "senza normalizzazione del contenuto delle celle)."
        )
    )
    parser.add_argument("url", help="Link copiato dal browser (tabella o pagina).")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Cartella di destinazione dei CSV (default: ./output).",
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
    sys.exit(run(session, args.url, output_dir))


if __name__ == "__main__":
    main()
