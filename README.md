# YouTube Downloader API

![YouTube Downloader API Banner](https://capsule-render.vercel.app/api?type=waving&height=180&text=YouTube%20Downloader%20API&fontSize=32&fontAlignY=40&desc=Fast%20%7C%20Simple%20%7C%20Reliable&descAlignY=65)

A simple YouTube search and download API powered by **FastAPI, yt-dlp and FFmpeg**.


## Features

- YouTube video search
- MP4 video download
- MP3 audio download
- Background download jobs
- Download progress
- Search cache
- Automatic file cleanup
- Browser download support
- CORS support

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | API homepage |
| GET | `/health` | Health check |
| GET | `/api/info` | API information |
| GET | `/api/search?q=QUERY` | YouTube search |
| POST | `/api/download` | Create download job |
| GET | `/api/jobs/{job_id}` | Check download status |
| GET | `/files/{filename}` | Get generated file |

## Search API

```text
GET /api/search?q=music&limit=10
```

Returns video title, thumbnail, channel, duration, video ID and YouTube URL.

## Download API

### MP4

```json
{
  "videoId": "VIDEO_ID",
  "format": "mp4"
}
```

### MP3

```json
{
  "videoId": "VIDEO_ID",
  "format": "mp3"
}
```

Send the request to:

```text
POST /api/download
```

The API returns a `job_id`. Check:

```text
GET /api/jobs/{job_id}
```

When the status becomes `completed`, use the returned `file_url`.

## API Source Code

The main API source is:

```text
main.py
```

It contains the FastAPI application, YouTube search, download jobs, yt-dlp processing, FFmpeg processing, progress tracking and file cleanup.

The API does the media processing on the server, so client applications and bots only need HTTP requests.

