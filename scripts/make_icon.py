"""Turn a generated PNG into the app's .ico set (multi-size) and the UI favicon."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def square_crop(image: Image.Image) -> Image.Image:
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return image.crop((left, top, left + side, top + side)).resize((512, 512), Image.LANCZOS)


def main(source: str | None) -> int:
    if not source or not Path(source).is_file():
        print("用法：python scripts/make_icon.py path/to/icon.png")
        return 1
    root = ROOT / "assets"
    root.mkdir(exist_ok=True)
    base = square_crop(Image.open(source).convert("RGBA"))
    base.save(root / "app.png")
    base.save(root / "app-512.png")
    base.resize((256, 256), Image.LANCZOS).save(root / "app-256.png")
    base.save(root / "app.ico", sizes=SIZES)
    (ROOT / "frontend" / "public").mkdir(exist_ok=True)
    base.save(ROOT / "frontend" / "public" / "favicon.ico", sizes=SIZES)
    # 16px check: the mark must still read as a shape, not a smudge
    preview = base.resize((16, 16), Image.LANCZOS).convert("RGB")
    extrema = preview.getextrema()
    spread = max(p[1] - p[0] for p in extrema)
    print(f"已生成 {root / 'app.ico'}；16px 明度跨度 {spread}")
    if spread < 40:
        print("警告：缩到 16px 后对比不足，图标可能在小尺寸下发糊")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
