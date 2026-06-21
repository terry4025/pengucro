# Sleek Minimalist Apple-style Dark Mode Design System

# Color Palette (OLED Black Canvas with iOS-style elevated surfaces)
CANVAS_COLOR = "#0A0A0C"       # Deep Space Black window background
SURFACE_COLOR = "#1C1C1E"      # Apple System Dark Gray (Form card container background)
ELEVATED_COLOR = "#2C2C2E"     # Apple Secondary Dark Gray (Input fields & OptionMenu background)
CARD_COLOR = "#3A3A3C"         # Apple Tertiary Dark Gray (Hover, focus, and selection background)
HAIRLINE_COLOR = "#38383A"     # Apple System Separator border (very thin, subtle contrast)

# Text Colors (iOS typography scale)
TEXT_PRIMARY = "#FFFFFF"       # Pure white for main text and headers
TEXT_BODY = "#E5E5EA"          # Apple primary label (off-white for reading comfort)
TEXT_MUTE = "#8E8E93"          # Apple secondary label (muted gray for secondary labels)
TEXT_DARK = "#000000"          # Black text for high-contrast white CTA buttons
TEXT_DISABLED = "#48484A"      # Apple placeholder text and disabled state

# Accent / Semantic Colors (Apple SF palette)
ACCENT_WHITE = "#FFFFFF"       # Primary white button
ACCENT_GREEN = "#30D158"       # Apple Success green (vibrant)
ACCENT_GREEN_HOVER = "#24B047" # Darker green for hover
ACCENT_RED = "#FF453A"         # Apple Destructive red (vibrant)
ACCENT_RED_HOVER = "#E03B30"   # Darker red for hover
ACCENT_YELLOW = "#FFD60A"      # Apple Alert/Warning yellow
ACCENT_YELLOW_HOVER = "#E0BC08" # Darker yellow for hover
ACCENT_BLUE = "#0A84FF"        # Apple Info blue / link color
ACCENT_BLUE_HOVER = "#0070E0"  # Darker blue for hover

# Rounded Corners Scale (Apple rounded corner hierarchy)
ROUNDED_SM = 6                 # Small controls, checkboxes
ROUNDED_MD = 10                # Buttons, input fields, dropdown buttons (classic Apple curvature)
ROUNDED_LG = 16                # Main cards, log panels, modal dialogs
ROUNDED_XL = 20                # Outer window containers (if applicable)

# Fonts (Sleek hierarchy: regular weights for text to avoid overlapping, bold for headers)
FONT_FAMILY = "Segoe UI"
FONT_DISPLAY = (FONT_FAMILY, 20, "bold") # Slightly smaller to prevent text clipping
FONT_HEADING = (FONT_FAMILY, 13, "bold")
FONT_BODY_MD = (FONT_FAMILY, 12, "normal") # Normal weight to prevent overcrowding and overlap
FONT_BODY_SM = (FONT_FAMILY, 11, "bold")   # Keep bold for small label headers
FONT_MONO = ("Segoe UI", 11, "normal")     # Clean sans-serif font for smooth logs without pointillism
