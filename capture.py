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


def crop_content(image_path, fixed_bounds=None):
    """Crop out all UI chrome from a Honto screenshot, keeping only book content.

    If fixed_bounds is provided, uses those bounds instead of detecting.
    Returns (left, top, right, bottom) crop bounds and the cropped width.
    """
    img = Image.open(image_path)
    bounds = fixed_bounds if fixed_bounds is not None else find_content_bounds(img)
    cropped = img.crop(bounds)
    cropped.save(image_path, "PNG")
    return bounds, cropped.size[0]



def is_center_spread(image_path):
    """Detect if a cropped spread is a center spread (single continuous image).

    Two separate photos placed side by side produce a sharp vertical seam —
    nearly every row has a large color jump at the center line. A single
    continuous image has smooth transitions across the center.

    Compares the color discontinuity at the center vs elsewhere in the image.
    """
    img = Image.open(image_path)
    w, h = img.size
    mid_x = w // 2
    pixels = img.load()

    # Sample the middle 60% of height to avoid top/bottom edge artifacts
    y_start = int(h * 0.2)
    y_end = int(h * 0.8)
    step = 2

    # Measure color discontinuity at the center line
    center_diffs = []
    for y in range(y_start, y_end, step):
        r1, g1, b1 = pixels[mid_x - 1, y][:3]
        r2, g2, b2 = pixels[mid_x + 1, y][:3]
        diff = abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
        center_diffs.append(diff)

    # Measure baseline discontinuity at quarter and three-quarter points
    baseline_diffs = []
    for check_x in [w // 4, 3 * w // 4]:
        for y in range(y_start, y_end, step):
            r1, g1, b1 = pixels[check_x - 1, y][:3]
            r2, g2, b2 = pixels[check_x + 1, y][:3]
            diff = abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
            baseline_diffs.append(diff)

    avg_center = sum(center_diffs) / len(center_diffs) if center_diffs else 0
    avg_baseline = (
        sum(baseline_diffs) / len(baseline_diffs) if baseline_diffs else 1
    )

    # A seam between two photos has much higher discontinuity at center
    # than the average discontinuity within a single photo
    ratio = avg_center / avg_baseline if avg_baseline > 0 else 0
    is_continuous = ratio < 1.8 or avg_center < 15

    return is_continuous


def split_spread(image_path, output_dir, page_num_right, page_num_left,
                  page_width=None):
    """Split a two-page spread into individual pages.

    If page_width is known (from a previous split), uses it for consistent
    page sizing. Otherwise splits at the center.
    For RTL books, the right page has the lower page number.

    Returns the width of each individual page for reuse.
    """
    img = Image.open(image_path)
    w, h = img.size
    mid = w // 2

    if page_width is not None:
        # Use learned page width for consistent splits
        right_page = img.crop((w - page_width, 0, w, h))
        left_page = img.crop((0, 0, page_width, h))
    else:
        # First split — just use the center
        right_page = img.crop((mid, 0, w, h))
        left_page = img.crop((0, 0, mid, h))

    right_path = os.path.join(output_dir, f"page_{page_num_right:04d}.png")
    left_path = os.path.join(output_dir, f"page_{page_num_left:04d}.png")

    right_page.save(right_path, "PNG")
    left_page.save(left_path, "PNG")

    return right_page.size[0]


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
    page_width = None  # learned from first split for consistent page sizing
    crop_bounds = None  # learned from first capture for consistent cropping

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
        # Detect bounds from the first capture, then reuse for consistency
        content_width = None
        if not args.no_crop:
            crop_box, content_width = crop_content(
                spread_path, fixed_bounds=crop_bounds
            )
            if crop_bounds is None:
                crop_bounds = crop_box
                print(
                    f"  Auto-crop: L={crop_box[0]} T={crop_box[1]} R={crop_box[2]} B={crop_box[3]}"
                )

        # Step 2: Split into individual pages (if requested)
        if args.split:
            if is_center_spread(spread_path):
                # Single page or center spread — keep as one image
                page_path = os.path.join(
                    args.output, f"page_{page_counter:04d}.png"
                )
                os.rename(spread_path, page_path)
                print(
                    f"  [spread {spread_num}/{args.pages}] Single/center → page_{page_counter:04d}.png"
                )
                page_counter += 1
            else:
                # Two-page spread — split using learned page width
                right_num = page_counter
                left_num = page_counter + 1
                pw = split_spread(
                    spread_path, args.output, right_num, left_num,
                    page_width=page_width,
                )
                os.remove(spread_path)
                if page_width is None:
                    page_width = pw
                    print(f"  Page width: {page_width}px")
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
