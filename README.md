# honto-capture

Capture book pages from the [Honto](https://honto.jp/) desktop reader app on macOS. Automatically crops UI chrome (title bar, navigation bar, padding) and optionally splits two-page spreads into individual pages.

## Requirements

- macOS (uses native screen capture and OCR APIs)
- Python 3.14+
- [uv](https://docs.astral.sh/uv/) package manager
- Honto desktop app installed and open to a book

### macOS Permissions

Grant your terminal the following in **System Settings > Privacy & Security**:

- **Accessibility** — required for simulating keyboard input
- **Screen Recording** — required for capturing the app window

## Installation

```bash
git clone https://github.com/causztic/honto-capture.git
cd honto-capture
uv sync
```

## Usage

1. Open the Honto app and navigate to the first page of your book
2. Run the capture script:

```bash
uv run python capture.py --pages 26 --split
```

3. The script will focus the Honto window, capture each page, and advance automatically

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--pages N` | 50 | Number of screen captures to take |
| `--delay SECONDS` | 1.0 | Delay between page captures |
| `--output DIR` | auto-detected | Output directory (auto-detected from book title via OCR) |
| `--start N` | 1 | Starting page number for filenames |
| `--direction left\|right` | left | Arrow key for next page (`left` for RTL Japanese books) |
| `--start-delay SECONDS` | 3.0 | Initial delay before starting capture |
| `--no-crop` | off | Skip auto-cropping of UI chrome |
| `--split` | off | Split two-page spreads into individual pages |

### Examples

Capture a 51-page photobook (26 spreads) with splitting:

```bash
uv run python capture.py --pages 26 --split
```

Capture 100 pages of a left-to-right book without splitting:

```bash
uv run python capture.py --pages 100 --direction right
```

Resume capture from page 51 into an existing output directory:

```bash
uv run python capture.py --pages 10 --start 51 --output ./output/my-book
```

## How It Works

1. **Window capture** — uses macOS Quartz APIs to screenshot the Honto window
2. **Title detection** — OCRs the title bar using macOS Vision framework to auto-name the output directory
3. **Auto-cropping** — detects and removes UI chrome (title bar, navigation bar, dark padding). Bounds are detected once from the first capture and reused for consistency
4. **Spread splitting** — when `--split` is enabled, detects whether each capture is a two-page spread or a center spread/single page using color discontinuity analysis at the center line. Two-page spreads are split; center spreads are kept whole
5. **Page width learning** — the individual page width is learned from the first split and reused for consistent page sizing

## Running Tests

```bash
uv run pytest
```

## Legal Disclaimer

This tool is provided for **personal, fair-use purposes only**. It is intended to help users create personal backups of digital books they have legally purchased through the Honto platform.

**You are solely responsible for ensuring your use of this tool complies with all applicable copyright laws and the terms of service of the Honto platform.** The authors of this tool do not condone or encourage copyright infringement, unauthorized distribution, or any use that violates intellectual property rights.

By using this tool, you acknowledge that:

- You will only capture books you have legally purchased
- You will not distribute, share, or make publicly available any captured content
- You will use captured content solely for personal reference and backup
- Your use complies with the copyright laws of your jurisdiction and any applicable fair use / fair dealing provisions

The authors of this tool bear no responsibility for any misuse or legal consequences arising from its use.
