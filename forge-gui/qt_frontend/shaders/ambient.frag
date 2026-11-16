#version 440
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    vec2 canvasSize;
};
void main() {
    vec2 uv = qt_TexCoord0;
    vec2 p = uv * vec2(canvasSize.x / canvasSize.y, 1.0);
    vec3 col = vec3(0.055, 0.066, 0.083);
    // Broad lighting behind the actual content supplies spatial detail to refract.
    float cool = exp(-length((p - vec2(0.90, 0.84)) * vec2(1.1, 1.7)) * 2.4);
    float warm = exp(-length((p - vec2(1.58, 0.30)) * vec2(1.0, 1.8)) * 3.2);
    float silver = exp(-pow((p.x + p.y * 0.72 - 1.28) * 2.8, 2.0));
    col += cool * vec3(0.055, 0.085, 0.13);
    col += warm * vec3(0.10, 0.06, 0.035);
    col += silver * vec3(0.025, 0.029, 0.035);
    float ribbon = exp(-pow((p.y - (0.87 + 0.045 * sin(p.x * 3.8))) / 0.024, 2.0));
    col += ribbon * vec3(0.037, 0.043, 0.055);
    float grain = fract(sin(dot(floor(uv * canvasSize), vec2(12.9898,78.233))) * 43758.5453);
    col += (grain - 0.5) * 0.003;
    fragColor = vec4(col, 1.0) * qt_Opacity;
}
