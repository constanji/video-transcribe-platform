# Deployment Assets

`images/` and `models/` are local-only payload directories for large Docker image
archives and model files. They are intentionally ignored by Git.

Server target: `/opt/plugin_parser` (compose project / network / volumes / containers
are all prefixed `plugin_parser` to avoid clashing with other services on the host).

## Layout on the server

```
/opt/plugin_parser/
├── releases/  uploaded source tarballs, one per version (pruned to the last 5)
├── src/       application source, unpacked from the newest tarball before each build
├── server/    docker-compose.yml + deploy.env (chmod 600) + scripts + release state
│              (.previous_tag, releases.log, deploy.env.bak)
├── images/    optional prebuilt image archives (api/web/moss.tar.gz)
└── models/    moss/OpenMOSS-Team/MOSS-Transcribe-Diarize/  +  summary/*.gguf
```

## Why releases work the way they do

The host cannot reach Docker Hub, and neither the host nor the developer machine can
rely on GitHub being reachable. Anything that assumes a hosted CI runner can SSH into
the host is unusable too, because the host is an internal address. So:

* **CI** (GitHub Actions) only validates the code: lint/compile, an import smoke test,
  a web typecheck and build, and a sanity check of the deployment descriptors.
* **CD** runs from a machine that can SSH to the host. The source is shipped over
  SFTP, the images are built *on the host* with `build-on-server.sh`, and each release
  gets an immutable tag. Only the SSH channel is required.

## Releasing (one command)

```bash
cd deployment-assets/server
./release.sh                       # full release of the current tree
RELEASE_SERVICES=api ./release.sh  # rebuild api only; web/moss are retagged
DRY_RUN=1 ./release.sh             # print the plan, change nothing
```

What it does, in order:

1. derive a version from `git describe`/short SHA plus a UTC timestamp
   (`<sha>-<YYYYMMDD>-<HHMMSS>`, `-dirty` when the tree has uncommitted changes);
   override with `VERSION=...`
2. pack the source (excluding venvs, caches, runtime data, models and image tarballs)
   together with a `BUILD_INFO` file recording the commit and dirty state
3. upload it to `/opt/plugin_parser/releases/`, unpack to `/opt/plugin_parser/src`
4. build `video-platform-{api,web,moss}:<version>` **on the host**
5. point `IMAGE_TAG` in `deploy.env` at the new tag (saving the old one to
   `.previous_tag`) and `docker compose up -d`
6. run `verify.sh`; **if it fails the release rolls itself back** to the previous tag
   and exits non-zero
7. append to `releases.log` and prune old tarballs (`KEEP_RELEASES=5`)

Useful knobs: `VERSION`, `RELEASE_SERVICES`, `DRY_RUN`, `VERIFY_TIMEOUT`,
`REQUIRE_CLEAN=1`, `KEEP_RELEASES`, `PRUNE_IMAGES=1` (removes superseded tags, which
also removes the ability to roll back to them), `APP_ROOT`, `PYTHON`.

```bash
./release.sh --rollback              # back to the previous tag
./release.sh --rollback=20261008     # back to a specific tag
```

Prerequisites on the machine running the release: a Python with `paramiko`
(`pip install paramiko`; declared as the `deploy` extra in `pyproject.toml`), or point
`PYTHON=...` at one. Connection settings come from `HOST`/`USER`/`PASSWORD`/`PORT`,
falling back to a git-ignored `tools/.remote.env` (copy `tools/.remote.env.example`).
No credentials are stored in the repository.

## Health gate

`verify.sh` runs on the host and is the gate for every release:

* all four containers running, and running the **tag that was just released**
* MOSS still has `/data` and `/models` mounted, api has `/data` (a missing shared
  media volume silently breaks every transcription)
* `/api/system/health` reports `database=connected`, `moss_reachable=true`, both model
  files present; the model *load* is only gated with `REQUIRE_MOSS_LOADED=1`
* web returns 200
* MOSS `/health` answers on loopback, and neither MOSS nor Postgres is published
  beyond the host / `plugin_parser_net`

Run it by hand any time: `cd /opt/plugin_parser/server && bash verify.sh`.

## Manual / fallback flows

Flow A — build on the server by hand:

1. Upload the source tree to `/opt/plugin_parser/src`, models to `/opt/plugin_parser/models`.
2. `cd /opt/plugin_parser/server && set -a && . ./deploy.env && set +a && \
   SRC=/opt/plugin_parser/src IMAGE_TAG=<tag> bash build-on-server.sh [api|web|moss]`
3. `docker compose --env-file deploy.env up -d && bash verify.sh`

Flow B — build locally, upload the images, load them:

1. `IMAGE_TAG=<tag> NEXT_PUBLIC_API_URL=http://<host>:18300 \
   bash deployment-assets/server/build-images.sh` → archives in `images/`
2. `SERVER_HOST=root@<host> deployment-assets/server/push-images.sh` uploads + `docker load`
3. `SERVER_HOST=root@<host> deployment-assets/server/deploy.sh` syncs descriptors and starts

## Continuous integration

`.github/workflows/ci.yml` runs on every push/PR:

| job | checks |
|---|---|
| `api` | dependencies install, `compileall`, import smoke test asserting the OpenAPI paths (including `/api/v1/meetings` and `/api/uploads`) |
| `web` | `npm ci`, `tsc --noEmit`, `next build` |
| `deployment` | `bash -n` on every script, `docker compose config -q`, and a guard that fails if payloads, secrets or files over 5 MB are ever tracked |

Two traps worth remembering:

* `pip install .` fails in this repo — the flat layout has several top-level
  directories, so setuptools auto-discovery aborts. The Docker build only works because
  it copies `pyproject.toml` into an empty directory first. CI installs the dependency
  list from `pyproject.toml` instead.
* Assert on `app.openapi()["paths"]`, not `app.routes`: FastAPI wraps an included router
  in a single `_IncludedRouter` entry, so `apps/api/meetings.py` routes never appear in
  `app.routes` individually.

## Notes

- `NEXT_PUBLIC_API_URL` is inlined into the web bundle at build time. Decide the API
  port BEFORE building the web image.
- Models are large (~4.6 GB) and are never synced automatically.
- Keep every image tag distinct rather than reusing `latest`, otherwise rollbacks have
  nothing to point at.
