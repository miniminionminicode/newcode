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

def get_pattern(kw):
    parts = kw.split(maxsplit=1)
    n = re.escape(parts[0])
    s = re.escape(parts[1]) if len(parts) > 1 else ""
    return re.compile(rf"{n}(?:st|nd|rd|th)?\s*{s}", re.IGNORECASE)

PATTERNS = [get_pattern(k) for k in KW_LIST if k]
WORKERS = int(os.getenv("THREADS", "25"))

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": f"{URL_BASE}/verify",
    "Origin": URL_BASE,
    AUTH_KEY: AUTH_VAL,
}

session = requests.Session()

def save_data(entry):
    store = []
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, "r", encoding="utf-8") as f:
                store = json.load(f)
        except: store = []
    
    uid = entry.get("course_id")
    store = [item for item in store if item.get("course_id") != uid]
    store.append(entry)
    
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)

def start_session():
    print("[SYSTEM] 🔐 Initializing Session...")
    try:
        req_link = session.post(f"{URL_BASE}/generate_link", headers=COMMON_HEADERS, json={}, timeout=15)
        cb = req_link.json().get("callback_url")
        if cb:
            session.get(cb, headers=COMMON_HEADERS)
            res = session.get(f"{URL_BASE}/status", headers=COMMON_HEADERS).json()
            return res.get("verified")
    except Exception as e:
        print(f"[SYSTEM] ❌ Auth Error: {e}")
    return False

def api_request(path, retries=3):
    # Handshake
    for attempt in range(retries):
        try:
            session.get(f"{URL_BASE}{SECURE_PATH}{path}&method=GET", headers=COMMON_HEADERS, timeout=10)
            r = session.get(f"{URL_BASE}{path}", headers=COMMON_HEADERS, timeout=25)
            if r.status_code == 200:
                return r.json(), True
            if r.status_code == 401: # Try re-verifying if unauthorized
                start_session()
        except Exception:
            if attempt < retries - 1:
                time.sleep(1) # Wait before retry
                continue
    return None, False

def process_item(item, idx, total):
    iid = item.get("id")
    iname = item.get("title") or "Unknown"
    print(f">>> [{idx}/{total}] ⚡ SYNCING: {iname}")
    
    result = {
        "course_id": str(iid),
        "course_name": iname,
        "image": item.get("image"),
        "image_large": item.get("image_large"),
        "start_at": item.get("start_at"),
        "subjects": [],
        "announcements": [],
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }

    class_res, ok = api_request(f"/api/classroom/{iid}")
    if ok:
        for group in class_res.get("classroom", []):
            gid, gname = group.get("id"), group.get("name")
            lesson_res, l_ok = api_request(f"/api/lesson/{gid}")
            if not l_ok: continue

            raw = (lesson_res.get("videos") or []) + (lesson_res.get("notes") or [])
            resolved = []
            for r_item in raw:
                detail, d_ok = api_request(f"/api/video/{r_item.get('id')}")
                if d_ok:
                    detail = detail if isinstance(detail, dict) else {}
                    uri = detail.get("video_url") or "" # Safety: fallback to empty string
                    
                    # Safety check: avoid AttributeError if uri is None
                    is_doc = uri.lower().endswith(".pdf") if uri else False
                    
                    resolved.append({
                        "id": str(r_item.get("id")),
                        "title": r_item.get("name"),
                        "m3u8": None if is_doc else uri,
                        "pdf": uri if is_doc else (detail.get("pdf_url") or (detail.get("pdfs")[0].get("url") if detail.get("pdfs") else None)),
                        "thumbnail": detail.get("thumbnail_url") or r_item.get("thumbnail_url"),
                        "timestamp": detail.get("created_at") or r_item.get("published_at"),
                        "type": "pdf" if is_doc else "video"
                    })
            result["subjects"].append({"subject_id": str(gid), "subject_name": gname, "content": resolved})

    save_data(result)
    return True

def main():
    if not start_session(): 
        print("❌ Handshake Failed.")
        return
    try:
        payload = session.get(DATA_URL, headers=COMMON_HEADERS).json()
    except: return

    matches = [b for b in payload if any(p.search(b.get('title', '')) for p in PATTERNS)]
    print(f"[LOG] Found {len(matches)} matches. Using {WORKERS} threads.")

    if os.path.exists(OUT_FILE): os.remove(OUT_FILE)

    with ThreadPoolExecutor(max_workers=WORKERS) as engine:
        tasks = [engine.submit(process_item, c, i+1, len(matches)) for i, c in enumerate(matches)]
        for t in as_completed(tasks):
            try:
                t.result()
            except Exception as e:
                print(f"[CRITICAL] Task failed: {e}")

if __name__ == "__main__":
    main()
