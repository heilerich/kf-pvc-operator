FROM python:3.7-buster AS build-env
RUN pip3 install --upgrade pip
RUN pip3 install kopf kubernetes

FROM gcr.io/distroless/python3-debian10
WORKDIR /app
ENV PYTHONPATH=/usr/local/lib/python3.7/site-packages
COPY --from=build-env /usr/local/lib/python3.7/site-packages /usr/local/lib/python3.7/site-packages
COPY --from=build-env /usr/local/bin/kopf /usr/local/bin/kopf
ADD ./src /app
ENTRYPOINT ["/usr/bin/python", "/usr/local/bin/kopf", "run", "operator.py", "--all-namespaces"]
