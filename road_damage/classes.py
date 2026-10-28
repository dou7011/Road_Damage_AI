"""Single source of truth for the 12 RDD2022 classes of the YOLO11m model.

Keys are the raw class codes stored in the model (``model.names`` values). Class *indexes* are never
defined here: they always come from the loaded model, so the display layer cannot misalign them.
Labels are display-only; counting, filtering and colouring always use the raw class code.
The web front end receives this table through ``catalog()`` (see ``routes.py``), so there is no
second copy in JavaScript.
"""

# Labels follow the RDD2018/RDD2022 annotation definitions. D0w0 is not documented there, so it uses a
# neutral label. Order is only the display order and is independent of the model's class indexes.
CLASS_INFO = {
    "D00": {"label": "縱向裂縫", "color": "#38bdf8"},
    "D10": {"label": "橫向裂縫", "color": "#a78bfa"},
    "D20": {"label": "龜裂", "color": "#fbbf24"},
    "D40": {"label": "坑洞", "color": "#fb7185"},
    "D44": {"label": "白線模糊", "color": "#94a3b8"},
    "D50": {"label": "人孔蓋", "color": "#f97316"},
    "Repair": {"label": "修補痕跡", "color": "#34d399"},
    "D43": {"label": "斑馬線模糊", "color": "#e879f9"},
    "D01": {"label": "縱向裂縫（施工接縫）", "color": "#22d3ee"},
    "D11": {"label": "橫向裂縫（施工接縫）", "color": "#818cf8"},
    "Block crack": {"label": "區塊狀裂縫", "color": "#facc15"},
    "D0w0": {"label": "其他道路破損", "color": "#a3e635"},
}
CLASS_COLORS = {code: info["color"] for code, info in CLASS_INFO.items()}
CLASS_LABELS = {code: info["label"] for code, info in CLASS_INFO.items()}


def display_name(code):
    """Readable name that keeps the raw code for debugging, e.g. ``坑洞（D40）``."""
    label = CLASS_LABELS.get(code)
    return f"{label}（{code}）" if label else code


def catalog():
    return {code: {**info, "display_name": display_name(code)} for code, info in CLASS_INFO.items()}
