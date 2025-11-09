"""PyQt5 application that embeds a scrcpy window for touch interaction."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from queue import Empty, SimpleQueue
from typing import IO, List, Optional, Tuple

from datetime import datetime

from PyQt5.QtCore import QEvent, QRect, QSize, Qt, QTimer, QObject, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QRubberBand,
)

import win32con
import win32gui

# ====== CONFIG ======
SCRCPY_EXE = "C:\\Tools\\scrcpy\\scrcpy.exe"
SNDCPY_EXE = "C:\\Tools\\sndcpy\\sndcpy.bat"
DEVICE_SERIAL: Optional[str] = None  # e.g. "R5CT60SV0RX"
SCRCPY_TITLE = "ShadowCastX-Touch Android"
DEFAULT_MAX_FPS = 240
DEFAULT_BITRATE = "16M"
DEFAULT_SCREENSHOT_DIR = "images"

BITRATE_PATTERN = re.compile(r"^\d+(?:\.\d+)?(?:[KMG](?:bit/s)?)?$", re.IGNORECASE)
SNDCPY_STOP_PROMPT_PATTERN = re.compile(
    r"press\s+enter\s+to\s+(stop|quit|exit|close|end|terminate|finish)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ThemePalette:
    """Collection of colors used to render the UI for a specific theme."""

    name: str
    window_bg: str
    panel_bg: str
    text_color: str
    muted_text: str
    status_text: str
    button_bg: str
    button_hover_bg: str
    button_pressed_bg: str
    button_border: str
    input_bg: str
    input_border: str
    disabled_bg: str
    disabled_text: str
    disabled_border: str
    divider: str
    accent: str
    android_bg: str
    overlay_scrim: str
    overlay_panel: str
    overlay_text: str


DARK_THEME = ThemePalette(
    name="dark",
    window_bg="#121212",
    panel_bg="#1e1e1e",
    text_color="#f3f3f3",
    muted_text="#bbbbbb",
    status_text="#e6e6e6",
    button_bg="#2a2a2a",
    button_hover_bg="#353535",
    button_pressed_bg="#2a2a2a",
    button_border="#3d3d3d",
    input_bg="#1c1c1c",
    input_border="#3d3d3d",
    disabled_bg="#181818",
    disabled_text="#7a7a7a",
    disabled_border="#2b2b2b",
    divider="#2f2f2f",
    accent="#377dff",
    android_bg="#000000",
    overlay_scrim="rgba(10, 10, 10, 220)",
    overlay_panel="rgba(0, 0, 0, 180)",
    overlay_text="#f0f0f0",
)


LIGHT_THEME = ThemePalette(
    name="light",
    window_bg="#f4f4f4",
    panel_bg="#ffffff",
    text_color="#1a1a1a",
    muted_text="#555555",
    status_text="#2d2d2d",
    button_bg="#ffffff",
    button_hover_bg="#eaeaea",
    button_pressed_bg="#dcdcdc",
    button_border="#c9c9c9",
    input_bg="#ffffff",
    input_border="#c9c9c9",
    disabled_bg="#f0f0f0",
    disabled_text="#9a9a9a",
    disabled_border="#d5d5d5",
    divider="#d1d1d1",
    accent="#0d6efd",
    android_bg="#f9f9f9",
    overlay_scrim="rgba(245, 245, 245, 220)",
    overlay_panel="rgba(255, 255, 255, 230)",
    overlay_text="#1a1a1a",
)
# ====================


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrcpyLaunchOptions:
    """Configuration values used when launching scrcpy."""

    max_fps: int = DEFAULT_MAX_FPS
    bitrate: str = DEFAULT_BITRATE
    stay_awake: bool = True
    audio: bool = False

    def __post_init__(self) -> None:
        if self.max_fps <= 0:
            raise ValueError("Frames per second must be greater than zero.")
        if not str(self.bitrate).strip():
            raise ValueError("Bitrate must be a non-empty value, e.g. '16M'.")

    def to_arguments(self) -> List[str]:
        """Return the list of scrcpy CLI arguments for the configured options."""

        args = [
            f"--window-title={SCRCPY_TITLE}",
            "--window-borderless",
            f"--max-fps={self.max_fps}",
            f"--video-bit-rate={self.bitrate}",
        ]
        if self.stay_awake:
            args.append("--stay-awake")
        if not self.audio:
            args.append("--no-audio")
        return args


@dataclass(frozen=True)
class DeviceInfo:
    """Represents a single row returned by ``adb devices``."""

    serial: str
    status: str

    @property
    def is_ready(self) -> bool:
        return self.status.strip().lower() == "device"


def _resolve_scrcpy() -> Optional[str]:
    """Return the path to the scrcpy executable if it can be resolved."""

    if SCRCPY_EXE and os.path.isfile(SCRCPY_EXE):
        return SCRCPY_EXE

    env = os.environ.get("SCRCPY_EXE")
    if env and os.path.isfile(env):
        return env

    from shutil import which

    return which("scrcpy")


def _resolve_sndcpy() -> Optional[str]:
    """Return the path to the sndcpy executable if it can be resolved."""

    if SNDCPY_EXE and os.path.exists(SNDCPY_EXE):
        return SNDCPY_EXE

    env = os.environ.get("SNDCPY_EXE")
    if env and os.path.exists(env):
        return env

    from shutil import which

    return which("sndcpy")


def list_connected_devices() -> List[DeviceInfo]:
    """Return all devices reported by ``adb devices``."""

    try:
        out = subprocess.check_output(
            ["adb", "devices"], stderr=subprocess.STDOUT
        ).decode("utf-8", "ignore")
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        logger.warning("Unable to query adb devices: %s", exc)
        return []

    devices: List[DeviceInfo] = []
    for raw_line in out.splitlines()[1:]:
        line = raw_line.strip()
        if not line or line.startswith("* daemon "):
            continue
        parts = line.split()
        if not parts:
            continue
        serial = parts[0].strip()
        status = parts[1].strip() if len(parts) > 1 else "unknown"
        if not serial:
            continue
        devices.append(DeviceInfo(serial=serial, status=status or "unknown"))

    return devices


def get_first_device() -> Optional[str]:
    """Return the first connected device according to ``adb devices``."""

    for device in list_connected_devices():
        if device.is_ready:
            return device.serial

    return None


class ScrcpyController(QObject):
    """Launches scrcpy and embeds the resulting native window."""

    started = pyqtSignal()
    stopped = pyqtSignal()
    error = pyqtSignal(str)
    audio_unavailable = pyqtSignal(str)

    def __init__(self, serial: Optional[str]):
        super().__init__()
        self.serial = serial
        self.proc: Optional[subprocess.Popen] = None
        self.hwnd: Optional[int] = None
        self.resolution: Optional[Tuple[int, int]] = None
        self.poll = QTimer(self)
        self.poll.setInterval(250)
        self.poll.timeout.connect(self._find_window)
        self._stderr_timer = QTimer(self)
        self._stderr_timer.setInterval(200)
        self._stderr_timer.timeout.connect(self._drain_process_output)
        self._stderr_queue: SimpleQueue[str] = SimpleQueue()
        self._stderr_thread: Optional[threading.Thread] = None
        self._audio_requested = False
        self._audio_warning_emitted = False
        self._stopping = False
        self._sndcpy_proc: Optional[subprocess.Popen] = None
        self._sndcpy_reader: Optional[threading.Thread] = None
        self._sndcpy_monitor: Optional[threading.Thread] = None
        self._sndcpy_prompt_ack = False

    @property
    def is_running(self) -> bool:
        """Return ``True`` when scrcpy is currently running."""

        return self.proc is not None and self.proc.poll() is None

    def start(
        self,
        fps: int = DEFAULT_MAX_FPS,
        bitrate: str = DEFAULT_BITRATE,
        *,
        stay_awake: bool = True,
        audio: bool = False,
    ) -> None:
        """Start scrcpy using the provided configuration values."""

        if self.is_running:
            logger.debug("scrcpy is already running; ignoring duplicate start request.")
            return

        if not self.serial:
            # Refresh the device serial each time we try to start in case a new
            # device has been plugged in since the controller was created.
            self.serial = get_first_device()
            if not self.serial:
                logger.info(
                    "No device serial detected; scrcpy will attempt auto-detection."
                )

        exe = _resolve_scrcpy()
        if not exe:
            self.error.emit("scrcpy.exe not found. Set SCRCPY_EXE to full path.")
            return

        try:
            options = ScrcpyLaunchOptions(
                max_fps=fps,
                bitrate=bitrate,
                stay_awake=stay_awake,
                audio=False,
            )
        except ValueError as exc:
            self.error.emit(str(exc))
            return

        self._update_resolution()

        self._audio_warning_emitted = False
        audio_enabled = False
        if audio:
            audio_enabled = self._start_sndcpy()
        else:
            self._stop_sndcpy()

        self._audio_requested = audio_enabled
        self._stderr_queue = SimpleQueue()
        self._stderr_thread = None

        args = [exe, *options.to_arguments()]
        if self.serial:
            args.insert(1, "-s")
            args.insert(2, self.serial)

        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        try:
            popen_kwargs = {"stderr": subprocess.PIPE, "text": True, "bufsize": 1}
            if creation_flags:
                self.proc = subprocess.Popen(
                    args, creationflags=creation_flags, **popen_kwargs
                )
            else:
                self.proc = subprocess.Popen(args, **popen_kwargs)
        except Exception as exc:  # noqa: BLE001 - broad to surface error to UI
            logger.error("Failed to start scrcpy: %s", exc)
            self.error.emit(str(exc))
            if audio_enabled:
                self._stop_sndcpy()
            return

        if self.proc and self.proc.stderr:
            self._start_output_reader(self.proc.stderr)
            self._stderr_timer.start()

        logger.debug("Started scrcpy with args: %s", args)
        self.poll.start()

    def stop(self) -> None:
        """Terminate the scrcpy process if it is running."""

        self._stderr_timer.stop()
        self.poll.stop()

        if self.proc and self.proc.poll() is None:
            try:
                self._stopping = True
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logger.warning("scrcpy did not exit in time; killing the process.")
                self.proc.kill()
            except Exception as exc:  # noqa: BLE001 - ensure clean shutdown
                logger.error("Error while stopping scrcpy: %s", exc)
            finally:
                self._stopping = False
        else:
            self._stopping = False

        self._clear_output_queue()

        self.proc = None
        self.hwnd = None
        self._stop_sndcpy()
        self.stopped.emit()

    def _find_window(self) -> None:
        hwnd = win32gui.FindWindow(None, SCRCPY_TITLE)
        if hwnd and win32gui.IsWindow(hwnd):
            self.hwnd = hwnd
            self.poll.stop()
            self.started.emit()

    def _update_resolution(self) -> None:
        """Update the current device resolution via ``adb shell wm size``."""

        cmd = ["adb"]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += ["shell", "wm", "size"]

        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode(
                "utf-8", "ignore"
            )
        except Exception as exc:  # noqa: BLE001 - adb errors vary widely
            logger.debug("Unable to query device resolution: %s", exc)
            self.resolution = None
            return

        pattern = re.compile(r"(?:Physical|Override) size:\s*(\d+)x(\d+)")
        for line in out.splitlines():
            match = pattern.search(line)
            if match:
                width, height = match.groups()
                self.resolution = (int(width), int(height))
                return

        self.resolution = None

    def _start_output_reader(self, stream: IO[str]) -> None:
        def _reader() -> None:
            for line in iter(stream.readline, ""):
                self._stderr_queue.put(line)
            stream.close()

        self._stderr_thread = threading.Thread(
            target=_reader, name="scrcpy-stderr", daemon=True
        )
        self._stderr_thread.start()

    def _drain_process_output(self) -> None:
        if not self.proc:
            self._stderr_timer.stop()
            self._clear_output_queue()
            return

        while True:
            try:
                line = self._stderr_queue.get_nowait()
            except Empty:
                break
            self._handle_scrcpy_log_line(line)

        if self.proc and self.proc.poll() is not None:
            code = self.proc.returncode
            unexpected = not self._stopping and code not in (0, None)
            self._stderr_timer.stop()
            self.stop()
            if unexpected:
                message = (
                    f"scrcpy exited unexpectedly with code {code}."
                    if code is not None
                    else "scrcpy exited unexpectedly."
                )
                self.error.emit(message)

    def _handle_scrcpy_log_line(self, line: str) -> None:
        stripped = line.strip()
        if stripped:
            logger.debug("scrcpy stderr: %s", stripped)
        if self._audio_requested and not self._audio_warning_emitted:
            if "Cannot create AudioRecord" in line or "stream explicitly disabled by the device" in line:
                self._notify_audio_unavailable(
                    "Audio capture is not available on this device. Streaming will continue without audio."
                )

    def _notify_audio_unavailable(self, message: str) -> None:
        if self._audio_warning_emitted:
            return
        self._audio_warning_emitted = True
        self._audio_requested = False
        self.audio_unavailable.emit(message)

    def _start_sndcpy(self) -> bool:
        exe = _resolve_sndcpy()
        if not exe:
            self._notify_audio_unavailable(
                "sndcpy executable not found. Audio will be disabled for this session."
            )
            return False

        self._stop_sndcpy()
        self._sndcpy_prompt_ack = False

        args = [exe]
        lower_exe = exe.lower()
        if lower_exe.endswith((".bat", ".cmd")):
            args = ["cmd.exe", "/c", exe]

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        popen_kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
        }
        env = os.environ.copy()
        if self.serial:
            env["ANDROID_SERIAL"] = self.serial
            env["ADB_SERIAL"] = self.serial

        try:
            if creation_flags:
                proc = subprocess.Popen(
                    args,
                    creationflags=creation_flags,
                    env=env,
                    **popen_kwargs,
                )
            else:
                proc = subprocess.Popen(args, env=env, **popen_kwargs)
        except FileNotFoundError:
            self._notify_audio_unavailable(
                "sndcpy executable not found. Audio will be disabled for this session."
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to start sndcpy: %s", exc)
            self._notify_audio_unavailable(f"Unable to start sndcpy: {exc}")
            return False

        self._sndcpy_proc = proc
        self._sndcpy_reader = threading.Thread(
            target=self._read_sndcpy_output,
            name="sndcpy-stdout",
            daemon=True,
        )
        self._sndcpy_reader.start()
        self._sndcpy_monitor = threading.Thread(
            target=self._monitor_sndcpy,
            name="sndcpy-monitor",
            daemon=True,
        )
        self._sndcpy_monitor.start()
        return True

    def _read_sndcpy_output(self) -> None:
        proc = self._sndcpy_proc
        if not proc or not proc.stdout:
            return

        for line in iter(proc.stdout.readline, ""):
            stripped = line.strip()
            if stripped:
                logger.debug("sndcpy: %s", stripped)

            lower = line.lower()
            if "press enter" in lower:
                # Recent versions of sndcpy prompt the user to press Enter in
                # two different situations:
                #   1. After granting capture permission on the device, where
                #      acknowledging the prompt should continue playback.
                #   2. To stop playback ("Press Enter to stop"), which should
                #      be ignored or audio ends immediately.
                if not SNDCPY_STOP_PROMPT_PATTERN.search(lower):
                    self._send_sndcpy_enter()

        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass

    def _send_sndcpy_enter(self) -> None:
        if self._sndcpy_prompt_ack:
            return
        proc = self._sndcpy_proc
        if not proc or not proc.stdin:
            return
        try:
            proc.stdin.write("\n")
            proc.stdin.flush()
            self._sndcpy_prompt_ack = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Unable to acknowledge sndcpy prompt: %s", exc)

    def _monitor_sndcpy(self) -> None:
        proc = self._sndcpy_proc
        if not proc:
            return
        try:
            code = proc.wait()
        except Exception:  # noqa: BLE001 - suppress teardown issues
            return

        if code not in (0, None) and self._audio_requested and not self._stopping:
            self._notify_audio_unavailable(
                "sndcpy exited unexpectedly. Audio playback will stop."
            )

    def _stop_sndcpy(self) -> None:
        proc = self._sndcpy_proc
        if not proc:
            self._sndcpy_proc = None
            self._sndcpy_prompt_ack = False
            self._audio_requested = False
            return

        self._sndcpy_proc = None
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            logger.debug("sndcpy did not exit in time; killing the process.")
            proc.kill()
        except Exception as exc:  # noqa: BLE001 - ensure shutdown continues
            logger.debug("Error while stopping sndcpy: %s", exc)
        finally:
            for stream in (getattr(proc, "stdout", None), getattr(proc, "stdin", None)):
                if stream:
                    try:
                        stream.close()
                    except Exception:  # noqa: BLE001 - ignore close errors
                        pass
            self._sndcpy_reader = None
            self._sndcpy_monitor = None
            self._sndcpy_prompt_ack = False
            self._audio_requested = False

    def _clear_output_queue(self) -> None:
        while True:
            try:
                self._stderr_queue.get_nowait()
            except Empty:
                break


class AndroidView(QWidget):
    """Widget responsible for embedding and resizing the scrcpy window."""

    def __init__(self, controller: ScrcpyController):
        super().__init__()
        self.controller = controller
        self.setAttribute(Qt.WA_NativeWindow, True)
        self._background = DARK_THEME.android_bg
        self._apply_background()
        controller.started.connect(self._embed)
        self.r_timer = QTimer(self)
        self.r_timer.setInterval(1000)
        self.r_timer.timeout.connect(self._resize_child)
        self.r_timer.start()

    def set_background_color(self, color: str) -> None:
        """Update the placeholder background behind the embedded window."""

        if self._background == color:
            return
        self._background = color
        self._apply_background()

    def _apply_background(self) -> None:
        self.setStyleSheet(f"background:{self._background};")

    def _embed(self) -> None:
        hwnd = self.controller.hwnd
        if not hwnd:
            return
        win32gui.SetParent(hwnd, int(self.winId()))
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        style &= ~(win32con.WS_CAPTION | win32con.WS_THICKFRAME |
                   win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX |
                   win32con.WS_SYSMENU)
        style |= win32con.WS_CHILD | win32con.WS_VISIBLE
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
        win32gui.SetWindowPos(
            hwnd,
            None,
            0,
            0,
            0,
            0,
            win32con.SWP_NOSIZE | win32con.SWP_NOMOVE | win32con.SWP_NOZORDER | win32con.SWP_FRAMECHANGED,
        )
        self._resize_child()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._resize_child()

    def _resize_child(self) -> None:
        hwnd = self.controller.hwnd
        if hwnd:
            rect = self.rect()
            width, height = rect.width(), rect.height()
            x_off = 0
            y_off = 0

            aspect: Optional[float] = None
            if self.controller.resolution and self.controller.resolution[1]:
                aspect = self.controller.resolution[0] / self.controller.resolution[1]
            else:
                try:
                    left, top, right, bottom = win32gui.GetClientRect(hwnd)
                    if bottom - top:
                        aspect = (right - left) / (bottom - top)
                except Exception:
                    aspect = None

            if aspect and height:
                available_aspect = width / height if height else aspect

                target_width = width
                target_height = height
                if available_aspect > aspect:
                    target_height = height
                    target_width = max(1, int(round(target_height * aspect)))
                else:
                    target_width = width
                    target_height = max(1, int(round(target_width / aspect)))

                x_off = (width - target_width) // 2
                y_off = (height - target_height) // 2
                width, height = target_width, target_height

            win32gui.MoveWindow(hwnd, x_off, y_off, width, height, True)


class CropDialog(QDialog):
    """Full screen dialog allowing the user to crop a screenshot."""

    def __init__(self, pixmap, parent=None, theme: Optional[ThemePalette] = None):
        super().__init__(parent)
        self.setWindowTitle("Crop Screenshot")
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setModal(True)
        self.setWindowState(Qt.WindowFullScreen)
        self._theme = theme or DARK_THEME

        self._pixmap = pixmap
        self._selection = QRect()
        self._origin = None

        screen = QApplication.primaryScreen()
        target_size = screen.size() if screen else pixmap.size()
        self._scaled_pixmap = pixmap.scaled(
            target_size,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self._scale_x = pixmap.width() / max(1, self._scaled_pixmap.width())
        self._scale_y = pixmap.height() / max(1, self._scaled_pixmap.height())

        self.label = QLabel()
        self.label.setPixmap(self._scaled_pixmap)
        self.label.setFixedSize(self._scaled_pixmap.size())
        self.label.setCursor(Qt.CrossCursor)
        self.label.installEventFilter(self)

        self.rubber_band = QRubberBand(QRubberBand.Rectangle, self.label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)
        layout.addStretch(1)
        layout.addWidget(self.label, alignment=Qt.AlignCenter)

        info_bar = QWidget()
        info_layout = QHBoxLayout(info_bar)
        info_layout.setContentsMargins(20, 12, 20, 12)
        info_layout.setSpacing(15)
        self._info_bar = info_bar

        instructions = QLabel("Click and drag to choose the crop. Press Esc to cancel.")
        self._instructions = instructions

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_button = buttons.button(QDialogButtonBox.Ok)
        if ok_button:
            ok_button.setText("Save Selection")
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if cancel_button:
            cancel_button.setText("Cancel")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        info_layout.addWidget(instructions)
        info_layout.addStretch(1)
        info_layout.addWidget(buttons)

        layout.addWidget(info_bar)
        self._apply_theme()

    def _apply_theme(self) -> None:
        theme = self._theme
        self.setStyleSheet(f"background-color: {theme.overlay_scrim};")
        self._info_bar.setStyleSheet(
            f"background-color: {theme.overlay_panel}; border-radius: 10px;"
        )
        self._instructions.setStyleSheet(
            f"color: {theme.overlay_text}; font-size: 14px;"
        )

    def eventFilter(self, watched, event):  # noqa: N802 (Qt API)
        if watched is self.label:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._origin = event.pos()
                self.rubber_band.setGeometry(QRect(self._origin, QSize()))
                self.rubber_band.show()
                return True
            if event.type() == QEvent.MouseMove and self._origin is not None:
                rect = QRect(self._origin, event.pos()).normalized()
                self.rubber_band.setGeometry(rect)
                return True
            if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                rect = QRect(self._origin, event.pos()).normalized() if self._origin else QRect()
                self._selection = rect
                self._origin = None
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.key() in (Qt.Key_Escape, Qt.Key_Q):
            self.reject()
            return
        super().keyPressEvent(event)

    def accept(self) -> None:  # noqa: N802 (Qt API)
        if not self._selection or self._selection.width() < 2 or self._selection.height() < 2:
            QMessageBox.warning(self, "Crop Screenshot", "Drag on the image to pick an area to save.")
            return
        super().accept()

    def selected_pixmap(self):
        rect = self._selection
        if not rect or rect.width() < 1 or rect.height() < 1:
            return self._pixmap

        mapped = QRect(
            int(rect.left() * self._scale_x),
            int(rect.top() * self._scale_y),
            int(rect.width() * self._scale_x),
            int(rect.height() * self._scale_y),
        )
        mapped = mapped.intersected(QRect(0, 0, self._pixmap.width(), self._pixmap.height()))
        if mapped.isNull():
            return self._pixmap
        return self._pixmap.copy(mapped)


class MainWindow(QWidget):
    """Primary window that manages the embedded scrcpy session."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ShadowCastX-Touch Android")
        self.resize(1400, 840)

        device = DEVICE_SERIAL or get_first_device()

        self.ctrl = ScrcpyController(device)
        self.view = AndroidView(self.ctrl)
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.deviceCombo = QComboBox()
        self.deviceCombo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.deviceCombo.setMinimumWidth(220)
        self.deviceCombo.setToolTip("Choose the Android device to embed.")
        self.deviceCombo.currentIndexChanged.connect(self._on_device_changed)

        self.refreshDevicesButton = QPushButton("Refresh")
        self.refreshDevicesButton.setToolTip(
            "Refresh the list of connected devices using adb."
        )
        self.refreshDevicesButton.clicked.connect(self._refresh_devices)

        self.fpsSpin = QSpinBox()
        self.fpsSpin.setRange(1, 240)
        self.fpsSpin.setValue(DEFAULT_MAX_FPS)
        self.fpsSpin.setSuffix(" fps")
        self.fpsSpin.setToolTip("Maximum frames per second to request from scrcpy.")
        self.fpsSpin.setAccelerated(True)

        self.bitrateInput = QLineEdit(DEFAULT_BITRATE)
        self.bitrateInput.setPlaceholderText(DEFAULT_BITRATE)
        self.bitrateInput.setToolTip(
            "Video bitrate passed to scrcpy. Examples: 16M, 8Mbit/s, 8000K."
        )
        self.bitrateInput.setClearButtonEnabled(True)
        self.bitrateInput.setFixedWidth(110)

        self.stayAwakeCheck = QCheckBox("Keep device awake")
        self.stayAwakeCheck.setChecked(True)

        self.audioCheck = QCheckBox("Enable audio")
        self.audioCheck.setToolTip(
            "Capture audio in addition to video via sndcpy when available."
        )
        self.audioCheck.setChecked(False)

        self.btnStart = QPushButton("Start Stream")
        self.btnStop = QPushButton("Stop")
        self.btnScreenshot = QPushButton("Screenshot")
        self.themeToggle = QPushButton("Switch to Light Mode")
        self.themeToggle.clicked.connect(self._toggle_theme)
        self.status = QLabel(self._device_label())
        self.status.setObjectName("StatusLabel")

        self.btnStart.clicked.connect(self._on_start_clicked)
        self.btnStop.clicked.connect(self.ctrl.stop)
        self.btnScreenshot.clicked.connect(self._capture_screenshot)
        self.ctrl.started.connect(self._on_stream_started)
        self.ctrl.stopped.connect(self._on_stream_stopped)
        self.ctrl.error.connect(self._on_error)
        self.ctrl.audio_unavailable.connect(self._on_audio_unavailable)

        config_bar = QWidget()
        config_layout = QHBoxLayout(config_bar)
        config_layout.setContentsMargins(0, 0, 0, 0)
        config_layout.setSpacing(12)

        device_label = QLabel("Device:")
        device_label.setProperty("colorRole", "muted")
        config_layout.addWidget(device_label)
        config_layout.addWidget(self.deviceCombo)
        config_layout.addWidget(self.refreshDevicesButton)
        config_layout.addSpacing(10)

        fps_label = QLabel("Max FPS:")
        fps_label.setProperty("colorRole", "muted")
        config_layout.addWidget(fps_label)
        config_layout.addWidget(self.fpsSpin)

        bitrate_label = QLabel("Bitrate:")
        bitrate_label.setProperty("colorRole", "muted")
        config_layout.addWidget(bitrate_label)
        config_layout.addWidget(self.bitrateInput)

        config_layout.addSpacing(10)
        config_layout.addWidget(self.stayAwakeCheck)
        config_layout.addWidget(self.audioCheck)
        config_layout.addStretch(1)

        top = QHBoxLayout()
        top.addWidget(self.btnStart)
        top.addWidget(self.btnStop)
        top.addWidget(self.btnScreenshot)
        top.addWidget(self.themeToggle)
        top.addStretch()
        top.addWidget(self.status)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setObjectName("Divider")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        layout.addWidget(config_bar)
        layout.addLayout(top)
        layout.addWidget(line)
        layout.addWidget(self.view)
        layout.setStretch(2, 1)

        config_bar.setObjectName("ConfigBar")

        self._theme = DARK_THEME
        self._apply_theme()
        self._refresh_devices()

    def _device_label(self) -> str:
        current_text = self.deviceCombo.currentText().strip()
        if self.ctrl.serial:
            return f"Device: {self.ctrl.serial}"
        if current_text:
            return f"Device: {current_text}"
        return "Device: Not Found"

    def _update_controls(self, running: bool) -> None:
        has_device = self._selected_serial() is not None
        self.btnStart.setEnabled(not running and has_device)
        self.btnStop.setEnabled(running)
        self.btnScreenshot.setEnabled(running)
        self.fpsSpin.setEnabled(not running)
        self.bitrateInput.setEnabled(not running)
        self.stayAwakeCheck.setEnabled(not running)
        self.audioCheck.setEnabled(not running)
        self.deviceCombo.setEnabled(not running)
        self.refreshDevicesButton.setEnabled(not running)

    def _gather_launch_settings(self) -> Optional[Tuple[int, str, bool, bool]]:
        bitrate = self._validated_bitrate()
        if bitrate is None:
            QMessageBox.warning(
                self,
                "scrcpy",
                "Enter a bitrate like 16M, 8Mbit/s or 8000K.",
            )
            self.bitrateInput.setFocus()
            self.bitrateInput.selectAll()
            return None

        return (
            self.fpsSpin.value(),
            bitrate,
            self.stayAwakeCheck.isChecked(),
            self.audioCheck.isChecked(),
        )

    def _toggle_theme(self) -> None:
        self._theme = LIGHT_THEME if self._theme.name == "dark" else DARK_THEME
        self._apply_theme()

    def _apply_theme(self) -> None:
        theme = self._theme
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(
                f"""
                QWidget {{
                    background-color: {theme.window_bg};
                    color: {theme.text_color};
                }}
                QWidget#ConfigBar {{
                    background-color: {theme.panel_bg};
                    border: 1px solid {theme.input_border};
                    border-radius: 8px;
                }}
                QLabel[colorRole='muted'] {{
                    color: {theme.muted_text};
                }}
                QLabel#StatusLabel {{
                    color: {theme.status_text};
                }}
                QFrame#Divider {{
                    background-color: {theme.divider};
                    min-height: 1px;
                    max-height: 1px;
                }}
                QPushButton {{
                    background-color: {theme.button_bg};
                    color: {theme.text_color};
                    border: 1px solid {theme.button_border};
                    border-radius: 6px;
                    padding: 6px 12px;
                }}
                QPushButton:hover {{
                    background-color: {theme.button_hover_bg};
                }}
                QPushButton:pressed {{
                    background-color: {theme.button_pressed_bg};
                }}
                QPushButton:disabled {{
                    background-color: {theme.disabled_bg};
                    color: {theme.disabled_text};
                    border: 1px solid {theme.disabled_border};
                }}
                QLineEdit, QSpinBox, QComboBox {{
                    background-color: {theme.input_bg};
                    color: {theme.text_color};
                    border: 1px solid {theme.input_border};
                    border-radius: 4px;
                    padding: 4px 6px;
                }}
                QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
                    color: {theme.disabled_text};
                    background-color: {theme.disabled_bg};
                    border: 1px solid {theme.disabled_border};
                }}
                QComboBox QAbstractItemView {{
                    background-color: {theme.input_bg};
                    color: {theme.text_color};
                    selection-background-color: {theme.accent};
                }}
                QCheckBox {{
                    spacing: 6px;
                }}
                QCheckBox::indicator {{
                    width: 16px;
                    height: 16px;
                }}
                QCheckBox::indicator:unchecked {{
                    border: 1px solid {theme.input_border};
                    background-color: {theme.input_bg};
                }}
                QCheckBox::indicator:checked {{
                    border: 1px solid {theme.accent};
                    background-color: {theme.accent};
                }}
                QToolTip {{
                    background-color: {theme.panel_bg};
                    color: {theme.text_color};
                    border: 1px solid {theme.input_border};
                }}
                """
            )

        next_mode = "Light" if theme.name == "dark" else "Dark"
        self.themeToggle.setText(f"Switch to {next_mode} Mode")
        self.themeToggle.setToolTip(f"Enable the {next_mode.lower()} theme.")
        self.view.set_background_color(theme.android_bg)

    def _validated_bitrate(self) -> Optional[str]:
        text = self.bitrateInput.text().strip()
        if not text:
            return DEFAULT_BITRATE

        if not BITRATE_PATTERN.fullmatch(text):
            return None

        suffix = ""
        if text.lower().endswith("bit/s"):
            text = text[:-5]
            suffix = "bit/s"

        normalized = text.strip().upper()
        if not normalized:
            return None

        return f"{normalized}{suffix}" if suffix else normalized

    def _on_stream_started(self) -> None:
        self.status.setText(f"{self._device_label()} — STREAMING")
        self.status.setToolTip("")
        self._update_controls(running=True)
        self._resize_window_to_device()
        # Trigger a follow-up resize once the event loop has processed the
        # scrcpy window embedding. Without this deferred call the phone feed
        # may appear at an incorrect scale until the user manually resizes the
        # window or the periodic timer fires.
        QTimer.singleShot(0, self._resize_window_to_device)

    def _on_stream_stopped(self) -> None:
        self.status.setText(f"{self._device_label()} — STOPPED")
        self._update_controls(running=False)

    def _on_start_clicked(self) -> None:
        serial = self._selected_serial()
        if not serial:
            QMessageBox.warning(
                self,
                "scrcpy",
                "Connect an authorized device before starting the stream.",
            )
            self._refresh_devices()
            return

        settings = self._gather_launch_settings()
        if not settings:
            return

        fps, bitrate, stay_awake, audio = settings
        self.status.setText(f"{self._device_label()} — STARTING…")
        self.ctrl.serial = serial
        self.ctrl.start(fps, bitrate, stay_awake=stay_awake, audio=audio)

    def _on_error(self, message: str) -> None:
        self.status.setText(f"{self._device_label()} — ERROR")
        QMessageBox.critical(self, "scrcpy", message)
        self._update_controls(running=False)

    def _on_audio_unavailable(self, message: str) -> None:
        was_blocked = self.audioCheck.blockSignals(True)
        self.audioCheck.setChecked(False)
        self.audioCheck.blockSignals(was_blocked)
        if self.ctrl.is_running:
            self.status.setText(f"{self._device_label()} — STREAMING (NO AUDIO)")
        QMessageBox.warning(self, "scrcpy", message)

    def _resize_window_to_device(self) -> None:
        if not self.ctrl.resolution:
            return

        screen = QApplication.primaryScreen()
        if not screen:
            return

        available = screen.availableGeometry()
        device_w, device_h = self.ctrl.resolution
        if not device_w or not device_h:
            return

        layout = self.layout()
        if not layout:
            return

        def _item_height(index: int) -> int:
            item = layout.itemAt(index)
            if not item:
                return 0
            widget = item.widget()
            if widget and widget.isVisible():
                return widget.height()
            hint = item.sizeHint()
            return hint.height()

        margins = layout.contentsMargins()
        spacing_total = layout.spacing() * max(0, layout.count() - 1)
        header_height = (
            margins.top()
            + margins.bottom()
            + _item_height(0)
            + _item_height(1)
            + _item_height(2)
            + spacing_total
        )
        horizontal_chrome = margins.left() + margins.right()

        frame_extra_w = max(0, self.frameGeometry().width() - self.geometry().width())
        frame_extra_h = max(0, self.frameGeometry().height() - self.geometry().height())

        max_view_width = max(1, available.width() - horizontal_chrome - frame_extra_w)
        max_view_height = max(1, available.height() - header_height - frame_extra_h)

        scale = min(max_view_width / device_w, max_view_height / device_h)
        if scale <= 0:
            return

        target_w = max(1, int(device_w * scale))
        target_h = max(1, int(device_h * scale))

        self.view.setMinimumSize(target_w, target_h)
        self.view.resize(target_w, target_h)

        total_width = target_w + horizontal_chrome + frame_extra_w
        total_height = target_h + header_height + frame_extra_h
        self.resize(total_width, total_height)

    def _refresh_devices(self) -> None:
        previous_serial = self._selected_serial() or self.ctrl.serial
        devices = list_connected_devices()

        self.deviceCombo.blockSignals(True)
        self.deviceCombo.clear()

        ready_indices = []
        for device in devices:
            label = self._format_device_entry(device)
            self.deviceCombo.addItem(label, device.serial if device.is_ready else None)
            index = self.deviceCombo.count() - 1
            tooltip = f"{device.serial} — {device.status or 'unknown'}"
            self.deviceCombo.setItemData(index, tooltip, Qt.ToolTipRole)
            if device.is_ready:
                ready_indices.append(index)

        if not devices:
            self.deviceCombo.addItem("No devices detected", None)

        selected_index = -1
        if previous_serial:
            selected_index = self.deviceCombo.findData(previous_serial)

        if selected_index == -1 and ready_indices:
            selected_index = ready_indices[0]

        if selected_index >= 0:
            self.deviceCombo.setCurrentIndex(selected_index)
        elif self.deviceCombo.count():
            self.deviceCombo.setCurrentIndex(0)

        self.deviceCombo.blockSignals(False)

        self.ctrl.serial = self._selected_serial()

        inactive = [d for d in devices if not d.is_ready]
        if inactive and not self.ctrl.is_running:
            summary = ", ".join(f"{d.serial} ({d.status})" for d in inactive)
            self.status.setToolTip(f"Non-ready devices detected: {summary}")
        else:
            self.status.setToolTip("")

        if not self.ctrl.is_running:
            self.status.setText(f"{self._device_label()} — STOPPED")

        self._update_controls(running=self.ctrl.is_running)

    def _format_device_entry(self, device: DeviceInfo) -> str:
        status = device.status.strip().lower()
        if device.is_ready:
            return device.serial
        friendly = {
            "unauthorized": "Unauthorized",
            "offline": "Offline",
            "recovery": "Recovery",
            "sideload": "Sideload",
        }.get(status, device.status or "Unknown")
        return f"{device.serial} ({friendly})"

    def _selected_serial(self) -> Optional[str]:
        data = self.deviceCombo.currentData()
        if isinstance(data, str) and data.strip():
            return data.strip()
        return None

    def _on_device_changed(self) -> None:
        self.ctrl.serial = self._selected_serial()
        if not self.ctrl.is_running:
            self.status.setText(f"{self._device_label()} — STOPPED")
        self._update_controls(running=self.ctrl.is_running)

    def _capture_screenshot(self) -> None:
        screen = QApplication.primaryScreen()
        if not screen:
            QMessageBox.warning(self, "Screenshot", "No primary screen available.")
            return

        target_window = self.ctrl.hwnd if self.ctrl.hwnd else int(self.view.winId())
        pixmap = screen.grabWindow(int(target_window))
        if pixmap.isNull() and target_window != int(self.view.winId()):
            # Fallback to grabbing the Qt wrapper widget if capturing the
            # embedded scrcpy window fails for any reason.
            pixmap = screen.grabWindow(int(self.view.winId()))
        if pixmap.isNull():
            QMessageBox.warning(self, "Screenshot", "Unable to capture screenshot.")
            return

        if self.ctrl.resolution:
            device_w, device_h = self.ctrl.resolution
            if device_w and device_h:
                pixmap = pixmap.scaled(
                    device_w,
                    device_h,
                    Qt.IgnoreAspectRatio,
                    Qt.SmoothTransformation,
                )

        dialog = CropDialog(pixmap, self, theme=self._theme)
        if dialog.exec_() != QDialog.Accepted:
            return

        cropped = dialog.selected_pixmap()
        default_name = datetime.now().strftime("screenshot-%Y%m%d-%H%M%S")
        name, ok = QInputDialog.getText(
            self,
            "Save Screenshot",
            "File name:",
            text=default_name,
        )
        if not ok:
            return

        name = name.strip()
        if not name:
            QMessageBox.warning(self, "Screenshot", "File name cannot be empty.")
            return

        if not name.lower().endswith(".png"):
            name += ".png"

        os.makedirs(DEFAULT_SCREENSHOT_DIR, exist_ok=True)
        path = os.path.join(DEFAULT_SCREENSHOT_DIR, name)

        if not cropped.save(path, "PNG"):
            QMessageBox.critical(self, "Screenshot", "Failed to save screenshot.")
            return

        QMessageBox.information(self, "Screenshot", f"Saved screenshot to:\n{os.path.abspath(path)}")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self.ctrl.stop()
        super().closeEvent(event)


def main() -> None:
    """Entrypoint used when launching the script directly."""

    logging.basicConfig(level=logging.INFO)

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()