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
    vec3 col = vec3(0.045, 0.056, 0.075);
    // Broad lighting behind the actual content supplies spatial detail to refract.
    float cool = exp(-length((p - vec2(0.86, 0.82)) * vec2(1.05, 1.65)) * 2.2);
    float warm = exp(-length((p - vec2(1.56, 0.28)) * vec2(0.95, 1.75)) * 2.9);
    float deepIndigo = exp(-length((p - vec2(0.22, 0.22)) * vec2(1.1, 1.4)) * 2.6);
    float silver = exp(-pow((p.x + p.y * 0.72 - 1.28) * 2.8, 2.0));
    col += cool * vec3(0.068, 0.118, 0.195);
    col += warm * vec3(0.138, 0.078, 0.042);
    col += deepIndigo * vec3(0.035, 0.052, 0.095);
    col += silver * vec3(0.028, 0.034, 0.044);
    float ribbon = exp(-pow((p.y - (0.87 + 0.045 * sin(p.x * 3.8))) / 0.024, 2.0));
    col += ribbon * vec3(0.042, 0.054, 0.076);
    float grain = fract(sin(dot(floor(uv * canvasSize), vec2(12.9898,78.233))) * 43758.5453);
    col += (grain - 0.5) * 0.003;
    fragColor = vec4(col, 1.0) * qt_Opacity;
}
