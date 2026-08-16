# Sleek Minimalist Apple-style Dark Mode Design System
#
# Naming contract
# ---------------
# The existing constants below are referenced by ui/*.py and by verify_ui.py.
# Do not rename or remove them. New tokens are appended in dedicated sections
# so that widgets can migrate incrementally.

# ---------------------------------------------------------------------------
# Surfaces (OLED black canvas with iOS-style elevated surfaces)
# ---------------------------------------------------------------------------
CANVAS_COLOR = "#0A0A0C"       # Deep Space Black window background
SURFACE_COLOR = "#1C1C1E"      # Apple System Dark Gray (card container background)
ELEVATED_COLOR = "#2C2C2E"     # Apple Secondary Dark Gray (input fields on a card)
CARD_COLOR = "#3A3A3C"         # Apple Tertiary Dark Gray (hover / focus / selection)
HAIRLINE_COLOR = "#38383A"     # Apple System Separator border (subtle, on a card)

# Controls that sit directly on CANVAS_COLOR need more separation than
# SURFACE_COLOR provides (SURFACE on CANVAS is only ~1.35:1 contrast, which is
# effectively invisible). Use CONTROL_COLOR + CONTROL_BORDER for those.
CONTROL_COLOR = "#2C2C2E"      # Control fill on the canvas (~2.2:1 vs canvas)
CONTROL_HOVER = "#3A3A3C"      # Hover state for canvas-level controls
CONTROL_BORDER = "#48484A"     # Visible hairline on canvas (~3.9:1 vs canvas)

# ---------------------------------------------------------------------------
# Text (iOS typography scale)
# ---------------------------------------------------------------------------
TEXT_PRIMARY = "#FFFFFF"       # Main text and headers
TEXT_BODY = "#E5E5EA"          # Apple primary label (off-white, reading comfort)
TEXT_MUTE = "#8E8E93"          # Apple secondary label (~5.2:1 on SURFACE)
TEXT_TERTIARY = "#98989D"      # Quiet but readable info text (~5.9:1 on SURFACE)
TEXT_DARK = "#000000"          # Black text for high-contrast light fills
TEXT_DISABLED = "#48484A"      # Genuinely disabled state ONLY (fails contrast)

# ---------------------------------------------------------------------------
# Accent / semantic colors (Apple SF palette)
# ---------------------------------------------------------------------------
ACCENT_WHITE = "#FFFFFF"       # Primary CTA fill
ACCENT_GREEN = "#30D158"
ACCENT_GREEN_HOVER = "#24B047"
ACCENT_RED = "#FF453A"
ACCENT_RED_HOVER = "#E03B30"
ACCENT_YELLOW = "#FFD60A"
ACCENT_YELLOW_HOVER = "#E0BC08"
ACCENT_BLUE = "#0A84FF"
ACCENT_BLUE_HOVER = "#0070E0"

# ---------------------------------------------------------------------------
# Status tints
# ---------------------------------------------------------------------------
# Saturated fills with white text fail contrast (white on ACCENT_RED is only
# ~3.4:1). Tinted surfaces with a bright foreground read better and look
# calmer. Every pair below clears 4.5:1.
TINT_NEUTRAL_BG = "#2C2C2E"
TINT_NEUTRAL_FG = "#E5E5EA"    # ~11.0:1
TINT_INFO_BG = "#122740"
TINT_INFO_FG = "#4DA3FF"       # ~5.8:1
TINT_RUNNING_BG = "#2E2612"
TINT_RUNNING_FG = "#FFD60A"    # ~10.6:1
TINT_SUCCESS_BG = "#10301C"
TINT_SUCCESS_FG = "#30D158"    # ~7.1:1
TINT_ERROR_BG = "#3A1A18"
TINT_ERROR_FG = "#FF6E63"      # ~5.7:1

# ---------------------------------------------------------------------------
# Rounded corners (Apple curvature hierarchy)
# ---------------------------------------------------------------------------
ROUNDED_SM = 6                 # Small controls, checkboxes
ROUNDED_MD = 10                # Buttons, input fields, dropdowns
ROUNDED_LG = 16                # Main cards, log panels, modal dialogs
ROUNDED_XL = 20                # Outer window containers
ROUNDED_PILL = 11              # Status badge pill

# ---------------------------------------------------------------------------
# Spacing scale (4px grid)
# ---------------------------------------------------------------------------
# Layout padding was previously ad-hoc (1, 3, 4, 6, 8, 10, 15, 20 mixed
# arbitrarily). Snap everything to this scale.
SPACE_0 = 0
SPACE_1 = 4                    # Label to field, tight inline gaps
SPACE_2 = 8                    # Between related controls
SPACE_3 = 12                   # Card inner padding
SPACE_4 = 16                   # Between sections
SPACE_5 = 20                   # Window gutter
SPACE_6 = 24

GUTTER = SPACE_5               # Left/right window margin
CARD_PAD = SPACE_3             # Inner padding of form cards
LABEL_GAP = SPACE_1            # Vertical gap between a label and its field

# Half-step, used only for the vertical gap *between* form rows. The form has
# nine label+field rows inside a fixed-height window, so a full 4px step there
# costs ~36px of the log panel's space for no readability gain -- the labels
# already separate the rows. LABEL_GAP stays at the full step because that gap
# is what stops a label from visually merging into its own field.
ROW_GAP = 2

# ---------------------------------------------------------------------------
# Control height scale
# ---------------------------------------------------------------------------
H_TITLEBAR = 36                # Fixed: asserted by verify_ui.py
H_BADGE = 24
H_STATUS = 20                 # Quiet header status line (dot + plain text)
H_GHOST = 26                   # Borderless / text-only buttons
H_CONTROL = 28                 # Entries, dropdowns, segmented buttons
H_BUTTON = 32                  # Dialog action buttons
H_CTA = 40                     # Full width primary call to action

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
FONT_FAMILY = "Segoe UI"
FONT_KOREAN_FAMILY = "Malgun Gothic"  # Windows-native Korean glyphs stay crisp at small sizes.
FONT_DISPLAY = (FONT_FAMILY, 20, "bold")
FONT_HEADING = (FONT_FAMILY, 13, "bold")
FONT_TITLE = (FONT_FAMILY, 14, "bold")
FONT_BODY_MD = (FONT_FAMILY, 12, "normal")
FONT_BODY_SM = (FONT_FAMILY, 11, "bold")   # Small label headers
FONT_CAPTION = (FONT_FAMILY, 10, "normal")
FONT_LABEL = (FONT_FAMILY, 11, "normal")
FONT_KR_DISPLAY = (FONT_KOREAN_FAMILY, 20, "bold")
FONT_KR_TITLE = (FONT_KOREAN_FAMILY, 13, "bold")
FONT_KR_BODY = (FONT_KOREAN_FAMILY, 12, "normal")
FONT_KR_LABEL = (FONT_KOREAN_FAMILY, 11, "normal")

# Consolas ships with every supported Windows release, so this needs no
# runtime probe. It is a real fixed-width face, which keeps the "[HH:MM:SS]"
# timestamp and "[category]" columns of the log panel aligned. Hangul glyphs
# fall back per-character, which is expected and still reads correctly.
FONT_MONO_FAMILY = "Consolas"
FONT_MONO = (FONT_MONO_FAMILY, 11, "normal")
FONT_MONO_BOLD = (FONT_MONO_FAMILY, 11, "bold")

# Digital clock readout: tabular digits stop the display from jittering as the
# millisecond field changes.
FONT_CLOCK = (FONT_MONO_FAMILY, 22, "bold")
FONT_CLOCK_MS = (FONT_MONO_FAMILY, 14, "bold")
