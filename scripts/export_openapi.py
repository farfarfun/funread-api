#!/usr/bin/env python3
"""把 FastAPI 的 openapi 导出到 dev 仓库的两个功能文档包里。

两份而不是一份：document-bundle-standard 要求「每个 feature 一份主文档」，而
`reader-client` 与 `rss-subscription` 是两个 feature。按路径前缀切开，`components`
顺着 `$ref` 只留本文档用得上的那些 —— 两边都塞全套定义会让每份文档多出几十 KB
无人引用的 schema，审阅时分不清哪些是这个 feature 的契约。

路径按应用里的注册顺序写出，**不排序**：那个顺序是人读得懂的（账户→小说→书架），
而且重新导出时 diff 里只会出现真正变了的端点。

`info` 与 `servers` 是手写的（标题、SSRF 警告、反代说明），所以从目标文件里读回来
再写回去 —— 否则每次导出都会把它们抹成 FastAPI 的默认值。

用法（在 apps/funread-api 下）：

    PYTHONPATH=src:../funread/src python scripts/export_openapi.py

⚠️ 本机 funsecret 的 `funread/cache/source/db_url` 是生产 MySQL。这个脚本只建
app、不碰数据库，但 import 链会解析配置，所以照例先 export 一个 sqlite URL。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

import yaml

#: 每份文档收哪些路径前缀。顺序无关，一个路径只会进一份。
BUNDLES: Dict[str, List[str]] = {
    "reader-client": ["/api/v1/auth", "/api/v1/reader", "/api/v1/shelf", "/api/v1/pool"],
    "rss-subscription": ["/api/v1/rss"],
}

#: dev 仓库根。这个脚本在 apps/funread-api/scripts/ 下。
DEV_ROOT = Path(__file__).resolve().parents[3]


def _target(bundle: str) -> Path:
    return DEV_ROOT / "docs" / "development" / bundle / "openapi" / "001-openapi.yaml"


def _preserved_header(path: Path) -> Dict[str, Any]:
    """从现有文件里取回手写的 `info` / `servers`。"""
    if not path.exists():
        raise SystemExit(f"目标文件不存在，先建好再导出：{path}")
    existing = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {"info": existing["info"], "servers": existing["servers"]}


#: `$ref` 里的 schema 名。只认 `#/components/schemas/X` —— FastAPI 不生成别的形式，
#: 真出现了也宁可漏掉触发 KeyError，不要悄悄当成普通字符串放过去。
_SCHEMA_REF = re.compile(r"^#/components/schemas/(.+)$")


def _referenced_schemas(node: Any, schemas: Dict[str, Any]) -> Set[str]:
    """顺着 `$ref` 收集 `node` 直接或间接用到的所有 schema 名。

    要递归是因为引用是有链的：一个路径引 `SearchPage`，它的 `items` 引
    `SearchBook`，后者又引 `SourceRef`。只扫一层会导出一份自身就 `$ref` 不全的文档。
    """
    found: Set[str] = set()
    pending: List[Any] = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            ref = current.get("$ref")
            if isinstance(ref, str):
                matched = _SCHEMA_REF.match(ref)
                if matched and matched.group(1) not in found:
                    name = matched.group(1)
                    found.add(name)
                    #  入队的是被引 schema 的定义，于是链上后续的引用也会被走到。
                    pending.append(schemas[name])
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return found


def main() -> int:
    #  不改就会落到 funsecret 里的生产库上。只是建 app，但没必要赌。
    os.environ.setdefault("FUNREAD_DATABASE_URL", "sqlite:////tmp/funread-openapi-export.db")

    from funread_api.app import create_app

    spec = create_app().openapi()

    schemas = spec["components"]["schemas"]

    for bundle, prefixes in BUNDLES.items():
        path = _target(bundle)
        paths = {
            route: item
            for route, item in spec["paths"].items()
            if any(route.startswith(prefix) for prefix in prefixes)
        }
        if not paths:
            raise SystemExit(f"{bundle} 一个路径都没匹配到，前缀是不是改了？")
        used = _referenced_schemas(paths, schemas)
        components = {
            #  `securitySchemes` 这些非 schema 的部分整块带走，它们不大也无从裁剪。
            key: value
            for key, value in spec["components"].items()
            if key != "schemas"
        }
        components["schemas"] = {
            name: definition for name, definition in schemas.items() if name in used
        }
        document = {
            "openapi": spec["openapi"],
            **_preserved_header(path),
            "paths": paths,
            "components": components,
        }
        path.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )
        print(
            f"{path.relative_to(DEV_ROOT)}：{len(paths)} 条路径、"
            f"{len(components['schemas'])} 个 schema"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
