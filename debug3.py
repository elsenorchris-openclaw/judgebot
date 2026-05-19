import sys; sys.path.insert(0, '.')
import config; config.apply_env()
import paper_judge_bot as pjb
import market_universe, live_data
class FakeRT:
    pass
rt = FakeRT()
import time
rt.ctx = type('o',(),{'now_utc': time.time()})()
rt.positions = {}
rt.cycle_data = live_data.prefetch(list(config.STATIONS))
cands = market_universe.list_candidates()
# Try first HIGH candidate
for c in cands:
    if c.series_prefix == 'KXHIGH':
        print(f'TICKER {c.ticker}  station={c.station}  climate_day={c.climate_day}')
        # Check what cycle_data has for this station/day/kind
        key = (c.station, c.climate_day, 'high')
        fk = rt.cycle_data['forecasts_by_station_day_kind'].get(key)
        print(f'  cycle_data has key: {key}? {fk is not None}')
        if fk: print(f'  sources: {list((fk.get("sources") or {}).keys())}')
        p = pjb.build_entry_packet(rt, c)
        if p is None: print('  packet=None')
        else:
            print(f'  mu_nbm={p.get("mu_nbm")} mu_hrrr={p.get("mu_hrrr")} mu_ecmwf={p.get("mu_ecmwf")} mu_nbp={p.get("mu_nbp")}')
            print(f'  prescreen: {pjb.prescreen(p)}')
        break
