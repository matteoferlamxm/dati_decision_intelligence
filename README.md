# Estrazione tabelle da Coda

Script Python locale che, dato un **link** copiato dal browser (Coda /
Superhuman Docs) e la tua **API key**, scarica in CSV la tabella a cui il
link punta, oppure tutte le tabelle della pagina a cui punta.

Tutto funziona sul tuo computer: l'unica comunicazione esterna sono le
chiamate dirette alle API ufficiali di Coda (`https://coda.io/apis/v1`).

## Struttura del progetto

```
dati_decision_intelligence/
├── .env.example       # Template di configurazione (nessun segreto)
├── .env               # La TUA API key (da creare, NON versionato)
├── .gitignore
├── requirements.txt   # Dipendenze Python
├── coda_export_v1.py  # Export standard: celle scritte come arrivano dall'API
├── coda_export_v2.py  # Export con celle appiattite su una sola riga di testo
├── output/            # CSV generati
└── README.md
```

I due script hanno la stessa interfaccia e differiscono solo nel
trattamento delle celle (vedi [Quale script usare](#quale-script-usare)).

## Requisiti

- **Python 3.10+** (verificato con Python 3.15.2)
- Un account Coda / Superhuman Docs con accesso al documento da esportare

## Setup

```bash
cd /Users/matteo.ferlal/Desktop/dati_decision_intelligence

# Virtual environment e dipendenze
python3.15 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configurazione
cp .env.example .env
```

### Ottenere la API key

1. Vai su [coda.io](https://coda.io) e accedi.
2. Icona profilo in alto a destra → **Account Settings**.
3. In **API Settings** clicca su **Generate API token**.
4. Incolla il token in `.env`:

```env
CODA_API_KEY=il_tuo_token_api_qui
```

⚠️ Il token ha gli stessi permessi del tuo utente: non condividerlo e non
committarlo su Git. `.env` è escluso da Git tramite `.gitignore`.

## Uso

```bash
python coda_export_v2.py "<link>"
```

Esempio con il link di una tabella (copiato dal browser, con l'ancora
`#Nome-Tabella_tuXXXX`):

```bash
python coda_export_v2.py "https://docs.superhuman.com/d/_dAbCDeFGH/Pagina_suXYZ#Nome-Tabella_tuWXYZ12"
```

Lo script risolve il link e sceglie da solo cosa fare:

| Il link punta a… | Cosa succede |
|---|---|
| **Una tabella** | Esporta quella tabella in `output/<nome_tabella>_<timestamp>.csv`. |
| **Una pagina** | Esporta tutte le sue tabelle (`table` e `view`) in `output/<nome_pagina>/`. Le sottopagine non sono incluse. |
| **Altro** (es. il doc intero) | Si ferma con un messaggio di errore. |

Unica opzione: `--output-dir CARTELLA` (default `./output`).

Se due tabelle hanno lo stesso nome, all'interno dello stesso export viene
aggiunto l'ID della tabella al nome del file, così non si sovrascrivono.

## Quale script usare

| | `coda_export_v1.py` | `coda_export_v2.py` |
|---|---|---|
| Celle con testo multi-riga | scritte con gli "a capo" originali | appiattite su una riga |
| Liste (multi-select, lookup) | scritte come lista Python | unite con `; ` |
| Oggetti (persona, allegato, riferimento) | scritti come dizionario Python | ridotti a nome/email/url |

Con `v2` ogni riga della tabella occupa una sola riga di testo nel CSV,
quindi il file resta leggibile anche aperto come testo semplice. Esempio:

| Su Coda | `v1` | `v2` |
|---|---|---|
| `- primo punto`<br>`- secondo punto` | stesso testo su due righe | `- primo punto; - secondo punto` |
| multi-select `Opzione A`, `Opzione B` | `['Opzione A', 'Opzione B']` | `Opzione A; Opzione B` |

Con `v2`, se una riga termina con `:` il testo successivo viene attaccato
con uno spazio invece del separatore (`Categoria: voce1; voce2`). Con
entrambi gli script, pandas racchiude tra virgolette i campi che
contengono virgole o "a capo", quindi la struttura delle colonne resta
corretta per Excel, Google Sheets o pandas.

## Dettagli tecnici

- **Risoluzione del link**: `GET /resolveBrowserLink` restituisce tipo, ID
  e nome della risorsa; l'ID del documento si ricava dall'`href` della
  risposta. Non servono ID di doc o di tabella nel `.env`.
- **Tabelle di una pagina**: l'API non ha un filtro per pagina. Lo script
  elenca le tabelle del doc e tiene quelle il cui campo `parent` è la
  pagina; se `parent` manca nell'elenco, lo legge dal dettaglio della
  tabella.
- **Autenticazione**: header `Authorization: Bearer <CODA_API_KEY>`.
- **Paginazione**: segue `nextPageLink` sia per le righe sia per l'elenco
  delle tabelle.
- **Rate limiting**: su `429` e `5xx` riprova con backoff esponenziale
  (rispettando `Retry-After`), fino a 5 tentativi.
- **Colonne**: le righe sono scaricate con `useColumnNames=true`, quindi
  il CSV ha i nomi leggibili delle colonne. In fondo vengono aggiunte
  `_coda_row_id` (ID stabile della riga su Coda) e `_coda_row_index`
  (posizione della riga al momento dell'export).
- **Encoding CSV**: `utf-8-sig`, apribile direttamente in Excel con
  caratteri accentati.

## Risoluzione problemi

- **"File .env non trovato"** → copia `.env.example` in `.env` e inserisci
  la API key.
- **Errore 401/403** → token non valido o senza accesso al documento.
- **Errore 404 sulla risoluzione del link** → il token non ha accesso al
  doc, oppure il link è stato copiato in modo incompleto.
- **"Il link punta a una risorsa di tipo '…'"** → usa il link di una
  tabella (con l'ancora `#Nome-Tabella_tuXXXX`) o di una pagina, non quello
  del documento intero.
- **Errori 429 ripetuti** → troppe chiamate ravvicinate: lo script già
  riprova da solo; se persiste, attendi qualche minuto.
