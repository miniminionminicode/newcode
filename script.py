# -*- coding: utf-8 -*-
import os
import time
import requests
import json
import re
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

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
THREADS = int(os.getenv("THREADS", "5"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/verify",
    "Origin": BASE_URL,
    A_KEY: A_VAL,
}

session = requests.Session()

# ────────────────────────────────────────────────
#   LOGGING & AUTH HELPERS
# ────────────────────────────────────────────────

def get_access_keys(path):
    """Fetches access tokens and logs the result."""
    access_url = f"{API_BASE}/{VAULT_PATH}?path={path}&method=GET"
    print(f"  [KEY-CHECK] Requesting keys for: {path}")
    try:
        r = session.get(access_url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            print(f"  [KEY-CHECK] ✅ Keys accepted for {path}")
            return True
        else:
            print(f"  [KEY-CHECK] ❌ Failed! Status: {r.status_code} | Body: {r.text[:100]}")
            return False
    except Exception as e:
        print(f"  [KEY-CHECK] ⚠️ Exception: {e}")
        return False

def verify_session():
    print("\n[AUTH] Starting Handshake...")
    try:
        gen_url = f"{BASE_URL}/generate_link"
        r_link = session.post(gen_url, headers=HEADERS, json={})
        print(f"[AUTH] Step 1 (Link Gen): {r_link.status_code}")
        
        cb_url = r_link.json().get("callback_url")
        if cb_url:
            print(f"[AUTH] Step 2 (Callback): Calling {cb_url[:50]}...")
            r_cb = session.get(cb_url, headers=HEADERS)
            
            status_url = f"{BASE_URL}/status"
            r_status = session.get(status_url, headers=HEADERS)
            status = r_status.json()
            
            if status.get("verified"):
                print(f"[AUTH] ✨ SUCCESS: Session is verified.")
                return True
            else:
                print(f"[AUTH] ❌ FAILED: Status shows 'not verified'. JSON: {status}")
        else:
            print(f"[AUTH] ❌ FAILED: No callback URL in response. Body: {r_link.text}")
    except Exception as e:
        print(f"[AUTH] ❌ CRITICAL ERROR: {e}")
    return False

def safe_api_call(path):
    """Executes API calls and logs full details on failure."""
    # Always get keys first
    get_access_keys(path)
    time.sleep(0.8) # Increased delay to allow server-side propagation
    
    full_url = f"{BASE_URL}{path}"
    try:
        r = session.get(full_url, headers=HEADERS, timeout=20)
        
        if r.status_code == 401:
            print(f"  [API] 401 Unauthorized at {path}. Attempting re-auth...")
            if verify_session():
                get_access_keys(path)
                r = session.get(full_url, headers=HEADERS, timeout=20)

        if r.status_code == 200:
            return r.json(), True
        else:
            print(f"  [API] ❌ ERROR {r.status_code} for URL: {full_url}")
            print(f"  [API] Headers Sent: {A_KEY}: {A_VAL}")
            print(f"  [API] Response Body: {r.text[:200]}") # Show start of error page
            return None, False
    except Exception as e:
        print(f"  [API] ⚠️ Exception calling {path}: {e}")
        return None, False

# ────────────────────────────────────────────────
#   CORE LOGIC
# ────────────────────────────────────────────────

def fetch_course_details(course):
    cid = course.get("id")
    cname = course.get("title", "Unknown")
    print(f"\n{'='*60}\n>>> PROCESSING: {cname} (ID: {cid})\n{'='*60}")

    out = {
        "course_id": str(cid),
        "course_name": cname,
        "subjects": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # Fetch Classroom
    classroom_data, ok = safe_api_call(f"/api/classroom/{cid}")
    if ok:
        subjects = classroom_data.get("classroom", [])
        print(f"  [DATA] Found {len(subjects)} subjects.")
        for sub in subjects:
            sub_id = sub.get("id")
            sub_name = sub.get("name")
            print(f"    └─ Subject: {sub_name}")
            
            lesson_data, l_ok = safe_api_call(f"/api/lesson/{sub_id}")
            if l_ok:
                raw_items = (lesson_data.get("videos") or []) + (lesson_data.get("notes") or [])
                resolved_list = []
                for item in raw_items:
                    details, d_ok = safe_api_call(f"/api/video/{item.get('id')}")
                    if d_ok:
                        vd = details if isinstance(details, dict) else {}
                        v_url = vd.get("video_url", "")
                        resolved_list.append({
                            "id": str(item.get("id")),
                            "title": item.get("name"),
                            "url": v_url,
                            "type": "pdf" if v_url.lower().endswith(".pdf") else "video"
                        })
                out["subjects"].append({"subject_name": sub_name, "content": resolved_list})

    # Save immediately
    save_to_file(out)
    return out

def save_to_file(course_data):
    data = []
    if os.path.exists(MASTER_JSON_FILE):
        try:
            with open(MASTER_JSON_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except: data = []
    
    data = [c for c in data if c.get("course_id") != course_data["course_id"]]
    data.append(course_data)
    with open(MASTER_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def main():
    if not verify_session():
        print("!! Initial Auth Failed. Script stopping.")
        return

    print(f"[INIT] Fetching batches from: {BATCHES_URL}")
    try:
        all_batches = session.get(BATCHES_URL, headers=HEADERS).json()
        filtered = [b for b in all_batches if SEARCH_PATTERN.search(b.get('title', ''))]
    except Exception as e:
        print(f"❌ Failed to fetch batch list: {e}")
        return

    if not filtered:
        print("!! No matches found for keywords.")
        return

    # Force limit 1
    target = filtered[0]
    print(f"[INIT] Target Found: {target.get('title')}")

    if os.path.exists(MASTER_JSON_FILE): os.remove(MASTER_JSON_FILE)
    
    fetch_course_details(target)
    print(f"\n[FINISH] Done. Results in {MASTER_JSON_FILE}")

if __name__ == "__main__":
    main()
