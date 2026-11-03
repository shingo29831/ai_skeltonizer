"""Module: @role: C++ネイティブ録音モジュール(native_recorder.dll)の自動ビルド・ロード・診断ログ収集・Remuxを担うモジュール"""
import ctypes
from datetime import datetime
import logging
import os
import subprocess
import time
from recorder.audio_processor_wrapper import get_short_path_name


class RecorderConfig(ctypes.Structure):
    _fields_ = [
        ("mic_gain", ctypes.c_float),
        ("system_gain", ctypes.c_float),
        ("noise_gate_threshold", ctypes.c_float),
        ("mic_device_name", ctypes.c_char_p),
        ("ffmpeg_command", ctypes.c_char_p),
        ("save_file_path", ctypes.c_char_p),
        ("temp_file_path", ctypes.c_char_p),
        ("ffmpeg_path", ctypes.c_char_p),
        ("df_model_path", ctypes.c_char_p),
        ("denoise_mode", ctypes.c_int32),
        ("preprocess_mode", ctypes.c_int32),
        ("low_priority", ctypes.c_int32),
    ]


def try_rebuild_native_recorder() -> None:
    """C++のソースファイルがDLLより新しい場合、CMakeビルドを実行して最新化する。"""
    try:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cpp", "audio_processor"))
        cpp_sources = [
            os.path.join(base_dir, "NativeRecorder.cpp"),
            os.path.join(base_dir, "NativeRecorder.h"),
            os.path.join(base_dir, "NativeAudioUtils.h"),
            os.path.join(base_dir, "WasapiCapture.cpp"),
            os.path.join(base_dir, "WasapiCapture.h"),
            os.path.join(base_dir, "AudioProcessor.cpp"),
            os.path.join(base_dir, "AudioProcessor.h"),
        ]
        build_dir = os.path.join(base_dir, "build")
        dll_paths = [
            os.path.join(build_dir, "Release", "native_recorder.dll"),
            os.path.join(build_dir, "Release", "audio_processor.dll"),
        ]

        existing_sources = [p for p in cpp_sources if os.path.exists(p)]
        if not existing_sources:
            return

        max_src_mtime = max(os.path.getmtime(p) for p in existing_sources)
        needs_build = any((not os.path.exists(dll_p) or max_src_mtime > os.path.getmtime(dll_p)) for dll_p in dll_paths)

        if needs_build:
            logging.info("[FFmpegRecorder] Newer C++ source detected. Rebuilding native_recorder.dll...")
            os.makedirs(build_dir, exist_ok=True)
            res = subprocess.run(
                ["cmake", "--build", ".", "--config", "Release"],
                cwd=build_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                timeout=30,
            )
            if res.returncode == 0:
                logging.info("[FFmpegRecorder] C++ module successfully rebuilt.")
            else:
                logging.warning(
                    f"[FFmpegRecorder] Auto-rebuild skipped: {res.stderr.decode('utf-8', errors='replace')}"
                )
    except Exception as e:
        logging.warning(f"[FFmpegRecorder] Auto-rebuild check skipped: {e}")


def load_native_recorder_dll() -> ctypes.CDLL:
    base_build_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cpp", "audio_processor", "build"))
    possible_paths = [
        os.path.join(base_build_dir, "Release", "native_recorder.dll"),
        os.path.join(base_build_dir, "native_recorder.dll"),
    ]

    dll_path = next((p for p in possible_paths if os.path.exists(p)), None)
    if not dll_path:
        raise FileNotFoundError(
            f"native_recorder.dll not found in {base_build_dir}. Please ensure C++ build was successful."
        )

    dll_dir = os.path.dirname(dll_path)
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(dll_dir)

    load_with_altered_search_path = 8
    for dep_dll in ("deep_filter.dll", "libspeexdsp.dll", "audio_processor.dll"):
        dep_path = os.path.join(dll_dir, dep_dll)
        if os.path.exists(dep_path):
            try:
                ctypes.CDLL(dep_path, winmode=load_with_altered_search_path)
            except Exception as e:
                logging.warning(f"[FFmpegRecorder] Failed to preload {dep_dll}: {e}")
        else:
            logging.warning(f"[FFmpegRecorder] Dependency not found: {dep_path}")

    try:
        native_lib = ctypes.CDLL(dll_path, winmode=load_with_altered_search_path)
    except Exception as e:
        logging.error(f"[FFmpegRecorder] Failed to load native_recorder.dll: {e}")
        raise RuntimeError(f"Failed to load native recorder module: {e}")

    native_lib.Recorder_Start.argtypes = [ctypes.POINTER(RecorderConfig)]
    native_lib.Recorder_Start.restype = ctypes.c_bool
    native_lib.Recorder_Stop.argtypes = []
    native_lib.Recorder_IsRunning.restype = ctypes.c_bool
    native_lib.Recorder_GetLastError.restype = ctypes.c_char_p
    if hasattr(native_lib, "Recorder_SetLowPriority"):
        native_lib.Recorder_SetLowPriority.argtypes = [ctypes.c_bool]
        native_lib.Recorder_SetLowPriority.restype = None

    return native_lib


def resolve_deepfilter_model_path(denoise_mode: int) -> str:
    if denoise_mode != 1:
        return ""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    possible_model_paths = [
        os.path.join(project_root, "cpp", "audio_processor", "models", "DeepFilterNet3", "DeepFilterNet3_onnx.tar.gz"),
        os.path.join(project_root, "cpp", "audio_processor", "models", "DeepFilterNet3_onnx.tar.gz"),
        os.path.join(project_root, "cpp", "audio_processor", "DeepFilterNet3_onnx.tar.gz"),
        os.path.join(project_root, "models", "DeepFilterNet3", "DeepFilterNet3_onnx.tar.gz"),
        os.path.join(project_root, "models", "DeepFilterNet3_onnx.tar.gz"),
    ]
    df_model_path = next(
        (p for p in possible_model_paths if os.path.isfile(p) and os.path.getsize(p) > 1024),
        "",
    )
    if not df_model_path:
        for root, _, files in os.walk(project_root):
            for f in files:
                if f.endswith(".tar.gz") and "deepfilter" in f.lower():
                    cand = os.path.join(root, f)
                    if os.path.getsize(cand) > 1024:
                        df_model_path = cand
                        break
            if df_model_path:
                break

    # 初回利用時またはモデル未配置時に自動取得
    if not df_model_path:
        target_path = os.path.join(
            project_root, "cpp", "audio_processor", "models", "DeepFilterNet3", "DeepFilterNet3_onnx.tar.gz"
        )
        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            import urllib.request

            model_url = "https://github.com/Rikorose/DeepFilterNet/raw/main/models/DeepFilterNet3_onnx.tar.gz"
            logging.info(f"[NativeRecorder] DeepFilterNet3 model not found. Downloading from {model_url}...")
            urllib.request.urlretrieve(model_url, target_path)
            if os.path.isfile(target_path) and os.path.getsize(target_path) > 1024:
                df_model_path = target_path
                logging.info("[NativeRecorder] DeepFilterNet3 model downloaded successfully.")
        except Exception as e:
            logging.warning(f"[NativeRecorder] Failed to auto-download DeepFilterNet3 model: {e}")

    if df_model_path:
        return get_short_path_name(df_model_path).replace("\\", "/")
    return ""


def collect_and_save_diagnostic(native_lib: ctypes.CDLL, save_dir: str, err_msg: str) -> None:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    diagnostic_path = os.path.join(project_root, "recording_diagnostic.log")
    try:
        with open(diagnostic_path, "w", encoding="utf-8") as f:
            f.write("=== Recording Error Diagnostic ===\n")
            f.write(f"Timestamp: {datetime.now()}\n")
            f.write(f"Error Message: {err_msg}\n\n")

            try:
                last_err = native_lib.Recorder_GetLastError().decode("utf-8", errors="replace")
                f.write(f"[NativeRecorder Last Error]: {last_err}\n\n")
            except Exception as e:
                f.write(f"[NativeRecorder Last Error]: (Failed to read: {e})\n\n")

            log_targets = [
                ("native_recorder_debug.log", os.path.join(save_dir, "native_recorder_debug.log")),
                ("native_ffmpeg.log", os.path.join(save_dir, "native_ffmpeg.log")),
                (
                    "app.log",
                    os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ValorantRecorder", "app.log"),
                ),
            ]
            for label, path in log_targets:
                f.write(f"--- {label} ({path}) ---\n")
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8", errors="replace") as lf:
                        f.write(lf.read()[-3000:])
                else:
                    f.write("File not found.\n")
                f.write("\n\n")
        logging.info(f"[FFmpegRecorder] Diagnostic log saved to: {diagnostic_path}")
    except Exception as write_err:
        logging.error(f"[FFmpegRecorder] Failed to write diagnostic log: {write_err}")


def remux_mkv_to_mp4(ffmpeg_path: str, temp_filepath: str, current_filepath: str, actual_encoder: str) -> None:
    if not (temp_filepath and current_filepath and os.path.exists(temp_filepath)):
        return

    for _ in range(6):
        try:
            with open(temp_filepath, "r+b"):
                break
        except IOError:
            time.sleep(0.5)

    remux_temp_path = current_filepath + ".tmp"
    try:
        logging.info(f"[FFmpegRecorder] Remuxing MKV to MP4: {temp_filepath} -> {current_filepath}")
        remux_cmd = [
            ffmpeg_path,
            "-y",
            "-i", temp_filepath,
            "-map", "0",
            "-c", "copy",
        ]
        if "hevc" in actual_encoder.lower():
            remux_cmd.extend(["-tag:v", "hvc1"])
        remux_cmd.extend(["-movflags", "+faststart", "-f", "mp4", remux_temp_path])

        creationflags = 0
        startupinfo = None
        if os.name == "nt":
            below_normal_priority_class = 0x00004000
            creationflags = subprocess.CREATE_NO_WINDOW | below_normal_priority_class
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

        remux_process = subprocess.run(
            remux_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        if remux_process.returncode == 0 and os.path.exists(remux_temp_path):
            os.replace(remux_temp_path, current_filepath)
            try:
                os.remove(temp_filepath)
            except Exception as rm_err:
                logging.warning(f"[FFmpegRecorder] Could not remove temp MKV: {rm_err}")
            logging.info("[FFmpegRecorder] Remux completed successfully.")
        else:
            if os.path.exists(remux_temp_path):
                try:
                    os.remove(remux_temp_path)
                except OSError:
                    pass
            logging.error(f"[FFmpegRecorder] Remux failed with code {remux_process.returncode}")
    except Exception as e:
        if os.path.exists(remux_temp_path):
            try:
                os.remove(remux_temp_path)
            except OSError:
                pass
        logging.error(f"[FFmpegRecorder] Remux exception: {e}")
