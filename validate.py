#!/usr/bin/env python3
"""Sanity-check parsed NCLT cause lists: item count vs NCLT's own entry count,
items with no content, and gaps in each section's numbering (101, 102, 104…)."""
import json, sys, glob, re
flag = 0
for f in sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1 else "data/2*.json")):
    d = json.load(open(f))
    lists = {l["name"]: l for l in d["lists"]}
    for b in d["benches"]:
        l = lists[b["list"]]
        probs = []
        n = l["items"]
        if l.get("unparsed"):
            probs.append("UNPARSED (no text / scanned?)")
        elif l.get("listed") and n < l["listed"]:
            probs.append(f"items {n} < listed {l['listed']}")
        for s in b["sections"]:
            nums = []
            for it in s["items"]:
                if not (it["c"] or it["p"] or it.get("ias")):
                    probs.append(f"empty item {it['n']}")
                m = re.fullmatch(r"([A-Za-z]*)(\d+)", it["n"])
                if m:
                    nums.append((m.group(1), int(m.group(2))))
            for (p1, a), (p2, c) in zip(nums, nums[1:]):
                if p1 == p2 and c != a + 1 and c != a and c % 100 != 1:
                    probs.append(f"gap {p1}{a}->{p2}{c} in '{s['t'][:20]}'")
        if probs:
            flag += 1
            print(f"{d['date']} {b['list']}: " + "; ".join(probs[:4]))
print("lists flagged:", flag)
