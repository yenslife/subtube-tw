#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from pathlib import Path

import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

SCRIPT_DIR = Path(__file__).resolve().parent

TOKEN_FILE = Path(os.getenv("YOUTUBE_TOKEN_FILE", SCRIPT_DIR / "token.json"))
CLIENT_SECRET_FILE = Path(os.getenv("YOUTUBE_CLIENT_SECRET_FILE", SCRIPT_DIR / "client_secret.json"))

def get_youtube_client():
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())

    if not creds or not creds.valid:
        if not CLIENT_SECRET_FILE.exists():
            print(f"ERROR: 找不到 {CLIENT_SECRET_FILE}", file=sys.stderr)
            print("請先從 Google Cloud Console 下載 OAuth Desktop client JSON，命名為 client_secret.json", file=sys.stderr)
            sys.exit(1)

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CLIENT_SECRET_FILE),
            SCOPES,
        )
        creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def resumable_upload(request):
    response = None
    error = None
    retry = 0

    while response is None:
        try:
            print("Uploading...", file=sys.stderr)
            status, response = request.next_chunk()

            if status:
                print(f"Uploaded {int(status.progress() * 100)}%", file=sys.stderr)

        except HttpError as e:
            if e.resp.status in [500, 502, 503, 504]:
                error = e
            else:
                raise

        except Exception as e:
            error = e

        if error:
            retry += 1
            if retry > 5:
                raise RuntimeError(f"Upload failed after retries: {error}") from error

            sleep_seconds = min(2 ** retry, 60)
            print(f"Retryable upload error: {error}", file=sys.stderr)
            print(f"Sleep {sleep_seconds}s then retry...", file=sys.stderr)
            time.sleep(sleep_seconds)
            error = None

    return response


def upload_video(
    youtube,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy_status: str,
    category_id: str,
):
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        chunksize=1024 * 1024 * 8,
        resumable=True,
        mimetype="video/mp4",
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = resumable_upload(request)
    video_id = response["id"]

    print(f"Video uploaded: {video_id}", file=sys.stderr)
    return video_id


def upload_caption(
    youtube,
    video_id: str,
    srt_path: Path,
    language: str,
    name: str,
):
    body = {
        "snippet": {
            "videoId": video_id,
            "language": language,
            "name": name,
            "isDraft": False,
        }
    }

    media = MediaFileUpload(
        str(srt_path),
        mimetype="application/octet-stream",
        resumable=False,
    )

    response = youtube.captions().insert(
        part="snippet",
        body=body,
        media_body=media,
    ).execute()

    caption_id = response["id"]
    print(f"Caption uploaded: {caption_id}", file=sys.stderr)
    return caption_id


def add_to_playlist(youtube, video_id: str, playlist_id: str):
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id,
            },
        }
    }

    response = youtube.playlistItems().insert(
        part="snippet",
        body=body,
    ).execute()

    item_id = response["id"]
    print(f"Added to playlist: {item_id}", file=sys.stderr)
    return item_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True, help="mp4 path")
    parser.add_argument("--srt", required=True, help="zh-TW srt path")
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--playlist-id", required=True)
    parser.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"])
    parser.add_argument("--caption-language", default="zh-TW")
    parser.add_argument("--caption-name", default="繁體中文")
    parser.add_argument("--category-id", default="22")
    parser.add_argument("--tags", default="translated,subtitle")
    args = parser.parse_args()

    video_path = Path(args.video)
    srt_path = Path(args.srt)

    if not video_path.exists():
        print(f"ERROR: 找不到影片：{video_path}", file=sys.stderr)
        sys.exit(1)

    if not srt_path.exists():
        print(f"ERROR: 找不到字幕：{srt_path}", file=sys.stderr)
        sys.exit(1)

    tags = [x.strip() for x in args.tags.split(",") if x.strip()]

    youtube = get_youtube_client()

    video_id = upload_video(
        youtube=youtube,
        video_path=video_path,
        title=args.title,
        description=args.description,
        tags=tags,
        privacy_status=args.privacy,
        category_id=args.category_id,
    )

    # YouTube 有時候影片剛上傳後 captions.insert 會太快，稍等比較穩
    print("Waiting before caption upload...", file=sys.stderr)
    time.sleep(10)

    upload_caption(
        youtube=youtube,
        video_id=video_id,
        srt_path=srt_path,
        language=args.caption_language,
        name=args.caption_name,
    )

    add_to_playlist(
        youtube=youtube,
        video_id=video_id,
        playlist_id=args.playlist_id,
    )

    result = {
        "video_id": video_id,
        "watch_url": f"https://www.youtube.com/watch?v={video_id}",
        "playlist_url": f"https://www.youtube.com/playlist?list={args.playlist_id}",
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
