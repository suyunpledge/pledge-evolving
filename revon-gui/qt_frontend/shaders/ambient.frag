#version 440
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    vec2 canvasSize;
    float modeAccent;
};

float hash21(vec2 p) {
    p = fract(p * vec2(127.1, 311.7));
    p += dot(p, p + 34.23);
    return fract(p.x * p.y);
}

vec2 hash22(vec2 p) {
    float n = hash21(p);
    return vec2(n, hash21(p + n * 19.37 + 7.13));
}

// Smooth 1D harmonic wave for organic aurora curtains
float auroraWave(float x, float phase) {
    return 0.50 * sin(x * 2.1 + phase)
         + 0.28 * sin(x * 4.3 - phase * 1.4 + 1.1)
         + 0.14 * cos(x * 7.9 + phase * 0.7 - 0.6)
         + 0.08 * sin(x * 13.2 - phase * 0.5);
}

// Vertical aurora curtain drapery: bright lower edge, silky vertical shafts fading upward
vec3 auroraCurtain(vec2 p, float baseY, float phase, float heightScale, float freq,
                   vec3 colLow, vec3 colMid, vec3 colHigh) {
    float curve = baseY + 0.085 * auroraWave(p.x * freq, phase);
    float dy = curve - p.y; // positive upward into the night sky (uv.y=0 is top)
    float lowerFade = smoothstep(-0.045, 0.015, dy);
    float upperFade = exp(-max(dy, 0.0) / heightScale);
    float verticalEnv = lowerFade * upperFade;

    // Fine vertical shimmer folds (polar curtain striations)
    float fold1 = 0.55 + 0.45 * sin(p.x * 14.0 * freq + phase * 1.7 + dy * 3.5);
    float fold2 = 0.60 + 0.40 * cos(p.x * 27.0 * freq - phase * 2.3 + sin(p.x * 6.0) * 2.0);
    float curtain = verticalEnv * mix(0.55, 1.18, fold1 * fold2);

    // Subtle color transition from lower emerald/cyan rim -> mid sapphire -> high violet tips
    float t = clamp(dy / (heightScale * 1.55), 0.0, 1.0);
    vec3 tint = mix(colLow, colMid, smoothstep(0.0, 0.48, t));
    tint = mix(tint, colHigh, smoothstep(0.38, 1.0, t));

    // Bright delicate lower ribbon core
    float rim = exp(-pow(dy / 0.022, 2.0)) * (0.55 + 0.45 * fold1);
    return tint * curtain + colLow * rim * 0.42;
}

// Crisp starfield layer with varied color temperatures
vec3 starLayer(vec2 p, float scale, float density, float minSize, float maxSize, bool withCross) {
    vec2 gv = fract(p * scale) - 0.5;
    vec2 id = floor(p * scale);
    vec2 rnd = hash22(id);
    if (rnd.x < density) return vec3(0.0);

    vec2 offset = (hash22(id + 41.7) - 0.5) * 0.72;
    vec2 d = gv - offset;
    float dist = length(d);

    float size = mix(minSize, maxSize, fract(rnd.y * 13.7));
    float core = smoothstep(size, 0.0, dist);
    core = core * core;
    float halo = exp(-dist / (size * 2.6)) * 0.24;

    float glint = 0.0;
    if (withCross && rnd.y > 0.62) {
        float sx = exp(-abs(d.x) / (size * 0.42)) * exp(-abs(d.y) / (size * 3.6));
        float sy = exp(-abs(d.y) / (size * 0.42)) * exp(-abs(d.x) / (size * 3.6));
        glint = (sx + sy) * 0.32;
    }

    // Star color temperature: ice-blue, pure white, pale lavender, warm champagne
    float temp = fract(rnd.x * 91.3 + rnd.y * 37.1);
    vec3 starCol = mix(vec3(0.68, 0.85, 1.00), vec3(0.95, 0.96, 1.00), smoothstep(0.0, 0.5, temp));
    starCol = mix(starCol, vec3(1.00, 0.89, 0.74), smoothstep(0.68, 1.0, temp));

    float intensity = mix(0.38, 1.0, fract(rnd.y * 53.3));
    return starCol * (core + halo + glint) * intensity;
}

void main() {
    vec2 uv = qt_TexCoord0;
    float aspect = canvasSize.x / max(canvasSize.y, 1.0);
    vec2 p = uv * vec2(aspect, 1.0);

    // 1. Deep polar night sky gradient (zenith dark indigo -> lower horizon navy)
    vec3 zenith = vec3(0.022, 0.032, 0.060);
    vec3 midSky = vec3(0.034, 0.048, 0.082);
    vec3 horizon = vec3(0.046, 0.066, 0.104);
    vec3 col = mix(zenith, midSky, smoothstep(0.0, 0.58, uv.y));
    col = mix(col, horizon, smoothstep(0.55, 1.0, uv.y));

    // 2. Subtle Milky Way / cosmic nebula dust band across the diagonal
    float mwCoord = (uv.x * 0.78 + uv.y * 0.62) - 0.72 + 0.06 * sin(p.x * 2.4);
    float milkyWay = exp(-pow(mwCoord / 0.24, 2.0));
    col += milkyWay * mix(vec3(0.024, 0.034, 0.068), vec3(0.020, 0.040, 0.072), modeAccent);

    // 3. Layered Aurora Borealis Curtains (极光帷幕)
    // Primary sweeping emerald-cyan-violet curtain across lower-mid to upper-right
    float slant1 = 0.76 - uv.x * 0.16;
    vec3 a1 = auroraCurtain(
        p, slant1, 0.6, 0.24, 0.85,
        mix(vec3(0.032, 0.185, 0.175), vec3(0.028, 0.205, 0.200), modeAccent),
        mix(vec3(0.048, 0.132, 0.235), vec3(0.042, 0.145, 0.255), modeAccent),
        vec3(0.095, 0.058, 0.168)
    );
    float mask1 = smoothstep(0.02, 0.32, uv.x) * smoothstep(1.08, 0.72, uv.x);
    col += a1 * mix(0.62, 0.92, mask1);

    // Secondary high twilight aurora veil (sapphire -> lavender)
    float slant2 = 0.42 + uv.x * 0.08;
    vec3 a2 = auroraCurtain(
        p + vec2(0.5, 0.0), slant2, 2.4, 0.20, 0.72,
        vec3(0.038, 0.125, 0.195),
        vec3(0.075, 0.068, 0.178),
        vec3(0.062, 0.038, 0.125)
    );
    float mask2 = smoothstep(0.10, 0.50, uv.x) * smoothstep(1.02, 0.60, uv.x);
    col += a2 * mask2 * 0.68;

    // Gentle warm revon ember glow on upper-right in Work/Chat mode (cooler violet in Code mode)
    float warmBloom = exp(-length((p - vec2(0.88 * aspect, 0.18)) * vec2(0.85, 1.35)) * 2.4);
    col += warmBloom * mix(vec3(0.095, 0.052, 0.038), vec3(0.048, 0.045, 0.095), modeAccent);

    // 4. Multi-depth Starry Sky (星辰)
    // Mask stars slightly in the densest lower aurora core for realistic atmospheric depth
    float starMask = mix(0.55, 1.0, smoothstep(0.88, 0.18, uv.y));
    vec3 starsFar  = starLayer(p, 26.0, 0.87, 0.022, 0.044, false) * 0.28;
    vec3 starsMid  = starLayer(p + vec2(4.3, 2.1), 13.0, 0.91, 0.020, 0.042, false) * 0.46;
    vec3 starsHero = starLayer(p + vec2(9.1, 5.7), 5.5,  0.92, 0.018, 0.036, true)  * 0.56;
    col += (starsFar + starsMid + starsHero) * starMask;

    // 5. Gentle edge vignette to anchor the composition
    float vig = smoothstep(1.32, 0.22, length((uv - 0.5) * vec2(1.12, 1.0)));
    col *= mix(0.82, 1.0, vig);

    // 6. Micro-grain dithering to prevent 8-bit gradient banding
    float grain = fract(sin(dot(floor(uv * canvasSize), vec2(12.9898, 78.233))) * 43758.5453);
    col += (grain - 0.5) * 0.0026;

    fragColor = vec4(col, 1.0) * qt_Opacity;
}
