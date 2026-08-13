import sys
import os

# Emulate test_ablation environment
sys.path.insert(0, r"d:\WORK\knowledge-retrieval-engine\backend\src")

from src.config import settings as settings_src
from config import settings as settings_root

print("Are they the same object?", settings_src is settings_root)
