import sys; sys.path.insert(0, '.')
import config; config.apply_env()
import paper_judge_bot as pjb
import market_universe, live_data
from collections import Counter
import time
class FakeRT: pass
rt = FakeRT()
rt.ctx = type('o',(),{'now_utc': time.time()})()
rt.positions = {}
rt.cycle_data = live_data.prefetch(list(config.STATIONS))
cands = market_universe.list_candidates()
# Run all candidates through prescreen, group by full rejection reason
rej_counts = Counter()
no_fc = []
for c in cands:
    p = pjb.build_entry_packet(rt, c)
    if p is None:
        rej_counts['__no_packet'] += 1
        continue
    rej = pjb.prescreen(p)
    if rej:
        first3 = ' '.join(rej.split()[:3])
        rej_counts[first3] += 1
        if 'no recent forecast' in rej:
            no_fc.append(c)
    else:
        rej_counts['__SURVIVED'] += 1
for k,v in rej_counts.most_common():
    print(f'  {v:>3} {k}')
print()
print('first 5 no-forecast candidates:')
for c in no_fc[:5]:
    print(f'  {c.ticker}  station={c.station}  climate_day={c.climate_day}  series={c.series_prefix}  floor={c.floor} cap={c.cap}')
    key = (c.station, c.climate_day, 'low' if c.series_prefix == 'KXLOW' else 'high')
    fk = rt.cycle_data['forecasts_by_station_day_kind'].get(key)
    print(f'    cycle_data key {key}: present={fk is not None}, sources={list((fk.get("sources") or {}).keys()) if fk else None}')
