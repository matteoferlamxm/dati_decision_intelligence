# Coda table export

Local Python scripts that, given a **link** copied from the browser (Coda /
Superhuman Docs) and your **API key**, download to CSV the table the link
points to, or all the tables of the page the link points to.

Everything runs on your computer: the only external communication is the
direct calls to the official Coda API (`https://coda.io/apis/v1`).

## Project structure

```
dati_decision_intelligence/
├── .env.example       # Configuration template (no secrets)
├── .env               # YOUR API key (to be created, NOT versioned)
├── .gitignore
├── requirements.txt   # Python dependencies
├── coda_export_v1.py  # Standard export: cells written as returned by the API
├── coda_export_v2.py  # Export with cells flattened to a single line of text
├── output/            # Generated CSV files
└── README.md
```

The two scripts share the same interface and only differ in how they handle
cell contents (see [Which script to use](#which-script-to-use)).

## Requirements

- **Python 3.10+** (tested with Python 3.15.2)
- A Coda / Superhuman Docs account with access to the document to export

## Setup

```bash
cd /Users/matteo.ferlal/Desktop/dati_decision_intelligence

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

## Usage

```bash
python coda_export_v2.py "<link>"
```

Example with the link of a table (copied from the browser, including the
`#Table-Name_tuXXXX` anchor):

```bash
python coda_export_v2.py "https://docs.superhuman.com/d/_dAbCDeFGH/Page_suXYZ#Table-Name_tuWXYZ12"
```

The script resolves the link and decides by itself what to do:

| The link points to… | What happens |
|---|---|
| **A table** | Exports that table to `output/<table_name>_<timestamp>.csv`. |
| **A page** | Exports all its tables (`table` and `view`) to `output/<page_name>/`. Subpages are not included. |
| **Anything else** (e.g. the whole doc) | Stops with an error message. |

The only option is `--output-dir FOLDER` (default `./output`).

If two tables have the same name within the same export, the table ID is
appended to the file name so that they do not overwrite each other.

## Which script to use

| | `coda_export_v1.py` | `coda_export_v2.py` |
|---|---|---|
| Cells with multi-line text | written with the original line breaks | flattened to one line |
| Lists (multi-select, lookup) | written as a Python list | joined with `; ` |
| Objects (person, attachment, reference) | written as a Python dict | reduced to name/email/url |

With `v2` each table row takes up a single line of text in the CSV, so the
file stays readable even when opened as plain text. Example:

| On Coda | `v1` | `v2` |
|---|---|---|
| `- first point`<br>`- second point` | same text on two lines | `- first point; - second point` |
| multi-select `Option A`, `Option B` | `['Option A', 'Option B']` | `Option A; Option B` |

With `v2`, if a line ends with `:` the following text is attached with a
space instead of the separator (`Category: item1; item2`). With both
scripts, pandas wraps in quotes the fields that contain commas or line
breaks, so the column structure stays correct for Excel, Google Sheets or
pandas.

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
- **Columns**: rows are downloaded with `useColumnNames=true`, so the CSV
  has the readable column names. Two columns are appended at the end:
  `_coda_row_id` (stable row ID on Coda) and `_coda_row_index` (position of
  the row at export time).
- **CSV encoding**: `utf-8-sig`, can be opened directly in Excel with
  accented characters.

## Troubleshooting

- **".env file not found"** → copy `.env.example` to `.env` and enter the
  API key.
- **401/403 error** → invalid token, or no access to the document.
- **404 error while resolving the link** → the token has no access to the
  doc, or the link was copied incompletely.
- **"The link points to a resource of type '…'"** → use the link of a
  table (with the `#Table-Name_tuXXXX` anchor) or of a page, not the one of
  the whole document.
- **Repeated 429 errors** → too many calls in a short time: the script
  already retries on its own; if it persists, wait a few minutes.
