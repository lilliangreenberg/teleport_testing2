#!/usr/bin/env python3
"""Display the current time in Dublin, Ireland."""

from datetime import datetime, timezone, timedelta
import zoneinfo

def get_dublin_time():
    dublin_tz = zoneinfo.ZoneInfo("Europe/Dublin")
    now = datetime.now(dublin_tz)
    return now.strftime("%A, %B %d, %Y %I:%M:%S %p %Z")

if __name__ == "__main__":
    print(f"Current time in Dublin: {get_dublin_time()}")
