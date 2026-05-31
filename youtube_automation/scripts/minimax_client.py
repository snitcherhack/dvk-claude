#!/usr/bin/env python3
"""
MiniMax Video Generation Client - YouTube Pipeline.

Genera clips de video vía API MiniMax/Hailuo a partir de un JSON de proyecto,
hace polling hasta completar y descarga los MP4 a proyecto/clips/.

Uso:
    python scripts/minimax_client.py proyectos/video3_ia/minimax.json
    python scripts/minimax_client.py proyectos/video3_ia/minimax.json --dry-run
    python scripts/minimax_client.py proyectos/video3_ia/minimax.json --poll 15 --max-attempts 40

Formato JSON de entrada (minimax.json):
{
  "project": "video3_ia",
  "model": "MiniMax-Hailuo-2.3-Fast",
  "resolution": "1080P",
  "scenes": [
    {"id": "intro_1", "prompt": "A glowing AI brain...", "duration": 6}
  ]
}

API key: variable de entorno MINIMAX_API_KEY o .env del proyecto.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path


# -- Constants ----------------------------------------------------------------
API_BASE = "https://api.minimax.io"
VIDEO_GENERATION_URL = f"{API_BASE}/v1/video_generation"
QUERY_URL = f"{API_BASE}/v1/query/video_generation"
FILE_URL_ENDPOINT = f"{API_BASE}/v1/files"
DEFAULT_POLL_INTERVAL = 10   # seconds
DEFAULT_MAX_ATTEMPTS = 60    # ~10 minutes at 10s interval
DEFAULT_MODEL = "MiniMax-Hailuo-2.3-Fast"
DEFAULT_RESOLUTION = "1080P"
DEFAULT_DURATION = 6         # único disponible en Hailuo 2.3

# -- Helpers -----------------------------------------------------------------


def load_api_key(project_dir: Path) -> str:
    """Load API key from env var or project .env file."""
    key = os.environ.get("MINIMAX_API_KEY", "")
    if key:
        return key

    env_path = project_dir / ".env"
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("MINIMAX_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")

    return ""


def api_request(method: str, url: str, api_key: str,
                body: dict = None, description: str = "") -> dict:
    """Make an HTTP request to MiniMax API. Returns parsed JSON or exits."""
    data = None
    if body:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        print(f"\n  ERROR [{description}]: HTTP {e.code}")
        print(f"  {error_body[:600]}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"\n  ERROR [{description}]: {e.reason}")
        sys.exit(1)


def download_file(url: str, dest: Path, api_key: str) -> None:
    """Download a file from MiniMax file URL to local path."""
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {api_key}")

    with urllib.request.urlopen(req, timeout=120) as resp:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(resp.read())

    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f"    Downloaded: {dest.name} ({size_mb:.1f} MB)")


# -- Core Logic ---------------------------------------------------------------


def create_task(api_key: str, scene: dict, model: str, resolution: str) -> str:
    """Submit video generation task. Returns task_id."""
    prompt = scene.get("prompt", "")
    duration = scene.get("duration", DEFAULT_DURATION)

    print(f"  [{scene['id']}] Submitting... ({len(prompt)} chars prompt)")

    body = {
        "model": model,
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
    }

    result = api_request("POST", VIDEO_GENERATION_URL, api_key, body,
                         f"Create {scene['id']}")

    task_id = result.get("task_id", "")
    if not task_id:
        print(f"  ERROR: No task_id in response: {json.dumps(result, indent=2)[:400]}")
        sys.exit(1)

    base = result.get("base_resp", {})
    status_code = base.get("status_code", 0)
    print(f"    Task: {task_id}  status_code={status_code}")

    return task_id


def poll_task(api_key: str, scene_id: str, task_id: str,
              poll_interval: int, max_attempts: int) -> dict:
    """Poll task status until completion. Returns final response dict."""
    for attempt in range(1, max_attempts + 1):
        time.sleep(poll_interval)

        result = api_request("GET", f"{QUERY_URL}?task_id={task_id}",
                            api_key, description=f"Poll {scene_id} #{attempt}")

        status = result.get("status", "Unknown")
        print(f"    [{scene_id}] attempt {attempt}/{max_attempts} -> {status}")

        if status == "Success":
            return result
        elif status == "Failed":
            print(f"    ERROR: Task {task_id} failed.")
            print(f"    {json.dumps(result, indent=2)[:500]}")
            sys.exit(1)
        elif status in ("Queueing", "Processing"):
            continue
        else:
            print(f"    Unknown status: {status}, continuing...")

    print(f"  ERROR: {scene_id} timed out after {max_attempts} attempts "
          f"({max_attempts * poll_interval}s)")
    sys.exit(1)


def get_download_url(api_key: str, file_id: str) -> str:
    """Get presigned download URL for a file."""
    result = api_request("GET", f"{FILE_URL_ENDPOINT}/{file_id}/url",
                        api_key, description=f"Get URL for {file_id}")
    return result.get("download_url", "")


def process_scene(api_key: str, scene: dict, clips_dir: Path,
                  model: str, resolution: str,
                  poll_interval: int, max_attempts: int,
                  resume: bool) -> dict:
    """Process a single scene: create -> poll -> download -> update state."""
    scene_id = scene["id"]
    mp4_path = clips_dir / f"{scene_id}.mp4"

    # -- Resume: skip already completed --
    if resume and mp4_path.exists():
        print(f"  [{scene_id}] Already exists ({mp4_path.stat().st_size / 1024:.0f} KB) -> skip")
        return scene

    if resume and scene.get("status") == "Success" and scene.get("download_url"):
        print(f"  [{scene_id}] Marked Success, downloading...")
        download_file(scene["download_url"], mp4_path, api_key)
        scene["local_path"] = str(mp4_path)
        return scene

    # -- Resume: jump to polling if task already created --
    task_id = scene.get("task_id", "")
    if not task_id:
        task_id = create_task(api_key, scene, model, resolution)
        scene["task_id"] = task_id

    # -- Poll until done --
    if scene.get("status") != "Success" or not scene.get("download_url"):
        result = poll_task(api_key, scene_id, task_id, poll_interval, max_attempts)
        scene["status"] = result.get("status", "Success")
        file_id = result.get("file_id", "")
        if file_id:
            scene["file_id"] = file_id
            download_url = get_download_url(api_key, file_id)
            scene["download_url"] = download_url
        else:
            print(f"    WARNING: No file_id in response, trying raw result...")
            download_url = result.get("download_url", "")
            if download_url:
                scene["download_url"] = download_url

    # -- Download --
    download_url = scene.get("download_url", "")
    if download_url:
        download_file(download_url, mp4_path, api_key)
        scene["local_path"] = str(mp4_path)
    else:
        print(f"    WARNING: No download_url for {scene_id}")

    return scene


# -- Entry Point --------------------------------------------------------------


def minimax_client(json_path: str, poll_interval: int = DEFAULT_POLL_INTERVAL,
                   max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                   dry_run: bool = False, resume: bool = True):
    """Run the MiniMax video generation pipeline."""
    json_path = Path(json_path).resolve()

    if not json_path.exists():
        print(f"ERROR: File not found: {json_path}")
        sys.exit(1)

    with open(json_path, encoding="utf-8") as f:
        config = json.load(f)

    project_name = config.get("project", json_path.parent.name)
    model = config.get("model", DEFAULT_MODEL)
    resolution = config.get("resolution", DEFAULT_RESOLUTION)
    scenes = config.get("scenes", [])

    if not scenes:
        print("ERROR: No scenes in config")
        sys.exit(1)

    project_dir = json_path.parent
    clips_dir = project_dir / "clips"
    api_key = load_api_key(project_dir)

    # -- Print summary --
    sep = "=" * 50
    print(sep)
    print("  MiniMax Video Generation Client")
    print(sep)
    print(f"  Project:    {project_name}")
    print(f"  Model:      {model}")
    print(f"  Resolution: {resolution}")
    print(f"  Scenes:     {len(scenes)}")
    print(f"  Output:     {clips_dir}")
    print(f"  Poll:       {poll_interval}s x {max_attempts} attempts")
    if dry_run:
        print(f"  DRY RUN -- no API calls")
    print(sep)

    if dry_run:
        print("\n-- Validation --")
        for scene in scenes:
            sid = scene.get("id", "???")
            prompt = scene.get("prompt", "")
            duration = scene.get("duration", DEFAULT_DURATION)
            print(f"  [{sid}] dur={duration}s prompt={len(prompt)} chars "
                  f"-> {clips_dir / f'{sid}.mp4'}")
        print(f"\n  OK - {len(scenes)} scenes would be generated. "
              f"Remove --dry-run to proceed.")
        return

    if not api_key:
        print("ERROR: MINIMAX_API_KEY not set (env var or proyecto/.env)")
        sys.exit(1)

    # -- Process each scene --
    t_start = time.time()
    clips_dir.mkdir(parents=True, exist_ok=True)

    for i, scene in enumerate(scenes):
        print(f"\n-- Scene {i + 1}/{len(scenes)}: {scene['id']} --")
        try:
            scene = process_scene(
                api_key, scene, clips_dir, model, resolution,
                poll_interval, max_attempts, resume
            )
        except KeyboardInterrupt:
            print(f"\n  Interrupted. Saving progress...")
            break

    # -- Save updated state --
    config["scenes"] = scenes
    config["_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"\n  State saved to {json_path}")

    elapsed = time.time() - t_start
    completed = sum(1 for s in scenes if s.get("status") == "Success")
    print(f"\n-- Done in {elapsed:.0f}s --")
    print(f"  Completed: {completed}/{len(scenes)} scenes")
    print(f"  Clips:     {clips_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="MiniMax Video Generation - YouTube Pipeline"
    )
    parser.add_argument("json_config", help="Path to minimax.json project config")
    parser.add_argument("--poll", type=int, default=DEFAULT_POLL_INTERVAL,
                        help=f"Polling interval in seconds (default {DEFAULT_POLL_INTERVAL})")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
                        help=f"Max polling attempts (default {DEFAULT_MAX_ATTEMPTS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate JSON without calling API")
    parser.add_argument("--no-resume", action="store_true",
                        help="Don't skip completed scenes (re-run all)")
    args = parser.parse_args()

    minimax_client(
        json_path=args.json_config,
        poll_interval=args.poll,
        max_attempts=args.max_attempts,
        dry_run=args.dry_run,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    main()
