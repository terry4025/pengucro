import customtkinter as ctk
import tkinter.font as tkfont

app = ctk.CTk()
FONT_MONO = ("Segoe UI", 11, "normal")

tb = ctk.CTkTextbox(app, font=FONT_MONO)
tb.pack()

# Setup custom apply_font_scaling hook
orig_apply_font_scaling = tb._apply_font_scaling

def custom_apply_font_scaling(font):
    scaled_font = orig_apply_font_scaling(font)
    
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
    
    return scaled_font

tb._apply_font_scaling = custom_apply_font_scaling

# Trigger initial update
tb._apply_font_scaling(tb._font)

# Apply scaling to 1.5x
print("Changing scaling to 1.5x")
ctk.set_widget_scaling(1.5)
app.update()

tb_tk_font = tkfont.Font(font=tb._textbox.cget("font"))
info_tk_font = tkfont.Font(font=tb._textbox.tag_cget("info", "font"))
bold_tk_font = tkfont.Font(font=tb._textbox.tag_cget("cat_bold", "font"))

print("Textbox actual font size (pixels):", tb_tk_font.actual("size"))
print("info tag actual font size (pixels):", info_tk_font.actual("size"))
print("cat_bold tag actual font size (pixels):", bold_tk_font.actual("size"))
