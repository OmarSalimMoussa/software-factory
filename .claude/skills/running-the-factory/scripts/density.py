"""Print one tree's comment density, counted as the comment-budgets check counts it: `python3 density.py <checkout>
<ref or WORK> <tree>`, for example `density.py . WORK tests`. WORK reads the tracked files as they are on disk.

The last field is comment lines per thousand, rounded down, which is the number the ratchet compares.
"""

import subprocess
import sys

root, ref, tree = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, root)

from checks.standard.comment_budgets import file_counts, measured_file  # noqa: E402 - the checkout joins the path above


def text(rel: str) -> str:
    if ref == "WORK":
        with open(f"{root}/{rel}", encoding="utf-8") as f:
            return f.read()
    return subprocess.run(
        ["git", "-C", root, "show", f"{ref}:{rel}"], capture_output=True, text=True, check=True
    ).stdout


listing = ["ls-files", tree] if ref == "WORK" else ["ls-tree", "-r", "--name-only", ref, tree]
files = subprocess.run(["git", "-C", root, *listing], capture_output=True, text=True, check=True).stdout.split()
comments = lines = 0
for rel in filter(measured_file, files):
    counts = file_counts(rel, text(rel))
    comments += counts[2]
    lines += counts[3]
print(tree, ref, comments, lines, comments * 1000 // lines)
