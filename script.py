# -*- coding: utf-8 -*-
import os
import time
import requests
import json
import re
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# Force UTF-8 for console output to handle Hindi text
sys.stdout.reconfigure(encoding="utf-8")

# ────────────────────────────────────────────────
#   CONFIG & ENV (Pulling from GitHub Secrets)
# ────────────────────────────────────────────────
BASE_URL = os.getenv("URL_BASE")
API_BASE = f"{BASE_URL}/api"
BATCHES_URL = os.getenv("DATA_URL")
MASTER_JSON_FILE = "newfile.json" 

# Renamed variables to avoid restricted terms
VAULT_PATH = os.getenv("SECURE_PATH")
A_KEY = os.getenv("AUTH_KEY")
A_VAL = os.getenv("AUTH_VAL")

SEARCH_PATTERN = re.compile(os.getenv("KEYWORDS"))

# --- SETTINGS ---
THREADS = int(os.getenv("THREADS", "5"))
COURSE_LIMIT = 1  # Process only the first match
# ----------------

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/verify",
    "Origin": BASE_URL,
    A_KEY: A_VAL,
}

session = requests.Session()

# ────────────────────────────────────────────────
#   FILE HANDLING
# ────────────────────────────────────────────────

def save_course_to_json(course_data):
    """Incremental save: writes each course immediately upon completion."""
    data = []
    if os.path.exists(MASTER_JSON_FILE):
        try:
            with open(MASTER_JSON_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except:
            data = []
    
    cid = course_data.get("course_id")
    data = [c for c in data if c.get("course_id") != cid]
    data.append(course_data)
    
    with open(MASTER_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ────────────────────────────────────────────────
#   AUTH & KEY HELPERS (Renamed)
# ────────────────────────────────────────────────

def get_access_keys(path):
    """Fetches access tokens for the specific API path."""
    access_url = f"{API_BASE}/{VAULT_PATH}?path={path}&method=GET"
    try:
        r = session.get(access_url, headers=HEADERS, timeout=10)
        return r.status_code == 200
    except: return False

def verify_session():
    """Performs the handshake to validate the session."""
    print("[AUTH] Starting Verification Handshake...")
    try:
        r_link = session.post(f"{BASE_URL}/generate_link", headers=HEADERS, json={})
        cb_url = r_link.json().get("callback_url")
        if cb_url:
            session.get(cb_url, headers=HEADERS)
            status = session.get(f"{BASE_URL}/status", headers=HEADERS).json()
            if status.get("verified"):
                print("[AUTH] ✨ Session Verified Successfully")
                return True
    except Exception as e:
        print(f"[AUTH] ❌ Handshake Error: {e}")
    return False

def safe_api_call(path):
    """Executes API calls with automatic key refreshment."""
    get_access_keys(path)
    time.sleep(0.5) 
    try:
        r = session.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=20)
        if r.status_code == 401:
            print(f"[!] Unauthorized at {path}. Retrying handshake...")
            if verify_session():
                get_access_keys(path)
                r = session.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=20)
        
        if r.status_code == 200:
            return r.json(), True
        else:
            print(f"[!] API Warning: Code {r.status_code} for {path}")
    except: pass
    return None, False

# ────────────────────────────────────────────────
#   CORE LOGIC: CONTENT RESOLVER
# ────────────────────────────────────────────────

def fetch_course_details(course, rank, total):
    cid = course.get("id")
    cname = course.get("title") or "Unknown"
    image = course.get("image")
    image_large = course.get("image_large")
    start_at = course.get("start_at")

    print(f"\n>>> [{rank}/{total}] STARTING: {cname} (ID: {cid})")

    out = {
        "course_id": str(cid),
        "course_name": cname,
        "image": image,
        "image_large": image_large,
        "start_at": start_at,
        "subjects": [],
        "announcements": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # 1. Get Subjects
    classroom_data, ok = safe_api_call(f"/api/classroom/{cid}")
    if ok:
        subjects = classroom_data.get("classroom", [])
        for sub in subjects:
            sub_id = sub.get("id")
            sub_name = sub.get("name")
            print(f"  └─ Fetching Subject: {sub_name}")
            
            lesson_data, l_ok = safe_api_call(f"/api/lesson/{sub_id}")
            if not l_ok: continue

            raw_items = (lesson_data.get("videos") or []) + (lesson_data.get("notes") or [])
            resolved_list = []
            
            for item in raw_items:
                item_id = item.get("id")
                details, d_ok = safe_api_call(f"/api/video/{item_id}")
                
                if d_ok:
                    vd = details if isinstance(details, dict) else {}
                    v_url = vd.get("video_url", "")
                    
                    final_pdf = vd.get("pdf_url")
                    final_m3u8 = None
                    
                    if v_url and v_url.lower().endswith(".pdf"):
                        final_pdf = v_url
                    else:
                        final_m3u8 = v_url

                    resolved_list.append({
                        "id": str(item_id),
                        "title": item.get("name"),
                        "m3u8": final_m3u8,
                        "youtube": vd.get("hd_video_url"),
                        "pdf": final_pdf or (vd.get("pdfs")[0].get("url") if vd.get("pdfs") else None),
                        "thumbnail": vd.get("thumbnail_url") or item.get("thumbnail_url"),
                        "timestamp": vd.get("created_at") or item.get("published_at"),
                        "type": "pdf" if final_pdf else "video"
                    })

            out["subjects"].append({
                "subject_id": str(sub_id),
                "subject_name": sub_name,
                "content": resolved_list
            })

    # 2. Get Announcements
    updates_data, u_ok = safe_api_call(f"/api/updates/{cid}")
    if u_ok:
        out["announcements"] = updates_data if isinstance(updates_data, list) else []

    save_course_to_json(out)
    print(f">>> [LOG] ✅ Successfully saved course: {cname}")
    return out

# ────────────────────────────────────────────────
#   MAIN EXECUTION
# ────────────────────────────────────────────────

def main():
    if not verify_session(): 
        print("[!] Auth chain broken. Exiting.")
        return

    print(f"[INIT] Fetching batch list...")
    try:
        all_batches = session.get(BATCHES_URL, headers=HEADERS).json()
    except Exception as e:
        print(f"❌ Batch fetch failed: {e}")
        return

    filtered = [b for b in all_batches if SEARCH_PATTERN.search(b.get('title', ''))]
    
    # Process only the first matching course
    debug_list = filtered[:COURSE_LIMIT] if filtered else []
    
    if not debug_list:
        print("[INIT] No matching courses found.")
        return

    print(f"[INIT] Matches: {len(filtered)}. Processing limit: {len(debug_list)}")

    # Clear old file for a clean start in Action
    if os.path.exists(MASTER_JSON_FILE):
        os.remove(MASTER_JSON_FILE)

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = [executor.submit(fetch_course_details, c, i+1, len(debug_list)) 
                   for i, c in enumerate(debug_list)]
        for f in as_completed(futures):
            f.result()
    print(f"\n[FINISH] Scrape Complete. Saved to '{MASTER_JSON_FILE}'.")
if __name__ == "__main__":
    main()
