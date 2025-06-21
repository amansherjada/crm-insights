# Use the official Python image from Docker Hub
FROM python:3.10.12-slim-bullseye

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies for ffmpeg and other tools
RUN apt-get update && \
    apt-get install -y ffmpeg curl build-essential && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file to the container
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code to the container
COPY main.py .

# Expose the port for FastAPI (Cloud Run expects 8080)
EXPOSE 8080

# Run the application using Gunicorn with Uvicorn worker
CMD ["gunicorn", "main:app", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8080", "--timeout", "1000"]
