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
    // Muted/caption text must stay >= 4.5:1 on the lightest glass over the aurora
    // band (~#24384a); dim is for decorative/disabled text only (>= 3:1).
    readonly property color textMuted: "#9eabbb"
    readonly property color textDim: "#8392a5"
    readonly property int captionSize: 12

    // Brand & interactive accents (Sapphire Steel + Keystone Ember)
    readonly property color accent: "#5b9bf8"
    readonly property color accentInk: "#f4f8ff"
    readonly property color accentTint: "#962a5794"
    readonly property color selectedTint: "#88344f6e"
    readonly property color glassTint: "#28283444"
    readonly property color ember: "#e89d5a"

    // Primary action: the single most salient control on a surface (send, approve).
    readonly property color primaryFill: "#3d7fe0"
    readonly property color primaryFillHover: "#4f8ff0"
    readonly property color primaryBorder: "#8ab8ff"

    // Keyboard focus ring: visible on every surface, never used for anything else.
    readonly property color focusRing: "#a9cbff"
    readonly property int focusRingWidth: 2

    // Opaque card over content (approval, confirmations): content must not bleed through.
    readonly property color overlayCard: "#fb141b25"
    readonly property color scrim: "#a0060910"

    // User message bubble
    readonly property color userBubble: "#30334a66"
    readonly property color userBubbleBorder: "#2effffff"

    // Semantic status colors
    readonly property color success: "#6fd69e"
    readonly property color successText: "#cdebd9"
    readonly property color warning: "#eab875"
    readonly property color warningText: "#f3d8ad"
    readonly property color warningBorder: "#8ceab875"
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
