# Deployment Assets

`images/` and `models/` are local-only payload directories for large Docker image
archives and model files. They are intentionally ignored by Git.

Server target: `/opt/plugin_parser` (compose project / network / volumes / containers
are all prefixed `plugin_parser` to avoid clashing with other services on the host).

## Layout on the server

```
/opt/plugin_parser/
├── src/      application source (built here when the server builds images)
├── server/   docker-compose.yml + deploy.env (chmod 600)
├── images/   optional prebuilt image archives (api/web/moss.tar.gz)
└── models/   moss/OpenMOSS-Team/MOSS-Transcribe-Diarize/  +  summary/*.gguf
```

## Flow A — build on the server (recommended when the local machine cannot reach PyPI/GitHub)

1. Upload the source tree to `/opt/plugin_parser/src` and models to
   `/opt/plugin_parser/models` (see `tools/remote.py put`).
2. On the server: `IMAGE_TAG=20261008 NEXT_PUBLIC_API_URL=http://<host>:18300 \
   bash deployment-assets/server/build-on-server.sh`
3. Copy `server/deploy.env.example` to `deploy.env`, fill secrets, `chmod 600`.
4. `cd /opt/plugin_parser/server && docker compose --env-file deploy.env up -d`

## Flow B — build locally, upload, load

1. `IMAGE_TAG=20261008 WEB_API_URL=http://<host>:18300 \
   bash deployment-assets/server/build-images.sh` → archives in `images/`.
2. `SERVER_HOST=root@<host> deployment-assets/server/push-images.sh` uploads + `docker load`.
3. `SERVER_HOST=root@<host> deployment-assets/server/deploy.sh` syncs descriptors and starts.

Notes:
- `NEXT_PUBLIC_API_URL` is inlined into the web bundle at build time. Decide the API
  port BEFORE building the web image.
- Models are large (~4.4 GB) and are never synced automatically.
