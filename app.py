import os
import io
import subprocess
import json
import tempfile
from urllib.parse import quote
from flask import Flask, request, jsonify, render_template, Response, send_file, abort
import threading
import uuid
import time

app = Flask(__name__)

MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "/media")
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".wmv", ".flv", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Top-level library categories (subfolders of MEDIA_ROOT)
LIBRARY_CATEGORIES = ["movies", "tvshows", "anime"]

# Primary-image filename priority (first match wins)
POSTER_NAMES = ["folder.jpg", "folder.png", "poster.jpg", "poster.png"]

# Store job progress
jobs = {}


def is_video(filename):
    return os.path.splitext(filename)[1].lower() in VIDEO_EXTENSIONS


def safe_path(path):
    """Normalize a path and ensure it stays within MEDIA_ROOT. Returns None if outside."""
    if not path:
        return None
    full = os.path.normpath(path)
    root = os.path.normpath(MEDIA_ROOT)
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


def find_poster(folder):
    """Return the path to the primary image for a folder, or None."""
    for name in POSTER_NAMES:
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate):
            return candidate
    return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/library")
def library():
    """List top-level categories and their entries with primary images."""
    categories = []
    for cat in LIBRARY_CATEGORIES:
        cat_dir = os.path.join(MEDIA_ROOT, cat)
        if not os.path.isdir(cat_dir):
            continue

        items = []
        try:
            names = sorted(os.listdir(cat_dir), key=str.lower)
        except (PermissionError, FileNotFoundError):
            continue

        for name in names:
            full = os.path.join(cat_dir, name)
            if not os.path.isdir(full):
                continue
            poster = find_poster(full)
            items.append({
                "name": name,
                "path": full,
                "image": f"/api/image?path={quote(poster)}" if poster else None,
            })

        categories.append({"name": cat, "count": len(items), "items": items})

    return jsonify({"categories": categories})


@app.route("/api/image")
def image():
    """Serve a poster image from within MEDIA_ROOT."""
    path = safe_path(request.args.get("path", ""))
    if not path or not os.path.isfile(path):
        abort(404)
    if os.path.splitext(path)[1].lower() not in IMAGE_EXTENSIONS:
        abort(403)
    return send_file(path)


PREVIEW_DURATION = 180  # seconds per transcoded preview segment


@app.route("/api/audiostreams")
def audiostreams():
    """List the audio tracks of a source file via ffprobe."""
    path = safe_path(request.args.get("path", ""))
    if not path or not os.path.isfile(path) or not is_video(path):
        abort(404)

    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-select_streams", "a",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        info = json.loads(result.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return jsonify([])

    tracks = []
    for s in info.get("streams", []):
        tags = s.get("tags") or {}
        tracks.append({
            "index": s.get("index"),
            "language": tags.get("language", "unknown"),
            "codec": s.get("codec_name"),
            "channels": s.get("channels"),
            "layout": s.get("channel_layout"),
        })
    return jsonify(tracks)


@app.route("/api/preview")
def preview():
    """Transcode a short segment to a browser-friendly H.264/AAC MP4.

    Browsers need the complete moov atom before playback, so we transcode to a
    temp file with -movflags +faststart (moov at the front of the file). We then
    read the finished file into memory, delete it immediately, and serve the
    bytes. Reading-then-deleting avoids leaking temp files: response-close hooks
    (call_on_close) are skipped by the WSGI file_wrapper fast-path, and unlinking
    an open file is unreliable on non-POSIX hosts.
    """
    path = safe_path(request.args.get("path", ""))
    if not path or not os.path.isfile(path) or not is_video(path):
        abort(404)

    try:
        start = str(max(0.0, float(request.args.get("start", 0))))
    except (TypeError, ValueError):
        start = "0"

    # Audio track selection: explicit absolute stream index, else first audio stream
    audio_index = request.args.get("audio_index")
    audio_map = f"0:{audio_index}" if audio_index and audio_index.isdigit() else "0:a:0"

    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()

    cmd = [
        "ffmpeg", "-y",
        "-ss", start,
        "-t", str(PREVIEW_DURATION),
        "-i", path,
        "-map", "0:v:0",
        "-map", audio_map,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        # Force browser-playable audio: AAC stereo regardless of source codec
        # (EAC3/DTS/TrueHD aren't natively decodable in browsers).
        "-af", "aformat=sample_fmts=fltp",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-movflags", "+faststart",
        "-f", "mp4",
        tmp.name,
    ]

    try:
        result = subprocess.run(cmd, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            abort(500)
        with open(tmp.name, "rb") as f:
            data = f.read()
    finally:
        _safe_unlink(tmp.name)

    return send_file(
        io.BytesIO(data),
        mimetype="video/mp4",
        as_attachment=False,
        download_name="preview.mp4",
        conditional=True,
    )


def _safe_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


@app.route("/api/browse")
def browse():
    path = safe_path(request.args.get("path", MEDIA_ROOT)) or MEDIA_ROOT

    if not os.path.isdir(path):
        return jsonify({"error": "Not a directory"}), 400

    try:
        entries = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isdir(full):
                entries.append({"name": name, "path": full, "type": "dir"})
            elif is_video(name):
                size = os.path.getsize(full)
                entries.append({"name": name, "path": full, "type": "video", "size": size})

        parent = os.path.dirname(path) if path != MEDIA_ROOT else None

        return jsonify({
            "current": path,
            "parent": parent if parent and parent.startswith(MEDIA_ROOT) else None,
            "entries": entries
        })
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403


@app.route("/api/clip", methods=["POST"])
def create_clip():
    data = request.json
    source = data.get("source")
    start = data.get("start", "00:00:00")
    end = data.get("end")
    clip_name = data.get("name", "clip").strip()
    audio_index = data.get("audio_index")
    job_id = str(uuid.uuid4())

    if not source or not os.path.isfile(source):
        return jsonify({"error": "Source file not found"}), 400
    if not end:
        return jsonify({"error": "End time required"}), 400
    if not clip_name:
        return jsonify({"error": "Clip name required"}), 400

    # Sanitize clip name
    clip_name = "".join(c for c in clip_name if c not in r'\/:*?"<>|')

    # Create clips/ folder next to the source file
    source_dir = os.path.dirname(source)
    clips_dir = os.path.join(source_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    ext = os.path.splitext(source)[1].lower() or ".mkv"
    output_path = os.path.join(clips_dir, f"{clip_name}{ext}")

    # Audio track selection (optional). If a specific track is chosen, keep only
    # the first video stream + that audio track; otherwise keep all streams.
    if audio_index is not None and str(audio_index).strip().isdigit():
        map_args = ["-map", "0:v:0", "-map", f"0:{int(audio_index)}"]
    else:
        map_args = ["-map", "0"]

    jobs[job_id] = {"status": "running", "output": output_path}

    def run_ffmpeg():
        cmd = [
            "ffmpeg", "-y",
            "-i", source,
            "-ss", start,
            "-to", end,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            *map_args,
            output_path
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode == 0:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["size"] = os.path.getsize(output_path)
            else:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = result.stderr[-500:] if result.stderr else "Unknown error"
        except Exception as e:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)

    thread = threading.Thread(target=run_ffmpeg, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/job/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/trailer/download", methods=["POST"])
def trailer_download():
    data = request.json or {}
    folder = safe_path(data.get("folder", ""))
    url = (data.get("url") or "").strip()

    if not folder or not os.path.isdir(folder):
        return jsonify({"error": "Folder not found"}), 400
    if not url:
        return jsonify({"error": "URL required"}), 400

    trailers_dir = os.path.join(folder, "trailers")
    os.makedirs(trailers_dir, exist_ok=True)

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running", "progress": "0%"}

    def run_download():
        try:
            import yt_dlp

            def hook(d):
                if d.get("status") == "downloading":
                    jobs[job_id]["progress"] = (d.get("_percent_str") or "").strip()
                elif d.get("status") == "finished":
                    jobs[job_id]["progress"] = "100%"

            ydl_opts = {
                "format": "bestvideo+bestaudio",
                "merge_output_format": "mkv",
                "outtmpl": os.path.join(trailers_dir, "%(title)s.%(ext)s"),
                "progress_hooks": [hook],
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            jobs[job_id]["status"] = "done"
        except Exception as e:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)[-500:]

    threading.Thread(target=run_download, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/files")
def list_files():
    """List files inside a folder's clips/ and trailers/ subfolders."""
    folder = safe_path(request.args.get("folder", ""))
    if not folder or not os.path.isdir(folder):
        return jsonify({"error": "Folder not found"}), 400

    def list_sub(sub):
        d = os.path.join(folder, sub)
        out = []
        if os.path.isdir(d):
            for name in sorted(os.listdir(d), key=str.lower):
                full = os.path.join(d, name)
                if os.path.isfile(full):
                    out.append({"name": name, "size": os.path.getsize(full), "path": full})
        return out

    return jsonify({"clips": list_sub("clips"), "trailers": list_sub("trailers")})


@app.route("/api/file", methods=["DELETE"])
def delete_file():
    data = request.json or {}
    path = safe_path(data.get("path", ""))
    if not path or not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404

    # Only files directly inside a clips/ or trailers/ subfolder may be deleted
    parent = os.path.basename(os.path.dirname(path))
    if parent not in ("clips", "trailers"):
        return jsonify({"error": "Only files in clips/ or trailers/ may be deleted"}), 403

    try:
        os.remove(path)
    except OSError as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
