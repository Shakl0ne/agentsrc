import os
import re

# Get all deleted png files from git
deleted_pngs = os.popen('git ls-tree -r HEAD~1 --name-only | grep "\.png"').read().splitlines()

# Get all current svg files
current_svgs = os.popen('find public -name "*.svg"').read().splitlines()
svg_basenames = {os.path.splitext(os.path.basename(f))[0] for f in current_svgs}

pngs_to_restore = []
for png in deleted_pngs:
    basename = os.path.splitext(os.path.basename(png))[0]
    if basename not in svg_basenames:
        pngs_to_restore.append(png)

# Restore the pngs
for png in pngs_to_restore:
    os.system(f'git checkout HEAD~1 -- "{png}"')

# Now find all markdown files and revert the .svg to .png if the svg doesn't exist
md_files = os.popen('find . -name "*.md"').read().splitlines()

for md_file in md_files:
    with open(md_file, 'r') as f:
        content = f.read()
    
    # Find all svg references
    def replace_svg_with_png(match):
        full_match = match.group(0)
        svg_path = match.group(1)
        basename = os.path.splitext(os.path.basename(svg_path))[0]
        if basename not in svg_basenames:
            return full_match.replace('.svg', '.png')
        return full_match

    new_content = re.sub(r'\[.*?\]\((.*?\.svg)\)', replace_svg_with_png, content)
    
    if new_content != content:
        with open(md_file, 'w') as f:
            f.write(new_content)

print(f"Restored {len(pngs_to_restore)} PNGs and updated markdown files.")
