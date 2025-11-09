# script.py — Embedded scrcpy window inside PyQt5 (Windows)
# Works with scrcpy 3.x (uses --video-bit-rate and --window-borderless)

import os
import sys
import subprocess
from typing import Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QRect, QSize, QEvent
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QMessageBox, QSizePolicy,
    QDialog, QDialogButtonBox, QInputDialog, QRubberBand
)

import win32con, win32gui

# ====== CONFIG ======
SCRCPY_EXE: Optional[str] = None  # Let the helper resolve the executable dynamically.
DEVICE_SERIAL: Optional[str] = None  # e.g. "R5CT60SV0RX"
SCRCPY_TITLE = "Android (Embedded)"
DEFAULT_MAX_FPS = 60
DEFAULT_BITRATE = "16M"
# ====================


def _resolve_scrcpy() -> Optional[str]:
    if SCRCPY_EXE and os.path.isfile(SCRCPY_EXE):
        return SCRCPY_EXE
    env = os.environ.get("SCRCPY_EXE")
    if env and os.path.isfile(env):
        return env
    from shutil import which
    return which("scrcpy")


def get_first_device() -> Optional[str]:
    try:
        out = subprocess.check_output(["adb", "devices"], stderr=subprocess.STDOUT).decode("utf-8","ignore")
        for line in out.splitlines()[1:]:
            if "\tdevice" in line:
                return line.split("\t")[0]
    except:
        pass
    return None


class ScrcpyController(QObject):
    started = pyqtSignal()
    stopped = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, serial: Optional[str]):
        super().__init__()
        self.serial = serial
        self.proc = None
        self.hwnd = None
        self.resolution = None
        self.poll = QTimer()
        self.poll.setInterval(250)
        self.poll.timeout.connect(self._find_window)

    def start(self, fps=DEFAULT_MAX_FPS, bitrate=DEFAULT_BITRATE):
        if not self.serial:
            # Refresh the device serial each time we try to start in case a new
            # device has been plugged in since the controller was created.
            self.serial = get_first_device()

        exe = _resolve_scrcpy()
        if not exe:
            self.error.emit("scrcpy.exe not found. Set SCRCPY_EXE to full path.")
            return

        self._update_resolution()

        args = [
            exe,
            f"--window-title={SCRCPY_TITLE}",
            "--window-borderless",         # ✅ updated for scrcpy 3.2
            f"--max-fps={fps}",
            f"--video-bit-rate={bitrate}", # ✅ updated
            "--stay-awake",
            "--no-audio",
        ]
        if self.serial:
            args.insert(1, "-s")
            args.insert(2, self.serial)

        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        try:
            if creation_flags:
                self.proc = subprocess.Popen(args, creationflags=creation_flags)
            else:
                self.proc = subprocess.Popen(args)
        except Exception as e:
            self.error.emit(str(e))
            return

        self.poll.start()

    def stop(self):
        self.poll.stop()
        if self.proc and self.proc.poll() is None:
            try: self.proc.terminate()
            except: pass
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
            for line in out.splitlines():
                if "Physical size:" in line:
                    size = line.split(":", 1)[1].strip()
                    break
                if "Override size:" in line:
                    size = line.split(":", 1)[1].strip()
                    break
            else:
                size = None
            if size:
                size = size.split()[0]
                if "x" in size:
                    w, h = size.split("x", 1)
                    self.resolution = (int(w), int(h))
                    return
        except Exception:
            pass
        self.resolution = None


class AndroidView(QWidget):
    def __init__(self, controller: ScrcpyController):
        super().__init__()
        self.controller = controller
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setStyleSheet("background:#000;")
        controller.started.connect(self._embed)
        self.r_timer = QTimer()
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

        self.btnStart.clicked.connect(lambda: self.ctrl.start())
        self.btnStop.clicked.connect(self.ctrl.stop)
        self.btnScreenshot.clicked.connect(self._capture_screenshot)
        self.ctrl.started.connect(self._on_stream_started)
        self.ctrl.stopped.connect(self._on_stream_stopped)
        self.ctrl.error.connect(lambda m: QMessageBox.critical(self, "scrcpy", m))

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

    def _device_label(self) -> str:
        return f"Device: {self.ctrl.serial or 'Not Found'}"

    def _on_stream_started(self):
        self.status.setText(f"{self._device_label()} — STREAMING")
        self._resize_window_to_device()

    def _on_stream_stopped(self):
        self.status.setText(f"{self._device_label()} — STOPPED")

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
        name, ok = QInputDialog.getText(self, "Save Screenshot", "File name:", text="screenshot")
        if not ok:
            return

        name = name.strip()
        if not name:
            QMessageBox.warning(self, "Screenshot", "File name cannot be empty.")
            return

        if not name.lower().endswith(".png"):
            name += ".png"

        os.makedirs("images", exist_ok=True)
        path = os.path.join("images", name)

        if not cropped.save(path, "PNG"):
            QMessageBox.critical(self, "Screenshot", "Failed to save screenshot.")
            return

        QMessageBox.information(self, "Screenshot", f"Saved screenshot to:\n{os.path.abspath(path)}")


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
