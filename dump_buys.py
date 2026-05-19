import json
print('=== BUYs ===\n')
for line in open('/home/ubuntu/paper_judge_bot/data/decisions.jsonl'):
    r = json.loads(line)
    if r.get('kind') != 'entry_decision': continue
    if r.get('decision') not in ('BUY_NO','BUY_YES'): continue
    ps = r.get('packet_summary') or {}
    print(f"### {r.get('ticker')} — {r.get('decision')}  conv={r.get('conviction')}  size={r.get('size_factor')}")
    print(f"   yes_ask={ps.get('yes_ask_c')}c  no_ask={ps.get('no_ask_c')}c  spread={ps.get('spread_c')}c")
    print(f"   mu_nbm={ps.get('mu_nbm')} mu_hrrr={ps.get('mu_hrrr')} disag={ps.get('disag_f')} obs={ps.get('obs')} rm={ps.get('running')}")
    print(f"   READ: {r.get('read')}")
    print(f"   RISKS: {r.get('key_risks')}")
    print(f"   CHANGE_MIND: {r.get('what_would_change_my_mind')}")
    print()
print('=== PARSE FAILURES ===\n')
for line in open('/home/ubuntu/paper_judge_bot/data/decisions.jsonl'):
    r = json.loads(line)
    if r.get('kind') != 'entry_decision': continue
    if r.get('parse_ok'): continue
    print(f"### {r.get('ticker')}")
    print(f"   parse_error: {r.get('parse_error')}")
    print(f"   elapsed_sec: {r.get('elapsed_sec')}")
    print(f"   read: {r.get('read')}")
    print()
