# Use a lightweight, modern Python base image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Install essential system dependencies
# (libgeos-dev is highly recommended for Shapely's C-extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Upgrade pip and install the Python dependencies
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Copy the backend and frontend code into the container
COPY main.py .
COPY static/ static/

# Create the necessary runtime directories
RUN mkdir -p uploads static/models

# Expose the port FastAPI runs on
EXPOSE 8000

# Command to run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]