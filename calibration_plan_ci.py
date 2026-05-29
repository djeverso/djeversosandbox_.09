"""
CI entry point for GitHub Actions.
Reads credentials from environment variables, writes calibration-plan.html
to the current directory (repo root).
"""
import os, sys, json
from datetime import date

# Patch config before importing the main module
os.environ.setdefault('JIRA_API_TOKEN', os.environ.get('JIRA_API_TOKEN', ''))

# Import shared logic
sys.path.insert(0, os.path.dirname(__file__))
import calibration_plan as cp

# Override config to use environment variables
cp.JIRA_EMAIL  = os.environ.get('JIRA_EMAIL', cp.JIRA_EMAIL)
cp.JIRA_TOKEN  = os.environ.get('JIRA_API_TOKEN', cp.JIRA_TOKEN)
# Write to repo root (current working directory)
cp.OUTPUT_DIR  = os.getcwd()
# No GitHub push from CI — git commit step in workflow handles it
cp.GITHUB_TOKEN = ''

def main_ci():
    html_body, _ = cp.fetch_page_html()
    rows = cp.parse_tables(html_body)

    today = date.today().isoformat()
    clean_rows = [{k: v for k, v in r.items() if not k.startswith('_')} for r in rows]
    json_data  = json.dumps(clean_rows, ensure_ascii=False, separators=(',', ':'))
    row_keys_json = json.dumps([r.get('_row_key', '') for r in rows],
                               ensure_ascii=False, separators=(',', ':'))

    html = cp.HTML_TEMPLATE
    html = html.replace('__DATA__',      json_data)
    html = html.replace('__ROW_KEYS__',  row_keys_json)
    html = html.replace('__GENERATED__', today)
    html = html.replace('__EDITABLE__',  'false')
    html = html.replace('__PORT__',      '7432')

    out = os.path.join(os.getcwd(), 'calibration-plan.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'Written: {out}')

if __name__ == '__main__':
    main_ci()
