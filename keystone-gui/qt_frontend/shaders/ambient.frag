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
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

float bokehOrb(vec2 p, vec2 center, float radius, float softness) {
    float d = length(p - center);
    float core = exp(-pow(d / radius, 2.0) * softness);
    float ring = exp(-pow((d - radius * 0.72) / (radius * 0.28), 2.0)) * 0.35;
    return core + ring * smoothstep(radius * 1.3, 0.0, d);
}

float starGlint(vec2 uv, vec2 gridScale) {
    vec2 gv = fract(uv * gridScale) - 0.5;
    vec2 id = floor(uv * gridScale);
    float n = hash21(id);
    if (n < 0.84) return 0.0;
    vec2 offset = vec2(fract(n * 31.7) - 0.5, fract(n * 73.1) - 0.5) * 0.65;
    float d = length(gv - offset);
    float size = mix(0.015, 0.042, fract(n * 17.3));
    float brightness = smoothstep(size, 0.0, d);
    return brightness * brightness * mix(0.35, 1.0, fract(n * 53.9));
}

void main() {
    vec2 uv = qt_TexCoord0;
    float aspect = canvasSize.x / max(canvasSize.y, 1.0);
    vec2 p = uv * vec2(aspect, 1.0);

    // Deep restful midnight-sapphire base
    vec3 col = vec3(0.036, 0.046, 0.066);

    // Broad atmospheric nebula blooms (cool sapphire, twilight violet, warm Keystone ember)
    float cool = exp(-length((p - vec2(0.78 * aspect * 0.56, 0.84)) * vec2(0.95, 1.45)) * 2.05);
    float warm = exp(-length((p - vec2(0.90 * aspect, 0.20)) * vec2(0.90, 1.55)) * 2.55);
    float twilight = exp(-length((p - vec2(0.30 * aspect, 0.24)) * vec2(1.05, 1.35)) * 2.25);
    float horizonMist = exp(-length((p - vec2(0.55 * aspect, 0.62)) * vec2(0.75, 1.65)) * 2.1);

    col += cool * mix(vec3(0.062, 0.118, 0.205), vec3(0.050, 0.128, 0.215), modeAccent);
    col += warm * mix(vec3(0.145, 0.076, 0.046), vec3(0.095, 0.068, 0.125), modeAccent * 0.65);
    col += twilight * mix(vec3(0.056, 0.048, 0.118), vec3(0.048, 0.068, 0.145), modeAccent);
    col += horizonMist * vec3(0.024, 0.042, 0.072);

    // Soft silk aurora veils with gentle domain-warped curves
    float warp1 = 0.055 * sin(p.x * 2.6 + 0.4) + 0.025 * cos(p.x * 4.8 - 0.8);
    float aurora1 = exp(-pow((p.y - (0.76 + warp1)) / 0.085, 2.0));
    float auroraCore1 = exp(-pow((p.y - (0.76 + warp1)) / 0.022, 2.0));

    float warp2 = 0.065 * cos(p.x * 2.1 - 0.7) + 0.020 * sin(p.x * 5.2 + 1.1);
    float aurora2 = exp(-pow((p.y - (0.34 + warp2)) / 0.11, 2.0)) * smoothstep(0.15, 0.85, uv.x);

    float diagonalSilk = exp(-pow((uv.x * 0.85 + uv.y * 0.65 - 0.92) * 3.1, 2.0));

    col += aurora1 * mix(vec3(0.032, 0.068, 0.118), vec3(0.028, 0.082, 0.132), modeAccent);
    col += auroraCore1 * vec3(0.038, 0.062, 0.096);
    col += aurora2 * mix(vec3(0.042, 0.036, 0.082), vec3(0.032, 0.058, 0.098), modeAccent);
    col += diagonalSilk * vec3(0.022, 0.030, 0.044);

    // Soft out-of-focus bokeh orbs (peripheral & atmospheric, never harsh)
    float b1 = bokehOrb(p, vec2(0.86 * aspect, 0.24), 0.13, 1.8);
    float b2 = bokehOrb(p, vec2(0.76 * aspect, 0.16), 0.065, 2.2);
    float b3 = bokehOrb(p, vec2(0.92 * aspect, 0.36), 0.045, 2.4);
    float b4 = bokehOrb(p, vec2(0.32 * aspect, 0.78), 0.14, 1.7);
    float b5 = bokehOrb(p, vec2(0.24 * aspect, 0.68), 0.055, 2.3);
    float b6 = bokehOrb(p, vec2(0.44 * aspect, 0.86), 0.040, 2.5);
    float b7 = bokehOrb(p, vec2(0.64 * aspect, 0.28), 0.050, 2.4);
    float b8 = bokehOrb(p, vec2(0.40 * aspect, 0.22), 0.085, 2.0);

    col += b1 * mix(vec3(0.042, 0.026, 0.018), vec3(0.028, 0.032, 0.052), modeAccent);
    col += b2 * vec3(0.034, 0.024, 0.020);
    col += b3 * vec3(0.024, 0.032, 0.048);
    col += b4 * vec3(0.022, 0.042, 0.070);
    col += b5 * vec3(0.026, 0.048, 0.078);
    col += b6 * vec3(0.020, 0.038, 0.062);
    col += b7 * vec3(0.024, 0.030, 0.048);
    col += b8 * vec3(0.022, 0.024, 0.052);

    // Subtle starlight dust motes
    float starsFar = starGlint(p, vec2(14.0, 14.0));
    float starsNear = starGlint(p + vec2(3.7, 1.9), vec2(8.0, 8.0));
    col += starsFar * vec3(0.045, 0.058, 0.082);
    col += starsNear * vec3(0.055, 0.052, 0.068);

    // Gentle edge vignette to anchor the frame
    float vig = smoothstep(1.25, 0.25, length((uv - 0.5) * vec2(1.15, 1.0)));
    col *= mix(0.84, 1.0, vig);

    // Micro-grain to eliminate 8-bit gradient banding
    float grain = fract(sin(dot(floor(uv * canvasSize), vec2(12.9898, 78.233))) * 43758.5453);
    col += (grain - 0.5) * 0.0028;

    fragColor = vec4(col, 1.0) * qt_Opacity;
}
