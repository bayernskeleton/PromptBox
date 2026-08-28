"""Generate multi-size icons and .ico from the chosen logo."""
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / 'logos'
SRC = OUT_DIR / 'Minimalist_flat_logo__app_icon_2026-07-05T10-39-17.png'

img = Image.open(SRC).convert('RGBA')
w, h = img.size
print(f'Original size: {w}x{h}')

# Crop bottom 10% to remove watermark
img = img.crop((0, 0, w, int(h * 0.9)))
print(f'After crop:    {img.size[0]}x{img.size[1]}')

# Trim any transparent/near-white border to make icon fill the frame
# (We keep as-is since the logo is centered on beige)

sizes = [16, 32, 48, 64, 128, 256]
for size in sizes:
    resized = img.resize((size, size), Image.LANCZOS)
    out = OUT_DIR / f'icon_{size}.png'
    resized.save(out)
    print(f'  wrote {out}')

# Save a Windows .ico with all sizes embedded
ico_path = OUT_DIR / 'promptbox.ico'
img_ico = img.resize((256, 256), Image.LANCZOS)
img_ico.save(
    ico_path,
    format='ICO',
    sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print(f'  wrote {ico_path}')

# Also save a clean 512 png for README hero
hero = img.resize((512, 512), Image.LANCZOS)
hero_path = OUT_DIR / 'promptbox_logo.png'
hero.save(hero_path)
print(f'  wrote {hero_path}')

print('done.')
