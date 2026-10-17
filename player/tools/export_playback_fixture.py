"""Export a small diagnostic world with the actual desktop package exporter.

This supplements _SHOWCASE movement acceptance with an immediate weapon pickup,
point-blank monster, authored UVs, >8 lights, a shadow caster and animated effects.
No test-only commands or executable content are embedded in the world.
"""
import argparse
import json
from pathlib import Path
import shutil
import tempfile

from PIL import Image, ImageDraw


def export(output):
    from editor.package_exporter import PackageExporter
    project = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='fio-playback-export-') as working:
        root = Path(working)
        assets = root/'assets'
        (assets/'textures').mkdir(parents=True)
        (root/'maps').mkdir()
        for category in ('textures','sprites','sounds'):
            shutil.copy2(project/'assets'/f'{category}.fiopak', assets/f'{category}.fiopak')
        image = Image.new('RGB',(64,64),'white')
        draw = ImageDraw.Draw(image)
        for box,color in [((0,0,31,31),'red'),((32,0,63,31),'green'),
                          ((0,32,31,63),'blue'),((32,32,63,63),'yellow')]:
            draw.rectangle(box,fill=color)
        draw.text((2,2),'UP',fill='white')
        image.save(assets/'textures'/'parity-grid.png')
        textures = {face:'parity-grid.png' for face in ('north','south','east','west','top','down')}
        data = {'version':3, 'brushes':[
            {'pos':[0,-16,-250], 'size':[1200,32,1200], 'textures':textures,
             'uv_natural':{'top':True}},
            {'pos':[0,128,-500], 'size':[900,256,16], 'textures':textures,
             'uv_angle':{'south':90}, 'uv_scale':{'south':[3,1]}, 'uv_shift':{'south':[.25,.5]}},
            {'pos':[70,50,-260], 'size':[30,100,40], 'textures':textures}],
            'things':[
            {'type':'PlayerStart','pos':[0,50,0], 'properties':{'angle':90}},
            {'type':'Prop','pos':[0,32,0], 'properties':{'collect_enabled':True,'collect_type':'item','collect_item':'gun1'}},
            {'type':'Monster','pos':[-40,0,-15], 'properties':{'name':'PointBlank','health':100,
             'awake':False,'wake_on_sight':False,'damage':0,'sprite_width':96,'sprite_height':128}},
            *[{'type':'Light','pos':[1000+i*10,1000,1000], 'properties':{'radius':1}} for i in range(14)],
            {'type':'Light','pos':[-100,200,-170], 'properties':{'radius':800,'intensity':2,'casts_shadows':True}},
            {'type':'Effect','pos':[150,110,-300], 'properties':{'effect_type':'FIRE','width':64,'height':128}},
            {'type':'Effect','pos':[-150,110,-300], 'properties':{'effect_type':'ORB','width':60,'height':60}}]}
        path = root/'maps'/'_ANDROID_PLAYBACK.json'
        path.write_text(json.dumps(data))
        success, errors = PackageExporter(None,str(root)).export(str(output),{'title':'Android playback diagnostic'},str(path))
        if not success or errors:
            raise RuntimeError(f'Desktop export failed: {errors}')
        print(f'Desktop exporter created {output}')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    export(parser.parse_args().output)
