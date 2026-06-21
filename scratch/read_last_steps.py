# -*- coding: utf-8 -*-
import json

path = r"C:\Users\Administrator\.gemini\antigravity\brain\1de2937b-3d9d-446d-87ed-b04f2d021bc6\.system_generated\logs\transcript_full.jsonl"

with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

print(f"Total lines: {len(lines)}")
# Find step 9954 or just print steps 9900 to 9955
for line in lines:
    data = json.loads(line)
    idx = data.get('step_index')
    if 9900 <= idx < 9955:
        print(f"Step {idx}, Source: {data.get('source')}, Type: {data.get('type')}")
        if data.get('type') == 'PLANNER_RESPONSE':
            content = data.get('content', '')
            print(f"  Content: {content}")
        elif data.get('type') == 'USER_INPUT':
            print(f"  User input: {data.get('content')}")
