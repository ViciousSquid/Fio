"""Desktop lighting and shadow formulas, with an ES-compatible atlas lookup."""
from engine.shaders import DEFAULT_SHADERS, SHADOW_GLSL, MAX_SHADOW_LIGHTS, light_ubo_source
from player.gles_shaders import translate

VERTEX = """#version 300 es
precision highp float;
layout(location=0) in vec3 position;
layout(location=1) in vec3 normal;
layout(location=2) in vec2 uv;
uniform mat4 view;
uniform mat4 projection;
uniform mat4 model;
out vec3 worldPos;
out vec3 worldNormal;
out vec2 texCoord;
void main() {
    worldPos = vec3(model * vec4(position, 1.0));
    worldNormal = transpose(inverse(mat3(model))) * normal;
    texCoord = uv;
    gl_Position = projection * view * vec4(worldPos, 1.0);
}
"""

# Preserve the desktop PCF loop, radial depth and slope bias. Only the cube
# lookup changes to an atlas, which needs one sampler on every GLES 3 GPU.
_ATLAS_SAMPLE = """
uniform highp sampler2D shadowAtlas;
uniform float shadowTileSize;
highp float _sampleShadowCube(int idx, highp vec3 delta) {
    vec3 a=abs(delta); float face; vec2 uv; float d;
    if(a.x>=a.y && a.x>=a.z) {
        d=a.x;face=delta.x>0.0?0.0:1.0;uv=vec2(delta.x>0.0?-delta.z:delta.z,-delta.y)/d;
    } else if(a.y>=a.z) {
        d=a.y;face=delta.y>0.0?2.0:3.0;uv=vec2(delta.x,delta.y>0.0?delta.z:-delta.z)/d;
    } else {
        d=a.z;face=delta.z>0.0?4.0:5.0;uv=vec2(delta.z>0.0?delta.x:-delta.x,-delta.y)/d;
    }
    uv=clamp(uv*.5+.5,vec2(.5/shadowTileSize),vec2(1.0-.5/shadowTileSize));
    uv=(vec2(face,float(idx))+uv)/vec2(6.0,""" + str(float(MAX_SHADOW_LIGHTS)) + """);
    return texture(shadowAtlas,uv).r;
}
"""
_shadow = _ATLAS_SAMPLE + SHADOW_GLSL[SHADOW_GLSL.index('// idx'):]
_fragment = DEFAULT_SHADERS['textured.frag'].replace(SHADOW_GLSL, _shadow)
_fragment = _fragment.replace('uniform vec4 model_tint;', 'uniform vec4 tint;\nuniform bool unlit;')
_fragment = _fragment.replace('texColor.rgb *= mix(vec3(1.0), model_tint.rgb, model_tint.a);',
                            'texColor *= tint;\n    if (unlit) { FragColor = vec4(applyFog(texColor.rgb, FragPos), texColor.a); return; }')
for _old, _new in (('FragPos','worldPos'), ('Normal','worldNormal'),
                  ('TexCoords','texCoord'), ('texture_diffuse','diffuse'),
                  ('active_lights','lightCount')):
    _fragment = _fragment.replace(_old, _new)
# Some Android GLES translators count block members against the 256-vector
# default-uniform limit. Four vec4 slots per light fail there at 64 lights.
# Pack radius/intensity into the unused position/colour components instead:
# 192 slots retain every desktop value and fit even the ES minimum of 224.
_fragment = light_ubo_source(_fragment)
_fragment = _fragment.replace('highp vec4 position;', 'highp vec4 position_radius;')
_fragment = _fragment.replace('vec4 color;', 'vec4 color_intensity;')
_fragment = _fragment.replace('    vec4 params;       // x=intensity, y=radius, z=shadow strength, w=shadow-only\n', '')
_fragment = _fragment.replace('lights[i].params.z', '(float(lights[i].indices.y) / 1000.0)')
_fragment = _fragment.replace('lights[i].params.w', 'float(lights[i].indices.z)')
for _old, _new in (('.position.xyz','.position_radius.xyz'),
                  ('.color.xyz','.color_intensity.xyz'),
                  ('.params.x','.color_intensity.w'), ('.params.y','.position_radius.w')):
    _fragment = _fragment.replace(_old,_new)
FRAGMENT = translate(_fragment, 'fragment').replace('precision mediump float;', 'precision highp float;')


def fragment(quality):
    """Keep lighting formulas, with player-specific loop budgets."""
    source = FRAGMENT
    if quality.shadow_samples != 20:
        source = source.replace('s < 20;', f's < {quality.shadow_samples};')
        # The first eight canonical taps are the symmetric cube corners.
        source = source.replace('return shadow / 20.0;',
                                f'return shadow / {quality.shadow_samples}.0;')
        # Include two shadow texels in the mobile bias: its smaller atlas must
        # still reject grazing-angle self-shadowing after depth quantisation.
        source = source.replace('highp float bias = diskRadius + clamp(',
                                'highp float bias = diskRadius + 2.0 * farPlane / shadowTileSize + clamp(')
    if quality.lights_per_batch < 64:
        source = source.replace('uniform int lightCount;',
            f'uniform int lightCount;\nuniform int lightIndices[{quality.lights_per_batch}];')
        source = source.replace('for(int i = 0; i < lightCount && i < 64; i++) {',
            f'for(int localLight = 0; localLight < lightCount && localLight < {quality.lights_per_batch}; localLight++) {{\n'
            '        int i = lightIndices[localLight];')
    return source
