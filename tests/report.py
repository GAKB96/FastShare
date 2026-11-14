"""Print self-test results in the GitHub log, add annotations, fail the step if anything failed."""
import json, sys
path = sys.argv[1]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception as e:
    print(f"::error::Self-test produced no results ({path}): {e}")
    sys.exit(1)
res = data["results"]
bad = [r for r in res if not r["ok"]]
for r in res:
    print(("PASS  " if r["ok"] else "FAIL  ") + r["name"] + (f"  -- {r['detail']}" if r["detail"] else ""))
label = sys.argv[2] if len(sys.argv) > 2 else path
for r in bad:
    d = r["detail"].replace("\n", " | ")[:300]
    print(f"::error::[{label}] {r['name']}: {d}")
print(f"::notice::[{label}] {len(res) - len(bad)}/{len(res)} checks passed in {data['seconds']}s ({data['theme']} theme)")
sys.exit(1 if bad or not res else 0)
