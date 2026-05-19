import sys; sys.path.insert(0, '.')
import config; config.apply_env()
import paper_judge_bot as pjb
import market_universe
import live_data
# Simulate a runtime
class FakeRT:
    pass
rt = FakeRT()
rt.ctx = type('o',(),{'now_utc': __import__('time').time()})()
rt.positions = {}
rt.cycle_data = live_data.prefetch(list(config.STATIONS))
print(f"prefetch: {len(rt.cycle_data['forecasts_by_station_day_kind'])} forecast keys")
# Try one candidate
cands = market_universe.list_candidates()
print(f"candidates: {len(cands)}")
# Find a KATL HIGH today candidate
for c in cands:
    if c.station == 'KATL' and c.series_prefix == 'KXHIGH':
        print(f"sample: {c.ticker}  station={c.station}  climate_day={c.climate_day}  series={c.series_prefix}")
        p = pjb.build_entry_packet(rt, c)
        if p is None:
            print('  packet is None')
        else:
            print(f'  mu_nbm={p.get("mu_nbm")}  mu_hrrr={p.get("mu_hrrr")}  mu_ecmwf={p.get("mu_ecmwf")}  mu_nbp={p.get("mu_nbp")}')
            print(f'  live_obs temp={(p.get("live_obs") or {}).get("temp_f")}')
            rej = pjb.prescreen(p)
            print(f'  prescreen result: {rej}')
        break
