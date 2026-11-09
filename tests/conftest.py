import os
import tempfile

# Point the app at a throw-away data folder and keep the weekly sync off
# before the app module is imported.
os.environ["BOKHYLLA_DATA_DIR"] = tempfile.mkdtemp(prefix="bokhylla-test-")
os.environ["BOKHYLLA_DISABLE_SCHEDULER"] = "1"
