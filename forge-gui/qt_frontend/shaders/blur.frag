#version 440
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    vec2 direction;
};
layout(binding = 1) uniform sampler2D sourceTexture;
void main() {
    vec2 uv = qt_TexCoord0;
    vec4 c = texture(sourceTexture, uv) * 0.227027;
    c += texture(sourceTexture, uv + direction * 1.384615) * 0.316216;
    c += texture(sourceTexture, uv - direction * 1.384615) * 0.316216;
    c += texture(sourceTexture, uv + direction * 3.230769) * 0.070270;
    c += texture(sourceTexture, uv - direction * 3.230769) * 0.070270;
    fragColor = c * qt_Opacity;
}
