import json, os
from asf.workers import lifecycle
KEEP = ('job', 'kind', 'model', 'item', 'started', 'ended', 'end_reason', 'harvested', 'pid')
CUT = '2026-09-23T02:47:45Z'
src = os.path.expanduser('~/.ASF/state/asf/sessions.jsonl')
rows = [r for rs in lifecycle.runs(src).values() for r in rs
        if r.get('ended') and r['ended'] <= CUT]
rows.sort(key=lambda r: (r['started'], r['job']))
with open('tests/fixtures/improve/asf-2026-09-23.jsonl', 'w', encoding='utf-8') as f:
    for r in rows:
        f.write(json.dumps({k: r[k] for k in KEEP if k in r}, sort_keys=True) + '\n')
print(len(rows), 'runs')          # 216
