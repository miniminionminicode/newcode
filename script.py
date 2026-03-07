# -*- coding: utf-8 -*-
import os, time, requests, json, re, sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding="utf-8")

# --- GENERIC ENV LOADERS ---
URL_BASE = os.getenv("URL_BASE")
DATA_URL = os.getenv("DATA_URL")
OUT_FILE = "newfile.json"
SECURE_PATH = os.getenv("SECURE_PATH")
AUTH_KEY = os.getenv("AUTH_KEY")
AUTH_VAL = os.getenv("AUTH_VAL")
KW_INPUT = os.getenv("KEYWORDS", "")
KW_LIST = [k.strip() for k in KW_INPUT.split(",")]
WORKERS = int(os.getenv("THREADS", "25"))

def get_pattern(kw):
    parts = kw.split(maxsplit=1)
    n = re.escape(parts[0])
    s = re.escape(parts[1]) if len(parts) > 1 else ""
    return re.compile(rf"{n}(?:st|nd|rd|th)?\s*{s}", re.IGNORECASE)

PATTERNS = [get_pattern(k) for k in KW_LIST if k]
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": f"{URL_BASE}/verify",
    "Origin": URL_BASE,
    AUTH_KEY: AUTH_VAL,
}

session = requests.Session()

def load_master_data():
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except: return []
    return []

def save_master_data(data):
    try:
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[CRITICAL] Failed to write to disk: {e}")

def start_session():
    for _ in range(3):
        try:
            req_link = session.post(f"{URL_BASE}/generate_link", headers=COMMON_HEADERS, json={}, timeout=15)
            cb = req_link.json().get("callback_url")
            if cb:
                session.get(cb, headers=COMMON_HEADERS)
                res = session.get(f"{URL_BASE}/status", headers=COMMON_HEADERS).json()
                if res.get("verified"): return True
        except: time.sleep(2)
    return False

def api_request(path, retries=3):
    for attempt in range(retries):
        try:
            session.get(f"{URL_BASE}{SECURE_PATH}{path}&method=GET", headers=COMMON_HEADERS, timeout=10)
            r = session.get(f"{URL_BASE}{path}", headers=COMMON_HEADERS, timeout=25)
            if r.status_code == 200: return r.json(), True
            if r.status_code == 401: start_session()
        except:
            if attempt < retries - 1: time.sleep(1)
    return None, False

def process_item(course_item, idx, total):
    iid = str(course_item.get("id"))
    iname = course_item.get("title") or "Unknown"
    print(f">>> [{idx}/{total}] ⚡ SYNCING: {iname}")

    # Load existing state to merge
    master_data = load_master_data()
    # Find existing course entry or create new
    course_entry = next((c for c in master_data if c["course_id"] == iid), None)
    
    if not course_entry:
        course_entry = {
            "course_id": iid,
            "course_name": iname,
            "image": course_item.get("image"),
            "image_large": course_item.get("image_large"),
            "start_at": course_item.get("start_at"),
            "subjects": [],
            "announcements": [],
            "synced_at": datetime.now(timezone.utc).isoformat()
        }
        master_data.append(course_entry)

    # 1. Subjects Logic
    class_res, ok = api_request(f"/api/classroom/{iid}")
    if ok and class_res:
        for group in class_res.get("classroom", []):
            sid = str(group.get("id"))
            sname = group.get("name")
            
            # Find or create subject
            subject_entry = next((s for s in course_entry["subjects"] if s["subject_id"] == sid), None)
            if not subject_entry:
                subject_entry = {"subject_id": sid, "subject_name": sname, "content": []}
                course_entry["subjects"].append(subject_entry)

            lesson_res, l_ok = api_request(f"/api/lesson/{sid}")
            if l_ok and lesson_res:
                raw_lectures = (lesson_res.get("videos") or []) + (lesson_res.get("notes") or [])
                for r_item in raw_lectures:
                    lid = str(r_item.get("id"))
                    # Skip if lecture already exists
                    if any(lec["id"] == lid for lec in subject_entry["content"]):
                        continue
                    
                    # Fetch details for new lecture
                    detail, d_ok = api_request(f"/api/video/{lid}")
                    if d_ok and detail:
                        uri = detail.get("video_url") or ""
                        is_doc = uri.lower().endswith(".pdf") if uri else False
                        subject_entry["content"].append({
                            "id": lid,
                            "title": r_item.get("name"),
                            "m3u8": None if is_doc else uri,
                            "pdf": uri if is_doc else (detail.get("pdf_url") or (detail.get("pdfs")[0].get("url") if detail.get("pdfs") else None)),
                            "thumbnail": detail.get("thumbnail_url") or r_item.get("thumbnail_url"),
                            "timestamp": detail.get("created_at") or r_item.get("published_at"),
                            "type": "pdf" if is_doc else "video"
                        })

    # 2. Announcements Logic
    announce_res, a_ok = api_request(f"/api/updates/{iid}")
    if a_ok and isinstance(announce_res, list):
        for ann in announce_res:
            aid = str(ann.get("id"))
            if not any(str(existing_ann.get("id")) == aid for existing_ann in course_entry["announcements"]):
                course_entry["announcements"].append(ann)

    course_entry["synced_at"] = datetime.now(timezone.utc).isoformat()
    save_master_data(master_data)
    return True

def main():
    if not start_session(): return
    try:
        payload = session.get(DATA_URL, headers=COMMON_HEADERS).json()
    except: return

    matches = [b for b in payload if any(p.search(b.get('title', '')) for p in PATTERNS)]
    print(f"[LOG] Found {len(matches)} courses. Workers: {WORKERS}")

    with ThreadPoolExecutor(max_workers=WORKERS) as engine:
        tasks = [engine.submit(process_item, c, i+1, len(matches)) for i, c in enumerate(matches)]
        for t in as_completed(tasks):
            try: t.result()
            except: pass

    print(f"\n[FINISH] Sync Complete. Incremental updates saved to '{OUT_FILE}'")

if __name__ == "__main__":
    main()
