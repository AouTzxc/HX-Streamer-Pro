import sys
import socket
import time
import struct
import json
import os
import shutil
import ctypes
from pathlib import Path
import cv2
import numpy as np

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QComboBox,
    QSlider,
    QFrame,
    QGraphicsDropShadowEffect,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QImage, QPixmap, QColor, QIcon


class ReceiverWorker(QThread):
    frame_received = pyqtSignal(QImage)
    fps_updated = pyqtSignal(int)
    status_updated = pyqtSignal(str)
    source_updated = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.is_running = False
        self.bind_ip = "0.0.0.0"
        self.port = 7878
        self.protocol = "TCP"
        self.preview_width = 0
        self.preview_height = 0
        self.timeout_sec = 3
        self.fps_limit = 120

        self.max_frame_bytes = 20 * 1024 * 1024
        self._last_emit_time = 0.0
        self._last_decode_warn_time = 0.0

    def request_stop(self):
        self.is_running = False

    def run(self):
        self.is_running = True
        self._last_emit_time = 0.0
        self._last_decode_warn_time = 0.0
        try:
            if self.protocol == "TCP":
                self._run_tcp()
            else:
                self._run_udp()
        except Exception as e:
            self.status_updated.emit(f"错误: {str(e)}")
        finally:
            self.is_running = False
            self.fps_updated.emit(0)
            self.source_updated.emit("None")
            self.status_updated.emit("Stopped")

    def _should_emit_frame(self, now):
        if self.fps_limit <= 0:
            return True
        interval = 1.0 / self.fps_limit
        if now - self._last_emit_time < interval:
            return False
        self._last_emit_time = now
        return True

    def _warn_decode_once_per_sec(self, text):
        now = time.time()
        if now - self._last_decode_warn_time >= 1.0:
            self.status_updated.emit(text)
            self._last_decode_warn_time = now

    def _decode_frame(self, data):
        if not data:
            return None

        if len(data) > self.max_frame_bytes:
            self._warn_decode_once_per_sec(
                f"帧过大({len(data)} bytes)，已丢弃。"
            )
            return None

        arr = np.frombuffer(data, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            self._warn_decode_once_per_sec("收到无法解码的 JPEG 帧")
            return None

        if self.preview_width > 0 and self.preview_height > 0:
            frame = cv2.resize(
                frame,
                (self.preview_width, self.preview_height),
                interpolation=cv2.INTER_AREA,
            )

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = frame_rgb.shape
        return QImage(
            frame_rgb.data, w, h, ch * w, QImage.Format.Format_RGB888
        ).copy()

    def _bind_socket(self, sock):
        try:
            sock.bind((self.bind_ip, self.port))
            return self.bind_ip
        except OSError as e:
            if self.bind_ip != "0.0.0.0":
                self.status_updated.emit(
                    f"绑定 {self.bind_ip}:{self.port} 失败({e})，已回退到 0.0.0.0"
                )
                sock.bind(("0.0.0.0", self.port))
                return "0.0.0.0"
            raise

    def _run_tcp(self):
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.settimeout(1.0)
        try:
            bound_ip = self._bind_socket(server_sock)
            server_sock.listen(1)
            self.status_updated.emit(
                f"TCP 监听中 -> {bound_ip}:{self.port}"
            )
            self.source_updated.emit("None")

            while self.is_running:
                try:
                    client_sock, client_addr = server_sock.accept()
                except socket.timeout:
                    continue

                client_ip, client_port = client_addr
                self.status_updated.emit(
                    f"TCP 已连接 <- {client_ip}:{client_port}"
                )
                self.source_updated.emit(f"{client_ip}:{client_port}")
                client_sock.settimeout(float(self.timeout_sec))

                fps_counter = 0
                last_fps_time = time.time()
                self._last_emit_time = 0.0

                with client_sock:
                    buffer = bytearray()
                    while self.is_running:
                        try:
                            chunk = client_sock.recv(65536)
                        except socket.timeout:
                            now = time.time()
                            if now - last_fps_time >= 1.0:
                                self.fps_updated.emit(fps_counter)
                                fps_counter = 0
                                last_fps_time = now
                            continue
                        except OSError:
                            self.status_updated.emit("TCP 客户端连接异常断开")
                            break

                        if not chunk:
                            self.status_updated.emit("TCP 客户端已断开，等待重连...")
                            break

                        buffer.extend(chunk)
                        invalid_frame = False

                        while self.is_running and len(buffer) >= 4:
                            frame_len = struct.unpack(">L", buffer[:4])[0]
                            if frame_len <= 0 or frame_len > self.max_frame_bytes:
                                self.status_updated.emit(
                                    f"收到非法帧长度: {frame_len} bytes，已断开当前连接"
                                )
                                buffer.clear()
                                invalid_frame = True
                                break

                            if len(buffer) < 4 + frame_len:
                                break

                            frame_data = bytes(buffer[4:4 + frame_len])
                            del buffer[:4 + frame_len]

                            now = time.time()
                            if not self._should_emit_frame(now):
                                continue

                            qt_img = self._decode_frame(frame_data)
                            if qt_img is None:
                                continue

                            self.frame_received.emit(qt_img)
                            fps_counter += 1

                            if now - last_fps_time >= 1.0:
                                self.fps_updated.emit(fps_counter)
                                fps_counter = 0
                                last_fps_time = now

                        if invalid_frame:
                            break

                self.fps_updated.emit(0)
                self.source_updated.emit("None")
        finally:
            server_sock.close()

    def _run_udp(self):
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp_sock.settimeout(1.0)
        try:
            bound_ip = self._bind_socket(udp_sock)
            self.status_updated.emit(
                f"UDP 监听中 -> {bound_ip}:{self.port}"
            )
            self.source_updated.emit("None")

            last_sender = None
            fps_counter = 0
            last_fps_time = time.time()
            self._last_emit_time = 0.0

            while self.is_running:
                try:
                    data, sender = udp_sock.recvfrom(65535)
                except socket.timeout:
                    now = time.time()
                    if now - last_fps_time >= 1.0:
                        self.fps_updated.emit(fps_counter)
                        fps_counter = 0
                        last_fps_time = now
                    continue

                sender_text = f"{sender[0]}:{sender[1]}"
                if sender_text != last_sender:
                    last_sender = sender_text
                    self.source_updated.emit(sender_text)
                    self.status_updated.emit(f"UDP 收流中 <- {sender_text}")

                now = time.time()
                if not self._should_emit_frame(now):
                    continue

                qt_img = self._decode_frame(data)
                if qt_img is None:
                    continue

                self.frame_received.emit(qt_img)
                fps_counter += 1

                if now - last_fps_time >= 1.0:
                    self.fps_updated.emit(fps_counter)
                    fps_counter = 0
                    last_fps_time = now
        finally:
            udp_sock.close()


class ModernInput(QLineEdit):
    def update_theme(self, is_dark):
        if is_dark:
            self.setStyleSheet(
                """
                QLineEdit { background-color: #3A3A3C; border: 1px solid #48484A; border-radius: 8px; color: white; padding: 5px 10px; font-size: 13px; }
                QLineEdit:focus { border: 1px solid #0A84FF; background-color: #48484A; }
            """
            )
        else:
            self.setStyleSheet(
                """
                QLineEdit { background-color: #FFFFFF; border: 1px solid #D1D1D6; border-radius: 8px; color: black; padding: 5px 10px; font-size: 13px; }
                QLineEdit:focus { border: 1px solid #007AFF; background-color: #F2F2F7; }
            """
            )


class ModernButton(QPushButton):
    def __init__(self, text, is_primary=False, parent=None):
        super().__init__(text, parent)
        self.is_primary = is_primary

    def update_theme(self, is_dark):
        if self.is_primary:
            bg, hover, text = ("#0A84FF", "#409CFF", "white")
        else:
            bg = "#3A3A3C" if is_dark else "#E5E5EA"
            hover = "#48484A" if is_dark else "#D1D1D6"
            text = "white" if is_dark else "black"

        self.setStyleSheet(
            f"""
            QPushButton {{ background-color: {bg}; color: {text}; border-radius: 8px; padding: 8px 15px; font-weight: bold; font-size: 13px; border: none; }}
            QPushButton:hover {{ background-color: {hover}; }}
        """
        )


class ThemeToggleButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__("☀", parent)
        self.setFixedSize(30, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def update_theme(self, is_dark):
        if is_dark:
            self.setStyleSheet(
                """
                QPushButton { background-color: rgba(255,255,255,0.1); color: #FFD60A; border-radius: 15px; font-size: 18px; border: 1px solid #48484A; }
                QPushButton:hover { background-color: rgba(255,255,255,0.2); }
            """
            )
        else:
            self.setStyleSheet(
                """
                QPushButton { background-color: #FFFFFF; color: #FF9500; border-radius: 15px; font-size: 18px; border: 1px solid #D1D1D6; }
                QPushButton:hover { background-color: #F2F2F7; }
            """
            )


def get_logo_icon():
    icon_path = Path(__file__).resolve().parent / "logo.ico"
    if icon_path.exists():
        return QIcon(str(icon_path))
    return QIcon()


def set_windows_app_user_model_id(app_id):
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


class ModernReceiverApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(720, 460)
        icon = get_logo_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self.app_name = "HX Streamer Receiver"
        self.is_dark_mode = True
        self.config_path = self.resolve_config_path()
        self.legacy_config_path = Path(__file__).resolve().parent / "receiver_config.json"
        self.ensure_config_directory()
        self.migrate_legacy_config()

        self.is_loading_config = False
        self.auto_save_timer = QTimer(self)
        self.auto_save_timer.setSingleShot(True)
        self.auto_save_timer.setInterval(500)
        self.auto_save_timer.timeout.connect(self.save_config)

        self.worker = ReceiverWorker()
        self.worker.frame_received.connect(self.update_preview)
        self.worker.fps_updated.connect(self.update_fps)
        self.worker.status_updated.connect(self.update_status)
        self.worker.source_updated.connect(self.update_source)
        self.worker.finished.connect(self.on_worker_finished)

        self.init_ui()
        self.bind_auto_save_events()
        self.load_config()
        self.apply_theme()
        self.center_window()
        self.old_pos = None

    def init_ui(self):
        self.main_widget = QFrame()
        self.main_widget.setObjectName("MainFrame")

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 80))
        shadow.setOffset(0, 5)
        self.main_widget.setGraphicsEffect(shadow)
        self.setCentralWidget(self.main_widget)

        main_layout = QHBoxLayout(self.main_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(20)

        controls_layout = QVBoxLayout()
        controls_layout.setSpacing(10)

        header_layout = QHBoxLayout()
        self.title_lbl = QLabel(self.app_name)
        self.title_lbl.setStyleSheet("font-size: 16px; font-weight: bold;")

        self.btn_theme = ThemeToggleButton()
        self.btn_theme.clicked.connect(self.toggle_theme)

        self.btn_close = QPushButton("×")
        self.btn_close.setFixedSize(24, 24)
        self.btn_close.clicked.connect(self.close)

        header_layout.addWidget(self.title_lbl)
        header_layout.addStretch()
        header_layout.addWidget(self.btn_theme)
        header_layout.addSpacing(8)
        header_layout.addWidget(self.btn_close)
        controls_layout.addLayout(header_layout)
        controls_layout.addSpacing(5)

        self.status_lbl = QLabel("Status: Ready")
        self.status_lbl.setStyleSheet("font-size: 12px; color: #888;")
        controls_layout.addWidget(self.status_lbl)

        self.source_lbl = QLabel("Source: None")
        self.source_lbl.setStyleSheet("font-size: 12px; color: #A0A0A0;")
        controls_layout.addWidget(self.source_lbl)

        self.fps_lbl = QLabel("FPS: 0")
        self.fps_lbl.setStyleSheet("font-size: 14px; font-weight: bold; color: #30D158;")
        controls_layout.addWidget(self.fps_lbl)

        form_layout = QVBoxLayout()
        form_layout.setSpacing(8)

        self.lbl_proto = QLabel("Protocol:")
        form_layout.addWidget(self.lbl_proto)
        self.proto_combo = QComboBox()
        self.proto_combo.addItems(["TCP", "UDP (Fast)"])
        self.proto_combo.setFixedHeight(30)
        form_layout.addWidget(self.proto_combo)

        form_layout.addWidget(QLabel("Bind IP (0.0.0.0 = all):"))
        self.inp_ip = ModernInput("0.0.0.0")
        form_layout.addWidget(self.inp_ip)

        form_layout.addWidget(QLabel("Port:"))
        self.inp_port = ModernInput("7878")
        form_layout.addWidget(self.inp_port)

        size_box = QHBoxLayout()
        self.inp_w = ModernInput("0")
        self.inp_h = ModernInput("0")
        size_box.addWidget(QLabel("W:"))
        size_box.addWidget(self.inp_w)
        size_box.addWidget(QLabel("H:"))
        size_box.addWidget(self.inp_h)
        form_layout.addLayout(size_box)
        controls_layout.addLayout(form_layout)

        controls_layout.addSpacing(10)

        self.lbl_timeout_title = QLabel("Socket Timeout: 3s")
        controls_layout.addWidget(self.lbl_timeout_title)

        self.slider_timeout = QSlider(Qt.Orientation.Horizontal)
        self.slider_timeout.setRange(1, 30)
        self.slider_timeout.setValue(3)
        self.slider_timeout.valueChanged.connect(self.on_timeout_change)
        controls_layout.addWidget(self.slider_timeout)

        self.lbl_fps_title = QLabel("Display FPS Limit: 120")
        controls_layout.addWidget(self.lbl_fps_title)

        self.slider_fps = QSlider(Qt.Orientation.Horizontal)
        self.slider_fps.setRange(1, 500)
        self.slider_fps.setValue(120)
        self.slider_fps.valueChanged.connect(self.on_fps_change)
        controls_layout.addWidget(self.slider_fps)

        controls_layout.addStretch()

        self.btn_action = ModernButton("Start Receiving", is_primary=True)
        self.btn_action.setFixedHeight(40)
        self.btn_action.clicked.connect(self.toggle_stream)
        controls_layout.addWidget(self.btn_action)

        self.preview_container = QFrame()
        self.preview_container.setObjectName("PreviewFrame")
        preview_layout = QVBoxLayout(self.preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)

        self.lbl_preview = QLabel("Waiting For Stream")
        self.lbl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self.lbl_preview)

        main_layout.addLayout(controls_layout, 3)
        main_layout.addWidget(self.preview_container, 5)

        self.theme_widgets = [
            self.inp_ip,
            self.inp_port,
            self.inp_w,
            self.inp_h,
            self.btn_action,
        ]

    def bind_auto_save_events(self):
        for widget in [self.inp_ip, self.inp_port, self.inp_w, self.inp_h]:
            widget.textChanged.connect(self.schedule_auto_save)
        self.proto_combo.currentIndexChanged.connect(self.schedule_auto_save)

    def resolve_config_path(self):
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / self.app_name / "config.json"
        return Path.home() / ".hx_streamer_receiver" / "config.json"

    def ensure_config_directory(self):
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"Create config dir failed: {e}")

    def migrate_legacy_config(self):
        if self.config_path.exists() or not self.legacy_config_path.exists():
            return
        try:
            shutil.copy2(self.legacy_config_path, self.config_path)
        except Exception as e:
            print(f"Migrate legacy config failed: {e}")

    def parse_int(self, value, default, min_value=None, max_value=None):
        try:
            result = int(value)
        except (TypeError, ValueError):
            return default
        if min_value is not None and result < min_value:
            return default
        if max_value is not None and result > max_value:
            return default
        return result

    def get_config_data(self):
        return {
            "ip": self.inp_ip.text().strip(),
            "port": self.inp_port.text().strip(),
            "preview_width": self.inp_w.text().strip(),
            "preview_height": self.inp_h.text().strip(),
            "timeout_sec": self.slider_timeout.value(),
            "fps_limit": self.slider_fps.value(),
            "protocol_index": self.proto_combo.currentIndex(),
            "is_dark_mode": self.is_dark_mode,
        }

    def load_config(self):
        if not self.config_path.exists():
            return

        try:
            with self.config_path.open("r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"Load config failed: {e}")
            return

        self.is_loading_config = True
        self.inp_ip.setText(str(config.get("ip", self.inp_ip.text())))
        self.inp_port.setText(str(config.get("port", self.inp_port.text())))
        self.inp_w.setText(str(config.get("preview_width", self.inp_w.text())))
        self.inp_h.setText(str(config.get("preview_height", self.inp_h.text())))

        timeout_sec = self.parse_int(
            config.get("timeout_sec"), self.slider_timeout.value(), 1, 30
        )
        fps_limit = self.parse_int(
            config.get("fps_limit"), self.slider_fps.value(), 1, 500
        )
        self.slider_timeout.setValue(timeout_sec)
        self.slider_fps.setValue(fps_limit)

        protocol_index = config.get("protocol_index")
        if protocol_index is None:
            protocol = str(config.get("protocol", "TCP")).upper()
            protocol_index = 1 if protocol.startswith("UDP") else 0
        protocol_index = self.parse_int(protocol_index, 0, 0, 1)
        self.proto_combo.setCurrentIndex(protocol_index)

        if isinstance(config.get("is_dark_mode"), bool):
            self.is_dark_mode = config["is_dark_mode"]

        self.lbl_timeout_title.setText(f"Socket Timeout: {self.slider_timeout.value()}s")
        self.lbl_fps_title.setText(f"Display FPS Limit: {self.slider_fps.value()}")
        self.is_loading_config = False

    def save_config(self):
        if self.is_loading_config:
            return

        try:
            self.ensure_config_directory()
            with self.config_path.open("w", encoding="utf-8") as f:
                json.dump(self.get_config_data(), f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"Save config failed: {e}")

    def schedule_auto_save(self, *_):
        if self.is_loading_config:
            return
        self.auto_save_timer.start()

    def on_timeout_change(self, value):
        self.lbl_timeout_title.setText(f"Socket Timeout: {value}s")
        self.schedule_auto_save()

    def on_fps_change(self, value):
        self.lbl_fps_title.setText(f"Display FPS Limit: {value}")
        if self.worker.isRunning():
            self.worker.fps_limit = value
        self.schedule_auto_save()

    def toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode
        self.apply_theme()
        self.schedule_auto_save()

    def apply_theme(self):
        if self.is_dark_mode:
            bg_main = "#1C1C1E"
            bg_preview = "#000000"
            text_color = "#E5E5E5"
            border_color = "#333333"
            close_bg = "#FF453A"
            combo_bg = "#3A3A3C"
            combo_border = "#48484A"
        else:
            bg_main = "#F2F2F7"
            bg_preview = "#E5E5EA"
            text_color = "#1C1C1E"
            border_color = "#D1D1D6"
            close_bg = "#FF3B30"
            combo_bg = "#FFFFFF"
            combo_border = "#D1D1D6"

        self.main_widget.setStyleSheet(
            f"""
            #MainFrame {{
                background-color: {bg_main};
                border-radius: 16px;
                border: 1px solid {border_color};
            }}
            QLabel {{ color: {text_color}; font-family: 'Segoe UI', sans-serif; }}
        """
        )

        self.preview_container.setStyleSheet(
            f"""
            #PreviewFrame {{
                background-color: {bg_preview};
                border-radius: 12px;
                border: 1px solid {border_color};
            }}
        """
        )

        self.btn_close.setStyleSheet(
            f"""
            QPushButton {{ background-color: {close_bg}; border-radius: 12px; color: white; font-weight: bold; }}
            QPushButton:hover {{ background-color: red; }}
        """
        )

        self.proto_combo.setStyleSheet(
            f"""
            QComboBox {{ background-color: {combo_bg}; color: {text_color}; border-radius: 8px; padding: 5px; border: 1px solid {combo_border}; }}
            QComboBox::drop-down {{ border: none; }}
            QComboBox QAbstractItemView {{ background-color: {combo_bg}; color: {text_color}; selection-background-color: #0A84FF; }}
        """
        )

        self.title_lbl.setStyleSheet(
            f"font-size: 16px; font-weight: bold; color: {text_color};"
        )

        for widget in self.theme_widgets:
            if hasattr(widget, "update_theme"):
                widget.update_theme(self.is_dark_mode)

        self.btn_theme.update_theme(self.is_dark_mode)
        if self.worker.isRunning():
            self.apply_stop_button_style()

    def set_stream_inputs_enabled(self, enabled):
        for widget in [
            self.inp_ip,
            self.inp_port,
            self.inp_w,
            self.inp_h,
            self.proto_combo,
            self.slider_timeout,
        ]:
            widget.setEnabled(enabled)

    def apply_stop_button_style(self):
        self.btn_action.setStyleSheet(
            """
            QPushButton { background-color: #FF453A; color: white; border-radius: 8px; border: none; font-weight: bold; font-size: 13px; }
            QPushButton:hover { background-color: #FF5D55; }
        """
        )

    def collect_settings(self):
        bind_ip = self.inp_ip.text().strip()
        if not bind_ip:
            bind_ip = "0.0.0.0"

        try:
            socket.inet_aton(bind_ip)
        except OSError:
            self.status_lbl.setText("Error: Bind IP 格式不正确")
            return None

        port = self.parse_int(self.inp_port.text(), None, 1, 65535)
        if port is None:
            self.status_lbl.setText("Error: 端口范围应为 1-65535")
            return None

        preview_w = self.parse_int(self.inp_w.text(), None, 0, 8192)
        preview_h = self.parse_int(self.inp_h.text(), None, 0, 8192)
        if preview_w is None or preview_h is None:
            self.status_lbl.setText("Error: 预览尺寸范围应为 0-8192")
            return None
        if (preview_w == 0) != (preview_h == 0):
            self.status_lbl.setText("Error: W/H 需同时为 0 或同时大于 0")
            return None
        if preview_w != 0 and preview_w < 16:
            self.status_lbl.setText("Error: 预览宽度应 >= 16")
            return None
        if preview_h != 0 and preview_h < 16:
            self.status_lbl.setText("Error: 预览高度应 >= 16")
            return None

        return {
            "bind_ip": bind_ip,
            "port": port,
            "preview_width": preview_w,
            "preview_height": preview_h,
            "timeout_sec": self.slider_timeout.value(),
            "fps_limit": self.slider_fps.value(),
            "protocol": "TCP" if self.proto_combo.currentIndex() == 0 else "UDP",
        }

    def toggle_stream(self):
        if not self.worker.isRunning():
            settings = self.collect_settings()
            if not settings:
                return

            self.worker.bind_ip = settings["bind_ip"]
            self.worker.port = settings["port"]
            self.worker.preview_width = settings["preview_width"]
            self.worker.preview_height = settings["preview_height"]
            self.worker.timeout_sec = settings["timeout_sec"]
            self.worker.fps_limit = settings["fps_limit"]
            self.worker.protocol = settings["protocol"]
            self.save_config()

            self.worker.start()
            self.btn_action.setText("Stop Receiving")
            self.apply_stop_button_style()
            self.set_stream_inputs_enabled(False)
            self.status_lbl.setText("Status: Starting...")
        else:
            self.status_lbl.setText("Status: Stopping...")
            self.worker.request_stop()
            self.btn_action.setEnabled(False)
            self.btn_action.setText("Stopping...")

    def on_worker_finished(self):
        self.btn_action.setEnabled(True)
        self.btn_action.setText("Start Receiving")
        self.btn_action.update_theme(self.is_dark_mode)
        self.set_stream_inputs_enabled(True)
        self.lbl_preview.setText("Waiting For Stream")
        self.lbl_preview.setPixmap(QPixmap())

    def update_preview(self, qt_img):
        pixmap = QPixmap.fromImage(qt_img)
        scaled_pixmap = pixmap.scaled(
            self.lbl_preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.lbl_preview.setPixmap(scaled_pixmap)

    def update_fps(self, fps):
        self.fps_lbl.setText(f"FPS: {fps}")

    def update_status(self, text):
        self.status_lbl.setText(text)

    def update_source(self, source):
        self.source_lbl.setText(f"Source: {source}")

    def center_window(self):
        qr = self.frameGeometry()
        cp = self.screen().availableGeometry().center()
        qr.moveCenter(cp)
        self.move(qr.topLeft())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.old_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self.old_pos:
            delta = event.globalPosition().toPoint() - self.old_pos
            self.move(self.pos() + delta)
            self.old_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self.old_pos = None

    def closeEvent(self, event):
        self.auto_save_timer.stop()
        self.save_config()
        if self.worker.isRunning():
            self.worker.request_stop()
            self.worker.wait(1500)
        super().closeEvent(event)


if __name__ == "__main__":
    set_windows_app_user_model_id("AouTzxc.HXStreamerReceiver")
    app = QApplication(sys.argv)
    app_icon = get_logo_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    window = ModernReceiverApp()
    window.show()
    sys.exit(app.exec())
