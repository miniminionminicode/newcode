# -*- coding: utf-8 -*-
import os
import json
import asyncio
import aiohttp
from datetime import datetime, timezone
# ────────────────────────────────────────────────
# ENV CONFIG
# ────────────────────────────────────────────────

URL_BASE = os.getenv("URL_BASE")
DATA_URL = os.getenv("DATA_URL")
SECURE_PATH = os.getenv("SECURE_PATH")

AUTH_KEY = os.getenv("AUTH_KEY")
AUTH_VAL = os.getenv("AUTH_VAL")

THREADS = int(os.getenv("THREADS", "40"))

OUT_FILE = "newfile.json"

TARGET_IDS = [848, 849, 391, 329]

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": f"{URL_BASE}/verify",
    "Origin": URL_BASE,
    AUTH_KEY: AUTH_VAL,
}

# concurrency limiter
SEM = asyncio.Semaphore(THREADS)


# ────────────────────────────────────────────────
# LOGGING
# ────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)


# ────────────────────────────────────────────────
# RETRY WRAPPER
# ────────────────────────────────────────────────

async def retry(coro, attempts=3):

    for i in range(attempts):

        try:
            return await coro()

        except Exception as e:
            log(f"Retry {i+1}/{attempts} failed → {e}")
            await asyncio.sleep(1)

    return None


# ────────────────────────────────────────────────
# SECURE KEY FETCH
# ────────────────────────────────────────────────

async def obtain_secure_key(session, path):

    async with SEM:

        url = f"{URL_BASE}{SECURE_PATH}{path}&method=GET"

        async with session.get(url, headers=COMMON_HEADERS, timeout=15) as r:
            log(f"[KEY] {path} → {r.status}")

            return r.status == 200


# ────────────────────────────────────────────────
# VERIFY SESSION
# ────────────────────────────────────────────────

async def verify_session(session):

    log("Starting verification handshake")

    try:

        async with session.post(
            f"{URL_BASE}/generate_link",
            headers=COMMON_HEADERS,
            json={},
            timeout=20
        ) as r:

            data = await r.json()

        cb_url = data.get("callback_url")

        if not cb_url:
            return False

        async with session.get(cb_url, headers=COMMON_HEADERS):
            pass

        async with session.get(
            f"{URL_BASE}/status",
            headers=COMMON_HEADERS
        ) as r:

            status = await r.json()

        if status.get("verified"):
            log("Session verified")
            return True

    except Exception as e:
        log(f"Verification error → {e}")

    return False


# ────────────────────────────────────────────────
# API REQUEST
# ────────────────────────────────────────────────

async def api_call(session, path):

    async def job():

        await obtain_secure_key(session, path)

        async with SEM:

            async with session.get(
                f"{URL_BASE}{path}",
                headers=COMMON_HEADERS,
                timeout=20
            ) as r:

                if r.status == 200:
                    return await r.json()

                if r.status == 401:

                    log("Unauthorized → re-verifying")

                    if await verify_session(session):

                        await obtain_secure_key(session, path)

                        async with session.get(
                            f"{URL_BASE}{path}",
                            headers=COMMON_HEADERS
                        ) as rr:

                            if rr.status == 200:
                                return await rr.json()

                raise Exception(f"HTTP {r.status}")

    return await retry(job)


# ────────────────────────────────────────────────
# RESOLVE CONTENT
# ────────────────────────────────────────────────

async def resolve_item(session, item):

    item_id = item.get("id")

    async def job():

        data = await api_call(session, f"/api/video/{item_id}")

        if not data:
            return None

        v_url = data.get("video_url", "")

        final_pdf = data.get("pdf_url")
        final_m3u8 = None

        if v_url and v_url.lower().endswith(".pdf"):
            final_pdf = v_url
        else:
            final_m3u8 = v_url

        return {
            "id": str(item_id),
            "title": item.get("name"),
            "m3u8": final_m3u8,
            "youtube": data.get("hd_video_url"),
            "pdf": final_pdf or (
                data.get("pdfs")[0]["url"]
                if data.get("pdfs") else None
            ),
            "thumbnail": data.get("thumbnail_url") or item.get("thumbnail_url"),
            "timestamp": data.get("created_at") or item.get("published_at"),
            "type": "pdf" if final_pdf else "video"
        }

    return await retry(job)


# ────────────────────────────────────────────────
# SUBJECT PROCESS
# ────────────────────────────────────────────────

async def process_subject(session, sub):

    sub_id = sub.get("id")
    sub_name = sub.get("name")

    log(f"Subject → {sub_name}")

    lesson_data = await api_call(session, f"/api/lesson/{sub_id}")

    if not lesson_data:
        return None

    raw_items = (
        (lesson_data.get("videos") or []) +
        (lesson_data.get("notes") or [])
    )

    tasks = [
        resolve_item(session, item)
        for item in raw_items
    ]

    results = await asyncio.gather(*tasks)

    results = [r for r in results if r]

    return {
        "subject_id": str(sub_id),
        "subject_name": sub_name,
        "content": results
    }


# ────────────────────────────────────────────────
# COURSE SCRAPER
# ────────────────────────────────────────────────

async def fetch_course(session, cid):

    log(f"Starting course {cid}")

    out = {
        "course_id": str(cid),
        "course_name": f"Course {cid}",
        "subjects": [],
        "announcements": [],
        "fetched_at": datetime.now(timezone.utc).isoformat()
    }

    classroom = await api_call(session, f"/api/classroom/{cid}")

    if not classroom:
        return out

    subjects = classroom.get("classroom", [])

    tasks = [
        process_subject(session, s)
        for s in subjects
    ]

    results = await asyncio.gather(*tasks)

    out["subjects"] = [r for r in results if r]

    updates = await api_call(session, f"/api/updates/{cid}")

    if isinstance(updates, list):
        out["announcements"] = updates

    return out


# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────

async def main():

    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(timeout=timeout) as session:

        if not await verify_session(session):
            log("Verification failed")
            return

        tasks = [
            fetch_course(session, cid)
            for cid in TARGET_IDS
        ]

        results = await asyncio.gather(*tasks)

        with open(OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        log(f"Saved → {OUT_FILE}")
# ────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(main())
