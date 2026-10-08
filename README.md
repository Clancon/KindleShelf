# Kindle Shelf 本地传书台

Kindle Shelf 让旧款 Kindle 通过内置浏览器下载书籍和文章。电脑或家庭服务器负责上传、抓取、转换与保存记录；Kindle 打开轻量书架页面，点击下载后即可离线阅读，无需使用亚马逊个人文档库。

当前版本：**0.2.0**。Windows、Linux 和 Docker 使用同一套 Python 代码，数据库版本仍为 schema 1，不需要另建 Windows 版或 Linux 版。

| 运行方式 | 启动入口 | 适用场景 |
| --- | --- | --- |
| Windows 本机 | `start.bat` | 在电脑上整理书籍，供同一局域网中的 Kindle 下载 |
| Linux 本机 | `python app.py --no-browser` | 已有 Python 和 Calibre 的机器 |
| Linux 单容器 | `docker compose up -d --build` | 家庭服务器持续运行，重建容器后保留数据 |

## 功能

- 上传 EPUB、PDF、MOBI、AZW/AZW3、TXT、HTML、DOCX、RTF、CBZ。
- 抓取普通网页、微信公众号正文和图片；提取失败时可粘贴正文。
- 自动转换为 MOBI，提供不依赖 JavaScript 的 Kindle 下载页。
- 使用 SQLite 保存书目、阅读状态、进度、下载次数和事件历史。
- 统计未读、在读、已完成、暂不继续、完成率和月度完成次数。
- 分页浏览历史、导出完整历史、在线备份数据库。

Kindle 下载兼容性仍以实际设备和固件为准。第七代设备建议优先使用 MOBI 或 TXT；原厂旧浏览器无法直接下载 EPUB，也不能像手机一样安装普通应用。页面支持不依赖 JavaScript 的操作，但真实 Kindle 的下载、Cookie 和中文显示仍需在设备上验证。

## Windows 本机启动

先安装 **Python 3.10 或更新版本**和 **Calibre**，再双击 `start.bat`。首次启动创建 `.venv` 并按锁文件安装依赖，后续仅在锁文件变化时更新环境。程序默认打开电脑管理页：

```text
http://127.0.0.1:8090/
```

让 Kindle 与电脑连接同一局域网，在 Kindle 的“体验版网页浏览器”中打开首页显示的书架地址，例如：

```text
http://192.168.1.20:8090/kindle
```

将地址保存为浏览器书签。Windows 防火墙需要允许 Python 在所使用的私人网络接收连接。电脑需保持运行且不休眠；下载到 Kindle 的书籍可继续离线阅读。

可在 PowerShell 中指定地址或端口后启动：

```powershell
$env:KINDLE_SHELF_PUBLIC_URL = "http://192.168.1.20:8090"
.\start.bat --no-browser
```

更改端口时同时设置对应的访问地址：

```powershell
$env:KINDLE_SHELF_PUBLIC_URL = "http://192.168.1.20:8091"
.\start.bat --port 8091 --no-browser
```

Calibre 不在标准安装位置时，设置 `EBOOK_CONVERT` 为 `ebook-convert` 可执行文件的绝对路径。Windows 本机运行不会自动读取 `.env`；请使用环境变量或命令行参数。

## Linux 单容器常驻部署

服务器需安装 Docker Engine 和 Docker Compose 插件。克隆项目后，在项目目录准备配置和数据目录：

```bash
git clone git@github.com:Clancon/KindleShelf.git
cd KindleShelf
cp .env.example .env
mkdir -p data
sudo chown -R 10001:10001 data
```

编辑私有的 `.env`，将访问地址改成服务器的实际局域网地址：

```dotenv
KINDLE_SHELF_PUBLIC_URL=http://192.168.1.50:8090
KINDLE_SHELF_HOST_PORT=8090
KINDLE_SHELF_BIND_IP=0.0.0.0
KINDLE_SHELF_TIMEZONE=Asia/Shanghai
```

`PUBLIC_URL` 用于首页展示和 Host 校验。容器内部 IP 无法作为 Kindle 的稳定访问地址，部署时应显式配置；使用其他主机名访问时，可在 `KINDLE_SHELF_TRUSTED_HOSTS` 中用逗号追加主机名。URL 只能包含 `http(s)://主机:端口`，不能带账号、路径或查询参数。

启动并检查：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 kindle-shelf
```

访问地址：

```text
电脑管理页：http://192.168.1.50:8090/
Kindle 书架：http://192.168.1.50:8090/kindle
健康检查：http://192.168.1.50:8090/health
```

Compose 只运行一个容器，包含应用、Calibre 和 Noto 中文字体，使用 Tini 转发信号并回收子进程。应用以 UID/GID `10001:10001` 运行，因此绑定的 `data/` 必须对该用户可写。Compose 不会自动创建缺失的绑定目录；启动前需执行上述准备步骤。

`./data` 挂载到 `/app/data`，持久化数据库、书籍和会话密钥。重建、替换容器不会清空这些文件。重启策略为 `unless-stopped`；Docker 服务随服务器启动时，未被手动停止的容器会自动恢复。健康检查能报告异常，Docker 本身不会仅因 `unhealthy` 状态自动重启仍在运行的进程。

默认容器限制为 2 GiB 内存、2 个 CPU 和 256 个进程；大书转换需要更多资源时可调整 `.env` 中的 `KINDLE_SHELF_MEMORY_LIMIT`、`KINDLE_SHELF_CPU_LIMIT`、`KINDLE_SHELF_PIDS_LIMIT`。

升级代码：

```bash
git pull --ff-only
docker compose up -d --build
docker compose ps
```

当前版本依赖可信局域网，没有登录系统。需要从公网或不可信网络访问时，应先在反向代理上配置认证、HTTPS 和访问控制，并验证旧 Kindle 浏览器的兼容性。详见 [SECURITY.md](SECURITY.md)。

## Linux 本机运行

以 Debian/Ubuntu 为例，系统中的 Python 必须达到 3.10：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv calibre fonts-noto-cjk
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
KINDLE_SHELF_PUBLIC_URL=http://192.168.1.50:8090 \
  .venv/bin/python app.py --no-browser
```

应用默认监听 `0.0.0.0:8090`，可通过 `--host`、`--port` 或 `KINDLE_SHELF_HOST`、`KINDLE_SHELF_PORT` 修改。希望开机后持续运行时，建议使用上面的 Compose 部署。

## 阅读记录

新内容默认为“未读”。电脑管理页可填写状态、0–100% 进度和备注；Kindle 页面可点击“开始阅读”“标为完成”或“重新阅读”。

- 进度达到 100% 会标记为完成。
- 设为“未读”会清空当前进度及本轮开始、完成时间。
- 已完成内容重新阅读时，旧表单中的 100% 会按新一轮阅读重置；明确输入较低进度时保留该进度。
- 之前的完成事件仍保留在历史中，移除书籍也不会抹去这些事件。

原厂 Kindle 不会向本服务提供本地书籍的实际阅读页码、阅读时长或书内进度，因此阅读状态需要手动更新。下载次数记录服务器返回完整文件的 HTTP 200 请求；HEAD、Range、304 和失败请求不计入，传输中途断开也不能保证识别。下载次数不代表设备已读过该书。

当前完成率统计现存书架；月度图按 UTC 统计完成事件次数，包括重读和之后被移除的书。事件详情按 `KINDLE_SHELF_TIMEZONE` 展示，默认 `Asia/Shanghai`。

## 数据迁移、备份与恢复

默认数据目录固定在项目根目录的 `data/`，不会随启动时的当前目录变化，也可通过 `KINDLE_SHELF_DATA` 修改。

```text
data/
├── kindle_shelf.db     # SQLite：书目、状态、统计和历史
├── .session-secret    # 随机生成的私有会话密钥
├── books/             # Kindle 可下载文件
├── .trash/            # 从书架移除的文件
└── .working/          # 转换及备份临时文件
```

旧版 `data/library.json` 会在首次启动时原子迁入 SQLite，并保留原文件；非法记录会中止迁移。数据库存在时不会重复导入，未知或未来的数据库版本会被拒绝。

从 Windows 迁移到 Linux：停止原程序，复制**整个 `data/`** 到服务器项目目录，设置 `10001:10001` 所有权后启动 Compose。不要仅复制 `.db` 而遗漏书籍和会话密钥。

统计页提供事务一致的在线数据库备份和完整历史 JSON 导出。数据库备份不包含书籍、`.trash` 或 `.session-secret`；完整恢复应备份整个数据目录。冷备份示例：

```bash
docker compose stop
tar -czf kindle-shelf-data-$(date +%Y%m%d).tar.gz data/
docker compose start
```

恢复时先停止服务，将备份解压到单独目录并核对，再恢复完整 `data/`、调整所有权并启动。替换现有数据前先备份现状。

## 格式与网页抓取

| 输入 | 自动模式 |
| --- | --- |
| EPUB、HTML、DOCX、RTF、CBZ | 经 Calibre 转换为 MOBI |
| PDF、MOBI、AZW、AZW3、TXT | 保留原文件 |
| 网页链接、粘贴正文 | 生成 MOBI |

旧 Kindle 浏览器拒绝 AZW3 等格式时，应选择 MOBI。PDF 通常不适合小屏幕阅读，自动模式保留 PDF，不承诺重新排版效果。

网页抓取仅允许公网 HTTP(S) 的 80/443 端口，逐跳校验重定向及图片地址，并限制响应、解压和图片字节数。公众号优先识别 `js_content` 和延迟加载图片。登录、验证码、环境验证或失效页面会被拒绝；可在能够阅读原文的设备上复制正文，再用“粘贴正文”添加。

独立 HTML 导入会清理活动内容，只允许源目录内的栅格图片；其他复杂格式仍依赖 Calibre 解析，不构成针对任意恶意文件的完整沙箱。

## 配置与接口

`.env.example` 是 Compose 的无密钥模板，不含真实配置。默认会话密钥随机生成并保存在数据目录；`KINDLE_SHELF_SECRET` 可由私有环境变量覆盖。不要提交 `.env`、数据或凭据。

| 接口 | 行为 |
| --- | --- |
| `/health` | 就绪返回 200；Calibre 或数据库异常返回 503，并提供应用版本 |
| `/api/books` | 当前书架 JSON |
| `/api/stats` | 聚合统计 JSON |
| `/api/history?limit=100` | 历史 JSON，单页最多 500 条，返回 `next_cursor` |
| `/api/history?before_id=123&limit=100` | 获取游标之前的历史，可用 `book_id` 筛选 |
| `/export/history` | 流式导出全部历史，可用 `book_id` 筛选 |
| `/backup/database` | 下载事务一致的 SQLite 数据库备份 |

所有写表单需要会话内的 CSRF Token；Kindle 页面已包含隐藏字段，无需 JavaScript。Host 校验、CSRF 和私网抓取限制不等同于用户认证。

## 开发、测试与发布

运行依赖锁定在 `requirements.txt`；开发依赖锁定在 `requirements-dev.txt`。两者都包含精确版本与 SHA-256 哈希。

Windows：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.txt
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip_audit --require-hashes --disable-pip -r requirements.txt
```

Linux 使用 `.venv/bin/python` 执行相同检查。真实转换测试需要 Calibre；普通本机环境缺少它时测试会显式跳过，CI 则要求安装且检测到转换器。

有可用 Docker 引擎时还可执行：

```bash
python scripts/docker_smoke.py
```

该脚本只使用临时构建上下文和数据，检查私有文件排除、非 root 运行、健康状态、真实 MOBI、阅读历史、资源配置及容器重启后的持久化。没有 Docker 时会明确报错，不会把未执行记作通过。

GitHub Actions 对 Windows/Ubuntu、Python 3.10/3.12 执行回归，并单独执行依赖审计和 Linux Docker 测试。具体审查结果及已验证范围见 [docs/REVIEW.md](docs/REVIEW.md)。

发布前审查暂存文件，再检查：

```bash
git diff --cached --check
python scripts/check_publish.py
```

发布检查针对 Git 暂存区，拒绝数据、书籍、日志、真实环境配置和常见凭据格式，只输出文件名。它不能替代人工审查。项目沿用 Apache 2.0 许可证，见 [LICENSE](LICENSE)。
