"""services/youtube.py: yt-dlp command building, parsing, size probe, self-update."""

import json
import os
from types import SimpleNamespace

import pytest

from services import youtube as yt


class FakeProc:
    def __init__(self, out=b"", err=b"", rc=0, on_communicate=None):
        self._out, self._err, self.returncode = out, err, rc
        self._hook = on_communicate

    async def communicate(self):
        if self._hook:
            self._hook()
        return self._out, self._err

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


@pytest.fixture
def spawned(monkeypatch):
    """Capture every create_subprocess_exec call; respond via a settable factory."""
    calls = []
    state = SimpleNamespace(factory=lambda args: FakeProc())

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        return state.factory(list(args))

    monkeypatch.setattr(yt.asyncio, "create_subprocess_exec", fake_exec)
    state.calls = calls
    return state


@pytest.fixture
def fixed_uid(monkeypatch):
    monkeypatch.setattr(yt.uuid, "uuid4", lambda: SimpleNamespace(hex="fixeduid"))
    return "fixeduid"


# ── ytdlp_error ────────────────────────────────────────────────────────────

class TestYtdlpError:
    def test_keeps_only_error_lines(self):
        err = b"WARNING: old\n[generic] x\nERROR: Unsupported URL: http://a\nERROR: second\n"
        assert yt.ytdlp_error(err) == "ERROR: Unsupported URL: http://a | ERROR: second"

    def test_falls_back_to_last_three_lines(self):
        err = b"a\nb\nc\nd\n"
        assert yt.ytdlp_error(err) == "b | c | d"

    def test_clips_long_lines(self):
        out = yt.ytdlp_error(b"ERROR: " + b"x" * 1000, limit=20)
        assert out == "ERROR: " + "x" * 13 + "…"

    def test_line_exactly_at_limit_not_clipped(self):
        line = "ERROR: " + "x" * 13
        assert yt.ytdlp_error(line.encode(), limit=20) == line

    def test_invalid_utf8_does_not_crash(self):
        assert yt.ytdlp_error(b"ERROR: \xff\xfe bad") .startswith("ERROR: ")


# ── format args ────────────────────────────────────────────────────────────

class TestVideoFormatArgs:
    def test_best(self):
        assert yt._video_format_args(None) == ["-f", "bv*+ba/b"]

    def test_capped_by_smaller_side(self):
        assert yt._video_format_args(720) == ["-f", "bv*+ba/b", "-S", "res:720"]


# ── search ─────────────────────────────────────────────────────────────────

class TestSearch:
    async def test_parses_flat_playlist_json(self, spawned):
        lines = [
            {"title": "Believer", "uploader": "ImagineDragonsVEVO", "duration": 204.6, "id": "abc"},
            {"title": "No uploader", "channel": "Chan", "duration": None, "url": "u2"},
        ]
        spawned.factory = lambda a: FakeProc(out=("\n".join(json.dumps(x) for x in lines) + "\nnot json\n").encode())
        tracks = await yt.search("believer", count=5)
        assert spawned.calls[0][:2] == ["yt-dlp", "ytsearch5:believer"]
        assert [t.title for t in tracks] == ["Believer", "No uploader"]
        assert tracks[0].artist == "ImagineDragonsVEVO" and tracks[0].duration == 204
        assert tracks[0].source == "youtube" and tracks[0].source_id == "abc" and tracks[0].bitrate == 160
        assert tracks[1].artist == "Chan" and tracks[1].duration == 0 and tracks[1].source_id == "u2"

    async def test_empty_output(self, spawned):
        spawned.factory = lambda a: FakeProc(out=b"")
        assert await yt.search("x") == []


# ── downloads ──────────────────────────────────────────────────────────────

class TestDownloadFromUrl:
    async def test_success_returns_mp3_and_passes_flags(self, spawned, tmp_path, fixed_uid):
        def make(args):
            return FakeProc(on_communicate=lambda: (tmp_path / f"{fixed_uid}.mp3").write_bytes(b"ID3"))
        spawned.factory = make
        path = await yt.download_from_url("https://www.tiktok.com/@u/video/1", download_dir=tmp_path)
        assert path == str(tmp_path / f"{fixed_uid}.mp3")
        cmd = spawned.calls[0]
        assert cmd[0] == "yt-dlp" and cmd[-1] == "https://www.tiktok.com/@u/video/1"
        assert "youtube:player_client=default,android_vr" in cmd
        assert cmd[cmd.index("--audio-format") + 1] == "mp3"

    async def test_failure_returns_none(self, spawned, tmp_path, fixed_uid):
        spawned.factory = lambda a: FakeProc(rc=1, err=b"ERROR: nope")
        assert await yt.download_from_url("https://x", download_dir=tmp_path) is None

    async def test_empty_file_is_failure(self, spawned, tmp_path, fixed_uid):
        spawned.factory = lambda a: FakeProc(on_communicate=lambda: (tmp_path / f"{fixed_uid}.mp3").write_bytes(b""))
        assert await yt.download_from_url("https://x", download_dir=tmp_path) is None

    async def test_cookies_passed_when_present(self, spawned, tmp_path, fixed_uid, tmp_settings):
        tmp_settings.cookies_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_settings.cookies_path.write_text("# Netscape")
        spawned.factory = lambda a: FakeProc(rc=1)
        await yt.download_from_url("https://x", download_dir=tmp_path)
        cmd = spawned.calls[0]
        assert cmd[cmd.index("--cookies") + 1] == str(tmp_settings.cookies_path)


class TestDownloadVideo:
    async def test_uses_shared_format_args_and_finds_any_extension(self, spawned, tmp_path, fixed_uid):
        spawned.factory = lambda a: FakeProc(on_communicate=lambda: (tmp_path / f"{fixed_uid}.mkv").write_bytes(b"v"))
        path = await yt.download_video_from_url("https://x", max_height=720, download_dir=tmp_path)
        assert path == str(tmp_path / f"{fixed_uid}.mkv")
        cmd = spawned.calls[0]
        assert cmd[1:5] == ["-f", "bv*+ba/b", "-S", "res:720"]
        assert cmd[cmd.index("--merge-output-format") + 1] == "mp4"

    async def test_failure(self, spawned, tmp_path, fixed_uid):
        spawned.factory = lambda a: FakeProc(rc=1)
        assert await yt.download_video_from_url("https://x", download_dir=tmp_path) is None


class TestDownloadTrack:
    async def test_mp3_quality_command(self, spawned, tmp_path, fixed_uid, make_track):
        spawned.factory = lambda a: FakeProc(on_communicate=lambda: (tmp_path / f"{fixed_uid}.mp3").write_bytes(b"a"))
        path = await yt.download(make_track(source_id="vid42"), "mp3_320", download_dir=tmp_path)
        assert path == str(tmp_path / f"{fixed_uid}.mp3")
        cmd = spawned.calls[0]
        assert cmd[-1] == "https://www.youtube.com/watch?v=vid42"
        assert "ffmpeg:-b:a 320k" in cmd

    async def test_flac_quality_command(self, spawned, tmp_path, fixed_uid, make_track):
        spawned.factory = lambda a: FakeProc(on_communicate=lambda: (tmp_path / f"{fixed_uid}.flac").write_bytes(b"a"))
        path = await yt.download(make_track(), "flac", download_dir=tmp_path)
        assert path.endswith(".flac")
        cmd = spawned.calls[0]
        assert cmd[cmd.index("--audio-format") + 1] == "flac"


# ── probe_video_sizes ──────────────────────────────────────────────────────

class TestProbeVideoSizes:
    async def test_estimates_from_offline_selection(self, monkeypatch, tmp_settings):
        seen_info_paths = []

        async def fake_run(*cmd, timeout=300):
            if "-J" in cmd:
                return 0, json.dumps({"duration": 100}).encode(), b""
            info = cmd[cmd.index("--load-info-json") + 1]
            seen_info_paths.append(info)
            assert os.path.exists(info)
            if "res:360" in cmd:
                return 0, b"1000 NA\n", b""
            if "res:720" in cmd:
                return 0, b"NA 800\n", b""          # HLS: 800 kbit/s * 125 * 100 s
            return 0, b"NA NA\n", b""

        monkeypatch.setattr(yt, "_run", fake_run)
        sizes = await yt.probe_video_sizes("https://x")
        assert sizes == {"duration": 100, "360": 1000, "720": 800 * 125 * 100, "best": 0}
        assert len(seen_info_paths) == 3 and not os.path.exists(seen_info_paths[0])

    async def test_metadata_failure_returns_none(self, monkeypatch, tmp_settings):
        async def fake_run(*cmd, timeout=300):
            return 1, b"", b"ERROR"
        monkeypatch.setattr(yt, "_run", fake_run)
        assert await yt.probe_video_sizes("https://x") is None

    async def test_bad_json_returns_none(self, monkeypatch, tmp_settings):
        async def fake_run(*cmd, timeout=300):
            return 0, b"{not json", b""
        monkeypatch.setattr(yt, "_run", fake_run)
        assert await yt.probe_video_sizes("https://x") is None

    async def test_selection_failure_is_zero(self, monkeypatch, tmp_settings):
        async def fake_run(*cmd, timeout=300):
            if "-J" in cmd:
                return 0, b'{"duration": 10}', b""
            return 1, b"", b""
        monkeypatch.setattr(yt, "_run", fake_run)
        assert await yt.probe_video_sizes("https://x") == {"duration": 10, "360": 0, "720": 0, "best": 0}


# ── _run ───────────────────────────────────────────────────────────────────

class TestRun:
    async def test_returns_code_and_output(self, spawned):
        spawned.factory = lambda a: FakeProc(out=b"o", err=b"e", rc=3)
        assert await yt._run("x") == (3, b"o", b"e")

    async def test_timeout_kills(self, monkeypatch):
        killed = []

        class Slow(FakeProc):
            async def communicate(self):
                import asyncio
                await asyncio.sleep(10)

            def kill(self):
                killed.append(True)

        async def fake_exec(*a, **k):
            return Slow()
        monkeypatch.setattr(yt.asyncio, "create_subprocess_exec", fake_exec)
        assert await yt._run("x", timeout=0.01) == (-1, b"", b"timed out")
        assert killed == [True]


# ── update_ytdlp ───────────────────────────────────────────────────────────

@pytest.fixture
def ytdlp_root(tmp_path, monkeypatch):
    root = tmp_path / "ytdlp"
    (root / "base" / "bin").mkdir(parents=True)
    (root / "old1").mkdir()                  # stale venv from an earlier upgrade
    (root / "current").symlink_to("base")
    monkeypatch.setattr(yt.settings, "ytdlp_dir", str(root))
    monkeypatch.setattr(yt.settings, "ytdlp_channel", "nightly")
    monkeypatch.setattr(yt.time, "time", lambda: 1700000000)

    async def fake_version():
        return "2026.01.01"
    monkeypatch.setattr(yt, "ytdlp_version", fake_version)
    return root


def _report(*names):
    return json.dumps({"install": [{"metadata": {"name": n}} for n in names]}).encode()


class TestUpdateYtdlp:
    async def test_disabled_without_dir(self, monkeypatch):
        monkeypatch.setattr(yt.settings, "ytdlp_dir", "")
        assert await yt.update_ytdlp() is None

    async def test_missing_current_link(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yt.settings, "ytdlp_dir", str(tmp_path))
        assert await yt.update_ytdlp() is None

    async def test_up_to_date_does_nothing(self, ytdlp_root, monkeypatch):
        calls = []

        async def fake_run(*cmd, timeout=300):
            calls.append(cmd)
            return 0, _report("certifi"), b""
        monkeypatch.setattr(yt, "_run", fake_run)
        assert await yt.update_ytdlp() is None
        assert len(calls) == 1 and "--dry-run" in calls[0] and "--pre" in calls[0]
        assert os.readlink(ytdlp_root / "current") == "base"

    async def test_check_failure(self, ytdlp_root, monkeypatch):
        async def fake_run(*cmd, timeout=300):
            return 1, b"", b"network down"
        monkeypatch.setattr(yt, "_run", fake_run)
        assert await yt.update_ytdlp() is None

    async def test_upgrade_swaps_link_and_prunes(self, ytdlp_root, monkeypatch):
        calls = []

        async def fake_run(*cmd, timeout=300):
            calls.append(cmd)
            if "--dry-run" in cmd:
                return 0, _report("yt_dlp"), b""   # underscore spelling is normalised
            if cmd[1:3] == ("-m", "venv"):
                (ytdlp_root / "v1700000000" / "bin").mkdir(parents=True)
                return 0, b"", b""
            if cmd[-1] == "--version":
                return 0, b"2026.10.23\n", b""
            return 0, b"", b""
        monkeypatch.setattr(yt, "_run", fake_run)

        assert await yt.update_ytdlp() == ("2026.01.01", "2026.10.23")
        assert os.readlink(ytdlp_root / "current") == "v1700000000"
        remaining = sorted(p.name for p in ytdlp_root.iterdir())
        assert remaining == ["base", "current", "v1700000000"]   # stale old1 pruned, previous kept
        install = next(c for c in calls if c[1] == "install" and "--dry-run" not in c)
        assert install[0] == str(ytdlp_root / "v1700000000" / "bin" / "pip")
        assert "--pre" in install and install[-1] == "yt-dlp[default]"

    async def test_stable_channel_has_no_pre(self, ytdlp_root, monkeypatch):
        monkeypatch.setattr(yt.settings, "ytdlp_channel", "stable")
        calls = []

        async def fake_run(*cmd, timeout=300):
            calls.append(cmd)
            return 0, _report(), b""
        monkeypatch.setattr(yt, "_run", fake_run)
        await yt.update_ytdlp()
        assert "--pre" not in calls[0]

    async def test_failed_install_cleans_up_and_keeps_current(self, ytdlp_root, monkeypatch):
        async def fake_run(*cmd, timeout=300):
            if "--dry-run" in cmd:
                return 0, _report("yt-dlp"), b""
            if cmd[1:3] == ("-m", "venv"):
                (ytdlp_root / "v1700000000").mkdir()
                return 0, b"", b""
            return 1, b"", b"pip exploded"
        monkeypatch.setattr(yt, "_run", fake_run)
        assert await yt.update_ytdlp() is None
        assert os.readlink(ytdlp_root / "current") == "base"
        assert not (ytdlp_root / "v1700000000").exists()


# ── hardening ──────────────────────────────────────────────────────────────

class TestHardening:
    async def test_every_download_refuses_live_streams(self, spawned, tmp_path, fixed_uid, make_track):
        spawned.factory = lambda a: FakeProc(rc=1)
        await yt.download(make_track(), "mp3_320", download_dir=tmp_path)
        await yt.download(make_track(), "flac", download_dir=tmp_path)
        await yt.download_from_url("https://x", download_dir=tmp_path)
        await yt.download_video_from_url("https://x", download_dir=tmp_path)
        for cmd in spawned.calls:
            assert cmd[cmd.index("--match-filter") + 1] == "!is_live", cmd

    async def test_url_download_ignores_playlists(self, spawned, tmp_path, fixed_uid):
        spawned.factory = lambda a: FakeProc(rc=1)
        await yt.download_from_url("https://youtube.com/watch?v=X&list=PL1", download_dir=tmp_path)
        assert "--no-playlist" in spawned.calls[0]

    async def test_url_is_last_argument(self, spawned, tmp_path, fixed_uid):
        spawned.factory = lambda a: FakeProc(rc=1)
        await yt.download_from_url("https://a", download_dir=tmp_path)
        await yt.download_video_from_url("https://b", download_dir=tmp_path)
        assert [c[-1] for c in spawned.calls] == ["https://a", "https://b"]

    async def test_subprocess_gets_own_session_for_group_kill(self, monkeypatch):
        seen = {}

        async def fake_exec(*a, **kw):
            seen.update(kw)
            return FakeProc()
        monkeypatch.setattr(yt.asyncio, "create_subprocess_exec", fake_exec)
        await yt._run("x")
        assert seen["start_new_session"] is True

    async def test_cancellation_kills_child(self, monkeypatch):
        import asyncio
        killed = []

        class Hanging(FakeProc):
            async def communicate(self):
                await asyncio.sleep(10)

            def kill(self):
                killed.append(True)

        async def fake_exec(*a, **kw):
            return Hanging()
        monkeypatch.setattr(yt.asyncio, "create_subprocess_exec", fake_exec)
        task = asyncio.create_task(yt._run("yt-dlp", timeout=60))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert killed == [True]

    def test_kill_prefers_process_group(self, monkeypatch):
        calls = []
        monkeypatch.setattr(yt.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
        yt._kill(SimpleNamespace(pid=4242, kill=lambda: calls.append("kill")))
        assert calls == [(4242, yt.signal.SIGKILL)]

    def test_kill_falls_back_when_group_gone(self, monkeypatch):
        calls = []

        def gone(pid, sig):
            raise ProcessLookupError
        monkeypatch.setattr(yt.os, "killpg", gone)
        yt._kill(SimpleNamespace(pid=4242, kill=lambda: calls.append("kill")))
        assert calls == ["kill"]


class TestUpdateHealing:
    async def test_dangling_current_is_repointed(self, tmp_path, monkeypatch):
        root = tmp_path / "ytdlp"
        (root / "v1" / "bin").mkdir(parents=True)
        (root / "v1" / "bin" / "yt-dlp").write_text("#!")
        (root / "current").symlink_to("v_deleted")       # dangling
        monkeypatch.setattr(yt.settings, "ytdlp_dir", str(root))

        async def fake_run(*cmd, timeout=300):
            return 0, json.dumps({"install": []}).encode(), b""

        async def fake_version():
            return "x"
        monkeypatch.setattr(yt, "_run", fake_run)
        monkeypatch.setattr(yt, "ytdlp_version", fake_version)
        await yt.update_ytdlp()
        assert os.readlink(root / "current") == "v1"

    async def test_updates_are_serialised(self, tmp_path, monkeypatch):
        import asyncio
        active, peak = [0], [0]

        async def slow_inner():
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            await asyncio.sleep(0.02)
            active[0] -= 1
        monkeypatch.setattr(yt, "_update_ytdlp", slow_inner)
        await asyncio.gather(yt.update_ytdlp(), yt.update_ytdlp(), yt.update_ytdlp())
        assert peak[0] == 1


# ── exact yt-dlp command lines (kill flag-level mutants) ───────────────────

class TestExactCommands:
    async def test_download_from_url(self, spawned, tmp_path, fixed_uid):
        spawned.factory = lambda a: FakeProc(rc=1)
        await yt.download_from_url("https://x", download_dir=tmp_path)
        assert spawned.calls[0] == [
            "yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0", "--hls-prefer-native",
            "--fragment-retries", "10", "--retries", "3", "--no-part", "--fixup", "detect_or_warn",
            "--user-agent", yt.BROWSER_UA, "--extractor-args", "youtube:player_client=default,android_vr", "-4",
            "-o", str(tmp_path / "fixeduid.%(ext)s"), "--max-filesize", f"{yt.settings.max_file_size_mb}m",
            "--no-playlist", "--match-filter", "!is_live", "--no-progress", "https://x",
        ]

    async def test_download_mp3(self, spawned, tmp_path, fixed_uid, make_track):
        spawned.factory = lambda a: FakeProc(rc=1)
        await yt.download(make_track(source_id="v"), "mp3_320", download_dir=tmp_path)
        assert spawned.calls[0] == [
            "yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0", "--postprocessor-args", "ffmpeg:-b:a 320k",
            "--user-agent", yt.BROWSER_UA, "--extractor-args", "youtube:player_client=default,android_vr", "-4",
            "-o", str(tmp_path / "fixeduid.%(ext)s"), "--max-filesize", f"{yt.settings.max_file_size_mb}m",
            "--match-filter", "!is_live", "--no-progress", "https://www.youtube.com/watch?v=v",
        ]

    async def test_download_flac(self, spawned, tmp_path, fixed_uid, make_track):
        spawned.factory = lambda a: FakeProc(rc=1)
        await yt.download(make_track(source_id="v"), "flac", download_dir=tmp_path)
        assert spawned.calls[0] == [
            "yt-dlp", "-x", "--audio-format", "flac", "--audio-quality", "0",
            "--user-agent", yt.BROWSER_UA, "--extractor-args", "youtube:player_client=default,android_vr", "-4",
            "-o", str(tmp_path / "fixeduid.%(ext)s"), "--max-filesize", f"{yt.settings.max_file_size_mb}m",
            "--match-filter", "!is_live", "--no-progress", "https://www.youtube.com/watch?v=v",
        ]

    async def test_download_video(self, spawned, tmp_path, fixed_uid):
        spawned.factory = lambda a: FakeProc(rc=1)
        await yt.download_video_from_url("https://x", max_height=360, download_dir=tmp_path)
        limit = yt.settings.send_limit_bytes // (1024 * 1024)
        assert spawned.calls[0] == [
            "yt-dlp", "-f", "bv*+ba/b", "-S", "res:360", "--merge-output-format", "mp4", "--no-playlist",
            "--fragment-retries", "10", "--retries", "3", "--no-part",
            "--user-agent", yt.BROWSER_UA, "--extractor-args", "youtube:player_client=default,android_vr", "-4",
            "-o", str(tmp_path / "fixeduid.%(ext)s"), "--max-filesize", f"{limit}m",
            "--match-filter", "!is_live", "--no-progress", "https://x",
        ]

    async def test_search(self, spawned):
        spawned.factory = lambda a: FakeProc(out=b"")
        await yt.search("q", count=3)
        assert spawned.calls[0] == ["yt-dlp", "ytsearch3:q", "--dump-json", "--flat-playlist", "--no-download", "--quiet"]


class TestExactUpdateCommands:
    async def test_all_three_pip_steps_exactly(self, ytdlp_root, monkeypatch):
        import sys as _sys
        calls = []

        async def fake_run(*cmd, timeout=300):
            calls.append((cmd, timeout))
            if "--dry-run" in cmd:
                return 0, _report("yt-dlp"), b""
            if cmd[1:3] == ("-m", "venv"):
                (ytdlp_root / "v1700000000" / "bin").mkdir(parents=True)
            if cmd[-1] == "--version":
                return 0, b"2026.10.23\n", b""
            return 0, b"", b""
        monkeypatch.setattr(yt, "_run", fake_run)
        await yt.update_ytdlp()
        flags = ("--no-cache-dir", "--disable-pip-version-check", "--quiet")
        new = ytdlp_root / "v1700000000"
        assert calls == [
            ((str(ytdlp_root / "current" / "bin" / "pip"), "install", "-U", "--dry-run", "--report", "-",
              *flags, "--pre", "yt-dlp[default]"), 300),
            ((_sys.executable, "-m", "venv", str(new)), 300),
            ((str(new / "bin" / "pip"), "install", *flags, "--pre", "yt-dlp[default]"), 300),
            ((str(new / "bin" / "yt-dlp"), "--version"), 60),
        ]

    async def test_missing_install_key_means_up_to_date(self, ytdlp_root, monkeypatch):
        async def fake_run(*cmd, timeout=300):
            return 0, b"{}", b""
        monkeypatch.setattr(yt, "_run", fake_run)
        assert await yt.update_ytdlp() is None

    def test_heal_picks_newest_venv(self, tmp_path):
        root = tmp_path / "y"
        for name, mtime in (("old", 1000), ("new", 2000)):
            (root / name / "bin").mkdir(parents=True)
            (root / name / "bin" / "yt-dlp").write_text("#!")
            os.utime(root / name, (mtime, mtime))
        (root / "current").symlink_to("gone")
        yt._heal_current(root, root / "current")
        assert os.readlink(root / "current") == "new" and not (root / "current.heal").exists()

    def test_heal_ignores_missing_or_real_current(self, tmp_path):
        root = tmp_path / "y"
        (root / "v1" / "bin").mkdir(parents=True)
        (root / "v1" / "bin" / "yt-dlp").write_text("#!")
        yt._heal_current(root, root / "current")           # no link at all: not ours to create
        assert not (root / "current").exists() and not (root / "current").is_symlink()

    def test_heal_with_no_usable_venv_leaves_link(self, tmp_path):
        root = tmp_path / "y"
        (root / "empty").mkdir(parents=True)
        (root / "current").symlink_to("gone")
        yt._heal_current(root, root / "current")
        assert os.readlink(root / "current") == "gone"

    async def test_run_captures_both_streams(self, monkeypatch):
        seen = {}

        async def fake_exec(*a, **kw):
            seen.update(kw)
            return FakeProc()
        monkeypatch.setattr(yt.asyncio, "create_subprocess_exec", fake_exec)
        await yt._run("x")
        assert seen["stdout"] == yt.asyncio.subprocess.PIPE and seen["stderr"] == yt.asyncio.subprocess.PIPE


class TestExactProbeAndSearch:
    async def test_probe_commands(self, monkeypatch, tmp_settings):
        calls = []

        async def fake_run(*cmd, timeout=300):
            calls.append((cmd, timeout))
            return (0, b'{"duration": 5}', b"") if "-J" in cmd else (0, b"10 NA", b"")
        monkeypatch.setattr(yt, "_run", fake_run)
        await yt.probe_video_sizes("https://v")
        meta, *selections = calls
        assert meta == (("yt-dlp", "-J", "--no-warnings", "--no-playlist", "--user-agent", yt.BROWSER_UA,
                         "--extractor-args", "youtube:player_client=default,android_vr", "-4", "https://v"), 90)
        info = selections[0][0][2]
        assert sorted(selections) == sorted([
            (("yt-dlp", "--load-info-json", info, "--no-warnings", "-f", "bv*+ba/b", "-S", "res:360",
              "-O", "%(filesize,filesize_approx)s %(tbr)s"), 30),
            (("yt-dlp", "--load-info-json", info, "--no-warnings", "-f", "bv*+ba/b", "-S", "res:720",
              "-O", "%(filesize,filesize_approx)s %(tbr)s"), 30),
            (("yt-dlp", "--load-info-json", info, "--no-warnings", "-f", "bv*+ba/b",
              "-O", "%(filesize,filesize_approx)s %(tbr)s"), 30),
        ])
        assert info.endswith(".info.json") and info.startswith(str(tmp_settings.download_path))

    async def test_probe_float_and_short_output(self, monkeypatch, tmp_settings):
        async def fake_run(*cmd, timeout=300):
            if "-J" in cmd:
                return 0, b'{"duration": 10}', b""
            if "res:360" in cmd:
                return 0, b"1234.9", b""          # size only, no tbr column
            if "res:720" in cmd:
                return 0, b"NA 1.5", b""
            return 0, b"", b""
        monkeypatch.setattr(yt, "_run", fake_run)
        assert await yt.probe_video_sizes("https://v") == {"duration": 10, "360": 1234, "720": 1875, "best": 0}

    async def test_search_defaults_for_missing_fields(self, spawned):
        spawned.factory = lambda a: FakeProc(out=b'{"id": "z"}\n{"title": "T", "duration": 7, "url": "u"}\n')
        a, b = await yt.search("q")
        assert (a.title, a.artist, a.duration, a.source_id) == ("Unknown", "Unknown", 0, "z")
        assert (b.title, b.artist, b.duration, b.source_id) == ("T", "Unknown", 7, "u")
        assert (a.source, a.bitrate, a.filesize) == ("youtube", 160, 0)

    async def test_search_has_a_timeout(self, monkeypatch):
        seen = {}

        async def fake_run(*cmd, timeout=300):
            seen["timeout"] = timeout
            return 0, b"", b""
        monkeypatch.setattr(yt, "_run", fake_run)
        await yt.search("q")
        assert seen["timeout"] == 60
