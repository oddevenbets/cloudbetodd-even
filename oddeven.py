import requests
import uuid
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import logging
import firebase_admin
from firebase_admin import credentials, db
import os, json

# ----------------------------------------------------
# Firebase setup (from environment variable)
# ----------------------------------------------------
# FIREBASE_KEY_JSON should contain the full service account JSON
firebase_key = json.loads(os.environ["FIREBASE_KEY_JSON"])
cred = credentials.Certificate(firebase_key)

# Replace with your Firebase Realtime Database URL
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
executor = ThreadPoolExecutor(max_workers=10)

# ----------------------------------------------------
# Firebase Helpers
# ----------------------------------------------------
def save_event_id(event_id):
    """Save an event ID into Firebase Realtime DB with timestamp"""
    ref = events_ref.child(str(event_id))
    ref.set({
        "created_at": datetime.utcnow().isoformat()
    })

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
    params = {
        "sport": "basketball",
        "live": "true",
        "markets": "basketball.odd_even"
    }

    try:
        response = requests.get(f"{FEED_API_URL}/events", headers=headers, params=params, timeout=10)
        response.raise_for_status()
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

    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Error fetching events: {e}")
        return []

def get_odd_even_market(event_id):
    params = {"markets": "basketball.odd_even"}
    try:
        response = requests.get(f"{FEED_API_URL}/events/{event_id}", headers=headers, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()

        if "markets" in data and "basketball.odd_even" in data["markets"]:
            market = data["markets"]["basketball.odd_even"]
            if "submarkets" in market:
                for submarket in market["submarkets"].values():
                    return submarket["selections"]
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Error fetching market: {e}")
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

        try:
            response = requests.post(f"{TRADING_API_URL}/place", headers=headers, json=bet_payload, timeout=10)
            response.raise_for_status()
            bet_response = response.json()
            bets_placed.append(bet_response)

            # Save event ID in Firebase
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

        except requests.exceptions.RequestException as e:
            logging.error(f"❌ Failed to place {side} bet on {event_info['event_name']}: {e}")

    return bets_placed

def check_bet_status(reference_id):
    try:
        response = requests.get(f"{TRADING_API_URL}/{reference_id}/status", headers=headers, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Error checking status for {reference_id}: {e}")
        return None

def monitor_bet(reference_id, max_checks=30, interval=10):
    terminal_states = [
        'ACCEPTED', 'REJECTED', 'WIN', 'LOSS', 'PUSH',
        'MARKET_SUSPENDED', 'INSUFFICIENT_FUNDS', 'CANCELLED'
    ]

    for _ in range(max_checks):
        status = check_bet_status(reference_id)
        if not status:
            time.sleep(interval)
            continue

        if status['status'] in terminal_states:
            logging.info(f"\n✅ Bet {reference_id} resolved:")
            logging.info(f"  Event: {active_bets.get(reference_id, {}).get('event', 'Unknown')}")
            logging.info(f"  Side: {active_bets.get(reference_id, {}).get('side', 'Unknown')}")
            logging.info(f"  Final Status: {status['status']}")
            active_bets.pop(reference_id, None)
            return

        time.sleep(interval)

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
            # Filter out already-seen events
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
