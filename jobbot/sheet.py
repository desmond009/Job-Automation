"""Import hand-picked HN hiring posts from a spreadsheet (e.g. a TextBackdoor .xlsx).

Only the post link and date are read from the sheet. Each post's full text is
fetched from HN, so imported jobs look exactly like ones from the HN fetcher and
go through the same pre-filter, scoring and contact steps.
"""
import re
import xml.etree.ElementTree as ET
import zipfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .sources import hn_post, session

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
HN_ITEM = re.compile(r"news\.ycombinator\.com/item\?id=(\d+)")


GSHEET = re.compile(r"docs\.google\.com/spreadsheets/d/([\w-]+)")


def local_copy(path):
    """A local .xlsx for a file path or a Google Sheets link (the sheet must be viewable by link)."""
    m = GSHEET.search(str(path))
    if not m:
        return Path(path).expanduser()
    r = session.get(f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx", timeout=60)
    if not r.ok or not r.content.startswith(b"PK"):
        raise ValueError(f"could not download the sheet (HTTP {r.status_code}); is it shared as 'anyone with the link'?")
    out = Path(tempfile.gettempdir()) / f"jobbot-{m.group(1)}.xlsx"
    out.write_bytes(r.content)
    return out


def read_rows(path):
    """Rows of the first sheet as lists of strings (stdlib only, no openpyxl)."""
    with zipfile.ZipFile(path) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
                strings.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in sheet.iterfind(".//m:row", NS):
        cells = {}
        for c in row.iterfind("m:c", NS):
            col = re.match(r"[A-Z]+", c.get("r")).group()
            v = c.find("m:v", NS)
            if c.get("t") == "s" and v is not None:
                cells[col] = strings[int(v.text)]
            elif c.get("t") == "inlineStr":
                cells[col] = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
            else:
                cells[col] = v.text if v is not None else ""
        width = max((_col_index(k) for k in cells), default=-1) + 1
        rows.append([cells.get(_col_name(i), "") for i in range(width)])
    return rows


def _col_index(name):
    n = 0
    for ch in name:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _col_name(i):
    name = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        name = chr(65 + r) + name
    return name


def hn_links(path):
    """(item id, post date) for every row that links an HN post, in sheet order."""
    rows = read_rows(path)
    header = next((i for i, r in enumerate(rows) if "Company" in r), None)
    if header is None:
        raise ValueError("no header row with a 'Company' column")
    cols = rows[header]
    date_col = next((i for i, c in enumerate(cols) if "date" in c.lower()), None)
    seen = {}
    for row in rows[header + 1:]:
        for cell in row:
            m = HN_ITEM.search(cell or "")
            if m:
                date = row[date_col] if date_col is not None and date_col < len(row) else ""
                seen.setdefault(m.group(1), date)
                break
    return list(seen.items())


def fetch_jobs(path, max_age_days, log):
    """Yield jobs (source "sheet") for posts newer than max_age_days that still exist on HN."""
    cutoff = (datetime.now() - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    links = hn_links(path)
    fresh = [(item, date) for item, date in links if not date or date >= cutoff]
    log(f"  {len(links)} HN posts in the sheet; {len(links) - len(fresh)} older than {max_age_days} days skipped")
    gone = 0
    for item, _ in fresh:
        try:
            post = session.get(f"https://hn.algolia.com/api/v1/items/{item}", timeout=30).json()
        except Exception as e:
            log(f"  ! hn item {item}: {e}")
            continue
        if not post.get("text"):  # deleted or flagged
            gone += 1
            continue
        yield hn_post(post, source="sheet")
    if gone:
        log(f"  {gone} posts deleted on HN")
