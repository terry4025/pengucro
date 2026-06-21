import customtkinter as ctk

app = ctk.CTk()
# Let's create two CTkFont objects: one normal, one bold, with the exact same family and size
font_family = "Segoe UI"
font_size = 11

font_normal = ctk.CTkFont(family=font_family, size=font_size, weight="normal")
font_bold = ctk.CTkFont(family=font_family, size=font_size, weight="bold")

tb = ctk.CTkTextbox(app, font=font_normal)
tb.pack()

# Configure tags
tb._textbox.tag_config("info", font=font_normal, foreground="white")
tb._textbox.tag_config("cat_default", font=font_normal, foreground="gray")
tb._textbox.tag_config("cat_bold", font=font_bold, foreground="blue")

tb.insert("end", "[00:41:31] ", "cat_default")
tb.insert("end", "[YesCaptcha] ", "cat_bold")
tb.insert("end", "This is normal body text\n", "info")

# Let's inspect the underlying Tkinter font configurations
print("Base font:", tb._textbox.cget("font"))
print("info tag font:", tb._textbox.tag_cget("info", "font"))
print("cat_default tag font:", tb._textbox.tag_cget("cat_default", "font"))
print("cat_bold tag font:", tb._textbox.tag_cget("cat_bold", "font"))

# Apply manual scaling change to simulate DPI change
ctk.set_widget_scaling(1.5)
print("After scaling 1.5x:")
print("Base font actual size:", font_normal.cget("size"))
print("Bold font actual size:", font_bold.cget("size"))
