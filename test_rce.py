import requests
import json

base_url = "http://localhost:5000"

def check_exec():
    try:
        res = requests.post(f"{base_url}/api/exec", json={"command": "echo test"})
        print(f"Status: {res.status_code}")
        print(res.text)
    except Exception as e:
        print("Could not connect to server:", e)

check_exec()
