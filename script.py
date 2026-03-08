# -*- coding: utf-8 -*-
import os
import time
import requests
import json
import re
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# Force UTF-8 for console
sys.stdout.reconfigure(encoding="utf-8")

# --- CONFIG FROM ENV ---
BASE_URL = os.getenv("URL_BASE")
API_BASE = f"{BASE_URL}/api"
BATCHES_URL = os.getenv("DATA_URL")
SECURE_PATH = os.getenv("SECURE_PATH")
A_KEY = os.getenv("AUTH_KEY")
A_VAL = os.getenv("AUTH_VAL")
KEYWORDS = os.getenv("KEYWORDS")
THREADS = int(os.getenv("THREADS", "5"))

MASTER_JSON_FILE = "newfile.json"
SEARCH_PATTERN = re.compile(KEYWORDS, re.IGNORECASE)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/verify",
    "Origin": BASE_URL,
    A_KEY: A_VAL,
}

session = requests.Session()

def save_to_json(course_data):
    data = []
    if os.path.exists(MASTER_JSON_FILE):
        try:
            with open(MASTER_JSON_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except: data = []
    
    cid = course_data.get("course_id")
    data = [c for c in data if c.get("course_id") != cid]
    data.append(course_data)
    
    with open(MASTER_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_keys(path):
    """Fetches necessary access keys for the specific path."""
    url = f"{API_BASE}/{SECURE_PATH}?path={path}&method=GET"
    try:
        r = session.get(url, headers=HEADERS, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"  [!] Key Fetch Error: {e}")
        return False

def verify_session():
    print("[AUTH] Starting Verification Handshake...")
    try:
        r_link = session.post(f"{BASE_URL}/generate_link", headers=HEADERS, json={})
        cb_url = r_link.json().get("callback_url")
        if cb_url:
            print(f"[AUTH] Callback URL obtained. Verifying...")
            session.get(cb_url, headers=HEADERS)
            status_res = session.get(f"{BASE_URL}/status", headers=HEADERS)
            status = status_res.json()
            if status.get("verified"):
                print("[AUTH] ✨ Session Verified")
                return True
        print(f"[AUTH] ❌ Verification failed. Response: {r_link.text}")
    except Exception as e:
        print(f"[AUTH] ❌ Handshake Error: {e}")
    return False

def safe_api_call(path):
    """Handles auto-retry on 401 and key updates."""
    get_keys(path)
    time.sleep(0.5) 
    try:
        r = session.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=20)
        if r.status_code == 401:
            print(f"  [!] 401 Unauthorized for {path}. Re-verifying...")
            if verify_session():
                get_keys(path)
                r = session.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=20)
        
        if r.status_code == 200:
            return r.json(), True
        else:
            print(f"  [!] API Error {r.status_code} for {path}")
    except Exception as e:
        print(f"  [!] Request Exception: {e}")
    return None, False

def fetch_course_details(course):
    cid = course.get("id")
    cname = course.get("title") or "Unknown"
    print(f"\n>>> PROCESSING: {cname} (ID: {cid})")

    out = {
        "course_id": str(cid),
        "course_name": cname,
        "image": course.get("image"),
        "subjects": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # 1. Fetch Classroom/Subjects
    print(f"  [*] Fetching subjects for {cid}...")
    classroom_data, ok = safe_api_call(f"/api/classroom/{cid}")
    
    if ok:
        subjects = classroom_data.get("classroom", [])
        print(f"  [*] Found {len(subjects)} subjects.")
        
        for sub in subjects:
            sub_id = sub.get("id")
            sub_name = sub.get("name")
            print(f"    └─ Subject: {sub_name} (ID: {sub_id})")
            
            lesson_data, l_ok = safe_api_call(f"/api/lesson/{sub_id}")
            if not l_ok: 
                print(f"      [!] Failed to load lessons for {sub_name}")
                continue

            raw_items = (lesson_data.get("videos") or []) + (lesson_data.get("notes") or [])
            resolved_list = []
            
            print(f"      [#] Resolving {len(raw_items)} items...")
            for item in raw_items:
                item_id = item.get("id")
                details, d_ok = safe_api_call(f"/api/video/{item_id}")
                
                if d_ok:
                    vd = details if isinstance(details, dict) else {}
                    v_url = vd.get("video_url", "")
                    is_pdf = v_url.lower().endswith(".pdf")
                    
                    resolved_list.append({
                        "id": str(item_id),
                        "title": item.get("name"),
                        "m3u8": None if is_pdf else v_url,
                        "pdf": v_url if is_pdf else (vd.get("pdfs")[0].get("url") if vd.get("pdfs") else None),
                        "type": "pdf" if is_pdf else "video"
                    })

            out["subjects"].append({
                "subject_id": str(sub_id),
                "subject_name": sub_name,
                "content": resolved_list
            })
    else:
        print(f"  [!] Failed to fetch classroom data for {cname}")

    save_to_json(out)
    print(f">>> ✅ Completed: {cname}")
    return out

def main():
    if not verify_session():
        print("Stopping: Initial Auth Failed.")
        return

    print(f"[INIT] Loading batches from {BATCHES_URL}...")
    try:
        all_batches = session.get(BATCHES_URL, headers=HEADERS).json()
        filtered = [b for b in all_batches if SEARCH_PATTERN.search(b.get('title', ''))]
        print(f"[INIT] Matches found: {len(filtered)}")
    except Exception as e:
        print(f"❌ Critical Error fetching batch list: {e}")
        return

    # FORCE ONLY 1ST COURSE
    if not filtered:
        print("No matching courses found.")
        return
        
    target_course = filtered[0] 
    print(f"[INIT] Limiting to 1st course only: {target_course.get('title')}")

    if os.path.exists(MASTER_JSON_FILE):
        os.remove(MASTER_JSON_FILE)

    # Process single course
    fetch_course_details(target_course)
    
    print(f"\n[FINISH] Scrape Complete. Saved to {MASTER_JSON_FILE}")

if __name__ == "__main__":
    main()
