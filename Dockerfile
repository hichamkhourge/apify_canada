# Apify base image with Python + Google Chrome + Selenium preinstalled.
# https://hub.docker.com/r/apify/actor-python-selenium
FROM apify/actor-python-selenium:3.12

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
ENV HEADLESS=True \
    AUTO_EXIT=True \
    IPTVV_DEBUG_DIR=/tmp/iptvv-logs

# Run the Apify Actor entry point.
CMD ["python3", "-m", "src"]
