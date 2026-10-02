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





