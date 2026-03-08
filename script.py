# -*- coding: utf-8 -*-

import os
import time
import requests
import json
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding="utf-8")

# ────────────────────────────────────────────────
# ENV & CONFIG
# ────────────────────────────────────────────────

URL_BASE = os.getenv("URL_BASE")
DATA_URL = os.getenv("DATA_URL")  # JSON course list
SECURE_PATH = os.getenv("SECURE_PATH")
AUTH_KEY = os.getenv("AUTH_KEY")
AUTH_VAL = os.getenv("AUTH_VAL")

THREADS = int(os.getenv("THREADS", "5"))
OUT_FILE = "newfile.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": f"{URL_BASE}/verify",
    "Origin": URL_BASE,
    AUTH_KEY: AUTH_VAL
}

session = requests.Session()

# ────────────────────────────────────────────────
# JSON HELPER
# ────────────────────────────────────────────────

def load_existing():
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

def merge_course(old_courses, new_course):
    """Merge new course into existing JSON without deleting anything."""
    cid = new_course["course_id"]
    course_map = {c["course_id"]: c for c in old_courses}

    if cid not in course_map:
        old_courses.append(new_course)
        return old_courses

    existing_course = course_map[cid]

    # Merge subjects
    subj_map = {s["subject_id"]: s for s in existing_course.get("subjects", [])}
    for sub in new_course.get("subjects", []):
        sid = sub["subject_id"]
        if sid not in subj_map:
            existing_course.setdefault("subjects", []).append(sub)
            continue
        # Merge content
        existing_items = {i["id"]: i for i in subj_map[sid].get("content", [])}
        for item in sub.get("content", []):
            if item["id"] not in existing_items:
                subj_map[sid].setdefault("content", []).append(item)

    # Merge announcements
    old_ann = {a.get("id"): a for a in existing_course.get("announcements", [])}
    for ann in new_course.get("announcements", []):
        if ann.get("id") not in old_ann:
            existing_course.setdefault("announcements", []).append(ann)

    return old_courses

def save_course(course_data):
    old = load_existing()
    merged = merge_course(old, course_data)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

# ────────────────────────────────────────────────
# AUTH / SUNNY-KEY
# ────────────────────────────────────────────────

def get_sunny_keys(path):
    try:
        url = f"{URL_BASE}{SECURE_PATH}{path}&method=GET"
        r = session.get(url, headers=HEADERS, timeout=10)
        time.sleep(0.3)  # mimic browser delay
        return r.status_code == 200
    except:
        return False

def verify_session():
    print("[AUTH] Starting Verification Handshake...")
    try:
        r_link = session.post(f"{URL_BASE}/generate_link", headers=HEADERS, json={})
        cb_url = r_link.json().get("callback_url")
        if cb_url:
            session.get(cb_url, headers=HEADERS)
            status = session.get(f"{URL_BASE}/status", headers=HEADERS).json()
            if status.get("verified"):
                print("[AUTH] ✨ Session Verified Successfully")
                return True
    except Exception as e:
        print(f"[AUTH] ❌ Handshake Error: {e}")
    return False

def safe_api_call(path, retries=3):
    for attempt in range(retries):
        get_sunny_keys(path)
        try:
            r = session.get(f"{URL_BASE}{path}", headers=HEADERS, timeout=20)
            if r.status_code == 401 and verify_session():
                get_sunny_keys(path)
                r = session.get(f"{URL_BASE}{path}", headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.json(), True
        except Exception as e:
            print(f"[WARN] API call failed {path} attempt {attempt+1}: {e}")
        time.sleep(0.5)
    return None, False

# ────────────────────────────────────────────────
# COURSE SCRAPER
# ────────────────────────────────────────────────

def fetch_course(course):
    cid = course.get("id")
    cname = course.get("title") or "Unknown"
    image = course.get("image_thumb")
    image_large = course.get("image_large")
    start_at = course.get("start_at")

    print(f"\n>>> STARTING COURSE {cid}: {cname}")

    out = {
        "course_id": str(cid),
        "course_name": cname,
        "image": image,
        "image_large": image_large,
        "start_at": start_at,
        "subjects": [],
        "announcements": [],
        "fetched_at": datetime.now(timezone.utc).isoformat()
    }

    # Subjects
    classroom, ok = safe_api_call(f"/api/classroom/{cid}")
    if ok:
        subjects = classroom.get("classroom", [])
        for sub in subjects:
            sub_id = sub.get("id")
            sub_name = sub.get("name")
            print(f"  └─ Subject: {sub_name}")

            lesson_data, l_ok = safe_api_call(f"/api/lesson/{sub_id}")
            if not l_ok:
                continue

            raw_items = (lesson_data.get("videos") or []) + (lesson_data.get("notes") or [])
            resolved = []

            for item in raw_items:
                item_id = item.get("id")
                details, d_ok = safe_api_call(f"/api/video/{item_id}")
                if not d_ok:
                    continue
                vd = details if isinstance(details, dict) else {}
                v_url = vd.get("video_url", "")
                final_pdf = vd.get("pdf_url")
                final_m3u8 = None
                if v_url and v_url.lower().endswith(".pdf"):
                    final_pdf = v_url
                else:
                    final_m3u8 = v_url

                resolved.append({
                    "id": str(item_id),
                    "title": item.get("name"),
                    "m3u8": final_m3u8,
                    "youtube": vd.get("hd_video_url"),
                    "pdf": final_pdf or (vd.get("pdfs")[0]["url"] if vd.get("pdfs") else None),
                    "thumbnail": vd.get("thumbnail_url") or item.get("thumbnail_url"),
                    "timestamp": vd.get("created_at") or item.get("published_at"),
                    "type": "pdf" if final_pdf else "video"
                })

            out["subjects"].append({
                "subject_id": str(sub_id),
                "subject_name": sub_name,
                "content": resolved
            })

    # Announcements
    updates, u_ok = safe_api_call(f"/api/updates/{cid}")
    if u_ok:
        out["announcements"] = updates if isinstance(updates, list) else []

    save_course(out)
    print(f">>> [LOG] ✅ Saved course: {cname}")
    return out

# ────────────────────────────────────────────────
# MAIN EXECUTION
# ────────────────────────────────────────────────

def main():
    if not verify_session():
        return

    print("[INIT] Fetching course list from DATA_URL")
    try:
        all_courses = session.get(DATA_URL, headers=HEADERS).json()
    except Exception as e:
        print(f"[ERROR] Failed to fetch DATA_URL: {e}")
        return

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = [executor.submit(fetch_course, c) for c in all_courses]
        for f in as_completed(futures):
            f.result()

    print(f"\n[FINISH] All courses processed. Check '{OUT_FILE}' for results.")

if __name__ == "__main__":
    main()
