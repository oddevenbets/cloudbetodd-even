import requests
import uuid
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import logging
import firebase_admin
from firebase_admin import credentials, db
import os, json, random, threading

# ----------------------------------------------------
# Firebase setup (from environment variable)
# ----------------------------------------------------
firebase_key = json.loads(os.environ["FIREBASE_KEY_JSON"])
cred = credentials.Certificate(firebase_key)

firebase_admin.initialize_app(cred, {
    "databaseURL": "https://odd-even-5e6eb-default-rtdb.firebaseio.com"
})

# Reference to "events" collection in Realtime DB
events_ref = db.reference("events")

# ----------------------------------------------------
# Logging setup
# ----------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(message)s")

# ----------------------------------------------------
# Constants (Cloudbet API key from environment variable)
# ----------------------------------------------------
API_KEY = os.environ["CLOUDBET_API_KEY"]
FEED_API_URL = "https://sports-api.cloudbet.com/pub/v2/odds"
TRADING_API_URL = "https://sports-api.cloudbet.com/pub/v3/bets"

headers = {
    "X-API-Key": API_KEY,
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# Global variables
active_bets = {}
executor = ThreadPoolExecutor(max_workers=5)  # reduced workers for safety

# ----------------------------------------------------
# Global Rate Limiter (max 1 request/sec to stay well under 2 RPS)
# ----------------------------------------------------
RATE_LIMIT_LOCK = threading.Lock()
LAST_CALL_TIME = 0
MIN_INTERVAL = 1.0  # 1 second between requests

def rate_limited_request(method, url, **kwargs):
    """Perform a request respecting Cloudbet's 2RPS limit (we enforce 1RPS)."""
    global LAST_CALL_TIME
    with RATE_LIMIT_LOCK:
        now = time.time()
        elapsed = now - LAST_CALL_TIME
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed + random.uniform(0.05, 0.2))  # add jitter
        LAST_CALL_TIME = time.time()
    return safe_request(method, url, **kwargs)

# ----------------------------------------------------
# Safe Request with Backoff
# ----------------------------------------------------
def safe_request(method, url, **kwargs):
    """Perform a request with exponential backoff if Cloudbet returns errors."""
    backoff = 5
    max_backoff = 600  # cap at 10 minutes

    for attempt in range(8):
        try:
            response = requests.request(method, url, headers=headers, timeout=10, **kwargs)

            if response.status_code == 429:
                logging.warning(f"⚠️ Rate limit hit. Backing off {backoff}s...")
                time.sleep(backoff + random.uniform(1, 5))
                backoff = min(backoff * 2, max_backoff)
                continue

            response.raise_for_status()
            return response

        except requests.exceptions.RequestException as e:
            logging.error(f"❌ Request error: {e}")
            time.sleep(backoff + random.uniform(1, 5))
            backoff = min(backoff * 2, max_backoff)

    logging.error(f"❌ Failed after multiple retries: {url}")
    return None

# ----------------------------------------------------
# Firebase Helpers
# ----------------------------------------------------
def save_event_id(event_id):
    """Save an event ID into Firebase Realtime DB with timestamp"""
    ref = events_ref.child(str(event_id))
    ref.set({"created_at": datetime.utcnow().isoformat()})

def load_existing_event_ids():
    """Load all event IDs from Firebase Realtime DB"""
    snapshot = events_ref.get()
    if snapshot:
        return set(snapshot.keys())
    return set()

# ----------------------------------------------------
# Betting Functions
# ----------------------------------------------------
def get_all_live_basketball_events():
    logging.info("\nSearching for all live basketball events...")
    params = {"sport": "basketball", "live": "true", "markets": "basketball.odd_even"}

    response = rate_limited_request("GET", f"{FEED_API_URL}/events", params=params)
    if not response:
        return []

    data = response.json()
    events = []

    if not data.get("competitions"):
        logging.info("No live basketball games available.")
        return events

    for competition in data["competitions"]:
        for event in competition["events"]:
            if event.get('status', '').lower() == 'trading_live':
                selections = get_odd_even_market(event['id'])
                if selections:
                    filtered_selections = [s for s in selections if float(s['price']) > 1.84]
                    if filtered_selections:
                        events.append({
                            'event_id': event['id'],
                            'event_name': f"{event['home']['name']} vs {event['away']['name']}",
                            'competition': competition['name'],
                            'selections': filtered_selections
                        })

    logging.info(f"Found {len(events)} live events with Odd/Even markets and odds > 1.84")
    return events

def get_odd_even_market(event_id):
    params = {"markets": "basketball.odd_even"}
    response = rate_limited_request("GET", f"{FEED_API_URL}/events/{event_id}", params=params)
    if not response:
        return None

    data = response.json()
    if "markets" in data and "basketball.odd_even" in data["markets"]:
        market = data["markets"]["basketball.odd_even"]
        if "submarkets" in market:
            for submarket in market["submarkets"].values():
                return submarket["selections"]
    return None

def place_bet(event_info, stake_per_side=1.0):
    bets_placed = []
    currency = "PLAY_EUR"

    for selection in event_info['selections']:
        side = selection['outcome']
        price = selection['price']

        bet_payload = {
            "eventId": str(event_info['event_id']),
            "marketUrl": f"basketball.odd_even/{side}",
            "price": str(price),
            "stake": str(stake_per_side),
            "currency": currency,
            "referenceId": str(uuid.uuid4()),
            "acceptPriceChange": "BETTER"
        }

        response = rate_limited_request("POST", f"{TRADING_API_URL}/place", json=bet_payload)
        if not response:
            logging.error(f"❌ Failed to place {side} bet on {event_info['event_name']}")
            continue

        bet_response = response.json()
        bets_placed.append(bet_response)

        save_event_id(event_info['event_id'])

        logging.info(f"\n🎯 Placed {side} bet:")
        logging.info(f"  Event: {event_info['event_name']}")
        logging.info(f"  Stake: {stake_per_side} {currency}")
        logging.info(f"  Price: {price}")
        logging.info(f"  Ref ID: {bet_response['referenceId']}")
        logging.info(f"  Status: {bet_response['status']}")

        if bet_response['status'] == 'PENDING_ACCEPTANCE':
            active_bets[bet_response['referenceId']] = {
                'event': event_info['event_name'],
                'side': side,
                'stake': stake_per_side,
                'currency': currency
            }
            executor.submit(monitor_bet, bet_response['referenceId'])

    return bets_placed

def check_bet_status(reference_id):
    response = rate_limited_request("GET", f"{TRADING_API_URL}/{reference_id}/status")
    if response:
        return response.json()
    return None

def monitor_bet(reference_id, max_checks=60, base_interval=45):
    terminal_states = [
        'ACCEPTED', 'REJECTED', 'WIN', 'LOSS', 'PUSH',
        'MARKET_SUSPENDED', 'INSUFFICIENT_FUNDS', 'CANCELLED'
    ]

    for attempt in range(max_checks):
        status = check_bet_status(reference_id)
        if status:
            if status['status'] in terminal_states:
                logging.info(f"\n✅ Bet {reference_id} resolved:")
                logging.info(f"  Event: {active_bets.get(reference_id, {}).get('event', 'Unknown')}")
                logging.info(f"  Side: {active_bets.get(reference_id, {}).get('side', 'Unknown')}")
                logging.info(f"  Final Status: {status['status']}")
                active_bets.pop(reference_id, None)
                return
            else:
                logging.info(f"ℹ️ Bet {reference_id} still {status['status']}")

        # Wait before next check (longer interval + jitter)
        sleep_time = base_interval + random.uniform(5, 15)
        time.sleep(sleep_time)

    logging.warning(f"\n⚠️ Bet {reference_id} monitoring ended without resolution")
    active_bets.pop(reference_id, None)

# ----------------------------------------------------
# Main
# ----------------------------------------------------
def main():
    stake_per_side = 1.0
    while True:
        logging.info(f"\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        existing_event_ids = load_existing_event_ids()
        events = get_all_live_basketball_events()
        
        if events:
            events = [event for event in events if str(event['event_id']) not in existing_event_ids]
            if events:
                logging.info(f"\n🚀 Placing bets on {len(events)} new events...")
                for event in events:
                    place_bet(event, stake_per_side)
            else:
                logging.info("✅ No new events to place bets on")
        else:
            logging.info("No live basketball games found")

        # Wait before checking again
        time.sleep(300)  # check every 5 minutes

if __name__ == "__main__":
    main()
