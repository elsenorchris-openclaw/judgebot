import config; config.apply_env()
from climate_normals import local_clock_context
import time
now = time.time()
high_active = []
low_active = []
none_active = []
for st in config.STATIONS:
    clk = local_clock_context(st, now)
    lh = clk.get('local_hour') if clk else None
    if lh is None:
        continue
    if 5.0 <= lh < 18.0:
        high_active.append((st, lh))
    elif lh >= 19.0 or lh < 8.0:
        low_active.append((st, lh))
    else:
        none_active.append((st, lh))
print(f'HIGH d+0 active (5-18 local): {len(high_active)} stations')
for st, lh in high_active:
    print(f'  {st}: local {lh:.2f}')
print(f'LOW d+0 active (19-08 local): {len(low_active)} stations')
for st, lh in low_active:
    print(f'  {st}: local {lh:.2f}')
print(f'NEITHER (8-19 local without HIGH window applying, edge cases): {len(none_active)}')
for st, lh in none_active:
    print(f'  {st}: local {lh:.2f}')
