# -*- coding: utf-8 -*-
"""
QQMCBridge adapter 网关字体自检脚本。
部署到目标机后，先运行本脚本确认绘图/围棋/AI 图片能正常显示中文与 emoji：

    python check_fonts.py

它会：
1. 检查 Pillow 是否已安装（缺失则绘图功能不可用）
2. 检查项目自带中文字体 misans.ttf 是否存在
3. 列出当前系统可用的 emoji 字体
4. 实际渲染一张含「中文 + emoji」的测试图到 media/test_font.png
"""

import os
import sys


def main() -> int:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[FAIL] 未安装 Pillow，绘图/围棋/AI 图片功能全部不可用。")
        print("       解决：pip install Pillow")
        return 1
    print("[OK] Pillow 已安装：", Image.__module__)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    cn_font = os.path.join(base_dir, "misans.ttf")
    if os.path.isfile(cn_font):
        print("[OK] 项目自带中文字体存在：", cn_font)
    else:
        print("[FAIL] 项目自带中文字体缺失：", cn_font)
        print("       这会导致所有中文显示为方块。请将其随目录一起部署到目标机。")

    emoji_candidates = [
        r"C:\Windows\Fonts\seguiemj.ttf",
        "/System/Library/Fonts/Apple Color Emoji.ttc",
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    ]
    found_emoji = [p for p in emoji_candidates if os.path.isfile(p)]
    if found_emoji:
        print("[OK] 找到 emoji 字体：")
        for p in found_emoji:
            print("       -", p)
    else:
        print("[WARN] 未找到任何 emoji 字体，emoji 可能显示为方框（不影响文字功能）。")

    # 实际渲染一张测试图
    try:
        font = ImageFont.truetype(cn_font, 48) if os.path.isfile(cn_font) else ImageFont.load_default()
        img = Image.new("RGB", (640, 160), "white")
        d = ImageDraw.Draw(img)
        d.text((20, 50), "中文测试 🦌 Hello 123", font=font, fill="black")
        media_dir = os.path.join(base_dir, "media")
        os.makedirs(media_dir, exist_ok=True)
        out = os.path.join(media_dir, "test_font.png")
        img.save(out)
        print("[OK] 测试图已生成：", out)
        print("      请在 QQ 中查看此图，确认中文与 emoji 是否正常显示。")
    except Exception as exc:  # noqa: BLE001
        print("[FAIL] 渲染测试图失败：", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
