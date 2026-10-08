# funread-api

`funread` 的后端服务，依赖 [funread](https://github.com/farfarfun/funread) 核心库并对外暴露 API，供
[funread-web](https://github.com/farfarfun/funread-web) 调用。搭配使用见
[funread-dev](https://github.com/farfarfun/funread-dev)。

## Install and run

Install a released version with `uv`:

```bash
uv tool install funread-api
```

The named CLI owns the server lifecycle:

```bash
funread-api server start
funread-api server status
funread-api server stop
funread-api server run --port 18811
funread-api server restart
```

The server listens on `127.0.0.1:18811` by default. `start` writes its PID and log to
`.run/funread-api.pid` and `.run/funread-api.log` below the current working directory;
`run` keeps the process in the foreground. Visit `/docs` for the API documentation and
`/healthz` for health checks.

For a repository checkout, `scripts/setup.sh` keeps installation separate from runtime:

```bash
scripts/setup.sh install-dev
scripts/setup.sh install-prod [version]
scripts/setup.sh publish
scripts/setup.sh start
scripts/setup.sh status
```

Runtime actions invoke only the installed `funread-api` CLI. The same CLI also provides
`upgrade [version]`, `rollback <version>`, and `uninstall`; the last action stops the
managed service before removing the package.

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
```
