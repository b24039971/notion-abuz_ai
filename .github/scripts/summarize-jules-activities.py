#!/usr/bin/env python3
import json
import sys
from datetime import datetime, timezone


TOKEN = "AUTONOMOUS_CONTINUE_TOKEN"


def parse_epoch(value):
    if not value:
        return 0
    try:
        normalized = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return 0


if len(sys.argv) != 2:
    print("usage: summarize-jules-activities.py <activities.json>", file=sys.stderr)
    sys.exit(2)

with open(sys.argv[1], "r", encoding="utf-8") as f:
    activities = json.load(f).get("activities", [])

latest_agent_epoch = 0
latest_user_epoch = 0
latest_token_epoch = 0
continue_token_count = 0
latest_agent_blob = ""

for activity in activities:
    originator = str(activity.get("originator", "")).lower()
    is_user = "user" in originator
    epoch = parse_epoch(activity.get("createTime"))
    blob = json.dumps(activity, ensure_ascii=False)

    if is_user:
        latest_user_epoch = max(latest_user_epoch, epoch)
        if TOKEN in blob:
            continue_token_count += 1
            latest_token_epoch = max(latest_token_epoch, epoch)
    else:
        latest_agent_epoch = max(latest_agent_epoch, epoch)
        if epoch >= latest_agent_epoch:
            latest_agent_blob = blob

latest_agent_lower = latest_agent_blob.lower()
wait_kind = "continue"
finalize_markers = (
    "before i wrap up",
    "wrap up my work",
    "ready for review",
    "ready to finalize",
    "ready for submission",
    "all plan steps completed",
    "open a new pull request",
    "open the pull request",
    "open/finalize the pr",
    "anything else you'd like me to review",
    "anything else you would like me to review",
)

if any(marker in latest_agent_lower for marker in finalize_markers):
    wait_kind = "finalize"

print(
    f"{latest_agent_epoch}\t{latest_user_epoch}\t"
    f"{latest_token_epoch}\t{wait_kind}\t{continue_token_count}"
)
