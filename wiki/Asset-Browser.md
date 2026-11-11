> Applies to Fio 3.5.0.1111.

The **Asset Browser** (`editor/asset_browser.py`) has four tabs.

## Textures

Select a texture and apply it to whole brushes or to single faces.

- **FACE** applies it to one face.
- **FIT** stretches it to fit the selected faces.
- **TILE** tiles it across the selected faces.

Applying `nodraw` to a face stops it being drawn ([Autocaulk](https://github.com/ViciousSquid/Fio/wiki/Autocaulk) does this for hidden faces). Texture folders dropped into `assets/textures` appear in the folder menu. A map's textures are loaded with the map, not all at start-up.

<img src="https://github.com/user-attachments/assets/fd4ea706-55bb-4122-a020-b0554e5a3bc3" width="350">

## Models

Browses the OBJ and GLB models in `assets/models` with a wireframe thumbnail of each. Choosing one sets it on a model [Prop](https://github.com/ViciousSquid/Fio/wiki/Prop), together with the model's own settings when it has them. A Z-up OBJ is turned Y-up when it loads; `# fio:up=y` (or `=z`) in the file overrides the guess.

<img src="https://github.com/user-attachments/assets/3ecabc99-2b3f-46ac-b06e-3d3ff670db81" width="350">

## Maps

Lists the maps in `maps/`; double-click loads one. The tab can be switched to **Packages**, where double-click plays a [`.fiopak`](https://github.com/ViciousSquid/Fio/wiki/.fiopak-archive).

## Audio

Lists the project's sounds for use by [Speakers](https://github.com/ViciousSquid/Fio/wiki/Speaker).
