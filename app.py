import os
import subprocess
import json
from flask import Flask, request, jsonify, render_template, Response
import threading
import uuid
import time

app = Flask(__name__)

MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "/media")
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".wmv", ".flv", ".webm"}

# Store job progress
jobs = {}


def is_video(filename):
    return os.path.splitext(filename)[1].lower() in VIDEO_EXTENSIONS


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/browse")
def browse():
    path = request.args.get("path", MEDIA_ROOT)
    path = os.path.normpath(path)

    # Security: don't allow traversal outside MEDIA_ROOT
    if not path.startswith(MEDIA_ROOT):
        path = MEDIA_ROOT

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

    jobs[job_id] = {"status": "running", "output": output_path}

    def run_ffmpeg():
        cmd = [
            "ffmpeg", "-y",
            "-ss", start,
            "-to", end,
            "-i", source,
            "-c", "copy",
            "-map", "0",
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
