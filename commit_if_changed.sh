#!/bin/bash
# Commit data/ only when the bundle content (not its timestamp) changed since HEAD.
set -euo pipefail
CHANGED=$(python - <<'PY'
import json, subprocess, hashlib
def h(o): return hashlib.sha256(json.dumps(o, sort_keys=True).encode()).hexdigest()
new = json.load(open('data/series.json'))
try: old = json.loads(subprocess.check_output(['git', 'show', 'HEAD:data/series.json'], text=True))
except Exception: old = {}
key = lambda d: {'series': d.get('series'), 'seasonal': d.get('seasonal'), 'equities': d.get('equities')}
print('yes' if h(key(new)) != h(key(old)) else 'no')
PY
)
if [ "$CHANGED" != "yes" ]; then echo "no new data"; git checkout -- data; exit 0; fi
LATEST=$(python -c "import json; m=json.load(open('data/manifest.json')); print(max(x['last_obs'] for x in m['series'] if x.get('last_obs') and x.get('source')=='EIA'))")
PROBLEMS=$(python -c "import json; m=json.load(open('data/manifest.json')); print(', '.join(x['key']+':'+x['status'] for x in m['series'] if x.get('status') in ('failed','missing','stale')) or 'none')")
git config user.name "energy-dashboard-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add data/series.json data/manifest.json data/bundle.json
git commit -m "Data through $LATEST" -m "Sources failed, missing or stale: $PROBLEMS"
git push
echo "committed: data through $LATEST; problems: $PROBLEMS"
