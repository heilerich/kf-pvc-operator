FROM python:3.7-slim-buster
RUN pip3 install --upgrade pip
RUN pip3 install kopf kubernetes
WORKDIR /app
ADD ./src /app
ENTRYPOINT ["/usr/local/bin/kopf", "run", "operator.py", "--all-namespaces"]
