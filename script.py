# -*- coding: utf-8 -*-
import os
import time
import requests
import json
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import re

sys.stdout.reconfigure(encoding="utf-8")

# ────────────────────────────────────────────────
#   ENV & CONFIG
# ────────────────────────────────────────────────
BASE_URL = os.getenv("URL_BASE")
BATCHES_URL = os.getenv("DATA_URL")
SECURE_PATH = os.getenv("SECURE_PATH")
AUTH_KEY = os.getenv("AUTH_KEY")
AUTH_VAL = os.getenv("AUTH_VAL")

MASTER_JSON_FILE = "newfile.json"
THREADS = int(os.getenv("THREADS", 3))
COURSE_LIMIT = 1  # Only first course

# Keyword from env, fallback to default
SEARCH_KEYWORD = os.getenv("SEARCH_KEYWORD")
SEARCH_PATTERN = re.compile(SEARCH_KEYWORD, re.IGNORECASE)

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": f"{BASE_URL}/verify",
    "Origin": BASE_URL,
    AUTH_KEY: AUTH_VAL,
}

session = requests.Session()

# ────────────────────────────────────────────────
#   JSON HANDLING
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

# ────────────────────────────────────────────────
#   API CALLS
# ────────────────────────────────────────────────
def safe_api_call(path, retries=5, delay=1):
    for attempt in range(1, retries + 1):
        try:
            session.get(f"{BASE_URL}{SECURE_PATH}{path}&method=GET", headers=COMMON_HEADERS, timeout=10)
            r = session.get(f"{BASE_URL}{path}", headers=COMMON_HEADERS, timeout=20)
            if r.status_code == 200:
                return r.json(), True
            elif r.status_code in [401, 403]:
                print(f"Retry {attempt}/{retries} → HTTP {r.status_code}")
            else:
                print(f"Attempt {attempt}/{retries} → HTTP {r.status_code}")
        except Exception as e:
            print(f"Attempt {attempt}/{retries} → Error: {e}")
        time.sleep(delay)
    return None, False

# ────────────────────────────────────────────────
#   COURSE RESOLVER
# ────────────────────────────────────────────────
def fetch_course_details(course, rank, total):
    cid = course.get("id")
    cname = course.get("title") or "Unknown"
    image_large = course.get("image_large")
    start_at = course.get("start_at")

    print(f"\n>>> [{rank}/{total}] STARTING: {cname} (ID: {cid})")

    out = {
        "course_id": str(cid),
        "course_name": cname,
        "image_large": image_large,
        "start_at": start_at,
        "subjects": [],
        "announcements": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

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
                if not d_ok: continue

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

    updates_data, u_ok = safe_api_call(f"/api/updates/{cid}")
    if u_ok:
        out["announcements"] = updates_data if isinstance(updates_data, list) else []

    save_course_to_json(out)
    print(f">>> [LOG] ✅ Successfully saved course: {cname}")
    return out

# ────────────────────────────────────────────────
#   MAIN
# ────────────────────────────────────────────────
def main():
    print("[INIT] Loading course list...")
    try:
        all_batches = session.get(BATCHES_URL, headers=COMMON_HEADERS).json()
    except Exception as e:
        print(f"❌ Failed to fetch batches: {e}")
        return

    filtered = [b for b in all_batches if SEARCH_PATTERN.search(b.get('title',''))]
    debug_list = filtered[:COURSE_LIMIT]
    print(f"[INIT] Total Matches: {len(filtered)}. Processing Now: {len(debug_list)}")

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = [executor.submit(fetch_course_details, c, i+1, len(debug_list)) for i, c in enumerate(debug_list)]
        for f in as_completed(futures):
            f.result()

    print(f"\n[FINISH] Scrape Complete. Check '{MASTER_JSON_FILE}' for results.")

if __name__ == "__main__":
    main()
