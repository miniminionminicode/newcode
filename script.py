# -*- coding: utf-8 -*-

import os
import time
import requests
import json
import re
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

sys.stdout.reconfigure(encoding="utf-8")

# ────────────────────────────────────────────────
# ENV CONFIG
# ────────────────────────────────────────────────

BASE_URL   = os.getenv("URL_BASE")     
API_BASE   = f"{BASE_URL}/api"
BATCHES_URL = os.getenv("DATA_URL")    

AUTH_KEY   = os.getenv("AUTH_KEY")       
AUTH_VAL   = os.getenv("AUTH_VAL")     

KEYWORDS   = os.getenv("KEYWORDS")
THREADS    = int(os.getenv("THREADS", 5))

OUTPUT_FILE = "newfile.json"

SEARCH_PATTERN = re.compile(KEYWORDS, re.IGNORECASE)

# ────────────────────────────────────────────────
# HEADERS
# ────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer":    f"{BASE_URL}/verify",
    "Origin":     BASE_URL,
    AUTH_KEY:     AUTH_VAL,          
}

session = requests.Session()

# ────────────────────────────────────────────────
# GLOBAL METRICS
# ────────────────────────────────────────────────

START_TIME = time.time()
API_CALLS  = 0
API_LOCK   = Lock()

# ────────────────────────────────────────────────
# FILE SAVE
# ────────────────────────────────────────────────

def save_course(course_data):
    data = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except:
            data = []

    cid  = course_data.get("course_id")
    data = [c for c in data if c.get("course_id") != cid]
    data.append(course_data)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"[FILE] Course saved -> {course_data.get('course_name')}")

# ────────────────────────────────────────────────
# TOKEN FETCHER  ← fixed: always uses /sunny-keys
# ────────────────────────────────────────────────

def fetch_security_token(path):
    # Hardcoded endpoint — matches the original working code exactly
    sunny_url = f"{API_BASE}{SECURE_PATH}?path={path}&method=GET"
    print(f"[TOKEN] Fetching token for {path}")
    try:
        r = session.get(sunny_url, headers=HEADERS, timeout=10)
        print(f"[TOKEN] Status {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"[TOKEN ERROR] {e}")
        return False

# ────────────────────────────────────────────────
# AUTH HANDSHAKE
# ────────────────────────────────────────────────

def verify_session():
    print("[AUTH] Starting verification handshake")
    try:
        r = session.post(
            f"{BASE_URL}/generate_link",
            headers=HEADERS,
            json={}
        )
        print(f"[AUTH] generate_link status: {r.status_code}")

        cb = r.json().get("callback_url")
        if cb:
            print("[AUTH] Opening callback")
            session.get(cb, headers=HEADERS)

            status = session.get(
                f"{BASE_URL}/status",
                headers=HEADERS
            ).json()
            print(f"[AUTH] Verification status -> {status}")

            if status.get("verified"):
                print("[AUTH] Session verified successfully")
                return True
    except Exception as e:
        print(f"[AUTH ERROR] {e}")
    return False

# ────────────────────────────────────────────────
# SAFE API CALL
# ────────────────────────────────────────────────

def safe_api_call(path):
    global API_CALLS

    with API_LOCK:
        API_CALLS += 1
        current_call = API_CALLS

    print(f"\n[API-{current_call}] Request -> {path}")

    fetch_security_token(path)
    time.sleep(0.3)

    try:
        r = session.get(
            f"{BASE_URL}{path}",
            headers=HEADERS,
            timeout=20
        )
        print(f"[API-{current_call}] Status {r.status_code}")

        if r.status_code == 401:
            print("[API] 401 Unauthorized -> re-authenticating")
            if verify_session():
                fetch_security_token(path)
                r = session.get(
                    f"{BASE_URL}{path}",
                    headers=HEADERS,
                    timeout=20
                )

        if r.status_code == 200:
            print(f"[API-{current_call}] Success")
            return r.json(), True

    except Exception as e:
        print(f"[API-{current_call} ERROR] {e}")

    return None, False

# ────────────────────────────────────────────────
# COURSE PROCESSOR
# ────────────────────────────────────────────────

def fetch_course_details(course, rank, total):
    cid   = course.get("id")
    cname = course.get("title") or "Unknown"

    print(f"\n========== COURSE {rank}/{total} ==========")
    print(f"[COURSE] {cname}")
    print(f"[COURSE] ID: {cid}")

    out = {
        "course_id":   str(cid),
        "course_name": cname,
        "image":       course.get("image"),
        "image_large": course.get("image_large"),
        "start_at":    course.get("start_at"),
        "subjects":    [],
        "announcements": [],
        "fetched_at":  datetime.now(timezone.utc).isoformat(),
    }

    # 1. Subjects
    classroom_data, ok = safe_api_call(f"/api/classroom/{cid}")
    if ok:
        subjects = classroom_data.get("classroom", [])
        print(f"[COURSE] Found {len(subjects)} subjects")

        for sub in subjects:
            sub_id   = sub.get("id")
            sub_name = sub.get("name")
            print(f"\n[SUBJECT] {sub_name} (ID: {sub_id})")

            lesson_data, l_ok = safe_api_call(f"/api/lesson/{sub_id}")
            if not l_ok:
                print("[SUBJECT] Failed to load lessons")
                continue

            videos = lesson_data.get("videos") or []
            notes  = lesson_data.get("notes")  or []
            print(f"[CONTENT] Videos: {len(videos)} | Notes: {len(notes)}")

            resolved_list = []

            for item in videos + notes:
                item_id = item.get("id")
                print(f"[ITEM] Resolving -> {item.get('name')} (ID {item_id})")

                details, d_ok = safe_api_call(f"/api/video/{item_id}")
                if d_ok:
                    vd    = details if isinstance(details, dict) else {}
                    v_url = vd.get("video_url", "")

                    final_pdf  = vd.get("pdf_url")
                    final_m3u8 = None

                    if v_url and v_url.lower().endswith(".pdf"):
                        final_pdf = v_url
                    else:
                        final_m3u8 = v_url

                    resolved_list.append({
                        "id":        str(item_id),
                        "title":     item.get("name"),
                        "m3u8":      final_m3u8,
                        "youtube":   vd.get("hd_video_url"),
                        "pdf":       final_pdf or (
                                         vd.get("pdfs")[0].get("url")
                                         if vd.get("pdfs") else None
                                     ),
                        "thumbnail": vd.get("thumbnail_url") or item.get("thumbnail_url"),
                        "timestamp": vd.get("created_at")    or item.get("published_at"),
                        "type":      "pdf" if final_pdf else "video",
                    })

            print(f"[SUBJECT] Resolved {len(resolved_list)} items")
            out["subjects"].append({
                "subject_id":   str(sub_id),
                "subject_name": sub_name,
                "content":      resolved_list,
            })

    # 2. Announcements
    updates_data, u_ok = safe_api_call(f"/api/updates/{cid}")
    if u_ok:
        print(f"[ANNOUNCEMENTS] Found {len(updates_data)} updates")
        out["announcements"] = updates_data if isinstance(updates_data, list) else []

    save_course(out)
    print(f"[COURSE DONE] {cname}")
    return out

# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────

def main():
    print("========== SCRAPER START ==========")

    if not verify_session():
        print("[ERROR] Authentication failed")
        return

    print("\n[INIT] Fetching batch list")
    try:
        all_batches = session.get(BATCHES_URL, headers=HEADERS).json()
        print(f"[INIT] Total batches available: {len(all_batches)}")
    except Exception as e:
        print(f"[ERROR] Batch fetch failed: {e}")
        return

    filtered = [
        b for b in all_batches
        if SEARCH_PATTERN.search(b.get("title", ""))
    ]
    print(f"[INIT] Matched courses: {len(filtered)}")

    if os.path.exists(OUTPUT_FILE):
        os.remove(OUTPUT_FILE)

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = [
            executor.submit(fetch_course_details, c, i + 1, len(filtered))
            for i, c in enumerate(filtered)
        ]
        for f in as_completed(futures):
            f.result()

    runtime = round(time.time() - START_TIME, 2)
    print("\n========== SUMMARY ==========")
    print(f"Total API Calls : {API_CALLS}")
    print(f"Courses Scraped : {len(filtered)}")
    print(f"Runtime         : {runtime} seconds")
    print("=============================")

if __name__ == "__main__":
    main()
