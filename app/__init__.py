import os

__version__ = "1.0.3"

# Set at image build time from the CI's GIT_COMMIT build arg (see Dockerfile); empty for local runs.
GIT_COMMIT = "" if os.environ.get("APP_GIT_COMMIT", "") in ("", "unknown") else os.environ["APP_GIT_COMMIT"]
REPO_URL = "https://github.com/ljmerza/minidsp-webui"
