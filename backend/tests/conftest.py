import os
import pytest

# Ensure all tests run with ENVIRONMENT=test unless explicitly overridden
os.environ["ENVIRONMENT"] = "test"

from config import settings
settings.ENVIRONMENT = "test"
