"""Original pixel-art OneClick price/effectiveness EXE icon.

Integer-grid drawing, hand-tuned 16/24px variants, explicit ICO frames.
Pillow is a developer dependency; client builds consume the finished assets.
"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 24, 32, 48, 64, 128, 256)
COLORS = dict(outline='#091726', tile='#142d48', rim='#294d69',
              gold='#ffc34b', light='#ffe29a', bronze='#b66a25',
              teal='#38dbc6', dark='#167e89', white='#f5fbff')


def artwork(size=32):
    if size not in (16,24,32):
        return artwork(24 if size == 48 else 32).resize((size,size),Image.Resampling.NEAREST)
    image = Image.new('RGBA',(size,size))
    draw = ImageDraw.Draw(image)
    def rect(box,color): draw.rectangle(box,fill=COLORS[color])
    def poly(points,color): draw.polygon(points,fill=COLORS[color])
    if size == 16:
        poly([(2,0),(13,0),(13,1),(15,1),(15,14),(13,14),(13,15),
              (2,15),(2,14),(0,14),(0,1),(2,1)],'outline')
        rect((2,1,13,14),'tile');rect((1,2,14,13),'tile')
        rect((2,4,7,6),'gold');rect((3,3,6,7),'gold')
        rect((4,4,5,6),'light')
        rect((10,3,12,3),'teal');rect((9,4,9,5),'teal')
        rect((13,4,13,6),'teal');rect((11,6,12,6),'teal')
        poly([(7,7),(7,13),(9,12),(10,14),(12,13),(10,11),(13,11)],'outline')
        poly([(8,8),(8,12),(9,11),(11,13),(12,12),(10,10),(12,10)],'white')
        return image
    if size == 24:
        poly([(3,0),(20,0),(20,1),(22,1),(22,3),(23,3),(23,20),
              (22,20),(22,22),(20,22),(20,23),(3,23),(3,22),
              (1,22),(1,20),(0,20),(0,3),(1,3),(1,1),(3,1)],'outline')
        rect((3,2,20,21),'tile');rect((2,3,21,20),'tile');rect((4,2,19,2),'rim')
        rect((4,5,10,10),'bronze');rect((3,6,11,9),'bronze')
        rect((4,5,10,9),'gold');rect((5,4,9,10),'gold')
        rect((5,4,9,4),'light');rect((6,6,8,6),'bronze')
        rect((7,5,7,9),'bronze');rect((6,8,8,8),'bronze')
        rect((15,4,18,4),'teal');rect((14,5,14,5),'teal')
        rect((13,6,13,9),'teal');rect((19,5,19,5),'teal')
        rect((20,6,20,9),'dark');rect((17,7,18,8),'white')
        rect((16,9,17,10),'teal');rect((14,11,19,11),'dark')
        rect((9,12,9,13),'teal');rect((11,10,11,11),'teal')
        poly([(12,12),(12,21),(15,19),(17,22),(20,20),(18,17),(21,17)],'outline')
        poly([(13,13),(13,19),(15,17),(18,21),(19,20),(17,16),(19,16)],'white')
        rect((5,17,6,18),'gold');rect((8,17,9,18),'teal')
        return image
    # Transparent stepped corners and a solid tile keep desktop contrast.
    poly([(5,1),(26,1),(26,2),(29,2),(29,5),(30,5),(30,26),
          (29,26),(29,29),(26,29),(26,30),(5,30),(5,29),(2,29),
          (2,26),(1,26),(1,5),(2,5),(2,2),(5,2)],'outline')
    rect((5,3,26,28),'tile');rect((3,5,28,26),'tile');rect((4,4,27,27),'tile')
    rect((6,3,25,3),'rim');rect((3,6,3,24),'rim')
    # Price coin with a pixel yuan sign.
    rect((6,8,14,14),'bronze');rect((8,7,12,16),'bronze')
    rect((5,9,15,13),'bronze');rect((7,7,13,14),'gold')
    rect((6,9,14,12),'gold');rect((8,6,12,14),'gold')
    rect((8,6,12,6),'light');rect((6,9,6,11),'light')
    rect((8,8,8,8),'bronze');rect((12,8,12,8),'bronze')
    rect((9,9,9,9),'bronze');rect((11,9,11,9),'bronze')
    rect((10,10,10,13),'bronze');rect((8,10,12,10),'bronze')
    rect((8,12,12,12),'bronze')
    # Effectiveness gauge with high-range needle.
    rect((21,6,24,6),'teal');rect((19,7,20,7),'teal')
    rect((18,8,18,9),'teal');rect((17,10,17,13),'teal')
    rect((25,7,25,7),'teal');rect((26,8,26,9),'teal')
    rect((27,10,27,13),'dark');rect((19,15,25,15),'dark')
    rect((23,10,24,11),'white');rect((22,11,23,12),'white')
    rect((21,12,22,13),'teal')
    # OneClick cursor, activation rays and a short connected pipeline.
    rect((12,17,13,17),'teal');rect((15,14,15,15),'teal')
    poly([(16,16),(16,28),(20,25),(23,30),(27,28),(24,23),(28,23)],'outline')
    poly([(17,18),(17,26),(20,23),(24,28),(25,27),(22,22),(25,22)],'white')
    rect((6,23,13,23),'dark');rect((6,22,8,24),'gold');rect((11,22,13,24),'teal')
    return image


def main():
    target = ROOT/'app/static'
    frames = {s:artwork(s) for s in SIZES}
    frames[256].save(target/'launcher.ico',format='ICO',sizes=[(s,s) for s in SIZES],
                     append_images=[frames[s] for s in SIZES if s != 256])
    frames[256].save(target/'launcher.png')
    # Existing Portal SVG reference shares the same unfiltered pixel geometry.
    rectangles = []
    for y in range(32):
        for x in range(32):
            r,g,b,a = frames[32].getpixel((x,y))
            if a:rectangles.append('<rect x="%d" y="%d" width="1" height="1" fill="#%02x%02x%02x"/>'%(x,y,r,g,b))
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" '
           'shape-rendering="crispEdges" role="img" aria-label="OneClick 价格与效能">'+''.join(rectangles)+'</svg>\n')
    (target/'launcher.svg').write_text(svg,encoding='utf-8')


if __name__ == '__main__':main()
