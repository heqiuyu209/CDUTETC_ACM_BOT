"""冷启动：一键生成基础 ACM 判题梗图，保证收图/发图开启初期图库不为空。

设计：
- 只用 Pillow 画文字梗图，不依赖任何外部素材，也不联网。
- 每个分类至少生成一张，文件名带标签前缀（``ac_accepted.png``），
  这样抽图时「按聊天文本匹配文件名标签前缀」才有东西可匹配。
- 中文字体优先，找不到就自动回退英文文案，绝不让脚本因为字体崩掉。

用法::

    python scripts/generate_acm_memes.py            # 生成到 assets/memes
    python scripts/generate_acm_memes.py --dir D:\\tmp\\memes
    python scripts/generate_acm_memes.py --force    # 覆盖同名文件
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 素材根目录：脚本在 scripts/ 下，仓库根即 parent.parent
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MEMES_DIR = REPO_ROOT / "assets" / "memes"

# 常见中文字体候选（Windows / Linux / macOS），取第一个存在的
CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

CANVAS_SIZE = (600, 400)
BACKGROUND = (18, 20, 26)
ACCENT = (96, 200, 140)
WARN = (232, 168, 76)
ERROR = (226, 96, 96)
TEXT = (238, 240, 245)


@dataclass(frozen=True)
class MemeSpec:
    """一张梗图的生成参数。"""

    category: str
    filename: str
    heading: str
    heading_cn: str
    body: str
    accent: tuple[int, int, int]


# 分类与阶段二/三完全一致：ac / wa_tle / question / slacking / interrupt
MEME_SPECS: tuple[MemeSpec, ...] = (
    MemeSpec("ac", "ac_accepted.png", "Accepted (0ms)", "一发入魂，绝杀",
             "AC 了就该摸鱼", ACCENT),
    MemeSpec("wa_tle", "wa_test2.png", "Wrong Answer on test 2", "别急着交，本地再跑跑",
             "样例过了不算过", ERROR),
    MemeSpec("wa_tle", "tle_limit.png", "Time Limit Exceeded", "你这个复杂度有点东西",
             "O(n^2) 也敢交？", WARN),
    MemeSpec("question", "question_what.png", "???", "？？？",
             "你这句话我看不懂", WARN),
    MemeSpec("slacking", "slacking_fish.png", "摸鱼时间", "今日宜：摸鱼",
             "补题明天再说", ACCENT),
    MemeSpec("interrupt", "interrupt_norepeat.png", "STOP", "禁止复读！",
             "复读机已被查水表", ERROR),
)


def find_cjk_font() -> str | None:
    """返回第一个可用的中文字体路径；都没有就返回 None（回退英文）。"""
    for candidate in CJK_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def load_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont:
    """按字号取字体；加载失败退回 Pillow 默认位图字体。"""
    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def render_meme(spec: MemeSpec, *, font_path: str | None) -> Image.Image:
    """画一张 600x400 的文字梗图：大字号判题结论 + 一句吐槽。"""
    image = Image.new("RGB", CANVAS_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)

    # 外框与左侧色条：比纯黑底更像一张「卡片」
    draw.rectangle((0, 0, CANVAS_SIZE[0] - 1, CANVAS_SIZE[1] - 1),
                   outline=spec.accent, width=4)
    draw.rectangle((0, 0, 16, CANVAS_SIZE[1] - 1), fill=spec.accent)

    heading_font = load_font(font_path, 46)
    label_font = load_font(font_path, 30)
    body_font = load_font(font_path, 26)
    tiny_font = load_font(font_path, 20)

    draw.text((48, 70), spec.heading, font=heading_font, fill=spec.accent)
    # 中文字体缺失时用英文代替，保证不出现方块乱码
    label = spec.heading_cn if font_path else spec.filename.rsplit(".", 1)[0]
    draw.text((48, 160), label, font=label_font, fill=TEXT)
    if font_path:
        draw.text((48, 230), spec.body, font=body_font, fill=(168, 174, 188))
    draw.text((48, 320), "cdutetc-acm-bot", font=tiny_font, fill=(110, 116, 130))
    return image


def generate(directory: Path, *, force: bool = False) -> list[Path]:
    """生成全部梗图，返回实际写入的文件路径列表。"""
    font_path = find_cjk_font()
    if not font_path:
        print("[warn] 未找到中文字体，正文改用英文，避免出现方块乱码")
    written: list[Path] = []
    for spec in MEME_SPECS:
        target = directory / spec.category / spec.filename
        if target.exists() and not force:
            print(f"[skip] 已存在：{target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        render_meme(spec, font_path=font_path).save(target, format="PNG")
        written.append(target)
        print(f"[ok]   {target}")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成基础 ACM 判题梗图（冷启动用）")
    parser.add_argument("--dir", default=str(DEFAULT_MEMES_DIR),
                        help="素材根目录，默认 assets/memes")
    parser.add_argument("--force", action="store_true", help="覆盖同名文件")
    args = parser.parse_args(argv)

    directory = Path(args.dir).expanduser().resolve()
    print(f"素材目录：{directory}")
    written = generate(directory, force=args.force)
    print(f"完成：新生成 {len(written)} 张，共 {len(MEME_SPECS)} 个模板")
    return 0


if __name__ == "__main__":
    sys.exit(main())
