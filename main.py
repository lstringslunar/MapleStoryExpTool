import collections
import json
import os
import sys
import threading
import time

import numpy as np
from PySide6.QtCore import QObject, Signal, Slot, Qt, QRect, QPoint, QSize
from PySide6.QtGui import QPainter, QPixmap, QFontDatabase, QFont, QColor, QGuiApplication, QImage
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QDoubleSpinBox, QSpinBox, QFormLayout, QHBoxLayout, \
    QLabel, QMessageBox, QPushButton, QRubberBand, QSizePolicy, QVBoxLayout, QWidget, QAbstractButton, \
    QGraphicsDropShadowEffect
from windows_capture import WindowsCapture, Frame, InternalCaptureControl

from helper import get_hwnd, get_resource_path, supports_gcb
from ui_ocr_extractor import UiOcrExtractor

# Disable High-DPI Scaling ------------------------------------------------------------------------
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
os.environ["QT_SCALE_FACTOR"] = "1"

# Default Configurations --------------------------------------------------------------------------
WINDOW_TITLE = "新楓之谷：經典版"
UPDATE_INTERVAL_MS = 100

DEFAULT_IDLE_TIMEOUT_MIN = 2.0
AVERAGING_WINDOW_SEC = 600.0
EXP_ROUNDING_DIGITS = -2

MAIN_WINDOW_BASE_WIDTH = 225

# -------------------------------------------------------------------------------------------------


EXP_REQ = [0, 15, 34, 57, 92, 135, 372, 560, 840, 1242, 1716, 2360, 3216, 4200, 5460, 7050, 8840, 11040, 13716, 16680,
           20216, 24402, 28980, 34320, 40512, 47216, 54900, 63666, 73080, 83720, 95700, 108480, 122760, 138666, 155540,
           174216, 194832, 216600, 240500, 266682, 294216, 324240, 356916, 391160, 428280, 468450, 510420, 555680,
           604416, 655200, 709716, 748608, 789631, 832902, 878545, 926689, 977471, 1031036, 1087536, 1147132, 1209994,
           1276301, 1346242, 1420016, 1497832, 1579913, 1666492, 1757815, 1854143, 1955750, 2062925, 2175973, 2295216,
           2410993, 2553663, 2693603, 2841212, 2996910, 3161140, 3334370, 3517093, 3709829, 3913127, 4127566, 4353756,
           4592341, 4844001, 5109452, 5389449, 5684790, 5996316, 6324914, 6671519, 7037118, 7422752, 7829518, 8258575,
           8711144, 9188514, 9692044, 10223168, 10783397, 11374327, 11997640, 12655110, 13348610, 14080113, 14851703,
           15665576, 16524049, 17429566, 18384706, 19392187, 20454878, 21575805, 22758159, 24005306, 25320796, 26708375,
           28171993, 29715818, 31344244, 33061908, 34873700, 36784778, 38800583, 40926854, 43169645, 45535341, 48030677,
           50662758, 53439077, 56367538, 59456479, 62714694, 66151459, 69776558, 73600313, 77633610, 81887931, 86375389,
           91108760, 96101520, 101367883, 106922842, 112782213, 118962678, 125481832, 132358236, 139611467, 147262175,
           155332142, 163844343, 172823012, 182293713, 192283408, 202820538, 213935103, 225658746, 238024845, 251068606,
           264827165, 279339693, 294647508, 310794191, 327825712, 345790561, 364739883, 384727628, 405810702, 428049128,
           451506220, 476248760, 502347192, 529875818, 558913012, 589541445, 621848316, 655925603, 691870326, 729784819,
           769777027, 811960808, 856456260, 903390063, 952895838, 1005114529, 1060194805, 1118293480, 1179575962,
           1244216724, 1312399800, 1384319309, 1460180007, 1540197871, 1624600714, 1713628833, 1807535693, 1906588648,
           2011069705]


def get_settings_path() -> str:
    base_dir = os.getenv("APPDATA") or os.path.expanduser("~")
    config_dir = os.path.join(base_dir, "MapleStoryExpTool")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "settings.json")


def format_exp(value: float, digits: int) -> str:
    rounded = round(value, digits)
    if digits <= 0:
        return f"{int(rounded):,d}"
    return f"{rounded:,.{digits}f}"


def format_time_remaining(seconds_left: float) -> str:
    if seconds_left < 60:
        return "距離升等還要：不到1分鐘"

    total_minutes = int(seconds_left // 60)
    days = total_minutes // (24 * 60)
    hours = (total_minutes % (24 * 60)) // 60
    minutes = total_minutes % 60

    if days > 0:
        return f"距離升等還要：{days}天{hours}小時{minutes}分鐘"
    elif hours > 0:
        return f"距離升等還要：{hours}小時{minutes}分鐘"
    return f"距離升等還要：{minutes}分鐘"


# OCR Components ----------------------------------------------------------------------------------
class CaptureSignals(QObject):
    data_updated = Signal(int, float, float)
    status_changed = Signal(str)


class CaptureWorker:
    def __init__(self, window_title: str):
        self.window_title = window_title
        self.signals = CaptureSignals()
        self.extractor = UiOcrExtractor()
        self._running = True
        self._capture_control = None

        self._should_draw_border = False if supports_gcb() else None

        # Latest raw frame, cached independently of the extractor (which may
        # clear/reuse its own screenshot reference). Used by the calibration
        # dialog. Guarded by a lock since it's written on the capture thread
        # and read from the Qt/UI thread.
        self.last_frame = None
        self._last_frame_lock = threading.Lock()

        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._running = False
        if self._capture_control:
            try:
                self._capture_control.stop()
            except Exception:
                pass

    def get_last_frame(self):
        # Thread-safe snapshot of the most recently captured raw frame -----------------------------
        with self._last_frame_lock:
            return None if self.last_frame is None else self.last_frame.copy()

    def _run(self):
        while self._running:
            capture = None
            try:
                target_hwnd = get_hwnd(WINDOW_TITLE)

                if not target_hwnd:
                    self.signals.status_changed.emit("搜尋遊戲視窗中...")
                    threading.Event().wait(1.0)
                    continue

                capture = WindowsCapture(
                    cursor_capture=False,
                    draw_border=self._should_draw_border,
                    minimum_update_interval=UPDATE_INTERVAL_MS,
                    window_hwnd=target_hwnd
                )

                @capture.event
                def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl):
                    self._capture_control = capture_control

                    if not self._running:
                        capture_control.stop()
                        return

                    try:
                        self.extractor.update(frame.frame_buffer)

                        with self._last_frame_lock:
                            self.last_frame = frame.frame_buffer.copy()

                        # UI template not (yet) matched in this frame - report a short,
                        # stable status instead of falling through to a stale/garbage read.
                        if not self.extractor.is_available():
                            self.signals.status_changed.emit("找不到遊戲介面，偵測中...")
                            return

                        level, conf_lv = self.extractor.get_player_level()
                        experience, conf_exp = self.extractor.get_player_experience()

                        if conf_lv is None or conf_exp is None:
                            self.signals.status_changed.emit("讀取中...")
                            return

                        if conf_lv < 0.8 or conf_exp < 0.8:
                            self.signals.status_changed.emit("讀取中...")
                            return

                        self.extractor._screenshot = None

                        lv_idx = int(level)
                        if 0 <= lv_idx < len(EXP_REQ):
                            requirement = EXP_REQ[lv_idx]
                            percent = (float(experience) * 100 / float(requirement)) if requirement else 0.0
                            self.signals.data_updated.emit(lv_idx, float(experience), percent)
                        else:
                            self.signals.status_changed.emit("等級讀取異常")
                    except Exception as err:
                        # Log the full detail for debugging, but only ever show a short,
                        # bounded message in the UI - a raw exception string can be
                        # arbitrarily long and shouldn't be able to affect the overlay.
                        print(f"[CaptureWorker] Frame processing error: {err}")
                        self.signals.status_changed.emit("處理時發生錯誤")

                @capture.event
                def on_closed():
                    self.signals.status_changed.emit("遊戲視窗已關閉")

                self.signals.status_changed.emit("已連接遊戲視窗")
                capture.start()

            except Exception as e:
                if self._running:
                    # Full detail to console; short, bounded status to the UI.
                    print(f"[CaptureWorker] Capture error: {e}")
                    self.signals.status_changed.emit("擷取畫面時發生錯誤，重試中...")

            finally:
                self._capture_control = None

            for _ in range(10):
                if not self._running:
                    return
                threading.Event().wait(0.1)


# UI COMPONENTS -----------------------------------------------------------------------------------

scale: float = 1.0
scale_value_changed = True


def s(px):
    global scale
    return round(px * scale)


def set_scale(_scale):
    global scale, scale_value_changed
    scale = _scale
    scale_value_changed = True


def get_scale():
    global scale
    return scale


def end_scale():
    global scale_value_changed
    scale_value_changed = False


def add_drop_shadow(parent, blur=4, offset=(1, 1), color=QColor(0, 0, 0, 60)):
    shadow = QGraphicsDropShadowEffect(parent)
    shadow.setBlurRadius(blur)
    shadow.setOffset(*offset)
    shadow.setColor(color)
    parent.setGraphicsEffect(shadow)


class SimpleImageButton(QAbstractButton):
    def __init__(self, resource_folder: str, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.pixmaps = {}
        self._load_images(resource_folder)

    def apply_scale(self):
        if 'normal' in self.pixmaps:
            self.setFixedSize(self.pixmaps['normal'].size() * get_scale())
        self.update()

    def _load_images(self, folder_path: str):
        states = {
            'normal': 'normal.png',
            'hover': 'mouseOver.png',
            'pressed': 'pressed.png',
            'disabled': 'disabled.png'
        }

        for state, filename in states.items():
            path = get_resource_path(os.path.join(folder_path, filename))
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                self.pixmaps[state] = pixmap

        if 'normal' in self.pixmaps:
            self.setFixedSize(self.pixmaps['normal'].size() * get_scale())

    def sizeHint(self) -> QSize:
        if 'normal' in self.pixmaps:
            return self.pixmaps['normal'].size() * get_scale()
        return super().sizeHint()

    def paintEvent(self, event):
        painter = QPainter(self)
        # painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        if not self.isEnabled() and 'disabled' in self.pixmaps:
            current_pixmap = self.pixmaps['disabled']
        elif self.isDown() and 'pressed' in self.pixmaps:
            current_pixmap = self.pixmaps['pressed']
        elif self.underMouse() and 'hover' in self.pixmaps:
            current_pixmap = self.pixmaps['hover']
        else:
            current_pixmap = self.pixmaps.get('normal', QPixmap())

        if not current_pixmap.isNull():
            painter.drawPixmap(self.rect(), current_pixmap)


class SimpleSlicedLabel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pixmaps = {}
        self._load_resources()

    def apply_scale(self):
        self.update()

    def _load_resources(self):
        directions = ['nw', 'n', 'ne', 'w', 'c', 'e', 'sw', 's', 'se']
        for d in directions:
            path = get_resource_path(f"resources/background/{d}.png")
            pixmap = QPixmap(path)
            self.pixmaps[d] = pixmap

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        rect = self.rect()
        w, h = rect.width(), rect.height()

        nw_w = s(self.pixmaps['nw'].width()) if not self.pixmaps['nw'].isNull() else 0
        nw_h = s(self.pixmaps['nw'].height()) if not self.pixmaps['nw'].isNull() else 0
        ne_w = s(self.pixmaps['ne'].width()) if not self.pixmaps['ne'].isNull() else 0
        ne_h = s(self.pixmaps['ne'].height()) if not self.pixmaps['ne'].isNull() else 0
        sw_w = s(self.pixmaps['sw'].width()) if not self.pixmaps['sw'].isNull() else 0
        sw_h = s(self.pixmaps['sw'].height()) if not self.pixmaps['sw'].isNull() else 0
        se_w = s(self.pixmaps['se'].width()) if not self.pixmaps['se'].isNull() else 0
        se_h = s(self.pixmaps['se'].height()) if not self.pixmaps['se'].isNull() else 0

        top_h = max(nw_h, ne_h)
        bottom_h = max(sw_h, se_h)
        left_w = max(nw_w, sw_w)
        right_w = max(ne_w, se_w)

        center_w = max(0, w - left_w - right_w)
        center_h = max(0, h - top_h - bottom_h)

        if not self.pixmaps['nw'].isNull():
            painter.drawPixmap(QRect(0, 0, nw_w, nw_h), self.pixmaps['nw'])
        if not self.pixmaps['n'].isNull() and center_w > 0:
            painter.drawPixmap(QRect(left_w, 0, center_w, top_h), self.pixmaps['n'])
        if not self.pixmaps['ne'].isNull():
            painter.drawPixmap(QRect(w - ne_w, 0, ne_w, ne_h), self.pixmaps['ne'])
        if not self.pixmaps['w'].isNull() and center_h > 0:
            painter.drawPixmap(QRect(0, top_h, left_w, center_h), self.pixmaps['w'])
        if not self.pixmaps['c'].isNull() and center_w > 0 and center_h > 0:
            painter.drawPixmap(QRect(left_w, top_h, center_w, center_h), self.pixmaps['c'])
        if not self.pixmaps['e'].isNull() and center_h > 0:
            painter.drawPixmap(QRect(w - right_w, top_h, right_w, center_h), self.pixmaps['e'])
        if not self.pixmaps['sw'].isNull():
            painter.drawPixmap(QRect(0, h - sw_h, sw_w, sw_h), self.pixmaps['sw'])
        if not self.pixmaps['s'].isNull() and center_w > 0:
            painter.drawPixmap(QRect(left_w, h - bottom_h, center_w, bottom_h), self.pixmaps['s'])
        if not self.pixmaps['se'].isNull():
            painter.drawPixmap(QRect(w - se_w, h - se_h, se_w, se_h), self.pixmaps['se'])


class SimpleLabel(QLabel):
    def set_font_size(self, size):
        _font = self.font()
        _font.setPixelSize(size)
        self.setFont(_font)


class SimpleLine(SimpleLabel):
    # Displays a single line of text --------------------------------------------------------------
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setWordWrap(False)

        add_drop_shadow(self)

        self.set_text(" ")

    def set_text(self, text):
        self.setText(text)

    def set_visible(self, visible):
        self.setVisible(visible)


class PlayerInfoLine(SimpleLine):
    def update_data(self, level: int, experience: float, percent: float, show_percent: bool):
        if show_percent:
            self.setText(f"LV: {level:03d} EXP: {experience:,.0f} [{percent:.02f}%]")
        else:
            self.setText(f"LV: {level:03d} EXP: {experience:,.0f}")


class ExpRateLine(SimpleLine):
    def update_data(self, window_minutes: int, rate_text: str, rate_percent: float, show_percent: bool):
        if show_percent:
            self.set_text(
                f"十分鐘經驗：{rate_text} [{rate_percent:.02f}%]")
        else:
            self.set_text(f"十分鐘經驗：{rate_text}")


class LevelEstimateLine(SimpleLine):
    def update_data(self, text: str):
        self.set_text(text)


class CropSelectLabel(QLabel):
    # A QLabel that shows a pixmap and lets the user drag out one rectangle on top of it (classic
    # rubber-band selection). Emits the finished rectangle in the label's own (displayed-pixmap)
    # coordinate space - the caller is responsible for mapping that back to native pixels.
    selection_made = Signal(QRect)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._origin = QPoint()
        self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self._current_rect = QRect()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.pixmap().isNull():
            self._origin = event.position().toPoint()
            self._rubber_band.setGeometry(QRect(self._origin, QSize()))
            self._rubber_band.show()
            event.accept()

    def mouseMoveEvent(self, event):
        if not self._origin.isNull():
            self._rubber_band.setGeometry(QRect(self._origin, event.position().toPoint()).normalized())
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self._origin.isNull():
            rect = QRect(self._origin, event.position().toPoint()).normalized()
            self._rubber_band.hide()
            self._origin = QPoint()
            self._current_rect = rect
            self.selection_made.emit(rect)
            event.accept()

    def current_rect(self) -> QRect:
        return self._current_rect

    def clear_selection(self):
        self._current_rect = QRect()
        self._rubber_band.hide()


class CalibrationDialog(QDialog):
    calibration_saved = Signal(tuple, tuple)  # lv_box, exp_box

    STEP_LV = 0
    STEP_EXP = 1

    def __init__(self, worker: "CaptureWorker", parent=None):
        super().__init__(parent)
        self.setWindowTitle("手動校正位置")
        self.setModal(True)

        self.worker = worker
        self.step = self.STEP_LV
        self.lv_rect_native = None
        self.exp_rect_native = None
        self.native_image = None  # BGRA, original captured resolution
        self.display_scale = 1.0  # native resolution = displayed resolution * display_scale

        layout = QVBoxLayout(self)

        self.instruction_label = QLabel()
        self.instruction_label.setWordWrap(True)
        layout.addWidget(self.instruction_label)

        self.image_label = CropSelectLabel()
        self.image_label.selection_made.connect(self._on_selection)
        layout.addWidget(self.image_label)

        btn_row = QHBoxLayout()
        self.refresh_btn = QPushButton("重新擷取畫面")
        self.refresh_btn.clicked.connect(self.refresh_capture)
        self.back_btn = QPushButton("上一步")
        self.back_btn.clicked.connect(self.go_back)
        self.next_btn = QPushButton("下一步")
        self.next_btn.clicked.connect(self.go_next)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)

        btn_row.addWidget(self.refresh_btn)
        btn_row.addWidget(self.back_btn)
        btn_row.addWidget(self.next_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self.refresh_capture()
        self._update_step_ui()

    def refresh_capture(self):
        frame = self.worker.get_last_frame()
        if frame is None or frame.size == 0:
            self.instruction_label.setText(
                "尚未取得遊戲畫面，請確認遊戲視窗已開啟並可見於畫面上，"
                "然後按「重新擷取畫面」再試一次。"
            )
            self.image_label.clear()
            self.native_image = None
            return

        self.native_image = np.ascontiguousarray(frame[:, :, :3][:, :, ::-1])  # BGRA -> RGB
        self._render_pixmap()
        self._update_step_ui()

    def _render_pixmap(self):
        if self.native_image is None:
            return

        h, w = self.native_image.shape[:2]
        qi = QImage(self.native_image.data, w, h, self.native_image.strides[0], QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qi)

        screen = QGuiApplication.primaryScreen()
        max_w = int(screen.availableGeometry().width() * 0.8) if screen else 1280
        max_h = int(screen.availableGeometry().height() * 0.7) if screen else 800

        if w > max_w or h > max_h:
            scaled = pixmap.scaled(
                max_w, max_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.display_scale = w / scaled.width()
        else:
            scaled = pixmap
            self.display_scale = 1.0

        self.image_label.setPixmap(scaled)
        self.image_label.setFixedSize(scaled.size())
        self.image_label.clear_selection()
        self.adjustSize()

    def _on_selection(self, rect: QRect):
        native_rect = self._to_native_box(rect)
        if native_rect is None:
            QMessageBox.warning(self, "選取範圍太小", "請重新拖曳出一個較大的範圍，需完整框住數字。")
            return

        if self.step == self.STEP_LV:
            self.lv_rect_native = native_rect
        else:
            self.exp_rect_native = native_rect

        self._update_step_ui()

    def _to_native_box(self, rect: QRect):
        if rect.width() < 3 or rect.height() < 3:
            return None
        scale_factor = self.display_scale
        x1 = round(rect.left() * scale_factor)
        y1 = round(rect.top() * scale_factor)
        x2 = round(rect.right() * scale_factor)
        y2 = round(rect.bottom() * scale_factor)
        return (x1, y1, x2, y2)

    def _update_step_ui(self):
        have_image = self.native_image is not None

        if self.step == self.STEP_LV:
            self.instruction_label.setText(
                "步驟 1 / 2：在下方畫面上拖曳選取 LV「數字」的範圍（僅限數字部分，請勿擷取 LV 字樣），完成後按「下一步」。"
            )
            self.back_btn.setEnabled(False)
            self.next_btn.setText("下一步")
            self.next_btn.setEnabled(have_image and self.lv_rect_native is not None)
        else:
            self.instruction_label.setText(
                "步驟 2 / 2：在下方畫面上拖曳選取 EXP「數字」的範圍（僅限數字部分，請勿擷取 EXP 字樣，可留向右多留一點空間），完成後按「儲存」。"
            )
            self.back_btn.setEnabled(True)
            self.next_btn.setText("儲存")
            self.next_btn.setEnabled(have_image and self.exp_rect_native is not None)

        self.refresh_btn.setEnabled(True)

    def go_back(self):
        self.step = self.STEP_LV
        self._update_step_ui()

    def go_next(self):
        if self.step == self.STEP_LV:
            self.step = self.STEP_EXP
            self._update_step_ui()
            return

        if self.lv_rect_native is None or self.exp_rect_native is None:
            return

        self.calibration_saved.emit(self.lv_rect_native, self.exp_rect_native)
        self.accept()


class SettingsWindow(QDialog):
    settings_changed = Signal()
    calibration_requested = Signal()
    calibration_cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(False)

        # Default settings
        self.show_player_info = True
        self.show_ten_min_exp = True
        self.show_level_estimate = True
        self.show_active_time = True
        self.show_percent = True
        self.idle_timeout_min = DEFAULT_IDLE_TIMEOUT_MIN
        self.new_scale = 100

        # Manual LV/EXP calibration boxes: (x1, y1, x2, y2) native pixel
        # coordinates, or None if auto-detection should be used instead.
        self.manual_lv_box = None
        self.manual_exp_box = None

        # Load user settings
        self.load_settings()

        # Background frame
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)

        self.bg_frame = SimpleSlicedLabel()
        root_layout.addWidget(self.bg_frame)

        self.frame_layout = QVBoxLayout(self.bg_frame)

        self.title_label = SimpleLabel(" 設定")
        self.title_label.setStyleSheet("color: black; font-weight: bold; background: transparent;")
        add_drop_shadow(self.title_label, 3, (1.5, 1.5))

        self.frame_layout.addWidget(self.title_label)

        self.form = QFormLayout()
        self.form.setContentsMargins(5, 8, 5, 5)
        self.form.setSpacing(5)

        self.player_info_checkbox = QCheckBox("顯示系統/玩家資訊", self)
        self.ten_min_exp_checkbox = QCheckBox("顯示十分鐘經驗", self)
        self.level_estimate_checkbox = QCheckBox("顯示升等時間", self)
        self.active_time_checkbox = QCheckBox("顯示持續練等時間", self)
        self.percent_checkbox = QCheckBox("顯示百分比", self)

        self.player_info_checkbox.setChecked(self.show_player_info)
        self.ten_min_exp_checkbox.setChecked(self.show_ten_min_exp)
        self.level_estimate_checkbox.setChecked(self.show_level_estimate)
        self.active_time_checkbox.setChecked(self.show_active_time)
        self.percent_checkbox.setChecked(self.show_percent)

        for cb in [self.player_info_checkbox, self.ten_min_exp_checkbox, self.level_estimate_checkbox,
                   self.active_time_checkbox, self.percent_checkbox]:
            add_drop_shadow(cb)

        self.idle_label = SimpleLabel("閒置倒數(分鐘)：", self)
        self.idle_label.setStyleSheet("color: black; background: transparent;")
        add_drop_shadow(self.idle_label)

        self.idle_timeout_spin = QDoubleSpinBox(self)
        self.idle_timeout_spin.setRange(0, 5.0)
        self.idle_timeout_spin.setSingleStep(0.5)
        self.idle_timeout_spin.setDecimals(1)
        self.idle_timeout_spin.setSuffix(" 分鐘")
        self.idle_timeout_spin.setValue(self.idle_timeout_min)
        add_drop_shadow(self.idle_timeout_spin)

        self.ui_scale_label = SimpleLabel("UI 比例(%)：", self)
        self.ui_scale_label.setStyleSheet("color: black; background: transparent;")
        add_drop_shadow(self.ui_scale_label)

        self.ui_scale_spin = QSpinBox(self)
        self.ui_scale_spin.setRange(50, 300)
        self.ui_scale_spin.setSingleStep(10)
        self.ui_scale_spin.setSuffix(" %")
        self.ui_scale_spin.setValue(self.new_scale)
        add_drop_shadow(self.ui_scale_spin)

        self.form.addRow(self.player_info_checkbox)
        self.form.addRow(self.ten_min_exp_checkbox)
        self.form.addRow(self.level_estimate_checkbox)
        self.form.addRow(self.active_time_checkbox)
        self.form.addRow(self.percent_checkbox)
        self.form.addRow(self.idle_label, self.idle_timeout_spin)
        self.form.addRow(self.ui_scale_label, self.ui_scale_spin)

        # Manual LV/EXP position calibration ------------------------------------------------------
        self.calibration_status_label = SimpleLine(self)
        self.calibration_status_label.setStyleSheet("color: black; background: transparent; ")
        self._update_calibration_status_label()

        # self.calibrate_button = QPushButton("手動校正位置...", self)
        self.calibrate_button = SimpleImageButton("resources/manual", self)
        self.calibrate_button.clicked.connect(self.calibration_requested.emit)

        # self.clear_calibration_button = QPushButton("改回自動偵測", self)
        self.clear_calibration_button = SimpleImageButton("resources/auto", self)
        self.clear_calibration_button.clicked.connect(self.clear_manual_boxes)

        self.calibration_row = QHBoxLayout()
        self.calibration_row.addWidget(self.calibrate_button)
        self.calibration_row.addWidget(self.clear_calibration_button)

        self.form.addRow(self.calibration_status_label)
        self.form.addRow(self.calibration_row)

        self.return_button = SimpleImageButton("resources/confirm", self)
        self.return_button.clicked.connect(self._close_settings)

        self.button_layout = QHBoxLayout()
        self.button_layout.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.button_layout.addWidget(self.return_button)

        self.frame_layout.addLayout(self.form)
        self.frame_layout.addSpacing(10)
        self.frame_layout.addLayout(self.button_layout)

        self.player_info_checkbox.toggled.connect(self._emit_changed)
        self.ten_min_exp_checkbox.toggled.connect(self._emit_changed)
        self.level_estimate_checkbox.toggled.connect(self._emit_changed)
        self.active_time_checkbox.toggled.connect(self._emit_changed)
        self.percent_checkbox.toggled.connect(self._emit_changed)
        self.idle_timeout_spin.valueChanged.connect(self._emit_changed)
        self.ui_scale_spin.valueChanged.connect(self._emit_changed)

        self.apply_scale()
        self.adjustSize()

    def apply_scale(self):

        self.bg_frame.apply_scale()
        self.frame_layout.setContentsMargins(s(8), 0, s(8), 0)
        self.frame_layout.setSpacing(0)

        self.title_label.setFixedHeight(s(24))
        self.title_label.set_font_size(s(12))

        self.form.setContentsMargins(s(4), s(8), s(4), 0)
        self.form.setSpacing(s(6))

        checkbox_style = f"""
            QCheckBox {{ color: black; font-size: {s(12)}px; }}
            QCheckBox::indicator {{ width: {s(12)}px; height: {s(12)}px; }}
            QCheckBox::indicator:unchecked {{ border-image: url("{get_resource_path("resources/check/0.png")}"); }}
            QCheckBox::indicator:checked {{ border-image: url("{get_resource_path("resources/check/1.png")}"); }}
        """
        for cb in [self.player_info_checkbox, self.ten_min_exp_checkbox, self.level_estimate_checkbox,
                   self.active_time_checkbox, self.percent_checkbox]:
            cb.setStyleSheet(checkbox_style)

        back = get_resource_path("resources/arrows/back.png")
        forward = get_resource_path("resources/arrows/forward.png")

        double_sb_style = f"""
            QDoubleSpinBox {{
                background: transparent;
                color: black;
                font-size: {s(12)}px;
                border: none;
                border-bottom: 1px solid #A0A0A0;
                border-radius: 0px; /* Prevents default OS rounded corners from clipping the line */
                padding: 0px;
                max-width: {s(80)}px;                
            }}
            QDoubleSpinBox:hover,
            QDoubleSpinBox:focus {{
                background: transparent;
                color: black;
                border: none;
                border-bottom: 1px solid #505050;
            }}
            QDoubleSpinBox::up-button {{
                image: url("{forward}");
                width: {s(12)}px;
                height: {s(12)}px;
                subcontrol-position: right;
            }}
            QDoubleSpinBox::down-button {{
                image: url("{back}");
                width: {s(12)}px;
                height: {s(12)}px;
                subcontrol-position: left;
            }}
        """
        sb_style = double_sb_style.replace('Double', '')

        self.idle_label.set_font_size(s(12))
        self.idle_timeout_spin.setStyleSheet(double_sb_style)
        self.idle_timeout_spin.setFixedHeight(s(24))

        self.ui_scale_label.set_font_size(s(12))
        self.ui_scale_spin.setStyleSheet(sb_style)
        self.ui_scale_spin.setFixedHeight(s(24))

        self.calibration_status_label.set_font_size(s(12))
        self.calibration_status_label.setContentsMargins(0, s(8), 0, 0)

        self.calibrate_button.apply_scale()
        self.clear_calibration_button.apply_scale()

        self.button_layout.setContentsMargins(0, 0, s(4), s(14))
        self.button_layout.setSpacing(0)

        self.return_button.apply_scale()
        self.adjustSize()

    def _emit_changed(self):
        self.show_player_info = self.player_info_checkbox.isChecked()
        self.show_ten_min_exp = self.ten_min_exp_checkbox.isChecked()
        self.show_level_estimate = self.level_estimate_checkbox.isChecked()
        self.show_active_time = self.active_time_checkbox.isChecked()
        self.show_percent = self.percent_checkbox.isChecked()
        self.idle_timeout_min = self.idle_timeout_spin.value()
        self.new_scale = self.ui_scale_spin.value()

        self.settings_changed.emit()
        self.save_settings()

    def _close_settings(self):
        self.hide()

    def set_manual_boxes(self, lv_box: tuple, exp_box: tuple):
        self.manual_lv_box = tuple(lv_box)
        self.manual_exp_box = tuple(exp_box)
        self._update_calibration_status_label()
        self.save_settings()

    def clear_manual_boxes(self):
        if self.manual_lv_box is None and self.manual_exp_box is None:
            return
        self.manual_lv_box = None
        self.manual_exp_box = None
        self._update_calibration_status_label()
        self.save_settings()
        self.calibration_cleared.emit()

    def _update_calibration_status_label(self):
        if self.manual_lv_box is not None and self.manual_exp_box is not None:
            self.calibration_status_label.set_text("目前使用：手動校正位置")
        else:
            self.calibration_status_label.set_text("目前使用：自動偵測位置")

    def load_settings(self):
        try:
            with open(get_settings_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
            return

        if not isinstance(data, dict):
            return

        self.show_player_info = bool(data.get("show_player_info", self.show_player_info))
        self.show_ten_min_exp = bool(data.get("show_ten_min_exp", self.show_ten_min_exp))
        self.show_level_estimate = bool(data.get("show_level_estimate", self.show_level_estimate))
        self.show_active_time = bool(data.get("show_active_time", self.show_active_time))
        self.show_percent = bool(data.get("show_percent", self.show_percent))

        try:
            idle_timeout_min = float(data.get("idle_timeout_min", self.idle_timeout_min))
            if 0.0 <= idle_timeout_min <= 5.0:
                self.idle_timeout_min = idle_timeout_min
        except (TypeError, ValueError):
            pass

        try:
            ui_scale = int(data.get("ui_scale", self.new_scale))
            if 50 <= ui_scale <= 300:
                self.new_scale = ui_scale
                set_scale(ui_scale / 100.0)
        except (TypeError, ValueError):
            pass

        self.manual_lv_box = self._load_box(data.get("manual_lv_box"))
        self.manual_exp_box = self._load_box(data.get("manual_exp_box"))

        if self.manual_lv_box is None or self.manual_exp_box is None:
            self.manual_lv_box = None
            self.manual_exp_box = None

    @staticmethod
    def _load_box(value):
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        try:
            return tuple(int(v) for v in value)
        except (TypeError, ValueError):
            return None

    def save_settings(self):
        data = {
            "show_player_info": self.show_player_info,
            "show_ten_min_exp": self.show_ten_min_exp,
            "show_level_estimate": self.show_level_estimate,
            "show_active_time": self.show_active_time,
            "show_percent": self.show_percent,
            "idle_timeout_min": self.idle_timeout_min,
            "ui_scale": self.new_scale,
            "manual_lv_box": list(self.manual_lv_box) if self.manual_lv_box else None,
            "manual_exp_box": list(self.manual_exp_box) if self.manual_exp_box else None,
        }
        try:
            with open(get_settings_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as err:
            print(f"[SettingsWindow] Failed to save settings: {err}")

    def closeEvent(self, event):
        self.hide()
        event.ignore()


class OverlayWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.last_data = None
        self._drag_position = QPoint()

        # Settings window setup
        self.settings_window = SettingsWindow()
        self.settings_window.settings_changed.connect(self.apply_settings)
        self.settings_window.calibration_requested.connect(self.open_calibration)
        self.settings_window.calibration_cleared.connect(self.clear_manual_calibration)

        # Background frame
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        self.bg_frame = SimpleSlicedLabel()
        root_layout.addWidget(self.bg_frame)

        self.frame_layout = QVBoxLayout(self.bg_frame)

        # Custom Overlay Title Bar
        self.title_bar = QWidget()
        self.title_bar.setFixedHeight(24)
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(2)

        self.title_label = SimpleLabel(" 經驗值計算器")
        self.title_label.setStyleSheet("color: black; font-weight: bold; background: transparent;")
        add_drop_shadow(self.title_label, 3, (1.5, 1.5))

        self.settings_button = SimpleImageButton("resources/open", self)
        self.settings_button.clicked.connect(self.show_settings)

        self.close_button = SimpleImageButton("resources/close", self)
        self.close_button.clicked.connect(self.close)

        title_layout.addWidget(self.title_label)
        title_layout.addStretch()
        title_layout.addWidget(self.settings_button)
        title_layout.addWidget(self.close_button)

        self.frame_layout.addWidget(self.title_bar)

        self.main_layout = QVBoxLayout()

        # Modular information display components
        self.player_info_line = PlayerInfoLine(self)
        self.exp_rate_line = ExpRateLine(self)
        self.level_estimate_line = LevelEstimateLine(self)
        self.active_time_line = SimpleLine(self)

        style = "color: black; background: transparent;"
        self.player_info_line.setStyleSheet(style)
        self.exp_rate_line.setStyleSheet(style)
        self.level_estimate_line.setStyleSheet(style)
        self.active_time_line.setStyleSheet(style)

        self.main_layout.addWidget(self.player_info_line)
        self.main_layout.addWidget(self.exp_rate_line)
        self.main_layout.addWidget(self.level_estimate_line)
        self.main_layout.addWidget(self.active_time_line)

        # Bottom button row
        self.button_layout = QHBoxLayout()
        self.button_layout.setAlignment(Qt.AlignmentFlag.AlignRight)

        self.reset_button = SimpleImageButton("resources/reset/", self)
        self.reset_button.clicked.connect(self.reset_tracker)

        self.button_layout.addWidget(self.reset_button)
        self.main_layout.addLayout(self.button_layout)
        self.frame_layout.addLayout(self.main_layout)

        # Initialize base styling scales
        self.current_scale = self.settings_window.new_scale / 100.0
        self.apply_scale()
        self.settings_window.apply_scale()

        # Worker initialization
        self.worker = CaptureWorker(WINDOW_TITLE)
        self.worker.signals.data_updated.connect(self.update_stats)
        self.worker.signals.status_changed.connect(self.update_status)
        self.worker.start()

        if self.settings_window.manual_lv_box and self.settings_window.manual_exp_box:
            self.worker.extractor.set_manual_boxes(
                self.settings_window.manual_lv_box,
                self.settings_window.manual_exp_box,
            )

        # O(1) EXP lookups
        self.base_exp_for_level = [0] * len(EXP_REQ)
        total = 0
        for i in range(len(EXP_REQ)):
            self.base_exp_for_level[i] = total
            total += EXP_REQ[i]

        # Tracking state
        self.verified_abs_exp = -1.0
        self.last_gain_time = 0.0
        self.active_session_start = -1.0

        # History queue
        self.exp_history = collections.deque()
        self.last_history_update = 0.0

        self.recompute_visibility()
        self.adjustSize()

    def apply_scale(self):
        self.setFixedWidth(s(MAIN_WINDOW_BASE_WIDTH))

        self.bg_frame.apply_scale()
        self.frame_layout.setContentsMargins(s(8), 0, s(8), 0)
        self.frame_layout.setSpacing(0)

        self.title_bar.setFixedHeight(s(24))
        self.title_label.set_font_size(s(12))
        self.settings_button.apply_scale()
        self.close_button.apply_scale()

        self.main_layout.setContentsMargins(s(4), s(8), s(4), 0)
        self.main_layout.setSpacing(s(6))

        self.player_info_line.set_font_size(s(12))
        self.exp_rate_line.set_font_size(s(12))
        self.level_estimate_line.set_font_size(s(12))
        self.active_time_line.set_font_size(s(12))

        self.button_layout.setContentsMargins(0, 0, 0, s(14))
        self.button_layout.setSpacing(0)

        self.reset_button.apply_scale()

        self.adjustSize()

    def moveEvent(self, event):
        # Handle synchronous movement of fixed settings window ------------------------------------
        super().moveEvent(event)
        if self.settings_window.isVisible():
            self.sync_settings_position()

    def sync_settings_position(self):
        # Anchors the settings frame to the right of the main UI, aligned flat --------------------
        self.settings_window.move(
            self.x() + self.width(),
            self.y()
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= s(24):
            self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and not self._drag_position.isNull():
            self.move(event.globalPosition().toPoint() - self._drag_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_position = QPoint()

    @property
    def idle_timeout_sec(self) -> float:
        return self.settings_window.idle_timeout_min * 60.0

    @Slot()
    def show_settings(self):
        self.settings_window.adjustSize()
        self.sync_settings_position()
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    @Slot()
    def open_calibration(self):
        dialog = CalibrationDialog(self.worker, self.settings_window)
        dialog.calibration_saved.connect(self._on_calibration_saved)
        dialog.exec()

    @Slot(tuple, tuple)
    def _on_calibration_saved(self, lv_box: tuple, exp_box: tuple):
        self.worker.extractor.set_manual_boxes(lv_box, exp_box)
        self.settings_window.set_manual_boxes(lv_box, exp_box)
        # Force a fresh read on the next frame rather than showing a stale value.
        self.last_data = None
        self.reset_tracker()

    @Slot()
    def clear_manual_calibration(self):
        self.worker.extractor.clear_manual_boxes()

    @Slot()
    def apply_settings(self):
        # Apply settings and recalculate scaling factors dynamically ------------------------------
        new_scale = self.settings_window.new_scale / 100.0

        if new_scale != get_scale():
            set_scale(new_scale)
            self.apply_scale()
            self.settings_window.apply_scale()

        if self.last_data is not None:
            self.update_stats(*self.last_data)
        else:
            self.recompute_visibility()
            self.adjustSize()

        # Ensure exact geometry calculation is completed before anchoring to the right side
        QApplication.processEvents()

        if self.settings_window.isVisible():
            self.sync_settings_position()

    def recompute_visibility(self):
        self.player_info_line.setVisible(self.settings_window.show_player_info)
        self.exp_rate_line.setVisible(self.settings_window.show_ten_min_exp)
        self.level_estimate_line.setVisible(self.settings_window.show_level_estimate)
        self.active_time_line.setVisible(self.settings_window.show_active_time)

        self.main_layout.activate()
        self.adjustSize()

    @Slot()
    def reset_tracker(self):
        # Clears accumulated EXP history and restarts tracking instantly --------------------------
        self.verified_abs_exp = -1.0
        self.last_gain_time = 0.0
        self.active_session_start = -1.0
        self.exp_history.clear()
        self.last_history_update = 0.0
        self.last_data = None

        # mins = int(AVERAGING_WINDOW_SEC / 60)

        self.player_info_line.set_text("統計已重置")
        self.exp_rate_line.set_text(f"十分鐘經驗：計算中...")
        self.level_estimate_line.set_text("距離升等還要：計算中...")
        self.active_time_line.set_text("持續練等：00:00")

        self.recompute_visibility()

    @Slot(int, float, float)
    def update_stats(self, level: int, experience: float, percent: float):
        # Update player exp status ----------------------------------------------------------------
        self.last_data = (level, experience, percent)

        # 1. Hard bounds check.
        if level < 0 or level >= len(EXP_REQ):
            return
        if experience < 0 or experience > EXP_REQ[level]:
            return

        current_abs_exp = self.base_exp_for_level[level] + experience

        # 2. Strict jump rejection check (> 5% of level EXP requirement).
        if self.verified_abs_exp != -1.0:
            delta = current_abs_exp - self.verified_abs_exp
            max_allowed_jump = max(EXP_REQ[level] * 0.05, 100.0)

            if delta > max_allowed_jump:
                return

        current_time = time.time()

        # Initial initialization.
        if self.verified_abs_exp == -1.0:
            self.verified_abs_exp = current_abs_exp
            self.last_gain_time = current_time
            self.active_session_start = current_time
            self.exp_history.append((current_time, self.verified_abs_exp))
            return

        # 3. Idle & break detection.
        exp_delta = current_abs_exp - self.verified_abs_exp

        if exp_delta > 0:
            if (current_time - self.last_gain_time) >= self.idle_timeout_sec:
                # Reset tracking values upon returning from idle
                self.exp_history.clear()
                self.exp_history.append((current_time, current_abs_exp))
                self.active_session_start = current_time

            self.last_gain_time = current_time
            self.verified_abs_exp = current_abs_exp

        # Ensure continuous timer logic handles session restarts correctly
        if self.active_session_start == -1.0:
            self.active_session_start = current_time

        # 4. History queue maintenance.
        if current_time - self.last_history_update >= 1.0:
            self.exp_history.append((current_time, self.verified_abs_exp))
            self.last_history_update = current_time

            while self.exp_history and (current_time - self.exp_history[0][0]) > AVERAGING_WINDOW_SEC:
                self.exp_history.popleft()

        # 5. Calculate average rate & formatting.
        is_idle = (current_time - self.last_gain_time) >= self.idle_timeout_sec
        window_minutes = int(AVERAGING_WINDOW_SEC / 60)
        req = EXP_REQ[level]

        rate_text = None
        rate_percent = 0
        eta_text = "距離升等還要：閒置中..."
        active_time_text = "持續練等：閒置中"

        if is_idle:
            rate_text = "閒置中"
            eta_text = "距離升等還要：閒置中..."
        else:
            # Active time calculation logic
            active_seconds = int(current_time - self.active_session_start)
            h = active_seconds // 3600
            m = (active_seconds % 3600) // 60
            s = active_seconds % 60
            if h > 0:
                active_time_text = f"持續練等：{h:02d}:{m:02d}:{s:02d}"
            else:
                active_time_text = f"持續練等：{m:02d}:{s:02d}"

            if len(self.exp_history) > 1:
                oldest_time, oldest_abs_exp = self.exp_history[0]
                time_window = current_time - oldest_time
                gained_in_window = self.verified_abs_exp - oldest_abs_exp

                if time_window > 0:
                    raw_rate = max(0.0, (gained_in_window / time_window) * AVERAGING_WINDOW_SEC)
                    rate_text = format_exp(raw_rate, EXP_ROUNDING_DIGITS)
                    rate_percent = (raw_rate * 100.0 / req) if req > 0 else 0.0

                    remaining_exp = max(0.0, float(req) - experience)

                    if raw_rate > 0:
                        seconds_left = (remaining_exp * AVERAGING_WINDOW_SEC) / raw_rate
                        eta_text = format_time_remaining(seconds_left)
                    else:
                        eta_text = "距離升等還要：無法估算"
                else:
                    rate_text = format_exp(0.0, EXP_ROUNDING_DIGITS)
                    rate_percent = 0.0
                    eta_text = "距離升等還要：無法估算"
            else:
                rate_text = "計算中..."
                eta_text = "距離升等還要：計算中..."

        # 6. Modular UI rendering.
        if self.settings_window.show_player_info:
            self.player_info_line.update_data(
                level,
                experience,
                percent,
                self.settings_window.show_percent,
            )

        if self.settings_window.show_ten_min_exp:
            self.exp_rate_line.update_data(
                window_minutes,
                rate_text,
                rate_percent,
                self.settings_window.show_percent,
            )

        if self.settings_window.show_level_estimate:
            self.level_estimate_line.update_data(eta_text)

        if self.settings_window.show_active_time:
            self.active_time_line.set_text(active_time_text)

        self.recompute_visibility()

        self.raise_()
        self.show()

    @Slot(str)
    def update_status(self, text: str):
        # Show capture status without breaking the modular display layout -------------------------
        if self.last_data is None:
            self.player_info_line.set_text(text)
        else:
            if self.settings_window.show_player_info:
                self.player_info_line.set_text(text)

        self.recompute_visibility()
        self.raise_()
        self.show()

    def closeEvent(self, event):
        self.worker.stop()
        self.settings_window.close()
        event.accept()
        os._exit(0)


if __name__ == '__main__':
    app = QApplication(sys.argv)

    # Load font file ------------------------------------------------------------------------------
    font_id = QFontDatabase.addApplicationFont(get_resource_path('resources/fonts/Huninn-Regular.ttf'))
    font_families = QFontDatabase.applicationFontFamilies(font_id)
    if font_families:
        font = QFont(font_families[0], 12)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        app.setFont(font)

    # Create overlay
    window = OverlayWindow()
    window.show()

    sys.exit(app.exec())
