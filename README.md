# NCLT Cause List — OneCourt

`onecourt.in/nclt/causelist/` — the NCLT daily cause lists as a searchable web page, location by location and court by court, instead of ~35 separate PDFs a day.

## How it works

```
GitHub Actions (cron)  ──►  scrape.py  ──►  data/*.json  ──►  index.html (on onecourt.in)
   every 30 min eve/night      read listing,     committed to       reads the JSON from
                               fetch PDFs,       this repo          raw.githubusercontent.com
                               parse tables
```

No server and no laptop needed. nclt.gov.in doesn't send CORS headers for these PDFs, so the scraper does the reading on a schedule.

### Source

`https://nclt.gov.in/all-cause-list?field_cause_date_value=<date>&field_cause_date_value_1=<date>&page=N` is a Drupal table (20 rows a page) of title, bench label, entry count, date and PDF link. Titles and file names follow no convention; only the **bench label and date** are reliable. The scraper walks that listing for each date.

- **Locations and courts.** The bench label (`Kolkata Bench Court-3`, `Principal Bench Court-I`) is normalised to a location plus a Roman court number. The Principal Bench and New Delhi Bench both become **New Delhi** (Principal Bench Court-I is Delhi's Court I).
- **List kinds** (from the title / file name): `main`, `supp` (Supplementary), `special`, `single`, `additional`, `rule63`, and `notice` (corrigenda — shown as a link only). On the page a court shows its main list first, then the others in that order.
- **Entry count.** NCLT's own count is saved as `listed` and used to check the parse.

Runs 9:00 AM – 11:00 PM IST (every 30 min morning and evening, hourly midday), none overnight. Each run checks 2 days back to 7 days ahead. NCLT sends no ETag, so a quiet run asks with `If-Modified-Since` and compares a hash; unchanged PDFs are not re-parsed and a run with no change commits nothing. Dates older than 60 days are pruned. A full backfill (`days_back` 60) takes roughly 50 minutes.

### Parsing

The PDFs are ruled tables (S.No | CP No | IA/CA No | Purpose | Section | Name of Parties | Counsel for Petitioner | Counsel for Respondent | IRP/RP/Liquidator | Remarks) but every bench formats them differently — columns in a different order, 9–12 columns, headers that wrap or sit outside the table. `parse_pdf()`:

1. reads each page's tables with PyMuPDF `find_tables()`;
2. maps columns from that PDF's header row (falling back to column position for pages that have no header of their own, and to a standard layout if there is none);
3. rebuilds the leftmost columns on pages where the table finder drops them;
4. groups rows: a row with an item number starts a matter; rows under it with an IA number are its IAs; one-cell rows are section headings or the Coram.

Item numbers run in series per section (1.., 101.., 201.., P1..), as NCLT prints them.

### What the page tells you when a parse can't be trusted

- `unparsed` — no text could be read (scanned PDFs; e.g. Cuttack and some Kolkata lists). The page links to the original PDF.
- `partial` — an item number is missing from inside a numbered run (e.g. 519, 521), or fewer than 90% of NCLT's own count were found. The page names the missing item and links the PDF.

### JSON shape (`data/<date>.json`)

```
{ date, fetched, lists: [{name, loc, court, kind, label, title, url, published, items, listed, unparsed?, partial?, gaps?, notice?}],
  benches: [{ list, loc, court, kind, time, vc, judges[],
              sections: [{ t: "ORDINARY LIST", items: [
                { n: "101", c: CP no., p: petitioner, r: respondent, pur: purpose, s: section,
                  pa/ra: counsel, rp: IRP/RP/Liquidator, rem: remarks,
                  ias: [{ a: IA no., pur, s, p, pa, ra, rp, rem }] } ] }] }] }
```

## Checking the parse

```bash
python3 validate.py        # items vs NCLT's count, empty items, numbering gaps
```

## Running locally

```bash
pip install -r requirements.txt
python3 scrape.py                          # refresh data/
python3 scrape.py --date 2026-10-13 --force
python3 scrape.py --parse some.pdf         # parse one PDF, print JSON
python3 -m http.server 8773                # then open http://localhost:8773/
```

On localhost the page reads `data/`; anywhere else it reads this repo's `data/` from `raw.githubusercontent.com` (see `DATA` in `index.html`).

## Deploying

- **Data:** this repo is `github.com/onecourtin/nclt-cause-list` (public). The workflow runs on its own; trigger it once by hand from the Actions tab.
- **Page:** upload `index.html` to `public_html/nclt/causelist/index.html` on the host. Only that file goes there. The existing `onecourt.in/nclt/` page (display board) is separate and stays as it is.
