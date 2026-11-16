from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime
from io import BytesIO
from itertools import pairwise

from PIL import Image, ImageDraw, ImageFont

from slopbot.themes import ChartTheme, chart_theme

ChartPoint = tuple[int, float, str]


def render_mmr_history_chart(
    *,
    player_name: str,
    playlist_name: str,
    points: Sequence[ChartPoint],
    theme_preset: str | None = None,
    theme_mode: str | None = None,
) -> BytesIO:
    """Render a compact Discord-friendly MMR chart without touching the filesystem."""
    if not points:
        raise ValueError("at least one MMR point is required")

    width, height = 1000, 560
    left, top, right, bottom = 90, 92, 38, 100
    plot_right = width - right
    plot_bottom = height - bottom
    plot_width = plot_right - left
    plot_height = plot_bottom - top

    theme = chart_theme(theme_preset, theme_mode)
    image = Image.new("RGB", (width, height), theme.background)
    draw = ImageDraw.Draw(image)
    font = _font(18, theme)
    title_font = _font(25, theme)

    values = [point[1] for point in points]
    low = min(values)
    high = max(values)
    padding = max(25.0, (high - low) * 0.15)
    y_min = max(0.0, low - padding)
    y_max = high + padding
    if y_max == y_min:
        y_max += 50.0

    start_time = points[0][0]
    end_time = points[-1][0]
    if end_time == start_time:
        end_time += 1

    def x_for(timestamp: int) -> int:
        return left + round((timestamp - start_time) / (end_time - start_time) * plot_width)

    def y_for(mmr: float) -> int:
        return top + round((y_max - mmr) / (y_max - y_min) * plot_height)

    if theme.sketch:
        draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=12, fill=theme.panel)
        _rough_line(draw, [(32, 27), (width - 30, 24)], theme.text, width=2, seed=3)
        _rough_line(
            draw,
            [(width - 27, 30), (width - 24, height - 31)],
            theme.text,
            width=2,
            seed=5,
        )
        _rough_line(
            draw,
            [(width - 31, height - 25), (30, height - 23)],
            theme.text,
            width=2,
            seed=7,
        )
        _rough_line(draw, [(25, height - 31), (23, 31)], theme.text, width=2, seed=11)
    else:
        draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=18, fill=theme.panel)
    title = " ".join(f"{player_name} | {playlist_name}".split())
    while title and draw.textlength(title, font=title_font) > plot_width:
        title = title[:-4].rstrip() + "..."
    draw.text((left, 44), title, fill=theme.text, font=title_font)

    for index in range(5):
        ratio = index / 4
        y = top + round(ratio * plot_height)
        value = y_max - ratio * (y_max - y_min)
        if theme.sketch:
            _rough_line(
                draw,
                [(left, y), (plot_right, y)],
                theme.grid,
                width=1,
                seed=100 + index,
                jitter=1,
            )
        else:
            draw.line((left, y, plot_right, y), fill=theme.grid, width=1)
        label = f"{value:,.0f}"
        label_box = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (left - (label_box[2] - label_box[0]) - 12, y - 9),
            label,
            fill=theme.muted,
            font=font,
        )

    coordinates = [(x_for(timestamp), y_for(mmr)) for timestamp, mmr, _rank in points]
    if len(coordinates) == 1:
        x, y = coordinates[0]
        coordinates = [(left, y), (plot_right, y)]
    polygon = [
        coordinates[0],
        *coordinates,
        (coordinates[-1][0], plot_bottom),
        (coordinates[0][0], plot_bottom),
    ]
    draw.polygon(polygon, fill=theme.fill)
    if theme.sketch:
        _rough_line(draw, coordinates, theme.line, width=4, seed=player_name + playlist_name)
    else:
        draw.line(coordinates, fill=theme.line, width=4, joint="curve")
    for x, y in (coordinates[0], coordinates[-1]):
        draw.ellipse(
            (x - 6, y - 6, x + 6, y + 6),
            fill=theme.line,
            outline=theme.text,
            width=2,
        )

    same_day = points[0][0] // 86400 == points[-1][0] // 86400
    date_format = "%H:%M UTC" if same_day else "%b %d UTC"
    start_label = datetime.fromtimestamp(start_time, tz=UTC).strftime(date_format)
    end_label = datetime.fromtimestamp(points[-1][0], tz=UTC).strftime(date_format)
    draw.text((left, plot_bottom + 18), start_label, fill=theme.muted, font=font)
    end_box = draw.textbbox((0, 0), end_label, font=font)
    draw.text(
        (plot_right - (end_box[2] - end_box[0]), plot_bottom + 18),
        end_label,
        fill=theme.muted,
        font=font,
    )
    delta = values[-1] - values[0]
    summary = (
        f"Start {values[0]:,.0f}   Current {values[-1]:,.0f}   "
        f"Change {delta:+,.0f}   High {high:,.0f}   Low {low:,.0f}"
    )
    draw.text((left, height - 48), summary, fill=theme.text, font=font)

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def _font(size: int, theme: ChartTheme) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ("Comic Sans MS", "Comic Sans MS.ttf", "ComicSansMS.ttf")
        if theme.sketch
        else ("DejaVuSans.ttf",)
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _rough_line(
    draw: ImageDraw.ImageDraw,
    points: Sequence[tuple[int, int]],
    fill: str,
    *,
    width: int,
    seed: int | str,
    jitter: int = 2,
) -> None:
    """Draw a deterministic, subtly imperfect line in the spirit of xkcd plots."""
    if len(points) < 2:
        return
    rng = random.Random(str(seed))
    for pass_index in range(2):
        rough: list[tuple[int, int]] = []
        for start, end in pairwise(points):
            if not rough:
                rough.append((start[0] + rng.randint(-jitter, jitter), start[1]))
            distance = max(abs(end[0] - start[0]), abs(end[1] - start[1]))
            steps = max(2, distance // 24)
            for step in range(1, steps + 1):
                ratio = step / steps
                x = round(start[0] + (end[0] - start[0]) * ratio)
                y = round(start[1] + (end[1] - start[1]) * ratio)
                if step != steps:
                    x += rng.randint(-jitter, jitter)
                    y += rng.randint(-jitter, jitter)
                rough.append((x, y))
        draw.line(rough, fill=fill, width=max(1, width - pass_index), joint="curve")
