# script.py — Embedded scrcpy window inside PyQt5 (Windows)
# Works with scrcpy 3.x (uses --video-bit-rate and --window-borderless)

"""PyQt5 wrapper around scrcpy that embeds the mirror directly in a window.

This module provides a small desktop utility that launches ``scrcpy`` in a
borderless child window, allows taking screenshots and offers a modern crop
dialog.  It is intentionally self-contained so the project can be distributed
as a single file while still remaining readable and maintainable.

The previous iteration of the script grew organically and leaned heavily on
global configuration, bare ``except`` clauses and ad-hoc signal management.
The refactor introduces structured configuration via ``dataclasses``, richer
type information, explicit error messages and a few quality-of-life
improvements inside the UI layer.
"""

from __future__ import annotations

import logging
import os
import sys
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QRect, QSize, QEvent, QPoint
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QMessageBox, QSizePolicy,
    QDialog, QDialogButtonBox, QInputDialog, QRubberBand
)

import win32con
import win32gui


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ====== CONFIG ======
# Note: the default executable path is intentionally Windows-centric because
# scrcpy embeds a native Win32 window.  Users can override it with
# ``SCRCPY_EXE`` in their environment.
SCRCPY_EXE = r"C:\Users\hilli\PycharmProjects\adbhelper\.venv\scrcpy\scrcpy.exe"
DEVICE_SERIAL: Optional[str] = None  # e.g. "R5CT60SV0RX"
SCRCPY_TITLE = "Android (Embedded)"
DEFAULT_MAX_FPS = 60
DEFAULT_BITRATE = "16M"
IMAGES_DIR = Path("images")
# ====================


def _resolve_scrcpy() -> Optional[str]:
    """Return the path to the scrcpy executable if it can be located."""

    candidates: Sequence[str] = [
        SCRCPY_EXE,
        os.environ.get("SCRCPY_EXE", ""),
    ]

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    from shutil import which

    resolved = which("scrcpy")
    if not resolved:
        logger.debug("scrcpy executable not found in PATH")
    return resolved


def get_first_device() -> Optional[str]:
    """Return the first connected device id or ``None`` if nothing is found."""

    try:
        out = subprocess.check_output(
            ["adb", "devices"],
            stderr=subprocess.STDOUT,
        ).decode("utf-8", "ignore")
    except FileNotFoundError:
        logger.warning("adb executable was not found on PATH")
        return None
    except subprocess.CalledProcessError as exc:  # pragma: no cover - defensive
        logger.error("Failed to query adb devices: %s", exc.output)
        return None

    for line in out.splitlines()[1:]:  # skip header
        if "\tdevice" in line:
            return line.split("\t", 1)[0]
    return None


@dataclass(frozen=True)
class ScrcpyProcessConfig:
    """Configuration container for launching a scrcpy process."""

    max_fps: int = DEFAULT_MAX_FPS
    bitrate: str = DEFAULT_BITRATE
    stay_awake: bool = True
    audio: bool = False
    window_title: str = SCRCPY_TITLE
    window_borderless: bool = True

    def flags(self) -> Iterable[str]:
        """Yield command-line arguments based on the configuration."""

        if self.window_title:
            yield f"--window-title={self.window_title}"
        if self.window_borderless:
            yield "--window-borderless"
        yield f"--max-fps={self.max_fps}"
        yield f"--video-bit-rate={self.bitrate}"
        if self.stay_awake:
            yield "--stay-awake"
        if not self.audio:
            yield "--no-audio"

    def build_args(self, exe: str, serial: Optional[str]) -> list[str]:
        """Return the full argument list for ``subprocess.Popen``."""

        args = [exe]
        if serial:
            args.extend(["-s", serial])
        args.extend(self.flags())
        return args


class ScrcpyController(QObject):
    started = pyqtSignal()
    stopped = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, serial: Optional[str]):
        super().__init__()
        self.serial: Optional[str] = serial
        self.proc: Optional[subprocess.Popen] = None
        self.hwnd: Optional[int] = None
        self.resolution: Optional[tuple[int, int]] = None
        self.poll = QTimer(self)
        self.poll.setInterval(250)
        self.poll.timeout.connect(self._find_window)

    @property
    def is_running(self) -> bool:
        """Return ``True`` if the scrcpy process is currently alive."""

        return self.proc is not None and self.proc.poll() is None

    def start(self, config: Optional[ScrcpyProcessConfig] = None):
        """Launch scrcpy and embed the resulting window."""

        if self.is_running:
            logger.info("scrcpy already running; ignoring duplicate start request")
            return

        config = config or ScrcpyProcessConfig()

        if not self.serial:
            # Refresh the device serial each time we try to start in case a new
            # device has been plugged in since the controller was created.
            self.serial = get_first_device()

        exe = _resolve_scrcpy()
        if not exe:
            self.error.emit("scrcpy.exe not found. Set SCRCPY_EXE to full path.")
            return

        self._update_resolution()

        args = config.build_args(exe, self.serial)
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        try:
            if creation_flags:
                self.proc = subprocess.Popen(args, creationflags=creation_flags)
            else:
                self.proc = subprocess.Popen(args)
        except FileNotFoundError:
            self.error.emit("Failed to launch scrcpy. Ensure the executable exists.")
            return
        except Exception as exc:  # pragma: no cover - defensive
            self.error.emit(str(exc))
            return

        logger.info("scrcpy started with args: %s", args)
        self.poll.start()

    def stop(self):
        self.poll.stop()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.warning("scrcpy did not terminate gracefully; killing")
                self.proc.kill()
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Error stopping scrcpy: %s", exc)
        self.proc = None
        self.hwnd = None
        self.stopped.emit()

    def _find_window(self):
        hwnd = win32gui.FindWindow(None, SCRCPY_TITLE)
        if hwnd and win32gui.IsWindow(hwnd):
            self.hwnd = hwnd
            self.poll.stop()
            self.started.emit()

    def _update_resolution(self):
        try:
            cmd = ["adb"]
            if self.serial:
                cmd += ["-s", self.serial]
            cmd += ["shell", "wm", "size"]
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8", "ignore")
        except (FileNotFoundError, subprocess.CalledProcessError):
            logger.debug("Failed to query device resolution")
            self.resolution = None
            return

        size: Optional[str] = None
        for line in out.splitlines():
            if "Physical size:" in line or "Override size:" in line:
                size = line.split(":", 1)[1].strip()
                break

        if size:
            size = size.split()[0]
            if "x" in size:
                try:
                    w, h = size.split("x", 1)
                    self.resolution = (int(w), int(h))
                    return
                except ValueError:  # pragma: no cover - defensive
                    logger.debug("Unexpected resolution format: %s", size)
        self.resolution = None


class AndroidView(QWidget):
    """Widget that hosts the scrcpy Win32 window."""

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

    def _embed(self):
        hwnd = self.controller.hwnd
        if not hwnd: return
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

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._resize_child()

    def _resize_child(self):
        hwnd = self.controller.hwnd
        if hwnd:
            r = self.rect()
            width, height = r.width(), r.height()
            x_off = 0
            y_off = 0

            aspect = None
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
    """Full-screen dialog that lets the user select a region to save."""

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
        self._origin: Optional[QPoint] = None

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

    def eventFilter(self, watched, event):
        if watched is self.label:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._origin = event.pos()
                self.rubber_band.setGeometry(QRect(self._origin, QSize()))
                self.rubber_band.show()
                return True
            elif event.type() == QEvent.MouseMove and self._origin is not None:
                rect = QRect(self._origin, event.pos()).normalized()
                self.rubber_band.setGeometry(rect)
                return True
            elif event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                rect = QRect(self._origin, event.pos()).normalized() if self._origin else QRect()
                self._selection = rect
                self._origin = None
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Q):
            self.reject()
            return
        super().keyPressEvent(event)

    def accept(self):
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
    """Primary window wiring together the controller, view and controls."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Android — Embedded scrcpy")
        self.resize(1400, 840)

        device = DEVICE_SERIAL or get_first_device()

        self.ctrl = ScrcpyController(device)
        self.view = AndroidView(self.ctrl)
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.btnStart = QPushButton("Start Stream")
        self.btnStop = QPushButton("Stop")
        self.btnScreenshot = QPushButton("Screenshot")
        self.status = QLabel(self._device_label())
        self.status.setStyleSheet("color:#bbb;")

        self.btnStart.clicked.connect(self._start_stream)
        self.btnStop.clicked.connect(self.ctrl.stop)
        self.btnScreenshot.clicked.connect(self._capture_screenshot)
        self.ctrl.started.connect(self._on_stream_started)
        self.ctrl.stopped.connect(self._on_stream_stopped)
        self.ctrl.error.connect(self._show_error)

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
        layout.addLayout(top)
        layout.addWidget(line)
        layout.addWidget(self.view)
        layout.setStretch(2, 1)

        self._sync_button_state()

    def _device_label(self) -> str:
        return f"Device: {self.ctrl.serial or 'Not Found'}"

    def _on_stream_started(self):
        self.status.setText(f"{self._device_label()} — STREAMING")
        self._resize_window_to_device()
        self._sync_button_state()

    def _on_stream_stopped(self):
        self.status.setText(f"{self._device_label()} — STOPPED")
        self._sync_button_state()

    def _show_error(self, message: str):
        QMessageBox.critical(self, "scrcpy", message)
        self._sync_button_state()

    def _start_stream(self):
        config = ScrcpyProcessConfig()
        self.ctrl.start(config)
        self._sync_button_state()

    def _sync_button_state(self):
        running = self.ctrl.is_running
        self.btnStart.setEnabled(not running)
        self.btnStop.setEnabled(running)

    def _resize_window_to_device(self):
        if not self.ctrl.resolution:
            return

        screen = QApplication.primaryScreen()
        if not screen:
            return

        available = screen.availableGeometry()
        max_width = available.width() * 0.9
        max_height = available.height() * 0.9

        device_w, device_h = self.ctrl.resolution
        if not device_h:
            return

        scale = min(max_width / device_w, max_height / device_h)
        scale = max(scale, 0.25)

        target_w = int(device_w * scale)
        target_h = int(device_h * scale)

        view_h = max(1, self.view.height())
        view_w = max(1, self.view.width())
        chrome_height = max(0, self.height() - view_h)
        chrome_width = max(0, self.width() - view_w)

        self.resize(target_w + chrome_width, target_h + chrome_height)

    def _capture_screenshot(self):
        screen = QApplication.primaryScreen()
        if not screen:
            QMessageBox.warning(self, "Screenshot", "No primary screen available.")
            return

        pixmap = screen.grabWindow(self.view.winId())
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
        default_name = "screenshot"
        name, ok = QInputDialog.getText(
            self,
            "Save Screenshot",
            "File name:",
            text=default_name,
        )
        if not ok:
            return

        name = name.strip() or default_name
        if not name.lower().endswith(".png"):
            name += ".png"

        IMAGES_DIR.mkdir(exist_ok=True)
        path = IMAGES_DIR / name

        if not cropped.save(str(path), "PNG"):
            QMessageBox.critical(self, "Screenshot", "Failed to save screenshot.")
            return

        QMessageBox.information(self, "Screenshot", f"Saved screenshot to:\n{path.resolve()}")


def main():
    """Entry point for running the PyQt application."""

    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()