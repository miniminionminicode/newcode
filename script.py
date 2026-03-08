# -*- coding: utf-8 -*-
import os
import time
import requests
import json
import re
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# Environment Variables
BASE_URL = os.getenv("URL_BASE")
API_BASE = f"{BASE_URL}/api"
BATCHES_URL = os.getenv("DATA_URL")
SECURE_PATH = os.getenv("SECURE_PATH")
AUTH_KEY = os.getenv("AUTH_KEY")
AUTH_VAL = os.getenv("AUTH_VAL")
KEYWORDS = os.getenv("KEYWORDS")
THREADS = int(os.getenv("THREADS", 5))

MASTER_JSON_FILE = "newfile.json"
SEARCH_PATTERN = re.compile(KEYWORDS, re.IGNORECASE)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/verify",
    "Origin": BASE_URL,
    AUTH_KEY: AUTH_VAL,
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

def get_resource_keys(path):
    # Obfuscated helper for the secure key endpoint
    url = f"{API_BASE}/{SECURE_PATH}?path={path}&method=GET"
    try:
        r = session.get(url, headers=HEADERS, timeout=10)
        return r.status_code == 200
    except: return False

def secure_handshake():
    try:
        r_link = session.post(f"{BASE_URL}/generate_link", headers=HEADERS, json={})
        cb_url = r_link.json().get("callback_url")
        if cb_url:
            session.get(cb_url, headers=HEADERS)
            status = session.get(f"{BASE_URL}/status", headers=HEADERS).json()
            return status.get("verified")
    except Exception as e:
        print(f"[AUTH] Handshake Failed: {e}")
    return False

def safe_api_call(path):
    get_resource_keys(path)
    time.sleep(0.5) 
    try:
        r = session.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=20)
        if r.status_code == 401:
            if secure_handshake():
                get_resource_keys(path)
                r = session.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json(), True
    except: pass
    return None, False

def fetch_course_details(course, rank, total):
    cid = course.get("id")
    cname = course.get("title") or "Unknown"
    
    print(f"[*] Processing {rank}/{total}: {cname}")

    out = {
        "course_id": str(cid),
        "course_name": cname,
        "image": course.get("image"),
        "subjects": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    classroom_data, ok = safe_api_call(f"/api/classroom/{cid}")
    if ok:
        subjects = classroom_data.get("classroom", [])
        for sub in subjects:
            sub_id = sub.get("id")
            lesson_data, l_ok = safe_api_call(f"/api/lesson/{sub_id}")
            if not l_ok: continue

            raw_items = (lesson_data.get("videos") or []) + (lesson_data.get("notes") or [])
            resolved_list = []
            
            for item in raw_items:
                details, d_ok = safe_api_call(f"/api/video/{item.get('id')}")
                if d_ok:
                    vd = details if isinstance(details, dict) else {}
                    v_url = vd.get("video_url", "")
                    is_pdf = v_url.lower().endswith(".pdf")
                    
                    resolved_list.append({
                        "id": str(item.get("id")),
                        "title": item.get("name"),
                        "m3u8": None if is_pdf else v_url,
                        "pdf": v_url if is_pdf else (vd.get("pdfs")[0].get("url") if vd.get("pdfs") else None),
                        "type": "pdf" if is_pdf else "video"
                    })

            out["subjects"].append({"subject_name": sub.get("name"), "content": resolved_list})

    save_to_json(out)
    return out

def main():
    if not secure_handshake(): 
        print("Auth failed.")
        return

    try:
        all_batches = session.get(BATCHES_URL, headers=HEADERS).json()
        filtered = [b for b in all_batches if SEARCH_PATTERN.search(b.get('title', ''))]
    except Exception as e:
        print(f"Batch fetch failed: {e}")
        return

    if os.path.exists(MASTER_JSON_FILE): os.remove(MASTER_JSON_FILE)

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = [executor.submit(fetch_course_details, c, i+1, len(filtered)) 
                   for i, c in enumerate(filtered)]
        for f in as_completed(futures): f.result()

if __name__ == "__main__":
    main()
