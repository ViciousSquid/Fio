# Shared editor fonts

These font files ship with the editor so Windows and macOS do not select
different installed fonts.

- Fio UI is derived from Selawik 1.01, Microsoft's open replacement for Segoe UI, from
  https://github.com/microsoft/Selawik/releases/tag/1.01
  (license in Selawik-OFL.txt). The modified fonts are renamed in accordance
  with the reserved font name requirement. Their hhea and OS/2 typographic
  metrics are normalized to 2100 units per em, 2100 ascent, 700 descent and
  zero line gap, with USE_TYPO_METRICS enabled. Outlines and advance widths
  are scaled to preserve their proportions. The 4/3-em line box matches the
  original Windows line height at the default and enlarged text sizes, with
  integral metrics that avoid different CoreText/GDI rounding results.
- Fio Mono Regular and Bold are derived from IBM Plex Mono, from
  https://github.com/google/fonts/tree/main/ofl/ibmplexmono
  (license in IBMPlexMono-OFL.txt). The modified fonts are renamed in
  accordance with the reserved font name requirement. Their hhea and OS/2
  typographic metrics are set to 1000 ascent, 250 descent and zero line gap
  (1000 units per em, USE_TYPO_METRICS on); outlines, advance widths and the
  Windows clipping metrics (usWinAscent/usWinDescent) are unchanged. Plex
  Mono's 1025/275 metrics fall between pixels at the console's 12px, which
  GDI rounded to a 15px line and FreeType to 17px; whole-pixel metrics give
  every platform the Windows line height.

Both use the SIL Open Font License 1.1. The application loads the fonts
directly through Qt; installation on the operating system is unnecessary.
