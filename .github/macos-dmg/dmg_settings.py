# dmgbuild settings for the Fio installer window: Fio.app on the left, a
# shortcut to /Applications on the right, an arrow between them.
#
#   dmgbuild -s .github/macos-dmg/dmg_settings.py \
#       -D app=build/Fio.app -D background=build/dmg-background.tiff \
#       "Fio" build/Fio-macOS.dmg
#
# dmgbuild runs this file with `defines` holding the -D values.
import os.path

app = defines['app']                     # noqa: F821 (provided by dmgbuild)
app_name = os.path.basename(app)

files = [app]
symlinks = {'Applications': '/Applications'}

format = 'UDZO'          # zlib: opens on every macOS version Fio supports
filesystem = 'HFS+'

background = defines['background']       # noqa: F821
window_rect = ((200, 120), (540, 320))   # matches make_background.WINDOW
icon_size = 96
text_size = 12
icon_locations = {
    app_name: (135, 135),                # matches make_background.APP_AT
    'Applications': (405, 135),          # matches make_background.APPS_AT
}

show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
default_view = 'icon-view'
arrange_by = None
