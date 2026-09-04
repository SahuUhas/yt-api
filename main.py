import asyncio
import hashlib
import os
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import BackgroundTask, FileResponse, JSONResponse
from pydantic import BaseModel, Field


APP_NAME = "YouTube Downloader API"
VERSION = "3.0.0"

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", os.getenv("SERVER_PORT", "30127")))

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"

STORAGE_DIR = Path(
    os.getenv("STORAGE_DIR", str(BASE_DIR / "data"))
).resolve()

MAX_FILE_AGE = max(
    60,
    int(os.getenv("MAX_FILE_AGE_SECONDS", "1800"))
)

MAX_CONCURRENT_DOWNLOADS = max(
    1,
    int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
)

MAX_SEARCH_RESULTS = max(
    1,
    int(os.getenv("MAX_SEARCH_RESULTS", "10"))
)

SEARCH_CACHE_TTL = max(
    10,
    int(os.getenv("SEARCH_CACHE_TTL_SECONDS", "60"))
)

MAX_JOBS = max(
    50,
    int(os.getenv("MAX_JOBS", "500"))
)

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    ""
).rstrip("/")


STORAGE_DIR.mkdir(
    parents=True,
    exist_ok=True
)


jobs: dict[str, dict[str, Any]] = {}
jobs_lock = asyncio.Lock()

search_cache: dict[
    str,
    tuple[float, dict[str, Any]]
] = {}

search_cache_lock = asyncio.Lock()

download_semaphore = asyncio.Semaphore(
    MAX_CONCURRENT_DOWNLOADS
)

download_tasks: set[asyncio.Task] = set()


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
        pattern="^(mp4|mp3)$"
    )


def error_response(
    code: str,
    message: str
) -> dict[str, Any]:

    return {
        "success": False,
        "error": {
            "code": code,
            "message": message
        }
    }


def raise_api_error(
    status_code: int,
    code: str,
    message: str
) -> None:

    raise HTTPException(
        status_code=status_code,
        detail=error_response(
            code,
            message
        )
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(
    request,
    exc: HTTPException
):

    if isinstance(exc.detail, dict):

        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail
        )

    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(
            "HTTP_ERROR",
            str(exc.detail)
        )
    )


def is_youtube_url(
    url: str
) -> bool:

    try:

        parsed = urlparse(
            url.strip()
        )

        host = (
            parsed.hostname or ""
        ).lower()

        return (
            parsed.scheme in {
                "http",
                "https"
            }
            and host in YOUTUBE_HOSTS
        )

    except Exception:

        return False


def validate_video_id(
    video_id: str
) -> bool:

    return bool(
        re.fullmatch(
            r"[A-Za-z0-9_-]{6,20}",
            video_id.strip()
        )
    )


def video_id_to_url(
    video_id: str
) -> str:

    video_id = video_id.strip()

    if not validate_video_id(
        video_id
    ):

        raise_api_error(
            400,
            "INVALID_VIDEO_ID",
            "Invalid YouTube video ID."
        )

    return (
        "https://www.youtube.com/watch?v="
        + video_id
    )


def resolve_download_url(
    request: DownloadRequest
) -> str:

    if request.url:

        url = request.url.strip()

        if not is_youtube_url(
            url
        ):

            raise_api_error(
                400,
                "INVALID_YOUTUBE_URL",
                "Only valid YouTube URLs are supported."
            )

        return url

    if request.videoId:

        return video_id_to_url(
            request.videoId
        )

    raise_api_error(
        400,
        "INVALID_REQUEST",
        "Provide either url or videoId."
    )

    return ""


def progress_hook(
    job_id: str
):

    def hook(
        data: dict[str, Any]
    ):

        job = jobs.get(
            job_id
        )

        if not job:
            return

        status = data.get(
            "status"
        )

        if status == "downloading":

            total = (
                data.get(
                    "total_bytes"
                )
                or data.get(
                    "total_bytes_estimate"
                )
            )

            downloaded = data.get(
                "downloaded_bytes",
                0
            )

            progress = 0

            if total:

                progress = round(
                    (
                        downloaded / total
                    ) * 100,
                    1
                )

                progress = min(
                    99.9,
                    max(
                        0,
                        progress
                    )
                )

            job["status"] = "downloading"

            job["progress"] = progress

            job["speed"] = (
                data.get(
                    "_speed_str"
                )
                or data.get(
                    "speed"
                )
            )

            job["eta"] = (
                data.get(
                    "_eta_str"
                )
                or data.get(
                    "eta"
                )
            )

        elif status == "finished":

            job["status"] = "processing"

            job["progress"] = 100

    return hook


def build_ydl_options(
    job_id: str,
    output_format: str
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

        "socket_timeout": 20,

        "retries": 3,

        "fragment_retries": 3,

        "concurrent_fragment_downloads": 4,

        "progress_hooks": [
            progress_hook(
                job_id
            )
        ]
    }

    if output_format == "mp3":

        common.update({

            "format":
                "bestaudio/best",

            "postprocessors": [
                {
                    "key":
                        "FFmpegExtractAudio",

                    "preferredcodec":
                        "mp3",

                    "preferredquality":
                        "192"
                }
            ]
        })

    else:

        common.update({

            "format":
                "b[ext=mp4]/"
                "bv*[ext=mp4]+ba[ext=m4a]/"
                "bv*+ba/b",

            "merge_output_format":
                "mp4"
        })

    return common


def find_output_file(
    job_id: str,
    output_format: str
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

    if expected.is_file():

        return expected

    candidates = [
        path
        for path in STORAGE_DIR.glob(
            f"{job_id}.*"
        )
        if (
            path.is_file()
            and not path.name.endswith(
                ".part"
            )
        )
    ]

    if not candidates:

        return None

    return max(
        candidates,
        key=lambda path:
            path.stat().st_mtime
    )


def cleanup_job_files(
    job_id: str
) -> None:

    for path in STORAGE_DIR.glob(
        f"{job_id}.*"
    ):

        try:

            if path.is_file():

                path.unlink(
                    missing_ok=True
                )

        except OSError:

            pass


def perform_download(
    job_id: str,
    url: str,
    output_format: str
):

    options = build_ydl_options(
        job_id,
        output_format
    )

    with yt_dlp.YoutubeDL(
        options
    ) as ydl:

        info = ydl.extract_info(
            url,
            download=True
        )

        title = (
            info.get("title")
            or "YouTube Download"
        )

    file_path = find_output_file(
        job_id,
        output_format
    )

    if not file_path:

        raise RuntimeError(
            "Download completed but output file was not found."
        )

    expected_extension = (
        ".mp3"
        if output_format == "mp3"
        else ".mp4"
    )

    if (
        file_path.suffix.lower()
        != expected_extension
    ):

        raise RuntimeError(
            f"Expected {expected_extension} output "
            f"but received "
            f"{file_path.suffix or 'unknown'}."
        )

    return file_path, title


async def run_download(
    job_id: str,
    url: str,
    output_format: str
):

    async with jobs_lock:

        if job_id not in jobs:

            return

        jobs[job_id][
            "status"
        ] = "waiting"

    try:

        async with download_semaphore:

            async with jobs_lock:

                if job_id in jobs:

                    jobs[job_id][
                        "status"
                    ] = "downloading"

            file_path, title = (
                await asyncio.to_thread(
                    perform_download,
                    job_id,
                    url,
                    output_format
                )
            )

        filename = file_path.name

        if PUBLIC_BASE_URL:

            file_url = (
                f"{PUBLIC_BASE_URL}"
                f"/files/{filename}"
            )

            status_url = (
                f"{PUBLIC_BASE_URL}"
                f"/api/jobs/{job_id}"
            )

        else:

            file_url = (
                f"/files/{filename}"
            )

            status_url = (
                f"/api/jobs/{job_id}"
            )

        async with jobs_lock:

            if job_id in jobs:

                jobs[job_id].update({

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

                    "status_url":
                        status_url,

                    "completed_at":
                        int(time.time())
                })

    except asyncio.CancelledError:

        cleanup_job_files(
            job_id
        )

        async with jobs_lock:

            if job_id in jobs:

                jobs[job_id].update({

                    "status":
                        "failed",

                    "progress":
                        0,

                    "error": {
                        "code":
                            "DOWNLOAD_CANCELLED",

                        "message":
                            "Download was cancelled."
                    },

                    "completed_at":
                        int(time.time())
                })

        raise

    except Exception as exc:

        cleanup_job_files(
            job_id
        )

        async with jobs_lock:

            if job_id in jobs:

                jobs[job_id].update({

                    "status":
                        "failed",

                    "progress":
                        0,

                    "error": {
                        "code":
                            "DOWNLOAD_FAILED",

                        "message":
                            str(exc)
                    },

                    "completed_at":
                        int(time.time())
                })


async def cleanup_loop():

    while True:

        try:

            now = time.time()

            active_ids = {

                job_id

                for job_id, job
                in jobs.items()

                if job.get(
                    "status"
                ) in {

                    "queued",
                    "waiting",
                    "starting",
                    "downloading",
                    "processing"

                }
            }

            for path in STORAGE_DIR.iterdir():

                try:

                    if not path.is_file():

                        continue

                    if any(
                        path.name.startswith(
                            f"{job_id}."
                        )
                        for job_id
                        in active_ids
                    ):

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

                    continue

            async with jobs_lock:

                expired = []

                for job_id, job
                in list(
                    jobs.items()
                ):

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

                        expired.append(
                            job_id
                        )

                for job_id in expired:

                    jobs.pop(
                        job_id,
                        None
                    )

                if len(jobs) > MAX_JOBS:

                    finished = sorted(

                        (
                            (jid, job)

                            for jid, job
                            in jobs.items()

                            if job.get(
                                "status"
                            ) in {

                                "completed",
                                "failed"

                            }
                        ),

                        key=lambda item:
                            item[1].get(
                                "completed_at",
                                item[1].get(
                                    "created_at",
                                    0
                                )
                            )
                    )

                    remove_count = (
                        len(jobs)
                        - MAX_JOBS
                    )

                    for jid, _ in finished[
                        :remove_count
                    ]:

                        jobs.pop(
                            jid,
                            None
                        )

        except Exception:

            pass

        await asyncio.sleep(
            60
        )


@asynccontextmanager
async def lifespan(app: FastAPI):

    cleanup_task = (
        asyncio.create_task(
            cleanup_loop()
        )
    )

    try:

        yield

    finally:

        cleanup_task.cancel()

        try:

            await cleanup_task

        except asyncio.CancelledError:

            pass

        for task in list(
            download_tasks
        ):

            task.cancel()


app = FastAPI(
    title=APP_NAME,
    version=VERSION,
    description=(
        "Fast YouTube downloader API "
        "powered by yt-dlp and FFmpeg."
    ),
    lifespan=lifespan
)


app.add_middleware(
    CORSMiddleware,

    allow_origins=["*"],

    allow_credentials=False,

    allow_methods=["*"],

    allow_headers=["*"]
)


@app.get("/")
async def root():

    if not INDEX_FILE.is_file():

        raise_api_error(
            500,
            "INDEX_NOT_FOUND",
            "index.html was not found."
        )

    return FileResponse(
        INDEX_FILE,
        media_type="text/html"
    )


@app.get("/health")
async def health():

    ffmpeg_path = shutil.which(
        "ffmpeg"
    )

    return {

        "success": True,

        "status": "ok",

        "api": {

            "name":
                APP_NAME,

            "version":
                VERSION
        },

        "yt_dlp":
            yt_dlp.version.__version__,

        "ffmpeg": {

            "available":
                bool(ffmpeg_path),

            "path":
                ffmpeg_path
        },

        "storage": {

            "directory":
                str(STORAGE_DIR),

            "available":
                STORAGE_DIR.exists(),

            "writable":
                os.access(
                    STORAGE_DIR,
                    os.W_OK
                )
        },

        "workers": {

            "max_concurrent_downloads":
                MAX_CONCURRENT_DOWNLOADS,

            "concurrent_fragments":
                4
        }
    }


@app.get("/api/info")
async def api_info():

    return {

        "success": True,

        "name":
            APP_NAME,

        "version":
            VERSION,

        "engine":
            "yt-dlp + FFmpeg",

        "supported_site":
            "YouTube",

        "formats": [
            "mp4",
            "mp3"
        ],

        "async_jobs":
            True,

        "search_cache":
            True,

        "automatic_cleanup":
            True,

        "max_concurrent_downloads":
            MAX_CONCURRENT_DOWNLOADS,

        "endpoints": {

            "home":
                "/",

            "health":
                "/health",

            "docs":
                "/docs",

            "search":
                "/api/search",

            "download":
                "/api/download",

            "job":
                "/api/jobs/{job_id}",

            "files":
                "/files/{filename}"
        }
    }


@app.get("/api/search")
async def search(

    q: str = Query(
        ...,
        min_length=1,
        max_length=200
    ),

    limit: int = Query(
        10,
        ge=1,
        le=25
    )
):

    q = q.strip()

    if not q:

        raise_api_error(
            400,
            "INVALID_SEARCH_QUERY",
            "Search query cannot be empty."
        )

    limit = min(
        limit,
        MAX_SEARCH_RESULTS
    )

    cache_key = (
        f"{q.lower()}:{limit}"
    )

    now = time.time()

    async with search_cache_lock:

        cached = search_cache.get(
            cache_key
        )

        if (
            cached
            and
            now - cached[0]
            < SEARCH_CACHE_TTL
        ):

            return cached[1]

        if cached:

            search_cache.pop(
                cache_key,
                None
            )

    options = {

        "quiet":
            True,

        "no_warnings":
            True,

        "extract_flat":
            True,

        "skip_download":
            True,

        "noplaylist":
            True,

        "socket_timeout":
            12,

        "retries":
            1
    }

    def search_sync():

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            return ydl.extract_info(
                f"ytsearch{limit}:{q}",
                download=False
            )

    try:

        data = await asyncio.to_thread(
            search_sync
        )

    except Exception as exc:

        raise_api_error(
            502,
            "SEARCH_FAILED",
            f"YouTube search failed: {exc}"
        )

    results = []

    for item in (
        data.get(
            "entries",
            []
        )
        or []
    ):

        if not item:

            continue

        video_id = item.get(
            "id"
        )

        if not video_id:

            continue

        results.append({

            "title":
                item.get("title"),

            "thumbnail":
                (
                    item.get("thumbnail")
                    or
                    f"https://i.ytimg.com/vi/"
                    f"{video_id}/hqdefault.jpg"
                ),

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
                    item.get("webpage_url")
                    or
                    f"https://www.youtube.com/watch?v="
                    f"{video_id}"
                )
        })

    response = {

        "success":
            True,

        "query":
            q,

        "count":
            len(results),

        "results":
            results
    }

    async with search_cache_lock:

        search_cache[
            cache_key
        ] = (
            time.time(),
            response
        )

        if len(search_cache) > 200:

            oldest_key = min(
                search_cache,
                key=lambda key:
                    search_cache[key][0]
            )

            search_cache.pop(
                oldest_key,
                None
            )

    return response


@app.post(
    "/api/download",
    status_code=202
)
async def create_download(
    request: DownloadRequest
):

    url = resolve_download_url(
        request
    )

    output_format = (
        request.format.lower()
    )

    job_id = uuid.uuid4().hex

    status_url = (

        f"{PUBLIC_BASE_URL}"
        f"/api/jobs/{job_id}"

        if PUBLIC_BASE_URL

        else

        f"/api/jobs/{job_id}"
    )

    job = {

        "success":
            True,

        "job_id":
            job_id,

        "status":
            "queued",

        "format":
            output_format,

        "progress":
            0,

        "speed":
            None,

        "eta":
            None,

        "created_at":
            int(time.time()),

        "status_url":
            status_url
    }

    async with jobs_lock:

        jobs[job_id] = job

    task = asyncio.create_task(
        run_download(
            job_id,
            url,
            output_format
        )
    )

    download_tasks.add(
        task
    )

    task.add_done_callback(
        download_tasks.discard
    )

    return job


@app.get(
    "/api/jobs/{job_id}"
)
async def get_job(
    job_id: str
):

    async with jobs_lock:

        job = jobs.get(
            job_id
        )

        if job:

            return dict(job)

    raise_api_error(
        404,
        "JOB_NOT_FOUND",
        "Download job not found."
    )


@app.get(
    "/files/{filename}"
)
async def get_file(
    filename: str
):

    if (

        not filename

        or "/" in filename

        or "\\" in filename

        or ".." in filename

    ):

        raise_api_error(
            400,
            "INVALID_FILENAME",
            "Invalid filename."
        )

    path = (
        STORAGE_DIR
        / filename
    ).resolve()

    storage_root = (
        STORAGE_DIR.resolve()
    )

    if path.parent != storage_root:

        raise_api_error(
            400,
            "INVALID_FILENAME",
            "Invalid filename."
        )

    if not path.is_file():

        raise_api_error(
            404,
            "FILE_NOT_FOUND",
            "File not found or expired."
        )

    extension = (
        path.suffix.lower()
    )

    if extension == ".mp3":

        media_type = "audio/mpeg"

    elif extension == ".mp4":

        media_type = "video/mp4"

    else:

        raise_api_error(
            403,
            "UNSUPPORTED_FILE",
            "Unsupported file type."
        )

    cleanup = BackgroundTask(
        delete_file_after_response,
        path
    )

    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        background=cleanup
    )


def delete_file_after_response(
    path: Path
):

    try:

        path.unlink(
            missing_ok=True
        )

    except OSError:

        pass


if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host=HOST,
        port=PORT
    )
