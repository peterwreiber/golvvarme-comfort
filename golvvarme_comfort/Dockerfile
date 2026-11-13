FROM public.ecr.aws/docker/library/python:3.12-alpine
ARG BUILD_VERSION=dev
ENV GOLVVARME_VERSION=$BUILD_VERSION
RUN apk add --no-cache tzdata
WORKDIR /app
COPY *.py ./
COPY static ./static
CMD ["python3", "-u", "/app/app.py"]
