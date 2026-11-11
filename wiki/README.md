# Wiki replacement pages

Full replacements for https://github.com/ViciousSquid/Fio/wiki, checked against
the `3.1.0.0611_CANARY` branch. Each file is one wiki page; the file name is the
page's file name in the wiki repository (`Fio.wiki.git`), so they can be copied
over as they are. `README.md` (this file) is not a wiki page.

**One file must be renamed when copied:** `Basic-concept---Brushes.md` is the
page `Basic-concept:--Brushes.md`. A colon cannot be checked out on Windows, so
it is not used in this repository.

```bash
git clone https://github.com/ViciousSquid/Fio.wiki.git
cp wiki/*.md wiki/.fiopak-archive.md Fio.wiki/
rm Fio.wiki/README.md
mv Fio.wiki/Basic-concept---Brushes.md "Fio.wiki/Basic-concept:--Brushes.md"
cd Fio.wiki && git add -A && git commit -m "Wiki for 3.5.0.1111" && git push
```

Two file names contain a U+2010 hyphen, as on the live wiki:
`Split‐Screen.md` and `Portal-Rendering-‐-Technical-Overview.md`.

New pages (not on the live wiki before): `Building-and-Running-from-Source`,
`Testing-and-CI`, `Terrain`, `Cutscenes`, `View-Filters-and-VisGroups`,
`Physics`, `Save-Games`, `Cell-Occlusion-Culling`, `Deathmatch-Plugin`,
`BigWorld-Plugin`, `Tidy-Plugin`, `Fio-Player-(Android)`.
