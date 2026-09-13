"""Capture a reproducible historical dataset. No credentials required."""
import gzip, json, time, urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1]
endpoints=['sessions','drivers','laps','pit','stints','race_control']
for endpoint in endpoints:
    path=root/'evidence'/f'{endpoint}-9558.json.gz'
    if path.exists():continue
    url=f'https://api.openf1.org/v1/{endpoint}?session_key=9558'
    for attempt in range(4):
        try:
            data=json.loads(urllib.request.urlopen(url,timeout=45).read())
            path.write_bytes(gzip.compress(json.dumps({'url':url,'fetched_at':time.time(),'data':data},separators=(',',':')).encode(),mtime=0))
            print(endpoint,len(data));break
        except Exception:
            if attempt==3:raise
            time.sleep(5*(attempt+1))
    time.sleep(2.2)
from analysis import analyze,chart
data={endpoint:json.loads(gzip.decompress((root/'evidence'/f'{endpoint}-9558.json.gz').read_bytes()))['data'] for endpoint in endpoints}
result=analyze(data,[44,4],40,50)
(root/'evidence'/'example.json').write_text(json.dumps(result,indent=2))
(root/'static'/'example.png').write_bytes(chart(result,'British GP 2024 · Silverstone'))
print(json.dumps(result['facts'],indent=2))
