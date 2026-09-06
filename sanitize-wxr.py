#!/usr/bin/env python3
"""Make a raw WordPress export usable as portable demo data.

The export always comes out of the Local install at http://axiom-blocks.local, so
every URL in it points at a host that only resolves on the machine that produced
it. Importing that file anywhere else, WordPress Playground in particular, gives
you dead nav links, missing screenshots, and no webfonts.

Run this on the file straight after exporting:

    python sanitize-wxr.py axiom-demo.wxr

It is idempotent, so running it twice does nothing the second time.

What it does:

  * Font faces get pointed back at the Google Fonts CDN. The font library
    downloaded the woff2 files into wp-content/uploads/fonts/ and kept the
    original CDN filenames, so the CDN URL is just the family directory plus
    that filename.
  * The two screenshots get pointed at the copies published on ps.w.org with
    the plugin assets. Root-relative paths were tried once and do not work:
    Playground resolves them against the current page, not the site root.
  * Internal links stay absolute, under a single placeholder host that matches
    wp:base_site_url. Playground's importWxr step rewrites URLs on that host to
    the live site URL, which is how a link picks up the /scope:.../ prefix the
    site is served under. Root-relative links were tried and do not work: there
    is nothing for the importer to match, so /blog/ resolves against the origin
    and lands on playground.wordpress.net/blog/ with the scope dropped.
  * Links that are only a fragment become bare anchors. Those already work
    anywhere and should not be tied to a host.
  * Postmeta that names local files is dropped. WordPress rewrites those itself
    when it sideloads the remote copy, and a stale value beats it to the punch.
"""

import argparse
import json
import re
import sys

LOCAL = 'http://axiom-blocks.local'
PUBLIC_HOST = 'https://wpaxiom.com'

# Directory each family lives under on the CDN. The version segment is pinned by
# the export, so bump these if a future export pulls a newer release.
GOOGLE_FONT_DIR = {
    '"Space Grotesk"': 'spacegrotesk/v22',
    '"DM Sans"': 'dmsans/v17',
}

# Screenshots ship with the plugin's WordPress.org assets under different names
# than they carry locally.
IMAGE_MAP = {
    '/wp-content/uploads/sable/hero.png':
        'https://ps.w.org/axiom-blocks/assets/demo-hero.png',
    '/wp-content/uploads/sable/deep-dive.png':
        'https://ps.w.org/axiom-blocks/assets/demo-deep-dive.png',
}

# Values naming a path on the exporting machine. WordPress sets these itself
# from the file it actually sideloads.
DROP_META = ('_wp_attached_file', '_wp_attachment_metadata', '_wp_font_face_file')

ITEM_RE = re.compile(r'<item>.*?</item>', re.S)
CONTENT_RE = re.compile(r'<content:encoded><!\[CDATA\[(.*?)\]\]></content:encoded>', re.S)


def escape_json(url):
    """Match how a URL is written inside a JSON payload in a CDATA block."""
    return url.replace('/', '\\/')


def build_font_map(text):
    """Map each local font filename to its Google Fonts CDN URL.

    The family comes from the font face's own settings rather than from the
    filename, so a family added later only needs a GOOGLE_FONT_DIR entry.
    """
    mapping = {}
    unknown = set()
    for item in ITEM_RE.findall(text):
        if 'CDATA[wp_font_face]' not in item:
            continue
        body = CONTENT_RE.search(item)
        if not body:
            continue
        try:
            settings = json.loads(body.group(1))
        except ValueError:
            continue
        src = settings.get('src', '')
        family = settings.get('fontFamily', '')
        if not isinstance(src, str) or not src.endswith('.woff2'):
            continue
        directory = GOOGLE_FONT_DIR.get(family)
        if directory is None:
            unknown.add(family)
            continue
        filename = src.rsplit('/', 1)[-1]
        mapping[filename] = f'https://fonts.gstatic.com/s/{directory}/{filename}'
    return mapping, unknown


def strip_anchor_host(text):
    """Reduce a local URL that is only a fragment to a bare anchor.

    These point at a section of the page they already sit on, so hanging a host
    off them buys nothing and sends the visitor to the front page instead.
    """
    return re.subn(re.escape(LOCAL) + r'/(#[^"\'<)\s\\]*)', r'\1', text)


def swap_host(text):
    """Move every remaining local URL onto the placeholder host.

    This covers link, guid, base_site_url and the links inside block markup in
    one pass, which is what keeps them consistent. importWxr matches content
    URLs against base_site_url, so a link only survives the import if it is
    absolute and sits on the same host.
    """
    text, plain = re.subn(re.escape(LOCAL), PUBLIC_HOST, text)
    text, escaped = re.subn(re.escape(escape_json(LOCAL)), escape_json(PUBLIC_HOST), text)
    return text, plain + escaped


def drop_meta(text, key):
    pattern = re.compile(
        r'[ \t]*<wp:postmeta>\s*<wp:meta_key><!\[CDATA\[' + re.escape(key) +
        r'\]\]></wp:meta_key>.*?</wp:postmeta>\n?', re.S)
    return pattern.subn('', text)


def sanitize(text):
    report = []

    font_map, unknown = build_font_map(text)
    if unknown:
        raise SystemExit(
            'No CDN directory known for font families: ' + ', '.join(sorted(unknown)) +
            '\nAdd them to GOOGLE_FONT_DIR before running.')

    fonts = 0
    for filename, cdn_url in font_map.items():
        local_url = f'{LOCAL}/wp-content/uploads/fonts/{filename}'
        text, n = re.subn(re.escape(local_url), cdn_url, text)
        fonts += n
        text, n = re.subn(re.escape(escape_json(local_url)), escape_json(cdn_url), text)
        fonts += n
    report.append(f'font URLs rewritten to the Google CDN: {fonts}')

    images = 0
    for path, public_url in IMAGE_MAP.items():
        text, n = re.subn(re.escape(LOCAL + path), public_url, text)
        images += n
        text, n = re.subn(re.escape(escape_json(LOCAL + path)), escape_json(public_url), text)
        images += n
    report.append(f'image URLs rewritten to ps.w.org: {images}')

    text, anchors = strip_anchor_host(text)
    report.append(f'fragment-only links reduced to bare anchors: {anchors}')

    # Runs after the font and image passes so those keep their own hosts rather
    # than being handed the placeholder.
    text, swapped = swap_host(text)
    report.append(f'URLs moved onto {PUBLIC_HOST}: {swapped}')

    dropped = 0
    for key in DROP_META:
        text, n = drop_meta(text, key)
        dropped += n
    report.append(f'postmeta entries naming local files dropped: {dropped}')

    leftover = text.count('axiom-blocks.local')
    if leftover:
        report.append(f'WARNING: {leftover} references to axiom-blocks.local remain')

    return text, report


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('path', nargs='?', default='axiom-demo.wxr')
    args = parser.parse_args()

    with open(args.path, encoding='utf-8') as handle:
        original = handle.read()

    cleaned, report = sanitize(original)

    for line in report:
        print(line)

    if cleaned == original:
        print('\nAlready clean, file left untouched.')
        return 0

    with open(args.path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(cleaned)
    print(f'\nWrote {args.path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
