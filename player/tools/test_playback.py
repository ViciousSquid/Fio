"""Extend native picker/movement acceptance with the desktop diagnostic world.

Run as python -m player.tools.test_playback DEBUG.apk _ANDROID_PLAYBACK.fiopak.
Captures real Android touch firing, health/death reports and visible gun HUD.
"""
import argparse
import json
import subprocess
import time

from .test_device import DeviceTest, ready_for_movement


class PlaybackTest(DeviceTest):
    def validate_world(self, pid, world, *, picker_selection='automated'):
        deadline = time.monotonic()+120*self.timeout_scale
        while time.monotonic()<deadline:
            reports=self.reports(pid)
            if ready_for_movement(reports):
                break
            time.sleep(2)
        else:
            raise RuntimeError('Diagnostic world did not settle')
        before=reports[-1]
        assert before['weapon']=='gun1' and before['weapon_hud'].endswith('gun1HUD.png'), before
        assert before['monsters'][0]['health']==100, before
        assert before['render']['lights']>=17 and before['render']['shadow_lights']>=1, before
        assert before['render']['effect_batches']>=2 and not before['missing_images'], before
        self.dismiss_fullscreen_hint(self.nodes())
        width,height=self.screenshot('armed-hud-effects.png')
        x,y=int(width*.90),int(height*.82)
        # A held real touch lets a slow emulator deliver several firing frames.
        hold=subprocess.Popen([*self.command,'shell','input','touchscreen','swipe',
                               str(x),str(y),str(x),str(y),str(int(3000*min(self.timeout_scale,5)))],
                              stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            time.sleep(3)
            self.screenshot('firing-hud.png')
            if hold.wait(timeout=90*self.timeout_scale):
                raise RuntimeError('Android fire gesture failed')
        finally:
            if hold.poll() is None:
                hold.terminate()
        deadline=time.monotonic()+30*self.timeout_scale
        after=[]
        while time.monotonic()<deadline:
            after=self.reports(pid)
            if after and after[-1]['monsters'][0]['dead']:
                break
            time.sleep(2)
        assert after and after[-1]['monsters'][0]['dead'], 'Point-blank firing did not kill the monster'
        self.screenshot('point-blank-kill.png')
        result={'passed':True,'before':before,'after':after[-1],
                'checks':['real pickup + weapon HUD path','17 engine lights including effects',
                          'active point-light shadow','two animated effect batches',
                          'real Android fire touch kills point-blank monster','no missing world images']}
        (self.output/'playback-result.json').write_text(json.dumps(result,indent=2))
        # Keep the existing forward/reverse and imported-world hash checks.
        super().validate_world(pid,world,picker_selection=picker_selection)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('apk');parser.add_argument('world')
    parser.add_argument('--adb',default='adb');parser.add_argument('--serial')
    parser.add_argument('--output',default='fio-playback-results')
    parser.add_argument('--timeout-scale',type=float,default=1)
    parser.add_argument('--skip-install',action='store_true')
    args=parser.parse_args()
    test=PlaybackTest(args.adb,args.serial,args.output,args.timeout_scale)
    try:
        test.run(args.apk,args.world,skip_install=args.skip_install)
    except Exception as exc:
        result=json.loads((test.output/'result.json').read_text())
        result.update(passed=False,status='failed',error=str(exc))
        (test.output/'result.json').write_text(json.dumps(result,indent=2))
        if test.pid:
            test.reports(test.pid)
        raise


if __name__=='__main__':
    main()
