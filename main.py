import collections
import json
import os
import sys
import threading
import time

from PySide6.QtCore import QObject, Signal, Slot, Qt, QRect, QPoint, QSize
from PySide6.QtGui import QFontMetrics, QPainter, QPixmap, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QAbstractButton,
)
from windows_capture import WindowsCapture, Frame, InternalCaptureControl

from helper import get_hwnd, get_resource_path
from ui_ocr_extractor import UiOcrExtractor

WINDOW_TITLE = "新楓之谷：經典版"
UPDATE_INTERVAL_MS = 100

# DEFAULT CONFIGURATIONS --------------------------------------------------------------------------
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
    config_dir = os.path.join(base_dir, "ExpTracker")
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
                    draw_border=False,
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


# ==================== CUSTOM STYLING COMPONENTS ====================

class ImageButton(QAbstractButton):
    def __init__(self, resource_folder: str, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.pixmaps = {}
        self.scale = 1.0
        self._load_images(resource_folder)

    def set_scale(self, scale: float):
        self.scale = scale
        if 'normal' in self.pixmaps:
            self.setFixedSize(self.pixmaps['normal'].size() * self.scale)
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
            self.setFixedSize(self.pixmaps['normal'].size() * self.scale)

    def sizeHint(self) -> QSize:
        if 'normal' in self.pixmaps:
            return self.pixmaps['normal'].size() * self.scale
        return super().sizeHint()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        if not self.isEnabled() and 'disabled' in self.pixmaps:
            current_pixmap = self.pixmaps['disabled']
        elif self.isDown() and 'pressed' in self.pixmaps:
            current_pixmap = self.pixmaps['pressed']
        elif self.underMouse() and 'hover' in self.pixmaps:
            current_pixmap = self.pixmaps['hover']
        else:
            current_pixmap = self.pixmaps.get('normal', QPixmap())

        if not current_pixmap.isNull():
            # Drawing to self.rect() automatically stretches properly due to scale
            painter.drawPixmap(self.rect(), current_pixmap)


class NineSliceWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pixmaps = {}
        self.scale = 1.0
        self._load_resources()

    def set_scale(self, scale: float):
        self.scale = scale
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

        # Apply scaling to the corners
        nw_w = int(self.pixmaps['nw'].width() * self.scale) if not self.pixmaps['nw'].isNull() else 0
        nw_h = int(self.pixmaps['nw'].height() * self.scale) if not self.pixmaps['nw'].isNull() else 0
        ne_w = int(self.pixmaps['ne'].width() * self.scale) if not self.pixmaps['ne'].isNull() else 0
        ne_h = int(self.pixmaps['ne'].height() * self.scale) if not self.pixmaps['ne'].isNull() else 0
        sw_w = int(self.pixmaps['sw'].width() * self.scale) if not self.pixmaps['sw'].isNull() else 0
        sw_h = int(self.pixmaps['sw'].height() * self.scale) if not self.pixmaps['sw'].isNull() else 0
        se_w = int(self.pixmaps['se'].width() * self.scale) if not self.pixmaps['se'].isNull() else 0
        se_h = int(self.pixmaps['se'].height() * self.scale) if not self.pixmaps['se'].isNull() else 0

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


# ==================== UI COMPONENTS ====================

class InfoLine(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self._full_text = ""

        self.label = QLabel(self)
        self.label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self.label.setWordWrap(False)
        # Ignored horizontal policy: the label's natural text width must
        # never dictate the layout's/window's preferred size. Width comes
        # only from the fixed-width parent window.
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.label)

    def set_text(self, text: str):
        # Defensive: never let a None/non-str value reach the label.
        self._full_text = "" if text is None else str(text)
        self._apply_elided_text()

    def _apply_elided_text(self):
        available_width = self.label.width()
        if available_width <= 0:
            # Not laid out yet; fall back to the full text for now, the
            # next resizeEvent will re-elide once a real width is known.
            self.label.setText(self._full_text)
            return

        metrics = QFontMetrics(self.label.font())
        elided = metrics.elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, available_width
        )
        self.label.setText(elided)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elided_text()

    def set_visible(self, visible: bool):
        super().setVisible(visible)


class PlayerInfoLine(InfoLine):
    def update_data(self, level: int, experience: float, percent: float, show_percent: bool):
        if show_percent:
            self.set_text(
                f"LV: {level:03d} | EXP: {experience:,.0f} [{percent:.02f}%]"
            )
        else:
            self.set_text(f"LV: {level:03d} | EXP: {experience:,.0f}")


class ExpRateLine(InfoLine):
    def update_data(
            self,
            window_minutes: int,
            rate_text: str,
            rate_percent: float | None,
            show_percent: bool,
    ):
        if show_percent and rate_percent is not None:
            self.set_text(
                f"{window_minutes}分鐘經驗：{rate_text} [{rate_percent:.02f}%]"
            )
        else:
            self.set_text(f"{window_minutes}分鐘經驗：<b>{rate_text}</b>")


class LevelEstimateLine(InfoLine):
    def update_data(self, text: str):
        self.set_text(text)


class OverlayButton(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.set_scale(1.0)

    def set_scale(self, scale: float):
        self.setStyleSheet(f"""
            QPushButton {{
                color: #FFFFFF;
                background-color: rgba(50, 50, 50, 200);
                border: 1px solid #777777;
                border-radius: {int(4 * scale)}px;
                padding: {int(2 * scale)}px {int(6 * scale)}px;
                font-size: {int(11 * scale)}px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: rgba(80, 80, 80, 230);
            }}
            QPushButton:pressed {{
                background-color: rgba(30, 30, 30, 250);
            }}
        """)


class SettingsWindow(QDialog):
    settings_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(False)

        self.show_player_info = True
        self.show_ten_min_exp = True
        self.show_level_estimate = True
        self.show_active_time = True
        self.show_percent = True
        self.idle_timeout_min = DEFAULT_IDLE_TIMEOUT_MIN
        self.ui_scale = 100

        # Overwrite the defaults above with the user's settings
        self.load_settings()

        # Root layout with 9-slice frame
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)

        self.bg_frame = NineSliceWidget()
        root_layout.addWidget(self.bg_frame)

        self.frame_layout = QVBoxLayout(self.bg_frame)
        self.frame_layout.setContentsMargins(10, 0, 10, 12)
        self.frame_layout.setSpacing(0)

        # Title Bar
        self.title_bar = QWidget()
        self.title_bar.setFixedHeight(24)
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(0, 0, 0, 0)

        self.title_label = QLabel("設定")
        self.title_label.setStyleSheet("color: black; font-weight: bold; font-size: 12px; background: transparent;")

        self.close_button = ImageButton("resources/close", self)
        self.close_button.clicked.connect(self._close_settings)

        title_layout.addWidget(self.title_label)
        title_layout.addStretch()
        title_layout.addWidget(self.close_button)

        self.frame_layout.addWidget(self.title_bar)

        # Body form area
        self.form = QFormLayout()
        self.form.setContentsMargins(5, 8, 5, 5)
        self.form.setSpacing(5)

        self.player_info_checkbox = QCheckBox("顯示玩家資訊(LV, EXP)", self)
        self.ten_min_exp_checkbox = QCheckBox("顯示十分鐘經驗", self)
        self.level_estimate_checkbox = QCheckBox("顯示升等推估", self)
        self.active_time_checkbox = QCheckBox("顯示持續練等時間", self)
        self.percent_checkbox = QCheckBox("顯示百分比", self)

        self.player_info_checkbox.setChecked(self.show_player_info)
        self.ten_min_exp_checkbox.setChecked(self.show_ten_min_exp)
        self.level_estimate_checkbox.setChecked(self.show_level_estimate)
        self.active_time_checkbox.setChecked(self.show_active_time)
        self.percent_checkbox.setChecked(self.show_percent)

        self.idle_timeout_spin = QDoubleSpinBox(self)
        self.idle_timeout_spin.setRange(0.1, 120.0)
        self.idle_timeout_spin.setSingleStep(0.5)
        self.idle_timeout_spin.setDecimals(1)
        self.idle_timeout_spin.setSuffix(" 分鐘")
        self.idle_timeout_spin.setValue(self.idle_timeout_min)

        self.idle_label = QLabel("閒置倒數(分鐘)：", self)

        # Global UI Scale Control
        self.ui_scale_spin = QSpinBox(self)
        self.ui_scale_spin.setRange(50, 300)
        self.ui_scale_spin.setSingleStep(10)
        self.ui_scale_spin.setSuffix(" %")
        self.ui_scale_spin.setValue(self.ui_scale)

        self.ui_scale_label = QLabel("UI 比例(%)：", self)

        self.form.addRow(self.player_info_checkbox)
        self.form.addRow(self.ten_min_exp_checkbox)
        self.form.addRow(self.level_estimate_checkbox)
        self.form.addRow(self.active_time_checkbox)
        self.form.addRow(self.percent_checkbox)
        self.form.addRow(self.idle_label, self.idle_timeout_spin)
        self.form.addRow(self.ui_scale_label, self.ui_scale_spin)

        # Confirm/Return button using resource/confirm/
        self.return_button = ImageButton("resources/confirm", self)
        self.return_button.clicked.connect(self._close_settings)

        self.btn_layout = QHBoxLayout()
        self.btn_layout.addStretch()
        self.btn_layout.addWidget(self.return_button)
        self.btn_layout.addStretch()

        self.frame_layout.addLayout(self.form)
        self.frame_layout.addSpacing(10)
        self.frame_layout.addLayout(self.btn_layout)

        self.player_info_checkbox.toggled.connect(self._emit_changed)
        self.ten_min_exp_checkbox.toggled.connect(self._emit_changed)
        self.level_estimate_checkbox.toggled.connect(self._emit_changed)
        self.active_time_checkbox.toggled.connect(self._emit_changed)
        self.percent_checkbox.toggled.connect(self._emit_changed)
        self.idle_timeout_spin.valueChanged.connect(self._emit_changed)
        self.ui_scale_spin.valueChanged.connect(self._emit_changed)

        # Base application styling logic
        self.apply_scale(1.0)
        self.adjustSize()

    def apply_scale(self, scale: float):
        self.bg_frame.set_scale(scale)
        self.bg_frame.layout().setContentsMargins(int(10 * scale), 0, int(10 * scale), int(12 * scale))

        self.title_bar.setFixedHeight(int(24 * scale))
        self.title_label.setStyleSheet(
            f"color: black; font-weight: bold; font-size: {int(12 * scale)}px; background: transparent;")
        self.close_button.set_scale(scale)

        self.form.setContentsMargins(int(5 * scale), int(8 * scale), int(5 * scale), int(5 * scale))
        self.form.setSpacing(int(5 * scale))

        check0 = get_resource_path("resources/check/0.png").as_posix()
        check1 = get_resource_path("resources/check/1.png").as_posix()

        checkbox_style = f"""
        QCheckBox {{ color: black; font-weight: bold; font-size: {int(12 * scale)}px; }}
        QCheckBox::indicator {{ width: {int(12 * scale)}px; height: {int(12 * scale)}px; }}
        QCheckBox::indicator:unchecked {{ image: url("{check0}"); }}
        QCheckBox::indicator:checked {{ image: url("{check1}"); }}
        """
        for cb in [self.player_info_checkbox, self.ten_min_exp_checkbox, self.level_estimate_checkbox,
                   self.active_time_checkbox, self.percent_checkbox]:
            cb.setStyleSheet(checkbox_style)

        self.idle_label.setStyleSheet(f"color: black; font-weight: bold; font-size: {int(12 * scale)}px;")
        self.idle_timeout_spin.setStyleSheet(f"color: black; font-weight: bold; font-size: {int(12 * scale)}px;")

        self.ui_scale_label.setStyleSheet(f"color: black; font-weight: bold; font-size: {int(12 * scale)}px;")
        self.ui_scale_spin.setStyleSheet(f"color: black; font-weight: bold; font-size: {int(12 * scale)}px;")

        self.return_button.set_scale(scale)
        self.adjustSize()

    def _emit_changed(self):
        self.show_player_info = self.player_info_checkbox.isChecked()
        self.show_ten_min_exp = self.ten_min_exp_checkbox.isChecked()
        self.show_level_estimate = self.level_estimate_checkbox.isChecked()
        self.show_active_time = self.active_time_checkbox.isChecked()
        self.show_percent = self.percent_checkbox.isChecked()
        self.idle_timeout_min = self.idle_timeout_spin.value()
        self.ui_scale = self.ui_scale_spin.value()
        self.settings_changed.emit()
        self.save_settings()

    def _close_settings(self):
        self.hide()

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
            if 0.1 <= idle_timeout_min <= 120.0:
                self.idle_timeout_min = idle_timeout_min
        except (TypeError, ValueError):
            pass

        try:
            ui_scale = int(data.get("ui_scale", self.ui_scale))
            if 50 <= ui_scale <= 300:
                self.ui_scale = ui_scale
        except (TypeError, ValueError):
            pass

    def save_settings(self):
        data = {
            "show_player_info": self.show_player_info,
            "show_ten_min_exp": self.show_ten_min_exp,
            "show_level_estimate": self.show_level_estimate,
            "show_active_time": self.show_active_time,
            "show_percent": self.show_percent,
            "idle_timeout_min": self.idle_timeout_min,
            "ui_scale": self.ui_scale,
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
        self.current_scale = 1.0

        # Settings window setup
        self.settings_window = SettingsWindow()
        self.settings_window.settings_changed.connect(self.apply_settings)

        # Root layout housing 9-slice widget frame
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        self.bg_frame = NineSliceWidget()
        root_layout.addWidget(self.bg_frame)

        self.frame_layout = QVBoxLayout(self.bg_frame)
        self.frame_layout.setContentsMargins(10, 0, 10, 12)
        self.frame_layout.setSpacing(0)

        # Custom Overlay Title Bar
        self.title_bar = QWidget()
        self.title_bar.setFixedHeight(24)
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(2)

        self.title_label = QLabel("經驗值計算器")
        self.title_label.setStyleSheet("color: black; font-weight: bold; font-size: 12px; background: transparent;")

        self.settings_button = ImageButton("resources/open/", self)
        self.settings_button.clicked.connect(self.show_settings)

        self.close_button = ImageButton("resources/close", self)
        self.close_button.clicked.connect(self.close)

        title_layout.addWidget(self.title_label)
        title_layout.addStretch()
        title_layout.addWidget(self.settings_button)
        title_layout.addWidget(self.close_button)

        self.frame_layout.addWidget(self.title_bar)

        # Inner Content container layout
        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(0, 5, 0, 0)
        self.main_layout.setSpacing(5)

        # Modular information display components
        self.player_info_line = PlayerInfoLine(self)
        self.exp_rate_line = ExpRateLine(self)
        self.level_estimate_line = LevelEstimateLine(self)
        self.active_time_line = InfoLine(self)

        self.main_layout.addWidget(self.player_info_line)
        self.main_layout.addWidget(self.exp_rate_line)
        self.main_layout.addWidget(self.level_estimate_line)
        self.main_layout.addWidget(self.active_time_line)

        # Bottom button row
        self.button_layout = QHBoxLayout()
        self.button_layout.setContentsMargins(6, 4, 6, 0)
        self.button_layout.setSpacing(4)

        self.reset_button = ImageButton("resources/reset/", self)

        self.reset_button.clicked.connect(self.reset_tracker)

        self.button_layout.addWidget(self.reset_button)

        self.main_layout.addLayout(self.button_layout)
        self.frame_layout.addLayout(self.main_layout)

        # Initialize base styling scales
        self.current_scale = self.settings_window.ui_scale / 100.0
        self.apply_scale(self.current_scale)
        self.settings_window.apply_scale(self.current_scale)

        # Worker initialization
        self.worker = CaptureWorker(WINDOW_TITLE)
        self.worker.signals.data_updated.connect(self.update_stats)
        self.worker.signals.status_changed.connect(self.update_status)
        self.worker.start()

        # Absolute EXP lookups
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
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= 32:
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
    def apply_settings(self):
        # Apply settings and recalculate scaling factors dynamically ------------------------------
        new_scale = self.settings_window.ui_scale / 100.0

        if new_scale != self.current_scale:
            self.current_scale = new_scale
            self.apply_scale(self.current_scale)
            self.settings_window.apply_scale(self.current_scale)

        if self.last_data is not None:
            self.update_stats(*self.last_data)
        else:
            self.recompute_visibility()
            self.adjustSize()

        # Ensure exact geometry calculation is completed before anchoring to the right side
        QApplication.processEvents()

        if self.settings_window.isVisible():
            self.sync_settings_position()

    def apply_scale(self, scale: float):
        # Recursively apply dynamic visual scaling properties to main components ------------------
        fixed_width = round(MAIN_WINDOW_BASE_WIDTH * scale)
        self.setFixedWidth(fixed_width)

        self.bg_frame.set_scale(scale)
        self.bg_frame.layout().setContentsMargins(int(10 * scale), 0, int(10 * scale), int(12 * scale))

        self.title_bar.setFixedHeight(int(24 * scale))
        self.title_label.setStyleSheet(
            f"color: black; font-weight: bold; font-size: {int(12 * scale)}px; background: transparent;")
        self.settings_button.set_scale(scale)
        self.close_button.set_scale(scale)

        self.main_layout.setContentsMargins(0, int(8 * scale), 0, 0)
        self.main_layout.setSpacing(int(5 * scale))

        line_style = f"QLabel {{ color: black; font-weight: bold; font-size: {int(12 * scale)}px; }}"
        self.player_info_line.setStyleSheet(line_style)
        self.exp_rate_line.setStyleSheet(line_style)
        self.level_estimate_line.setStyleSheet(line_style)
        self.active_time_line.setStyleSheet(line_style)

        self.button_layout.setContentsMargins(int(6 * scale), int(4 * scale), int(6 * scale), 0)
        self.button_layout.setSpacing(int(4 * scale))

        self.reset_button.set_scale(scale)

        self.adjustSize()

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

        mins = int(AVERAGING_WINDOW_SEC / 60)

        self.player_info_line.set_text("統計已重置")
        self.exp_rate_line.set_text(f"{mins}分鐘經驗：計算中...")
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

            while (
                    self.exp_history
                    and (current_time - self.exp_history[0][0]) > AVERAGING_WINDOW_SEC
            ):
                self.exp_history.popleft()

        # 5. Calculate average rate & formatting.
        is_idle = (current_time - self.last_gain_time) >= self.idle_timeout_sec
        window_minutes = int(AVERAGING_WINDOW_SEC / 60)
        req = EXP_REQ[level]

        rate_text = None
        rate_percent = None
        eta_text = "距離升等還要：計算中..."
        active_time_text = "持續練等：閒置中"

        if is_idle:
            rate_text = "閒置中"
            eta_text = ""
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
                    raw_rate = max(
                        0.0,
                        (gained_in_window / time_window) * AVERAGING_WINDOW_SEC,
                    )
                    rate_text = format_exp(raw_rate, EXP_ROUNDING_DIGITS)
                    rate_percent = (
                        raw_rate * 100.0 / req
                        if req > 0
                        else 0.0
                    )

                    remaining_exp = max(0.0, float(req) - experience)

                    if raw_rate > 0:
                        seconds_left = (
                                               remaining_exp * AVERAGING_WINDOW_SEC
                                       ) / raw_rate
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


if __name__ == "__main__":
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    window = OverlayWindow()
    window.show()
    sys.exit(app.exec())
