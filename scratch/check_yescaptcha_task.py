import requests
import json

client_key = "3dae6d235caf5de1a4b368179556645b80e698d4127415"
task_id = "17beaa02-6cc3-11f1-b25d-525400d743ab"

r = requests.post("https://api.yescaptcha.com/getTaskResult", json={
    "clientKey": client_key,
    "taskId": task_id
})

print("Status:", r.status_code)
try:
    print(json.dumps(r.json(), indent=2, ensure_ascii=False))
except Exception as e:
    print("Error:", e)
    print(r.text)
