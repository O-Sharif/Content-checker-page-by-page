"""
qa_check.py — Responsive QA tool.

What it does:
  1. Captures full-page screenshots of LIVE and STAGING at multiple viewport
     widths (desktop down to mobile).
  2. Extracts text content from both pages (at the largest width).
  3. Generates an HTML report with:
       - Side-by-side screenshots grouped by width (one row per width)
       - Width selector tabs at the top to jump between widths
       - Text content "Changed" diff with word-level highlighting

How to use:
  1. Edit LIVE_URL and STAGING_URL below.
  2. Install dependencies (one time):
        pip install -r requirements.txt
        playwright install chromium
  3. Run:
        python qa_check.py
  4. Open the generated `qa_report.html` in a browser.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from playwright.sync_api import sync_playwright
from PIL import Image

# ===========================================================================
# >>> EDIT THESE <<<
# ===========================================================================
LIVE_URL = "https://malbisdentstaging.com/services"
STAGING_URL = "https://malbisparkwaydentistry.kinsta.cloud/services/"

# Viewport widths to capture (px). Each width produces its own pair of
# screenshots in the report.
VIEWPORT_WIDTHS = [1440, 768, 320]

# Each viewport's height. The full-page screenshot will scroll past this; the
# height just defines what's "above the fold" for any sticky elements.
VIEWPORT_HEIGHT = 900

# How long to wait after DOM loads for fonts/images/animations to settle (seconds).
WAIT_SECONDS = 4

# How similar two text snippets need to be to count as "changed" (vs. separate
# entries). 0.0 = totally different, 1.0 = identical. 0.6 catches rewordings
# while staying strict enough to avoid false pairs.
TEXT_SIMILARITY_THRESHOLD = 0.6

OUTPUT_DIR = "qa_output"
OUTPUT_FILE = "qa_report.html"
# ===========================================================================

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ===========================================================================
# Browser-side text extraction
# ===========================================================================
EXTRACT_TEXT_JS = r"""
() => {
  const SKIP_TAGS = new Set([
    'SCRIPT','STYLE','NOSCRIPT','TEMPLATE','IFRAME','HEAD','META','LINK'
  ]);

  function isVisible(el) {
    if (!el || el.nodeType !== 1) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    return true;
  }

  // Pull just the FIRST family name from a font-family stack.
  function primaryFamily(fontFamily) {
    if (!fontFamily) return '';
    const first = fontFamily.split(',')[0].trim();
    return first.replace(/^["']|["']$/g, '');
  }

  const texts = [];
  function walk(el) {
    if (!el || el.nodeType !== 1) return;
    if (SKIP_TAGS.has(el.tagName)) return;
    if (!isVisible(el)) return;

    let ownText = '';
    for (const node of el.childNodes) {
      if (node.nodeType === 3) ownText += node.nodeValue;
    }
    ownText = ownText.replace(/\s+/g, ' ').trim();
    if (ownText.length > 0) {
      const trimmed = ownText.trim();
      const isJunk = (
        /^[|·•»›→\-–—\s]+$/.test(trimmed) ||
        /^skip\s+to\s+(content|main|navigation)/i.test(trimmed) ||
        (/^[a-z][a-z0-9-]+$/i.test(trimmed) && trimmed.includes('-') && trimmed.length > 8) ||
        /^(web\s+design\s+by|powered\s+by|designed\s+by|developed\s+by)$/i.test(trimmed) ||
        trimmed.length < 2
      );
      if (!isJunk) {
        const cs = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        let parent = el.parentElement;
        let parent_id = '';
        let parent_class = '';
        while (parent && parent !== document.body) {
          if (parent.id) { parent_id = parent.id; break; }
          if (parent.className && typeof parent.className === 'string') {
            parent_class = parent.className.trim().split(/\s+/).slice(0, 2).join(' ');
            if (parent_class) break;
          }
          parent = parent.parentElement;
        }
        texts.push({
          tag: el.tagName.toLowerCase(),
          text: ownText,
          font_family: cs.fontFamily || '',
          font_primary: primaryFamily(cs.fontFamily),
          font_size: cs.fontSize || '',
          font_weight: cs.fontWeight || '',
          color: cs.color || '',
          background_color: cs.backgroundColor || '',
          x: Math.round(r.left + window.scrollX),
          y: Math.round(r.top + window.scrollY),
          w: Math.round(r.width),
          h: Math.round(r.height),
          parent_id: parent_id,
          parent_class: parent_class,
        });
      }
    }

    for (const child of el.children) walk(child);
  }
  walk(document.body);
  return texts;
}
"""




# ===========================================================================
# Capture: one browser session covers all widths for one URL
# ===========================================================================
def capture_url_at_widths(
    url: str,
    widths: list[int],
    out_dir: Path,
    label: str,
    extract_text_at_width: int | None = None,
) -> tuple[dict[int, str], list[dict] | None, str]:
    """
    For each viewport width: open the URL in a fresh context with that
    viewport, scroll to trigger lazy-load, screenshot.

    Args:
      extract_text_at_width: if set, also extract page text at this width.
        Text content is the same regardless of width — we just need to grab
        it once.

    Returns:
      (screenshots_by_width, texts_or_None, error_message)
        screenshots_by_width: { width_int: relative_path_str }
        texts_or_None: list of {tag, text} if extract_text_at_width was set
    """
    screenshots: dict[int, str] = {}
    texts: list[dict] | None = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            for width in widths:
                print(f"  [{label}] {width}px ...", end=" ", flush=True)
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": width, "height": VIEWPORT_HEIGHT},
                )
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(WAIT_SECONDS * 1000)
                    page.evaluate(
                        "() => new Promise(resolve => {"
                        "  const distance = 300;"
                        "  const delay = 100;"
                        "  const timer = setInterval(() => {"
                        "    window.scrollBy(0, distance);"
                        "    if (window.innerHeight + window.scrollY >= document.body.scrollHeight) {"
                        "      clearInterval(timer);"
                        "      window.scrollTo(0, 0);"
                        "      resolve();"
                        "    }"
                        "  }, delay);"
                        "})"
                    )
                    page.wait_for_timeout(500)

                    if extract_text_at_width == width and texts is None:
                        texts = page.evaluate(EXTRACT_TEXT_JS)

                    img_path = out_dir / f"{label}_{width}.png"
                    page.screenshot(path=str(img_path), full_page=True)
                    screenshots[width] = f"{OUTPUT_DIR}/{label}_{width}.png"
                    print("OK")
                except Exception as e:
                    print(f"FAILED: {type(e).__name__}: {e}")
                finally:
                    context.close()

            browser.close()
        return screenshots, texts, ""
    except Exception as e:
        return screenshots, texts, f"{type(e).__name__}: {e}"


# ===========================================================================
# Cropping helper for font-mismatch screenshots
# ===========================================================================
def crop_text_region(
    screenshot_path: Path,
    out_path: Path,
    x: int, y: int, w: int, h: int,
    pad_x: int = 80,
    pad_y: int = 30,
) -> bool:
    """
    Crop a region from a full-page screenshot, with padding around the text
    so the user can see surrounding context. Returns True on success.
    """
    try:
        img = Image.open(screenshot_path)
        iw, ih = img.size
        left   = max(0, x - pad_x)
        top    = max(0, y - pad_y)
        right  = min(iw, x + w + pad_x)
        bottom = min(ih, y + h + pad_y)
        if right <= left or bottom <= top:
            return False
        crop = img.crop((left, top, right, bottom))
        crop.save(out_path)
        return True
    except Exception as e:
        print(f"  [warn] crop failed: {e}")
        return False


# ===========================================================================
# Font diff
# ===========================================================================
def diff_fonts(live_texts: list[dict], staging_texts: list[dict]) -> dict:
    """
    Returns:
      live_fonts:    sorted list of unique primary font families on live
      staging_fonts: sorted list of unique primary font families on staging
      mismatches:    text blocks that exist on both sides (matched by exact text)
                     but use different primary font families. Each entry:
                       {text, tag, live_font, staging_font, ...}
    """
    def collect_fonts(items: list[dict]) -> list[str]:
        seen: dict[str, int] = {}
        for it in items:
            f = (it.get("font_primary") or "").strip()
            if not f:
                continue
            seen[f] = seen.get(f, 0) + 1
        return sorted(seen.keys(), key=lambda k: (-seen[k], k.lower()))

    live_fonts = collect_fonts(live_texts)
    staging_fonts = collect_fonts(staging_texts)

    # Per-text mismatches: match by exact (case-insensitive, whitespace-collapsed) text
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip().lower()

    staging_by_text: dict[str, dict] = {}
    for it in staging_texts:
        key = norm(it.get("text", ""))
        if key and key not in staging_by_text:
            staging_by_text[key] = it

    mismatches: list[dict] = []
    seen_text: set[str] = set()
    for it in live_texts:
        key = norm(it.get("text", ""))
        if not key or key in seen_text:
            continue
        seen_text.add(key)
        stg = staging_by_text.get(key)
        if not stg:
            continue
        live_f = (it.get("font_primary") or "").strip()
        stg_f = (stg.get("font_primary") or "").strip()
        if live_f and stg_f and live_f.lower() != stg_f.lower():
            mismatches.append({
                "text": it.get("text", ""),
                "tag": it.get("tag", ""),
                "live_font": live_f,
                "staging_font": stg_f,
                "live_full": it.get("font_family", ""),
                "staging_full": stg.get("font_family", ""),
                "live_size": it.get("font_size", ""),
                "staging_size": stg.get("font_size", ""),
                "live_weight": it.get("font_weight", ""),
                "staging_weight": stg.get("font_weight", ""),
                "live_pos": (it.get("x", 0), it.get("y", 0), it.get("w", 0), it.get("h", 0)),
                "staging_pos": (stg.get("x", 0), stg.get("y", 0), stg.get("w", 0), stg.get("h", 0)),
                "parent": it.get("parent_id") or it.get("parent_class") or "",
                # Crop image paths (filled in by main() if cropping succeeds)
                "live_crop": None,
                "staging_crop": None,
            })

    return {
        "live_fonts": live_fonts,
        "staging_fonts": staging_fonts,
        "mismatches": mismatches,
    }


# ===========================================================================
# Color diff
# ===========================================================================
def _normalize_color(c: str) -> str:
    """
    Normalize a CSS color string to a canonical 'rgb(r, g, b)' or 'rgba(...)'
    form so colors written in different syntaxes (oklch, hsl, hex, named)
    compare equal when they represent the same visual color.

    Returns "" for fully transparent / empty values so we don't flag
    transparent backgrounds as color differences.
    """
    if not c:
        return ""
    rgba = _parse_to_rgba(c)
    if rgba is None:
        # Couldn't parse — fall back to the raw string with whitespace normalized
        return re.sub(r"\s+", "", c).lower()
    r, g, b, a = rgba
    if a == 0.0:
        return ""  # fully transparent
    if a is None or a >= 1.0:
        return f"rgb({r},{g},{b})"
    return f"rgba({r},{g},{b},{a:g})"


def _color_to_hex(c: str) -> str:
    """
    Convert any CSS color string to '#rrggbb' (or '#rrggbbaa' if it has alpha).
    Handles: rgb(), rgba(), hsl(), hsla(), hwb(), oklab(), oklch(), lab(),
    lch(), hex, and basic named colors. Returns the original string if it
    can't be parsed.
    """
    if not c:
        return ""
    s = c.strip()
    rgba = _parse_to_rgba(s)
    if rgba is None:
        return s  # unparseable; show original
    r, g, b, a = rgba
    if a is None or a >= 1.0:
        return f"#{r:02x}{g:02x}{b:02x}"
    aa = max(0, min(255, int(round(a * 255))))
    return f"#{r:02x}{g:02x}{b:02x}{aa:02x}"


def _color_to_rgb_string(c: str) -> str:
    """
    Convert any CSS color to a canonical 'rgb(r, g, b)' or 'rgba(r, g, b, a)'
    string. This is what we use for comparison so live and staging match
    when they describe the same color in different syntaxes (oklch vs rgb).
    """
    if not c:
        return ""
    rgba = _parse_to_rgba(c)
    if rgba is None:
        return c.strip()
    r, g, b, a = rgba
    if a is None or a >= 1.0:
        return f"rgb({r}, {g}, {b})"
    return f"rgba({r}, {g}, {b}, {a:g})"


# ----- Color-format parsers ---------------------------------------------------

_NAMED_COLORS = {
    # Common named colors. Browsers support 140+ but these cover ~all real usage.
    "transparent": (0, 0, 0, 0.0),
    "black": (0, 0, 0, 1.0), "white": (255, 255, 255, 1.0),
    "red": (255, 0, 0, 1.0), "green": (0, 128, 0, 1.0), "blue": (0, 0, 255, 1.0),
    "yellow": (255, 255, 0, 1.0), "cyan": (0, 255, 255, 1.0),
    "magenta": (255, 0, 255, 1.0), "silver": (192, 192, 192, 1.0),
    "gray": (128, 128, 128, 1.0), "grey": (128, 128, 128, 1.0),
    "maroon": (128, 0, 0, 1.0), "olive": (128, 128, 0, 1.0),
    "lime": (0, 255, 0, 1.0), "aqua": (0, 255, 255, 1.0),
    "teal": (0, 128, 128, 1.0), "navy": (0, 0, 128, 1.0),
    "fuchsia": (255, 0, 255, 1.0), "purple": (128, 0, 128, 1.0),
    "orange": (255, 165, 0, 1.0), "pink": (255, 192, 203, 1.0),
    "brown": (165, 42, 42, 1.0), "gold": (255, 215, 0, 1.0),
}


def _parse_to_rgba(c: str) -> tuple[int, int, int, float | None] | None:
    """
    Parse any CSS color into (r, g, b, alpha). Returns None if unparseable.
    r/g/b are 0-255 ints; alpha is 0.0-1.0 float (or None for fully opaque).
    """
    if not c:
        return None
    s = c.strip().lower()

    # Named color
    if s in _NAMED_COLORS:
        r, g, b, a = _NAMED_COLORS[s]
        return (r, g, b, None if a == 1.0 else a)

    # Hex
    if s.startswith("#"):
        hexpart = s[1:]
        if len(hexpart) == 3:
            r, g, b = (int(ch * 2, 16) for ch in hexpart)
            return (r, g, b, None)
        if len(hexpart) == 4:
            r, g, b, a = (int(ch * 2, 16) for ch in hexpart)
            return (r, g, b, a / 255.0)
        if len(hexpart) == 6:
            r = int(hexpart[0:2], 16); g = int(hexpart[2:4], 16); b = int(hexpart[4:6], 16)
            return (r, g, b, None)
        if len(hexpart) == 8:
            r = int(hexpart[0:2], 16); g = int(hexpart[2:4], 16); b = int(hexpart[4:6], 16)
            a = int(hexpart[6:8], 16) / 255.0
            return (r, g, b, a)
        return None

    # Functional notations: rgb(), rgba(), hsl(), hsla(), hwb(),
    # oklab(), oklch(), lab(), lch()
    m = re.match(r"^([a-z]+)\(([^)]+)\)$", s)
    if not m:
        return None
    fn = m.group(1)
    raw_args = m.group(2).replace("/", " ").replace(",", " ")
    parts = [p for p in raw_args.split() if p]
    if not parts:
        return None

    def num(s: str, *, percent_max: float = 100.0) -> float:
        s = s.strip()
        if s.endswith("%"):
            return float(s[:-1]) / 100.0 * percent_max
        return float(s)

    try:
        if fn in ("rgb", "rgba"):
            r = int(round(num(parts[0], percent_max=255)))
            g = int(round(num(parts[1], percent_max=255)))
            b = int(round(num(parts[2], percent_max=255)))
            a = float(parts[3]) if len(parts) >= 4 else None
            if a is not None and parts[3].endswith("%"):
                a = a / 100.0
            return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)),
                    a if a is None or a < 1.0 else None)

        if fn in ("hsl", "hsla"):
            h = float(parts[0].rstrip("deg")) % 360
            sat = num(parts[1], percent_max=1.0)
            light = num(parts[2], percent_max=1.0)
            r, g, b = _hsl_to_rgb(h, sat, light)
            a = float(parts[3]) if len(parts) >= 4 else None
            if a is not None and parts[3].endswith("%"):
                a = a / 100.0
            return (r, g, b, a if a is None or a < 1.0 else None)

        if fn == "hwb":
            h = float(parts[0].rstrip("deg")) % 360
            w = num(parts[1], percent_max=1.0)
            blk = num(parts[2], percent_max=1.0)
            r, g, b = _hwb_to_rgb(h, w, blk)
            a = float(parts[3]) if len(parts) >= 4 else None
            return (r, g, b, a if a is None or a < 1.0 else None)

        if fn == "oklch":
            L = num(parts[0], percent_max=1.0)
            C = float(parts[1])
            H = float(parts[2].rstrip("deg"))
            r, g, b = _oklch_to_rgb(L, C, H)
            a = float(parts[3]) if len(parts) >= 4 else None
            return (r, g, b, a if a is None or a < 1.0 else None)

        if fn == "oklab":
            L = num(parts[0], percent_max=1.0)
            a_val = float(parts[1])
            b_val = float(parts[2])
            r, g, b = _oklab_to_rgb(L, a_val, b_val)
            a = float(parts[3]) if len(parts) >= 4 else None
            return (r, g, b, a if a is None or a < 1.0 else None)

        if fn == "lch":
            # CIE LCh — approximate via oklch (close enough for QA purposes)
            L = num(parts[0], percent_max=1.0)
            C = float(parts[1]) / 100.0
            H = float(parts[2].rstrip("deg"))
            r, g, b = _oklch_to_rgb(L, C, H)
            a = float(parts[3]) if len(parts) >= 4 else None
            return (r, g, b, a if a is None or a < 1.0 else None)

        if fn == "lab":
            L = num(parts[0], percent_max=1.0)
            a_val = float(parts[1]) / 100.0
            b_val = float(parts[2]) / 100.0
            r, g, b = _oklab_to_rgb(L, a_val, b_val)
            a = float(parts[3]) if len(parts) >= 4 else None
            return (r, g, b, a if a is None or a < 1.0 else None)
    except (ValueError, IndexError):
        return None
    return None


def _hsl_to_rgb(h: float, s: float, l: float) -> tuple[int, int, int]:
    """h in degrees [0,360), s/l in [0,1]. Returns (r,g,b) ints in [0,255]."""
    s = max(0.0, min(1.0, s))
    l = max(0.0, min(1.0, l))
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2
    if h < 60:    r1, g1, b1 = c, x, 0
    elif h < 120: r1, g1, b1 = x, c, 0
    elif h < 180: r1, g1, b1 = 0, c, x
    elif h < 240: r1, g1, b1 = 0, x, c
    elif h < 300: r1, g1, b1 = x, 0, c
    else:         r1, g1, b1 = c, 0, x
    return (int(round((r1 + m) * 255)),
            int(round((g1 + m) * 255)),
            int(round((b1 + m) * 255)))


def _hwb_to_rgb(h: float, w: float, b: float) -> tuple[int, int, int]:
    """HWB to RGB. h in degrees, w/b in [0,1]."""
    if w + b >= 1:
        gray = int(round(w / (w + b) * 255))
        return (gray, gray, gray)
    r, g, bl = _hsl_to_rgb(h, 1.0, 0.5)
    rng = 1 - w - b
    return (
        int(round((r / 255 * rng + w) * 255)),
        int(round((g / 255 * rng + w) * 255)),
        int(round((bl / 255 * rng + w) * 255)),
    )


def _oklab_to_rgb(L: float, a: float, b: float) -> tuple[int, int, int]:
    """OKLab → linear sRGB → sRGB. Math from https://bottosson.github.io/posts/oklab/"""
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b

    l = l_ ** 3
    m = m_ ** 3
    s = s_ ** 3

    r_lin = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g_lin = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b_lin = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s

    def to_srgb(x: float) -> int:
        x = max(0.0, min(1.0, x))
        if x <= 0.0031308:
            v = 12.92 * x
        else:
            v = 1.055 * (x ** (1 / 2.4)) - 0.055
        return int(round(max(0.0, min(1.0, v)) * 255))

    return (to_srgb(r_lin), to_srgb(g_lin), to_srgb(b_lin))


def _oklch_to_rgb(L: float, C: float, H_deg: float) -> tuple[int, int, int]:
    """OKLCh → OKLab → RGB."""
    import math
    H = math.radians(H_deg)
    a = C * math.cos(H)
    b = C * math.sin(H)
    return _oklab_to_rgb(L, a, b)


def diff_colors(live_texts: list[dict], staging_texts: list[dict]) -> dict:
    """
    Returns:
      live_colors:    sorted list of unique text colors used on live (with counts)
      staging_colors: same for staging
      mismatches:     text blocks that exist on both sides (matched by exact text)
                      but use a different text color. Each entry includes
                      bounding box info and parent context for cropping.

    Background-color isn't reported in the side-by-side palette (most elements
    inherit `transparent` so the list would be noisy), but background mismatches
    on flagged text ARE shown alongside the text-color mismatch when present.
    """
    def collect_colors(items: list[dict]) -> list[tuple[str, int]]:
        seen: dict[str, int] = {}
        for it in items:
            c = _normalize_color(it.get("color", ""))
            if not c:
                continue
            seen[c] = seen.get(c, 0) + 1
        return sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))

    live_colors = collect_colors(live_texts)
    staging_colors = collect_colors(staging_texts)

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip().lower()

    staging_by_text: dict[str, dict] = {}
    for it in staging_texts:
        key = norm(it.get("text", ""))
        if key and key not in staging_by_text:
            staging_by_text[key] = it

    mismatches: list[dict] = []
    seen_text: set[str] = set()
    for it in live_texts:
        key = norm(it.get("text", ""))
        if not key or key in seen_text:
            continue
        seen_text.add(key)
        stg = staging_by_text.get(key)
        if not stg:
            continue
        live_c = _normalize_color(it.get("color", ""))
        stg_c = _normalize_color(stg.get("color", ""))
        if live_c and stg_c and live_c != stg_c:
            mismatches.append({
                "text": it.get("text", ""),
                "tag": it.get("tag", ""),
                "live_color": _color_to_hex(it.get("color", "")),
                "staging_color": _color_to_hex(stg.get("color", "")),
                "live_color_raw": it.get("color", ""),
                "staging_color_raw": stg.get("color", ""),
                "live_color_rgb": _color_to_rgb_string(it.get("color", "")),
                "staging_color_rgb": _color_to_rgb_string(stg.get("color", "")),
                "live_bg": _color_to_hex(_normalize_color(it.get("background_color", ""))),
                "staging_bg": _color_to_hex(_normalize_color(stg.get("background_color", ""))),
                "live_pos": (it.get("x", 0), it.get("y", 0), it.get("w", 0), it.get("h", 0)),
                "staging_pos": (stg.get("x", 0), stg.get("y", 0), stg.get("w", 0), stg.get("h", 0)),
                "parent": it.get("parent_id") or it.get("parent_class") or "",
                "live_crop": None,
                "staging_crop": None,
            })

    return {
        "live_colors": live_colors,
        "staging_colors": staging_colors,
        "mismatches": mismatches,
    }


# ===========================================================================
# Text diff
# ===========================================================================
def _normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def diff_texts(
    live_texts: list[dict],
    staging_texts: list[dict],
    threshold: float = TEXT_SIMILARITY_THRESHOLD,
) -> dict:
    """
    Returns 'changed' — pairs of texts that look similar but aren't identical,
    along with a similarity score.
    """
    live_norm = [(i, _normalize_text(b["text"]), b) for i, b in enumerate(live_texts)]
    staging_norm = [(i, _normalize_text(b["text"]), b) for i, b in enumerate(staging_texts)]

    # Step 1: drop exact (normalized) matches from both sides
    staging_by_text: dict[str, list[int]] = {}
    for idx, norm, _ in staging_norm:
        staging_by_text.setdefault(norm, []).append(idx)

    live_used: set[int] = set()
    staging_used: set[int] = set()

    for li, lnorm, _ in live_norm:
        cands = [c for c in staging_by_text.get(lnorm, []) if c not in staging_used]
        if cands:
            staging_used.add(cands[0])
            live_used.add(li)

    # Step 2: fuzzy-pair the rest
    changed: list[tuple[dict, dict, float]] = []
    live_remaining = [(i, n, b) for i, n, b in live_norm if i not in live_used]
    staging_remaining = [(i, n, b) for i, n, b in staging_norm if i not in staging_used]

    for li, lnorm, lb in live_remaining:
        best_score = 0.0
        best_si = -1
        for si, snorm, _ in staging_remaining:
            if si in staging_used:
                continue
            longer = max(len(lnorm), len(snorm)) or 1
            if abs(len(lnorm) - len(snorm)) > longer * 0.7:
                continue
            score = SequenceMatcher(None, lnorm, snorm).ratio()
            if score > best_score:
                best_score = score
                best_si = si
        if best_si >= 0 and best_score >= threshold:
            staging_used.add(best_si)
            live_used.add(li)
            changed.append((lb, staging_texts[best_si], best_score))

    return {"changed": changed}


def inline_diff_html(a: str, b: str) -> tuple[str, str]:
    a_words = re.findall(r"\S+|\s+", a)
    b_words = re.findall(r"\S+|\s+", b)
    matcher = SequenceMatcher(None, a_words, b_words)
    a_out: list[str] = []
    b_out: list[str] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        a_chunk = "".join(a_words[i1:i2])
        b_chunk = "".join(b_words[j1:j2])
        if op == "equal":
            a_out.append(html.escape(a_chunk))
            b_out.append(html.escape(b_chunk))
        elif op == "replace":
            a_out.append(f'<span class="del">{html.escape(a_chunk)}</span>')
            b_out.append(f'<span class="add">{html.escape(b_chunk)}</span>')
        elif op == "delete":
            a_out.append(f'<span class="del">{html.escape(a_chunk)}</span>')
        elif op == "insert":
            b_out.append(f'<span class="add">{html.escape(b_chunk)}</span>')
    return "".join(a_out), "".join(b_out)


# ===========================================================================
# CSS / JS / HTML
# ===========================================================================
CSS = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  margin: 0;
  padding: 24px;
  background: #f6f7f9;
  color: #1a1a1a;
  line-height: 1.5;
}
.container { max-width: 1700px; margin: 0 auto; }
h1 { margin: 0 0 8px; font-size: 26px; }
h2 { margin: 32px 0 12px; font-size: 20px; border-bottom: 1px solid #e3e5e8; padding-bottom: 6px; }
.timestamp { color: #6b7280; font-size: 13px; margin-bottom: 20px; }
.meta {
  background: #fff;
  border: 1px solid #e3e5e8;
  border-radius: 8px;
  padding: 16px 20px;
  margin-bottom: 20px;
  display: grid;
  grid-template-columns: 140px 1fr;
  gap: 6px 16px;
  font-size: 14px;
}
.meta dt { color: #6b7280; }
.meta dd { margin: 0; word-break: break-all; }

/* Sticky controls bar — visible while you scroll through every width block */
.controls {
  position: sticky;
  top: 0;
  z-index: 50;
  background: #fff;
  border: 1px solid #e3e5e8;
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 16px;
  display: flex;
  gap: 20px;
  align-items: center;
  flex-wrap: wrap;
  font-size: 14px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.04);
}
.controls label { cursor: pointer; user-select: none; display: inline-flex; align-items: center; gap: 6px; }
.controls input[type=range] { width: 200px; }
.controls .zoom-val { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; min-width: 44px; }

/* Width tabs */
.width-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 20px;
  background: #fff;
  border: 1px solid #e3e5e8;
  border-radius: 8px;
  padding: 12px;
}
.width-tab {
  display: inline-block;
  padding: 6px 12px;
  background: #f3f4f6;
  border: 1px solid #e3e5e8;
  border-radius: 6px;
  font-size: 13px;
  color: #374151;
  text-decoration: none;
  cursor: pointer;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.width-tab:hover { background: #e0e7ff; border-color: #818cf8; }

/* Per-width comparison block */
.width-block {
  background: #fff;
  border: 1px solid #e3e5e8;
  border-radius: 8px;
  margin-bottom: 24px;
  overflow: hidden;
}
.width-block-header {
  padding: 14px 18px;
  background: #f9fafb;
  border-bottom: 1px solid #e3e5e8;
  font-weight: 600;
  font-size: 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.width-label {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: #eef2ff;
  color: #4338ca;
  padding: 3px 10px;
  border-radius: 4px;
  font-size: 13px;
}
.width-compare {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0;
}
.width-pane {
  display: flex;
  flex-direction: column;
  border-right: 1px solid #e3e5e8;
}
.width-pane:last-child { border-right: none; }
.width-pane-header {
  padding: 8px 14px;
  background: #fafbfc;
  border-bottom: 1px solid #e3e5e8;
  font-weight: 600;
  font-size: 13px;
}
.width-pane-header .label-live    { color: #15803d; }
.width-pane-header .label-staging { color: #1d4ed8; }
.width-pane-header a {
  font-weight: 400;
  font-size: 12px;
  color: #6b7280;
  text-decoration: none;
  margin-left: 8px;
}
.width-pane-header a:hover { text-decoration: underline; }
.shot-wrap {
  overflow: auto;
  max-height: 80vh;
  background: #fafbfc;
}
.shot-wrap img {
  display: block;
  max-width: 100%;
  height: auto;
  transition: transform 0.1s;
  transform-origin: top left;
}
.shot-missing {
  padding: 40px;
  text-align: center;
  color: #9ca3af;
  font-style: italic;
  font-size: 14px;
}
.error-box {
  background: #fee2e2;
  border: 1px solid #fca5a5;
  color: #7f1d1d;
  padding: 16px;
  border-radius: 6px;
  font-size: 14px;
  margin: 16px 0;
}

/* Content section */
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.stat {
  background: #fff;
  border: 1px solid #e3e5e8;
  border-radius: 8px;
  padding: 14px 16px;
}
.stat .num { font-size: 24px; font-weight: 700; }
.stat .label { color: #6b7280; font-size: 13px; }
.stat.zero .num { color: #15803d; }
.stat.alert .num { color: #b91c1c; }
.card {
  background: #fff;
  border: 1px solid #e3e5e8;
  border-radius: 8px;
  padding: 16px 20px;
  margin-bottom: 16px;
}
.card h3 { margin: 0 0 12px; font-size: 16px; }
.tag {
  display: inline-block;
  font-size: 11px;
  background: #eef2ff;
  color: #4338ca;
  padding: 1px 6px;
  border-radius: 4px;
  margin-right: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  text-transform: uppercase;
}
.list-empty { color: #15803d; font-style: italic; font-size: 14px; }
.changed-pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  padding: 12px 0;
  border-bottom: 1px solid #f1f3f5;
}
.changed-pair:last-child { border-bottom: none; }
.changed-side {
  background: #fafbfc;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 14px;
  word-break: break-word;
}
.changed-side.live    { border-left: 3px solid #15803d; }
.changed-side.staging { border-left: 3px solid #1d4ed8; }
.side-label {
  font-size: 11px;
  font-weight: 600;
  color: #6b7280;
  text-transform: uppercase;
  margin-bottom: 4px;
  display: block;
}
.del { background: #ffeef0; color: #8a1f11; text-decoration: line-through; padding: 0 2px; border-radius: 3px; }
.add { background: #e6ffed; color: #0a6b2c; padding: 0 2px; border-radius: 3px; }

/* Fonts section */
.fonts-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}
.font-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.font-chip {
  display: inline-block;
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 14px;
  border: 1px solid #e3e5e8;
}
.font-chip-shared { background: #f3f4f6; color: #1a1a1a; }
.font-chip-live { background: #dcfce7; color: #166534; border-color: #86efac; }
.font-chip-staging { background: #dbeafe; color: #1e40af; border-color: #93c5fd; }

/* Color chips & swatches */
.color-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.color-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border-radius: 6px;
  font-size: 13px;
  border: 1px solid #e3e5e8;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.color-chip-shared { background: #f9fafb; color: #1a1a1a; }
.color-chip-live { background: #dcfce7; color: #166534; border-color: #86efac; }
.color-chip-staging { background: #dbeafe; color: #1e40af; border-color: #93c5fd; }
.color-swatch {
  display: inline-block;
  width: 16px; height: 16px;
  border-radius: 3px;
  border: 1px solid rgba(0,0,0,0.15);
  vertical-align: middle;
  /* Checkered backdrop so transparent/alpha colors are visible */
  background-image:
    linear-gradient(45deg, #ddd 25%, transparent 25%),
    linear-gradient(-45deg, #ddd 25%, transparent 25%),
    linear-gradient(45deg, transparent 75%, #ddd 75%),
    linear-gradient(-45deg, transparent 75%, #ddd 75%);
  background-size: 8px 8px;
  background-position: 0 0, 0 4px, 4px -4px, -4px 0;
}
.color-hex {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
}
.color-count {
  color: #6b7280;
  font-size: 11px;
}
.font-swatch {
  display: inline-block;
  width: 12px; height: 12px;
  border-radius: 3px;
  border: 1px solid #e3e5e8;
  vertical-align: middle;
  margin-right: 2px;
}
.font-mismatch-row {
  padding: 12px 0;
  border-bottom: 1px solid #f1f3f5;
}
.font-mismatch-row:last-child { border-bottom: none; }
.font-mismatch-text {
  font-size: 14px;
  margin-bottom: 8px;
  word-break: break-word;
}
.font-mismatch-pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.alt-mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  color: #6b7280;
  margin-top: 4px;
  word-break: break-all;
}
.crop-pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin: 10px 0 12px;
}
.crop-col .side-label { margin-bottom: 4px; }
.crop-wrap {
  background: #fafbfc;
  border: 1px solid #e3e5e8;
  border-radius: 6px;
  padding: 6px;
  overflow: hidden;
  max-height: 220px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.crop-wrap img {
  max-width: 100%;
  max-height: 200px;
  display: block;
}
.crop-missing {
  color: #9ca3af;
  font-style: italic;
  font-size: 12px;
  padding: 20px;
}

@media (max-width: 1100px) {
  .width-compare { grid-template-columns: 1fr; }
  .width-pane { border-right: none; border-bottom: 1px solid #e3e5e8; }
  .changed-pair { grid-template-columns: 1fr; }
  .fonts-grid { grid-template-columns: 1fr; }
  .font-mismatch-pair { grid-template-columns: 1fr; }
  .crop-pair { grid-template-columns: 1fr; }
}
"""

JS = """
const syncToggle = document.getElementById('sync');
const zoomSlider = document.getElementById('zoom');
const zoomLabel = document.getElementById('zoom-label');

// ZOOM — apply uniformly to every screenshot in every width block
function applyZoom() {
  const v = zoomSlider ? zoomSlider.value : 100;
  document.querySelectorAll('.shot-wrap img').forEach(img => {
    img.style.transform = `scale(${v / 100})`;
  });
  if (zoomLabel) zoomLabel.textContent = `${v}%`;
}
if (zoomSlider) {
  zoomSlider.addEventListener('input', applyZoom);
  applyZoom();
}

// SMOOTH-SCROLL via width tabs
document.querySelectorAll('.width-tab').forEach(tab => {
  tab.addEventListener('click', e => {
    e.preventDefault();
    const target = document.querySelector(tab.getAttribute('href'));
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
});

// SYNC SCROLL — within each width block, scroll live and staging together
document.querySelectorAll('.width-block').forEach(block => {
  const wraps = block.querySelectorAll('.shot-wrap');
  if (wraps.length !== 2) return;
  let syncing = false;
  const sync = (src, dest) => {
    if (!syncToggle || !syncToggle.checked) return;
    if (syncing) return;
    syncing = true;
    dest.scrollTop = src.scrollTop;
    dest.scrollLeft = src.scrollLeft;
    requestAnimationFrame(() => { syncing = false; });
  };
  wraps[0].addEventListener('scroll', () => sync(wraps[0], wraps[1]));
  wraps[1].addEventListener('scroll', () => sync(wraps[1], wraps[0]));
});
"""


# ===========================================================================
# Report rendering
# ===========================================================================
def render_width_block(
    width: int,
    live_url: str,
    staging_url: str,
    live_img: str | None,
    staging_img: str | None,
) -> str:
    def pane(label_class: str, label: str, url: str, img_path: str | None) -> str:
        if img_path:
            content = (
                f'<div class="shot-wrap">'
                f'<img src="{html.escape(img_path)}" alt="{label} at {width}px">'
                f'</div>'
            )
        else:
            content = '<div class="shot-missing">— screenshot not available —</div>'
        return f"""
        <div class="width-pane">
          <div class="width-pane-header">
            <span class="{label_class}">{label}</span>
            <a href="{html.escape(url)}" target="_blank">Open ↗</a>
          </div>
          {content}
        </div>
        """

    return f"""
    <div class="width-block" id="width-{width}">
      <div class="width-block-header">
        <span>Viewport width</span>
        <span class="width-label">{width}px</span>
      </div>
      <div class="width-compare">
        {pane("label-live", "LIVE", live_url, live_img)}
        {pane("label-staging", "STAGING", staging_url, staging_img)}
      </div>
    </div>
    """


def render_text_section(text_diff: dict | None) -> str:
    if text_diff is None:
        return ""
    n_changed = len(text_diff["changed"])

    cls = "zero" if n_changed == 0 else "alert"
    summary = f"""
    <div class="summary-grid">
      <div class="stat {cls}"><div class="num">{n_changed}</div><div class="label">Changed text</div></div>
    </div>
    """

    if text_diff["changed"]:
        rows = []
        for live_b, staging_b, score in text_diff["changed"]:
            a_html, b_html = inline_diff_html(live_b["text"], staging_b["text"])
            rows.append(f"""
              <div class="changed-pair">
                <div class="changed-side live">
                  <span class="side-label">Live <span class="tag">{html.escape(live_b['tag'])}</span></span>
                  {a_html}
                </div>
                <div class="changed-side staging">
                  <span class="side-label">Staging <span class="tag">{html.escape(staging_b['tag'])}</span> · {int(score * 100)}% match</span>
                  {b_html}
                </div>
              </div>
            """)
        changed_html = '<div class="card"><h3>Changed text</h3>' + "".join(rows) + '</div>'
    else:
        changed_html = '<div class="card"><h3>Changed text</h3><div class="list-empty">No changed text detected.</div></div>'

    return f"""
    <h2>Content Mismatches</h2>
    {summary}
    {changed_html}
    """


def render_fonts_section(font_diff: dict | None) -> str:
    if font_diff is None:
        return ""

    live_fonts = font_diff.get("live_fonts", [])
    staging_fonts = font_diff.get("staging_fonts", [])
    mismatches = font_diff.get("mismatches", [])

    # Highlight which fonts exist on one side but not the other
    only_live = sorted(set(live_fonts) - {f.lower() for f in staging_fonts}, key=str.lower)
    only_live = [f for f in live_fonts if f.lower() not in {s.lower() for s in staging_fonts}]
    only_staging = [f for f in staging_fonts if f.lower() not in {l.lower() for l in live_fonts}]

    def font_chip(name: str, kind: str) -> str:
        cls = {"shared": "font-chip-shared", "only-live": "font-chip-live",
               "only-staging": "font-chip-staging"}.get(kind, "font-chip-shared")
        return f'<span class="font-chip {cls}" style="font-family: {html.escape(name)}, sans-serif;">{html.escape(name)}</span>'

    def font_list(items: list[str], side_only: list[str]) -> str:
        if not items:
            return '<div class="list-empty">— none detected —</div>'
        chips = []
        side_set = {s.lower() for s in side_only}
        for f in items:
            kind = "only-live" if (f in side_only and side_only is only_live) else \
                   "only-staging" if (f in side_only and side_only is only_staging) else "shared"
            chips.append(font_chip(f, kind))
        return f'<div class="font-chips">{"".join(chips)}</div>'

    fonts_used_html = f"""
    <div class="card">
      <h3>Font families in use</h3>
      <div class="fonts-grid">
        <div>
          <div class="side-label" style="color:#15803d;">LIVE ({len(live_fonts)})</div>
          {font_list(live_fonts, only_live)}
        </div>
        <div>
          <div class="side-label" style="color:#1d4ed8;">STAGING ({len(staging_fonts)})</div>
          {font_list(staging_fonts, only_staging)}
        </div>
      </div>
      <p style="font-size:12px;color:#6b7280;margin:12px 0 0;">
        <span class="font-swatch font-chip-live"></span> only on live &nbsp;
        <span class="font-swatch font-chip-staging"></span> only on staging &nbsp;
        <span class="font-swatch font-chip-shared"></span> shared
      </p>
    </div>
    """

    if mismatches:
        rows = []
        for m in mismatches:
            text_short = m["text"]
            if len(text_short) > 200:
                text_short = text_short[:200] + "…"

            parent_html = ""
            if m.get("parent"):
                parent_html = f'<span class="alt-mono" style="margin-left:8px;">in <code>{html.escape(m["parent"])}</code></span>'

            # Crop screenshots showing where this text lives on the page
            live_crop = m.get("live_crop")
            staging_crop = m.get("staging_crop")
            crops_html = ""
            if live_crop or staging_crop:
                live_img = (
                    f'<img src="{html.escape(live_crop)}" alt="live screenshot of this text">'
                    if live_crop else '<div class="crop-missing">— crop unavailable —</div>'
                )
                staging_img = (
                    f'<img src="{html.escape(staging_crop)}" alt="staging screenshot of this text">'
                    if staging_crop else '<div class="crop-missing">— crop unavailable —</div>'
                )
                crops_html = f"""
                  <div class="crop-pair">
                    <div class="crop-col">
                      <div class="side-label" style="color:#15803d;">LIVE — where on the page</div>
                      <div class="crop-wrap">{live_img}</div>
                    </div>
                    <div class="crop-col">
                      <div class="side-label" style="color:#1d4ed8;">STAGING — where on the page</div>
                      <div class="crop-wrap">{staging_img}</div>
                    </div>
                  </div>
                """

            rows.append(f"""
              <div class="font-mismatch-row">
                <div class="font-mismatch-text">
                  <span class="tag">{html.escape(m['tag'])}</span>{html.escape(text_short)}
                  {parent_html}
                </div>
                {crops_html}
                <div class="font-mismatch-pair">
                  <div class="changed-side live">
                    <span class="side-label">LIVE font</span>
                    <span style="font-family: {html.escape(m['live_font'])}, sans-serif; font-weight:600;">{html.escape(m['live_font'])}</span>
                    <span class="alt-mono" style="margin-left:8px;">{html.escape(m.get('live_size',''))} / weight {html.escape(m.get('live_weight',''))}</span>
                    <div class="alt-mono" title="full computed font-family">{html.escape(m['live_full'])}</div>
                  </div>
                  <div class="changed-side staging">
                    <span class="side-label">STAGING font</span>
                    <span style="font-family: {html.escape(m['staging_font'])}, sans-serif; font-weight:600;">{html.escape(m['staging_font'])}</span>
                    <span class="alt-mono" style="margin-left:8px;">{html.escape(m.get('staging_size',''))} / weight {html.escape(m.get('staging_weight',''))}</span>
                    <div class="alt-mono" title="full computed font-family">{html.escape(m['staging_full'])}</div>
                  </div>
                </div>
              </div>
            """)
        mismatch_html = (
            f'<div class="card"><h3>Per-text font mismatches ({len(mismatches)} found)</h3>'
            f'<p style="font-size:13px;color:#6b7280;margin:0 0 12px;">'
            f'Text that appears on both pages but is rendered with a different '
            f'font-family. The screenshot crops show where each piece of flagged '
            f'text sits on the page.</p>'
            f'{"".join(rows)}</div>'
        )
    else:
        mismatch_html = (
            '<div class="card"><h3>Per-text font mismatches</h3>'
            '<div class="list-empty">No font-family mismatches found across matching text.</div>'
            '</div>'
        )

    return f"""
    <h2>Fonts</h2>
    {fonts_used_html}
    {mismatch_html}
    """


def render_colors_section(color_diff: dict | None) -> str:
    if color_diff is None:
        return ""

    live_colors = color_diff.get("live_colors", [])
    staging_colors = color_diff.get("staging_colors", [])
    mismatches = color_diff.get("mismatches", [])

    live_set = {c for c, _ in live_colors}
    staging_set = {c for c, _ in staging_colors}

    def color_chip(rgb: str, count: int, kind: str) -> str:
        cls = {"shared": "color-chip-shared", "only-live": "color-chip-live",
               "only-staging": "color-chip-staging"}.get(kind, "color-chip-shared")
        hex_val = _color_to_hex(rgb)
        return (
            f'<span class="color-chip {cls}">'
            f'<span class="color-swatch" style="background:{html.escape(rgb)};"></span>'
            f'<span class="color-hex">{html.escape(hex_val)}</span>'
            f'<span class="color-count">×{count}</span>'
            f'</span>'
        )

    def color_list(items: list[tuple[str, int]], other_set: set[str], side_kind: str) -> str:
        if not items:
            return '<div class="list-empty">— none detected —</div>'
        chips = []
        for rgb, count in items:
            kind = side_kind if rgb not in other_set else "shared"
            chips.append(color_chip(rgb, count, kind))
        return f'<div class="color-chips">{"".join(chips)}</div>'

    colors_used_html = f"""
    <div class="card">
      <h3>Text colors in use</h3>
      <div class="fonts-grid">
        <div>
          <div class="side-label" style="color:#15803d;">LIVE ({len(live_colors)})</div>
          {color_list(live_colors, staging_set, "only-live")}
        </div>
        <div>
          <div class="side-label" style="color:#1d4ed8;">STAGING ({len(staging_colors)})</div>
          {color_list(staging_colors, live_set, "only-staging")}
        </div>
      </div>
      <p style="font-size:12px;color:#6b7280;margin:12px 0 0;">
        <span class="font-swatch color-chip-live"></span> only on live &nbsp;
        <span class="font-swatch color-chip-staging"></span> only on staging &nbsp;
        <span class="font-swatch color-chip-shared"></span> shared
      </p>
    </div>
    """

    if mismatches:
        rows = []
        for m in mismatches:
            text_short = m["text"]
            if len(text_short) > 200:
                text_short = text_short[:200] + "…"

            parent_html = ""
            if m.get("parent"):
                parent_html = f'<span class="alt-mono" style="margin-left:8px;">in <code>{html.escape(m["parent"])}</code></span>'

            live_crop = m.get("live_crop")
            staging_crop = m.get("staging_crop")
            crops_html = ""
            if live_crop or staging_crop:
                live_img = (
                    f'<img src="{html.escape(live_crop)}" alt="live screenshot of this text">'
                    if live_crop else '<div class="crop-missing">— crop unavailable —</div>'
                )
                staging_img = (
                    f'<img src="{html.escape(staging_crop)}" alt="staging screenshot of this text">'
                    if staging_crop else '<div class="crop-missing">— crop unavailable —</div>'
                )
                crops_html = f"""
                  <div class="crop-pair">
                    <div class="crop-col">
                      <div class="side-label" style="color:#15803d;">LIVE — where on the page</div>
                      <div class="crop-wrap">{live_img}</div>
                    </div>
                    <div class="crop-col">
                      <div class="side-label" style="color:#1d4ed8;">STAGING — where on the page</div>
                      <div class="crop-wrap">{staging_img}</div>
                    </div>
                  </div>
                """

            # Background row only if at least one side has a non-transparent bg
            bg_html = ""
            if m.get("live_bg") or m.get("staging_bg"):
                live_bg_chip = (
                    f'<span class="color-swatch" style="background:{html.escape(m["live_color_raw"] if not m["live_bg"] else m["live_bg"])};"></span>'
                    f'<span class="color-hex">{html.escape(m["live_bg"] or "transparent")}</span>'
                ) if m.get("live_bg") else '<span class="alt-mono">transparent</span>'
                staging_bg_chip = (
                    f'<span class="color-swatch" style="background:{html.escape(m["staging_bg"])};"></span>'
                    f'<span class="color-hex">{html.escape(m["staging_bg"])}</span>'
                ) if m.get("staging_bg") else '<span class="alt-mono">transparent</span>'
                bg_html = f"""
                  <div class="alt-mono" style="margin-top:6px;">
                    Background: LIVE {live_bg_chip} &nbsp;·&nbsp; STAGING {staging_bg_chip}
                  </div>
                """

            rows.append(f"""
              <div class="font-mismatch-row">
                <div class="font-mismatch-text">
                  <span class="tag">{html.escape(m['tag'])}</span>{html.escape(text_short)}
                  {parent_html}
                </div>
                {crops_html}
                <div class="font-mismatch-pair">
                  <div class="changed-side live">
                    <span class="side-label">LIVE color</span>
                    <span class="color-swatch" style="background:{html.escape(m['live_color_raw'])};"></span>
                    <span class="color-hex" style="font-weight:600;">{html.escape(m['live_color'])}</span>
                    <div class="alt-mono">{html.escape(m['live_color_rgb'])}</div>
                  </div>
                  <div class="changed-side staging">
                    <span class="side-label">STAGING color</span>
                    <span class="color-swatch" style="background:{html.escape(m['staging_color_raw'])};"></span>
                    <span class="color-hex" style="font-weight:600;">{html.escape(m['staging_color'])}</span>
                    <div class="alt-mono">{html.escape(m['staging_color_rgb'])}</div>
                  </div>
                </div>
                {bg_html}
              </div>
            """)
        mismatch_html = (
            f'<div class="card"><h3>Per-text color mismatches ({len(mismatches)} found)</h3>'
            f'<p style="font-size:13px;color:#6b7280;margin:0 0 12px;">'
            f'Text that appears on both pages but is rendered with a different '
            f'color. Crops show where on the page each piece of flagged text sits.</p>'
            f'{"".join(rows)}</div>'
        )
    else:
        mismatch_html = (
            '<div class="card"><h3>Per-text color mismatches</h3>'
            '<div class="list-empty">No color mismatches found across matching text.</div>'
            '</div>'
        )

    return f"""
    <h2>Colors</h2>
    {colors_used_html}
    {mismatch_html}
    """


def render_report(
    live_url: str,
    staging_url: str,
    widths: list[int],
    live_shots: dict[int, str],
    staging_shots: dict[int, str],
    text_diff: dict | None,
    font_diff: dict | None,
    color_diff: dict | None,
    live_err: str,
    staging_err: str,
) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    error_blocks = []
    if live_err:
        error_blocks.append(f'<div class="error-box"><strong>Live:</strong> {html.escape(live_err)}</div>')
    if staging_err:
        error_blocks.append(f'<div class="error-box"><strong>Staging:</strong> {html.escape(staging_err)}</div>')
    error_html = "".join(error_blocks)

    tabs_html = "".join(
        f'<a class="width-tab" href="#width-{w}">{w}px</a>' for w in widths
    )

    blocks_html = "".join(
        render_width_block(
            width=w,
            live_url=live_url,
            staging_url=staging_url,
            live_img=live_shots.get(w),
            staging_img=staging_shots.get(w),
        )
        for w in widths
    )

    text_html = render_text_section(text_diff)
    fonts_html = render_fonts_section(font_diff)
    colors_html = render_colors_section(color_diff)

    body = f"""
    <h1>QA Responsive Comparison</h1>
    <p class="timestamp">Generated {timestamp}</p>

    <dl class="meta">
      <dt>Live URL</dt><dd><a href="{html.escape(live_url)}" target="_blank">{html.escape(live_url)}</a></dd>
      <dt>Staging URL</dt><dd><a href="{html.escape(staging_url)}" target="_blank">{html.escape(staging_url)}</a></dd>
      <dt>Widths</dt><dd>{", ".join(f"{w}px" for w in widths)}</dd>
    </dl>

    {error_html}

    <div class="controls">
      <label><input type="checkbox" id="sync" checked> Sync scroll</label>
      <label>Zoom: <input type="range" id="zoom" min="25" max="200" value="100"> <span id="zoom-label" class="zoom-val">100%</span></label>
    </div>

    <h2>Screenshots by viewport width</h2>
    <div class="width-tabs">{tabs_html}</div>
    {blocks_html}

    {text_html}
    {fonts_html}
    {colors_html}
    """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>QA Responsive Comparison</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="container">{body}</div>
  <script>{JS}</script>
</body>
</html>
"""


# ===========================================================================
# Main
# ===========================================================================
def main() -> None:
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(exist_ok=True)

    # Use the largest width for text extraction (most content visible, no
    # mobile-only collapsed nav, etc.)
    text_extraction_width = max(VIEWPORT_WIDTHS)

    print(f"Capturing LIVE at {len(VIEWPORT_WIDTHS)} widths: {VIEWPORT_WIDTHS}")
    live_shots, live_texts, live_err = capture_url_at_widths(
        LIVE_URL, VIEWPORT_WIDTHS, out_dir, "live",
        extract_text_at_width=text_extraction_width,
    )

    print(f"\nCapturing STAGING at {len(VIEWPORT_WIDTHS)} widths: {VIEWPORT_WIDTHS}")
    staging_shots, staging_texts, staging_err = capture_url_at_widths(
        STAGING_URL, VIEWPORT_WIDTHS, out_dir, "staging",
        extract_text_at_width=text_extraction_width,
    )

    text_diff = None
    font_diff = None
    color_diff = None
    if live_texts is not None and staging_texts is not None:
        print("\nComparing text content...")
        text_diff = diff_texts(live_texts, staging_texts)
        print(f"  -> {len(text_diff['changed'])} changed text pair(s)")
        print("Comparing fonts...")
        font_diff = diff_fonts(live_texts, staging_texts)
        print(f"  -> {len(font_diff['live_fonts'])} live fonts, "
              f"{len(font_diff['staging_fonts'])} staging fonts, "
              f"{len(font_diff['mismatches'])} per-text mismatch(es)")
        print("Comparing colors...")
        color_diff = diff_colors(live_texts, staging_texts)
        print(f"  -> {len(color_diff['live_colors'])} live colors, "
              f"{len(color_diff['staging_colors'])} staging colors, "
              f"{len(color_diff['mismatches'])} per-text mismatch(es)")

        # Crop screenshots for both font AND color mismatches
        live_full_shot = out_dir / f"live_{text_extraction_width}.png"
        staging_full_shot = out_dir / f"staging_{text_extraction_width}.png"
        if live_full_shot.exists() and staging_full_shot.exists():
            crops_dir = out_dir / "font_crops"
            crops_dir.mkdir(exist_ok=True)

            if font_diff["mismatches"]:
                print("Cropping font-mismatch regions...")
                for i, m in enumerate(font_diff["mismatches"]):
                    lx, ly, lw, lh = m["live_pos"]
                    sx, sy, sw, sh = m["staging_pos"]
                    live_crop_path = crops_dir / f"font_live_{i}.png"
                    stg_crop_path = crops_dir / f"font_staging_{i}.png"
                    if crop_text_region(live_full_shot, live_crop_path, lx, ly, lw, lh):
                        m["live_crop"] = f"{OUTPUT_DIR}/font_crops/font_live_{i}.png"
                    if crop_text_region(staging_full_shot, stg_crop_path, sx, sy, sw, sh):
                        m["staging_crop"] = f"{OUTPUT_DIR}/font_crops/font_staging_{i}.png"

            if color_diff["mismatches"]:
                print("Cropping color-mismatch regions...")
                for i, m in enumerate(color_diff["mismatches"]):
                    lx, ly, lw, lh = m["live_pos"]
                    sx, sy, sw, sh = m["staging_pos"]
                    live_crop_path = crops_dir / f"color_live_{i}.png"
                    stg_crop_path = crops_dir / f"color_staging_{i}.png"
                    if crop_text_region(live_full_shot, live_crop_path, lx, ly, lw, lh):
                        m["live_crop"] = f"{OUTPUT_DIR}/font_crops/color_live_{i}.png"
                    if crop_text_region(staging_full_shot, stg_crop_path, sx, sy, sw, sh):
                        m["staging_crop"] = f"{OUTPUT_DIR}/font_crops/color_staging_{i}.png"

    print("\nGenerating report...")
    report = render_report(
        live_url=LIVE_URL,
        staging_url=STAGING_URL,
        widths=VIEWPORT_WIDTHS,
        live_shots=live_shots,
        staging_shots=staging_shots,
        text_diff=text_diff,
        font_diff=font_diff,
        color_diff=color_diff,
        live_err=live_err,
        staging_err=staging_err,
    )

    Path(OUTPUT_FILE).write_text(report, encoding="utf-8")
    print(f"\nReport written to: {OUTPUT_FILE}")
    print(f"Screenshots in:    {OUTPUT_DIR}/")
    print("Open qa_report.html in your browser.")


if __name__ == "__main__":
    main()