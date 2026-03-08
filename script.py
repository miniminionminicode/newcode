# -*- coding: utf-8 -*-
import os
import time
import requests
import json
import re
import sys
from datetime import datetime, timezone

# Force UTF-8 for console output
sys.stdout.reconfigure(encoding="utf-8")

# ────────────────────────────────────────────────
#   CONFIG & ENV
# ────────────────────────────────────────────────
BASE_URL = os.getenv("URL_BASE")
API_BASE = f"{BASE_URL}/api"
BATCHES_URL = os.getenv("DATA_URL")
MASTER_JSON_FILE = "newfile.json" 

VAULT_PATH = os.getenv("SECURE_PATH")
A_KEY = os.getenv("AUTH_KEY")
A_VAL = os.getenv("AUTH_VAL")

SEARCH_PATTERN = re.compile(os.getenv("KEYWORDS"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/verify",
    "Origin": BASE_URL,
    "Accept": "application/json",
    A_KEY: A_VAL,
}

session = requests.Session()

# ────────────────────────────────────────────────
#   LOGGING & AUTH HELPERS
# ────────────────────────────────────────────────

def get_access_keys(path):
    """
    """

    
    access_url = f"{API_BASE}/{VAULT_PATH}?path={path}&method=GET"
    print(f"  [KEY-FETCH] URL: {access_url}")
    try:
        # We use a fresh GET to the keys endpoint
        r = session.get(access_url, headers=HEADERS, timeout=15)
        
        if r.status_code == 200:
            print(f"  [KEY-FETCH] ✅ Success (200)")
            # If the server sends back a special token in the body, 
            # we might need to inject it into headers. 
            # For now, we rely on the session cookies/server-side state.
            return True
        else:
            print(f"  [KEY-FETCH] ❌ 403 Forbidden. The server rejected the key request.")
            return False
    except Exception as e:
        print(f"  [KEY-FETCH] ⚠️ Error: {e}")
        return False

def verify_session():
    print("\n[AUTH] Starting Handshake...")
    try:
        # Logic matches your working local script
        r_link = session.post(f"{BASE_URL}/generate_link", headers=HEADERS, json={})
        cb_url = r_link.json().get("callback_url")
        
        if cb_url:
            print(f"[AUTH] Callback detected. Validating...")
            session.get(cb_url, headers=HEADERS)
            
            r_status = session.get(f"{BASE_URL}/status", headers=HEADERS)
            if r_status.json().get("verified"):
                print(f"[AUTH] ✨ Verified successfully.")
                return True
        print(f"[AUTH] ❌ Verification failed. Response: {r_link.text}")
    except Exception as e:
        print(f"[AUTH] ❌ Handshake Error: {e}")
    return False

def safe_api_call(path):
    """Executes the key-fetch THEN the data-fetch."""
    # Step 1: Get Keys
    key_success = get_access_keys(path)
    
    # Step 2: Wait for server to register the key
    time.sleep(1.0) 
    
    # Step 3: Fetch Data
    full_url = f"{BASE_URL}{path}"
    try:
        r = session.get(full_url, headers=HEADERS, timeout=20)
        
        # If we get a 403 even after keys, the server might want a session refresh
        if r.status_code == 403:
            print(f"  [API] ❌ 403 at {path}. Trying a 'hard' retry...")
            verify_session()
            get_access_keys(path)
            time.sleep(1.0)
            r = session.get(full_url, headers=HEADERS, timeout=20)

        if r.status_code == 200:
            return r.json(), True
        else:
            print(f"  [API] ❌ Failed with {r.status_code}")
            return None, False
    except Exception as e:
        print(f"  [API] ⚠️ Request Exception: {e}")
        return None, False

# ────────────────────────────────────────────────
#   CORE LOGIC
# ────────────────────────────────────────────────

def fetch_course_details(course):
    cid = course.get("id")
    cname = course.get("title", "Unknown")
    print(f"\n>>> PROCESSING: {cname} (ID: {cid})")

    out = {
        "course_id": str(cid),
        "course_name": cname,
        "subjects": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # Classroom Data fetch
    data, ok = safe_api_call(f"/api/classroom/{cid}")
    if ok:
        subjects = data.get("classroom", [])
        print(f"  [DATA] Found {len(subjects)} subjects.")
        # ... logic for subjects ...
        # (Shortened for clarity, keep your existing loop here)
    
    save_to_file(out)
    return out

def save_to_file(course_data):
    data = []
    if os.path.exists(MASTER_JSON_FILE):
        try:
            with open(MASTER_JSON_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except: data = []
    data.append(course_data)
    with open(MASTER_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def main():
    if not verify_session(): return

    try:
        all_batches = session.get(BATCHES_URL, headers=HEADERS).json()
        filtered = [b for b in all_batches if SEARCH_PATTERN.search(b.get('title', ''))]
        if filtered:
            fetch_course_details(filtered[0])
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
