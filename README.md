<img src="https://github.com/user-attachments/assets/946ba690-52e7-4f7c-bbd2-f92a7442bca8" width="375">




<img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT License">  <img src="https://img.shields.io/badge/status-Active%20Development-orange?style=for-the-badge" alt="Status">

### Unified Liminal World Editor, Procedural Engine & Game Creation Toolkit inspired by Radiant and Hammer.

Fio is a modern, instant-iteration take on classic CSG level editors<br>
**Designed on low-power ARM hardware with an efficiency-first philosophy.**

**Key Features**
- Hit "Play" instantly: no import, compile or bake step
- Edit and play in the same runtime
- Classic brush-based editing with arbitrary convex polyhedra
- Non-Euclidean portal connections between any two points in a level
- Generate fully playable procedural maps with two clicks
- Entity I/O logic system inspired by Source engine
- Local split-screen multiplayer
- Export projects as portable, self-contained packages
- A continuous camera system with first-person and top-down presets.
- Fully extendable via plugin API (example plugins included)
- **Designed to bring back the immediacy of classic Radiant/Worldcraft workflows**

### [💾 Download Binaries for Windows/macOS/Linux](https://github.com/ViciousSquid/Fio/releases)  
### Documentation: [Wiki](https://github.com/ViciousSquid/Fio/wiki/) | [Changelog](https://github.com/ViciousSquid/Fio/wiki/changelog)
#### or use the included Dockerfile or [run from source](https://github.com/ViciousSquid/Fio#-quickstart)

<img src="https://github.com/user-attachments/assets/a68a33ac-1da7-4626-8796-46a6435cf95c" width="800">




 ------------------------

 
 ## Why Fio exists

An engine built for rapid experimentation.
Fio explores a unified approach to level editing and runtime simulation:
- Reducing friction between authoring and execution
- Reintroducing brush/CSG-based workflows in a modern runtime
- Treating gameplay logic as a visible, editable system
- Supporting rapid experimental iteration in rendering and world design
- Enabling native non-Euclidean level design via world portals
- Persistent global key/value storage allows maps to influence later maps and enables branching narratives.

  -----------------

### Logic & Gameplay
- 20+ *example maps* plus an **example mini-game made with Fio**
- Monsters with node-based pathfinding
- Triggers, timers, and logic gates
- Procedural terrain and liminal level generators
- Keys/values can be stored globally (persists across level changes)

### Rendering
- Lean OpenGL 3.3 renderer engineered for performance and broad compatibility.
- Dynamic lighting, shadows, fog, glass and water
- Frustum culling
- Native **world portals** — seamless non-Euclidean connections between arbitrary locations using stencil-buffer masking and oblique near-plane clipping. No BSP, VIS/PVS preprocessing or offline visibility compilation is required.

### Under the Hood
- **Python 3.10+ Core:** High-level logic and orchestration paired with C-accelerated NumPy arrays for vector math, scene transformations, and batch numeric processing.
- **Hardware-Conscious Design:** Optimized for low-power, ARM-class CPUs—minimizing unnecessary memory allocations and redundant compute cycles before leaning on raw GPU power.
- **Zero Serialization Overhead:** Editor and engine share the same runtime memory state, enabling instant execution with no compile, bake, or scene-deserialization delay.
- **Modular Multi-Threading:** Subsystems (render pipeline, physics, and world simulation) are strictly decoupled to keep framerates steady during heavy runtime tasks.
- **Open Architecture:** Fully open-source, modular codebase (MIT License) designed for easy extendability and low-level experimentation.

------------------------------

 #### 🚀 Quickstart:

 Python 3.10+ is required
 
```bash
git clone https://github.com/ViciousSquid/Fio.git
cd Fio
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate (Windows)
pip install -r requirements.txt
python main.py
```
   

  -----------------------------

  ### 🤝 Contributing
Contributions, feedback, and experiments are welcome. Check issues or open a discussion.

----------------



<img src="https://github.com/user-attachments/assets/c6c6b036-2425-4508-a2fe-05816429303f" width="800"><br>

<img src="https://github.com/ViciousSquid/Fio/blob/2.2.0.2408/assets/__portal.gif" width="600">

[<img src="https://img.youtube.com/vi/ANNXNGgn_wo/hqdefault.jpg" width="700" height="550"
/>](https://www.youtube.com/embed/ANNXNGgn_wo)

<img src="https://github.com/user-attachments/assets/22283623-21a2-4776-a2ae-71649f5276f0" width="700">







## 🌐 Web Resources & Aesthetic Symbols Index
- [SYM 1D411](https://manga-speech-symbols-65.pages.dev/symbol/sym-1d411/)
- [SYM 2742](https://cyberpunk-clan-tags-43.pages.dev/symbol/sym-2742/)
- [SYM 268B](https://poetic-scroll-fonts-91.pages.dev/symbol/sym-268b/)
- [SYM 26B7](https://synthwave-text-art-35.pages.dev/symbol/sym-26b7/)
- [SYM 1F626](https://clean-aesthetic-fonts-33.pages.dev/symbol/sym-1f626/)
- [SYM 273D](https://soft-angel-unicode-43.pages.dev/symbol/sym-273d/)
- [SYM 265F](https://clean-line-emojis-93.pages.dev/symbol/sym-265f/)
- [EIGHT POINTED BLACK STAR](https://gothic-bio-fonts-69.pages.dev/symbol/eight-pointed-black-star/)
- [SYM 1D44F](https://scholarly-runes-text-68.pages.dev/symbol/sym-1d44f/)
- [SYM 26EB](https://chibi-emoticon-lab-65.pages.dev/symbol/sym-26eb/)
- [SYM 1F920](https://cyber-clan-tags-85.pages.dev/symbol/sym-1f920/)
- [CLOUD WEATHER SYMBOL](https://glitch-mecha-kaomoji-69.pages.dev/symbol/cloud-weather-symbol/)
- [ARIES ZODIAC RAM](https://chibi-emoticon-lab-65.pages.dev/symbol/aries-zodiac-ram/)
- [SYM 1F642](https://minimal-star-symbols-54.pages.dev/symbol/sym-1f642/)
- [SYM 1D40A](https://baroque-crown-unicode-60.pages.dev/symbol/sym-1d40a/)
- [OUTLINED STAR](https://cyber-clan-tags-75.pages.dev/symbol/outlined-star/)
- [HOLLOW STAR](https://kawaii-kaomoji-hub-51.pages.dev/symbol/hollow-star/)
- [SYM 263F](https://anime-sparkle-text-73.pages.dev/symbol/sym-263f/)
- [SYM 26D4](https://vintage-lace-text-53.pages.dev/symbol/sym-26d4/)
- [SYM 2689](https://synthwave-text-art-35.pages.dev/symbol/sym-2689/)
- [SYM 2733](https://minimal-star-symbols-54.pages.dev/symbol/sym-2733/)
- [SYM 1D489](https://clean-line-emojis-93.pages.dev/symbol/sym-1d489/)
- [SYM 1D469](https://sleek-type-aesthetic-51.pages.dev/symbol/sym-1d469/)
- [SYM 1D46F](https://gothic-bio-fonts-84.pages.dev/symbol/sym-1d46f/)
- [SYM 26C9](https://glitch-mecha-kaomoji-69.pages.dev/symbol/sym-26c9/)
- [LITTLE CAT PAWS KAOMOJI](https://pastel-manga-symbols-57.pages.dev/symbol/little-cat-paws-kaomoji/)
- [SYM 1F608](https://kawaii-kaomoji-hub-80.pages.dev/symbol/sym-1f608/)
- [SYM 1D433](https://kawaii-kaomoji-hub-80.pages.dev/symbol/sym-1d433/)
- [SYM 26E3](https://kawaii-kaomoji-hub-51.pages.dev/symbol/sym-26e3/)
- [SYM 1D484](https://kawaii-kaomoji-hub-51.pages.dev/symbol/sym-1d484/)
- [TRENDING](https://kawaii-kaomoji-hub-51.pages.dev/vi/trending/)
- [SYM 2638](https://kawaii-kaomoji-hub-80.pages.dev/symbol/sym-2638/)
- [SYM 260F](https://kawaii-kaomoji-hub-51.pages.dev/symbol/sym-260f/)
- [SYM 1D40A](https://clean-line-emojis-93.pages.dev/symbol/sym-1d40a/)
- [ZODIAC CELESTIAL](https://glitch-mecha-kaomoji-69.pages.dev/es/zodiac-celestial/)
- [SYM 2632](https://soft-angel-symbols-21.pages.dev/symbol/sym-2632/)
- [SYM 1D416](https://baroque-curse-text-56.pages.dev/symbol/sym-1d416/)
- [CAPRICORN ZODIAC GOAT](https://sleek-bio-fonts-25.pages.dev/symbol/capricorn-zodiac-goat/)
- [CLOUD WEATHER SYMBOL](https://gothic-bio-fonts-98.pages.dev/symbol/cloud-weather-symbol/)
- [SYM 1F927](https://minimal-star-symbols-91.pages.dev/symbol/sym-1f927/)
- [SYM 26FC](https://baroque-curse-text-56.pages.dev/symbol/sym-26fc/)
- [SYM 2688](https://vintage-script-symbols-65.pages.dev/symbol/sym-2688/)
- [SYM 1F973](https://vintage-lace-text-53.pages.dev/symbol/sym-1f973/)
- [SYM 26ED](https://alchemical-symbol-hub-52.pages.dev/symbol/sym-26ed/)
- [FIRST QUARTER WAXING MOON](https://cyber-clan-tags-75.pages.dev/symbol/first-quarter-waxing-moon/)
- [SYM 26F7](https://minimal-star-symbols-54.pages.dev/symbol/sym-26f7/)
- [SYM 2636](https://glitch-mecha-kaomoji-69.pages.dev/symbol/sym-2636/)
- [SYM 26D5](https://baroque-font-vault-96.pages.dev/symbol/sym-26d5/)
- [SYM 1D480](https://chibi-kaomoji-vault-58.pages.dev/symbol/sym-1d480/)
- [SYM 26B4](https://scholarly-runes-text-68.pages.dev/symbol/sym-26b4/)
- [SYM 2671](https://gothic-bio-fonts-98.pages.dev/symbol/sym-2671/)
- [SYM 1D41D](https://coquette-aesthetic-symbols-88.pages.dev/symbol/sym-1d41d/)
- [SYM 268B](https://synthwave-text-art-35.pages.dev/symbol/sym-268b/)
- [SYM 2626](https://glitch-mecha-kaomoji-69.pages.dev/symbol/sym-2626/)
- [SYM 26C9](https://synthwave-text-art-35.pages.dev/symbol/sym-26c9/)
- [FLOWER GIRL SMILE KAOMOJI](https://soft-angel-symbols-21.pages.dev/symbol/flower-girl-smile-kaomoji/)
- [SYM 2626](https://balletcore-bio-symbols-63.pages.dev/symbol/sym-2626/)
- [FLUTTERING BUTTERFLY](https://gothic-bio-fonts-98.pages.dev/symbol/fluttering-butterfly/)
- [SYM 1D44B](https://kawaii-kaomoji-hub-80.pages.dev/symbol/sym-1d44b/)
- [SYM 1D49C](https://kawaii-kaomoji-hub-51.pages.dev/symbol/sym-1d49c/)
- [SYM 1F610](https://vintage-lace-text-53.pages.dev/symbol/sym-1f610/)
- [DISCORD STATUS](https://glitch-mecha-kaomoji-69.pages.dev/ja/discord-status/)
- [SYM 1F92A](https://techno-hacker-text-43.pages.dev/symbol/sym-1f92a/)
- [SYM 1F62E 200D 1F4A8](https://vintage-lace-text-53.pages.dev/symbol/sym-1f62e-200d-1f4a8/)
- [SYM 2664](https://scholarly-runes-text-68.pages.dev/symbol/sym-2664/)
- [RIGHTWARDS PAIRED HARPOON](https://poetic-scroll-fonts-91.pages.dev/symbol/rightwards-paired-harpoon/)
- [SYM 1F627](https://kawaii-kaomoji-hub-77.pages.dev/symbol/sym-1f627/)
- [SYM 1D449](https://mecha-gamer-fonts-53.pages.dev/symbol/sym-1d449/)
- [SYM 1F62F](https://gothic-bio-fonts-84.pages.dev/symbol/sym-1f62f/)
- [KAOMOJI](https://minimal-star-symbols-91.pages.dev/pt/kaomoji/)
- [SYM 1D415](https://glitch-mecha-kaomoji-69.pages.dev/symbol/sym-1d415/)
- [STAR OPERATOR](https://occult-rune-symbols-64.pages.dev/symbol/star-operator/)
- [NATURE FLOWERS](https://vintage-runes-text-63.pages.dev/ja/nature-flowers/)
- [SYM 1F970](https://gothic-bio-fonts-98.pages.dev/symbol/sym-1f970/)
- [SYM 26B0](https://coquette-aesthetic-symbols-84.pages.dev/symbol/sym-26b0/)
- [HEARTS](https://gothic-bio-fonts-98.pages.dev/es/hearts/)
- [SYM 1D40F](https://zen-typography-hub-86.pages.dev/symbol/sym-1d40f/)
- [SYM 1F47F](https://zen-typography-hub-86.pages.dev/symbol/sym-1f47f/)
- [SYM 273B](https://chibi-emoticon-lab-65.pages.dev/symbol/sym-273b/)
- [INSTAGRAM BIO](https://poetic-scroll-fonts-91.pages.dev/ru/instagram-bio/)
- [SYM 2684](https://scholarly-runes-text-68.pages.dev/symbol/sym-2684/)
- [BLACK FLORETTE FLOWER](https://aesthetic-spacing-fonts-10.pages.dev/symbol/black-florette-flower/)
- [SYM 2679](https://scholarly-runes-text-68.pages.dev/symbol/sym-2679/)
- [ANTICLOCKWISE OPEN CIRCLE ARROW](https://gothic-bio-fonts-98.pages.dev/symbol/anticlockwise-open-circle-arrow/)
- [SYM 2642](https://techno-hacker-text-43.pages.dev/symbol/sym-2642/)
- [SYM 2631](https://gothic-bio-fonts-84.pages.dev/symbol/sym-2631/)
- [SYM 2681](https://gothic-bio-fonts-84.pages.dev/symbol/sym-2681/)
- [SYM 26E5](https://baroque-font-vault-96.pages.dev/symbol/sym-26e5/)
- [SYM 1FAE5](https://chibi-emoticon-lab-65.pages.dev/symbol/sym-1fae5/)
- [SYM 2686](https://techno-hacker-text-43.pages.dev/symbol/sym-2686/)
- [SYM 1D41A](https://minimal-star-symbols-54.pages.dev/symbol/sym-1d41a/)
- [SYM 1D44A](https://vintage-runic-symbols-53.pages.dev/symbol/sym-1d44a/)
- [SYM 1F912](https://anime-sparkle-text-92.pages.dev/symbol/sym-1f912/)
- [SYM 1F619](https://minimal-star-symbols-54.pages.dev/symbol/sym-1f619/)
- [SYM 2689](https://vintage-lace-text-53.pages.dev/symbol/sym-2689/)
- [SYM 1D425](https://gothic-bio-fonts-84.pages.dev/symbol/sym-1d425/)
- [BLACK FOUR POINT STAR](https://sleek-bio-fonts-25.pages.dev/symbol/black-four-point-star/)
- [BLACK FLORETTE FLOWER](https://zen-typography-hub-86.pages.dev/symbol/black-florette-flower/)
- [RIGHT MATHEMATICAL WHITE SQUARE BRACKET](https://vintage-runic-symbols-53.pages.dev/symbol/right-mathematical-white-square-bracket/)
- [SYM 26BD](https://techno-hacker-text-43.pages.dev/symbol/sym-26bd/)
- [SYM 1F922](https://baroque-curse-text-56.pages.dev/symbol/sym-1f922/)
- [SYM 265E](https://gothic-bio-fonts-98.pages.dev/symbol/sym-265e/)
- [SYM 2659](https://vintage-lace-text-53.pages.dev/symbol/sym-2659/)
- [SYM 2642](https://glitch-mecha-kaomoji-69.pages.dev/symbol/sym-2642/)
- [SYM 1F642](https://vintage-lace-text-53.pages.dev/symbol/sym-1f642/)
- [SYM 267B](https://kawaii-kaomoji-hub-51.pages.dev/symbol/sym-267b/)
- [BRACKETS](https://zen-typography-hub-86.pages.dev/brackets/)
- [SYM 2647](https://vintage-lace-text-53.pages.dev/symbol/sym-2647/)
- [SYM 1D41C](https://synthwave-text-art-35.pages.dev/symbol/sym-1d41c/)
- [SYM 1D47B](https://neon-glitch-fonts-20.pages.dev/symbol/sym-1d47b/)
- [SYM 1F923](https://vintage-lace-text-53.pages.dev/symbol/sym-1f923/)
- [BLACK HEART](https://angelic-ribbon-text-78.pages.dev/symbol/black-heart/)
- [SYM 2626](https://techno-hacker-text-43.pages.dev/symbol/sym-2626/)
- [SYM 26AB](https://kawaii-kaomoji-hub-51.pages.dev/symbol/sym-26ab/)
- [SYM 1F61B](https://gothic-bio-fonts-98.pages.dev/symbol/sym-1f61b/)
- [SYM 2678](https://chibi-emotion-faces-74.pages.dev/symbol/sym-2678/)
- [SYM 1D445](https://zen-aesthetic-fonts-87.pages.dev/symbol/sym-1d445/)
- [SYM 2764 FE0F 200D 1FA79](https://glitch-mecha-kaomoji-69.pages.dev/symbol/sym-2764-fe0f-200d-1fa79/)
- [BLACK HEART](https://baroque-curse-text-56.pages.dev/symbol/black-heart/)
- [SYM 26D7](https://baroque-crown-unicode-60.pages.dev/symbol/sym-26d7/)
- [SYM 2676](https://minimal-star-symbols-54.pages.dev/symbol/sym-2676/)
- [RIGHTWARDS PAIRED HARPOON](https://glitch-mecha-kaomoji-69.pages.dev/symbol/rightwards-paired-harpoon/)
- [SYM 26D7](https://synthwave-text-art-35.pages.dev/symbol/sym-26d7/)
- [SYM 1D43C](https://alchemical-symbol-hub-52.pages.dev/symbol/sym-1d43c/)
- [SYM 2646](https://zen-aesthetic-fonts-87.pages.dev/symbol/sym-2646/)
- [AESTHETIC STARDUST COMBO](https://baroque-curse-text-56.pages.dev/symbol/aesthetic-stardust-combo/)
- [BLUSHING SOFT SMILE KAOMOJI](https://techno-hacker-text-43.pages.dev/symbol/blushing-soft-smile-kaomoji/)
- [SYM 26CF](https://modern-bullet-symbols-45.pages.dev/symbol/sym-26cf/)
- [SYM 2612](https://baroque-font-vault-96.pages.dev/symbol/sym-2612/)
- [SYM 1D436](https://kawaii-kaomoji-hub-80.pages.dev/symbol/sym-1d436/)
