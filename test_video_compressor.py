import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from video_compressor import (
    Batch, Cancelled, DeviceCache, GPU, ProgressParser, Settings, build_command, check_encoder,
    device_strategies, duration_seconds, encoder_probe_command, find_tool, gpu_environment,
    parse_nvidia_gpus, selectable_dxgi_gpus,
)


class CommandTests(unittest.TestCase):
    def test_encoder_arguments(self):
        probe = {"streams": [
            {"index": 0, "codec_type": "video", "disposition": {"attached_pic": 1}},
            {"index": 1, "codec_type": "video"},
            {"index": 2, "codec_type": "audio", "codec_name": "aac"},
            {"index": 3, "codec_type": "audio", "codec_name": "opus"},
            {"index": 4, "codec_type": "subtitle", "codec_name": "subrip"},
            {"index": 5, "codec_type": "subtitle", "codec_name": "dvd_subtitle"},
        ]}
        for encoder, flag, value in (
            ("libx265", "-crf", "20"), ("libx264", "-crf", "16"),
            ("hevc_nvenc", "-cq", "20"), ("hevc_qsv", "-global_quality", "20"),
            ("hevc_amf", "-qp_i", "20"), ("h264_nvenc", "-cq", "20"),
            ("h264_qsv", "-global_quality", "20"), ("h264_amf", "-qp_i", "20"),
        ):
            with self.subTest(encoder=encoder):
                args = build_command("ffmpeg", "in.mkv", "out.mp4", probe, Settings(encoder))
                self.assertEqual(args[args.index(flag) + 1], value)
                self.assertEqual([args[i + 1] for i, arg in enumerate(args) if arg == "-map"],
                                 ["0:1", "0:2", "0:3", "0:4"])
                self.assertEqual(args[args.index("-c:a:0") + 1], "copy")
                self.assertEqual(args[args.index("-c:a:1") + 1], "aac")
                self.assertEqual(args[args.index("-b:a:1") + 1], "192k")
                self.assertNotIn("-vf", args)
                self.assertNotIn("-r", args)
                self.assertEqual("hvc1" in args, encoder == "libx265" or encoder.startswith("hevc_"))
                self.assertEqual(args[args.index("-fps_mode") + 1], "passthrough")

    def test_detected_gpu_indexes_exclude_virtual_adapters(self):
        self.assertEqual(selectable_dxgi_gpus([
            (0, 0x8086, "Intel Graphics"),
            (1, 0x8086, "Remote Virtual Display"),
            (2, 0x1002, "AMD Radeon Graphics"),
            (3, 0x10DE, "NVIDIA GeForce"),
        ]), [
            GPU("Intel", 0, "Intel Graphics"),
            GPU("AMD", 2, "AMD Radeon Graphics"),
        ])
        self.assertEqual(parse_nvidia_gpus(
            "GPU 0: NVIDIA GeForce RTX 3060 (UUID: GPU-1234-abcd)\n"
            "GPU 1: NVIDIA GeForce RTX 3060 (UUID: GPU-5678-efab)\n"
        ), [
            GPU("NVIDIA", 0, "NVIDIA GeForce RTX 3060", "GPU-1234-abcd"),
            GPU("NVIDIA", 1, "NVIDIA GeForce RTX 3060", "GPU-5678-efab"),
        ])

    def test_gpu_selection_is_shared_by_probe_and_encode(self):
        probe = {"streams": [{"index": 0, "codec_type": "video"}]}
        examples = (
            (Settings("hevc_qsv", gpu=GPU("Intel", 2, "Intel")), [
                "-init_hw_device", "d3d11va=dx:2",
                "-init_hw_device", "qsv=hw@dx", "-filter_hw_device", "hw",
            ]),
            (Settings("hevc_amf", gpu=GPU("AMD", 1, "AMD")), [
                "-init_hw_device", "d3d11va=dx:1",
            ]),
            (Settings("h264_nvenc", gpu=GPU("NVIDIA", 1, "GeForce", "GPU-5678-efab")), [
                "-gpu", "0",
            ]),
        )
        for settings, expected in examples:
            with self.subTest(encoder=settings.encoder):
                strategy = device_strategies(settings)[0]
                probe_command = encoder_probe_command("ffmpeg", settings, strategy)
                self.assertIn("color=s=1280x720:r=30", probe_command)
                self.assertEqual(probe_command[probe_command.index("-frames:v") + 1], "5")
                self.assertEqual(probe_command[probe_command.index("-pix_fmt") + 1], "nv12")
                for command in (probe_command,
                                build_command("ffmpeg", "in.mkv", "out.mp4", probe, settings, strategy)):
                    index = command.index(expected[0])
                    self.assertEqual(command[index:index + len(expected)], expected)
                    if settings.encoder.endswith("_amf"):
                        self.assertNotIn("-filter_hw_device", command)
                environment = gpu_environment(settings.gpu)
                if settings.encoder.endswith("_nvenc"):
                    self.assertEqual(environment["CUDA_VISIBLE_DEVICES"], "GPU-5678-efab")
                else:
                    self.assertIsNone(environment)

    def test_probe_fallback_caches_winner_per_encoder_and_gpu(self):
        settings = Settings("h264_qsv", gpu=GPU("Intel", 2, "Intel"))
        candidates = device_strategies(settings)
        self.assertEqual([strategy.args for strategy in candidates[1:]], [
            ("-init_hw_device", "qsv=hw:hw_any,child_device=2"),
            ("-init_hw_device", "qsv=hw:hw_any,child_device=2,child_device_type=d3d11va"),
            ("-init_hw_device", "qsv=hw:hw_any,child_device=2,child_device_type=dxva2"),
            ("-qsv_device", "2"),
            (),
        ])
        cache = DeviceCache()
        fail = SimpleNamespace(returncode=1, stderr="Direct3D initialization failed")
        success = SimpleNamespace(returncode=0, stderr="")
        with patch("video_compressor.subprocess.run", side_effect=[fail, fail, success, success]) as run:
            self.assertEqual(check_encoder("ffmpeg", settings, cache), (candidates[2], ""))
            self.assertEqual(check_encoder("ffmpeg", settings, cache), (candidates[2], ""))
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                encoder_probe_command("ffmpeg", settings, candidates[index]) for index in (0, 1, 2, 2)
            ])
        other = Settings("hevc_qsv", gpu=settings.gpu)
        self.assertEqual(cache.ordered("ffmpeg", other), device_strategies(other))
        other_gpu = Settings(settings.encoder, gpu=GPU("Intel", 1, "Intel"))
        self.assertEqual(cache.ordered("ffmpeg", other_gpu), device_strategies(other_gpu))

    def test_failed_probe_returns_full_stderr_of_last_strategy(self):
        settings = Settings("h264_amf", gpu=GPU("AMD", 1, "AMD"))
        first = SimpleNamespace(returncode=1, stderr="D3D11 initialization failed")
        last = SimpleNamespace(returncode=2, stderr="AMF failed\nSecond error line\n")
        with patch("video_compressor.subprocess.run", side_effect=[first, last]) as run:
            strategy, error = check_encoder("ffmpeg", settings, DeviceCache())
        self.assertIsNone(strategy)
        self.assertIn("FFmpeg 自动选卡", error)
        self.assertTrue(error.endswith("AMF failed\nSecond error line"))
        self.assertNotIn("-init_hw_device", run.call_args.args[0])

    def test_real_encode_starts_with_cached_strategy_then_retries(self):
        settings = Settings("h264_amf", gpu=GPU("AMD", 1, "AMD"))
        primary, automatic = device_strategies(settings)
        cache = DeviceCache()
        cache.remember("ffmpeg", settings, automatic)
        batch = Batch(cache)
        commands = []

        def run(args, consume, **_kwargs):
            tmp = Path(args[-1])
            self.assertFalse(tmp.exists())
            tmp.write_bytes(b"mp4")
            commands.append(args)
            if len(commands) == 1:
                consume(SimpleNamespace(stdout=StringIO("AMF failed to initialize\n")))
                return 1, None
            return 0, None

        probe = {"streams": [{"index": 0, "codec_type": "video"}]}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "test.mp4"
            source.write_bytes(b"input")
            with patch.object(batch, "probe", return_value=probe), patch.object(batch, "_run", side_effect=run):
                output, _, _ = batch.encode("ffmpeg", "ffprobe", source, settings, lambda _: None, lambda _: None)
            self.assertEqual(output.read_bytes(), b"mp4")
        self.assertNotIn("-init_hw_device", commands[0])
        self.assertIn("d3d11va=dx:1", commands[1])
        self.assertEqual(cache.ordered("ffmpeg", settings)[0], primary)

    def test_progress_report_retains_fractions_and_reports_speed_and_eta(self):
        parser = ProgressParser(10_000)
        self.assertEqual(parser.feed("warning: test=1"), (False, None))
        for line in ("frame=0", "out_time_us=N/A", "speed=N/A", "progress=continue"):
            self.assertEqual(parser.feed(line), (True, None))
        for line in ("frame=8", "fps=12.5", "out_time_us=7500000", "speed=0.35x"):
            self.assertEqual(parser.feed(line), (True, None))
        handled, progress = parser.feed("progress=continue")
        self.assertTrue(handled)
        self.assertEqual(progress.percent, 0.075)
        self.assertEqual(progress.fps, 12.5)
        self.assertEqual(progress.status(), "已处理 00:00:07 / 02:46:40 · 速度 0.35x · 12.5 fps · 预计剩余 07:55:50")

    def test_progress_without_known_duration_uses_clock_field(self):
        parser = ProgressParser(0)
        for line in ("frame=12", "out_time_us=N/A", "out_time=00:01:02.500", "speed=N/A"):
            parser.feed(line)
        _, progress = parser.feed("progress=end")
        self.assertEqual(progress.elapsed, 62.5)
        self.assertIsNone(progress.percent)
        self.assertEqual(progress.status(), "已处理 00:01:02 / 未知 · 速度 — · 预计剩余 —")

    def test_missing_duration(self):
        self.assertEqual(duration_seconds({"format": {"duration": "N/A"}}), 0)
        self.assertEqual(duration_seconds({"format": {"duration": "N/A"},
                                           "streams": [{"duration": "1.5"}]}), 1.5)

    def test_cancel_running_process(self):
        batch = Batch()
        started = threading.Event()
        results = []

        def consume(process):
            if process.stdout.readline().strip() == "ready":
                started.set()
            process.communicate()

        def work():
            try:
                batch._run([sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(10)"],
                           consume)
            except Cancelled:
                results.append("cancelled")

        worker = threading.Thread(target=work)
        worker.start()
        try:
            self.assertTrue(started.wait(5))
        finally:
            batch.cancel()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, ["cancelled"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class EncodeTests(unittest.TestCase):
    def test_encode_with_audio_subtitles_and_scale(self):
        ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "测试 竖屏.mkv"
            subs = directory / "subs.srt"
            subs.write_text("1\n00:00:00,000 --> 00:00:00,900\nDemo\n", encoding="utf-8")
            subprocess.run(
                [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=480x800:rate=12",
                 "-f", "lavfi", "-i", "sine=frequency=440", "-i", str(subs),
                 "-map", "0:v", "-map", "1:a", "-map", "1:a", "-map", "2:s",
                 "-c:v", "mpeg4", "-c:a:0", "aac", "-c:a:1", "libopus", "-c:s", "srt",
                 "-metadata", "title=Demo", "-t", "1", str(source)],
                check=True, capture_output=True,
            )
            batch = Batch()
            occupied = directory / "测试 竖屏_compressed.mp4"
            occupied.write_bytes(b"existing")
            progress = []
            first, before, after = batch.encode(ffmpeg, ffprobe, source, Settings(),
                                                progress.append, lambda _line: None)
            self.assertEqual(first.name, "测试 竖屏_compressed_1.mp4")
            self.assertEqual(occupied.read_bytes(), b"existing")
            self.assertEqual(before, source.stat().st_size)
            self.assertEqual(after, first.stat().st_size)
            self.assertGreater(len(progress), 1)
            self.assertEqual(progress[-1].percent, 100)
            probed = batch.probe(ffprobe, first)
            video, audio1, audio2, subtitle = probed["streams"]
            self.assertEqual((video["width"], video["height"], video["avg_frame_rate"]),
                             (480, 800, "12/1"))
            self.assertEqual((video["codec_name"], video["codec_tag_string"]), ("hevc", "hvc1"))
            self.assertEqual([audio1["codec_name"], audio2["codec_name"], subtitle["codec_name"]],
                             ["aac", "aac", "mov_text"])
            self.assertEqual(probed["format"]["tags"]["title"], "Demo")

            alternate = directory / "alternate"
            alternate.mkdir()
            second, _, _ = batch.encode(ffmpeg, ffprobe, source,
                                         Settings("libx264", height=360, output_dir=alternate),
                                         lambda _value: None, lambda _line: None)
            scaled = batch.probe(ffprobe, second)["streams"][0]
            self.assertEqual(second.parent, alternate)
            self.assertEqual((scaled["width"], scaled["height"], scaled["codec_name"]),
                             (360, 600, "h264"))
            self.assertFalse(list(directory.rglob(".compress-*")))


if __name__ == "__main__":
    unittest.main()
