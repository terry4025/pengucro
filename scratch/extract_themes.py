import subprocess

try:
    content = subprocess.check_output(['git', 'show', 'ed5cbc1:data/themes.py']).decode('utf-8')
    with open('scratch/themes_old.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Success")
except Exception as e:
    print(f"Error: {e}")
