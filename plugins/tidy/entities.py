from __future__ import annotations

# Tidy intentionally has no carryable-object entity. Tidyable objects are
# core Props carrying the plugin-owned tidy_category metadata.
try:
    from editor.things import Thing
except Exception:  # pragma: no cover - standalone player
    from plugins.entitybase import Thing

BOOK_MODEL = "plugins/tidy/assets/book.obj"
DEFAULT_TIDY_MODEL = BOOK_MODEL
N_COVERS = 12
COVERS = [f"plugins/tidy/assets/covers/cover_{i:02d}.png" for i in range(1, N_COVERS + 1)]

class TidyReceptacle(Thing):
    """A drop-zone that accepts tidy Props and arranges them into slots."""
    pixmap_path = "plugins/tidy/assets/tidyreceptacle.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties["type"] = "tidyreceptacle"
        self.properties.setdefault("accepts", "any")
        self.properties.setdefault("capacity", 24)
        self.properties.setdefault("slot_cols", 6)
        self.properties.setdefault("slot_spacing", [28.0, 40.0, 0.0])
        self.properties.setdefault("slot_offset", [0.0, 0.0, 0.0])
        self.properties.setdefault("reach", 140.0)
        self.properties.setdefault("disabled", False)

    def accepts_category(self, category: str) -> bool:
        acc = str(self.properties.get("accepts", "any")).strip().lower()
        return acc in ("", "any", "*") or acc == str(category).strip().lower()

    def slot_world_pos(self, index: int):
        cols = max(1, int(self.properties.get("slot_cols", 6)))
        sp = self.properties.get("slot_spacing", [28.0, 40.0, 0.0])
        off = self.properties.get("slot_offset", [0.0, 0.0, 0.0])
        try:
            sx = float(sp[0]); sy = float(sp[1]); sz = float(sp[2] if len(sp) > 2 else 0.0)
        except (TypeError, ValueError, IndexError):
            sx, sy, sz = 28.0, 40.0, 0.0
        col = index % cols
        row = index // cols
        cx = (col - (cols - 1) / 2.0) * sx
        base = self.pos
        return [base[0] + float(off[0]) + cx,
                base[1] + float(off[1]) + row * sy,
                base[2] + float(off[2]) + row * sz]

class TidyGoal(Thing):
    """Tracks tidy progress and fires OnComplete at the configured target."""
    pixmap_path = "plugins/tidy/assets/tidygoal.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties["type"] = "tidygoal"
        self.properties.setdefault("target", "all")
        self.properties.setdefault("category", "any")
        self.properties.setdefault("show_hud", True)
        self.properties.setdefault("disabled", False)

    def target_count(self, total_matching: int) -> int:
        target = self.properties.get("target", "all")
        if isinstance(target, str):
            if target.strip().lower() in ("all", "", "*"):
                return total_matching
            try:
                target = int(float(target))
            except ValueError:
                return total_matching
        try:
            target = int(target)
        except (TypeError, ValueError):
            return total_matching
        return max(0, min(target, total_matching)) if total_matching else max(0, target)

    def matches(self, category: str) -> bool:
        wanted = str(self.properties.get("category", "any")).strip().lower()
        return wanted in ("", "any", "*") or wanted == str(category).strip().lower()
