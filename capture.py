"""
Honto Book Page Capture

Opens the Honto app, captures each page as a screenshot,
and navigates to the next page using keyboard input.

Usage:
    1. Open the Honto app and navigate to the first page of your book
    2. Run: uv run python capture.py [--pages N] [--delay SECONDS] [--output DIR]
    3. The script will focus the Honto window, then capture + advance pages

Requirements:
    - Grant Accessibility permissions to your terminal in
      System Settings > Privacy & Security > Accessibility
    - Grant Screen Recording permissions in
      System Settings > Privacy & Security > Screen Recording
"""

import argparse
import os
import re
import sys
import time

import Vision
from AppKit import NSBitmapImageRep, NSPNGFileType
from Foundation import NSURL
from PIL import Image
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGRectNull,
    CGWindowListCopyWindowInfo,
    CGWindowListCreateImage,
    kCGHIDEventTap,
    kCGNullWindowID,
    kCGWindowImageBoundsIgnoreFraming,
    kCGWindowListOptionAll,
    kCGWindowListOptionIncludingWindow,
)


def extract_title_from_titlebar(image_path):
    """OCR the title bar region of a Honto window screenshot to get the book title.

    Crops the title bar strip and runs macOS Vision OCR, filtering out
    page counters and short UI text.
    """
    img = Image.open(image_path)
    w, h = img.size
    # Title bar is ~66px at 2x Retina (132px). Crop generously to include text.
    titlebar_height = 140
    titlebar = img.crop((0, 0, w, titlebar_height))
    titlebar_path = image_path + ".titlebar.png"
    titlebar.save(titlebar_path, "PNG")

    try:
        image_url = NSURL.fileURLWithPath_(titlebar_path)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLanguages_(["ja", "en"])
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)

        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
            image_url, {}
        )
        handler.performRequests_error_([request], None)

        results = request.results()
        texts = []
        for obs in results or []:
            candidate = obs.topCandidates_(1)[0]
            text = candidate.string()
            # Filter out page counters ("50/51")
            if re.match(r"^\d+/\d+$", text):
                continue
            # Clean up garbled UI icons that OCR misreads from toolbar buttons
            text = re.sub(r"^[■●＝＝】\[\]|=\-\s]+", "", text)
            text = text.strip()
            if len(text) <= 1:
                continue
            texts.append(text)

        title = " ".join(texts).strip()
    finally:
        os.remove(titlebar_path)

    return title


def sanitize_filename(name):
    """Convert a string to a safe directory/file name."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:100] if name else "untitled"


def is_dark(pixel, threshold=70):
    """Check if a pixel is dark (part of padding or nav bar background)."""
    r, g, b = pixel[0], pixel[1], pixel[2]
    return r < threshold and g < threshold and b < threshold


def is_light_toolbar(pixel):
    """Check if a pixel is part of the light gray title bar (~242,244,245)."""
    r, g, b = pixel[0], pixel[1], pixel[2]
    return r > 200 and g > 200 and b > 200 and abs(r - g) < 15 and abs(r - b) < 15


def find_content_bounds(img):
    """Find the book content area within a Honto window screenshot.

    Layout (top to bottom):
    - Light gray title bar (~76px at 2x): RGB ~(242,244,245)
    - Book content (may have dark page edges)
    - Black nav bar (~36px at 2x): RGB (0,0,0) with colored scrubber

    Layout (left/right):
    - Dark gray padding: RGB ~(50,50,59)
    - Book page(s)
    - Possible gray gap between pages
    - Dark gray padding
    """
    w, h = img.size
    pixels = img.load()

    # --- TOP: Skip light title bar, then any dark padding ---
    # Title bar is ~76px at 2x Retina (~152px). Cap search to avoid mistaking
    # light-colored photo content for toolbar.
    max_toolbar_height = 200
    top = 0
    in_toolbar = True
    for y in range(h):
        if in_toolbar:
            # Check multiple x positions to avoid false positives from
            # light-colored photo content at a single sample point
            if y >= max_toolbar_height:
                in_toolbar = False
                # Fall through to dark padding check
            else:
                toolbar_count = 0
                num_samples = 5
                for si in range(num_samples):
                    sx = int(w * (si + 1) / (num_samples + 1))
                    sr, sg, sb = pixels[sx, y][:3]
                    if is_light_toolbar((sr, sg, sb)):
                        toolbar_count += 1
                if toolbar_count < num_samples * 0.6:
                    in_toolbar = False
                else:
                    continue
            # After toolbar ends, check if we hit dark padding or content
            r, g, b = pixels[w // 2, y][:3]
            if is_dark((r, g, b)):
                pass  # fall through to dark padding skip below
            else:
                top = y
                break
        if not in_toolbar:
            # Check multiple x positions for dark padding
            dark_count = 0
            num_samples = 5
            for si in range(num_samples):
                sx = int(w * (si + 1) / (num_samples + 1))
                sr, sg, sb = pixels[sx, y][:3]
                if is_dark((sr, sg, sb)):
                    dark_count += 1
            if dark_count < num_samples * 0.6:
                top = y
                break

    # --- BOTTOM: Scan up from bottom, skip nav bar ---
    # Nav bar layout (bottom to top): colored scrubber line, dark bar, content.
    # Strategy: find the transition from dark nav bar to content.
    # The nav bar is at most ~100px at 2x Retina. Cap the search to avoid
    # mistaking dark photo content for nav bar.
    max_navbar_height = 120
    bottom = h
    y = h - 1
    check_points = 30
    min_y = max(top, h - max_navbar_height)
    # Phase 1: skip scrubber/colored rows at very bottom (thin, <10px)
    while y > min_y:
        dark_count = 0
        for ci in range(check_points):
            cx = int(w * (ci + 1) / (check_points + 1))
            r, g, b = pixels[cx, y][:3]
            if r < 50 and g < 50 and b < 50:
                dark_count += 1
        if dark_count > check_points * 0.8:
            break  # hit the dark bar
        y -= 1
    # Phase 2: skip the dark nav bar
    while y > min_y:
        dark_count = 0
        for ci in range(check_points):
            cx = int(w * (ci + 1) / (check_points + 1))
            r, g, b = pixels[cx, y][:3]
            if r < 50 and g < 50 and b < 50:
                dark_count += 1
        if dark_count < check_points * 0.3:
            bottom = y + 1
            break
        y -= 1

    # --- LEFT: Dark gray padding ~(50,50,59) ---
    content_mid_y = (top + bottom) // 2
    left = 0
    for x in range(w):
        r, g, b = pixels[x, content_mid_y][:3]
        if not is_dark((r, g, b)):
            left = x
            break

    # --- RIGHT: Dark gray padding ---
    right = w
    for x in range(w - 1, 0, -1):
        r, g, b = pixels[x, content_mid_y][:3]
        if not is_dark((r, g, b)):
            right = x + 1
            break

    return left, top, right, bottom


def crop_content(image_path):
    """Crop out all UI chrome from a Honto screenshot, keeping only book content.

    Returns (left, top, right, bottom) crop bounds and the cropped width.
    """
    img = Image.open(image_path)
    bounds = find_content_bounds(img)
    cropped = img.crop(bounds)
    cropped.save(image_path, "PNG")
    return bounds, cropped.size[0]


def is_single_page(content_width, spread_width):
    """Determine if the cropped content is a single page vs a two-page spread.

    A single page (cover, colophon) will be roughly half the width of a spread.
    """
    if spread_width is None:
        return False
    return content_width < spread_width * 0.7


def is_center_spread(image_path):
    """Detect if a cropped spread is a center spread (single continuous image).

    In a two-page spread, the Honto viewer has a visible dark gap/seam between
    the two pages at the center. A center spread has continuous image content
    across the center with no such gap.

    Checks for a consistent dark vertical strip at the center of the image.
    """
    img = Image.open(image_path)
    w, h = img.size
    mid_x = w // 2
    pixels = img.load()

    # Sample the middle 60% of height to avoid top/bottom edge artifacts
    y_start = int(h * 0.2)
    y_end = int(h * 0.8)

    # Check if there's a consistent dark vertical strip at/near center
    # Try each column in a narrow band around center
    for offset in range(-3, 4):
        x = mid_x + offset
        if x < 0 or x >= w:
            continue
        dark_count = 0
        sample_count = 0
        for y in range(y_start, y_end, 2):
            r, g, b = pixels[x, y][:3]
            if is_dark((r, g, b), threshold=80):
                dark_count += 1
            sample_count += 1
        # If any column in the center band is mostly dark, there's a gap
        if sample_count > 0 and dark_count / sample_count > 0.7:
            return False  # Has a gap → normal two-page spread

    return True  # No gap found → center spread


def split_spread(image_path, output_dir, page_num_right, page_num_left):
    """Split a two-page spread into individual pages.

    Splits at the center gap between the two pages. Finds the actual gap
    position by scanning for the darkest vertical strip near the center.
    For RTL books, the right page has the lower page number.
    """
    img = Image.open(image_path)
    w, h = img.size
    pixels = img.load()

    # Find the actual gap center by looking for the darkest vertical strip
    # near the middle of the image
    mid = w // 2
    search_range = w // 20  # Search ±5% of width around center
    best_x = mid
    best_dark = 0

    y_start = int(h * 0.2)
    y_end = int(h * 0.8)

    for x in range(mid - search_range, mid + search_range + 1):
        if x < 0 or x >= w:
            continue
        dark_count = 0
        sample_count = 0
        for y in range(y_start, y_end, 3):
            r, g, b = pixels[x, y][:3]
            if is_dark((r, g, b), threshold=80):
                dark_count += 1
            sample_count += 1
        if dark_count > best_dark:
            best_dark = dark_count
            best_x = x

    # Find the full width of the gap (dark strip)
    gap_left = best_x
    gap_right = best_x
    for x in range(best_x, max(best_x - 30, 0), -1):
        dark_count = sum(
            1 for y in range(y_start, y_end, 5)
            if is_dark(pixels[x, y][:3], threshold=80)
        )
        if dark_count / max(len(range(y_start, y_end, 5)), 1) > 0.5:
            gap_left = x
        else:
            break
    for x in range(best_x, min(best_x + 30, w)):
        dark_count = sum(
            1 for y in range(y_start, y_end, 5)
            if is_dark(pixels[x, y][:3], threshold=80)
        )
        if dark_count / max(len(range(y_start, y_end, 5)), 1) > 0.5:
            gap_right = x
        else:
            break

    right_page = img.crop((gap_right, 0, w, h))
    left_page = img.crop((0, 0, gap_left, h))

    right_path = os.path.join(output_dir, f"page_{page_num_right:04d}.png")
    left_path = os.path.join(output_dir, f"page_{page_num_left:04d}.png")

    right_page.save(right_path, "PNG")
    left_page.save(left_path, "PNG")

    return right_path, left_path


def find_honto_window():
    """Find the main Honto app content window."""
    windows = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID)
    best = None
    for w in windows:
        owner = w.get("kCGWindowOwnerName", "")
        if "honto" in owner.lower():
            bounds = w.get("kCGWindowBounds", {})
            h = bounds.get("Height", 0)
            if best is None or h > best.get("kCGWindowBounds", {}).get("Height", 0):
                best = w
    return best


def capture_window(window_id, output_path):
    """Capture a screenshot of a specific window."""
    image = CGWindowListCreateImage(
        CGRectNull,
        kCGWindowListOptionIncludingWindow,
        window_id,
        kCGWindowImageBoundsIgnoreFraming,
    )
    if image is None:
        print("  Failed to capture window. Check Screen Recording permissions.")
        return False

    bitmap = NSBitmapImageRep.alloc().initWithCGImage_(image)
    png_data = bitmap.representationUsingType_properties_(NSPNGFileType, None)
    png_data.writeToFile_atomically_(output_path, True)
    return True


def bring_honto_to_front():
    """Activate the Honto app (bring to foreground)."""
    from AppKit import NSWorkspace

    workspace = NSWorkspace.sharedWorkspace()
    apps = workspace.runningApplications()
    for app in apps:
        if "honto" in (app.localizedName() or "").lower():
            app.activateWithOptions_(1 << 1)
            time.sleep(0.3)
            return True
    return False


def press_left_arrow():
    """Simulate pressing the left arrow key (next page in Japanese RTL books)."""
    key_down = CGEventCreateKeyboardEvent(None, 123, True)
    key_up = CGEventCreateKeyboardEvent(None, 123, False)
    CGEventPost(kCGHIDEventTap, key_down)
    CGEventPost(kCGHIDEventTap, key_up)


def press_right_arrow():
    """Simulate pressing the right arrow key (prev page in Japanese RTL books)."""
    key_down = CGEventCreateKeyboardEvent(None, 124, True)
    key_up = CGEventCreateKeyboardEvent(None, 124, False)
    CGEventPost(kCGHIDEventTap, key_down)
    CGEventPost(kCGHIDEventTap, key_up)


def main():
    parser = argparse.ArgumentParser(
        description="Capture Honto book pages as screenshots"
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=50,
        help="Number of spreads (screen navigations) to capture (default: 50)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay between pages in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output directory (default: auto-detected from book title)",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=1,
        help="Starting page number for filenames (default: 1)",
    )
    parser.add_argument(
        "--direction",
        choices=["left", "right"],
        default="left",
        help="Arrow key for next page: 'left' for RTL Japanese books, 'right' for LTR (default: left)",
    )
    parser.add_argument(
        "--start-delay",
        type=float,
        default=3.0,
        help="Initial delay before starting capture (default: 3.0)",
    )
    parser.add_argument(
        "--no-crop",
        action="store_true",
        help="Skip auto-cropping of UI chrome",
    )
    parser.add_argument(
        "--split",
        action="store_true",
        help="Split two-page spreads into individual pages",
    )
    args = parser.parse_args()

    advance = press_left_arrow if args.direction == "left" else press_right_arrow

    # Find the honto window
    window = find_honto_window()
    if window is None:
        print("Error: Honto app not found. Make sure it's open.")
        sys.exit(1)

    window_id = window["kCGWindowNumber"]
    bounds = window.get("kCGWindowBounds", {})
    print(
        f"Found Honto window (ID: {window_id}, {bounds.get('Width')}x{bounds.get('Height')})"
    )

    # Bring honto to front
    if not bring_honto_to_front():
        print("Warning: Could not activate Honto app. Continuing anyway...")

    # Detect book title from title bar via OCR
    if args.output is None:
        print("Detecting book title from title bar...")
        time.sleep(0.5)
        tmp_path = "/tmp/honto_title_detect.png"
        if capture_window(window_id, tmp_path):
            title = extract_title_from_titlebar(tmp_path)
            os.remove(tmp_path)
            if title:
                output_dir = os.path.join("./output", sanitize_filename(title))
                print(f"Book title: {title}")
            else:
                output_dir = "./output/untitled"
                print("Could not detect title, using 'untitled'")
        else:
            output_dir = "./output/untitled"
            print("Could not capture for title detection, using 'untitled'")
        args.output = output_dir

    os.makedirs(args.output, exist_ok=True)

    print(f"Starting capture in {args.start_delay}s...")
    print(f"Will capture {args.pages} spreads with {args.delay}s delay.")
    if args.split:
        print("Splitting spreads into individual pages.")
    print(f"Direction: {args.direction} arrow for next page")
    print(f"Output: {os.path.abspath(args.output)}/")
    print()

    time.sleep(args.start_delay)

    page_counter = args.start  # tracks individual page numbers when splitting
    spread_width = None  # learned from the first two-page spread

    for i in range(args.pages):
        spread_num = i + 1
        spread_filename = f"spread_{spread_num:04d}.png"
        spread_path = os.path.join(args.output, spread_filename)

        # Re-focus honto before every capture to ensure it's in front
        bring_honto_to_front()

        # Re-find window each time in case it moved
        window = find_honto_window()
        if window is None:
            print(f"  Lost Honto window at spread {spread_num}, stopping.")
            break

        window_id = window["kCGWindowNumber"]

        if not capture_window(window_id, spread_path):
            print(f"  [spread {spread_num}] FAILED - stopping.")
            break

        # Step 1: Crop UI chrome (title bar, nav bar, dark padding)
        content_width = None
        if not args.no_crop:
            crop_box, content_width = crop_content(spread_path)
            if i == 0:
                print(
                    f"  Auto-crop: L={crop_box[0]} T={crop_box[1]} R={crop_box[2]} B={crop_box[3]}"
                )

        # Step 2: Split into individual pages (if requested)
        if args.split:
            single = is_single_page(content_width, spread_width)
            center = not single and is_center_spread(spread_path)

            if single or center:
                # Single page or center spread — keep as one image, don't split
                page_path = os.path.join(
                    args.output, f"page_{page_counter:04d}.png"
                )
                os.rename(spread_path, page_path)
                label = "Center spread" if center else "Single page"
                print(
                    f"  [spread {spread_num}/{args.pages}] {label} → page_{page_counter:04d}.png"
                )
                page_counter += 1
            else:
                # Two-page spread — learn the spread width from the first one
                if spread_width is None and content_width is not None:
                    spread_width = content_width
                    print(f"  Spread width: {spread_width}px")

                right_num = page_counter
                left_num = page_counter + 1
                split_spread(spread_path, args.output, right_num, left_num)
                os.remove(spread_path)
                page_counter += 2
                print(
                    f"  [spread {spread_num}/{args.pages}] → page_{right_num:04d}.png, page_{left_num:04d}.png"
                )
        else:
            print(f"  [spread {spread_num}/{args.pages}] Saved {spread_filename}")

        # Don't advance after the last page
        if i < args.pages - 1:
            advance()
            time.sleep(args.delay)

    print(f"\nDone! {page_counter - args.start} pages saved to {os.path.abspath(args.output)}/")


if __name__ == "__main__":
    main()
