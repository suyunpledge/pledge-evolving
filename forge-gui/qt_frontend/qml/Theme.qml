pragma Singleton
import QtQuick

QtObject {
    // Canvas & depth
    readonly property color bgBase: "#0d1118"
    readonly property color bgFallback: "#121822"
    readonly property color sidebarBg: "#820c1017"
    readonly property color divider: "#1affffff"

    // Typography hierarchy
    readonly property color textPrimary: "#eef2f7"
    readonly property color textSecondary: "#b8c4d2"
    readonly property color textMuted: "#8291a4"
    readonly property color textDim: "#68788c"

    // Brand & interactive accents (Sapphire Steel + Forge Ember)
    readonly property color accent: "#5b9bf8"
    readonly property color accentInk: "#f4f8ff"
    readonly property color accentTint: "#962a5794"
    readonly property color selectedTint: "#88344f6e"
    readonly property color glassTint: "#28283444"
    readonly property color ember: "#e89d5a"

    // User message bubble
    readonly property color userBubble: "#2434485f"
    readonly property color userBubbleBorder: "#24ffffff"

    // Semantic status colors
    readonly property color success: "#6fd69e"
    readonly property color successText: "#cdebd9"
    readonly property color warning: "#eab875"
    readonly property color warningText: "#f3d8ad"
    readonly property color danger: "#f08a8a"
    readonly property color dangerSurface: "#dc2c181b"
    readonly property color dangerBorder: "#55e06c6c"
    readonly property color dangerText: "#f6c7c4"
    readonly property color dangerTint: "#6852262a"

    // Solid-mode fallback surfaces
    readonly property color solidSurface: "#222a36"
    readonly property color solidSelected: "#2d3b4e"
    readonly property color solidPrimary: "#244775"
    readonly property color solidBorder: "#5c697a"
}
