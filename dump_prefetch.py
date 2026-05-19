import config; config.apply_env()
import json, os
import live_data
import logging
logging.basicConfig(level=logging.WARNING)
stations = list(config.STATIONS)
print(f'Prefetching {len(stations)} stations...')
data = live_data.prefetch(stations)
# Save full dump with stringified tuple keys
os.makedirs('/tmp/prefetch_dump', exist_ok=True)
fk_serializable = {f'{s}|{d}|{k}': v for (s,d,k), v in data['forecasts_by_station_day_kind'].items()}
serial = {'by_station': data['by_station'], 'forecasts': fk_serializable, 'fetched_ts': data['fetched_ts']}
with open('/tmp/prefetch_dump/full.json','w') as f:
    json.dump(serial, f, indent=2, default=str)
print(f'Full dump saved: /tmp/prefetch_dump/full.json ({os.path.getsize("/tmp/prefetch_dump/full.json")} bytes)')
print()
print('=== Per-station summary ===')
hdr = f'{"station":<6} {"NWS_t":>6} {"NWS_dp":>6} {"wethr_t":>7} {"wethr_dp":>7} {"clc":>3} {"wind":>5} {"local_hr":>8} {"peak_hr":>7} {"h_to_peak":>9} {"class":<12}'
print(hdr)
for st in stations:
    sd = data['by_station'].get(st, {})
    nws = sd.get('nws_obs') or {}
    wet = sd.get('wethr_obs') or {}
    clk = sd.get('clock') or {}
    nws_t = nws.get('temp_f'); nws_dp = nws.get('dewpt_f')
    wt = wet.get('temp_f'); wdp = wet.get('dew_point_f')
    clc = wet.get('cloud_layer_count')
    wind = wet.get('wind_speed_mph')
    print(f'{st:<6} {(nws_t if nws_t is not None else 0):>6.1f} {(nws_dp if nws_dp is not None else 0):>6.1f} {(wt if wt is not None else 0):>7.1f} {(wdp if wdp is not None else 0):>7.1f} {str(clc if clc is not None else "-"):>3} {(wind if wind is not None else 0):>5.1f} {(clk.get("local_hour") or 0):>8.2f} {(clk.get("peak_hour_local") or 0):>7.2f} {(clk.get("h_to_peak") or 0):>+9.2f} {(clk.get("climate_class") or ""):<12}')
print()
print('=== Climate + sun ===')
for st in stations:
    sd = data['by_station'].get(st, {})
    cl = sd.get('climate') or {}
    clk = sd.get('clock') or {}
    rmin = sd.get('running_min_today')
    rmax = sd.get('running_max_today')
    print(f'  {st}: peak_norm={cl.get("peak_f")} low_norm={cl.get("low_f")} sunrise={clk.get("sunrise_local_h")} solar_noon={clk.get("solar_noon_local_h")} sunset={clk.get("sunset_local_h")} lag={clk.get("peak_lag_h")}h  rmin_today={rmin}  rmax_today={rmax}')
print()
print('=== Forecast keys ===')
print(f'Total: {len(fk_serializable)}')
