import io
import os
import re
from urllib.request import urlopen

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA70-\U0001FAFF"
    "\u2600-\u26FF"
    "\U00002700-\U000027BF"
    "]+",
    flags=re.UNICODE,
)


def _sanitize_pdf_text(text: str) -> str:
    value = str(text or "")
    replacements = {
        "✅": "[提示]",
        "⚠️": "[注意]",
        "⚠": "[注意]",
        "❗": "[注意]",
        "📍": "[地点]",
        "✈️": "[出行]",
        "✈": "[出行]",
        "🌧️": "[天气]",
        "🌧": "[天气]",
    }
    for src, dst in replacements.items():
        value = value.replace(src, dst)

    # 去除 Unicode 变体选择符，避免字体缺字时显示黑方块
    value = value.replace("\ufe0f", "").replace("\ufe0e", "")
    # 清理 emoji 等高概率缺字字符
    value = _EMOJI_PATTERN.sub("", value)
    # 过滤不可见控制字符（保留换行和制表符）
    value = "".join(ch for ch in value if ch in {"\n", "\t"} or ord(ch) >= 32)
    return value


def structured_plan_to_markdown(destination: str, days: int, total_budget: int, data: dict) -> str:
    lines = [
        f"# {destination} 旅行计划",
        "",
        f"- 天数: {days}",
        f"- 预算: ¥{total_budget}",
        "",
        "## 总览",
        data.get("overview", "(无)"),
        "",
        "## 预算策略",
        data.get("budget_summary", "(无)"),
        "",
    ]

    daily_plan = data.get("daily_plan", [])
    if isinstance(daily_plan, list) and daily_plan:
        lines.append("## 每日安排")
        lines.append("")
        for d in daily_plan:
            lines.append(f"### Day {d.get('day', '?')} - {d.get('theme', '未命名主题')}")
            highlights = d.get("highlights", [])
            if isinstance(highlights, list) and highlights:
                lines.append("- 亮点:")
                for h in highlights:
                    lines.append(f"  - {h}")
            lines.append(f"- 餐饮: {d.get('food', '(无)')}")
            lines.append(f"- 交通: {d.get('transport', '(无)')}")
            lines.append(f"- 预计花费: ¥{d.get('estimated_cost', 0)}")
            lines.append("")

    tips = data.get("tips", [])
    if isinstance(tips, list) and tips:
        lines.append("## 出行提示")
        for t in tips:
            lines.append(f"- {t}")
    return "\n".join(lines)


def markdown_to_pdf_bytes(markdown_text: str, daily_route_maps: list[dict] | None = None) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    font_name = "Helvetica"
    using_cid_font = False
    for font_path, fname in [
        ("C:/Windows/Fonts/msyh.ttc", "MSYH"),
        ("C:/Windows/Fonts/simsun.ttc", "SIMSUN"),
    ]:
        if os.path.exists(font_path):
            try:
                pdfmetrics.registerFont(TTFont(fname, font_path))
                font_name = fname
                break
            except Exception:
                continue

    # Streamlit Cloud(Linux) 上通常没有 Windows 中文字体，这里回退到 ReportLab 的 CJK 字体
    if font_name == "Helvetica":
        try:
            font_name = "STSong-Light"
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))
            using_cid_font = True
        except Exception:
            # 最后兜底仍使用 Helvetica（中文可能无法完整显示）
            font_name = "Helvetica"

    left = 40
    right = 40
    top = height - 46
    bottom = 40
    content_width = width - left - right
    y = top

    def draw_footer() -> None:
        c.setStrokeColor(colors.HexColor("#D1D5DB"))
        c.line(left, 26, width - right, 26)
        c.setFillColor(colors.HexColor("#6B7280"))
        c.setFont(font_name, 9)
        c.drawRightString(width - right, 14, f"第 {c.getPageNumber()} 页")
        c.setFillColor(colors.black)

    def start_new_page() -> None:
        nonlocal y
        draw_footer()
        c.showPage()
        y = top

    def ensure_space(need_height: float) -> None:
        nonlocal y
        if y - need_height < bottom:
            start_new_page()

    def wrap_text(text: str, font_size: float, max_width: float) -> list[str]:
        clean = _sanitize_pdf_text(text).replace("\t", "    ").strip()
        if not clean:
            return [""]

        # CJK 字体使用按字符换行即可；非 CJK 保持同样逻辑
        if using_cid_font:
            lines = []
            current = ""
            for ch in clean:
                candidate = f"{current}{ch}"
                if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
                    current = candidate
                else:
                    if current:
                        lines.append(current)
                    current = ch
            if current:
                lines.append(current)
            return lines or [""]

        lines = []
        current = ""
        for ch in clean:
            candidate = f"{current}{ch}"
            if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = ch
        if current:
            lines.append(current)
        return lines or [""]

    def draw_wrapped_text(
        text: str,
        *,
        font_size: float,
        color: colors.Color = colors.black,
        indent: float = 0,
        leading: float = 16,
        spacing_after: float = 4,
    ) -> None:
        nonlocal y
        wrapped = wrap_text(text, font_size, content_width - indent)
        c.setFont(font_name, font_size)
        c.setFillColor(color)
        for w in wrapped:
            ensure_space(leading)
            c.drawString(left + indent, y, w)
            y -= leading
        y -= spacing_after
        c.setFillColor(colors.black)

    def draw_bullet(text: str, *, level: int = 1) -> None:
        nonlocal y
        font_size = 11
        leading = 16
        base_indent = 8 + (level - 1) * 18
        bullet_indent = base_indent
        text_indent = base_indent + 14
        wrapped = wrap_text(text, font_size, content_width - text_indent)

        c.setFont(font_name, font_size)
        c.setFillColor(colors.HexColor("#111827"))
        if wrapped:
            ensure_space(leading)
            c.drawString(left + bullet_indent, y, "-")
            c.drawString(left + text_indent, y, wrapped[0])
            y -= leading
            for w in wrapped[1:]:
                ensure_space(leading)
                c.drawString(left + text_indent, y, w)
                y -= leading
        y -= 2

    # 标题卡片
    lines = markdown_text.splitlines()
    if lines:
        first_title = ""
        for l in lines:
            if l.strip().startswith("# "):
                first_title = l.strip()[2:].strip()
                break
        if first_title:
            ensure_space(70)
            c.setFillColor(colors.HexColor("#E8F0FF"))
            c.setStrokeColor(colors.HexColor("#BFD4FF"))
            c.roundRect(left, y - 46, content_width, 52, 8, stroke=1, fill=1)
            c.setFillColor(colors.HexColor("#0F2E6E"))
            c.setFont(font_name, 22)
            c.drawString(left + 12, y - 15, first_title)
            c.setFillColor(colors.HexColor("#475569"))
            c.setFont(font_name, 10)
            c.drawString(left + 12, y - 32, "智能旅行规划报告")
            y -= 66
            c.setFillColor(colors.black)

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            y -= 6
            continue

        stripped = line.strip()
        if stripped.startswith("# "):
            # 已在标题卡片展示主标题，这里跳过
            continue
        if stripped.startswith("## "):
            y -= 2
            draw_wrapped_text(
                stripped[3:].strip(),
                font_size=15,
                color=colors.HexColor("#1E3A8A"),
                leading=19,
                spacing_after=6,
            )
            continue
        if stripped.startswith("### "):
            draw_wrapped_text(
                stripped[4:].strip(),
                font_size=12,
                color=colors.HexColor("#0F766E"),
                leading=16,
                spacing_after=3,
            )
            continue
        if stripped.startswith("- "):
            draw_bullet(stripped[2:].strip(), level=1)
            continue
        if line.startswith("  - "):
            draw_bullet(line[4:].strip(), level=2)
            continue

        draw_wrapped_text(
            stripped,
            font_size=11,
            color=colors.HexColor("#111827"),
            leading=16,
            spacing_after=2,
        )

    if daily_route_maps:
        for item in daily_route_maps:
            day_no = item.get("day", "?")
            image_url = str(item.get("image_url", "")).strip()
            if not image_url:
                continue

            theme = _sanitize_pdf_text(item.get("theme", "")).strip()
            raw_spots = item.get("spots", [])
            spots = []
            if isinstance(raw_spots, list):
                for s in raw_spots:
                    name = _sanitize_pdf_text(str(s)).strip()
                    if name:
                        spots.append(name)
            if len(spots) > 8:
                spots = spots[:8]

            start_new_page()
            c.setFillColor(colors.HexColor("#ECFEFF"))
            c.setStrokeColor(colors.HexColor("#A5F3FC"))
            c.roundRect(left, height - 96, content_width, 56, 8, stroke=1, fill=1)
            c.setFillColor(colors.HexColor("#155E75"))
            c.setFont(font_name, 16)
            c.drawString(left + 12, height - 62, f"Day {day_no} 路线图")
            c.setFillColor(colors.HexColor("#334155"))
            c.setFont(font_name, 10)
            c.drawString(left + 12, height - 80, "路线图由高德静态地图生成")

            # 图前说明：主题 + 当日景点
            text_y = height - 116
            c.setFillColor(colors.HexColor("#0F172A"))
            if theme:
                c.setFont(font_name, 11)
                for line in wrap_text(f"主题：{theme}", 11, content_width):
                    c.drawString(left, text_y, line)
                    text_y -= 15
                text_y -= 2

            if spots:
                c.setFont(font_name, 11)
                c.drawString(left, text_y, "当日景点：")
                text_y -= 16
                c.setFont(font_name, 10.5)
                for idx, spot in enumerate(spots, start=1):
                    wrapped_spot = wrap_text(f"{idx}. {spot}", 10.5, content_width - 10)
                    for line in wrapped_spot:
                        c.drawString(left + 10, text_y, line)
                        text_y -= 14
                text_y -= 4

            try:
                with urlopen(image_url, timeout=18) as resp:
                    image_bytes = resp.read()

                image_reader = ImageReader(io.BytesIO(image_bytes))
                img_w, img_h = image_reader.getSize()
                if img_w <= 0 or img_h <= 0:
                    raise ValueError("图片尺寸异常")

                max_w = content_width
                max_h = max(180, text_y - (bottom + 10))
                ratio = min(max_w / float(img_w), max_h / float(img_h))
                draw_w = float(img_w) * ratio
                draw_h = float(img_h) * ratio
                x = left + (content_width - draw_w) / 2
                y_img = max(bottom + 8, text_y - draw_h)
                c.drawImage(image_reader, x, y_img, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto")
            except Exception as e:
                c.setFont(font_name, 11)
                c.setFillColor(colors.HexColor("#B91C1C"))
                c.drawString(left, height - 110, f"该日路线图加载失败: {e}")
                c.setFillColor(colors.black)

    draw_footer()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()
