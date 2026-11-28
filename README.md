# funread-api

`funread` 的后端服务，依赖 [funread](https://github.com/farfarfun-read/funread) 核心库并对外暴露 API，供
[funread-web](https://github.com/farfarfun-read/funread-web) 调用。搭配使用见
[funread-dev](https://github.com/farfarfun-read/funread-dev)。

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

## Casdoor 单点登录

在 Casdoor 为 Funread 创建独立的 Application，**Redirect URLs** 精确填写
`https://read.farfarfun.com/api/v1/auth/oidc/callback`。确认
`https://id.farfarfun.com/.well-known/openid-configuration` 返回的 `issuer`，
并记录 Application 的 Client ID、Client Secret 和统一管理员 ID Token 的 `sub`。
不要把用户名、邮箱或显示名填进 `admin_subject`。

在运行 API 的账号下编辑 `~/.farfarfun/funread/api/config.toml`：

```toml
[oidc]
issuer = "https://id.farfarfun.com"
client_id = "Funread Application 的 Client ID"
client_secret = "Funread Application 的 Client Secret"
redirect_uri = "https://read.farfarfun.com/api/v1/auth/oidc/callback"
admin_subject = "统一管理员 ID Token 的 sub"
timeout_seconds = 120
```

将域名和凭据替换为实际值，配置文件权限设为 `0600`，并通过 HTTPS 访问 Funread。
启动时 API 自动给已有的 `reader_user` 表补充 OIDC 身份列和唯一索引；数据库应先备份。
首次登录按 `(issuer, sub)` 新建独立的本地读者记录，书架、进度和订阅继续以本地
`user_id` 关联，不会按同名或邮箱合并旧账号。仅配置的管理员 `sub` 能访问管理端；
停用账号会使会话和设备凭据失效。

配置 OIDC 后，本地密码登录、注册、共享管理口令登录和密码换设备凭据入口关闭；
浏览器与 light 客户端从 `/web/login` 跳转 Casdoor。打包客户端在浏览器登录后，
到「我的设备」创建一次性显示的设备凭据，再在客户端的「连接服务器」页选择
「统一账号」并录入。设备凭据可在「我的设备」单独吊销。未配置 `[oidc]` 时原有
本地登录保持可用。

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
```

---

## 关于 farfarfun

[farfarfun](https://github.com/farfarfun) 是一个专注于实用工具库的开源组织，
涵盖云存储、数据处理、AI、多媒体与开发工具链等方向。

- 🏠 组织主页：<https://github.com/farfarfun>
- 📦 PyPI：<https://pypi.org/user/niuliangtao/>
- 📧 联系：farfarfun@qq.com

本项目基于 [MIT](LICENSE) 协议开源。
