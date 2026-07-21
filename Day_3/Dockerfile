# Start from a small, official Python image (has Python already installed)
FROM python:3.11-slim

# Everything from here on happens inside a folder called /app inside the box
WORKDIR /app

# Copy just the dependency list first, and install it.
# (Doing this before copying the rest of the code means Docker can reuse
# this step on future rebuilds if only your code changed, not your deps --
# faster rebuilds.)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the actual script into the box
COPY enterprise_multiagent.py .

# Default settings if you don't override them -- safe, offline, no cost
ENV MOCK=1
ENV LAB_STAGE=0

# Only matters when running in "serve" mode (Stage 5), but harmless otherwise
EXPOSE 8000

# What runs when the container starts, unless you tell it to do something else
CMD ["python", "enterprise_multiagent.py"]
