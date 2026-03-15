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
    top = 0
    in_toolbar = True
    for y in range(h):
        r, g, b = pixels[w // 2, y][:3]
        if in_toolbar:
            if not is_light_toolbar((r, g, b)):
                in_toolbar = False
                # Check if we hit dark padding or directly content
                if is_dark((r, g, b)):
                    continue  # skip dark padding after toolbar
                else:
                    top = y
                    break
        else:
            if not is_dark((r, g, b)):
                top = y
                break

    # --- BOTTOM: Scan up from bottom, skip nav bar ---
    # Nav bar layout (bottom to top): colored scrubber line, black bar, content.
    # Strategy: find the transition from black nav bar to content.
    # First, skip any non-black rows at the very bottom (scrubber/icons),
    # then skip the black bar, then we've found the content edge.
    bottom = h
    y = h - 1
    check_points = 30
    # Phase 1: skip scrubber/colored rows at very bottom (thin, <10px)
    while y > top:
        black_count = 0
        for ci in range(check_points):
            cx = int(w * (ci + 1) / (check_points + 1))
            r, g, b = pixels[cx, y][:3]
            if r < 30 and g < 30 and b < 30:
                black_count += 1
        if black_count > check_points * 0.8:
            break  # hit the black bar
        y -= 1
    # Phase 2: skip the black nav bar
    while y > top:
        black_count = 0
        for ci in range(check_points):
            cx = int(w * (ci + 1) / (check_points + 1))
            r, g, b = pixels[cx, y][:3]
            if r < 30 and g < 30 and b < 30:
                black_count += 1
        if black_count < check_points * 0.3:
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


def split_spread(image_path, output_dir, page_num_right, page_num_left):
    """Split a two-page spread into individual pages.

    Splits at the center of the image. After cropping UI chrome, the remaining
    content is the two book pages side by side with no visible separator.
    For RTL books, the right page has the lower page number.
    """
    img = Image.open(image_path)
    w, h = img.size
    mid = w // 2

    right_page = img.crop((mid, 0, w, h))
    left_page = img.crop((0, 0, mid, h))

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

            if single:
                # Single page (cover, colophon, etc.) — just rename, don't split
                page_path = os.path.join(
                    args.output, f"page_{page_counter:04d}.png"
                )
                os.rename(spread_path, page_path)
                print(
                    f"  [spread {spread_num}/{args.pages}] Single page → page_{page_counter:04d}.png"
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
