import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from video_compressor import (
    Batch, Cancelled, GPU, Settings, build_command, duration_seconds, encoder_probe_command, find_tool,
    gpu_environment, parse_nvidia_gpus, selectable_dxgi_gpus,
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
                "-init_hw_device", "d3d11va=adapter:2",
                "-init_hw_device", "qsv=hw@adapter", "-filter_hw_device", "hw",
            ]),
            (Settings("hevc_amf", gpu=GPU("AMD", 1, "AMD")), [
                "-init_hw_device", "d3d11va=adapter:1", "-filter_hw_device", "adapter",
            ]),
            (Settings("h264_nvenc", gpu=GPU("NVIDIA", 1, "GeForce", "GPU-5678-efab")), [
                "-gpu", "0",
            ]),
        )
        for settings, expected in examples:
            with self.subTest(encoder=settings.encoder):
                for command in (encoder_probe_command("ffmpeg", settings),
                                build_command("ffmpeg", "in.mkv", "out.mp4", probe, settings)):
                    index = command.index(expected[0])
                    self.assertEqual(command[index:index + len(expected)], expected)
                environment = gpu_environment(settings.gpu)
                if settings.encoder.endswith("_nvenc"):
                    self.assertEqual(environment["CUDA_VISIBLE_DEVICES"], "GPU-5678-efab")
                else:
                    self.assertIsNone(environment)

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
            self.assertEqual(progress[-1], 100)
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
