FROM eclipse-temurin:21-jre-jammy

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Pre-bake the R5 jar (r5py 1.1.6 -> R5 v7.5.1) so startup needs no network.
RUN mkdir -p /opt/r5 \
 && curl -fL -o /opt/r5/r5.jar \
    https://github.com/r5py/r5/releases/download/v7.5.1-r5py/r5-v7.5.1-r5py-all.jar

COPY server.py .

ENV R5_JAR=/opt/r5/r5.jar \
    R5_MAX_MEMORY=6G \
    OSM_PBF=/data/sydney.osm.pbf \
    GTFS_ZIP=/data/gtfs-sydney.zip

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
