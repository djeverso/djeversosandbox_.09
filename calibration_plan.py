"""
calibration_plan.py
Fetches the PK12 Item Calibration Tracking Confluence page, parses all tables,
and generates a self-contained interactive HTML report for Psychometrics, Content,
Engineering, and Product stakeholders.
"""

import base64
import json
import os
import re
import ssl
import threading
import urllib.request
import urllib.parse
import urllib.error
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFLUENCE_BASE  = 'https://illuminate.atlassian.net/wiki'
PAGE_ID          = '19342884975'
JIRA_EMAIL       = os.environ.get('JIRA_EMAIL', 'david.everson@renaissance.com')
JIRA_TOKEN       = os.environ['JIRA_API_TOKEN']

OUTPUT_DIR     = r'C:\Users\DJEVERSO\.claude\projects\Content-Smartsheet-Roadmap\results\calibration-plan'

GITHUB_TOKEN   = os.environ.get('GITHUB_TOKEN_VAL', '')
GITHUB_REPO    = 'djeverso/djeversosandbox_.09'
GITHUB_FILE    = 'calibration-plan.html'
GITHUB_BRANCH  = 'main'

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx

def confluence_get(path, params=None):
    url = f'{CONFLUENCE_BASE}{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    import base64
    creds = base64.b64encode(f'{JIRA_EMAIL}:{JIRA_TOKEN}'.encode()).decode()
    req = urllib.request.Request(url, headers={
        'Authorization': f'Basic {creds}',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, context=_ctx()) as resp:
        return json.loads(resp.read())

# ---------------------------------------------------------------------------
# Fetch page as storage (XHTML) format
# ---------------------------------------------------------------------------

def fetch_page_html():
    print('Fetching Confluence page...')
    data = confluence_get(
        f'/rest/api/content/{PAGE_ID}',
        params={'expand': 'body.storage,title'}
    )
    return data['body']['storage']['value'], data['title']

# ---------------------------------------------------------------------------
# Parse tables from Confluence storage XHTML
# ---------------------------------------------------------------------------

def strip_tags(s):
    """Remove XML/HTML tags, collapse whitespace."""
    s = re.sub(r'<[^>]+>', ' ', s or '')
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def parse_tables(html):
    """
    Walk the storage XHTML and find all tables, resolving each table's title
    from the nearest preceding heading and the H2 section it lives under.

    Returns list of dicts, one per data row:
      priority, title, product, grade_band, item_count,
      cal_start, cal_end, recruitment, platform, notes
    """
    rows = []

    # ── Flatten the doc into a sequence of (tag, text) tokens ──────────────
    # We need to track: current H2, current H3, and table cells.
    tokens = list(re.finditer(
        r'<(h[123456]|table|tr|th|td|/table|/tr)[^>]*>(.*?)(?=<(?:h[123456]|table|tr|th|td|/table|/tr)[^>]*>|$)',
        html, re.DOTALL | re.IGNORECASE
    ))

    current_h2    = ''
    current_title = ''
    in_table      = False
    header_row    = []
    current_row   = []
    col_map       = {}   # lowercased col name → index

    # We'll do a simpler linear scan with a state machine
    # Split into block-level chunks
    chunks = re.split(r'(</?(?:h[1-6]|table|thead|tbody|colgroup|col|tr|th|td)[^>]*>)', html, flags=re.IGNORECASE)

    state       = 'outside'   # outside | in_table | in_header_row | in_row | in_cell
    current_h2  = 'Unknown'
    current_h3  = ''
    current_tag = ''
    cell_buf    = ''
    header_cells = []
    row_cells    = []
    col_index    = 0

    def normalize_col(s):
        return re.sub(r'\s+', ' ', strip_tags(s)).strip().lower().rstrip('-').strip()

    row_counters = {}   # title → count of data rows seen, for stable keys

    def make_row(title, h2, headers, cells):
        """Map header→cell, fill missing with ''."""
        m = {}
        for i, h in enumerate(headers):
            m[normalize_col(h)] = cells[i] if i < len(cells) else ''

        def g(*keys):
            for k in keys:
                if k in m: return m[k]
            return ''

        product    = g('product', 'prod')
        grade_band = g('grade band', 'grade')
        item_count = g('item count', 'items', 'count')
        cal_start  = g('calibration start date', 'calibration start',
                       'field test start date', 'field test start', 'start date', 'start')
        cal_end    = g('calibration end date', 'calibration end',
                       'field test end date', 'field test end', 'end date', 'end')
        recruit    = g('recruit-ment', 'recruitment', 'recruit')
        platform   = g('platform')
        notes      = g('notes')

        # Priority from H2
        if 'required' in h2.lower():
            priority = 'Required'
        elif 'bolster' in h2.lower():
            priority = 'Bolstering'
        else:
            priority = h2

        row_counters[title] = row_counters.get(title, 0) + 1
        return {
            'priority':    priority,
            'title':       title,
            'product':     product,
            'grade_band':  grade_band,
            'item_count':  item_count,
            'cal_start':   cal_start,
            'cal_end':     cal_end,
            'recruitment': recruit,
            'platform':    platform,
            'notes':       notes,
            '_row_key':    f'{title}::{row_counters[title]}',
            '_headers':    [normalize_col(h) for h in headers],
        }

    i = 0
    in_table_flag   = False
    in_row_flag     = False
    in_cell_flag    = False
    is_header_row   = False
    header_done     = False
    table_headers   = []
    row_buf         = []
    has_numbering   = False   # True when table has a leading numbering column
    current_h3      = ''
    last_text_buf   = ''

    while i < len(chunks):
        chunk = chunks[i]
        if not chunk:
            i += 1
            continue

        tag_match = re.match(r'^<(/?)(\w+)', chunk, re.IGNORECASE)
        if not tag_match:
            # Plain text content
            text = strip_tags(chunk)
            if text:
                if in_cell_flag:
                    cell_buf += (' ' if cell_buf else '') + text
                else:
                    last_text_buf = text
            i += 1
            continue

        closing = tag_match.group(1) == '/'
        tag     = tag_match.group(2).lower()

        # Non-structural tags (p, strong, em, br, ac:*, etc.) inside a cell
        # → treat the whole chunk as text to strip
        if tag not in ('h1','h2','h3','h4','h5','h6',
                       'table','thead','tbody','colgroup','col','tr','th','td'):
            if in_cell_flag:
                text = strip_tags(chunk)
                if text:
                    cell_buf += (' ' if cell_buf else '') + text
            i += 1
            continue

        if tag in ('h1','h2','h3','h4','h5','h6') and not closing:
            # Collect all text until closing tag
            close_pat = re.compile(f'</{tag}>', re.IGNORECASE)
            j = i + 1
            heading_parts = []
            while j < len(chunks):
                cm = re.match(r'^<(/?)(\w+)', chunks[j], re.IGNORECASE)
                if cm and cm.group(1) == '/' and cm.group(2).lower() == tag:
                    j += 1
                    break
                heading_parts.append(strip_tags(chunks[j]))
                j += 1
            heading_text = ' '.join(p for p in heading_parts if p).strip()
            if tag == 'h2':
                current_h2 = heading_text
                current_h3 = ''
            elif tag == 'h3':
                current_h3 = heading_text
            i = j
            continue

        if tag == 'table' and not closing:
            in_table_flag = True
            table_headers = []
            header_done   = False
            has_numbering = False
            i += 1
            continue

        if tag == 'table' and closing:
            in_table_flag = False
            i += 1
            continue

        if tag == 'tr' and not closing:
            in_row_flag   = True
            is_header_row = False
            row_buf       = []
            i += 1
            continue

        if tag == 'tr' and closing:
            in_row_flag = False
            if in_table_flag and row_buf:
                if not header_done:
                    if any(c.strip() for c in row_buf):
                        table_headers = row_buf[:]
                        header_done   = True
                else:
                    if any(c.strip() for c in row_buf):
                        r = make_row(current_h3 or current_h2, current_h2, table_headers, row_buf)
                        rows.append(r)
            row_buf = []
            i += 1
            continue

        if tag in ('th', 'td') and not closing:
            # Self-closing like <th /> — detect numbering column, skip
            if chunk.rstrip().endswith('/>'):
                if in_row_flag:
                    if 'numberingColumn' in chunk:
                        has_numbering = True
                    else:
                        row_buf.append('')
                i += 1
                continue
            # Skip numbering column cells entirely
            if 'numberingColumn' in chunk:
                has_numbering = True
                # consume until matching close tag
                j = i + 1
                depth = 1
                while j < len(chunks) and depth > 0:
                    cm = re.match(r'^<(/?)(\w+)', chunks[j], re.IGNORECASE)
                    if cm and cm.group(2).lower() == tag:
                        depth += (0 if cm.group(1) == '/' else 1) - (1 if cm.group(1) == '/' else 0)
                        if cm.group(1) == '/': depth -= 1;
                        if cm.group(1) == '': depth += 1
                    j += 1
                # find the closing td/th
                j = i + 1
                while j < len(chunks):
                    cm = re.match(r'^<(/?)(\w+)', chunks[j], re.IGNORECASE)
                    if cm and cm.group(1) == '/' and cm.group(2).lower() == tag:
                        j += 1
                        break
                    j += 1
                i = j
                continue
            in_cell_flag = True
            cell_buf     = ''
            i += 1
            continue

        if tag in ('th', 'td') and closing:
            in_cell_flag = False
            if in_row_flag:
                row_buf.append(cell_buf.strip())
            cell_buf = ''
            i += 1
            continue

        i += 1

    # Remove rows where all key fields are blank (empty placeholder rows)
    rows = [r for r in rows if any(r[k].strip() for k in ('product','grade_band','item_count','notes'))]

    print(f'Parsed {len(rows)} data rows from {len(set(r["title"] for r in rows))} tables.')
    return rows

# ---------------------------------------------------------------------------
# GitHub upload
# ---------------------------------------------------------------------------

def push_to_github(local_path):
    import base64
    api = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}'
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
    }
    with open(local_path, 'rb') as f:
        content_b64 = base64.b64encode(f.read()).decode()

    ctx = _ctx()
    req = urllib.request.Request(api, headers=headers)
    sha = None
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            sha = json.loads(resp.read())['sha']
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    payload = {
        'message': f'Update calibration-plan.html ({date.today().isoformat()})',
        'content': content_b64,
        'branch':  GITHUB_BRANCH,
    }
    if sha:
        payload['sha'] = sha

    body = json.dumps(payload).encode()
    req  = urllib.request.Request(api, data=body, method='PUT', headers=headers)
    with urllib.request.urlopen(req, context=ctx) as resp:
        result = json.loads(resp.read())
    return result['content']['html_url']

# ---------------------------------------------------------------------------
# Confluence write-back
# ---------------------------------------------------------------------------

def get_page_version():
    data = confluence_get(f'/rest/api/content/{PAGE_ID}', params={'expand': 'version'})
    return data['version']['number']

def update_cell_in_storage(storage_html, row_key, field_key, new_value):
    """
    Locate the Nth data row of the table whose H3/H2 title matches row_key,
    find the column matching field_key, replace that cell's text content.

    row_key format: "Table Title::N"  (N is 1-based data-row index)
    field_key: one of the normalized column names (e.g. 'product', 'cal_start')
    """
    title_part, n_str = row_key.rsplit('::', 1)
    target_row_n = int(n_str)

    # We rebuild the storage HTML by re-running the same chunked parse,
    # but this time replacing the target cell content.
    chunks = re.split(r'(</?(?:h[1-6]|table|thead|tbody|colgroup|col|tr|th|td)[^>]*>)',
                      storage_html, flags=re.IGNORECASE)

    current_h2 = ''
    current_h3 = ''
    in_table_flag = False
    in_row_flag   = False
    in_cell_flag  = False
    header_done   = False
    table_headers = []
    row_buf_idx   = []   # (chunk_index, is_numbering) per cell
    row_count     = 0
    target_col    = None
    target_cell_start = None
    target_cell_end   = None

    def normalize_col(s):
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', s or '')).strip().lower().rstrip('-').strip()

    i = 0
    cell_start_i = None
    current_cell_chunks = []

    while i < len(chunks):
        chunk = chunks[i]
        tm = re.match(r'^<(/?)(\w+)', chunk, re.IGNORECASE)

        if not tm:
            i += 1
            continue

        closing = tm.group(1) == '/'
        tag = tm.group(2).lower()

        if tag not in ('h1','h2','h3','h4','h5','h6',
                       'table','thead','tbody','colgroup','col','tr','th','td'):
            i += 1
            continue

        if tag in ('h1','h2','h3','h4','h5','h6') and not closing:
            j = i + 1
            parts = []
            while j < len(chunks):
                cm = re.match(r'^<(/?)(\w+)', chunks[j], re.IGNORECASE)
                if cm and cm.group(1) == '/' and cm.group(2).lower() == tag:
                    j += 1; break
                parts.append(re.sub(r'<[^>]+>', ' ', chunks[j]))
                j += 1
            ht = ' '.join(p.strip() for p in parts if p.strip())
            if tag == 'h2': current_h2 = ht; current_h3 = ''
            elif tag == 'h3': current_h3 = ht
            i = j; continue

        if tag == 'table' and not closing:
            in_table_flag = True; table_headers = []; header_done = False; row_count = 0
            i += 1; continue
        if tag == 'table' and closing:
            in_table_flag = False; i += 1; continue

        if tag == 'tr' and not closing:
            in_row_flag = True; row_buf_idx = []; i += 1; continue

        if tag == 'tr' and closing:
            in_row_flag = False
            if in_table_flag and row_buf_idx:
                if not header_done:
                    header_done = True
                else:
                    row_count += 1
                    cur_title = current_h3 or current_h2
                    if cur_title == title_part and row_count == target_row_n:
                        # Find target column index
                        col_idx = None
                        for ci, (hdr_norm,) in enumerate([(normalize_col(h),) for h in table_headers]):
                            candidates = {
                                'product': ['product','prod'],
                                'grade_band': ['grade band','grade'],
                                'item_count': ['item count','items','count'],
                                'cal_start': ['calibration start date','calibration start','field test start date','field test start','start date','start'],
                                'cal_end': ['calibration end date','calibration end','field test end date','field test end','end date','end'],
                                'recruitment': ['recruit-ment','recruitment','recruit'],
                                'platform': ['platform'],
                                'notes': ['notes'],
                            }.get(field_key, [field_key])
                            if hdr_norm in candidates:
                                col_idx = ci; break
                        if col_idx is not None and col_idx < len(row_buf_idx):
                            target_cell_start, target_cell_end = row_buf_idx[col_idx]
            i += 1; continue

        if tag in ('th','td') and not closing:
            if 'numberingColumn' in chunk:
                j = i + 1
                while j < len(chunks):
                    cm = re.match(r'^<(/?)(\w+)', chunks[j], re.IGNORECASE)
                    if cm and cm.group(1) == '/' and cm.group(2).lower() == tag:
                        j += 1; break
                    j += 1
                i = j; continue
            if chunk.rstrip().endswith('/>'):
                if header_done: row_buf_idx.append((i, i))
                i += 1; continue
            cell_start_i = i
            i += 1; continue

        if tag in ('th','td') and closing:
            if in_row_flag and cell_start_i is not None:
                if not header_done:
                    # collect header text
                    parts = []
                    for k in range(cell_start_i + 1, i):
                        parts.append(re.sub(r'<[^>]+>', ' ', chunks[k]))
                    table_headers.append(' '.join(p.strip() for p in parts if p.strip()))
                else:
                    row_buf_idx.append((cell_start_i, i))
            cell_start_i = None
            i += 1; continue

        i += 1

    if target_cell_start is None:
        raise ValueError(f'Could not locate cell for row_key={row_key!r} field={field_key!r}')

    # Replace the content between the opening and closing td/th tags
    # with a simple <p> wrapping new_value (Confluence storage format)
    open_tag  = chunks[target_cell_start]
    close_tag = chunks[target_cell_end]
    escaped   = new_value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    new_cell_content = f'<p>{escaped}</p>'

    new_chunks = (chunks[:target_cell_start + 1]
                  + [new_cell_content]
                  + chunks[target_cell_end:])
    return ''.join(new_chunks)


def save_to_confluence(row_key, field_key, new_value):
    """Fetch current storage, patch one cell, PUT back."""
    data = confluence_get(
        f'/rest/api/content/{PAGE_ID}',
        params={'expand': 'body.storage,version,title'}
    )
    storage = data['body']['storage']['value']
    version = data['version']['number']
    title   = data['title']

    new_storage = update_cell_in_storage(storage, row_key, field_key, new_value)

    import base64
    creds = base64.b64encode(f'{JIRA_EMAIL}:{JIRA_TOKEN}'.encode()).decode()
    url   = f'{CONFLUENCE_BASE}/rest/api/content/{PAGE_ID}'
    payload = json.dumps({
        'version': {'number': version + 1},
        'title':   title,
        'type':    'page',
        'body':    {'storage': {'value': new_storage, 'representation': 'storage'}},
    }).encode()
    req = urllib.request.Request(url, data=payload, method='PUT', headers={
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    })
    ctx = _ctx()
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Local HTTP server (for editable mode)
# ---------------------------------------------------------------------------

SERVER_PORT = 7432
_server_html = ''   # set by serve_mode()

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass   # silence access log

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            body = _server_html.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == '/save':
            length  = int(self.headers.get('Content-Length', 0))
            body    = json.loads(self.rfile.read(length))
            row_key    = body.get('row_key', '')
            field_key  = body.get('field_key', '')
            new_value  = body.get('value', '')
            try:
                save_to_confluence(row_key, field_key, new_value)
                resp = json.dumps({'ok': True}).encode()
                self.send_response(200)
            except Exception as e:
                resp = json.dumps({'ok': False, 'error': str(e)}).encode()
                self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()


def serve_mode(html_content):
    global _server_html
    _server_html = html_content
    httpd = HTTPServer(('127.0.0.1', SERVER_PORT), _Handler)
    url = f'http://127.0.0.1:{SERVER_PORT}'
    print(f'Editable report running at {url}')
    print('Open that URL in Chrome or Edge. Press Ctrl+C to stop.')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nServer stopped.')


# ---------------------------------------------------------------------------
# Unique output path
# ---------------------------------------------------------------------------

def unique_output_path(base_dir, filename):
    path = os.path.join(base_dir, filename)
    if not os.path.exists(path): return path
    stem, ext = os.path.splitext(filename)
    v = 2
    while True:
        candidate = os.path.join(base_dir, f'{stem}-v{v}{ext}')
        if not os.path.exists(candidate): return candidate
        v += 1

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>PK12 Item Calibration Tracking</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 13px;
  background: #f4f6f9;
  color: #1a1a2e;
  min-height: 100vh;
}

/* ── Header ── */
#page-header {
  background: #1a1a2e;
  color: #fff;
  padding: 7px 28px;
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
#page-header h1 { font-size: 15px; font-weight: 700; white-space: nowrap; }
#page-header .subtitle { font-size: 11px; color: rgba(255,255,255,0.45); white-space: nowrap; }
.header-meta { display: flex; gap: 8px; flex-wrap: wrap; margin-left: auto; }
.header-badge {
  font-size: 11px; background: rgba(255,255,255,0.12);
  border-radius: 4px; padding: 2px 8px; color: rgba(255,255,255,0.8);
  white-space: nowrap;
}

/* ── Toolbar ── */
#toolbar {
  background: #fff;
  border-bottom: 1px solid #e2e5ea;
  padding: 8px 28px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  position: sticky;
  top: 0;
  z-index: 200;
  box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}
.filter-group { display: flex; align-items: center; gap: 5px; }
.filter-label { font-size: 11px; color: #6b7280; white-space: nowrap; }
.tb-sep { width: 1px; height: 18px; background: #e2e5ea; flex-shrink: 0; }

.dd-wrapper { position: relative; }
.dd-btn {
  padding: 4px 10px; border-radius: 4px;
  border: 1px solid #d1d5db; background: #fff;
  color: #374151; cursor: pointer; font-size: 11px;
  white-space: nowrap; min-width: 90px;
}
.dd-btn:hover { background: #f0f4ff; border-color: #93c5fd; }
.dd-panel {
  display: none; position: absolute; top: calc(100% + 3px); left: 0;
  background: #fff; border: 1px solid #d1d5db; border-radius: 6px;
  box-shadow: 0 6px 18px rgba(0,0,0,0.12); z-index: 300;
  min-width: 200px; max-height: 260px; overflow-y: auto;
}
.dd-panel.open { display: block; }
.dd-opt { display: flex; align-items: center; gap: 7px; padding: 5px 10px; cursor: pointer; font-size: 11px; }
.dd-opt:hover { background: #f5f7ff; }
.dd-opt input { cursor: pointer; margin: 0; }
.dd-divider { height: 1px; background: #e5e7eb; margin: 3px 0; }

/* ── View toggle ── */
#view-toggle { display: flex; gap: 0; margin-left: auto; border: 1px solid #d1d5db; border-radius: 5px; overflow: hidden; }
.view-btn {
  padding: 4px 14px; background: #fff; border: none;
  cursor: pointer; font-size: 11px; color: #374151;
  transition: all 0.12s;
}
.view-btn + .view-btn { border-left: 1px solid #d1d5db; }
.view-btn:hover { background: #f0f4ff; }
.view-btn.active { background: #1a1a2e; color: #fff; }

#result-count { font-size: 11px; color: #9ca3af; white-space: nowrap; }

/* ── Pills ── */
.pill {
  font-size: 10px; font-weight: 600; padding: 2px 7px;
  border-radius: 10px; white-space: nowrap; display: inline-block;
}
.pill-required   { background: #fee2e2; color: #dc2626; }
.pill-bolstering { background: #fef3c7; color: #d97706; }
.pill-gray  { background: #f3f4f6; color: #6b7280; }
.pill-teal  { background: #ccfbf1; color: #0f766e; }

/* ── Main content ── */
#main-content { padding: 16px 28px 60px; }

/* ── Flat table ── */
.data-table { width: 100%; border-collapse: collapse; }
.data-table th {
  text-align: left; font-size: 10px; font-weight: 700;
  color: #9ca3af; text-transform: uppercase; letter-spacing: 0.05em;
  padding: 6px 10px; white-space: nowrap; background: #fff;
  border-bottom: 2px solid #e2e5ea;
}
.data-table td {
  padding: 7px 10px; vertical-align: top;
  border-bottom: 1px solid #f3f4f6; font-size: 12px;
  background: #fff;
}
.data-table tr:hover td { background: #f8f9fb; }
.data-table tr.priority-required  td:first-child { border-left: 3px solid #dc2626; }
.data-table tr.priority-bolstering td:first-child { border-left: 3px solid #d97706; }

.cell-title { font-weight: 600; font-size: 11px; color: #374151; max-width: 180px; line-height: 1.3; }
.cell-notes { font-size: 11px; color: #6b7280; max-width: 320px; line-height: 1.4; }
.cell-notes .full-text { display: none; }
.cell-notes.expanded .short-text { display: none; }
.cell-notes.expanded .full-text  { display: inline; }
.expand-btn { color: #2563eb; font-size: 10px; cursor: pointer; text-decoration: underline; }
.cell-date { white-space: nowrap; font-size: 11px; }
.cell-date.has-date { color: #1a1a2e; }
.cell-date.no-date  { color: #d1d5db; font-style: italic; }
.date-tbd { color: #9ca3af; font-style: italic; }
.recruit-yes   { color: #dc2626; font-weight: 700; }
.recruit-no    { color: #9ca3af; }
.recruit-tbd   { color: #d97706; font-style: italic; }
.recruit-maybe { color: #d97706; font-weight: 600; }

/* ── Timeline ── */
#timeline-wrap {
  background: #fff;
  border-radius: 8px;
  border: 1px solid #e2e5ea;
  overflow: hidden;
}
/* Single scrollable container: label column sticky, chart scrolls right */
.tl-scroll-container {
  overflow-x: auto;
  overflow-y: visible;
}
.tl-table {
  border-collapse: collapse;
  min-width: 100%;
}
.tl-table td, .tl-table th {
  padding: 0;
  margin: 0;
}
.tl-header-label {
  position: sticky;
  left: 0;
  background: #f8f9fb;
  z-index: 10;
  width: 200px;
  min-width: 200px;
  max-width: 200px;
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.05em; color: #9ca3af;
  padding: 7px 12px;
  border-bottom: 2px solid #e2e5ea;
  border-right: 2px solid #e2e5ea;
  height: 32px;
  vertical-align: middle;
}
.tl-header-chart {
  background: #f8f9fb;
  border-bottom: 2px solid #e2e5ea;
  vertical-align: middle;
  padding: 0;
  white-space: nowrap;
}
.tl-month-cells {
  display: flex;
}
.tl-month-cell {
  flex-shrink: 0;
  font-size: 10px; font-weight: 600; color: #6b7280;
  padding: 7px 0; text-align: center;
  border-left: 1px solid #e2e5ea;
  white-space: nowrap;
  user-select: none;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.tl-month-cell.is-today-month {
  background: #fef9ec;
  color: #b45309;
  font-weight: 700;
}
.tl-data-row {
  border-bottom: 1px solid #f3f4f6;
}
.tl-data-row:last-child { border-bottom: none; }
.tl-data-row:hover .tl-label-cell,
.tl-data-row:hover .tl-bar-td { background: #f8f9fb; }
.tl-label-cell {
  position: sticky;
  left: 0;
  background: #fafafa;
  z-index: 5;
  width: 200px;
  min-width: 200px;
  max-width: 200px;
  padding: 6px 12px;
  font-size: 11px;
  border-right: 2px solid #e2e5ea;
  line-height: 1.3;
  vertical-align: middle;
}
.tl-label-product { font-weight: 600; color: #1a1a2e; }
.tl-label-grade   { font-size: 10px; color: #6b7280; }
.tl-bar-td {
  vertical-align: middle;
  padding: 0;
  background: #fff;
}
.tl-bar-row {
  position: relative;
  height: 42px;
}
.tl-grid-line {
  position: absolute;
  top: 0; bottom: 0;
  width: 1px;
  background: #e2e5ea;
  pointer-events: none;
}
.tl-today-line {
  position: absolute;
  top: 0; bottom: 0;
  width: 2px;
  background: #ef4444;
  opacity: 0.7;
  pointer-events: none;
  z-index: 4;
}
.tl-bar {
  position: absolute;
  top: 50%; transform: translateY(-50%);
  height: 18px;
  border-radius: 4px;
  cursor: pointer;
  transition: filter 0.1s;
  display: flex;
  align-items: center;
  padding: 0 6px;
  font-size: 10px;
  font-weight: 600;
  color: #fff;
  overflow: hidden;
  white-space: nowrap;
  min-width: 6px;
  z-index: 2;
}
.tl-bar:hover { filter: brightness(1.12); z-index: 3; }
.tl-bar.psycho-bar {
  background: #7c3aed;
  font-size: 9px;
}
.tl-tbd-label {
  position: absolute;
  left: 8px;
  top: 50%; transform: translateY(-50%);
  font-size: 10px; color: #9ca3af;
  font-style: italic;
}
.tl-tbd-end-label {
  position: absolute;
  top: 50%; transform: translateY(-50%);
  font-size: 9px; color: rgba(255,255,255,0.85);
  font-style: italic;
  font-weight: 400;
  right: 4px;
  pointer-events: none;
}
.tl-legend {
  display: flex;
  gap: 16px;
  padding: 10px 14px;
  border-top: 1px solid #e2e5ea;
  font-size: 11px;
  color: #6b7280;
  background: #fafafa;
  flex-wrap: wrap;
}
.tl-legend-item { display: flex; align-items: center; gap: 5px; }
.tl-legend-swatch {
  width: 14px; height: 10px; border-radius: 2px;
  display: inline-block;
}

/* ── Tooltip ── */
#tooltip {
  display: none; position: fixed; z-index: 9999;
  background: #1a1a2e; color: #fff; border-radius: 7px;
  padding: 10px 14px; font-size: 11px; max-width: 360px;
  line-height: 1.55; pointer-events: none;
  box-shadow: 0 4px 20px rgba(0,0,0,0.3);
}
#tooltip strong { display: block; margin-bottom: 5px; font-size: 12px; border-bottom: 1px solid rgba(255,255,255,0.15); padding-bottom: 5px; }
.tt-row { display: flex; gap: 8px; margin-bottom: 2px; }
.tt-label { color: rgba(255,255,255,0.45); min-width: 80px; flex-shrink: 0; }

/* ── Empty ── */
.empty-state { text-align: center; padding: 60px 0; color: #9ca3af; font-size: 13px; }

/* ── Re-sync button ── */
#resync-btn {
  padding: 4px 12px; border-radius: 4px;
  border: 1px solid #d1d5db; background: #fff;
  color: #374151; cursor: pointer; font-size: 11px; font-weight: 600;
  white-space: nowrap; transition: all 0.12s;
}
#resync-btn:hover  { background: #f0fdf4; border-color: #86efac; color: #166534; }
#resync-btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* ── Editable cells ── */
.editable-cell {
  cursor: text;
  border-radius: 3px;
  padding: 1px 3px;
  margin: -1px -3px;
  transition: background 0.1s;
}
.editable-cell:hover { background: #f0f4ff; outline: 1px dashed #93c5fd; }
.editable-cell:focus {
  outline: 2px solid #2563eb;
  background: #fff;
  border-radius: 3px;
}
.save-indicator {
  display: inline-block; margin-left: 6px;
  font-size: 10px; font-weight: 600; padding: 1px 5px; border-radius: 3px;
}
.save-indicator.saving { background: #fef3c7; color: #92400e; }
.save-indicator.saved  { background: #d1fae5; color: #065f46; }
.save-indicator.error  { background: #fee2e2; color: #991b1b; }
#edit-banner {
  display: none; background: #dbeafe; border-bottom: 1px solid #93c5fd;
  padding: 6px 28px; font-size: 11px; color: #1e40af;
}
#edit-banner.visible { display: block; }
</style>
</head>
<body>

<div id="page-header">
  <h1>PK12 Item Calibration Tracking</h1>
  <div class="subtitle">Live from Confluence — refreshed each run</div>
  <div class="header-meta">
    <span class="header-badge" id="generated-badge"></span>
    <span class="header-badge" id="row-badge"></span>
    <a class="header-badge" style="color:rgba(255,255,255,0.7);text-decoration:none"
       href="https://illuminate.atlassian.net/wiki/spaces/CON/pages/19342884975/PK12+Item+Calibration+Tracking"
       target="_blank">Confluence ↗</a>
  </div>
</div>

<div id="toolbar">
  <div class="filter-group">
    <span class="filter-label">Priority:</span>
    <div class="dd-wrapper">
      <button class="dd-btn" id="btn-priority">All ▾</button>
      <div class="dd-panel" id="panel-priority"></div>
    </div>
  </div>
  <div class="tb-sep"></div>
  <div class="filter-group">
    <span class="filter-label">Table:</span>
    <div class="dd-wrapper">
      <button class="dd-btn" id="btn-title">All ▾</button>
      <div class="dd-panel" id="panel-title"></div>
    </div>
  </div>
  <div class="tb-sep"></div>
  <div class="filter-group">
    <span class="filter-label">Product:</span>
    <div class="dd-wrapper">
      <button class="dd-btn" id="btn-product">All ▾</button>
      <div class="dd-panel" id="panel-product"></div>
    </div>
  </div>
  <div class="tb-sep"></div>
  <div class="filter-group">
    <span class="filter-label">Platform:</span>
    <div class="dd-wrapper">
      <button class="dd-btn" id="btn-platform">All ▾</button>
      <div class="dd-panel" id="panel-platform"></div>
    </div>
  </div>
  <div class="tb-sep"></div>
  <div class="filter-group">
    <span class="filter-label">Recruitment:</span>
    <div class="dd-wrapper">
      <button class="dd-btn" id="btn-recruitment">All ▾</button>
      <div class="dd-panel" id="panel-recruitment"></div>
    </div>
  </div>
  <div class="tb-sep"></div>
  <span id="result-count"></span>
  <div id="view-toggle">
    <button class="view-btn" data-view="table">Table</button>
    <button class="view-btn active" data-view="timeline">Timeline</button>
  </div>
  <button id="resync-btn" onclick="triggerResync()" title="Re-fetch from Confluence and update this page">↺ Re-sync</button>
</div>
<div id="resync-banner" style="display:none;padding:6px 28px;font-size:11px;font-weight:600"></div>

<div id="edit-banner">✏️ <strong>Edit mode</strong> — click any cell in the Table view to edit it. Changes save directly to Confluence when you press Enter or click away.</div>
<div id="main-content"></div>
<div id="tooltip"></div>

<script>
const ALL_ROWS    = __DATA__;
const ROW_KEYS    = __ROW_KEYS__;
const GENERATED   = '__GENERATED__';
const EDITABLE    = __EDITABLE__;
const API_PORT    = __PORT__;
const GH_TOKEN    = '__GITHUB_TOKEN__';
const GH_REPO     = '__GITHUB_REPO__';
const GH_WORKFLOW = 'resync.yml';
const GH_BRANCH   = 'main';

const COL_LABELS = {
  priority:'Priority', title:'Table / Section', product:'Product',
  grade_band:'Grade Band', item_count:'Item Count',
  cal_start:'Field Test Start Date', cal_end:'Field Test End Date',
  recruitment:'Recruitment', platform:'Platform', notes:'Notes',
};

const ALL_COLS = ['priority','title','product','grade_band','item_count','cal_start','cal_end','recruitment','platform','notes'];

// ── State ──────────────────────────────────────────────────────────────────
const state = {
  view: 'timeline',
  priority: new Set(), title: new Set(), product: new Set(),
  platform: new Set(), recruitment: new Set(),
};

// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function uniq(arr) { return [...new Set(arr.filter(Boolean))].sort(); }

// ── Date parsing for timeline (fuzzy, season/quarter-aware) ───────────────
const MON = {jan:1,feb:2,mar:3,apr:4,may:5,jun:6,jul:7,aug:8,sep:9,oct:10,nov:11,dec:12};

// Strip non-date verbiage: keep only the first recognizable date fragment
function extractDatePart(s) {
  if (!s) return s;
  // Try to isolate up to the first non-date word after a date keyword
  // Remove anything after "with", "and", "for", parentheses, etc.
  return s.replace(/\s+(with|and|for|–|—|\(|\/\/|incl|including|per|as)\b.*/i, '').trim();
}

function parseTimelineDate(raw, isEnd) {
  if (!raw) return null;
  const s = extractDatePart(raw);
  const lc = s.toLowerCase().trim();
  if (!lc || lc.includes('tbd') || lc.includes('n/a') || lc.includes('still under') || lc === '—') return null;

  // Extract year from string (use 2026 as fallback)
  const yrMatch = lc.match(/\b(20\d{2})\b/);
  const yr = yrMatch ? +yrMatch[1] : 2026;
  const yr2match = lc.match(/\b(\d{2})\b/);
  const yr2 = yr2match ? 2000 + +yr2match[1] : yr;

  // Season / quarter keywords — different mappings for start vs end
  const START_SEASONS = {
    fall:6, bts:6, winter:0, spring:-1, pm1:6, pm2:0, boy:6, moy:0, eoy:2, pm3:2,
  };
  // month index (0-based) for start dates
  const START_MONTH_MAP = {
    fall:8, bts:8, winter:12, spring:1, pm1:8, pm2:12, boy:8, moy:12, eoy:4, pm3:4,
  };
  // month index (1-based) for end dates
  const END_MONTH_MAP = {
    fall:11, bts:11, winter:2, spring:5, pm1:9, pm2:2, boy:9, moy:2, eoy:4, pm3:5,
  };

  // Check season/quarter keywords
  for (const [kw, mo] of Object.entries(isEnd ? END_MONTH_MAP : START_MONTH_MAP)) {
    if (lc.includes(kw)) {
      const finalYr = (isEnd && mo <= 2 && lc.includes('winter')) ? yr : yr;
      return { year: finalYr, month: mo };
    }
  }

  // Named month: "Jan 2026" / "January 2026"
  const mn = lc.match(/\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*[\s,]+(\d{2,4})\b/);
  if (mn) {
    const y = mn[2].length === 2 ? 2000 + +mn[2] : +mn[2];
    return { year: y, month: MON[mn[1]] };
  }
  // "MM/DD/YY" or "MM/DD/YYYY"
  const sl = lc.match(/(\d{1,2})\/\d{1,2}\/(\d{2,4})/);
  if (sl) {
    const y = sl[2].length === 2 ? 2000 + +sl[2] : +sl[2];
    return { year: y, month: +sl[1] };
  }
  // Bare year
  if (lc.match(/^\d{4}$/)) return { year: +lc, month: isEnd ? 12 : 1 };

  return null;
}

function monthKey(y, m) { return y * 12 + m; }
function keyToYM(k)      { const m = k % 12; return { year: Math.floor(k / 12), month: m === 0 ? 12 : m }; }
const MONTH_NAMES = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

// ── Dropdowns ─────────────────────────────────────────────────────────────
function buildDropdown(btnId, panelId, items, stateKey) {
  const panel = document.getElementById(panelId);
  const btn   = document.getElementById(btnId);
  panel.innerHTML = '';
  const allId  = `${panelId}-all`;
  const allDiv = document.createElement('div');
  allDiv.className = 'dd-opt';
  allDiv.innerHTML = `<input type="checkbox" id="${allId}" checked><label for="${allId}"><strong>All</strong></label>`;
  panel.appendChild(allDiv);
  panel.appendChild(Object.assign(document.createElement('div'), {className:'dd-divider'}));
  items.forEach((item, i) => {
    const d = document.createElement('div'); d.className = 'dd-opt';
    const uid = `${panelId}-${i}`;
    d.innerHTML = `<input type="checkbox" id="${uid}" value="${esc(item)}" checked><label for="${uid}">${esc(item)}</label>`;
    panel.appendChild(d);
  });
  function sync() {
    const checked = [...panel.querySelectorAll('input[value]:checked')].map(c => c.value);
    state[stateKey] = checked.length === items.length ? new Set() : new Set(checked);
    btn.textContent = state[stateKey].size === 0 ? 'All ▾' : `${checked.length} selected ▾`;
    render();
  }
  const allChk = panel.querySelector('#' + allId);
  allChk.addEventListener('change', () => {
    panel.querySelectorAll('input[value]').forEach(c => c.checked = allChk.checked);
    sync();
  });
  panel.querySelectorAll('input[value]').forEach(c => c.addEventListener('change', () => {
    allChk.checked = [...panel.querySelectorAll('input[value]')].every(c => c.checked);
    sync();
  }));
  btn.addEventListener('click', e => { e.stopPropagation(); panel.classList.toggle('open'); });
  panel.addEventListener('click', e => e.stopPropagation());
}

document.addEventListener('click', () =>
  document.querySelectorAll('.dd-panel').forEach(p => p.classList.remove('open'))
);

function initDropdowns() {
  buildDropdown('btn-priority',    'panel-priority',    uniq(ALL_ROWS.map(r=>r.priority)),    'priority');
  buildDropdown('btn-title',       'panel-title',       uniq(ALL_ROWS.map(r=>r.title)),       'title');
  buildDropdown('btn-product',     'panel-product',     uniq(ALL_ROWS.map(r=>r.product)),     'product');
  buildDropdown('btn-platform',    'panel-platform',    uniq(ALL_ROWS.map(r=>r.platform)),    'platform');
  buildDropdown('btn-recruitment', 'panel-recruitment', uniq(ALL_ROWS.map(r=>r.recruitment)), 'recruitment');
}
initDropdowns();

// ── View toggle ────────────────────────────────────────────────────────────
document.querySelectorAll('.view-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.view = btn.dataset.view;
    render();
  });
});

// ── Filter ─────────────────────────────────────────────────────────────────
function applyFilters() {
  return ALL_ROWS.filter(r => {
    if (state.priority.size    > 0 && !state.priority.has(r.priority))       return false;
    if (state.title.size       > 0 && !state.title.has(r.title))             return false;
    if (state.product.size     > 0 && !state.product.has(r.product))         return false;
    if (state.platform.size    > 0 && !state.platform.has(r.platform))       return false;
    if (state.recruitment.size > 0 && !state.recruitment.has(r.recruitment)) return false;
    return true;
  });
}

// ── Cell renderers ─────────────────────────────────────────────────────────
function renderPriorityPill(p) {
  if (!p) return '';
  const cls = p === 'Required' ? 'pill-required' : p === 'Bolstering' ? 'pill-bolstering' : 'pill-gray';
  return `<span class="pill ${cls}">${esc(p)}</span>`;
}
function renderDate(d) {
  if (!d) return `<span class="cell-date no-date">—</span>`;
  const lc = d.toLowerCase();
  if (lc.includes('tbd') || lc.includes('n/a') || lc.includes('still under'))
    return `<span class="cell-date date-tbd">${esc(d)}</span>`;
  return `<span class="cell-date has-date">${esc(d)}</span>`;
}
function renderRecruitment(r) {
  if (!r) return `<span class="recruit-no">—</span>`;
  const lc = r.toLowerCase();
  if (lc === 'yes')                        return `<span class="recruit-yes">Yes ✓</span>`;
  if (lc === 'no')                         return `<span class="recruit-no">No</span>`;
  if (lc === 'maybe' || lc === 'maybe?')  return `<span class="recruit-maybe">Maybe?</span>`;
  if (lc === 'tbd')                        return `<span class="recruit-tbd">TBD</span>`;
  return `<span class="recruit-maybe">${esc(r)}</span>`;
}
function renderNotes(notes, rowIdx) {
  if (!notes) return '';
  const MAX = 120;
  if (notes.length <= MAX) return `<div class="cell-notes">${esc(notes)}</div>`;
  const short = notes.substring(0, MAX).trim();
  return `<div class="cell-notes" id="notes-${rowIdx}">
    <span class="short-text">${esc(short)}… <span class="expand-btn" onclick="toggleNotes(${rowIdx})">more</span></span>
    <span class="full-text">${esc(notes)} <span class="expand-btn" onclick="toggleNotes(${rowIdx})">less</span></span>
  </div>`;
}
function toggleNotes(idx) {
  const el = document.getElementById(`notes-${idx}`);
  if (el) el.classList.toggle('expanded');
}

// ── Flat table renderer ────────────────────────────────────────────────────
const EDITABLE_COLS = new Set(['product','grade_band','item_count','cal_start','cal_end','recruitment','platform','notes']);

function renderTable(rows) {
  const cols = ALL_COLS;
  const thead = `<thead><tr>${cols.map(c =>
    `<th>${esc(COL_LABELS[c] || c)}</th>`
  ).join('')}</tr></thead>`;

  const tbody = rows.map((r, idx) => {
    const globalIdx = ALL_ROWS.indexOf(r);
    const rowKey    = ROW_KEYS[globalIdx] || '';
    const prCls = r.priority === 'Required' ? 'priority-required'
                : r.priority === 'Bolstering' ? 'priority-bolstering' : '';
    const cells = cols.map(c => {
      const editable = EDITABLE && EDITABLE_COLS.has(c);
      let val = '';
      switch (c) {
        case 'priority':    val = renderPriorityPill(r.priority); break;
        case 'title':       val = `<div class="cell-title">${esc(r.title)}</div>`; break;
        case 'product':
          val = editable
            ? makeEditableCell(r.product || '', rowKey, 'product')
            : (r.product ? `<span class="pill pill-gray">${esc(r.product)}</span>` : '');
          break;
        case 'grade_band':
          val = editable ? makeEditableCell(r.grade_band || '', rowKey, 'grade_band') : esc(r.grade_band);
          break;
        case 'item_count':
          val = editable ? makeEditableCell(r.item_count || '', rowKey, 'item_count') : esc(r.item_count);
          break;
        case 'cal_start':
          val = editable ? makeEditableCell(r.cal_start || '', rowKey, 'cal_start') : renderDate(r.cal_start);
          break;
        case 'cal_end':
          val = editable ? makeEditableCell(r.cal_end || '', rowKey, 'cal_end') : renderDate(r.cal_end);
          break;
        case 'recruitment':
          val = editable ? makeEditableCell(r.recruitment || '', rowKey, 'recruitment') : renderRecruitment(r.recruitment);
          break;
        case 'platform':
          val = editable ? makeEditableCell(r.platform || '', rowKey, 'platform')
            : (r.platform ? `<span class="pill pill-teal">${esc(r.platform)}</span>` : '<span style="color:#d1d5db">—</span>');
          break;
        case 'notes':
          val = editable ? makeEditableCell(r.notes || '', rowKey, 'notes') : renderNotes(r.notes, idx);
          break;
        default: val = esc(r[c] || '');
      }
      return `<td>${val}</td>`;
    }).join('');
    return `<tr class="${prCls}">${cells}</tr>`;
  }).join('');

  return `<div style="background:#fff;border-radius:8px;border:1px solid #e2e5ea;overflow:auto">
    <table class="data-table">${thead}<tbody>${tbody}</tbody></table>
  </div>`;
}

// ── Timeline renderer ──────────────────────────────────────────────────────
const MONTH_PX = 144;
const TODAY    = new Date();
const TODAY_KEY = monthKey(TODAY.getFullYear(), TODAY.getMonth() + 1);

// 3 weeks ≈ 0.75 months in pixel terms
const PSYCHO_MONTHS = 0.75;

// Product color palette — assigned on first encounter, stable across renders
const PRODUCT_COLORS = [
  '#2563eb','#059669','#dc2626','#d97706','#7c3aed',
  '#0891b2','#be185d','#65a30d','#ea580c','#6366f1',
];
const _productColorMap = {};
let   _productColorIdx = 0;
function productColor(product) {
  const key = (product || '').trim().toLowerCase();
  if (!_productColorMap[key]) {
    _productColorMap[key] = PRODUCT_COLORS[_productColorIdx % PRODUCT_COLORS.length];
    _productColorIdx++;
  }
  return _productColorMap[key];
}

function buildDataRow(r, start, end, minKey, totalMonths, chartWidth, todayX, gridHtml) {
  const label  = r.product || r.title;
  const grade  = r.grade_band || '';
  const color  = productColor(r.product);
  let barHtml  = '';

  if (!start && !end) {
    barHtml = `<div class="tl-tbd-label">Dates TBD</div>`;
  } else {
    const sk = start ? monthKey(start.year, start.month) : null;
    const ek = end   ? monthKey(end.year,   end.month)   : null;
    let leftPx, rightPx;
    if (sk !== null && ek !== null) {
      leftPx  = ((sk - minKey) / totalMonths) * chartWidth;
      rightPx = (((ek - minKey) + 1) / totalMonths) * chartWidth;
    } else if (sk !== null) {
      leftPx  = ((sk - minKey) / totalMonths) * chartWidth;
      rightPx = (((sk - minKey) + 4) / totalMonths) * chartWidth;
    } else {
      leftPx  = ((ek - minKey) / totalMonths) * chartWidth;
      rightPx = (((ek - minKey) + 1) / totalMonths) * chartWidth;
    }
    const widthPx   = Math.max(rightPx - leftPx, 8);
    const rowIdx    = ALL_ROWS.indexOf(r);
    const hasTbdEnd = !end && start;
    const barParts  = [r.product, r.grade_band, r.item_count].filter(Boolean);
    const labelText = widthPx > 48 ? esc(barParts.join(' · ')) : '';
    barHtml += `<div class="tl-bar"
      style="left:${leftPx.toFixed(1)}px;width:${widthPx.toFixed(1)}px;background:${color}"
      onmouseenter="showTipRow(event,${rowIdx})"
      onmouseleave="hideTip()">${labelText}${hasTbdEnd ? '<span class="tl-tbd-end-label">tbd end</span>' : ''}</div>`;
    const analysisWidth = PSYCHO_MONTHS * MONTH_PX;
    if (ek !== null || hasTbdEnd) {
      const anaLabel = analysisWidth > 36 ? 'Cal Analysis' : '';
      barHtml += `<div class="tl-bar psycho-bar"
        style="left:${rightPx.toFixed(1)}px;width:${analysisWidth.toFixed(1)}px"
        onmouseenter="showTipPsycho(event)"
        onmouseleave="hideTip()">${esc(anaLabel)}</div>`;
    }
  }

  return `<tr class="tl-data-row">
    <td class="tl-label-cell">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:4px">
        <div>
          <div class="tl-label-product">${esc(label)}</div>
          ${grade ? `<div class="tl-label-grade">${esc(grade)}</div>` : ''}
        </div>
        <div style="flex-shrink:0;margin-top:1px">${renderPriorityPill(r.priority)}</div>
      </div>
    </td>
    <td class="tl-bar-td">
      <div class="tl-bar-row" style="width:${chartWidth}px">
        ${gridHtml}${barHtml}
      </div>
    </td>
  </tr>`;
}

function renderTimeline(rows) {
  const parsedRows = rows.map(r => ({
    r,
    start: parseTimelineDate(r.cal_start, false),
    end:   parseTimelineDate(r.cal_end,   true),
  })).sort((a, b) => {
    const aHas = a.start || a.end ? 0 : 1;
    const bHas = b.start || b.end ? 0 : 1;
    if (aHas !== bHas) return aHas - bHas;
    const aKey = a.start ? monthKey(a.start.year, a.start.month) : (a.end ? monthKey(a.end.year, a.end.month) : 0);
    const bKey = b.start ? monthKey(b.start.year, b.start.month) : (b.end ? monthKey(b.end.year, b.end.month) : 0);
    return aKey - bKey;
  });

  const datedRows = parsedRows.filter(p => p.start || p.end);
  const tbdRows   = parsedRows.filter(p => !p.start && !p.end);

  // Determine date range from dated rows only
  let minKey = monthKey(TODAY.getFullYear(), TODAY.getMonth()) - 1;
  let maxKey = monthKey(TODAY.getFullYear() + 1, TODAY.getMonth() + 1);
  datedRows.forEach(({start, end}) => {
    if (start) minKey = Math.min(minKey, monthKey(start.year, start.month) - 1);
    if (end)   maxKey = Math.max(maxKey, monthKey(end.year, end.month) + 2);
  });
  if (maxKey - minKey < 12) maxKey = minKey + 12;

  const totalMonths = maxKey - minKey + 1;
  const chartWidth  = totalMonths * MONTH_PX;

  const todayFrac = (TODAY_KEY - minKey + TODAY.getDate() / 31) / totalMonths;
  const todayX    = todayFrac * chartWidth;

  // Month header
  let monthHeaderHtml = '<div class="tl-month-cells">';
  for (let k = minKey; k <= maxKey; k++) {
    const {year, month} = keyToYM(k);
    const isToday = k === TODAY_KEY;
    const label   = month === 1 || k === minKey
      ? `${MONTH_NAMES[month]} ${year}` : MONTH_NAMES[month];
    monthHeaderHtml += `<div class="tl-month-cell${isToday ? ' is-today-month' : ''}" style="width:${MONTH_PX}px">${label}</div>`;
  }
  monthHeaderHtml += '</div>';

  // Shared grid HTML (reused per row)
  let gridHtml = '';
  for (let k = minKey; k <= maxKey; k++) {
    const x = (k - minKey) * MONTH_PX;
    gridHtml += `<div class="tl-grid-line" style="left:${x}px"></div>`;
  }
  gridHtml += `<div class="tl-today-line" style="left:${todayX.toFixed(1)}px"></div>`;

  // Dated rows tbody
  const datedTbody = datedRows.map(({r, start, end}) =>
    buildDataRow(r, start, end, minKey, totalMonths, chartWidth, todayX, gridHtml)
  ).join('');

  // TBD rows tbody (no chart area — just label column + grey text)
  const tbdTbody = tbdRows.map(({r}) => {
    const label = r.product || r.title;
    const grade = r.grade_band || '';
    return `<tr class="tl-data-row">
      <td class="tl-label-cell" style="background:#f9fafb">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:4px">
          <div>
            <div class="tl-label-product" style="color:#6b7280">${esc(label)}</div>
            ${grade ? `<div class="tl-label-grade">${esc(grade)}</div>` : ''}
          </div>
          <div style="flex-shrink:0;margin-top:1px">${renderPriorityPill(r.priority)}</div>
        </div>
      </td>
      <td class="tl-bar-td" style="background:#f9fafb">
        <div class="tl-bar-row" style="width:${chartWidth}px">
          <div class="tl-tbd-label">—</div>
        </div>
      </td>
    </tr>`;
  }).join('');

  // Build product legend from rows that have dates
  const seenProducts = [];
  datedRows.forEach(({r}) => {
    const p = (r.product || '').trim();
    if (p && !seenProducts.includes(p)) seenProducts.push(p);
  });
  const productLegend = seenProducts.map(p =>
    `<div class="tl-legend-item"><div class="tl-legend-swatch" style="background:${productColor(p)};border-radius:2px"></div> ${esc(p)}</div>`
  ).join('');

  return `<div id="timeline-wrap">
    <div class="tl-scroll-container">
      <table class="tl-table">
        <thead>
          <tr>
            <th class="tl-header-label">Item</th>
            <th class="tl-header-chart"><div style="width:${chartWidth}px">${monthHeaderHtml}</div></th>
          </tr>
        </thead>
        <tbody>${datedTbody}</tbody>
      </table>
    </div>
    ${tbdTbody ? `<div style="border-top:3px solid #e2e5ea;background:#f9fafb">
      <div style="padding:6px 12px 5px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#9ca3af;border-bottom:1px solid #e2e5ea;background:#f1f3f6">
        Dates TBD
      </div>
      <table class="tl-table" style="background:#f9fafb">
        <tbody>${tbdTbody}</tbody>
      </table>
    </div>` : ''}
    <div class="tl-legend">
      ${productLegend}
      <div class="tl-legend-item" style="margin-left:8px"><div class="tl-legend-swatch" style="background:#7c3aed;border-radius:2px"></div> Cal Analysis</div>
      <div class="tl-legend-item"><div style="width:2px;height:12px;background:#ef4444;display:inline-block;border-radius:1px"></div> Today</div>
      <div style="margin-left:auto;font-size:10px;color:#9ca3af">Seasons mapped to calendar dates &nbsp;|&nbsp; "tbd end" = ~4 mo estimate</div>
    </div>
  </div>`;
}

// ── Tooltip helpers ────────────────────────────────────────────────────────
function buildTooltipHtml(r) {
  const lines = [
    `<strong>${esc(r.product || r.title)} — ${esc(r.grade_band)}</strong>`,
    `<div class="tt-row"><span class="tt-label">Table</span><span>${esc(r.title)}</span></div>`,
    `<div class="tt-row"><span class="tt-label">Priority</span><span>${esc(r.priority)}</span></div>`,
    `<div class="tt-row"><span class="tt-label">Item Count</span><span>${esc(r.item_count||'—')}</span></div>`,
    `<div class="tt-row"><span class="tt-label">FT Start</span><span>${esc(r.cal_start||'—')}</span></div>`,
    `<div class="tt-row"><span class="tt-label">FT End</span><span>${esc(r.cal_end||'—')}</span></div>`,
    `<div class="tt-row"><span class="tt-label">Platform</span><span>${esc(r.platform||'—')}</span></div>`,
    `<div class="tt-row"><span class="tt-label">Recruitment</span><span>${esc(r.recruitment||'—')}</span></div>`,
    r.notes ? `<div class="tt-row" style="margin-top:4px"><span class="tt-label">Notes</span><span style="max-width:240px;white-space:normal">${esc(r.notes.substring(0,160))}${r.notes.length>160?'…':''}</span></div>` : '',
  ].filter(Boolean).join('');
  return lines;
}

const tip = document.getElementById('tooltip');
function showTipRow(e, idx) {
  const r = ALL_ROWS[idx];
  if (!r) return;
  tip.innerHTML = buildTooltipHtml(r);
  tip.style.display = 'block';
  moveTip(e);
}
function showTipPsycho(e) {
  tip.innerHTML = 'Estimated 3 weeks for psychometric calibration analysis, if started after field test ends.';
  tip.style.display = 'block';
  moveTip(e);
}
function moveTip(e) {
  const x = Math.min(e.clientX + 14, window.innerWidth  - tip.offsetWidth  - 10);
  const y = Math.min(e.clientY + 14, window.innerHeight - tip.offsetHeight - 10);
  tip.style.left = x + 'px';
  tip.style.top  = y + 'px';
}
function hideTip() { tip.style.display = 'none'; }
document.addEventListener('mousemove', e => { if (tip.style.display !== 'none') moveTip(e); });

// ── Re-sync (triggers GitHub Actions workflow) ────────────────────────────
function triggerResync() {
  const btn    = document.getElementById('resync-btn');
  const banner = document.getElementById('resync-banner');
  btn.disabled = true;
  btn.textContent = '↺ Triggering…';
  banner.style.display = 'block';
  banner.style.background = '#fef9ec';
  banner.style.color = '#92400e';
  banner.textContent = 'Sending re-sync request to GitHub…';

  fetch(`https://api.github.com/repos/${GH_REPO}/actions/workflows/${GH_WORKFLOW}/dispatches`, {
    method: 'POST',
    headers: {
      'Authorization': `token ${GH_TOKEN}`,
      'Accept': 'application/vnd.github+json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ ref: GH_BRANCH }),
  })
  .then(r => {
    if (r.status === 204) {
      banner.style.background = '#d1fae5';
      banner.style.color = '#065f46';
      banner.innerHTML = '✓ Re-sync triggered. The report usually updates within 60–90 seconds. <strong>Refresh this page</strong> after that to see the latest data.';
      btn.textContent = '↺ Re-sync';
      // Re-enable after 90s
      setTimeout(() => { btn.disabled = false; }, 90000);
    } else {
      return r.text().then(t => { throw new Error(`${r.status}: ${t}`); });
    }
  })
  .catch(err => {
    banner.style.background = '#fee2e2';
    banner.style.color = '#991b1b';
    banner.textContent = 'Re-sync failed: ' + err.message;
    btn.disabled = false;
    btn.textContent = '↺ Re-sync';
  });
}

// ── Editable cell save ────────────────────────────────────────────────────
function saveCell(el, rowKey, fieldKey) {
  const newValue = el.innerText.trim();
  const ind = document.createElement('span');
  ind.className = 'save-indicator saving';
  ind.textContent = 'saving…';
  el.parentNode.appendChild(ind);

  fetch(`http://127.0.0.1:${API_PORT}/save`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ row_key: rowKey, field_key: fieldKey, value: newValue }),
  })
  .then(r => r.json())
  .then(data => {
    ind.className = 'save-indicator ' + (data.ok ? 'saved' : 'error');
    ind.textContent = data.ok ? 'saved ✓' : 'error: ' + (data.error || '?');
    if (data.ok) setTimeout(() => ind.remove(), 2500);
  })
  .catch(err => {
    ind.className = 'save-indicator error';
    ind.textContent = 'network error';
  });
}

function makeEditableCell(text, rowKey, fieldKey) {
  const safeText = esc(text);
  return `<span class="editable-cell"
    contenteditable="true"
    data-row-key="${esc(rowKey)}"
    data-field="${esc(fieldKey)}"
    onblur="saveCell(this, this.dataset.rowKey, this.dataset.field)"
    onkeydown="if(event.key==='Enter'){event.preventDefault();this.blur()}"
  >${safeText}</span>`;
}

// ── Main render ────────────────────────────────────────────────────────────
function render() {
  const rows = applyFilters();
  document.getElementById('result-count').textContent = `${rows.length} row${rows.length !== 1 ? 's' : ''}`;

  const content = document.getElementById('main-content');
  if (!rows.length) {
    content.innerHTML = '<div class="empty-state">No rows match the current filters.</div>';
    return;
  }
  content.innerHTML = state.view === 'timeline' ? renderTimeline(rows) : renderTable(rows);
}

// ── Init ───────────────────────────────────────────────────────────────────
document.getElementById('generated-badge').textContent = `Generated ${GENERATED}`;
document.getElementById('row-badge').textContent       = `${ALL_ROWS.length} rows across ${new Set(ALL_ROWS.map(r=>r.title)).size} tables`;
if (EDITABLE) {
  const banner = document.getElementById('edit-banner');
  if (banner) banner.classList.add('visible');
}
render();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import sys
    serve = '--serve' in sys.argv

    html_body, page_title = fetch_page_html()
    rows = parse_tables(html_body)

    today = date.today().isoformat()
    # Strip internal keys before embedding in HTML
    clean_rows = [{k: v for k, v in r.items() if not k.startswith('_')} for r in rows]
    json_data  = json.dumps(clean_rows, ensure_ascii=False, separators=(',', ':'))

    # Full rows (with _row_key/_headers) for server use
    full_json  = json.dumps(rows, ensure_ascii=False, separators=(',', ':'))

    editable_flag = 'true' if serve else 'false'

    row_keys_json = json.dumps([r.get('_row_key','') for r in rows],
                               ensure_ascii=False, separators=(',', ':'))

    html = HTML_TEMPLATE
    html = html.replace('__DATA__',         json_data)
    html = html.replace('__ROW_KEYS__',     row_keys_json)
    html = html.replace('__GENERATED__',    today)
    html = html.replace('__EDITABLE__',     editable_flag)
    html = html.replace('__PORT__',         str(SERVER_PORT))
    html = html.replace('__GITHUB_TOKEN__', GITHUB_TOKEN)
    html = html.replace('__GITHUB_REPO__',  GITHUB_REPO)

    if serve:
        serve_mode(html)
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = unique_output_path(OUTPUT_DIR, f'calibration-plan-{today}.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f'Saved: {out_path}')

    try:
        gh_url = push_to_github(out_path)
        print(f'Pushed to GitHub: {gh_url}')
    except Exception as e:
        print(f'GitHub push failed: {e}')

    return out_path

if __name__ == '__main__':
    main()
