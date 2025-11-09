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
SCRCPY_EXE = r"C:\\Users\\hilli\\PycharmProjects\\adbhelper\\.venv\\scrcpy\\scrcpy.exe"
DEVICE_SERIAL: Optional[str] = None  # e.g. "R5CT60SV0RX"
SCRCPY_TITLE = "Android (Embedded)"
DEFAULT_MAX_FPS = 60
DEFAULT_BITRATE = "16M"
DEFAULT_SCREENSHOT_DIR = "images"

BITRATE_PATTERN = re.compile(r"^\d+(?:\.\d+)?(?:[KMG](?:bit/s)?)?$", re.IGNORECASE)
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


def _resolve_scrcpy() -> Optional[str]:
    """Return the path to the scrcpy executable if it can be resolved."""

    if SCRCPY_EXE and os.path.isfile(SCRCPY_EXE):
        return SCRCPY_EXE

    env = os.environ.get("SCRCPY_EXE")
    if env and os.path.isfile(env):
        return env

    from shutil import which

    return which("scrcpy")


def get_first_device() -> Optional[str]:
    """Return the first connected device according to ``adb devices``."""

    try:
        out = subprocess.check_output(
            ["adb", "devices"], stderr=subprocess.STDOUT
        ).decode("utf-8", "ignore")
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        logger.warning("Unable to query adb devices: %s", exc)
        return None

    for line in out.splitlines()[1:]:
        if not line.strip():
            continue
        serial, *rest = line.split("\t")
        status = rest[0] if rest else ""
        if status.strip() == "device":
            return serial.strip()

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
                audio=audio,
            )
        except ValueError as exc:
            self.error.emit(str(exc))
            return

        self._update_resolution()

        self._audio_requested = audio
        self._audio_warning_emitted = False
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
        if (
            self._audio_requested
            and not self._audio_warning_emitted
            and (
                "Cannot create AudioRecord" in line
                or "stream explicitly disabled by the device" in line
            )
        ):
            self._audio_warning_emitted = True
            self.audio_unavailable.emit(
                "Audio capture is not available on this device. Streaming will continue without audio."
            )

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
        self.setStyleSheet("background:#000;")
        controller.started.connect(self._embed)
        self.r_timer = QTimer(self)
        self.r_timer.setInterval(1000)
        self.r_timer.timeout.connect(self._resize_child)
        self.r_timer.start()

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

    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crop Screenshot")
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setModal(True)
        self.setWindowState(Qt.WindowFullScreen)
        self.setStyleSheet("background-color: rgba(10, 10, 10, 220);")

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
        info_bar.setStyleSheet("background-color: rgba(0, 0, 0, 180);")

        instructions = QLabel("Click and drag to choose the crop. Press Esc to cancel.")
        instructions.setStyleSheet("color: #f0f0f0; font-size: 14px;")

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
        self.setWindowTitle("Android — Embedded scrcpy")
        self.resize(1400, 840)

        device = DEVICE_SERIAL or get_first_device()

        self.ctrl = ScrcpyController(device)
        self.view = AndroidView(self.ctrl)
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

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
        self.audioCheck.setToolTip("Capture audio in addition to video when supported.")
        self.audioCheck.setChecked(False)

        self.btnStart = QPushButton("Start Stream")
        self.btnStop = QPushButton("Stop")
        self.btnScreenshot = QPushButton("Screenshot")
        self.status = QLabel(self._device_label())
        self.status.setStyleSheet("color:#bbb;")

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

        fps_label = QLabel("Max FPS:")
        fps_label.setStyleSheet("color:#bbb;")
        config_layout.addWidget(fps_label)
        config_layout.addWidget(self.fpsSpin)

        bitrate_label = QLabel("Bitrate:")
        bitrate_label.setStyleSheet("color:#bbb;")
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
        top.addStretch()
        top.addWidget(self.status)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#333;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        layout.addWidget(config_bar)
        layout.addLayout(top)
        layout.addWidget(line)
        layout.addWidget(self.view)
        layout.setStretch(2, 1)

        self._update_controls(running=False)

    def _device_label(self) -> str:
        return f"Device: {self.ctrl.serial or 'Not Found'}"

    def _update_controls(self, running: bool) -> None:
        self.btnStart.setEnabled(not running)
        self.btnStop.setEnabled(running)
        self.btnScreenshot.setEnabled(running)
        self.fpsSpin.setEnabled(not running)
        self.bitrateInput.setEnabled(not running)
        self.stayAwakeCheck.setEnabled(not running)
        self.audioCheck.setEnabled(not running)

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
        self._update_controls(running=True)
        self._resize_window_to_device()

    def _on_stream_stopped(self) -> None:
        self.status.setText(f"{self._device_label()} — STOPPED")
        self._update_controls(running=False)

    def _on_start_clicked(self) -> None:
        settings = self._gather_launch_settings()
        if not settings:
            return

        fps, bitrate, stay_awake, audio = settings
        self.status.setText(f"{self._device_label()} — STARTING…")
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

        dialog = CropDialog(pixmap, self)
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
