# Coda table export

Local Python scripts that, given a **link** copied from the browser (Coda /
Superhuman Docs) and your **API key**, export the table the link points to
(or all the tables of the page it points to) to CSV or Markdown.

Everything runs on your computer: the only external communication is the
direct calls to the official Coda API (`https://coda.io/apis/v1`).

## Project structure

```
dati_decision_intelligence/
├── .env.example       # Configuration template (no secrets)
├── .env               # YOUR API key (to be created, NOT versioned)
├── .gitignore
├── requirements.txt   # Python dependencies
├── coda_export_v1.py  # Table -> csv-raw / csv-clean / md
├── coda_export_v2.py  # (experimental) whole page -> one .md with embedded Excel files
├── import/            # (v2) .xlsx files and mapping.json
├── output/            # Generated files
└── README.md
```

## Requirements

- **Python 3.10+** (tested with Python 3.15.2)
- A Coda / Superhuman Docs account with access to the document to export

## Setup

```bash
cd .../Desktop/dati_decision_intelligence

# Virtual environment and dependencies
python3.15 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configuration
cp .env.example .env
```

### Getting the API key

1. Go to [coda.io](https://coda.io) and sign in.
2. Profile icon in the top right → **Account Settings**.
3. Under **API Settings**, click **Generate API token**.
4. Paste the token into `.env`:

```env
CODA_API_KEY=your_api_token_here
```

⚠️ The token has the same permissions as your user: do not share it and do
not commit it to Git. `.env` is excluded from Git via `.gitignore`.

## `coda_export_v1.py` — export a table

```bash
python coda_export_v1.py [--csv-raw | --csv-clean | --md] "<link>" [--output-dir FOLDER]
```

Example with the link of a table (copied from the browser, including the
`#Table-Name_tuXXXX` anchor):

```bash
python coda_export_v1.py "https://docs.superhuman.com/d/_dAbCDeFGH/Page_suXYZ#Table-Name_tuWXYZ12"
python coda_export_v1.py --csv-clean "https://docs.superhuman.com/d/_dAbCDeFGH/Page_suXYZ#Table-Name_tuWXYZ12"
python coda_export_v1.py --md "https://docs.superhuman.com/d/_dAbCDeFGH/Page_suXYZ#Table-Name_tuWXYZ12"
```

### Output formats

| Flag | File | Cell contents |
|---|---|---|
| `--csv-raw` (default) | `<table>_csv-raw_<timestamp>.csv` | As returned by the API: embedded line breaks, lists and objects are written as they are |
| `--csv-clean` | `<table>_csv-clean_<timestamp>.csv` | Flattened: every table row takes a single line of text |
| `--md` | `<table>_<timestamp>.md` | `# <table name>` title and a Markdown table; contents as in `--csv-raw`, with line breaks written as `<br>` and `\|` escaped so the table stays valid |

The three flags are mutually exclusive; without any flag the export is
`--csv-raw`.

The CSV files use the `utf-8-sig` encoding (they open directly in Excel with
accented characters), the `.md` file uses `utf-8`.

How `--csv-clean` flattens the cells:

| On Coda | `--csv-raw` | `--csv-clean` |
|---|---|---|
| `- first point`<br>`- second point` | same text on two lines | `- first point; - second point` |
| multi-select `Option A`, `Option B` | `['Option A', 'Option B']` | `Option A; Option B` |
| a person / attachment / reference | a Python dict | name, email or url |

If a line ends with `:` the following text is attached with a space instead
of the separator (`Category: item1; item2`).

### What the link can point to

| The link points to… | What happens |
|---|---|
| **A table** | Exports that table to `output/`. |
| **A page** | Exports all its tables (`table` and `view`) to `output/<page_name>/`. Subpages are not included. |
| **Anything else** (e.g. the whole doc) | Stops with an error message. |

If two tables have the same name within the same export, the table ID is
appended to the file name so that they do not overwrite each other.

## `coda_export_v2.py` — whole page with embedded Excel files (experimental)

For tables whose cells refer to other sections of the page ("see Country
Weights below"), and those sections link to a spreadsheet. Given the link of
a **table**, the script:

1. exports the **whole page** the table lives in, as Markdown, keeping the
   order of text and tables;
2. finds the "see X below" / "vedi X sotto" references inside the table cells
   and the section with that title;
3. looks for the spreadsheet linked in that section among the `.xlsx` files
   in `import/` and writes it **inside the cell** that makes the reference,
   as an HTML table (table inside table);
4. removes the links to the original files, keeping only their label;
5. saves a single `output/<table>_<timestamp>.md`.

```bash
python coda_export_v2.py "<table link>" [--import-dir import] [--output-dir output]
```

The spreadsheets linked from the page are private, so they are not
downloaded: put them in `import/` and map the file ID (the part after `/d/`
in the Google link) to the file name in `import/mapping.json`:

```json
{
  "1-Ubu2JUC0y41_AOs9wJfNe_9ZFGdFd2z": "meta_licensed_country_renewal_2026.xlsx"
}
```

A link without an entry is not matched automatically (a page can have
several): the cell gets the placeholder `[file not available: <label>]` and
the log prints the line to add to `mapping.json`.

Notes:

- Numbers are written as Excel displays them (the cell's number format);
  where the format is *General*, non-integers get 2 decimals.
- `$` is written as `&#36;` inside the embedded table: Markdown viewers with
  math support (VS Code, Obsidian) would otherwise read `$ ... $` as a
  formula and break the outer table.
- The script warns if the exported table has a different number of rows than
  the one reported by the API (possible truncation).

## Technical details

- **Link resolution**: `GET /resolveBrowserLink` returns the type, ID and
  name of the resource; the document ID is taken from the `href` of the
  response. No doc or table IDs are needed in `.env`.
- **Tables of a page**: the API has no per-page filter. The script lists
  the tables of the doc and keeps those whose `parent` field is the page;
  if `parent` is missing from the list, it reads it from the table detail.
- **Authentication**: `Authorization: Bearer <CODA_API_KEY>` header.
- **Pagination**: follows `nextPageLink` both for rows and for the list of
  tables.
- **Rate limiting**: on `429` and `5xx` it retries with exponential backoff
  (honoring `Retry-After`), up to 5 attempts.
- **Columns**: rows are downloaded with `useColumnNames=true`, so the files
  have the readable column names. Two columns are appended at the end:
  `_coda_row_id` (stable row ID on Coda) and `_coda_row_index` (position of
  the row at export time).
- **Page export (v2)**: `POST /pages/{id}/export` with `outputFormat:
  markdown`, then the status URL is polled until the download link is ready.
  The text is decoded as UTF-8 (the download arrives mis-decoded).

## Troubleshooting

- **".env file not found"** → copy `.env.example` to `.env` and enter the
  API key.
- **401/403 error** → invalid token, or no access to the document.
- **404 error while resolving the link** → the token has no access to the
  doc, or the link was copied incompletely.
- **"The link points to a resource of type '…'"** → use the link of a
  table (with the `#Table-Name_tuXXXX` anchor) or of a page, not the one of
  the whole document. v2 only accepts the link of a table.
- **Repeated 429 errors** → too many calls in a short time: the script
  already retries on its own; if it persists, wait a few minutes.
- **v2: `[file not available: ...]` in a cell** → add the .xlsx file to
  `import/` and its entry to `import/mapping.json`.
