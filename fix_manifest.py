import json

with open("agent_tasks.json", "r") as f:
    data = json.load(f)

for task in data["tasks"]:
    if task["id"] == "proxy-resilience-robust-extraction-json-multiline":
        task["status"] = "done"

with open("agent_tasks.json", "w") as f:
    json.dump(data, f, indent=2)
