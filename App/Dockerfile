FROM python:3.10.21-alpine3.24

# Expose the Flask app port
EXPOSE 5000

# Set the working directory inside the container
WORKDIR /opt/app-root/src

# Install Flask and the Prometheus client (pinned)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code into the container (owned by the non-root user)
COPY --chown=1001:0 app.py ./
COPY --chown=1001:0 templates ./templates
COPY --chown=1001:0 static ./static

# `flask run` settings. Host defaults to loopback; Kubernetes overrides
# FLASK_RUN_HOST with the pod IP (see kube/app/deployment.yaml) so the app is never
# bound to 0.0.0.0.
ENV FLASK_APP=app \
    FLASK_RUN_HOST=127.0.0.1 \
    FLASK_RUN_PORT=5000

# Never run as root
USER 1001

# Set the default command to run the Flask app
CMD ["flask", "run"]
