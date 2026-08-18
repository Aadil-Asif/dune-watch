#!/usr/bin/env python3
import json
import urllib.request
import urllib.error
import os
from datetime import datetime, timedelta

# Configuration
MOVIE_TITLE = "Dune: Part Three"
TARGET_DATE = "2026-12-14"  # December 14, 2026
TARGET_TIME = "6:00p"  # 6:00 PM (12-hour format with 'p' suffix)
THEATER_ID = "AAOON"  # Cinemark Seven Bridges, Woodridge IL
THEATER_NAME = "Cinemark Seven Bridges and IMAX"
THEATER_KEY = "cinemark-woodridge"

# Notification settings
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "")

STATE_FILE = "state.json"

def load_state():
    """Load the state file tracking previous showtimes."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "version": 1,
        "last_run": None,
        "showtimes": {},
        "consecutive_failures": 0,
        "seeded": False
    }

def save_state(state):
    """Save state to file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def fetch_showtimes():
    """Fetch showtimes from Fandango API for the target date."""
    url = f"https://www.fandango.com/napi/theaterMovieShowtimes/{THEATER_ID}?startDate={TARGET_DATE}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"https://www.fandango.com/cinemark-seven-bridges-and-imax-aaoon/theater-page",
        "Accept": "application/json",
    }
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
        return data
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.reason}")
        return None
    except Exception as e:
        print(f"Error fetching showtimes: {e}")
        return None

def extract_target_showtime(api_response):
    """Extract the specific showtime we're monitoring from API response."""
    if not api_response or "movieShowtimes" not in api_response:
        return None
    
    for movie in api_response.get("movieShowtimes", []):
        if movie.get("movieTitle", "").strip() != MOVIE_TITLE:
            continue
        
        for showtime in movie.get("showtimes", []):
            # Check if this is our target time
            time_string = showtime.get("dateTime", "").split()[-1]  # Get time part
            
            if time_string != TARGET_TIME:
                continue
            
            # This is our target showtime!
            status = "available" if showtime.get("isAvailable") else "soldout"
            
            return {
                "date": TARGET_DATE,
                "time": TARGET_TIME,
                "status": status,
                "theater": THEATER_NAME,
                "theater_key": THEATER_KEY,
                "title": MOVIE_TITLE,
                "url": f"https://www.fandango.com/cinemark-seven-bridges-and-imax-aaoon/theater-page",
                "ticketing_date": showtime.get("ticketingDate")
            }
    
    return None

def send_notification(title, message):
    """Send notification via ntfy.sh."""
    if not NTFY_TOPIC:
        print("⚠️  NTFY_TOPIC not set - skipping notification")
        return False
    
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    
    payload = {
        "title": title,
        "message": message,
        "priority": "high",
        "tags": ["movie", "dune"]
    }
    
    headers = {
        "Content-Type": "application/json",
    }
    
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    
    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        
        with urllib.request.urlopen(req, timeout=10) as response:
            print(f"✅ Notification sent: {title}")
            return True
    except Exception as e:
        print(f"❌ Failed to send notification: {e}")
        return False

def main():
    """Main monitoring loop."""
    print(f"\n{'='*60}")
    print(f"Dune: Part Three Ticket Monitor")
    print(f"Theater: {THEATER_NAME}")
    print(f"Target: {TARGET_DATE} at {TARGET_TIME}")
    print(f"{'='*60}\n")
    
    # Load previous state
    state = load_state()
    state["last_run"] = datetime.utcnow().isoformat()
    
    # Fetch current showtimes
    print("📡 Fetching showtimes from Fandango...")
    api_response = fetch_showtimes()
    
    if not api_response:
        state["consecutive_failures"] += 1
        print(f"❌ Failed to fetch (attempt {state['consecutive_failures']})")
        save_state(state)
        return
    
    state["consecutive_failures"] = 0
    
    # Extract our target showtime
    current_showtime = extract_target_showtime(api_response)
    
    if not current_showtime:
        print(f"❌ Target showtime not found (may not be released yet)")
        state["showtimes"] = {}
        save_state(state)
        return
    
    print(f"✅ Found: {current_showtime['time']} - Status: {current_showtime['status'].upper()}")
    
    # Check if this is our first run (seeding)
    if not state.get("showtimes"):
        print("📝 First run - recording initial state (no notifications sent)")
        state["seeded"] = True
        state["showtimes"] = {
            f"{THEATER_KEY}:{TARGET_DATE}+{TARGET_TIME}": current_showtime
        }
        save_state(state)
        return
    
    # Compare with previous state
    showtime_key = f"{THEATER_KEY}:{TARGET_DATE}+{TARGET_TIME}"
    previous_showtime = state["showtimes"].get(showtime_key)
    
    if not previous_showtime:
        # First time seeing this showtime
        print(f"🆕 NEW: First time seeing this showtime")
        send_notification(
            "🎬 Dune: Part Three Now Available!",
            f"{TARGET_DATE} at {TARGET_TIME}\n{THEATER_NAME}\n\nTickets are now available!"
        )
    elif previous_showtime.get("status") == "soldout" and current_showtime["status"] == "available":
        # FREED: Went from sold out to available
        print(f"🔓 FREED: Tickets became available!")
        send_notification(
            "🎬 Dune Tickets Freed!",
            f"{TARGET_DATE} at {TARGET_TIME}\n{THEATER_NAME}\n\nTickets just became available - get them quick!"
        )
    elif previous_showtime.get("status") == current_showtime["status"]:
        # No change
        print(f"➡️  No change: Still {current_showtime['status'].upper()}")
    else:
        # Status changed but not sold-out → available
        print(f"⚠️  Status changed: {previous_showtime.get('status')} → {current_showtime['status']}")
    
    # Update state
    state["showtimes"] = {showtime_key: current_showtime}
    save_state(state)
    # TEST MODE: Send a test notification
if os.getenv("TEST_MODE") == "true":
    send_notification(
        "🧪 TEST NOTIFICATION",
        "If you're reading this, notifications are working!"
    )
    print("\n✅ Check complete\n")

if __name__ == "__main__":
    main()
