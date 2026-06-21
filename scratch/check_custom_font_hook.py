import customtkinter as ctk
import tkinter.font as tkfont

app = ctk.CTk()
FONT_MONO = ("Segoe UI", 11, "normal")

tb = ctk.CTkTextbox(app, font=FONT_MONO)
tb.pack()

# Setup custom font update hook
orig_update_font = tb._update_font

def custom_update_font():
    orig_update_font()
    
    scaled_font = tb._apply_font_scaling(tb._font)
    if isinstance(scaled_font, (tuple, list)):
        family = scaled_font[0]
        size = scaled_font[1]
    else:
        try:
            family = scaled_font.cget("family")
            size = scaled_font.cget("size")
        except Exception:
            family = "Segoe UI"
            size = -11
            
    font_normal = (family, size, "normal")
    font_bold = (family, size, "bold")
    
    tb._textbox.tag_config("info", font=font_normal, foreground="white")
    tb._textbox.tag_config("cat_bold", font=font_bold, foreground="blue")

tb._update_font = custom_update_font
custom_update_font()

# Apply scaling to 1.5x
ctk.set_widget_scaling(1.5)

tb_tk_font = tkfont.Font(font=tb._textbox.cget("font"))
info_tk_font = tkfont.Font(font=tb._textbox.tag_cget("info", "font"))
bold_tk_font = tkfont.Font(font=tb._textbox.tag_cget("cat_bold", "font"))

print("Textbox actual font size (pixels):", tb_tk_font.actual("size"))
print("info tag actual font size (pixels):", info_tk_font.actual("size"))
print("cat_bold tag actual font size (pixels):", bold_tk_font.actual("size"))
print("info tag family:", info_tk_font.actual("family"))
print("cat_bold tag weight:", bold_tk_font.actual("weight"))
