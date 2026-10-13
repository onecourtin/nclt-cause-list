#!/usr/bin/env python3
"""Fetch NCLT daily cause lists and turn them into JSON.

NCLT publishes every bench's lists on one Drupal page,

    https://nclt.gov.in/all-cause-list?field_cause_date_value=YYYY-MM-DD
        &field_cause_date_value_1=YYYY-MM-DD&page=N

as a table of (title, bench label, number of entries, date, PDF link). File
names and titles follow no convention, so the scraper takes the PDF links from
that listing. Only the bench label and the date are reliable; the title is
used just to tell a main list from a supplementary / special / single-bench /
additional one.

Output, all under data/:

    index.json          which dates exist, and per file its ETag/hash
    <YYYY-MM-DD>.json   the parsed lists for that date

The PDFs are ruled tables (S.No | CP No | IA/CA No | Purpose | Section | Name
of Parties | Counsel for Petitioner | Counsel for Respondent | IRP/RP/
Liquidator | Remarks) but every bench uses its own variation, so the parser
reads each table with PyMuPDF's table finder and maps columns from the header
row of that PDF.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import fitz  # PyMuPDF
import requests

SITE = "https://nclt.gov.in"
LISTING = SITE + "/all-cause-list"
DATA = Path(__file__).resolve().parent / "data"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OneCourt cause-list reader; +https://onecourt.in)",
}

ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]
ROMAN_TO_INT = {r: i for i, r in enumerate(ROMAN) if r}

# NCLT splits one location over two taxonomy names. The Principal Bench sits
# in New Delhi, and "Principal Bench Court-I" is Delhi's Court 1.
LOCATION_ALIASES = {"Principal Bench": "New Delhi", "New Delhi Bench": "New Delhi"}
LOCATION_FIRST = ["New Delhi"]
LOCATION_LAST = ["Registrar NCLT"]

_BENCH_RE = re.compile(r"^(?P<bench>.+?)\s*Court[\s\-–—_.]*(?P<num>[IVXivx]+|\d+)\s*$")


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def parse_bench(label: str) -> dict:
    """'Kolkata Bench Court-3' -> location 'Kolkata', court 'III'."""
    label = _clean(label)
    m = _BENCH_RE.match(label)
    if m:
        bench, num = m.group("bench").strip(" -–—,"), m.group("num")
        court = ROMAN[int(num)] if num.isdigit() and 0 < int(num) < len(ROMAN) else num.upper()
    else:
        bench, court = label, "I"
    bench = LOCATION_ALIASES.get(bench, bench)
    loc = re.sub(r"\s+Bench$", "", bench).strip() or "Unknown"
    return {"loc": loc, "court": court}


def kind_of(title: str) -> str:
    """main | supp | special | single | additional | rule63 | notice — from the listing title or file name."""
    t = (title or "").lower()
    if re.search(r"corrigend|addend|erratum|errata|notice|revised", t) and not re.search(r"cause\s*list", t):
        return "notice"
    if re.search(r"suppl?e?menta|\bsupp\b|supp[-_ ]", t):
        return "supp"
    if re.search(r"rule\s*-?\s*63", t):
        return "rule63"
    if "special" in t:
        return "special"
    if "single" in t:
        return "single"
    if "additional" in t:
        return "additional"
    return "main"


KIND_LABEL = {"main": "Cause List", "supp": "Supplementary", "special": "Special Bench",
              "single": "Single Bench", "additional": "Additional", "rule63": "Rule 63", "notice": "Notice"}
# Order within a court: main first, everything else after.
KIND_ORDER = {"main": 0, "supp": 1, "special": 2, "single": 3, "additional": 4, "rule63": 5, "notice": 6}

# ── PDF parsing ──────────────────────────────────────────────────────────────

SNO_RE = re.compile(r"^[A-Za-z]{0,2}\s?\d+(\.\d+)?\.?$")
VS_SPLIT = re.compile(r"\s+(?:vs\.?|v/s\.?|versus)\s+", re.I)
PREFACE_RE = re.compile(
    r"cisco|webex|hearing through|following matters|video conference|parties shall|"
    r"appearance|national company|not through mobile|username|audio|camera|"
    r"counsels? should|counsels?/ parties|register maintained|date\s*:|day\s*:|time\s*:|"
    r"vc link|join the|infrastructure|judgments|citations|^\d+\s*\.",
    re.I,
)
TIME_RE = re.compile(r"TIME\s*:?\s*(\d{1,2}\s*[:.]\s*\d{2}\s*[AP]\.?\s?M\.?)", re.I)
URL_RE = re.compile(r"https?://\S+")


def _role(text: str) -> str | None:
    t = _clean(text).lower()
    if not t:
        return None
    if "remark" in t:
        return "rem"
    if re.search(r"\birp\b|\brp\b|liquidator|professional|mp\b", t) and "counsel" not in t and "advocate" not in t:
        return "rp"
    if "part" in t and re.search(r"parties|name", t):
        return "party"
    if re.search(r"respondent", t) and re.search(r"counsel|advocate|name|resp", t):
        return "ra"
    if re.search(r"petitioner|applicant", t) and re.search(r"counsel|advocate|name", t):
        return "pa"
    if re.search(r"counsel for\s*p|counsel for\s*$", t):
        return "pa"
    if re.search(r"counsel for\s*r", t):
        return "ra"
    if re.search(r"counsel|advocate", t):
        return "counsel"
    if "purpose" in t:
        return "pur"
    if "section" in t or "rule" in t and "ia" not in t:
        return "sec"
    if re.search(r"\bcp\b|c\.p|cp/", t):
        return "cp"
    if re.search(r"\bia\b|ca/|ma\b|main matter|i\.a", t):
        return "ia"
    if re.search(r"^(s\.?\s?l?\.?|sr\.?)\s*no|^sr\.?$|^s\.?$|^sl|serial", t):
        return "sno"
    return None


def _header_map(rows, ncols):
    """Column roles from the header rows of a table, or None if there isn't one."""
    for hi, row in enumerate(rows[:4]):
        joined = " ".join(_clean(c) for c in row).lower()
        # a real header has a short "Name of Parties" cell, not a case title that mentions parties
        if any("parties" in _clean(c).lower() and len(_clean(c)) <= 25 for c in row) and sum(1 for c in row if _clean(c)) >= 5:
            # headers can wrap onto the next row ("Sr." / "No.")
            head = [_clean(c) for c in row]
            nxt = rows[hi + 1] if hi + 1 < len(rows) else []
            nxt_txt = " ".join(_clean(c) for c in nxt).lower()
            if nxt and re.search(r"counsel|^no\.?$|rp/|no\.", nxt_txt) and not re.search(r"\d", nxt[0] or ""):
                head = [_clean(f"{a} {_clean(b)}") for a, b in zip(head, list(nxt) + [""] * (len(head) - len(nxt)))]
                skip = hi + 2
            else:
                skip = hi + 1
            roles, seen_counsel = [], 0
            for c in head:
                r = _role(c)
                if r == "counsel":
                    r = "pa" if seen_counsel == 0 else "ra"
                    seen_counsel += 1
                roles.append(r)
            # duplicated "Sr. No." column (Allahabad): the second is the IA sub-number
            if roles.count("sno") > 1:
                i = [k for k, r in enumerate(roles) if r == "sno"][1]
                roles[i] = "subno"
            # two numbered columns before parties may both be plain 'No.' cells: first is sno
            return roles, skip
    return None


# Used when a PDF's table has no header row of its own.
CANON = {
    10: ["sno", "cp", "ia", "pur", "sec", "party", "pa", "ra", "rp", "rem"],
    9: ["sno", "cp", "ia", "pur", "sec", "party", "pa", "ra", "rem"],
    11: ["sno", "cp", "subno", "ia", "pur", "sec", "party", "pa", "ra", "rp", "rem"],
    12: ["sno", "cp", "ia", "pur", "sec", "x", "party", "x", "pa", "ra", "rp", "rem"],
}


def _lines(cell) -> list[str]:
    return [_clean(x) for x in str(cell or "").split("\n") if _clean(x)]


def _join_counsel(cell) -> list[str]:
    """Advocates, one per entry. A wrapped line continues the previous name
    when the previous is cut short or the new one is a bracketed enrolment
    number / starts lower-case."""
    out = []
    for ln in _lines(cell):
        if out and (out[-1].endswith((",", "-", "&", "/")) or ln.startswith("(") or ln[0].islower()
                    or re.fullmatch(r"[A-Z][a-z]+", ln) and re.fullmatch(r"(Mr|Ms|Mrs|Dr|Shri|Smt)\.? \S+", out[-1])):
            out[-1] = _clean(out[-1] + " " + ln)
        else:
            out.append(ln)
    return out


class Item:
    def __init__(self, n):
        self.n = n
        self.f = {k: [] for k in ("cp", "party", "pa", "ra", "rp", "rem", "pur", "sec")}
        self.ias = []

    def add(self, row):
        for k in ("cp", "party", "rp", "rem", "pur", "sec"):
            if row.get(k):
                self.f[k].append(row[k])
        for k in ("pa", "ra"):
            if row.get(k):
                self.f[k].extend(_join_counsel(row[k]))

    def out(self) -> dict:
        party = _clean(" ".join(self.f["party"]))
        parts = VS_SPLIT.split(party, maxsplit=1)
        d = {"n": self.n, "c": _clean(" ".join(self.f["cp"])), "p": parts[0], "r": parts[1] if len(parts) > 1 else ""}
        if self.f["pur"]:
            d["pur"] = _clean(" ".join(self.f["pur"]))
        if self.f["sec"]:
            d["s"] = _clean(" ".join(self.f["sec"]))
        if self.f["pa"]:
            d["pa"] = "; ".join(self.f["pa"])
        if self.f["ra"]:
            d["ra"] = "; ".join(self.f["ra"])
        if self.f["rp"]:
            d["rp"] = _clean(" ".join(self.f["rp"]))
        if self.f["rem"]:
            d["rem"] = _clean(" ".join(self.f["rem"]))
        if self.ias:
            d["ias"] = self.ias
        return d


def _ia_row(row) -> dict:
    d = {"a": _clean(row.get("ia", "")) or _clean(row.get("cp", ""))}
    if row.get("pur"):
        d["pur"] = _clean(row["pur"])
    if row.get("sec"):
        d["s"] = _clean(row["sec"])
    if row.get("party"):
        d["p"] = _clean(row["party"])
    pa = _join_counsel(row.get("pa"))
    ra = _join_counsel(row.get("ra"))
    if pa:
        d["pa"] = "; ".join(pa)
    if ra:
        d["ra"] = "; ".join(ra)
    if row.get("rp"):
        d["rp"] = _clean(row["rp"])
    if row.get("rem"):
        d["rem"] = _clean(row["rem"])
    return d



def _page_tables(page, good_edges):
    """Table rows for a page. Some pages lose their leftmost columns (the
    table finder starts the grid at the first ruled column), so when a page's
    table is narrower than the document's usual one, the missing cells are
    rebuilt from the words that sit to its left."""
    out = []
    for tb in page.find_tables().tables:
        rows = tb.extract()
        if not rows:
            continue
        ncols = len(rows[0])
        edges = good_edges.get("edges")
        want = good_edges.get("ncols")
        if want and ncols < want and edges and len(edges) - 1 == want and tb.bbox[0] > edges[0] + 20:
            k = want - ncols
            left_edges = edges[: k + 1]
            x_cut = tb.bbox[0] + 1
            words = [w for w in page.get_text("words") if w[2] <= x_cut + 2 and w[0] >= left_edges[0] - 3 and tb.bbox[1] - 2 <= w[1] <= tb.bbox[3]]
            new_rows = []
            for ri, row in enumerate(tb.rows):
                y0, y1 = row.bbox[1], row.bbox[3]
                cells = []
                for ci in range(k):
                    xa, xb = left_edges[ci], left_edges[ci + 1]
                    ws = [w for w in words if y0 - 1 <= w[1] < y1 - 1 and xa - 3 <= w[0] < xb - 1]
                    ws.sort(key=lambda w: (round(w[1]), w[0]))
                    cells.append(" ".join(w[4] for w in ws))
                new_rows.append(cells + [c or "" for c in rows[ri]])
            rows = new_rows
        elif ncols >= 9 and (not good_edges.get("ncols") or ncols >= good_edges["ncols"]):
            xs = sorted({round(c[0], 1) for c in tb.cells} | {round(c[2], 1) for c in tb.cells})
            if len(xs) - 1 == ncols:
                good_edges["edges"], good_edges["ncols"] = xs, ncols
        xs = sorted({round(c[0], 1) for c in tb.cells} | {round(c[2], 1) for c in tb.cells})
        ivals = list(zip(xs[:-1], xs[1:])) if len(xs) - 1 == len(rows[0]) else None
        out.append((rows, ivals))
    return out


def parse_pdf(path_or_bytes) -> dict:
    """Parse one cause-list PDF -> {time, judges, vc, sections}."""
    doc = fitz.open(stream=path_or_bytes, filetype="pdf") if isinstance(path_or_bytes, bytes) else fitz.open(path_or_bytes)
    bench = {"time": "", "judges": [], "vc": "", "sections": []}

    # time and VC link sit in the preamble text of the first page
    pre = doc[0].get_text()
    m = TIME_RE.search(pre)
    if m:
        bench["time"] = re.sub(r"\s+", " ", m.group(1)).replace(" :", ":").replace(": ", ":").upper()
    u = URL_RE.search(pre)
    if u:
        bench["vc"] = u.group(0).rstrip(").,;")

    roles = None
    section = None
    item = None

    def new_section(title):
        nonlocal section
        section = {"t": _clean(title), "items": []}
        bench["sections"].append(section)

    def close():
        nonlocal item
        if item is not None:
            if section is None:
                new_section("")
            section["items"].append(item.out())
        item = None

    good_edges: dict = {}
    role_x: list = []
    for page in doc:
        for rows, ivals in _page_tables(page, good_edges):
            ncols = len(rows[0])
            start = 0
            hm = _header_map(rows, ncols)
            if hm:
                roles, start = hm
                if len(roles) != ncols:
                    roles = None
                elif ivals:
                    role_x = list(zip(ivals, roles))
            elif roles is None or len(roles) != ncols:
                if role_x and ivals:
                    # same layout, different grid: map columns by position
                    roles = []
                    for (a, b) in ivals:
                        mid = (a + b) / 2
                        roles.append(next((r for (x0, x1), r in role_x if x0 - 1 <= mid <= x1 + 1), None))
                else:
                    roles = CANON.get(ncols)
                if roles is None:
                    continue
            for row in rows[start:]:
                cells = [str(c or "") for c in row]
                if not any(_clean(c) for c in cells):
                    continue
                filled = [i for i, c in enumerate(cells) if _clean(c)]
                first = _clean(cells[0])

                # a row with text in one cell only: heading, coram or preamble
                if len(filled) == 1 and not SNO_RE.match(first) and filled[0] == 0:
                    txt = _clean(cells[0])
                    if re.match(r"^coram", txt, re.I):
                        close()
                        j = _clean(re.sub(r"^coram\s*:?", "", txt, flags=re.I))
                        if j and j not in bench["judges"]:
                            bench["judges"].append(j)
                    elif not PREFACE_RE.search(txt) and len(txt) < 160:
                        close()
                        if section is not None and not section["items"]:
                            section["t"] = _clean(section["t"] + " " + txt)
                        else:
                            new_section(txt)
                    continue
                if any(PREFACE_RE.search(_clean(c)) for c in cells[:1]) and len(filled) <= 2 and not SNO_RE.match(first):
                    continue

                row_d = {}
                last = None
                for i, c in enumerate(cells):
                    r = roles[i] if i < len(roles) else None
                    if r in (None, "x"):
                        r = last  # unlabeled spacer column: belongs to its neighbour
                    else:
                        last = r
                    if r and _clean(c):
                        row_d[r] = (row_d[r] + "\n" + c) if r in row_d and r in ("pa", "ra") else (
                            (row_d[r] + " " + c) if r in row_d else c)
                sno = _clean(row_d.get("sno", "")) if "sno" in row_d else ""
                if sno and not SNO_RE.match(sno):
                    # junk in the number column (e.g. wrapped text) — treat as continuation
                    row_d.pop("sno")
                    sno = ""

                if sno:
                    close()
                    item = Item(sno.rstrip("."))
                    item.add(row_d)
                elif item is not None:
                    has_id = row_d.get("ia") or row_d.get("subno")
                    if has_id:
                        item.ias.append(_ia_row(row_d))
                    elif row_d.get("cp") and not item.f["cp"]:
                        item.add(row_d)
                    else:
                        # continuation of the previous row across a page break
                        if item.ias:
                            last_ia = item.ias[-1]
                            for k, tgt in (("pur", "pur"), ("sec", "s"), ("party", "p"), ("rem", "rem"), ("rp", "rp")):
                                if row_d.get(k):
                                    last_ia[tgt] = _clean(last_ia.get(tgt, "") + " " + row_d[k])
                            for k in ("pa", "ra"):
                                if row_d.get(k):
                                    last_ia[k] = "; ".join(filter(None, [last_ia.get(k, ""), *_join_counsel(row_d[k])]))
                        else:
                            item.add(row_d)
    close()
    bench["sections"] = [s for s in bench["sections"] if s["items"]]
    if not bench["judges"]:
        del bench["judges"]
    if not bench["vc"]:
        del bench["vc"]
    return bench


# ── Listing page ─────────────────────────────────────────────────────────────

def parse_listing(html: str) -> list[dict]:
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        a = re.search(r'href="([^"]+\.pdf)"', row, re.I)
        if not a:
            continue
        cells = [_clean(re.sub(r"<[^>]+>", " ", c)) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 5:
            continue
        title, bench, count, d = cells[1], cells[2], cells[3], cells[4]
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", d)
        if not m:
            continue
        out.append({
            "title": title,
            "bench": bench,
            "count": int(count) if count.isdigit() else None,
            "date": f"{m.group(3)}-{m.group(2)}-{m.group(1)}",
            "href": urljoin(SITE, a.group(1)),
        })
    return out


# ── Fetching ─────────────────────────────────────────────────────────────────

STATS: dict[str, int] = {}


def fetch(session, url, prev=None, pdf=True, params=None):
    status, body, meta = _fetch(session, url, prev, pdf, params)
    STATS[status] = STATS.get(status, 0) + 1
    return status, body, meta


def _fetch(session, url, prev, pdf, params):
    headers = {}
    if prev and prev.get("etag"):
        headers["If-None-Match"] = prev["etag"]
    elif prev and prev.get("modified"):
        headers["If-Modified-Since"] = prev["modified"]  # NCLT sends no ETag
    for attempt in range(3):
        try:
            r = session.get(url, headers=headers, params=params, timeout=60)
            break
        except requests.RequestException as e:
            if attempt == 2:
                print(f"  ! {url}: {e}", file=sys.stderr)
                return "error", None, None
            time.sleep(3 * (attempt + 1))
    if r.status_code == 304:
        return "same", None, prev
    if r.status_code == 404 or (r.status_code == 200 and not r.content):
        return "missing", None, None
    if r.status_code != 200 or (pdf and not r.content.startswith(b"%PDF")):
        print(f"  ! {url}: HTTP {r.status_code}, {len(r.content)} bytes, {r.headers.get('Content-Type', '')}", file=sys.stderr)
        return "error", None, None
    meta = {
        "etag": r.headers.get("ETag", ""),
        "modified": r.headers.get("Last-Modified", ""),
        "sha": hashlib.sha256(r.content).hexdigest()[:16],
        "bytes": len(r.content),
    }
    if prev and prev.get("sha") == meta["sha"]:
        return "same", None, meta
    return "new", r.content, meta


def read_day(session, date: str, max_pages=12):
    """All listing rows for one date (the page shows 20 per page)."""
    rows = []
    for page in range(max_pages):
        status, body, _ = fetch(session, LISTING, None, pdf=False, params={
            "field_cause_date_value": date, "field_cause_date_value_1": date, "page": page})
        if status != "new":
            return rows, status == "error"
        got = parse_listing(body.decode("utf-8", "replace"))
        if not got:
            break
        rows.extend(got)
        time.sleep(0.4)
    return rows, False


def list_key(row: dict) -> str:
    return row["href"]


def build_date(session, date, rows, index, force) -> bool:
    entry = index["dates"].get(date, {"files": {}})
    files = entry["files"]
    changed = False
    fresh = {}
    present = set()

    # stable names: location-court-kind, numbered when a court has several of a kind
    seen = {}
    named = []
    for row in sorted(rows, key=lambda r: r["href"]):
        b = parse_bench(row["bench"])
        kind = kind_of(row["title"] + " " + row["href"].rsplit("/", 1)[-1])
        base = f"{b['loc']}|{b['court']}|{kind}"
        seen[base] = seen.get(base, 0) + 1
        named.append((row, b, kind, base + (f"|{seen[base]}" if seen[base] > 1 else "")))

    for row, b, kind, name in named:
        present.add(name)
        prev = None if force else files.get(name)
        if prev and prev.get("url") != row["href"]:
            prev = None
        status, body, meta = fetch(session, row["href"], prev)
        if status in ("missing", "error"):
            continue
        meta = {**(meta or {}), "url": row["href"], "loc": b["loc"], "court": b["court"], "kind": kind,
                "title": row["title"], "count": row["count"]}
        if status == "new":
            fresh[name] = body
            files[name] = meta
            changed = True
            print(f"  + {date} {name} ({meta['bytes'] // 1024} KB)")
        else:
            files[name] = {**files.get(name, {}), **meta}

    for name in list(files):
        if name not in present:
            del files[name]
            changed = True

    if not files:
        return False
    if not changed and (DATA / f"{date}.json").exists():
        return False

    lists, benches = [], []
    for name, meta in sorted(files.items(), key=lambda kv: loc_sort(kv[1]["loc"]) + (court_num(kv[1]["court"]), KIND_ORDER[kv[1]["kind"]], kv[0])):
        body = fresh.get(name)
        if body is None:
            status, body, _ = fetch(session, meta["url"], None)
            if status != "new":
                continue
        if meta["kind"] == "notice":
            lists.append({"name": name, "loc": meta["loc"], "court": meta["court"], "kind": "notice", "label": meta["title"] or "Notice",
                          "title": meta["title"], "url": meta["url"], "published": meta.get("modified"), "items": 0, "notice": True})
            continue
        try:
            parsed = parse_pdf(body)
        except Exception as e:
            print(f"  ! parse failed for {date} {name}: {e}", file=sys.stderr)
            parsed = {"time": "", "sections": []}
        n_items = sum(len(s["items"]) for s in parsed["sections"])
        entry_l = {
            "name": name, "loc": meta["loc"], "court": meta["court"], "kind": meta["kind"],
            "label": KIND_LABEL[meta["kind"]], "title": meta["title"], "url": meta["url"],
            "published": meta.get("modified"), "items": n_items, "listed": meta.get("count"),
        }
        # Say so when the parse can't be trusted: scanned PDFs have no text,
        # and a page can be missing an item the Tribunal's own count includes.
        if n_items == 0:
            entry_l["unparsed"] = True
        else:
            gaps = find_gaps(parsed)
            if gaps:
                entry_l["gaps"] = gaps
            if gaps or (meta.get("count") and n_items < meta["count"] * 0.9):
                entry_l["partial"] = True
        lists.append(entry_l)
        benches.append({"list": name, "loc": meta["loc"], "court": meta["court"], "kind": meta["kind"], **parsed})

    out = {"date": date, "fetched": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"), "lists": lists, "benches": benches}
    (DATA / f"{date}.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    entry["files"] = files
    entry["items"] = sum(l["items"] for l in lists)
    entry["fetched"] = out["fetched"]
    index["dates"][date] = entry
    return True


def find_gaps(parsed: dict) -> list[str]:
    """Item numbers missing from inside a numbered run (e.g. 519, 521 -> 520)."""
    gaps = []
    for s in parsed["sections"]:
        prev = None
        for it in s["items"]:
            m = re.fullmatch(r"([A-Za-z]*)(\d+)", it["n"])
            if not m:
                continue
            cur = (m.group(1), int(m.group(2)))
            if prev and prev[0] == cur[0] and cur[1] > prev[1] + 1 and cur[1] % 100 != 1:
                gaps.extend(f"{cur[0]}{k}" for k in range(prev[1] + 1, min(cur[1], prev[1] + 6)))
            prev = cur
    return gaps


def loc_sort(loc: str) -> tuple:
    if loc in LOCATION_FIRST:
        return (0, loc)
    if loc in LOCATION_LAST:
        return (2, loc)
    return (1, loc)


def court_num(c: str) -> int:
    return ROMAN_TO_INT.get(c, 99)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days-back", type=int, default=2)
    ap.add_argument("--days-ahead", type=int, default=7)
    ap.add_argument("--keep-days", type=int, default=60, help="delete parsed dates older than this")
    ap.add_argument("--date", action="append", help="scrape only this date (YYYY-MM-DD); repeatable")
    ap.add_argument("--force", action="store_true", help="re-download and re-parse even if unchanged")
    ap.add_argument("--parse", metavar="PDF", help="just parse a local PDF and print JSON")
    args = ap.parse_args()

    if args.parse:
        print(json.dumps(parse_pdf(args.parse), ensure_ascii=False, indent=1))
        return

    DATA.mkdir(exist_ok=True)
    idx_path = DATA / "index.json"
    index = json.loads(idx_path.read_text()) if idx_path.exists() else {"dates": {}}

    today = dt.datetime.now(IST).date()
    if args.date:
        dates = sorted(args.date)
    else:
        dates = [(today + dt.timedelta(days=d)).isoformat() for d in range(-args.days_back, args.days_ahead + 1)]

    session = requests.Session()
    session.headers.update(HEADERS)
    changed = False
    total_rows = 0
    for date in dates:
        rows, failed = read_day(session, date)
        total_rows += len(rows)
        if failed or not rows:
            continue
        print(f"{date}: {len(rows)} files")
        changed |= build_date(session, date, rows, index, args.force)

    print("Requests: " + ", ".join(f"{k} {v}" for k, v in sorted(STATS.items())))
    if STATS.get("error") and not (STATS.get("new") or STATS.get("same")):
        print("::error::Every request to NCLT failed — the site may be blocking this runner.")
        sys.exit(1)

    cutoff = (today - dt.timedelta(days=args.keep_days)).isoformat()
    for date in list(index["dates"]):
        if date < cutoff:
            del index["dates"][date]
            (DATA / f"{date}.json").unlink(missing_ok=True)
            changed = True

    if not changed and idx_path.exists():
        print("No changes.")
        return
    index["dates"] = dict(sorted(index["dates"].items()))
    index["updated"] = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
