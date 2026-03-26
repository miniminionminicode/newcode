# -*- coding: utf-8 -*-

import os
import time
import requests
import json
import sys
from datetime import datetime, timezone
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

SECURE_PATH = os.getenv("SECURE_PATH")

OUTPUT_FILE = "newfile.json"

# ────────────────────────────────────────────────
# RETRY CONFIG
# ────────────────────────────────────────────────

MAX_RETRIES      = 20
RETRY_BASE_DELAY = 5
RETRY_MAX_DELAY  = 60
RATE_LIMIT_PAUSE = 10

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
SKIPPED    = []

# ────────────────────────────────────────────────
# MERGE HELPERS
# ────────────────────────────────────────────────

def better(new_val, old_val):
    """Returns True if new_val is a real improvement over old_val."""
    if new_val is None or new_val == "" or new_val == "error":
        return False
    if old_val is None or old_val == "" or old_val == "error":
        return True
    return False  # both have values — keep old, don't overwrite


def merge_item(old_item, new_item):
    """
    Merge a new video/pdf item into the existing one.
    Only update fields where new value is better than old.
    Never remove or null-out existing good data.
    """
    merged = dict(old_item)  # start from old as base

    for field in ["title", "m3u8", "youtube", "pdf", "thumbnail", "timestamp"]:
        new_val = new_item.get(field)
        old_val = old_item.get(field)
        if better(new_val, old_val):
            merged[field] = new_val
            print(f"    [MERGE] '{field}' updated: {old_val} → {new_val}")

    # type: only update if old was error or null
    if old_item.get("type") in (None, "", "error") and new_item.get("type") not in (None, "", "error"):
        merged["type"] = new_item["type"]

    # remove error flag if item now resolved successfully
    if new_item.get("type") not in (None, "", "error") and "error" in merged:
        del merged["error"]

    return merged


def merge_items(old_items, new_items):
    """
    Merge two lists of content items (videos/pdfs).
    - Existing items: smart field-level merge
    - Brand new items (not in old): add them
    - Items only in old (API didn't return them): keep as-is
    """
    old_map = {item["id"]: item for item in old_items}
    new_map = {item["id"]: item for item in new_items}

    result = []

    # Process all old items — merge with new if available, else keep old
    for item_id, old_item in old_map.items():
        if item_id in new_map:
            new_item = new_map[item_id]
            # Only merge if new item is not an error
            if new_item.get("type") == "error":
                print(f"    [KEEP] Item {item_id} — new fetch failed, keeping old data")
                result.append(old_item)
            else:
                result.append(merge_item(old_item, new_item))
        else:
            # Not returned by API this time — keep old untouched
            print(f"    [KEEP] Item {item_id} — not in new fetch, keeping old data")
            result.append(old_item)

    # Add brand new items that didn't exist before
    for item_id, new_item in new_map.items():
        if item_id not in old_map:
            print(f"    [NEW] Item {item_id} — '{new_item.get('title')}' added")
            result.append(new_item)

    return result


def merge_subjects(old_subjects, new_subjects):
    """
    Merge subject lists.
    - Existing subjects: merge content items
    - New subjects: add them
    - Old subjects not returned by API: keep as-is
    """
    old_map = {s["subject_id"]: s for s in old_subjects}
    new_map = {s["subject_id"]: s for s in new_subjects}

    result = []

    for sub_id, old_sub in old_map.items():
        if sub_id in new_map:
            new_sub = new_map[sub_id]
            # Merge content items
            merged_content = merge_items(
                old_sub.get("content", []),
                new_sub.get("content", [])
            )
            result.append({
                "subject_id":   sub_id,
                "subject_name": new_sub.get("subject_name") or old_sub.get("subject_name"),
                "content":      merged_content,
            })
            print(f"  [SUBJECT MERGED] {old_sub.get('subject_name')} — {len(merged_content)} items")
        else:
            # Subject not returned this run — keep old entirely
            print(f"  [SUBJECT KEEP] {old_sub.get('subject_name')} — not in new fetch, keeping")
            result.append(old_sub)

    for sub_id, new_sub in new_map.items():
        if sub_id not in old_map:
            print(f"  [SUBJECT NEW] {new_sub.get('subject_name')} — added fresh")
            result.append(new_sub)

    return result


def merge_announcements(old_list, new_list):
    """Merge announcements — add new ones, keep all old ones, no duplicates."""
    if not new_list:
        return old_list  # new fetch failed or empty — keep old
    old_ids = {a.get("id") for a in old_list if a.get("id")}
    merged = list(old_list)
    for ann in new_list:
        if ann.get("id") not in old_ids:
            merged.append(ann)
            print(f"  [ANNOUNCEMENT NEW] id={ann.get('id')}")
    return merged


# ────────────────────────────────────────────────
# JSON LOAD / SAVE
# ────────────────────────────────────────────────

def load_json():
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return []


def save_json(data):
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def save_course(course_data):
    """Smart save: merge with existing data, never overwrite good data with empty/error."""
    all_data = load_json()
    cid      = course_data.get("course_id")

    existing = next((c for c in all_data if c.get("course_id") == cid), None)

    if existing:
        print(f"\n[SAVE] Merging course: {course_data.get('course_name')}")

        merged = dict(existing)  # base = existing

        # Top-level scalar fields — only update if new value is better
        for field in ["course_name", "image", "image_large", "start_at"]:
            if better(course_data.get(field), existing.get(field)):
                merged[field] = course_data[field]

        # Subjects — smart merge
        if course_data.get("subjects"):
            merged["subjects"] = merge_subjects(
                existing.get("subjects", []),
                course_data.get("subjects", [])
            )
        else:
            print(f"  [KEEP] Subjects — new fetch returned nothing, keeping old")

        # Announcements — additive merge
        merged["announcements"] = merge_announcements(
            existing.get("announcements", []),
            course_data.get("announcements", [])
        )

        merged["fetched_at"] = course_data["fetched_at"]

        all_data = [c for c in all_data if c.get("course_id") != cid]
        all_data.append(merged)

    else:
        print(f"\n[SAVE] New course: {course_data.get('course_name')}")
        all_data.append(course_data)

    save_json(all_data)
    print(f"[FILE] Saved -> {course_data.get('course_name')}")


# ────────────────────────────────────────────────
# TOKEN FETCHER
# ────────────────────────────────────────────────

def fetch_security_token(path):
    sunny_url = f"{API_BASE}{SECURE_PATH}?path={path}&method=GET"
    try:
        r = session.get(sunny_url, headers=HEADERS, timeout=10)
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
        cb = r.json().get("short_url")
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
# SAFE API CALL
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

            if r.status_code == 200:
                return r.json(), True

            elif r.status_code == 429:
                wait = min(RETRY_BASE_DELAY * attempt, RETRY_MAX_DELAY)
                total_wait = wait + RATE_LIMIT_PAUSE
                print(f"{tag} ⚠️  429 Rate-limited. Pausing {total_wait}s before retry {attempt}/{MAX_RETRIES} ...")
                time.sleep(total_wait)

            elif r.status_code == 401:
                print(f"{tag} 401 Unauthorized -> re-authenticating ...")
                if verify_session():
                    continue
                else:
                    break

            elif r.status_code >= 500:
                wait = min(RETRY_BASE_DELAY * attempt, RETRY_MAX_DELAY)
                print(f"{tag} Server error {r.status_code}. Waiting {wait}s ...")
                time.sleep(wait)

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

    # Load whatever we already have for this course
    existing_data = load_json()
    existing      = next((c for c in existing_data if c.get("course_id") == str(cid)), None)

    out = {
        "course_id":     str(cid),
        "course_name":   cname,
        "image":         course.get("image"),
        "image_large":   course.get("image_large"),
        "start_at":      course.get("start_at"),
        "subjects":      [],        # will be filled or left empty (merge handles preservation)
        "announcements": [],
        "fetched_at":    datetime.now(timezone.utc).isoformat(),
    }

    # ── 1. Subjects ──────────────────────────────────────────────
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
                # API failed — pass empty content, merge will preserve old
                print(f"[SUBJECT] ⚠️  Lesson fetch failed — old data will be preserved by merge")
                out["subjects"].append({
                    "subject_id":   str(sub_id),
                    "subject_name": sub_name,
                    "content":      [],   # merge will keep old content
                })
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
                    # Failed — add error placeholder; merge will keep old if exists
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

    else:
        # Classroom API failed entirely — out["subjects"] stays []
        # merge_subjects in save_course will see empty new subjects
        # and preserve ALL old subjects untouched
        print(f"[COURSE] ⚠️  Classroom fetch failed — all old subjects will be preserved by merge")

    # ── 2. Announcements ─────────────────────────────────────────
    updates_data, u_ok = safe_api_call(f"/api/updates/{cid}", "updates")
    if u_ok:
        out["announcements"] = updates_data if isinstance(updates_data, list) else []
    else:
        print(f"[COURSE] ⚠️  Announcements fetch failed — old announcements will be preserved by merge")
        # leave out["announcements"] = [] — merge_announcements keeps old when new is empty

    # ── Smart save / merge ───────────────────────────────────────
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

    total = len(all_batches)
    print(f"[INIT] Total courses to process: {total}")

    # ── NO os.remove(OUTPUT_FILE) — we never wipe existing data ──

    for i, course in enumerate(all_batches):
        fetch_course_details(course, i + 1, total)

    runtime = round(time.time() - START_TIME, 2)

    print("\n========== SUMMARY ==========")
    print(f"Total API Calls : {API_CALLS}")
    print(f"Courses Scraped : {total}")
    print(f"Skipped Items   : {len(SKIPPED)}")
    print(f"Runtime         : {runtime} seconds")

    if SKIPPED:
        print("\n[SKIPPED PATHS — failed after all retries]")
        for p in SKIPPED:
            print(f"  - {p}")

    print("=============================")


if __name__ == "__main__":
    main()
