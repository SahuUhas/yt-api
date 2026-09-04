# YouTube Downloader API

A fast and reliable YouTube downloader API powered by **FastAPI**, **yt-dlp**, and **FFmpeg**.

The API provides YouTube search, asynchronous MP4/MP3 downloads, job tracking, direct file serving, caching, concurrency protection, and automatic storage cleanup.

## Features

- ⚡ Fast YouTube search
- 🔎 Search results with title, thumbnail, channel, duration, video ID, and URL
- 🚀 Asynchronous/background downloads
- 🎬 MP4 video downloads
- 🎵 MP3 audio downloads
- 📊 Real-time download job status and progress
- 💾 Short-term search result caching
- 🛡️ Concurrent download protection
- 📥 Direct downloadable file URLs
- 🧹 Automatic temporary-file cleanup
- 🗑️ Failed and cancelled download cleanup
- 🔐 Filename/path traversal protection
- ❤️ Health check endpoint
- 📚 Automatic FastAPI Swagger documentation
- 🌐 CORS enabled
- 📄 Root `index.html` tester
- 🐳 Docker-ready
- 👤 Runs as a non-root user inside Docker

## Project Structure

```text
project/
├── main.py
├── index.html
├── requirements.txt
├── Dockerfile
├── README.md
└── data/
```

`templates/` is not required.

The web interface is loaded directly from:

```text
/app/index.html
```

## Requirements

```txt
fastapi>=0.115,<1.0
uvicorn[standard]>=0.30,<1.0
yt-dlp
pydantic>=2.7,<3.0
```

FFmpeg is required for audio extraction and video/audio merging.

The provided Dockerfile installs FFmpeg automatically.

## Configuration

The API supports the following environment variables:

| Variable | Default | Description |
|---|---:|---|
| `HOST` | `0.0.0.0` | Server host |
| `PORT` | `30127` | API port |
| `SERVER_PORT` | `30127` | Fallback port |
| `STORAGE_DIR` | `./data` | Download storage directory |
| `MAX_FILE_AGE_SECONDS` | `1800` | Maximum fallback file age |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Maximum simultaneous downloads |
| `SEARCH_CACHE_TTL_SECONDS` | `60` | Search cache lifetime |
| `MAX_SEARCH_RESULTS` | `10` | Maximum search results |
| `MAX_JOBS` | `500` | Maximum in-memory job records |
| `PUBLIC_BASE_URL` | empty | Public API base URL |

### Recommended defaults

For a small/medium server:

```env
STORAGE_DIR=/data
MAX_FILE_AGE_SECONDS=1800
MAX_CONCURRENT_DOWNLOADS=2
SEARCH_CACHE_TTL_SECONDS=60
MAX_SEARCH_RESULTS=10
MAX_JOBS=500
```

Increasing concurrent downloads can increase CPU, RAM, disk, bandwidth, and FFmpeg usage. Keep the value appropriate for the server.

## API Endpoints

### Home

```http
GET /
```

Returns the root `index.html` tester.

### Health

```http
GET /health
```

Example:

```json
{
  "success": true,
  "status": "ok",
  "api": {
    "name": "YouTube Downloader API",
    "version": "3.0.0"
  }
}
```

### API Information

```http
GET /api/info
```

Returns API version, engine, supported formats, configuration, and available endpoints.

### YouTube Search

```http
GET /api/search?q=QUERY&limit=5
```

Example:

```text
/api/search?q=Alan Walker&limit=5
```

Response format:

```json
{
  "success": true,
  "query": "Alan Walker",
  "count": 5,
  "results": [
    {
      "title": "Video title",
      "thumbnail": "https://...",
      "duration": 240,
      "channel": "Channel name",
      "videoId": "XXXXXXXXXXX",
      "url": "https://www.youtube.com/watch?v=XXXXXXXXXXX"
    }
  ]
}
```

Search uses `yt-dlp` directly. No `yt-search` package is required.

### Create Download Job

```http
POST /api/download
```

MP4 example:

```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "format": "mp4"
}
```

MP3 example:

```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "format": "mp3"
}
```

A YouTube video ID can also be used:

```json
{
  "videoId": "VIDEO_ID",
  "format": "mp4"
}
```

The API immediately returns a job ID:

```json
{
  "success": true,
  "job_id": "abc123...",
  "status": "queued",
  "format": "mp4",
  "progress": 0,
  "status_url": "/api/jobs/abc123..."
}
```

The download continues in the background.

### Check Download Job

```http
GET /api/jobs/{job_id}
```

Possible states include:

```text
queued
waiting
downloading
processing
completed
failed
```

When completed, the response includes:

```json
{
  "status": "completed",
  "progress": 100,
  "title": "Video title",
  "filename": "abc123.mp4",
  "file_url": "/files/abc123.mp4"
}
```

### Get File

```http
GET /files/{filename}
```

Supported files:

```text
.mp4
.mp3
```

The API streams the file to the client.

After the response has completed, the served file is automatically deleted.

## Storage Cleanup

Downloaded files are stored in the configured storage directory.

Default:

```text
data/
```

Docker default:

```text
/data/
```

The API has two cleanup mechanisms.

### Immediate cleanup

When a completed file is requested through:

```text
/files/{filename}
```

the file is scheduled for deletion after the response finishes.

### Safety cleanup

A background cleanup task runs every 60 seconds.

Files older than:

```text
MAX_FILE_AGE_SECONDS
```

are removed automatically.

The default is:

```text
1800 seconds
```

which is 30 minutes.

Active download files are protected from the age-based cleanup.

Failed and cancelled jobs also remove their temporary files.

## Performance

The API is designed to avoid blocking FastAPI's event loop during yt-dlp operations.

Search and downloads use background threads for the blocking yt-dlp work.

Downloads are protected by a concurrency semaphore:

```text
MAX_CONCURRENT_DOWNLOADS=2
```

Each download can also use concurrent fragments:

```text
concurrent_fragment_downloads=4
```

MP4 format selection first attempts an MP4-compatible single-file format when available, then falls back to separate video/audio streams and FFmpeg merging when necessary.

## Caching

Search results are cached temporarily in memory.

Default cache lifetime:

```text
60 seconds
```

This improves repeated searches without permanently storing search data.

The cache is intentionally limited and is cleared when the API process restarts.

## Error Format

API errors use a consistent structure:

```json
{
  "success": false,
  "error": {
    "code": "ERROR_CODE",
    "message": "Description of the error."
  }
}
```

Common errors include:

```text
INVALID_REQUEST
INVALID_YOUTUBE_URL
INVALID_VIDEO_ID
INVALID_SEARCH_QUERY
SEARCH_FAILED
DOWNLOAD_FAILED
DOWNLOAD_CANCELLED
JOB_NOT_FOUND
FILE_NOT_FOUND
INVALID_FILENAME
UNSUPPORTED_FILE
```

## Docker

Build:

```bash
docker build -t youtube-downloader-api .
```

Run:

```bash
docker run \
  -p 8000:8000 \
  -v youtube-data:/data \
  youtube-downloader-api
```

The container includes:

- Python 3.12
- FastAPI
- Uvicorn
- yt-dlp
- FFmpeg
- CA certificates

The application runs as a non-root user.

## Docker Project Layout

```text
project/
├── Dockerfile
├── requirements.txt
├── main.py
├── index.html
└── README.md
```

The `data/` directory is created automatically when required.

## Local Development

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Make sure FFmpeg is installed and available in `PATH`.

Start the API:

```bash
uvicorn main:app --host 0.0.0.0 --port 30127
```

Or:

```bash
python main.py
```

Open:

```text
http://127.0.0.1:30127/
```

API documentation:

```text
http://127.0.0.1:30127/docs
```

## Messenger Bot Integration

The API is suitable for a Messenger bot flow:

```text
/video query
      ↓
GET /api/search
      ↓
Show 5 results
      ↓
User replies 1-5
      ↓
POST /api/download
      ↓
Receive job_id immediately
      ↓
Poll /api/jobs/{job_id}
      ↓
completed
      ↓
Get file_url
      ↓
Send MP4 to Messenger
```

The bot does not need `yt-search`, `yt-dlp`, or FFmpeg installed.

Only the API server handles YouTube processing.

## Important Performance Note

The API can make the YouTube processing stage fast, but sending a large MP4 to Messenger still depends on:

- API server upload bandwidth
- Bot server download bandwidth
- Messenger upload speed
- Video file size
- Server CPU/FFmpeg performance

The API therefore avoids unnecessary processing and returns a background job immediately, while the bot can poll the job status.

## Reliability

Speed is intentionally balanced with stability.

The API does not start unlimited downloads simultaneously.

This prevents a sudden number of requests from consuming all CPU, RAM, bandwidth, or FFmpeg processes.

Recommended approach:

```text
Fast search
    +
Background downloads
    +
Limited concurrency
    +
Retry handling
    +
Automatic cleanup
    =
Fast and stable API
```

## Updating yt-dlp

YouTube changes over time. Keep `yt-dlp` updated:

```bash
python -m pip install --upgrade yt-dlp
```

For Docker deployments, rebuilding the image installs the current version allowed by the requirements.

## Security

The API validates YouTube URLs and video IDs.

File requests reject:

```text
../
/
\
```

and only supported `.mp4` and `.mp3` files are served.

Do not expose private secrets or credentials in `index.html`, `main.py`, or public API responses.

## License

Use and modify this project according to the licenses of the included dependencies and your own project requirements.

---

**YouTube Downloader API v3.0.0**

Fast search • Background downloads • MP4/MP3 • Job tracking • Auto cleanup • Docker ready
