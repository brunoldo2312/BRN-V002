import json, time
data = json.load(open('trust_network.json'))
now = time.time()
keep, removed = {}, []
print("Analisando trust_network.json...")
for pub, n in data.get('nodes', {}).items():
    age = int(now - n.get('last_seen', 0))
    score = n.get('trust_score', 0)
    if age < 300:
        keep[pub] = n
        print(f"  [KEEP]    {pub[:24]}... score={score} age={age}s")
    else:
        removed.append(pub)
        print(f"  [REMOVE]  {pub[:24]}... score={score} age={age}s")
data['nodes'] = keep
json.dump(data, open('trust_network.json', 'w'), indent=2)
print()
print(f">>> {len(keep)} mantido(s), {len(removed)} removido(s)")
