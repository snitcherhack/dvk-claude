#!/usr/bin/env python3
"""
Tests for minimax_client.py — mocks urllib to validate all flows without API calls.

Usage:
    cd youtube_automation
    python scripts/test_minimax_client.py
"""

import json
import sys
import io
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add scripts/ to path so we can import minimax_client
sys.path.insert(0, str(Path(__file__).resolve().parent))
import minimax_client as mc


# ── Test Helpers ──────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, data, status=200):
        if isinstance(data, bytes):
            self._data = data
        else:
            self._data = json.dumps(data).encode("utf-8")
        self.status = status

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def make_urlopen(mock_urlopen, responses: list):
    """Make urlopen return a sequence of FakeResponses."""
    call_count = [0]

    def _urlopen(req, timeout=30):
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(responses):
            resp = responses[idx]
            if isinstance(resp, Exception):
                raise resp
            return resp
        raise RuntimeError(f"Unexpected API call #{idx}")

    mock_urlopen.side_effect = _urlopen


# ── Tests ─────────────────────────────────────────────────────────────────

def test_dry_run():
    """--dry-run validates JSON without needing API key."""
    print("TEST: --dry-run")
    json_path = Path(__file__).resolve().parent.parent / "proyectos" / "video3_ia" / "minimax.json"
    if not json_path.exists():
        print(f"  SKIP: {json_path} not found")
        return

    # Should not call API, should not fail on missing key
    mc.minimax_client(str(json_path), dry_run=True)
    print("  PASS")


def test_api_key_from_env():
    """Loads API key from environment."""
    print("TEST: API key from env var")
    with patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key-123"}):
        key = mc.load_api_key(Path("/tmp"))
    assert key == "test-key-123", f"Expected test-key-123, got {key}"
    print("  PASS")


def test_api_key_from_dotenv():
    """Loads API key from project .env file."""
    print("TEST: API key from .env file")
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_path.write_text('MINIMAX_API_KEY="dotenv-key-456"\nOTHER=thing\n')
        key = mc.load_api_key(Path(tmp))
    assert key == "dotenv-key-456", f"Expected dotenv-key-456, got {key}"
    print("  PASS")


def test_api_key_missing():
    """Returns empty string when no key found."""
    print("TEST: API key missing")
    with patch.dict(os.environ, {}, clear=True):
        with tempfile.TemporaryDirectory() as tmp:
            key = mc.load_api_key(Path(tmp))
    assert key == "", f"Expected empty, got {key}"
    print("  PASS")


@patch("minimax_client.urllib.request.urlopen")
def test_create_task(mock_urlopen):
    """POST /v1/video_generation returns task_id."""
    print("TEST: Create task")
    scene = {"id": "test_1", "prompt": "A test prompt", "duration": 6}
    make_urlopen(mock_urlopen, [
        FakeResponse({"task_id": "task-abc-123", "base_resp": {"status_code": 0}}),
    ])

    task_id = mc.create_task("fake-key", scene, "MiniMax-Hailuo-2.3-Fast", "1080P")
    assert task_id == "task-abc-123", f"Got {task_id}"
    print("  PASS")


@patch("minimax_client.urllib.request.urlopen")
def test_poll_until_success(mock_urlopen):
    """Polling returns result when status=Success."""
    print("TEST: Poll until Success")
    make_urlopen(mock_urlopen, [
        FakeResponse({"status": "Queueing"}),
        FakeResponse({"status": "Processing"}),
        FakeResponse({"status": "Processing"}),
        FakeResponse({"status": "Success", "file_id": "file-xyz"}),
    ])

    result = mc.poll_task("fake-key", "test_1", "task-abc", poll_interval=0, max_attempts=5)
    assert result["status"] == "Success"
    assert result["file_id"] == "file-xyz"
    print("  PASS")


@patch("minimax_client.urllib.request.urlopen")
def test_poll_failed(mock_urlopen):
    """Polling exits on status=Failed."""
    print("TEST: Poll Failed exits")
    make_urlopen(mock_urlopen, [
        FakeResponse({"status": "Queueing"}),
        FakeResponse({"status": "Failed", "error": "bad prompt"}),
    ])

    try:
        mc.poll_task("fake-key", "test_1", "task-abc", poll_interval=0, max_attempts=5)
        assert False, "Should have exited"
    except SystemExit as e:
        assert e.code == 1, f"Exit code {e.code}"
    print("  PASS")


@patch("minimax_client.urllib.request.urlopen")
def test_get_download_url(mock_urlopen):
    """GET /v1/files/{id}/url returns download_url."""
    print("TEST: Get download URL")
    make_urlopen(mock_urlopen, [
        FakeResponse({"download_url": "https://cdn.minimax.io/videos/clip.mp4"}),
    ])

    url = mc.get_download_url("fake-key", "file-xyz")
    assert "cdn.minimax.io" in url
    print("  PASS")


@patch("minimax_client.urllib.request.urlopen")
def test_process_scene_full_flow(mock_urlopen):
    """Full scene flow: create -> poll -> get_url -> download."""
    print("TEST: Full scene flow (mock)")
    scene = {"id": "full_1", "prompt": "Cinematic drone shot", "duration": 6}

    with tempfile.TemporaryDirectory() as tmp:
        clips_dir = Path(tmp) / "clips"
        clips_dir.mkdir()

        make_urlopen(mock_urlopen, [
            # Create task
            FakeResponse({"task_id": "task-full-001", "base_resp": {"status_code": 0}}),
            # Poll
            FakeResponse({"status": "Queueing"}),
            FakeResponse({"status": "Success", "file_id": "file-full-001"}),
            # Get download URL
            FakeResponse({"download_url": "https://cdn.example.com/full_1.mp4"}),
            # Download (raw bytes for "video" content)
            FakeResponse(b"fake-mp4-bytes"),
        ])

        result = mc.process_scene(
            "fake-key", scene, clips_dir,
            "MiniMax-Hailuo-2.3-Fast", "1080P",
            poll_interval=0, max_attempts=5, resume=False
        )

        assert result["task_id"] == "task-full-001"
        assert result["status"] == "Success"
        assert result["file_id"] == "file-full-001"
        assert "download_url" in result
        assert Path(result["local_path"]).exists()
        assert Path(result["local_path"]).stat().st_size == len(b"fake-mp4-bytes")
        print("  PASS")


@patch("minimax_client.urllib.request.urlopen")
def test_process_scene_resume_skip(mock_urlopen):
    """Resume: skips scene when MP4 already exists."""
    print("TEST: Resume skip existing MP4")
    scene = {"id": "skip_1", "prompt": "whatever"}

    with tempfile.TemporaryDirectory() as tmp:
        clips_dir = Path(tmp) / "clips"
        clips_dir.mkdir()
        # Create a fake existing file
        (clips_dir / "skip_1.mp4").write_bytes(b"existing-video")

        # Should make NO API calls
        mock_urlopen.side_effect = RuntimeError("Should not call API")

        result = mc.process_scene(
            "fake-key", scene, clips_dir,
            "MiniMax-Hailuo-2.3-Fast", "1080P",
            poll_interval=0, max_attempts=5, resume=True
        )

        # Scene unchanged, no new task_id
        assert "task_id" not in result
        print("  PASS")


@patch("minimax_client.urllib.request.urlopen")
def test_process_scene_resume_polling(mock_urlopen):
    """Resume: jumps to polling when task_id exists but not done."""
    print("TEST: Resume jump to polling")
    scene = {"id": "resume_1", "prompt": "test", "task_id": "existing-task"}

    with tempfile.TemporaryDirectory() as tmp:
        clips_dir = Path(tmp) / "clips"
        clips_dir.mkdir()

        make_urlopen(mock_urlopen, [
            # Poll (already had task_id, so no create call)
            FakeResponse({"status": "Success", "file_id": "file-resume-001"}),
            # Get download URL
            FakeResponse({"download_url": "https://cdn.example.com/resume_1.mp4"}),
            # Download
            FakeResponse(b"resumed-video-bytes"),
        ])

        result = mc.process_scene(
            "fake-key", scene, clips_dir,
            "MiniMax-Hailuo-2.3-Fast", "1080P",
            poll_interval=0, max_attempts=5, resume=True
        )

        assert result["status"] == "Success"
        assert Path(result["local_path"]).exists()
        print("  PASS")


@patch("minimax_client.urllib.request.urlopen")
def test_resume_marked_success(mock_urlopen):
    """Resume: downloads directly when status=Success + download_url present."""
    print("TEST: Resume download from saved state")
    scene = {
        "id": "download_1",
        "prompt": "test",
        "task_id": "old-task",
        "status": "Success",
        "download_url": "https://cdn.example.com/already_done.mp4"
    }

    with tempfile.TemporaryDirectory() as tmp:
        clips_dir = Path(tmp) / "clips"
        clips_dir.mkdir()

        make_urlopen(mock_urlopen, [
            FakeResponse(b"cached-video-content"),
        ])

        result = mc.process_scene(
            "fake-key", scene, clips_dir,
            "MiniMax-Hailuo-2.3-Fast", "1080P",
            poll_interval=0, max_attempts=5, resume=True
        )

        assert Path(result["local_path"]).exists()
        print("  PASS")


# ── Runner ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("  MiniMax Client Tests")
    print("=" * 50)

    tests = [
        test_dry_run,
        test_api_key_from_env,
        test_api_key_from_dotenv,
        test_api_key_missing,
        test_create_task,
        test_poll_until_success,
        test_poll_failed,
        test_get_download_url,
        test_process_scene_full_flow,
        test_process_scene_resume_skip,
        test_process_scene_resume_polling,
        test_resume_marked_success,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'=' * 50}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'=' * 50}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
