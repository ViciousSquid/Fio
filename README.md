1.0.14 DEV branch:

Terrain generator!

#### ARM-Optimized Shaders

- Removed expensive transpose(inverse(model)) from fragment shaders
- Normal matrix is now pre-computed on CPU and passed as a uniform
- This alone can give 20-30% improvement on ARM GPUs

####  Reduced Fog Ray Marching

- Changed from 32 steps to 16 steps for fog volumes
- Early-out threshold raised from 0.99 to 0.95
- 
#### Per-Frame Light Optimisations

- Lights are now uploaded once per frame instead of once per shader switch
- Tracks which shader has lights uploaded to avoid redundant calls

#### Shader State Tracking

- Tracks _current_shader to avoid unnecessary glUseProgram calls
- Reduces driver overhead significantly on emulated x64

#### Performance flags:

Performance Flags at the top of the Renderer class:

```python
self.arm_mode = True           # Use ARM-optimized shaders
self.shadows_enabled = False   # Disable shadows for ARM
self.fog_quality = 'low'       # 16 ray march steps
self.skip_culling_in_renderer = True  # Trust logic thread's culling
```
