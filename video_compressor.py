import ctypes
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


QUALITIES = {
    "原画质（视觉无损）": 20,
    "高画质": 23,
    "均衡": 26,
    "体积优先": 28,
    "自定义": None,
}
ENCODERS = {
    "H.265 · CPU（推荐）": "libx265",
    "H.264 · CPU（兼容优先）": "libx264",
    "H.265 · NVIDIA": "hevc_nvenc",
    "H.264 · NVIDIA": "h264_nvenc",
    "H.265 · Intel": "hevc_qsv",
    "H.264 · Intel": "h264_qsv",
    "H.265 · AMD": "hevc_amf",
    "H.264 · AMD": "h264_amf",
}
SPEEDS = {"慢速（压缩效率优先）": "slow", "中速": "medium", "快速": "fast"}
RESOLUTIONS = {"保留原分辨率": None, "最高 1080p": 1080, "最高 720p": 720}
AUDIO_COPY = {"aac", "alac", "mp3", "ac3", "eac3"}
TEXT_SUBTITLES = {"mov_text", "subrip", "ass", "ssa", "webvtt", "text"}
AUTO_GPU = "自动"
GPU_BACKENDS = {"NVIDIA": "nvenc", "Intel": "qsv", "AMD": "amf"}
HIDDEN_PROCESS = {
    "encoding": "utf-8", "errors": "replace",
    "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    "stdin": subprocess.DEVNULL,
}


def enable_dpi_awareness():
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


@dataclass(frozen=True)
class GPU:
    vendor: str
    index: int
    name: str
    device_uuid: str = ""

    @property
    def label(self):
        return f"{self.name}（{self.vendor} · {self.index}）"


@dataclass(frozen=True)
class Settings:
    encoder: str = "libx265"
    crf: int = 20
    speed: str = "slow"
    height: int = None
    output_dir: Path = None
    gpu: GPU | None = None


class AdapterDescription(ctypes.Structure):
    _fields_ = [
        ("description", wintypes.WCHAR * 128),
        ("vendor_id", wintypes.UINT), ("device_id", wintypes.UINT),
        ("subsystem_id", wintypes.UINT), ("revision", wintypes.UINT),
        ("dedicated_video_memory", ctypes.c_size_t),
        ("dedicated_system_memory", ctypes.c_size_t),
        ("shared_system_memory", ctypes.c_size_t),
        ("luid_low", wintypes.DWORD), ("luid_high", wintypes.LONG),
    ]


def com_method(pointer, slot, result_type, *arg_types):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
    return ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *arg_types)(table.contents[slot])


def dxgi_adapters():
    factory = ctypes.c_void_p()
    iid = (ctypes.c_ubyte * 16).from_buffer_copy(
        uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le
    )
    create_factory = ctypes.windll.dxgi.CreateDXGIFactory1
    result = create_factory(ctypes.byref(iid), ctypes.byref(factory))
    if result < 0:
        raise OSError(f"CreateDXGIFactory1: {result:#x}")
    adapters = []
    try:
        # IDXGIFactory::EnumAdapters, which is also used by FFmpeg's d3d11va backend.
        enumerate_adapter = com_method(factory, 7, wintypes.LONG, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p))
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            result = enumerate_adapter(factory, index, ctypes.byref(adapter))
            if result & 0xffffffff == 0x887a0002:
                break
            if result < 0:
                raise OSError(f"EnumAdapters: {result:#x}")
            try:
                description = AdapterDescription()
                get_desc = com_method(adapter, 8, wintypes.LONG, ctypes.POINTER(AdapterDescription))
                if get_desc(adapter, ctypes.byref(description)) >= 0:
                    adapters.append((index, description.vendor_id, description.description))
            finally:
                com_method(adapter, 2, wintypes.ULONG)(adapter)
            index += 1
    finally:
        com_method(factory, 2, wintypes.ULONG)(factory)
    return adapters


def physical_gpu_name(name):
    return bool(name.strip()) and not re.search(
        r"oray|parsec|virtual|remote|idd.*driver|indirect display|microsoft basic display",
        name, re.IGNORECASE,
    )


def selectable_dxgi_gpus(adapters):
    vendors = {0x8086: "Intel", 0x1002: "AMD"}
    return [GPU(vendors[vendor_id], index, name.strip()) for index, vendor_id, name in adapters
            if vendor_id in vendors and physical_gpu_name(name)]


def parse_nvidia_gpus(output):
    return [GPU("NVIDIA", int(index), name.strip(), device_uuid)
            for index, name, device_uuid in re.findall(
                r"^GPU (\d+): (.+?) \(UUID: (GPU-[\da-fA-F-]+)\)\s*$", output, re.MULTILINE
            ) if physical_gpu_name(name)]


def detect_gpus():
    if sys.platform != "win32":
        return []
    try:
        devices = selectable_dxgi_gpus(dxgi_adapters())
    except (AttributeError, OSError):
        devices = []
    smi = shutil.which("nvidia-smi.exe")
    if not smi:
        for directory in (
            Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI",
        ):
            candidate = directory / "nvidia-smi.exe"
            if candidate.is_file():
                smi = str(candidate)
                break
    if smi:
        try:
            output = subprocess.run([smi, "-L"], capture_output=True, timeout=5, **HIDDEN_PROCESS)
            if output.returncode == 0:
                devices.extend(parse_nvidia_gpus(output.stdout))
        except (OSError, subprocess.TimeoutExpired):
            pass
    return devices


@dataclass(frozen=True)
class DeviceStrategy:
    label: str
    args: tuple[str, ...] = ()


def device_strategies(settings):
    gpu = settings.gpu
    if gpu is not None:
        index = gpu.index
        if settings.encoder.endswith("_qsv"):
            child = f"qsv=hw:hw_any,child_device={index}"
            return (
                DeviceStrategy("QSV / D3D11 派生", (
                    "-init_hw_device", f"d3d11va=dx:{index}",
                    "-init_hw_device", "qsv=hw@dx", "-filter_hw_device", "hw",
                )),
                DeviceStrategy("QSV / 默认子设备类型", ("-init_hw_device", child)),
                DeviceStrategy("QSV / D3D11 子设备", (
                    "-init_hw_device", f"{child},child_device_type=d3d11va",
                )),
                DeviceStrategy("QSV / DXVA2 子设备", (
                    "-init_hw_device", f"{child},child_device_type=dxva2",
                )),
                DeviceStrategy("QSV / -qsv_device", ("-qsv_device", str(index))),
                DeviceStrategy("FFmpeg 自动选卡"),
            )
        if settings.encoder.endswith("_amf"):
            return (
                DeviceStrategy("AMF / D3D11", ("-init_hw_device", f"d3d11va=dx:{index}")),
                DeviceStrategy("FFmpeg 自动选卡"),
            )
    return (DeviceStrategy("默认设备"),)


class DeviceCache:
    def __init__(self):
        self.winners = {}
        self.lock = threading.Lock()

    def ordered(self, ffmpeg, settings):
        candidates = device_strategies(settings)
        with self.lock:
            winner = self.winners.get((ffmpeg, settings.encoder, settings.gpu))
        if winner in candidates:
            return (winner, *(candidate for candidate in candidates if candidate != winner))
        return candidates

    def remember(self, ffmpeg, settings, strategy):
        with self.lock:
            self.winners[ffmpeg, settings.encoder, settings.gpu] = strategy


def gpu_environment(gpu):
    if gpu is not None and gpu.vendor == "NVIDIA":
        # NVML and CUDA ordinals can differ; expose the selected UUID as device 0.
        return {**os.environ, "CUDA_VISIBLE_DEVICES": gpu.device_uuid}
    return None


def encoder_failure_hint(encoder):
    if encoder == "hevc_amf":
        return "AMD H.265 硬件编码不可用。部分旧款 Radeon R7 200 显卡不支持 H.265；请尝试同一显卡的 H.264，或改用 CPU。"
    if encoder == "h264_amf":
        return "AMD H.264 硬件编码不可用。请检查显卡型号和 AMD 驱动，或改用 CPU 编码。"
    if encoder.endswith("_qsv"):
        return "Intel Quick Sync 硬件编码不可用。请检查 Intel 显卡驱动，尝试 H.264 或改用 CPU。"
    return "请检查显卡驱动及编码支持，或改用 CPU 编码。"


def find_tool(name):
    filename = name + ".exe" if sys.platform == "win32" else name
    entrypoint = sys.executable if getattr(sys, "frozen", False) else __file__
    local = Path(entrypoint).resolve().parent / filename
    if local.is_file():
        return str(local)
    found = shutil.which(filename)
    if found:
        return found
    raise FileNotFoundError(f"找不到 {filename}。请将它放在程序所在的文件夹，或加入 PATH。")


def duration_seconds(probe):
    for container in [probe.get("format", {}), *probe.get("streams", [])]:
        try:
            duration = float(container.get("duration", 0))
            if 0 < duration < float("inf"):
                return duration
        except (ValueError, TypeError):
            pass
    return 0


def encoder_options(settings):
    encoder, crf, speed = settings.encoder, settings.crf, settings.speed
    if encoder in ("libx264", "libx265"):
        quality = max(0, crf - 4) if encoder == "libx264" else crf
        return ["-preset", speed, "-crf", str(quality)]
    if encoder.endswith("_nvenc"):
        preset = {"slow": "p6", "medium": "p4", "fast": "p2"}[speed]
        args = ["-preset", preset, "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
        if settings.gpu is not None and settings.gpu.vendor == "NVIDIA":
            args += ["-gpu", "0"]
        return args
    if encoder.endswith("_qsv"):
        return ["-preset", speed, "-global_quality", str(crf)]
    if encoder.endswith("_amf"):
        quality = {"slow": "quality", "medium": "balanced", "fast": "speed"}[speed]
        return ["-quality", quality, "-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf)]
    raise ValueError(f"不支持的编码器：{encoder}")


def encoder_probe_command(ffmpeg, settings, strategy):
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            *strategy.args, "-f", "lavfi", "-i", "color=s=1280x720:r=30",
            "-an", "-frames:v", "5", "-pix_fmt", "nv12",
            "-c:v", settings.encoder, *encoder_options(settings),
            "-f", "null", "-"]


def check_encoder(ffmpeg, settings, cache):
    last_error = ""
    for strategy in cache.ordered(ffmpeg, settings):
        try:
            result = subprocess.run(encoder_probe_command(ffmpeg, settings, strategy), capture_output=True,
                                    timeout=12, env=gpu_environment(settings.gpu), **HIDDEN_PROCESS)
        except subprocess.TimeoutExpired as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
            last_error = f"{strategy.label}：硬件编码检测超时。"
            if stderr:
                last_error += f"\n{stderr}"
        except OSError as exc:
            return None, str(exc)
        else:
            if result.returncode == 0:
                cache.remember(ffmpeg, settings, strategy)
                return strategy, ""
            stderr = result.stderr.strip()
            last_error = f"{strategy.label}：FFmpeg 退出码 {result.returncode}"
            if stderr:
                last_error += f"\n{stderr}"
    return None, last_error


def build_command(ffmpeg, source, output, probe, settings, strategy=None):
    streams = probe.get("streams", [])
    videos = [s for s in streams if s["codec_type"] == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise ValueError("找不到可压缩的视频轨。")
    video = next((s for s in videos if s.get("disposition", {}).get("default")), videos[0])
    if strategy is None:
        strategy = device_strategies(settings)[0]
    args = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-n",
            "-progress", "pipe:1", "-stats_period", "0.25", "-nostats", *strategy.args,
            "-i", str(source),
            "-map", f"0:{video['index']}", "-c:v", settings.encoder, *encoder_options(settings)]
    if settings.encoder == "libx265" or settings.encoder.startswith("hevc_"):
        args += ["-tag:v", "hvc1"]
    if settings.height:
        short = settings.height
        long = short * 16 // 9
        args += ["-vf", f"scale=w='min(iw,if(gte(iw,ih),{long},{short}))':"
                        f"h='min(ih,if(gte(iw,ih),{short},{long}))':"
                        "force_original_aspect_ratio=decrease:force_divisible_by=2"]
    args += ["-fps_mode", "passthrough"]
    audio_index = 0
    for stream in streams:
        codec = stream.get("codec_name")
        if stream["codec_type"] == "audio":
            args += ["-map", f"0:{stream['index']}", f"-c:a:{audio_index}",
                     "copy" if codec in AUDIO_COPY else "aac"]
            if codec not in AUDIO_COPY:
                args += [f"-b:a:{audio_index}", "192k"]
            audio_index += 1
        elif stream["codec_type"] == "subtitle" and codec in TEXT_SUBTITLES:
            args += ["-map", f"0:{stream['index']}"]
    args += ["-c:s", "mov_text", "-map_metadata", "0", "-map_chapters", "0",
             "-movflags", "+faststart", str(output)]
    return args


def progress_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None


def progress_time(values):
    micros = progress_number(values.get("out_time_us"))
    if micros is not None:
        return micros / 1_000_000
    parts = values.get("out_time", "").split(":")
    if len(parts) == 3:
        hours, minutes, seconds = map(progress_number, parts)
        if hours is not None and minutes is not None and seconds is not None:
            return hours * 3600 + minutes * 60 + seconds
    return 0.0


def clock(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


@dataclass(frozen=True)
class EncodeProgress:
    duration: float
    elapsed: float = 0.0
    fps: float = 0.0
    speed: float = 0.0
    finished: bool = False

    @property
    def percent(self):
        if self.finished:
            return 100.0
        if self.duration and self.elapsed:
            return min(99.9, 100 * self.elapsed / self.duration)
        return None

    def status(self):
        total = clock(self.duration) if self.duration else "未知"
        speed = f"{self.speed:.2f}x" if self.speed else "—"
        fps = f" · {self.fps:.1f} fps" if self.fps else ""
        remaining = "—"
        if self.duration and self.speed:
            eta = (max(0, self.duration - self.elapsed) / self.speed)
            if math.isfinite(eta):
                remaining = clock(math.ceil(eta))
        return f"已处理 {clock(self.elapsed)} / {total} · 速度 {speed}{fps} · 预计剩余 {remaining}"


class ProgressParser:
    def __init__(self, duration):
        self.duration = duration
        self.values = {}
        self.wrote_frames = False
        self.elapsed = 0.0

    def feed(self, line):
        key, sep, val = line.partition("=")
        if not sep or not (key in {
            "frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms", "out_time",
            "dup_frames", "drop_frames", "speed", "progress",
        } or re.fullmatch(r"stream_\d+_\d+_q", key)):
            return False, None
        self.values[key] = val.strip()
        if key != "progress":
            return True, None
        values, self.values = self.values, {}
        elapsed = progress_time(values)
        self.elapsed = max(self.elapsed, elapsed)
        self.wrote_frames |= bool(progress_number(values.get("frame")) or elapsed)
        if not self.wrote_frames:
            return True, None
        return True, EncodeProgress(
            self.duration, self.elapsed, progress_number(values.get("fps")) or 0.0,
            progress_number(values.get("speed", "").removesuffix("x")) or 0.0,
        )


class Cancelled(Exception):
    pass


class Batch:
    def __init__(self, device_cache=None):
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.device_cache = device_cache if device_cache is not None else DeviceCache()

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass

    def _run(self, args, consume, **kwargs):
        with self.lock:
            self.check_cancelled()
            self.process = process = subprocess.Popen(
                args, stdout=subprocess.PIPE, **HIDDEN_PROCESS, **kwargs,
            )
        try:
            with process:
                result = consume(process)
            self.check_cancelled()
            return process.returncode, result
        finally:
            with self.lock:
                self.process = None

    def probe(self, ffprobe, source):
        code, (out, err) = self._run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)],
            lambda process: process.communicate(), stderr=subprocess.PIPE,
        )
        if code:
            raise RuntimeError(err.strip() or f"ffprobe 退出码：{code}")
        return json.loads(out)

    def encode(self, ffmpeg, ffprobe, source, settings, progress, log):
        probe = self.probe(ffprobe, source)
        duration = duration_seconds(probe)
        directory = settings.output_dir or source.parent
        before = source.stat().st_size

        def consume(process):
            for raw in process.stdout:
                line = raw.strip()
                handled, update = parser.feed(line)
                if update is not None:
                    progress(update)
                elif line and not handled:
                    log(line)

        with tempfile.TemporaryDirectory(prefix=".compress-", dir=directory) as temp_dir:
            temp = Path(temp_dir) / "output.mp4"
            strategies = self.device_cache.ordered(ffmpeg, settings)
            for attempt, strategy in enumerate(strategies, start=1):
                parser = ProgressParser(duration)
                if len(strategies) > 1:
                    log(f"[编码] 正在使用 {strategy.label}（{attempt}/{len(strategies)}）")
                args = build_command(ffmpeg, source, temp, probe, settings, strategy)
                code, _ = self._run(args, consume, stderr=subprocess.STDOUT,
                                    env=gpu_environment(settings.gpu))
                if not code:
                    self.device_cache.remember(ffmpeg, settings, strategy)
                    break
                if parser.wrote_frames or attempt == len(strategies):
                    break
                temp.unlink(missing_ok=True)
            if code:
                hint = "" if settings.encoder in ("libx264", "libx265") else encoder_failure_hint(settings.encoder)
                raise RuntimeError(f"FFmpeg 退出码：{code}。请查看日志。{hint}")
            after = temp.stat().st_size
            self.check_cancelled()
            output = publish_output(temp, directory, source.stem)
        progress(EncodeProgress(duration, parser.elapsed, finished=True))
        return output, before, after


def publish_output(temp, directory, stem):
    index = 0
    while True:
        suffix = f"_{index}" if index else ""
        output = directory / f"{stem}_compressed{suffix}.mp4"
        try:
            with output.open("xb"):
                pass
        except FileExistsError:
            index += 1
            continue
        try:
            temp.replace(output)
        except OSError:
            output.unlink()
            raise
        return output


def format_size(size):
    return f"{size / 1024 ** 2:.2f} MB"


class App:
    def __init__(self, root):
        self.root = root
        self.files = {}
        self.events = queue.Queue()
        self.batch = None
        self.closing = False
        self.output_dir = None
        self.quality = tk.StringVar(value=next(iter(QUALITIES)))
        self.encoder = tk.StringVar(value=next(iter(ENCODERS)))
        self.gpu_label = tk.StringVar(value=AUTO_GPU)
        self.gpu_status = tk.StringVar(value="正在检测显卡…")
        self.gpus = {}
        self.device_cache = DeviceCache()
        self.gpus_detected = False
        self.probe_id = 0
        self.probe_result = None
        self.speed = tk.StringVar(value=next(iter(SPEEDS)))
        self.resolution = tk.StringVar(value=next(iter(RESOLUTIONS)))
        self.output_label = tk.StringVar(value="各视频所在文件夹")
        self.crf = tk.DoubleVar(value=20)
        self.crf_label = tk.StringVar(value="20")
        self.status = tk.StringVar(value="请选择要压缩的视频")
        self.file_progress = tk.DoubleVar()
        self.batch_progress = tk.DoubleVar()
        self.setup()
        self.root.after(80, self.poll)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        threading.Thread(target=self.discover_gpus, daemon=True).start()

    def setup(self):
        root = self.root
        root.title("视频压缩工具 · FFmpeg")
        scale = root.winfo_fpixels("1i") / 96 if sys.platform == "win32" else 1
        width = min(round(930 * scale), root.winfo_screenwidth() - 32)
        height = min(round(800 * scale), root.winfo_screenheight() - 96)
        root.geometry(f"{width}x{height}")
        root.minsize(min(round(780 * scale), width), min(round(620 * scale), height))
        root.configure(background="#edf2f7")
        families = font.families(root)
        family = next((name for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC") if name in families),
                      font.nametofont("TkDefaultFont", root=root).actual("family"))
        normal_font = (family, -round(15 * scale))
        bold_font = (*normal_font, "bold")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=normal_font)
        style.configure("TFrame", background="#edf2f7")
        style.configure("TLabelframe", background="#edf2f7", padding=10)
        style.configure("TLabelframe.Label", background="#edf2f7", foreground="#12233c",
                        font=bold_font)
        style.configure("TLabel", background="#edf2f7", foreground="#203452")
        style.configure("Accent.TButton", background="#0055c9", foreground="white", padding=(16, 5))
        style.map("Accent.TButton", background=[("active", "#0048ad"), ("disabled", "#a5b4c8")])
        style.configure("Horizontal.TScale", background="#edf2f7")
        style.configure("Horizontal.TProgressbar", background="#087ad4", troughcolor="#dce5ef")
        style.configure("Treeview", rowheight=round(25 * scale))
        style.configure("Treeview.Heading", font=bold_font, padding=(4, 4))
        root.option_add("*TCombobox*Listbox.font", normal_font)

        banner = tk.Frame(root, background="#122542", padx=22, pady=14)
        banner.pack(fill="x")
        tk.Label(banner, text="视频压缩工具", background="#122542", foreground="white",
                 font=(family, -round(31 * scale), "bold")).pack(anchor="w")
        tk.Label(banner, text="FFmpeg 批量压缩  /  默认保留原分辨率和帧率，原文件不会被覆盖",
                 background="#122542", foreground="#c6d9ef",
                 font=normal_font).pack(anchor="w")
        body = ttk.Frame(root, padding=(16, 8))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        files = ttk.LabelFrame(body, text=" 1  视频文件 ")
        files.grid(row=0, column=0, sticky="nsew", pady=(3, 5))
        buttons = ttk.Frame(files)
        buttons.pack(fill="x", pady=(0, 5))
        self.change_buttons = []
        for label, action in (("添加视频…", self.add_files), ("移除所选", self.remove_files)):
            button = ttk.Button(buttons, text=label, command=action)
            button.pack(side="left", padx=(0, 8))
            self.change_buttons.append(button)
        table = ttk.Frame(files)
        table.pack(fill="both", expand=True)
        columns = ("name", "before", "after", "saved", "status")
        self.table = ttk.Treeview(table, columns=columns, show="headings", height=4)
        for column, title, width in zip(
            columns, ("文件", "原大小", "压缩后", "节省", "状态"), (300, 90, 90, 85, 125)
        ):
            self.table.heading(column, text=title)
            self.table.column(column, width=round(width * scale), stretch=column in ("name", "status"),
                              anchor="w" if column in ("name", "status") else "center")
        scrollbar = ttk.Scrollbar(table, command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)

        options = ttk.LabelFrame(body, text=" 2  压缩设置 ")
        options.grid(row=1, column=0, sticky="ew", pady=5)
        options.columnconfigure((1, 3), weight=1)
        self.combos = []
        for row, col, title, variable, values in (
            (0, 0, "画质", self.quality, QUALITIES), (0, 2, "编码器", self.encoder, ENCODERS),
            (1, 0, "速度", self.speed, SPEEDS), (1, 2, "分辨率", self.resolution, RESOLUTIONS),
        ):
            ttk.Label(options, text=title).grid(row=row, column=col, sticky="w", padx=(0, 8))
            combo = ttk.Combobox(options, state="readonly", textvariable=variable,
                                 values=list(values), font=normal_font)
            combo.grid(row=row, column=col + 1, sticky="ew", padx=(0, 12), pady=2)
            self.combos.append(combo)
        self.combos[0].bind("<<ComboboxSelected>>", self.select_quality)
        self.combos[1].bind("<<ComboboxSelected>>", self.select_encoder)
        ttk.Label(options, text="显卡").grid(row=2, column=0, sticky="w")
        self.gpu_combo = ttk.Combobox(options, state="readonly", textvariable=self.gpu_label,
                                      values=[AUTO_GPU], font=normal_font)
        self.gpu_combo.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(0, 12), pady=2)
        self.gpu_combo.bind("<<ComboboxSelected>>", self.select_gpu)
        self.combos.append(self.gpu_combo)
        ttk.Label(options, textvariable=self.gpu_status).grid(row=2, column=3, sticky="w")
        ttk.Label(options, text="质量值").grid(row=3, column=0, sticky="w")
        self.slider = ttk.Scale(options, from_=10, to=40, variable=self.crf, command=self.slider_changed)
        self.slider.grid(row=3, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(options, textvariable=self.crf_label, width=3).grid(row=3, column=2, sticky="w")
        ttk.Label(options, text="数值越小，画质越高、文件通常越大").grid(row=3, column=3, sticky="w")
        self.select_quality()
        output = ttk.Frame(options)
        output.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Label(output, text="输出到").pack(side="left", padx=(0, 8))
        for label, action in (("选择文件夹…", self.choose_output), ("恢复默认", self.reset_output)):
            button = ttk.Button(output, text=label, command=action)
            button.pack(side="right", padx=(6, 0))
            self.change_buttons.append(button)
        ttk.Label(output, textvariable=self.output_label).pack(side="left")

        activity = ttk.LabelFrame(body, text=" 3  进度与日志 ")
        activity.grid(row=2, column=0, sticky="ew", pady=5)
        activity.columnconfigure(1, weight=1)
        for row, label, var in (
            (0, "当前文件", self.file_progress), (1, "批次进度", self.batch_progress),
        ):
            ttk.Label(activity, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
            bar = ttk.Progressbar(activity, variable=var)
            bar.grid(row=row, column=1, sticky="ew", pady=3)
            if row == 0:
                self.file_bar = bar
        self.log_box = ScrolledText(activity, height=3, wrap="word", state="disabled",
                                    background="#ffffff", foreground="#203452",
                                    font=(family, -round(14 * scale)))
        self.log_box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        self.log("输出为 MP4；如果压缩后比原文件大，文件列表和日志会提示。")

        footer = ttk.Frame(body)
        footer.grid(row=3, column=0, sticky="ew", pady=(5, 0))
        self.start_button = ttk.Button(footer, text="开始压缩", style="Accent.TButton", command=self.start)
        self.start_button.pack(side="right")
        self.cancel_button = ttk.Button(footer, text="取消任务", state="disabled", command=self.cancel)
        self.cancel_button.pack(side="right", padx=8)
        ttk.Label(footer, textvariable=self.status).pack(side="left")

    def slider_changed(self, value):
        self.crf_label.set(str(round(float(value))))

    def select_quality(self, _event=None):
        preset = QUALITIES[self.quality.get()]
        self.slider.configure(state="normal" if preset is None else "disabled")
        if preset is not None:
            self.crf.set(preset)
            self.crf_label.set(str(preset))

    def discover_gpus(self):
        self.events.put(("gpus", detect_gpus()))

    def select_gpu(self, _event=None):
        gpu = self.gpus.get(self.gpu_label.get())
        if gpu:
            codec = "h264" if self.encoder.get().startswith("H.264") else "hevc"
            matching = f"{codec}_{GPU_BACKENDS[gpu.vendor]}"
            self.encoder.set(next(label for label, encoder in ENCODERS.items() if encoder == matching))
        self.refresh_encoder_status()

    def select_encoder(self, _event=None):
        gpu = self.gpus.get(self.gpu_label.get())
        if gpu and not ENCODERS[self.encoder.get()].endswith("_" + GPU_BACKENDS[gpu.vendor]):
            self.gpu_label.set(AUTO_GPU)
        self.refresh_encoder_status()

    def refresh_encoder_status(self):
        self.probe_id += 1
        self.probe_result = None
        encoder = ENCODERS[self.encoder.get()]
        if encoder in ("libx264", "libx265"):
            self.gpu_status.set(
                "CPU 编码" if self.gpus else
                "未检测到可选显卡" if self.gpus_detected else "正在检测显卡…"
            )
            return
        self.gpu_status.set("正在检测硬件编码…")
        settings = Settings(encoder=encoder, gpu=self.gpus.get(self.gpu_label.get()))
        threading.Thread(target=self.probe_encoder, args=(self.probe_id, settings), daemon=True).start()

    def probe_encoder(self, probe_id, settings):
        try:
            result = check_encoder(find_tool("ffmpeg"), settings, self.device_cache)
        except FileNotFoundError as exc:
            result = None, str(exc)
        self.events.put(("encoder_probe", (probe_id, settings, *result)))

    def add_files(self):
        chosen = filedialog.askopenfilenames(title="添加视频文件", filetypes=[
            ("视频文件", "*.mp4 *.mkv *.mov *.avi *.m4v *.wmv *.webm *.flv *.ts *.mpg *.mpeg"),
            ("所有文件", "*"),
        ])
        known = set(self.files.values())
        for name in chosen:
            path = Path(name).resolve()
            if path not in known:
                try:
                    size = format_size(path.stat().st_size)
                except OSError as exc:
                    self.log(f"{path.name}：{exc}")
                    continue
                row = self.table.insert("", "end", values=(path.name, size, "—", "—", "等待中"))
                self.files[row] = path
                known.add(path)
        self.status.set(f"已添加 {len(self.files)} 个文件")

    def remove_files(self):
        for row in self.table.selection():
            self.table.delete(row)
            del self.files[row]
        self.status.set(f"已添加 {len(self.files)} 个文件")

    def choose_output(self):
        folder = filedialog.askdirectory(title="选择输出文件夹")
        if folder:
            self.output_dir = Path(folder)
            self.output_label.set(folder)

    def reset_output(self):
        self.output_dir = None
        self.output_label.set("各视频所在文件夹")

    def log(self, message):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_busy(self, busy):
        for button in [*self.change_buttons, self.start_button]:
            button.configure(state="disabled" if busy else "normal")
        for combo in self.combos:
            combo.configure(state="disabled" if busy else "readonly")
        if busy:
            self.slider.configure(state="disabled")
        else:
            self.select_quality()
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def start(self):
        if not self.files:
            messagebox.showinfo("尚未添加文件", "请先添加要压缩的视频。")
            return
        try:
            ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
        except FileNotFoundError as exc:
            messagebox.showerror("缺少 FFmpeg 工具", str(exc))
            return
        encoder = ENCODERS[self.encoder.get()]
        if encoder not in ("libx264", "libx265"):
            if self.probe_result is None:
                messagebox.showinfo("正在检测", "请等待硬件编码检测结束。")
                return
            if not self.probe_result:
                messagebox.showerror("硬件编码不可用", encoder_failure_hint(encoder))
                return
        settings = Settings(encoder, round(self.crf.get()), SPEEDS[self.speed.get()],
                            RESOLUTIONS[self.resolution.get()], self.output_dir,
                            self.gpus.get(self.gpu_label.get()))
        self.batch = Batch(self.device_cache)
        self.file_progress.set(0)
        self.batch_progress.set(0)
        for row in self.files:
            self.table.set(row, "after", "—")
            self.table.set(row, "saved", "—")
            self.table.set(row, "status", "等待中")
        self.set_busy(True)
        threading.Thread(target=self.work,
                         args=(self.batch, list(self.files.items()), ffmpeg, ffprobe, settings),
                         daemon=True).start()

    def work(self, batch, files, ffmpeg, ffprobe, settings):
        def emit(event, *details):
            self.events.put((event, details))

        successes = failures = larger = 0
        for index, (row, source) in enumerate(files):
            if batch.cancelled.is_set():
                break
            emit("current", row, index + 1, len(files), source.name)
            try:
                output, before, after = batch.encode(
                    ffmpeg, ffprobe, source, settings,
                    lambda update: emit("progress", update, (index + (update.percent or 0) / 100)
                                        * 100 / len(files)),
                    lambda line: emit("log", line),
                )
            except Cancelled:
                emit("row", row, "已取消")
                break
            except Exception as exc:
                failures += 1
                emit("row", row, "失败")
                emit("log", f"失败：{source.name}：{exc}")
                emit("progress", None, (index + 1) * 100 / len(files))
            else:
                successes += 1
                larger += after > before
                emit("result", row, before, after)
                emit("log", f"输出：{output}")
                if after > before:
                    emit("log", "提示：压缩后比原文件大，两个文件均已保留。")
        emit("done", batch.cancelled.is_set(), successes, failures, larger)

    def update_file_bar(self, value):
        if value is None:
            if self.file_bar.cget("mode") != "indeterminate":
                self.file_bar.configure(mode="indeterminate")
                self.file_progress.set(0)
                self.file_bar.start(65)
        else:
            if self.file_bar.cget("mode") != "determinate":
                self.file_bar.stop()
                self.file_bar.configure(mode="determinate")
            self.file_progress.set(value)

    def poll(self):
        for _ in range(100):
            try:
                event, details = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "gpus":
                self.gpus = {gpu.label: gpu for gpu in details}
                self.gpus_detected = True
                self.gpu_combo.configure(values=[AUTO_GPU, *self.gpus])
                if ENCODERS[self.encoder.get()] in ("libx264", "libx265"):
                    self.refresh_encoder_status()
            elif event == "encoder_probe":
                probe_id, settings, strategy, error = details
                if probe_id == self.probe_id:
                    self.probe_result = strategy is not None
                    self.gpu_status.set(
                        "可用（FFmpeg 自动选卡）" if strategy and not strategy.args
                        and settings.gpu and settings.gpu.vendor in ("Intel", "AMD") else
                        "可用" if strategy else
                        "不可用（可尝试 AMD H.264）" if settings.encoder == "hevc_amf" else "不可用"
                    )
                    if strategy:
                        self.log(f"[显卡检测] {settings.encoder} 可用：{strategy.label}。")
                    elif error:
                        for line in error.splitlines():
                            self.log(f"[显卡检测] {line}")
            elif event == "current":
                row, index, total, filename = details
                self.update_file_bar(None)
                self.table.set(row, "status", "压缩中")
                self.status.set(f"正在准备…（{index}/{total}）")
                self.log(f"正在处理 {index}/{total}：{filename}")
            elif event == "progress":
                update, overall = details
                self.update_file_bar(update.percent if update else 0)
                self.batch_progress.set(overall)
                if update and self.batch and not self.batch.cancelled.is_set():
                    self.status.set("当前文件已完成" if update.finished else update.status())
            elif event == "row":
                self.table.set(details[0], "status", details[1])
            elif event == "result":
                row, before, after = details
                self.table.set(row, "before", format_size(before))
                self.table.set(row, "after", format_size(after))
                self.table.set(row, "saved", f"{(1 - after / before):.1%}" if before else "—")
                self.table.set(row, "status", "完成（体积增大）" if after > before else "完成")
            elif event == "log":
                self.log(details[0])
            elif event == "done":
                cancelled, successes, failures, larger = details
                self.batch = None
                if self.file_bar.cget("mode") == "indeterminate":
                    self.update_file_bar(0)
                if self.closing:
                    self.root.destroy()
                    return
                if cancelled:
                    for row in self.files:
                        if self.table.set(row, "status") == "等待中":
                            self.table.set(row, "status", "未处理")
                self.set_busy(False)
                summary = f"{'已取消' if cancelled else '批次结束'}：完成 {successes}，失败 {failures}"
                if larger:
                    summary += f"，体积增大 {larger}"
                self.status.set(summary)
                self.log(summary)
        self.root.after(80, self.poll)

    def cancel(self):
        if self.batch:
            self.batch.cancel()
            self.cancel_button.configure(state="disabled")
            self.status.set("正在取消…")

    def close(self):
        if self.batch:
            self.closing = True
            self.cancel()
        else:
            self.root.destroy()


if __name__ == "__main__":
    enable_dpi_awareness()
    window = tk.Tk()
    App(window)
    window.mainloop()
