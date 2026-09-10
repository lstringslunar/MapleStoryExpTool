import re
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
import yaml

from helper import get_resource_path, imread

MODEL_DIR: Path = get_resource_path('resources/models/PP-OCRv6_tiny_rec_onnx', as_posix=False)
MODEL_PATH = MODEL_DIR / 'inference.onnx'
CONFIG_PATH = MODEL_DIR / 'inference.yml'

# Bounding box offsets (lv template top-left corner as origin)

# LV_OFFSET = (32, 11, 76, 23)
# HP_OFFSET = (236, 2, 325, 14)
# MP_OFFSET = (347, 2, 433, 14)
# EXP_OFFSET = (464, 2, 562, 14)
#
# TEMPLATE_DIST = 573.0

LV_OFFSET = (37, 18, 85, 30)
EXP_OFFSET = (825, 4, 977, 18)

TEMPLATE_DIST = 992.0


class UiOcrExtractor:
    def __init__(self):
        # self._template_a = self._load_template('resources/templates/lv.png')
        # self._template_b = self._load_template('resources/templates/shop.png')
        self._template_a = self._load_template('resources/templates/lv_v2.png')
        self._template_b = self._load_template('resources/templates/shop_v2.png')

        self._screenshot = np.array([])
        self._size = (0, 0)
        self._scale = 0

        # Top-Left and Bottom-Right corners : [x1, y1, x2, y2]
        self._lv_box = (0, 0, 1, 1)
        self._exp_box = (0, 0, 1, 1)

        # Manual calibration
        self._manual_mode = False

        # Skip check: skip inferencing if no change
        self._lv_last_array = None
        self._exp_last_array = None
        self._lv_last = 0
        self._exp_last = 0

        self._model = PPOCRv6TinyTextRecognition(MODEL_PATH, CONFIG_PATH)

    def is_available(self) -> bool:
        return self._scale > 0

    def set_manual_boxes(self, lv_box: tuple, exp_box: tuple) -> None:
        # Switch into manual calibration mode -----------------------------------------------------
        self._manual_mode = True
        self._lv_box = tuple(int(round(v)) for v in lv_box)
        self._exp_box = tuple(int(round(v)) for v in exp_box)

        # is_available() only checks if (_scale > 0)
        self._scale = 1.0

        # Delete cached boxes
        self._lv_last_array = None
        self._exp_last_array = None

    def clear_manual_boxes(self) -> None:
        # Return to automatic template-matching detection mode ------------------------------------
        self._manual_mode = False
        self._scale = 0
        self._size = (0, 0)

    def _safe_crop(self, box):
        # Returns `None` if the box is invalid ----------------------------------------------------
        screenshot = self._screenshot
        if screenshot is None or screenshot.size == 0:
            return None

        h, w = screenshot.shape[0], screenshot.shape[1]
        if h <= 0 or w <= 0:
            return None

        x1, y1, x2, y2 = box
        x1 = max(0, min(int(x1), w))
        x2 = max(0, min(int(x2), w))
        y1 = max(0, min(int(y1), h))
        y2 = max(0, min(int(y2), h))

        if x2 <= x1 or y2 <= y1:
            return None

        return screenshot[y1:y2, x1:x2, :3]

    def get_player_level(self) -> tuple:
        # No UI match / bad geometry -> nothing to read yet.
        if not self.is_available():
            return 0, 0

        img = self._safe_crop(self._lv_box)
        if img is None:
            return 0, 0

        # Skip check
        if np.array_equal(self._lv_last_array, img):
            return self._lv_last, 1
        self._lv_last_array = img.copy()

        # Inference
        try:
            text, confidence = self._model.recognize(img)
        except Exception as err:
            print(f"[UiOcrExtractor] LV recognition failed: {err}")
            return 0, 0

        # Remove non-digit
        digits = re.sub(r'\D', '', text)

        # Target 2 to 3 digits
        if len(digits) >= 2:
            # Take up to 3 digits from the end/main sequence
            level = digits[-3:] if len(digits) >= 3 else digits

            try:
                level_int = int(level)
            except ValueError:
                return 0, 0

            self._lv_last = level_int
            return level_int, confidence

        return 0, 0

    def get_player_experience(self) -> tuple:
        # No UI match / bad geometry -> nothing to read yet.
        if not self.is_available():
            return 0, 0

        img = self._safe_crop(self._exp_box)
        if img is None:
            return 0, 0

        # Skip check
        if np.array_equal(self._exp_last_array, img):
            return self._exp_last, 1
        self._exp_last_array = img.copy()

        # Inference
        try:
            text, confidence = self._model.recognize(img)
        except Exception as err:
            print(f"[UiOcrExtractor] EXP recognition failed: {err}")
            return 0, 0

        # Extract the first number-like value containing digits, spaces, and dots
        match = re.search(r'^\D*([\d\s.]*?\d)(?=\D|$)', text)

        if match:
            # Remove non-digit
            digits = re.sub(r'\D', '', match.group(1))
            if digits:
                try:
                    exp_int = int(digits)
                except ValueError:
                    return 0, 0

                self._exp_last = exp_int
                return exp_int, confidence

        return 0, 0

    def _compute_box(self, anchor, offset) -> tuple:
        x, y = anchor
        x1, y1, x2, y2 = offset

        x1 = round(x + x1 * self._scale)
        y1 = round(y + y1 * self._scale)
        x2 = round(x + x2 * self._scale)
        y2 = round(y + y2 * self._scale)

        return x1, y1, x2, y2

    def update(self, screenshot: np.ndarray) -> None:
        if screenshot is None or screenshot.size == 0 or screenshot.ndim < 2:
            # Nothing usable in this frame
            self._screenshot = screenshot
            if not self._manual_mode:
                self._scale = 0
            return

        self._screenshot = screenshot

        if self._manual_mode:
            return

        size = (screenshot.shape[0], screenshot.shape[1])

        size_changed = self._size != size
        self._size = size

        if not size_changed and self._scale > 0:
            return

        try:
            gray = cv2.cvtColor(screenshot, cv2.COLOR_BGRA2GRAY)

            # Try to detect the UI scale by using template matching -------------------------------
            scales = np.linspace(0.5, 2.0, 60)

            best_score_a = 0
            best_score_b = 0
            best_pos_a = (0, 0)
            best_pos_b = (0, 0)

            for scale in scales:
                resized_a = cv2.resize(self._template_a, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
                resized_b = cv2.resize(self._template_b, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)

                result_a = cv2.matchTemplate(gray, resized_a, cv2.TM_CCOEFF_NORMED)
                result_b = cv2.matchTemplate(gray, resized_b, cv2.TM_CCOEFF_NORMED)

                _, score_a, _, pos_a = cv2.minMaxLoc(result_a)
                _, score_b, _, pos_b = cv2.minMaxLoc(result_b)

                if score_a > best_score_a:
                    best_score_a = score_a
                    best_pos_a = pos_a

                if score_b > best_score_b:
                    best_score_b = score_b
                    best_pos_b = pos_b

            if best_score_a < 0.7 or best_score_b < 0.7:  # No match found
                self._scale = 0
                return

            new_scale = (best_pos_b[0] - best_pos_a[0]) / TEMPLATE_DIST

            if new_scale < 0.1 or new_scale > 4:  # No match found / nonsensical scale
                self._scale = 0
                return

            self._scale = new_scale
            self._lv_box = self._compute_box(best_pos_a, LV_OFFSET)
            self._exp_box = self._compute_box(best_pos_a, EXP_OFFSET)

        except Exception as err:
            # Any failure in template matching (odd frame format, OpenCV error, etc.)
            # should leave the extractor in a well-defined "not detected" state
            # rather than propagating and crashing the capture thread.
            print(f"[UiOcrExtractor] UI scale detection failed: {err}")
            self._scale = 0

    @staticmethod
    def _load_template(path):
        resource_path = get_resource_path(path)
        template = imread(resource_path, cv2.IMREAD_GRAYSCALE)
        assert template is not None, f"Failed to load template at {resource_path}."

        return template.copy()


class PPOCRv6TinyTextRecognition:
    def __init__(self, model_path, config_path):
        # Load model ------------------------------------------------------------------------------
        self.session = ort.InferenceSession(
            model_path,
            providers=["CPUExecutionProvider"],
        )

        # PP-OCRv6 input: [N, 3, 48, 320] ---------------------------------------------------------
        input_info = self.session.get_inputs()[0]

        self.input_name = input_info.name
        self.input_shape = input_info.shape

        self.input_height = self.input_shape[2]
        self.input_width = self.input_shape[3]

        if not isinstance(self.input_height, int):
            self.input_height = 48

        if not isinstance(self.input_width, int):
            self.input_width = 320

        # Load PaddleOCR model configuration ------------------------------------------------------
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        # Load character dictionary ---------------------------------------------------------------
        postprocess = self.config.get("PostProcess", {})
        self.character_dict = postprocess.get("character_dict", [])

        if not self.character_dict:
            raise RuntimeError("No character_dict found in inference.yml")

        # PaddleOCR's CTC decoder uses index 0 as blank.
        self.characters = ["blank"] + self.character_dict

        # PP-OCRv6 uses CTCLabelDecode.
        self.use_space_char = postprocess.get("use_space_char", False)

        if self.use_space_char:
            self.characters.append(" ")

    def preprocess(self, image):
        """
        Convert OpenCV BGR image into PP-OCRv6 input.

        Input:
            BGR uint8 image, H x W x 3

        Output:
            float32 array, 1 x 3 x 48 x 320
        """

        if image is None:
            raise ValueError("Input image is None")

        # Make sure image is 3-channel BGR --------------------------------------------------------
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)

        if image.shape[2] != 3:
            raise ValueError(f"Expected 3-channel image, got {image.shape}")

        h, w = image.shape[:2]
        ratio = w / float(h)
        resized_width = min(int(np.ceil(self.input_height * ratio)), self.input_width)

        # C++ accelerated operations: Resize, normalization ([-1, 1] range), and HWC->CHW transpose
        blob = cv2.dnn.blobFromImage(
            image,
            scalefactor=1.0 / 127.5,
            size=(resized_width, self.input_height),
            mean=(127.5, 127.5, 127.5),
            swapRB=False,
            crop=False
        )

        # Pad horizontally if the image is smaller than input_width
        if resized_width < self.input_width:
            pad_w = self.input_width - resized_width
            blob = np.pad(blob, ((0, 0), (0, 0), (0, 0), (0, pad_w)), mode='constant', constant_values=0)

        return blob

    def decode(self, prediction):
        # Pred shape: [1, sequence_length, num_classes]

        # Remove batch dimension
        prediction = prediction[0]

        # Get best class at every timestep
        indices = np.argmax(prediction, axis=1)

        # Confidence of selected class
        scores = np.max(prediction, axis=1)

        text = []
        confidence = []

        last_index = 0

        for index, score in zip(indices, scores):

            index = int(index)

            # CTC blank
            if index == 0:
                last_index = index
                continue

            # CTC repeated character
            if index == last_index:
                continue

            if index >= len(self.characters):
                last_index = index
                continue

            text.append(self.characters[index])

            confidence.append(float(score))

            last_index = index

        if confidence:
            avg_confidence = float(np.mean(confidence))
        else:
            avg_confidence = 0.0

        return "".join(text), avg_confidence

    def recognize(self, image):
        input_tensor = self.preprocess(image)

        outputs = self.session.run(None, {self.input_name: input_tensor})
        pred = outputs[0]

        text, confidence = self.decode(pred)

        return text, confidence
