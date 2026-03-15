"""Tests for honto-capture image processing functions.

Tests cover the pure/PIL-based functions that don't require macOS APIs.
"""

import os
import tempfile

import pytest
from PIL import Image

from capture import (
    crop_content,
    find_content_bounds,
    is_center_spread,
    is_dark,
    is_light_toolbar,
    sanitize_filename,
    split_spread,
)


# --- is_dark ---


class TestIsDark:
    def test_black_pixel(self):
        assert is_dark((0, 0, 0)) is True

    def test_dark_gray_padding(self):
        assert is_dark((50, 50, 59)) is True

    def test_white_pixel(self):
        assert is_dark((255, 255, 255)) is False

    def test_bright_color(self):
        assert is_dark((200, 100, 100)) is False

    def test_at_threshold(self):
        assert is_dark((69, 69, 69)) is True
        assert is_dark((70, 70, 70)) is False

    def test_custom_threshold(self):
        assert is_dark((60, 60, 60), threshold=50) is False
        assert is_dark((60, 60, 60), threshold=80) is True

    def test_one_channel_above(self):
        assert is_dark((10, 10, 80)) is False


# --- is_light_toolbar ---


class TestIsLightToolbar:
    def test_toolbar_color(self):
        assert is_light_toolbar((242, 244, 245)) is True

    def test_pure_white(self):
        assert is_light_toolbar((255, 255, 255)) is True

    def test_dark_pixel(self):
        assert is_light_toolbar((50, 50, 50)) is False

    def test_colored_pixel(self):
        assert is_light_toolbar((240, 200, 240)) is False

    def test_boundary(self):
        assert is_light_toolbar((201, 201, 201)) is True
        assert is_light_toolbar((199, 199, 199)) is False


# --- sanitize_filename ---


class TestSanitizeFilename:
    def test_normal_text(self):
        assert sanitize_filename("My Book Title") == "My Book Title"

    def test_special_characters(self):
        assert sanitize_filename('Book: "Title" <1>') == "Book Title 1"

    def test_whitespace_collapse(self):
        assert sanitize_filename("a   b\t\nc") == "a b c"

    def test_empty_string(self):
        assert sanitize_filename("") == "untitled"

    def test_only_special_chars(self):
        assert sanitize_filename(':<>"/\\|?*') == "untitled"

    def test_truncation(self):
        long_name = "a" * 200
        assert len(sanitize_filename(long_name)) == 100

    def test_japanese_text(self):
        name = "漫画アクション えなこ「清く、熱く。」"
        assert sanitize_filename(name) == "漫画アクション えなこ「清く、熱く。」"


# --- find_content_bounds ---


def _make_honto_screenshot(width=1080, height=900, content_color=(200, 150, 100)):
    """Create a synthetic Honto-style screenshot with toolbar, content, and nav bar.

    Layout:
    - Top 80px: light gray toolbar (242, 244, 245)
    - Next 10px: dark padding (50, 50, 59)
    - Content area with 20px dark padding on left/right
    - Bottom 40px: black nav bar (0, 0, 0)
    - Bottom 5px: colored scrubber line
    """
    img = Image.new("RGB", (width, height))
    pixels = img.load()

    toolbar_color = (242, 244, 245)
    padding_color = (50, 50, 59)
    navbar_color = (0, 0, 0)
    scrubber_color = (100, 150, 200)

    toolbar_h = 80
    padding_top = 10
    padding_lr = 20
    navbar_h = 40
    scrubber_h = 5

    for y in range(height):
        for x in range(width):
            if y < toolbar_h:
                pixels[x, y] = toolbar_color
            elif y < toolbar_h + padding_top:
                pixels[x, y] = padding_color
            elif y >= height - scrubber_h:
                pixels[x, y] = scrubber_color
            elif y >= height - navbar_h:
                pixels[x, y] = navbar_color
            elif x < padding_lr or x >= width - padding_lr:
                pixels[x, y] = padding_color
            else:
                pixels[x, y] = content_color

    return img


class TestFindContentBounds:
    def test_basic_layout(self):
        img = _make_honto_screenshot()
        left, top, right, bottom = find_content_bounds(img)

        # Content starts after toolbar (80) + padding (10) = 90
        assert top == 90
        # Content ends before navbar (40px from bottom)
        assert bottom == 900 - 40
        # Left/right padding is 20px
        assert left == 20
        assert right == 1080 - 20

    def test_no_padding(self):
        """Content fills the entire area between toolbar and navbar."""
        img = Image.new("RGB", (500, 400))
        pixels = img.load()
        for y in range(400):
            for x in range(500):
                if y < 80:
                    pixels[x, y] = (242, 244, 245)
                elif y >= 360:
                    pixels[x, y] = (0, 0, 0)
                else:
                    pixels[x, y] = (180, 180, 180)

        left, top, right, bottom = find_content_bounds(img)
        assert top == 80
        assert bottom == 360
        assert left == 0
        assert right == 500

    def test_toolbar_height_cap(self):
        """Toolbar detection stops at max_toolbar_height even if content is light."""
        img = Image.new("RGB", (500, 500), (242, 244, 245))  # All light gray
        left, top, right, bottom = find_content_bounds(img)
        # Should cap toolbar at 200 and not treat entire image as toolbar
        assert top <= 200


# --- crop_content ---


class TestCropContent:
    def test_crops_and_saves(self):
        img = _make_honto_screenshot()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
            img.save(path, "PNG")

        try:
            bounds, content_width = crop_content(path)
            cropped = Image.open(path)
            # Should be smaller than original
            assert cropped.size[0] < 1080
            assert cropped.size[1] < 900
            assert content_width == cropped.size[0]
        finally:
            os.unlink(path)

    def test_fixed_bounds(self):
        img = _make_honto_screenshot()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
            img.save(path, "PNG")

        try:
            fixed = (100, 100, 500, 400)
            bounds, content_width = crop_content(path, fixed_bounds=fixed)
            cropped = Image.open(path)
            assert bounds == fixed
            assert cropped.size == (400, 300)
        finally:
            os.unlink(path)


# --- is_center_spread ---


def _make_two_page_spread(width=2000, height=1000):
    """Create a synthetic two-page spread with two distinct photos side by side."""
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    mid = width // 2
    for y in range(height):
        for x in range(width):
            if x < mid:
                # Left page: warm tones
                pixels[x, y] = (200, 150, 100)
            else:
                # Right page: cool tones
                pixels[x, y] = (100, 150, 200)
    return img


def _make_center_spread(width=2000, height=1000):
    """Create a synthetic center spread (single continuous image)."""
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    for y in range(height):
        for x in range(width):
            # Smooth gradient across the full width
            r = int(100 + 100 * x / width)
            g = 150
            b = int(200 - 100 * x / width)
            pixels[x, y] = (r, g, b)
    return img


def _make_single_page(width=1000, height=1000):
    """Create a synthetic single page."""
    return Image.new("RGB", (width, height), (180, 180, 180))


class TestIsCenterSpread:
    def test_two_page_spread(self):
        img = _make_two_page_spread()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
            img.save(path, "PNG")
        try:
            assert is_center_spread(path) is False
        finally:
            os.unlink(path)

    def test_center_spread(self):
        img = _make_center_spread()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
            img.save(path, "PNG")
        try:
            assert is_center_spread(path) is True
        finally:
            os.unlink(path)

    def test_single_page(self):
        img = _make_single_page()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
            img.save(path, "PNG")
        try:
            assert is_center_spread(path) is True
        finally:
            os.unlink(path)

    def test_spread_with_similar_backgrounds(self):
        """Two pages with similar but not identical content should still be detected."""
        img = Image.new("RGB", (2000, 1000))
        pixels = img.load()
        mid = 1000
        for y in range(1000):
            for x in range(2000):
                if x < mid:
                    # Left: light photo with subtle variation
                    pixels[x, y] = (220, 220 - y % 20, 210)
                else:
                    # Right: slightly different photo
                    pixels[x, y] = (180, 200 + y % 15, 190)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
            img.save(path, "PNG")
        try:
            assert is_center_spread(path) is False
        finally:
            os.unlink(path)


# --- split_spread ---


class TestSplitSpread:
    def test_basic_split(self):
        img = _make_two_page_spread(width=2000, height=1000)
        with tempfile.TemporaryDirectory() as tmpdir:
            spread_path = os.path.join(tmpdir, "spread.png")
            img.save(spread_path, "PNG")

            pw = split_spread(spread_path, tmpdir, 1, 2)

            right = Image.open(os.path.join(tmpdir, "page_0001.png"))
            left = Image.open(os.path.join(tmpdir, "page_0002.png"))

            assert right.size == (1000, 1000)
            assert left.size == (1000, 1000)
            assert pw == 1000

            # Right page should have cool tones (from right half)
            assert right.getpixel((500, 500)) == (100, 150, 200)
            # Left page should have warm tones (from left half)
            assert left.getpixel((500, 500)) == (200, 150, 100)

    def test_split_with_learned_width(self):
        """Using a learned page_width should produce consistent page sizes."""
        img = Image.new("RGB", (2010, 1000), (150, 150, 150))
        with tempfile.TemporaryDirectory() as tmpdir:
            spread_path = os.path.join(tmpdir, "spread.png")
            img.save(spread_path, "PNG")

            pw = split_spread(spread_path, tmpdir, 1, 2, page_width=1000)

            right = Image.open(os.path.join(tmpdir, "page_0001.png"))
            left = Image.open(os.path.join(tmpdir, "page_0002.png"))

            # Both pages should be exactly 1000px wide
            assert right.size[0] == 1000
            assert left.size[0] == 1000

    def test_rtl_page_ordering(self):
        """Right page gets the lower page number (RTL convention)."""
        img = _make_two_page_spread(width=2000, height=500)
        with tempfile.TemporaryDirectory() as tmpdir:
            spread_path = os.path.join(tmpdir, "spread.png")
            img.save(spread_path, "PNG")

            split_spread(spread_path, tmpdir, 5, 6)

            assert os.path.exists(os.path.join(tmpdir, "page_0005.png"))
            assert os.path.exists(os.path.join(tmpdir, "page_0006.png"))
