"""Deterministic original pipeline mark, shared by the EXE and Portal."""
from pathlib import Path
from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
svg='''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><rect x="8" y="8" width="240" height="240" rx="52" fill="#102d46"/><path d="M68 180V76h75q40 0 40 38t-40 38H104" fill="none" stroke="#f7fbff" stroke-width="26" stroke-linecap="round" stroke-linejoin="round"/><path d="M104 183h68" fill="none" stroke="#39c7af" stroke-width="20" stroke-linecap="round"/><circle cx="68" cy="76" r="16" fill="#39c7af"/><circle cx="184" cy="183" r="16" fill="#f4b75d"/></svg>'''

def main():
    target=ROOT/'app/static'
    (target/'launcher.svg').write_text(svg,encoding='utf-8')
    image=Image.new('RGBA',(1024,1024),(0,0,0,0));draw=ImageDraw.Draw(image)
    def box(v):return tuple(x*4 for x in v)
    draw.rounded_rectangle(box((8,8,248,248)),radius=208,fill='#102d46')
    draw.line([(272,720),(272,304),(572,304)],fill='#f7fbff',width=104)
    draw.arc(box((105,76,183,152)),270,450,fill='#f7fbff',width=104)
    draw.line([(572,608),(416,608)],fill='#f7fbff',width=104)
    draw.line([(416,732),(688,732)],fill='#39c7af',width=80)
    draw.ellipse(box((52,60,84,92)),fill='#39c7af')
    draw.ellipse(box((168,167,200,199)),fill='#f4b75d')
    image.save(target/'launcher.ico',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
    image.resize((256,256),Image.Resampling.LANCZOS).save(target/'launcher.png')

if __name__=='__main__':main()
