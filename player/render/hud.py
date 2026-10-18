"""First-person item HUD driven by the engine's compiled item definitions."""
from PIL import Image, ImageDraw, ImageFont


def paint(canvas, state, images):
    hud = state.get('game_hud')
    if not hud or hud['cinematic']:
        return
    width, height = canvas.size
    draw = ImageDraw.Draw(canvas)
    held = hud['held']
    if held and held.hud_sprite and not hud['overhead']:
        _, sprite = images.frame(held.hud_sprite)
        h = max(1, int(held.hud_height * height / 600))
        w = max(1, round(sprite.width*h/max(1, sprite.height)))
        x = (width-w)//2 if held.hud_align == 'center' else (20 if held.hud_align == 'left' else width-w-20)
        canvas.alpha_composite(sprite.resize((w, h), Image.Resampling.LANCZOS), (x, height-h))
        if hud['flash'] and held.weapon and held.weapon.muzzle_flash:
            _, flash = images.frame(held.weapon.muzzle_flash)
            canvas.alpha_composite(flash.resize((w, h), Image.Resampling.LANCZOS), (x, height-h))
    font = ImageFont.load_default(size=max(22, round(height*.085)))
    draw.text((5, height-5), str(hud['health']), font=font, anchor='ld', fill=(240, 145, 40), stroke_width=1, stroke_fill='black')
    stats = []
    if held and held.weapon and held.weapon.fires:
        stats.append('∞' if held.weapon.ammo_per_shot == 0 else str(hud['ammo']))
    if hud['armor']:
        stats.append(f"Armor {hud['armor']}")
    draw.text((width*.18, height-8), '  '.join(stats), anchor='ld', font=ImageFont.load_default(size=max(18, height//20)), fill='white', stroke_width=1, stroke_fill='black')
    if held and held.weapon and held.weapon.fires and not hud['overhead']:
        x, y = width//2, height//2
        color = (255, 185, 75) if hud['ready'] else (160, 160, 160)
        for dx,dy in ((-1,0),(1,0),(0,-1),(0,1)):
            draw.line((x+dx*4,y+dy*4,x+dx*10,y+dy*10), fill=color, width=2)
    if hud['prompt']:
        draw.text((width*.5,height*.8), hud['prompt'], font=ImageFont.load_default(size=max(14,height//24)), anchor='mm', fill='white', stroke_width=1, stroke_fill='black')
