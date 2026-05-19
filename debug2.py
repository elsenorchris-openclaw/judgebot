import sys; sys.path.insert(0, '.')
import config; config.apply_env()
import market_universe
from collections import Counter
cands = market_universe.list_candidates()
print(f'total: {len(cands)}')
print('by series:')
for s,n in Counter(c.series_prefix for c in cands).items():
    print(f'  {s}: {n}')
print('by station:')
for s,n in Counter(c.station for c in cands).most_common():
    print(f'  {s}: {n}')
print('sample candidates:')
for c in cands[:5]:
    print(f'  ticker={c.ticker}  station={c.station}  series_prefix={c.series_prefix}  climate_day={c.climate_day}')
