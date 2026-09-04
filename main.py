import asyncio
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


APP_NAME = "YouTube Downloader API"
VERSION = "2.0.0"

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", os.getenv("SERVER_PORT", "8000")))

STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "/data"))
MAX_FILE_AGE = int(os.getenv("MAX_FILE_AGE_SECONDS", "1800"))

MAX_CONCURRENT_DOWNLOADS = int(
    os.getenv("MAX_CONCURRENT_DOWNLOADS", "4")
)

MAX_SEARCH_RESULTS = int(
    os.getenv("MAX_SEARCH_RESULTS", "10")
)

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    ""
).rstrip("/")

STORAGE_DIR.mkdir(parents=True, exist_ok=True)


app = FastAPI(
    title=APP_NAME,
    version=VERSION,
    description=(
        "Fast YouTube downloader API powered by "
        "yt-dlp and FFmpeg."
    ),
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


jobs: dict[str, dict[str, Any]] = {}

jobs_lock = asyncio.Lock()

download_semaphore = asyncio.Semaphore(
    MAX_CONCURRENT_DOWNLOADS
)


YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


class DownloadRequest(BaseModel):
    url: Optional[str] = None

    videoId: Optional[str] = None

    format: str = Field(
        default="mp4",
        pattern="^(mp4|mp3)$",
    )


def error_response(
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }


def is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url)

        host = (
            parsed.hostname or ""
        ).lower()

        return host in YOUTUBE_HOSTS

    except Exception:
        return False


def validate_video_id(
    video_id: str,
) -> bool:

    return bool(
        re.fullmatch(
            r"[A-Za-z0-9_-]{6,20}",
            video_id,
        )
    )


def video_id_to_url(
    video_id: str,
) -> str:

    if not validate_video_id(video_id):
        raise HTTPException(
            status_code=400,
            detail=error_response(
                "INVALID_VIDEO_ID",
                "Invalid YouTube video ID.",
            ),
        )

    return (
        "https://www.youtube.com/watch?v="
        + video_id
    )


def resolve_download_url(
    request: DownloadRequest,
) -> str:

    if request.url:

        if not is_youtube_url(
            request.url
        ):
            raise HTTPException(
                status_code=400,
                detail=error_response(
                    "INVALID_YOUTUBE_URL",
                    "Only valid YouTube URLs are supported.",
                ),
            )

        return request.url

    if request.videoId:
        return video_id_to_url(
            request.videoId
        )

    raise HTTPException(
        status_code=400,
        detail=error_response(
            "INVALID_REQUEST",
            "Provide either url or videoId.",
        ),
    )


def progress_hook(
    job_id: str,
):
    def hook(
        data: dict[str, Any],
    ):

        job = jobs.get(job_id)

        if not job:
            return

        status = data.get("status")

        if status == "downloading":

            total = (
                data.get("total_bytes")
                or data.get("total_bytes_estimate")
            )

            downloaded = (
                data.get(
                    "downloaded_bytes",
                    0,
                )
            )

            progress = None

            if total:
                progress = round(
                    (
                        downloaded
                        / total
                    )
                    * 100,
                    1,
                )

            job["status"] = (
                "downloading"
            )

            job["progress"] = (
                progress
                if progress is not None
                else 0
            )

            job["speed"] = (
                data.get("_speed_str")
                or data.get("speed")
            )

            job["eta"] = (
                data.get("_eta_str")
                or data.get("eta")
            )

        elif status == "finished":

            job["status"] = (
                "processing"
            )

            job["progress"] = 100

    return hook


def build_ydl_options(
    job_id: str,
    output_format: str,
) -> dict[str, Any]:

    output_template = str(
        STORAGE_DIR
        / f"{job_id}.%(ext)s"
    )

    common = {
        "outtmpl": output_template,

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "restrictfilenames": True,

        "socket_timeout": 30,

        "retries": 3,

        "fragment_retries": 3,

        "progress_hooks": [
            progress_hook(job_id)
        ],
    }

    if output_format == "mp3":

        common.update(
            {
                "format": (
                    "bestaudio/best"
                ),

                "postprocessors": [
                    {
                        "key":
                            "FFmpegExtractAudio",

                        "preferredcodec":
                            "mp3",

                        "preferredquality":
                            "192",
                    }
                ],
            }
        )

    else:

        common.update(
            {
                "format": (
                    "bv*[ext=mp4]+"
                    "ba[ext=m4a]/"
                    "b[ext=mp4]/"
                    "bv*+ba/b"
                ),

                "merge_output_format":
                    "mp4",
            }
        )

    return common


def find_output_file(
    job_id: str,
    output_format: str,
) -> Optional[Path]:

    expected_extension = (
        "mp3"
        if output_format == "mp3"
        else "mp4"
    )

    expected = (
        STORAGE_DIR
        / f"{job_id}.{expected_extension}"
    )

    if expected.exists():
        return expected

    candidates = [
        path
        for path in STORAGE_DIR.glob(
            f"{job_id}.*"
        )
        if path.is_file()
    ]

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda path: path.stat().st_mtime,
    )


def perform_download(
    job_id: str,
    url: str,
    output_format: str,
):

    options = build_ydl_options(
        job_id,
        output_format,
    )

    with yt_dlp.YoutubeDL(
        options
    ) as ydl:

        info = ydl.extract_info(
            url,
            download=True,
        )

        title = (
            info.get("title")
            or "YouTube Download"
        )

    file_path = find_output_file(
        job_id,
        output_format,
    )

    if not file_path:

        raise RuntimeError(
            "Download completed but output file was not found."
        )

    return file_path, title


async def run_download(
    job_id: str,
    url: str,
    output_format: str,
):

    async with jobs_lock:

        if job_id not in jobs:
            return

        jobs[job_id][
            "status"
        ] = "starting"

    try:

        async with download_semaphore:

            file_path, title = (
                await asyncio.to_thread(
                    perform_download,
                    job_id,
                    url,
                    output_format,
                )
            )

        filename = file_path.name

        if PUBLIC_BASE_URL:

            file_url = (
                f"{PUBLIC_BASE_URL}"
                f"/files/{filename}"
            )

        else:

            file_url = (
                f"/files/{filename}"
            )

        async with jobs_lock:

            jobs[job_id].update(
                {
                    "status":
                        "completed",

                    "progress":
                        100,

                    "title":
                        title,

                    "filename":
                        filename,

                    "file_url":
                        file_url,

                    "completed_at":
                        int(time.time()),
                }
            )

    except Exception as exc:

        async with jobs_lock:

            jobs[job_id].update(
                {
                    "status":
                        "failed",

                    "progress":
                        0,

                    "error": {
                        "code":
                            "DOWNLOAD_FAILED",

                        "message":
                            str(exc),
                    },

                    "completed_at":
                        int(time.time()),
                }
            )


async def cleanup_loop():

    while True:

        now = time.time()

        for path in STORAGE_DIR.iterdir():

            try:

                if not path.is_file():
                    continue

                age = (
                    now
                    - path.stat().st_mtime
                )

                if age > MAX_FILE_AGE:

                    path.unlink(
                        missing_ok=True
                    )

            except OSError:
                pass

        async with jobs_lock:

            expired_jobs = []

            for job_id, job in jobs.items():

                completed_at = (
                    job.get(
                        "completed_at"
                    )
                )

                if (
                    completed_at
                    and
                    now - completed_at
                    > MAX_FILE_AGE
                ):
                    expired_jobs.append(
                        job_id
                    )

            for job_id in expired_jobs:

                jobs.pop(
                    job_id,
                    None,
                )

        await asyncio.sleep(300)


@app.on_event("startup")
async def startup_event():

    app.state.cleanup_task = (
        asyncio.create_task(
            cleanup_loop()
        )
    )


@app.on_event("shutdown")
async def shutdown_event():

    task = getattr(
        app.state,
        "cleanup_task",
        None,
    )

    if task:

        task.cancel()


@app.get("/")
async def root():

    return {
        "name": APP_NAME,
        "version": VERSION,
        "status": "online",
        "message": (
            "YouTube Downloader API is running."
        ),
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "search":
                "/api/search",

            "download":
                "/api/download",

            "job":
                "/api/jobs/{job_id}",

            "files":
                "/files/{filename}",
        },
    }


@app.get("/health")
async def health():

    ffmpeg_path = (
        shutil.which("ffmpeg")
    )

    return {
        "success": True,
        "status": "ok",
        "api": {
            "name":
                APP_NAME,

            "version":
                VERSION,
        },
        "yt_dlp": (
            yt_dlp.version.__version__
        ),
        "ffmpeg": {
            "available":
                bool(ffmpeg_path),

            "path":
                ffmpeg_path,
        },
        "storage": {
            "directory":
                str(STORAGE_DIR),

            "available":
                STORAGE_DIR.exists(),
        },
        "workers": {
            "max_concurrent_downloads":
                MAX_CONCURRENT_DOWNLOADS,
        },
    }


@app.get("/api/info")
async def api_info():

    return {
        "success": True,
        "name": APP_NAME,
        "version": VERSION,
        "engine": (
            "yt-dlp + FFmpeg"
        ),
        "supported_site":
            "YouTube",
        "formats": [
            "mp4",
            "mp3",
        ],
        "async_jobs": True,
        "max_concurrent_downloads":
            MAX_CONCURRENT_DOWNLOADS,
    }


@app.get("/api/search")
async def search(
    q: str = Query(
        ...,
        min_length=1,
        max_length=200,
    ),

    limit: int = Query(
        10,
        ge=1,
        le=25,
    ),
):

    limit = min(
        limit,
        MAX_SEARCH_RESULTS,
    )

    options = {
        "quiet": True,

        "no_warnings": True,

        "extract_flat": True,

        "skip_download": True,

        "noplaylist": True,

        "socket_timeout": 15,

        "retries": 2,
    }

    def search_sync():

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            return ydl.extract_info(
                f"ytsearch{limit}:{q}",
                download=False,
            )

    try:

        data = await asyncio.to_thread(
            search_sync
        )

    except Exception:

        raise HTTPException(
            status_code=502,
            detail=error_response(
                "SEARCH_FAILED",
                "YouTube search failed.",
            ),
        )

    results = []

    for item in (
        data.get("entries", [])
        or []
    ):

        if not item:
            continue

        video_id = item.get("id")

        if not video_id:
            continue

        results.append(
            {
                "title":
                    item.get("title"),

                "thumbnail":
                    item.get("thumbnail"),

                "duration":
                    item.get("duration"),

                "channel":
                    (
                        item.get("channel")
                        or
                        item.get("uploader")
                    ),

                "videoId":
                    video_id,

                "url":
                    (
                        item.get(
                            "webpage_url"
                        )
                        or
                        f"https://www.youtube.com/watch?v={video_id}"
                    ),
            }
        )

    return {
        "success": True,
        "query": q,
        "count": len(results),
        "results": results,
    }


@app.post(
    "/api/download",
    status_code=202,
)
async def create_download(
    request: DownloadRequest,
):

    url = resolve_download_url(
        request
    )

    job_id = uuid.uuid4().hex

    job = {
        "success": True,

        "job_id":
            job_id,

        "status":
            "queued",

        "format":
            request.format,

        "progress":
            0,

        "created_at":
            int(time.time()),

        "status_url":
            f"/api/jobs/{job_id}",
    }

    async with jobs_lock:

        jobs[job_id] = job

    asyncio.create_task(
        run_download(
            job_id,
            url,
            request.format,
        )
    )

    return job


@app.get(
    "/api/jobs/{job_id}"
)
async def get_job(
    job_id: str,
):

    async with jobs_lock:

        job = jobs.get(
            job_id
        )

    if not job:

        raise HTTPException(
            status_code=404,
            detail=error_response(
                "JOB_NOT_FOUND",
                "Download job not found.",
            ),
        )

    return job


@app.get(
    "/files/{filename}"
)
async def get_file(
    filename: str,
):

    if (
        "/" in filename
        or "\\" in filename
        or ".." in filename
    ):

        raise HTTPException(
            status_code=400,
            detail=error_response(
                "INVALID_FILENAME",
                "Invalid filename.",
            ),
        )

    path = (
        STORAGE_DIR
        / filename
    )

    if not path.is_file():

        raise HTTPException(
            status_code=404,
            detail=error_response(
                "FILE_NOT_FOUND",
                "File not found or expired.",
            ),
        )

    extension = (
        path.suffix.lower()
    )

    if extension == ".mp3":

        media_type = (
            "audio/mpeg"
        )

    elif extension == ".mp4":

        media_type = (
            "video/mp4"
        )

    else:

        raise HTTPException(
            status_code=403,
            detail=error_response(
                "UNSUPPORTED_FILE",
                "Unsupported file type.",
            ),
        )

    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
    )


if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
    )
