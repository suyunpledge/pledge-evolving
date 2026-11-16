#version 440
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    vec2 surfaceSize;
    vec2 sceneSize;
    vec2 sceneOrigin;
    vec2 pointerPosition;
    vec4 tintColor;
    float cornerRadius;
    float shadowPadding;
    float refractionStrength;
    float frostAmount;
    float hoverAmount;
    float selectedAmount;
};
layout(binding = 1) uniform sampler2D backgroundTexture;
layout(binding = 2) uniform sampler2D blurredTexture;

float roundedBox(vec2 p, vec2 halfSize, float r) {
    vec2 q = abs(p) - halfSize + r;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}
void main() {
    vec2 fullSize = surfaceSize + vec2(shadowPadding * 2.0);
    vec2 local = qt_TexCoord0 * fullSize - shadowPadding;
    vec2 p = local - surfaceSize * 0.5;
    float r = min(cornerRadius, min(surfaceSize.x, surfaceSize.y) * 0.5);
    float d = roundedBox(p, surfaceSize * 0.5, r);
    float aa = max(fwidth(d), 0.65);
    float mask = 1.0 - smoothstep(-aa, aa, d);
    float thickness = min(20.0, min(surfaceSize.x, surfaceSize.y) * 0.30);
    float rimDepth = clamp(1.0 + d / thickness, 0.0, 1.0);
    // SDF gradient defines the curved glass normal, including rounded corners.
    vec2 normal = normalize(vec2(
        roundedBox(p + vec2(0.5, 0.0), surfaceSize * 0.5, r) - roundedBox(p - vec2(0.5, 0.0), surfaceSize * 0.5, r),
        roundedBox(p + vec2(0.0, 0.5), surfaceSize * 0.5, r) - roundedBox(p - vec2(0.0, 0.5), surfaceSize * 0.5, r)
    ) + vec2(0.00001));
    float lens = rimDepth * rimDepth * (1.0 - rimDepth * 0.55);
    vec2 displaced = sceneOrigin + local - normal * refractionStrength * lens;
    vec2 uv = clamp(displaced / sceneSize, vec2(0.001), vec2(0.999));
    vec2 dispersion = normal * lens * 0.9 / sceneSize;
    vec3 clear = vec3(texture(backgroundTexture, uv + dispersion).r,
                      texture(backgroundTexture, uv).g,
                      texture(backgroundTexture, uv - dispersion).b);
    vec3 frosted = texture(blurredTexture, uv).rgb;
    vec3 col = mix(clear, frosted, frostAmount);
    col = mix(col, tintColor.rgb, tintColor.a + selectedAmount * 0.035);
    // Transmission, a broad reflected softbox, and edge Fresnel are separate terms.
    col += vec3(0.022, 0.026, 0.032) + selectedAmount * vec3(0.024, 0.028, 0.035);
    float softbox = pow(1.0 - clamp(local.y / surfaceSize.y, 0.0, 1.0), 3.0);
    col += softbox * 0.035;
    float light = pow(max(dot(normal, normalize(vec2(-0.55, -0.83))), 0.0), 2.0);
    float lowerReflection = pow(max(dot(normal, normalize(vec2(0.7, 0.7))), 0.0), 3.0);
    float hairline = exp(-pow((d + 0.8) / 0.65, 2.0));
    col += hairline * (0.045 + 0.29 * light + 0.08 * lowerReflection);
    col += pow(rimDepth, 4.0) * (0.055 * light + 0.025 * lowerReflection);
    float pointerGlow = exp(-length((local - pointerPosition) / vec2(145.0, 100.0)) * 2.5);
    col += hoverAmount * pointerGlow * vec3(0.045, 0.048, 0.054);
    float shadowD = roundedBox(p - vec2(0.0, 4.0), surfaceSize * 0.5, r);
    float shadow = exp(-max(shadowD, 0.0) / 5.0) * 0.32 * (1.0 - mask);
    fragColor = vec4(col * mask, mask + shadow) * qt_Opacity;
}
