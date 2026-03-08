# -*- coding: utf-8 -*-

import os
import json
import asyncio
import aiohttp
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

# ────────────────────────────────────────────────
# ENV CONFIG
# ────────────────────────────────────────────────

URL_BASE = os.getenv("URL_BASE")
DATA_URL = os.getenv("DATA_URL")  # course json
SECURE_PATH = os.getenv("SECURE_PATH")

AUTH_KEY = os.getenv("AUTH_KEY")
AUTH_VAL = os.getenv("AUTH_VAL")

THREADS = int(os.getenv("THREADS", "25"))

OUT_FILE = "newfile.json"

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": f"{URL_BASE}/verify",
    "Origin": URL_BASE,
    AUTH_KEY: AUTH_VAL,
}

SEM = asyncio.Semaphore(THREADS)

# ────────────────────────────────────────────────
# JSON MERGE (APPEND ONLY)
# ────────────────────────────────────────────────

def load_existing():

    if not os.path.exists(OUT_FILE):
        return []

    try:
        with open(OUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []


def merge_data(old, new):

    course_map = {c["course_id"]: c for c in old}

    for course in new:

        cid = course["course_id"]

        if cid not in course_map:
            course_map[cid] = course
            continue

        existing_course = course_map[cid]

        subj_map = {
            s["subject_id"]: s
            for s in existing_course.get("subjects", [])
        }

        for sub in course.get("subjects", []):

            sid = sub["subject_id"]

            if sid not in subj_map:
                existing_course.setdefault("subjects", []).append(sub)
                continue

            existing_sub = subj_map[sid]

            existing_items = {
                x["id"]: x
                for x in existing_sub.get("content", [])
            }

            for item in sub.get("content", []):

                if item["id"] not in existing_items:
                    existing_sub.setdefault("content", []).append(item)

        old_ann = {
            a.get("id"): a
            for a in existing_course.get("announcements", [])
        }

        for ann in course.get("announcements", []):

            if ann.get("id") not in old_ann:
                existing_course.setdefault("announcements", []).append(ann)

    return list(course_map.values())


# ────────────────────────────────────────────────
# RETRY WRAPPER
# ────────────────────────────────────────────────

async def retry(coro, attempts=3):

    for i in range(attempts):

        try:
            return await coro()

        except Exception as e:
            print(f"Retry {i+1}/{attempts} → {e}")
            await asyncio.sleep(1)

    return None


# ────────────────────────────────────────────────
# AUTH
# ────────────────────────────────────────────────

async def verify_session(session):

    print("[AUTH] Starting Verification Handshake...")

    async with session.post(
        f"{URL_BASE}/generate_link",
        headers=COMMON_HEADERS,
        json={}
    ) as r:

        data = await r.json()

    cb = data.get("callback_url")

    if not cb:
        return False

    await session.get(cb, headers=COMMON_HEADERS)

    async with session.get(
        f"{URL_BASE}/status",
        headers=COMMON_HEADERS
    ) as r:

        status = await r.json()

    if status.get("verified"):
        print("[AUTH] Session Verified")
        return True

    return False


# ────────────────────────────────────────────────
# SECURE KEY
# ────────────────────────────────────────────────

async def get_secure_key(session, path):

    url = f"{URL_BASE}{SECURE_PATH}{path}&method=GET"

    async with SEM:

        async with session.get(url, headers=COMMON_HEADERS):
            return True


# ────────────────────────────────────────────────
# API REQUEST
# ────────────────────────────────────────────────

async def api_call(session, path):

    async def job():

        await get_secure_key(session, path)

        async with SEM:

            async with session.get(
                f"{URL_BASE}{path}",
                headers=COMMON_HEADERS
            ) as r:

                if r.status == 200:
                    return await r.json()

                if r.status == 401:

                    if await verify_session(session):

                        await get_secure_key(session, path)

                        async with session.get(
                            f"{URL_BASE}{path}",
                            headers=COMMON_HEADERS
                        ) as rr:

                            if rr.status == 200:
                                return await rr.json()

                raise Exception(f"HTTP {r.status}")

    return await retry(job)


# ────────────────────────────────────────────────
# VIDEO RESOLVE
# ────────────────────────────────────────────────

async def resolve_item(session, item):

    item_id = item.get("id")

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


# ────────────────────────────────────────────────
# SUBJECT
# ────────────────────────────────────────────────

async def process_subject(session, sub):

    sub_id = sub.get("id")
    sub_name = sub.get("name")

    print(f"  └─ Subject: {sub_name}")

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
# COURSE
# ────────────────────────────────────────────────

async def fetch_course(session, course):

    cid = course.get("id")

    print(f"\n>>> STARTING COURSE {cid}")

    out = {
        "course_id": str(cid),
        "course_name": course.get("title"),
        "image": course.get("image_thumb"),
        "image_large": course.get("image_large"),
        "start_at": course.get("start_at"),
        "subjects": [],
        "announcements": [],
        "fetched_at": datetime.now(timezone.utc).isoformat()
    }

    classroom = await api_call(session, f"/api/classroom/{cid}")

    if classroom:

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
            return

        print("[INIT] Loading course list")

        async with session.get(DATA_URL) as r:
            courses = await r.json()

        tasks = [
            fetch_course(session, c)
            for c in courses
        ]

        results = await asyncio.gather(*tasks)

        old = load_existing()

        merged = merge_data(old, results)

        with open(OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)

        print("[FINISH] JSON Updated")


if __name__ == "__main__":
    asyncio.run(main())
