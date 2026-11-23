视频压缩工具 · FFmpeg
====================

一、Windows 准备

1. 如需运行 Python 源码，请安装 Python 3.10 或更高版本（勾选 Add Python to PATH，
   并保留安装程序默认选中的 Tcl/Tk 组件）。
2. 从 https://www.gyan.dev/ffmpeg/builds/ 下载 FFmpeg 的 release full build
   （ffmpeg-release-full.7z）。解压后从 bin 文件夹复制 ffmpeg.exe 和 ffprobe.exe，
   放到 video_compressor.py 或打包生成的 video_compressor.exe 同一文件夹。
   也可以将 FFmpeg 的 bin 文件夹加到 PATH。
3. 使用源码时，在该文件夹打开终端，运行：

       python video_compressor.py

   使用已打包的版本时，直接双击 video_compressor.exe，无需安装 Python。

二、如何压缩

1. 点击「添加视频」并多选文件。不选输出文件夹时，每个视频输出到各自原来的
   文件夹，例如 旅行.mp4 → 旅行_compressed.mp4。同名输出会自动编号，不覆盖。
2. 最在意画质时，建议先试默认组合：H.265 · CPU、原画质（视觉无损）、
   慢速、保留原分辨率。用一段代表性素材试压，对比画质和体积后再批量处理。
3. H.265 CPU 画质档位使用 CRF 20 / 23 / 26 / 28。值越小，画质通常越高，
   文件也往往越大；「自定义」可使用滑块。H.264 兼容性更好，质量值约低 4；
   NVIDIA、Intel、AMD 各使用自己的质量控制，数值不能直接比较，且需要
   支持相应编码格式的硬件和驱动。可用「显卡」下拉框指定检测到的显卡，程序
   会检测编码器是否可用。若 H.265 不可用，可尝试同一显卡的 H.264、
   另一块显卡或 CPU 编码。
4. 默认不改变分辨率或帧率。可选择最高 1080p 或 720p 按比例缩小横屏或竖屏
   视频，不主动放大。输出是 MP4，保留主视频、音频、可转换为 MP4 文本的
   字幕与兼容的元数据。图片字幕和封面不会保留，文本字幕的样式可能变化。
   AAC 等兼容音轨直接复制，其余转为 AAC 192 kb/s。
5. 「开始压缩」后可查看当前文件及批次进度、日志、压缩前后大小和节省比例。
   取消时保留已完成的文件，当前未完成的输出会清理。

「视觉无损」是高画质有损重编码，不表示与原始文件逐像素相同，也不保证更小。
原本已经高效压缩的视频，输出可能反而更大；工具会提示，原文件始终保留。
如果目标设备无法播放 H.265，请选择 H.264。

三、在 Windows 打包 EXE

在源码目录打开终端，执行：

       pip install pyinstaller
       pyinstaller -F -w video_compressor.py

也可双击 build_exe.bat 运行等效构建。生成的文件在 dist\video_compressor.exe。
把 ffmpeg.exe 与 ffprobe.exe 复制到 dist，或者已经将 FFmpeg 加到 PATH。
仅分发程序 EXE 时，使用者仍需安装或准备这两个 FFmpeg 工具。
