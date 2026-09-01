"""Генерирует PWA-иконки (icon-192.png, icon-512.png)."""
from PIL import Image, ImageDraw

def make_icon(size, path):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size
    # закруглённый фон градиент-подобный (просто два цвета)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=(108, 92, 231, 255))
    d.rounded_rectangle([0, int(s * 0.55), s - 1, s - 1], radius=int(s * 0.22), fill=(0, 206, 201, 255))
    # символ календаря: белая плашка
    m = int(s * 0.10)
    tw = int(s * 0.55)
    th = int(s * 0.44)
    tx = (s - tw) // 2
    ty = int(s * 0.16)
    d.rounded_rectangle([tx, ty + int(s*0.10), tx + tw, ty + th], radius=int(s*0.06), fill=(255, 255, 255, 255))
    # верхняя полоска-карабин
    d.rounded_rectangle([tx + int(s*0.06), ty, tx + int(s*0.18), ty + int(s*0.12)], radius=2, fill=(255,255,255,255))
    d.rounded_rectangle([tx + tw - int(s*0.18), ty, tx + tw - int(s*0.06), ty + int(s*0.12)], radius=2, fill=(255,255,255,255))
    # точки-дата
    dot = int(s * 0.05)
    for i in range(3):
        for j in range(2):
            x = tx + int(s*0.12) + i * int(s*0.18)
            y = ty + int(s*0.20) + j * int(s*0.16)
            d.ellipse([x, y, x + dot, y + dot], fill=(108, 92, 231, 255))
        d.ellipse([x + int(s*0.18) + int(s*0.02), y - int(s*0.16), x + int(s*0.18) + int(s*0.02) + dot, y], fill=(108,92,231,255))
    img.save(path)

make_icon(192, "icon-192.png")
make_icon(512, "icon-512.png")
print("icons saved")
