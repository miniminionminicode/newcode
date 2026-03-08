# -*- coding: utf-8 -*-
import os
import time
import requests
import json
import re
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding="utf-8")

# ────────────────────────────────────────────────
# CONFIG (ENV BASED)
# ────────────────────────────────────────────────
BASE_URL = os.getenv("URL_BASE")
API_BASE = f"{BASE_URL}/api"
BATCHES_URL = os.getenv("DATA_URL")
MASTER_JSON_FILE = "newfile.json"
SEARCH_PATTERN = re.compile(os.getenv("KEYWORDS", ".*"), re.IGNORECASE)

THREADS = int(os.getenv("THREADS", "5"))
COURSE_LIMIT = int(os.getenv("COURSE_LIMIT", "1"))
VAULT_PATH = os.getenv("SECURE_PATH")
A_KEY = os.getenv("AUTH_KEY")
A_VAL = os.getenv("AUTH_VAL")

# Enhanced Headers to mimic a real browser better
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/verify",
    "Origin": BASE_URL,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    A_KEY: A_VAL,
}

session = requests.Session()

# ────────────────────────────────────────────────
# AUTH SYSTEM
# ────────────────────────────────────────────────

def get_access_keys(path):
    url = f"{API_BASE}/{VAULT_PATH}?path={path}&method=GET"
    try:
        # Use session to ensure cookies are captured
        r = session.get(url, headers=HEADERS, timeout=10)
        return r.status_code == 200
    except:
        return False

def verify_session():
    print("[AUTH] Starting handshake...")
    try:
        r_link = session.post(f"{BASE_URL}/generate_link", headers=HEADERS, json={})
        cb_url = r_link.json().get("callback_url")
        if cb_url:
            session.get(cb_url, headers=HEADERS)
            status_res = session.get(f"{BASE_URL}/status", headers=HEADERS)
            status = status_res.json()
            if status.get("verified"):
                print("[AUTH] ✅ Session verified")
                return True
    except Exception as e:
        print(f"[AUTH] ❌ Error: {e}")
    return False

def safe_api_call(path):
    # Step 1: Request keys for this specific path
    get_access_keys(path)
    
    # Step 2: Critical delay for server-side propagation
    time.sleep(0.8) 

    try:
        r = session.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=20)
        
        # If 403, attempt ONE re-handshake
        if r.status_code == 403:
            print(f"[API] 403 Forbidden for {path}. Attempting refresh...")
            if verify_session():
                get_access_keys(path)
                time.sleep(1.0)
                r = session.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=20)

        if r.status_code == 200:
            return r.json(), True
        else:
            print(f"[API] ❌ Failed: {r.status_code} for {path}")
    except:
        pass
    return None, False

# ────────────────────────────────────────────────
# CORE SCRAPER & FILE HANDLING
# ────────────────────────────────────────────────

def save_course_to_json(course_data):
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

def fetch_course_details(course, rank, total):
    cid = course.get("id")
    cname = course.get("title") or "Unknown"
    print(f"\n>>> [{rank}/{total}] STARTING: {cname}")

    out = {
        "course_id": str(cid),
        "course_name": cname,
        "image": course.get("image"),
        "image_large": course.get("image_large"),
        "start_at": course.get("start_at"),
        "subjects": [],
        "announcements": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    classroom_data, ok = safe_api_call(f"/api/classroom/{cid}")
    if ok:
        subjects = classroom_data.get("classroom", [])
        for sub in subjects:
            sub_id, sub_name = sub.get("id"), sub.get("name")
            print(f"  └─ Subject: {sub_name}")
            lesson_data, l_ok = safe_api_call(f"/api/lesson/{sub_id}")
            if not l_ok: continue

            raw_items = (lesson_data.get("videos") or []) + (lesson_data.get("notes") or [])
            resolved_list = []
            for item in raw_items:
                details, d_ok = safe_api_call(f"/api/video/{item.get('id')}")
                if d_ok:
                    vd = details if isinstance(details, dict) else {}
                    v_url = vd.get("video_url", "")
                    f_pdf = v_url if v_url and v_url.lower().endswith(".pdf") else vd.get("pdf_url")
                    
                    resolved_list.append({
                        "id": str(item.get("id")),
                        "title": item.get("name"),
                        "m3u8": None if f_pdf else v_url,
                        "youtube": vd.get("hd_video_url"),
                        "pdf": f_pdf or (vd.get("pdfs")[0].get("url") if vd.get("pdfs") else None),
                        "thumbnail": vd.get("thumbnail_url") or item.get("thumbnail_url"),
                        "timestamp": vd.get("created_at") or item.get("published_at"),
                        "type": "pdf" if f_pdf else "video"
                    })
            out["subjects"].append({"subject_id": str(sub_id), "subject_name": sub_name, "content": resolved_list})

    updates_data, u_ok = safe_api_call(f"/api/updates/{cid}")
    if u_ok:
        out["announcements"] = updates_data if isinstance(updates_data, list) else []

    save_course_to_json(out)
    print(f">>> ✅ Saved: {cname}")
    return out

def main():
    if not verify_session(): return
    print("[INIT] Fetching batch list...")
    try:
        all_batches = session.get(BATCHES_URL, headers=HEADERS).json()
    except Exception as e:
        print(f"Batch fetch failed: {e}"); return

    filtered = [b for b in all_batches if SEARCH_PATTERN.search(b.get("title", ""))]
    debug_list = filtered[:COURSE_LIMIT] if COURSE_LIMIT else filtered
    print(f"[INIT] Matches: {len(filtered)} | Processing: {len(debug_list)}")

    if os.path.exists(MASTER_JSON_FILE): os.remove(MASTER_JSON_FILE)

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = [executor.submit(fetch_course_details, c, i + 1, len(debug_list)) for i, c in enumerate(debug_list)]
        for f in as_completed(futures): f.result()

    print(f"\n[FINISH] Output saved → {MASTER_JSON_FILE}")

if __name__ == "__main__":
    main()
