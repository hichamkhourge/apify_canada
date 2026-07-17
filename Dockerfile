# Apify base image with Python + Google Chrome + Selenium preinstalled.
# https://hub.docker.com/r/apify/actor-python-selenium
FROM apify/actor-python-selenium:3.12

# libcairo2 is the native backend cairosvg needs to rasterize the IBO Player
# login captcha. The Debian-based Apify image doesn't ship it by default, and it
# runs as a non-root user, so switch to root for the apt install.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends libcairo2 \
 && rm -rf /var/lib/apt/lists/*
# Restore the image's non-root user for the dependency install and runtime.
USER myuser

# Install Python dependencies first to leverage Docker layer caching.
COPY requirements.txt ./
RUN echo "Python version:" \
 && python --version \
 && echo "Installing dependencies:" \
 && pip install --no-cache-dir -r requirements.txt \
 && echo "Installed packages:" \
 && pip freeze

# Copy the rest of the source (actor wrapper + automation module).
COPY . ./

# undetected-chromedriver downloads a matching driver at runtime into HOME.
# HEADLESS is intentionally False: the actor runs Chrome headed under an Xvfb
# virtual display (see the actor's xvfb-run launch command), which loads the
# proxy-auth extension reliably and is less bot-detectable than new-headless.
ENV HEADLESS=False \
    AUTO_EXIT=True \
    IPTVV_DEBUG_DIR=/tmp/iptvv-logs

# Run the Apify Actor entry point.
CMD ["python3", "-m", "src"]
