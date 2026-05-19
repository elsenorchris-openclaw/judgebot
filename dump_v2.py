import config; config.apply_env()
import json
import live_data
import logging
logging.basicConfig(level=logging.WARNING)
print(f'Prefetching all stations (may take 60-90s for first run as gridpoints resolve)...')
data = live_data.prefetch(list(config.STATIONS))
print(f'Done — {len(data["by_station"])} stations')
print()
# Check KDCA in full detail as a representative example
print('=== KDCA full station block ===')
sd = data['by_station']['KDCA']
print(f"  NWS obs: {sd.get('nws_obs')}")
print(f"  wethr obs (key fields): temp_f={sd.get('wethr_obs',{}).get('temp_f')} dewpt={sd.get('wethr_obs',{}).get('dew_point_f')} clc={sd.get('wethr_obs',{}).get('cloud_layer_count')} wind={sd.get('wethr_obs',{}).get('wind_speed_mph')}")
print(f"  climate normals: {sd.get('climate')}")
print(f"  clock local: {sd.get('clock',{}).get('local_iso')} h_to_peak={sd.get('clock',{}).get('h_to_peak')}")
print(f"  running_min/max today: {sd.get('running_min_today')}/{sd.get('running_max_today')}")
print(f"  wfo: {sd.get('wfo')}")
hfc = sd.get('hourly_forecast_24h') or []
print(f"  hourly forecast: {len(hfc)} hours")
if hfc:
    print(f"    [+0h] {hfc[0]}")
    print(f"    [+6h] {hfc[6] if len(hfc) > 6 else "-"}")
    print(f"    [+12h] {hfc[12] if len(hfc) > 12 else "-"}")
afd = sd.get('afd')
if afd:
    print(f"  AFD issued: {afd.get('issued_iso')} (office {afd.get('office')})")
    syn = afd.get('synopsis') or ''
    print(f"    synopsis: {syn[:300]}")
print(f"  model_mae_high: {sd.get('model_mae_high')}")
print(f"  model_mae_low: {sd.get('model_mae_low')}")
print(f"  persistence_high: {sd.get('persistence_high')}")
print(f"  persistence_low: {sd.get('persistence_low')}")
print()
# Coverage across all 20 stations
print('=== Coverage check across 20 stations ===')
print(f'{"station":<6} {"nws":<4} {"wethr":<5} {"clock":<5} {"climate":<7} {"hourly_fc":<9} {"afd":<3} {"mae_h":<5} {"persist":<7}')
for st, sd in data['by_station'].items():
    nws = '✓' if sd.get('nws_obs') else '-'
    wethr = '✓' if sd.get('wethr_obs') else '-'
    clk = '✓' if sd.get('clock') else '-'
    cli = '✓' if sd.get('climate') else '-'
    hfc_n = len(sd.get('hourly_forecast_24h') or [])
    afd_ok = '✓' if sd.get('afd') else '-'
    mae_ok = '✓' if sd.get('model_mae_high') else '-'
    p_ok = '✓' if sd.get('persistence_high') else '-'
    print(f'{st:<6} {nws:<4} {wethr:<5} {clk:<5} {cli:<7} {hfc_n:<9} {afd_ok:<3} {mae_ok:<5} {p_ok:<7}')
