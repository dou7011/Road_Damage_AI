import unittest

import numpy as np

from road_damage.images import annotate, label_font
from road_damage.reports import detection_rows


def frame(index, *items):
    return {"frame_index": index, "timestamp": index / 10, "detections": [
        {"id": number, "class_code": code, "confidence": confidence}
        for number, (code, confidence) in enumerate(items, start=1)
    ]}


class DetectionRowTests(unittest.TestCase):
    def test_every_detection_in_every_frame_is_listed_in_order(self):
        rows = list(detection_rows([
            frame(0, ("D00", .5)), frame(5, ("D00", .6), ("D40", .7)), frame(30, ("D20", .8)),
        ]))
        self.assertEqual(rows, [
            [0, 0, "D00", .5, 1], [5, .5, "D00", .6, 1], [5, .5, "D40", .7, 2], [30, 3, "D20", .8, 1],
        ])

    def test_no_frames_has_no_rows(self):
        self.assertEqual(list(detection_rows([])), [])


class LabelTests(unittest.TestCase):
    def test_chinese_label_is_drawn_when_cjk_font_exists(self):
        if label_font(20) is None:
            self.skipTest("沒有可用的中文字型")
        image = np.full((240, 320, 3), 128, dtype=np.uint8)
        detection = {
            "id": 1, "class_code": "D40", "class_name": "D40", "confidence": .9,
            "bbox": {"x1": 40, "y1": 100, "x2": 200, "y2": 200},
        }
        output = annotate(image, [detection])
        label_area = output[60:100, 40:200]
        self.assertGreater(np.abs(label_area.astype(int) - 128).sum(), 10000)
        # Dark glyph pixels prove text was rendered over the colored label background.
        self.assertTrue((label_area.sum(axis=2) < 200).any())


if __name__ == "__main__":
    unittest.main()
