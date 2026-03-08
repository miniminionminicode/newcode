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

BASE_URL    = os.getenv("URL_BASE")
API_BASE    = f"{BASE_URL}/api"
BATCHES_URL = os.getenv("DATA_URL")

AUTH_KEY    = os.getenv("AUTH_KEY")
AUTH_VAL    = os.getenv("AUTH_VAL")

KEYWORDS    = os.getenv("KEYWORDS")
THREADS     = int(os.getenv("THREADS", 5))
SECURE_PATH = os.getenv("SECURE_PATH")

OUTPUT_FILE = "newfile.json"

SEARCH_PATTERN = re.compile(KEYWORDS, re.IGNORECASE)

# ────────────────────────────────────────────────
# RETRY CONFIG
# ────────────────────────────────────────────────

MAX_RETRIES      = 20    # max retries per API call
RETRY_BASE_DELAY = 5     # seconds to wait on first 429
RETRY_MAX_DELAY  = 60    # cap backoff at 60 seconds
RATE_LIMIT_PAUSE = 10    # extra pause added on top of backoff for 429

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
SKIP_LOCK  = Lock()
SKIPPED    = []   # paths skipped after all retries exhausted

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
# TOKEN FETCHER
# ────────────────────────────────────────────────

def fetch_security_token(path):
    xyzurl = f"{API_BASE}{SECURE_PATH}?path={path}&method=GET"
    try:
        r = session.get(xyzurl, headers=HEADERS, timeout=10)
        print(f"[TOKEN] {path} -> {r.status_code}")
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
        r = session.post(f"{BASE_URL}/generate_link", headers=HEADERS, json={})
        print(f"[AUTH] generate_link status: {r.status_code}")

        cb = r.json().get("callback_url")
        if cb:
            session.get(cb, headers=HEADERS)
            status = session.get(f"{BASE_URL}/status", headers=HEADERS).json()
            print(f"[AUTH] Verification status -> {status}")
            if status.get("verified"):
                print("[AUTH] Session verified successfully")
                return True
    except Exception as e:
        print(f"[AUTH ERROR] {e}")
    return False

# ────────────────────────────────────────────────
# SAFE API CALL  — retry up to MAX_RETRIES times
# ────────────────────────────────────────────────

def safe_api_call(path, label=""):
    global API_CALLS

    with API_LOCK:
        API_CALLS += 1
        call_id = API_CALLS

    tag = f"[API-{call_id}]{f' ({label})' if label else ''}"
    print(f"\n{tag} -> {path}")

    for attempt in range(1, MAX_RETRIES + 1):

        fetch_security_token(path)
        time.sleep(0.3)

        try:
            r = session.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=20)
            print(f"{tag} Attempt {attempt}/{MAX_RETRIES} -> HTTP {r.status_code}")

            # ── 200 OK ───────────────────────────────────────────
            if r.status_code == 200:
                return r.json(), True

            # ── 429 Too Many Requests → exponential backoff ──────
            elif r.status_code == 429:
                wait = min(RETRY_BASE_DELAY * attempt, RETRY_MAX_DELAY)
                total_wait = wait + RATE_LIMIT_PAUSE
                print(f"{tag} ⚠️  429 Rate-limited. Pausing {total_wait}s before retry {attempt}/{MAX_RETRIES} ...")
                time.sleep(total_wait)

            # ── 401 Unauthorized → re-auth once then retry ───────
            elif r.status_code == 401:
                print(f"{tag} 401 Unauthorized -> re-authenticating ...")
                if verify_session():
                    continue   # retry immediately after re-auth
                else:
                    break      # can't recover

            # ── 5xx Server Error → backoff and retry ─────────────
            elif r.status_code >= 500:
                wait = min(RETRY_BASE_DELAY * attempt, RETRY_MAX_DELAY)
                print(f"{tag} Server error {r.status_code}. Waiting {wait}s ...")
                time.sleep(wait)

            # ── 403 / 404 / other → unrecoverable, skip now ──────
            else:
                print(f"{tag} Unrecoverable status {r.status_code}. Skipping.")
                break

        except requests.exceptions.Timeout:
            wait = min(RETRY_BASE_DELAY * attempt, RETRY_MAX_DELAY)
            print(f"{tag} Timeout on attempt {attempt}. Waiting {wait}s ...")
            time.sleep(wait)

        except Exception as e:
            wait = min(RETRY_BASE_DELAY * attempt, RETRY_MAX_DELAY)
            print(f"{tag} Exception: {e}. Waiting {wait}s ...")
            time.sleep(wait)

    # ── All retries exhausted ────────────────────────────────────
    print(f"{tag} ❌ SKIPPED after {MAX_RETRIES} retries -> {path}")
    with SKIP_LOCK:
        SKIPPED.append(path)
    return None, False

# ────────────────────────────────────────────────
# COURSE PROCESSOR
# ────────────────────────────────────────────────

def fetch_course_details(course, rank, total):
    cid   = course.get("id")
    cname = course.get("title") or "Unknown"

    print(f"\n========== COURSE {rank}/{total} ==========")
    print(f"[COURSE] {cname}  (ID: {cid})")

    out = {
        "course_id":     str(cid),
        "course_name":   cname,
        "image":         course.get("image"),
        "image_large":   course.get("image_large"),
        "start_at":      course.get("start_at"),
        "subjects":      [],
        "announcements": [],
        "fetched_at":    datetime.now(timezone.utc).isoformat(),
    }

    # 1. Subjects
    classroom_data, ok = safe_api_call(f"/api/classroom/{cid}", "classroom")
    if ok:
        subjects = classroom_data.get("classroom", [])
        print(f"[COURSE] Found {len(subjects)} subjects")

        for sub in subjects:
            sub_id   = sub.get("id")
            sub_name = sub.get("name")
            print(f"\n[SUBJECT] {sub_name} (ID: {sub_id})")

            lesson_data, l_ok = safe_api_call(f"/api/lesson/{sub_id}", "lesson")
            if not l_ok:
                print("[SUBJECT] Failed to load lessons — skipping subject")
                continue

            videos = lesson_data.get("videos") or []
            notes  = lesson_data.get("notes")  or []
            print(f"[CONTENT] Videos: {len(videos)} | Notes: {len(notes)}")

            resolved_list = []

            for item in videos + notes:
                item_id   = item.get("id")
                item_name = item.get("name", "Unknown")
                print(f"[ITEM] Resolving -> {item_name} (ID {item_id})")

                details, d_ok = safe_api_call(f"/api/video/{item_id}", item_name[:40])

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
                        "title":     item_name,
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
                else:
                    # Placeholder so item is still present in output with error flag
                    resolved_list.append({
                        "id":        str(item_id),
                        "title":     item_name,
                        "m3u8":      None,
                        "youtube":   None,
                        "pdf":       None,
                        "thumbnail": item.get("thumbnail_url"),
                        "timestamp": item.get("published_at"),
                        "type":      "error",
                        "error":     "failed_after_retries",
                    })

            print(f"[SUBJECT] Resolved {len(resolved_list)} items")
            out["subjects"].append({
                "subject_id":   str(sub_id),
                "subject_name": sub_name,
                "content":      resolved_list,
            })

    # 2. Announcements
    updates_data, u_ok = safe_api_call(f"/api/updates/{cid}", "updates")
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
    print(f"Skipped Items   : {len(SKIPPED)}")
    print(f"Runtime         : {runtime} seconds")

    if SKIPPED:
        print("\n[SKIPPED PATHS — failed after all retries]")
        for p in SKIPPED:
            print(f"  - {p}")

    print("=============================")

if __name__ == "__main__":
    main()
